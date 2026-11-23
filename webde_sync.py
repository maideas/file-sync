#!/usr/bin/env python3
"""Safe two-way sync between a local folder and the web.de Online-Speicher
(or any other rclone remote), built on `rclone bisync`.

All synchronisation logic -- change detection against the previous run,
conflict handling, safety aborts, locking, backups, crash recovery -- is done
by rclone itself.  This wrapper only adds what bisync lacks for everyday use:

  * named profiles (TOML), so paths and filter rules are never retyped,
  * a preview of the planned changes (bisync --dry-run) and a confirmation,
  * safe defaults: backups of overwritten/deleted files, --max-delete guard,
    keep-both conflict handling, --resilient/--recover, a per-profile lock,
  * a fix for WebDAV servers without settable modification times such as
    web.de (--ignore-times, otherwise same-size edits are silently skipped),
  * a guided first run (--resync), a content check (--verify), readable logs.

Usage:
    webde_sync.py [options] PROFILE             # profile from profiles.toml
    webde_sync.py [options] LOCAL_DIR REMOTE    # ad-hoc, e.g. ~/Docs webde:/Docs
    webde_sync.py --list | --example-config

Arguments after `--` are passed to `rclone bisync` / `rclone check` unchanged
(e.g. `-- --tpslimit 4`), except for the flags this wrapper controls itself
(--force, --resync, --dry-run, ...), which are refused.  Requires Python >= 3.11
and rclone >= 1.66.
See README.md for setup.
"""

from __future__ import annotations

import argparse
import ast
import fcntl
import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

APP = "webde-sync"
MIN_RCLONE = (1, 66, 0)
BACKUP_DIR = ".webde-sync-backup"
STAMP_FMT = "%Y-%m-%d_%H%M%S"
LOCK_EXPIRY = "5m"       # bisync --max-lock (stale locks of our own runs are removed)
KEEP_LOGS = 30

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_ABORTED = 0, 1, 2, 3

EXAMPLE_CONFIG = """\
# webde_sync.py profiles -- one [section] per folder pair.
# Run a profile with:  webde_sync.py docs

[docs]
local  = "~/Documents"
remote = "webde:/Documents"       # rclone remote, see `rclone listremotes`

# rclone filter rules, first match wins: https://rclone.org/filtering/
# Changing them requires one `--resync` run afterwards (bisync's rule).
filters = [
  "- .git/**",
  "- .venv/**",
  "- *.tmp",
  "- ~$*",
]
# filters_file = "~/.config/webde-sync/docs.filters"   # appended to `filters`

# max_delete        = 50      # abort if more than this % of files would be deleted
# backups           = true    # keep replaced/deleted files in .webde-sync-backup/<time>/
# keep_backups_days = 30      # prune older backups after a successful run; 0 = keep all
# check_access      = false   # require an RCLONE_TEST file on both sides (mount guard)
# sync_empty_dirs   = false   # also create/delete empty directories
# conflict_resolve  = "none"  # none = keep both copies; or local, remote, larger, ...
# resync_mode       = "local" # winner for differing files in --resync: local, remote, ...
# rclone_flags      = ["--tpslimit", "4", "--transfers", "2"]
"""


class SyncError(Exception):
    """Fatal problem, reported as `error: ...` (exit code EXIT_FAIL)."""


class UsageError(SyncError):
    """Bad command line or configuration (exit code EXIT_USAGE)."""


def info(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr, flush=True)


def tilde(p: Path | str) -> str:
    """Show paths below $HOME as ~/... (shorter, and no user name in output)."""
    s, home = str(p), str(Path.home())
    return "~" + s[len(home):] if s == home or s.startswith(home + "/") else s


# --------------------------------------------------------------------- profiles

SIDE_ALIASES = {"local": "path1", "remote": "path2"}
CONFLICT_RESOLVE = ("none", "path1", "path2", "newer", "older", "larger", "smaller")
RESYNC_MODES = ("path1", "path2", "newer", "older", "larger", "smaller")
PROFILE_KEYS: dict[str, type] = {
    "local": str, "remote": str, "filters": list, "filters_file": str,
    "max_delete": int, "backups": bool, "keep_backups_days": int,
    "check_access": bool, "sync_empty_dirs": bool, "conflict_resolve": str,
    "resync_mode": str, "rclone_flags": list,
}


@dataclass
class Profile:
    name: str | None            # None for ad-hoc LOCAL_DIR REMOTE runs
    local: Path
    remote: str
    filters: list[str] = field(default_factory=list)
    filters_file: Path | None = None
    max_delete: int = 50
    backups: bool = True
    keep_backups_days: int = 30
    check_access: bool = False
    sync_empty_dirs: bool = False
    conflict_resolve: str = "none"
    resync_mode: str = "path1"
    rclone_flags: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.name or "(ad-hoc)"


def default_config_path() -> Path:
    if os.environ.get("WEBDE_SYNC_CONFIG"):
        return Path(os.environ["WEBDE_SYNC_CONFIG"]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP / "profiles.toml"


def default_state_home() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / APP


def load_config(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:      # incl. TOML syntax and non-UTF-8 bytes
        raise UsageError(f"cannot read config {tilde(path)}: {e}") from None
    for name, section in data.items():
        if not isinstance(section, dict):
            raise UsageError(f"{tilde(path)}: '{name}' must be a [section] (one per profile)")
    return data


def _choice(value: str, choices: tuple[str, ...], key: str, where: str) -> str:
    value = SIDE_ALIASES.get(value, value)
    if value not in choices:
        allowed = ", ".join(["local", "remote", *(c for c in choices if c not in SIDE_ALIASES.values())])
        raise UsageError(f"{where}: {key} = {value!r} is invalid (allowed: {allowed})")
    return value


def normalize_remote(remote: str) -> str:
    """webde:Docs/ and webde:Docs are the same folder (keep webde: and webde:/)."""
    stripped = remote.rstrip("/")
    return remote if stripped.endswith(":") or not stripped else stripped


def expand_path(value: str, key: str, where: str) -> Path:
    try:
        return Path(value).expanduser()
    except RuntimeError:                    # ~user of a user that does not exist
        raise UsageError(f"{where}: {key} = {value!r}: cannot resolve the home "
                         "directory in this path") from None


def profile_from_dict(name: str | None, raw: dict, where: str) -> Profile:
    for key, value in raw.items():
        if key not in PROFILE_KEYS:
            raise UsageError(f"{where}: unknown key {key!r} (known: {', '.join(PROFILE_KEYS)})")
        typ = PROFILE_KEYS[key]
        if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
            raise UsageError(f"{where}: {key} must be of type {typ.__name__}")
        if typ is list and not all(isinstance(v, str) for v in value):
            raise UsageError(f"{where}: {key} must be a list of strings")
    for key in ("local", "remote"):
        if not raw.get(key):
            raise UsageError(f"{where}: '{key}' is required")
    p = Profile(name=name, local=expand_path(raw["local"], "local", where),
                remote=normalize_remote(raw["remote"]))
    p.filters = list(raw.get("filters", []))
    if raw.get("filters_file"):
        p.filters_file = expand_path(raw["filters_file"], "filters_file", where)
    p.max_delete = raw.get("max_delete", p.max_delete)
    if not 0 <= p.max_delete <= 100:
        raise UsageError(f"{where}: max_delete is a percentage (0-100)")
    p.backups = raw.get("backups", p.backups)
    p.keep_backups_days = raw.get("keep_backups_days", p.keep_backups_days)
    if p.keep_backups_days < 0:
        raise UsageError(f"{where}: keep_backups_days must be >= 0")
    p.check_access = raw.get("check_access", p.check_access)
    p.sync_empty_dirs = raw.get("sync_empty_dirs", p.sync_empty_dirs)
    p.conflict_resolve = _choice(raw.get("conflict_resolve", "none"), CONFLICT_RESOLVE,
                                 "conflict_resolve", where)
    p.resync_mode = _choice(raw.get("resync_mode", "path1"), RESYNC_MODES, "resync_mode", where)
    p.rclone_flags = list(raw.get("rclone_flags", []))
    return p


# ----------------------------------------------------------------------- rclone

class Rclone:
    """Thin runner for the rclone binary with a private environment."""

    def __init__(self, binary: str):
        self.binary = binary
        self.env = dict(os.environ)
        self.pass_fds: tuple[int, ...] = ()   # the profile lock, inherited by rclone
        self.flags: list[str] = []            # user flags valid for every command

    def cmd(self, args: list[str]) -> list[str]:
        return [self.binary, *args]

    def run(self, args: list[str], check: bool = True,
            add_flags: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(self.cmd(args + (self.flags if add_flags else [])),
                              capture_output=True, text=True,
                              check=False,
                              stdin=subprocess.DEVNULL, env=self.env, pass_fds=self.pass_fds)
        if check and proc.returncode != 0:
            raise SyncError(f"`rclone {' '.join(args)}` failed (exit {proc.returncode}):\n"
                            f"{proc.stderr.strip()}")
        return proc

    def version(self) -> tuple[int, int, int]:
        out = self.run(["version"]).stdout
        m = re.search(r"rclone v(\d+)\.(\d+)\.(\d+)", out)
        if not m:
            raise SyncError(f"cannot determine the rclone version from: {out[:80]!r}")
        return int(m[1]), int(m[2]), int(m[3])


# Flags that only `rclone bisync` knows; other commands (lsf, check, ...) reject them.
BISYNC_VALUE_FLAGS = {"--backup-dir1", "--backup-dir2", "--check-filename", "--check-sync",
                      "--compare", "--conflict-loser", "--conflict-resolve", "--conflict-suffix",
                      "--filters-file", "--max-lock", "--resync-mode", "--workdir"}
BISYNC_BOOL_FLAGS = {"--check-access", "--create-empty-src-dirs", "--download-hash", "--force",
                     "--ignore-listing-checksum", "--max-delete-renames-aware", "--no-cleanup",
                     "--no-slow-hash", "--recover", "--remove-empty-dirs", "--resilient",
                     "--resync", "-1", "--slow-hash-sync-only"}


# rclone filter flags (the whole "Filter" group of `rclone help flags`; a test
# compares these sets with the rclone binaries): users may put them in
# rclone_flags / after `--` (bisync combines them with the --filters-file
# rules), but helper calls must not see them -- `purge` rejects filters
# outright, and they could hide entries from the lsf calls that check backups
# and emptiness.
FILTER_VALUE_FLAGS = {"--exclude", "--exclude-from", "--exclude-if-present", "--files-from",
                      "--files-from-raw", "--files-from0", "--filter", "-f", "--filter-from",
                      "--hash-filter", "--include", "--include-from", "--max-age",
                      "--max-depth", "--max-size", "--metadata-exclude",
                      "--metadata-exclude-from", "--metadata-filter", "--metadata-filter-from",
                      "--metadata-include", "--metadata-include-from", "--min-age", "--min-size"}
FILTER_BOOL_FLAGS = {"--delete-excluded", "--ignore-case"}
VALUE_FLAGS = BISYNC_VALUE_FLAGS | FILTER_VALUE_FLAGS      # the next argument is their value

# Flags this wrapper sets itself.  Passed through (rclone_flags / after `--`)
# they would come last on the rclone command line and silently override it:
# --force without a preview, a "sync" that is really a dry-run or a resync,
# state/backup/log locations the wrapper does not know about.
_FIXED = "it is set by webde_sync.py"
OWNED_FLAGS = {
    "--force": "use `webde_sync.py --force`, which shows a preview first",
    "--resync": "use `webde_sync.py --resync`", "-1": "use `webde_sync.py --resync`",
    "--resync-mode": "set `resync_mode` in the profile",
    "--dry-run": "use `webde_sync.py -n`", "-n": "use `webde_sync.py -n`",
    "--interactive": "webde_sync.py asks for confirmation itself",
    "-i": "webde_sync.py asks for confirmation itself",
    "--filters-file": "use `filters` / `filters_file` in the profile",
    "--backup-dir1": "backups are controlled by `backups` in the profile",
    "--backup-dir2": "backups are controlled by `backups` in the profile",
    "--workdir": _FIXED, "--log-file": _FIXED, "--use-json-log": _FIXED,
}
RE_SHORT_FLAGS = re.compile(r"[A-Za-z1]*")       # rclone's one-letter flags, e.g. -v -n -1


def split_short_flags(flags: list[str]) -> list[str]:
    """Spell out combined one-letter flags the way rclone reads them (-vn ->
    -v -n, -fRULE -> -f RULE), so the checks below see every flag.  Only used
    for those checks and the helper calls; bisync gets the user's own spelling."""
    out, is_value = [], False
    for f in flags:
        if is_value or f.startswith("--") or len(f) <= 2 or f[0] != "-" or f[2] == "=":
            out.append(f)
            is_value = not is_value and f in VALUE_FLAGS
            continue
        head, has_f, rule = f[1:].partition("f")        # -f is the only one with a value
        if RE_SHORT_FLAGS.fullmatch(head) and (has_f or not head.isdigit()):
            out += [f"-{c}" for c in head] + (["-f"] if has_f else []) + ([rule] if rule else [])
            is_value = bool(has_f) and not rule
        else:                                           # e.g. a stray value like -5d
            out.append(f)
    return out


def check_user_flags(flags: list[str]) -> None:
    """Refuse user-supplied rclone flags that this wrapper controls itself."""
    is_value = False
    for f in flags:
        if is_value:
            is_value = False
            continue
        name = f.split("=", 1)[0]
        if name in OWNED_FLAGS:
            hint = (f" (if {name} is the value of the flag before it, write that as "
                    "--flag=VALUE)") if len(name) == 2 else ""
            raise UsageError(f"rclone flag {name} is not accepted in rclone_flags or after "
                             f"`--`: {OWNED_FLAGS[name]}{hint}")
        is_value = f in VALUE_FLAGS


def _drop_flags(flags: list[str], value_flags: set[str], bool_flags: set[str]) -> list[str]:
    out, i = [], 0
    while i < len(flags):
        name = flags[i].split("=", 1)[0]
        n = 2 if flags[i] in VALUE_FLAGS else 1     # the flag and, if separate, its value
        if name not in value_flags and name not in bool_flags:
            out += flags[i:i + n]
        i += n
    return out


def general_flags(flags: list[str]) -> list[str]:
    """Drop bisync-only flags (and their values) from a user flag list."""
    return _drop_flags(flags, BISYNC_VALUE_FLAGS, BISYNC_BOOL_FLAGS)


def strip_filter_flags(flags: list[str]) -> list[str]:
    """Drop rclone filter flags (and their values) from a user flag list."""
    return _drop_flags(flags, FILTER_VALUE_FLAGS, FILTER_BOOL_FLAGS)


def find_rclone(explicit: str | None) -> Rclone:
    binary = explicit or os.environ.get("WEBDE_SYNC_RCLONE") or "rclone"
    path = shutil.which(binary)
    if path is None:
        raise SyncError(f"rclone not found ({binary}) -- install it, see README.md")
    rc = Rclone(path)
    ver = rc.version()
    if ver < MIN_RCLONE:
        raise SyncError(
            f"rclone v{'.'.join(map(str, ver))} is too old, bisync needs >= "
            f"v{'.'.join(map(str, MIN_RCLONE))}. Distribution packages are often older: "
            "install the current release from https://rclone.org/install/ (e.g. into "
            "~/.local/bin) or point --rclone / $WEBDE_SYNC_RCLONE at it.")
    return rc


def config_is_encrypted(rc: Rclone) -> bool:
    lines = rc.run(["config", "file"], check=False).stdout.strip().splitlines()
    try:
        with open(lines[-1], encoding="utf-8", errors="replace") as f:
            return "RCLONE_ENCRYPT_V0:" in f.read(8192)
    except (IndexError, OSError):
        return False


def ensure_config_password(rc: Rclone) -> None:
    """rclone's own password prompt would be invisible while we capture its
    output, so ask here (once) and hand the password to rclone via its env."""
    if not config_is_encrypted(rc):
        return
    if rc.env.get("RCLONE_CONFIG_PASS") or rc.env.get("RCLONE_PASSWORD_COMMAND"):
        return
    if not sys.stdin.isatty():
        raise SyncError("rclone.conf is encrypted: set RCLONE_PASSWORD_COMMAND (or "
                        "RCLONE_CONFIG_PASS) for non-interactive runs")
    try:
        rc.env["RCLONE_CONFIG_PASS"] = getpass.getpass("rclone config password: ")
    except EOFError:
        raise SyncError("no rclone config password entered") from None
    rc.env["RCLONE_ASK_PASSWORD"] = "false"   # fail instead of prompting invisibly
    proc = rc.run(["listremotes"], check=False)
    if proc.returncode != 0:
        raise SyncError(f"cannot unlock rclone.conf: {proc.stderr.strip()}")


RE_INLINE_SECRET = re.compile(r"[,:]\s*([A-Za-z_]*(?:pass|token|secret)[A-Za-z_]*|key)\s*=", re.IGNORECASE)


def check_remote_configured(rc: Rclone, remote: str) -> None:
    if RE_INLINE_SECRET.search(remote):
        raise UsageError("REMOTE contains credentials; they would end up in the output, logs "
                         "and state files -- configure them with `rclone config` instead")
    if ":" not in remote:
        raise UsageError(f"REMOTE {remote!r} is not an rclone remote path "
                         "(expected NAME:path, e.g. webde:/Documents)")
    if remote.startswith(":"):          # on-the-fly backend, e.g. :webdav,url=...:
        return
    name = remote.split(":", 1)[0].split(",", 1)[0]
    if "/" in name:
        raise UsageError(f"REMOTE {remote!r} looks like a local path, not NAME:path")
    configured = rc.run(["listremotes"]).stdout.split()
    if f"{name}:" not in configured:
        raise UsageError(f"rclone remote '{name}:' is not configured (have: "
                         f"{' '.join(configured) or 'none'}) -- run `rclone config`")


def plain_http_url(url: str) -> bool:
    """True for http:// URLs that leave this machine (password + files unencrypted)."""
    m = re.match(r"\s*http://(\[[^\]]*\]|[^/:]*)", url, re.IGNORECASE)
    return bool(m) and m[1].lower() not in ("localhost", "[::1]") \
        and not m[1].startswith("127.")


def warn_plain_http(rc: Rclone, remote: str) -> None:
    if remote.startswith(":"):
        urls = re.findall(r"url\s*=\s*['\"]?([^,'\"]+)", remote)
    else:
        # `config dump` is read in memory only; nothing of it is printed or stored
        proc = rc.run(["config", "dump"], check=False)
        try:
            section = json.loads(proc.stdout).get(remote.split(":", 1)[0].split(",", 1)[0], {})
        except (ValueError, AttributeError):
            section = {}
        urls = [section.get("url", "")]
    if any(plain_http_url(u) for u in urls):
        warn(f"{remote} uses plain http://: your password and all files travel unencrypted "
             "-- use https:// (web.de: https://webdav.smartdrive.web.de)")


def remote_dir_exists(rc: Rclone, remote: str) -> bool:
    proc = rc.run(["lsf", "--max-depth", "1", "--dirs-only", remote], check=False)
    if proc.returncode == 0:
        return True
    if proc.returncode == 3:            # rclone's exit code for "directory not found"
        return False
    raise SyncError(f"cannot access {remote}:\n{proc.stderr.strip()}")


def remote_join(root: str, sub: str) -> str:
    return root + sub if root.endswith((":", "/")) else f"{root}/{sub}"


# ------------------------------------------------------------------------ state

@dataclass
class State:
    root: Path

    @property
    def workdir(self) -> Path:
        return self.root / "bisync"

    @property
    def filters(self) -> Path:
        return self.root / "filters.txt"

    @property
    def baseline(self) -> Path:
        return self.root / "baseline.json"

    @property
    def logs(self) -> Path:
        return self.root / "logs"


def state_for(profile: Profile, state_home: Path) -> State:
    if profile.name:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", profile.name)
        key = f"{safe}-{hashlib.sha256(profile.name.encode()).hexdigest()[:8]}"
    else:
        key = "adhoc-" + hashlib.sha256(
            f"{profile.local}\0{profile.remote}".encode()).hexdigest()[:16]
    st = State(state_home / key)
    for d in (state_home, st.root, st.workdir, st.logs):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)        # listings reveal the whole file inventory
    return st


class ProfileLock:
    """Exclusive per-profile lock, held for the whole run (preview + sync).

    The lock descriptor is inherited by every rclone child, so the lock stays
    held even if this script dies while rclone keeps running.  Holding it
    therefore proves that no bisync of this profile is active."""

    def __init__(self, path: Path):
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            raise SyncError("another webde_sync run for this profile is in progress") from None

    def release(self) -> None:
        os.close(self.fd)


def is_rclone_bisync(pid: int) -> bool:
    """Whether PID is a running `rclone bisync` (a reused PID of another program is not)."""
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return bool(argv) and b"rclone" in Path(os.fsdecode(argv[0])).name.encode() \
        and b"bisync" in argv[1:]


def clear_stale_bisync_locks(st: State) -> None:
    """Remove bisync lock files left behind by killed runs (we hold the profile lock)."""
    for lck in st.workdir.glob("*.lck"):
        try:
            pid = int(json.loads(lck.read_text(encoding="utf-8")).get("PID", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            pid = 0
        if pid > 0 and is_rclone_bisync(pid):
            raise SyncError(f"rclone bisync (PID {pid}) still holds the lock {tilde(lck)} "
                            "-- wait for it to finish")
        lck.unlink(missing_ok=True)
        warn("removed a stale bisync lock left by an interrupted run")


def escape_glob(s: str) -> str:
    return re.sub(r"([\\*?\[\]{}])", r"\\\1", s)


def render_filters(profile: Profile, st: State) -> str:
    lines = ["# generated by webde_sync.py -- edit the profile instead",
             f"- /{BACKUP_DIR}/**"]                    # must come first: first match wins
    try:
        rel = st.root.resolve().relative_to(profile.local)
        lines.append(f"- /{escape_glob(rel.as_posix())}/**")   # never sync our own state
    except ValueError:
        pass
    lines += profile.filters
    if profile.filters_file:
        try:
            lines += profile.filters_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as e:
            raise UsageError(f"cannot read filters_file: {e}") from None
    return "\n".join(lines) + "\n"


def write_if_changed(path: Path, text: str) -> None:
    if not path.is_file() or path.read_text(encoding="utf-8") != text:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


def clear_stale_listings(st: State) -> None:
    """Drop bisync's listing files before a confirmed --resync rebuilds them --
    including leftovers of an older path pair (local/remote changed in the
    profile), which the *.path1.lst globs below would otherwise still see."""
    for lst in st.workdir.glob("*.lst*"):
        try:
            lst.unlink()
        except OSError:
            pass


def baseline_problem(profile: Profile, st: State, filters_text: str) -> str | None:
    """Why a --resync is needed, or None if the last resync is still valid."""
    try:
        data = json.loads(st.baseline.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "this is the first sync of this profile"
    except (OSError, ValueError):
        return "the baseline record is unreadable"
    if data.get("local") != str(profile.local) or data.get("remote") != profile.remote:
        return "the local or remote path changed since the last resync"
    if data.get("filters_sha256") != hashlib.sha256(filters_text.encode()).hexdigest():
        return "the filter rules changed since the last resync"
    if not any(st.workdir.glob("*.path1.lst")):
        return "bisync's listings from the last run are missing"
    return None


def listing_is_empty(st: State) -> bool:
    """True if bisync's local listing (written by the last run) holds no files."""
    for lst in st.workdir.glob("*.path1.lst"):
        try:
            lines = lst.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if any(line.strip() and not line.startswith("#") for line in lines):
            return False
    return True


def local_has_files(rc: Rclone, profile: Profile, st: State) -> bool:
    proc = rc.run(["lsf", "-R", "--files-only", "--filter-from", str(st.filters),
                   *strip_filter_flags(rc.flags), str(profile.local)],
                  check=False, add_flags=False)
    return proc.returncode != 0 or bool(proc.stdout.strip())


NOTHING_YET = ("Nothing to sync yet (both sides are empty, or the filters exclude every "
               "file),\nso no baseline was created. Add files on either side and run "
               "again; that run does the first sync.")


def write_baseline(profile: Profile, st: State, filters_text: str, ver: tuple) -> None:
    data = {"local": str(profile.local), "remote": profile.remote,
            "filters_sha256": hashlib.sha256(filters_text.encode()).hexdigest(),
            "rclone": ".".join(map(str, ver)),
            "resync_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    write_if_changed(st.baseline, json.dumps(data, indent=1) + "\n")


# ------------------------------------------------------------- bisync & report

def bisync_args(profile: Profile, st: State, *, resync: bool, dry_run: bool, force: bool,
                stamp: str, extra: list[str]) -> list[str]:
    a = ["bisync", str(profile.local), profile.remote,
         "--workdir", str(st.workdir), "--filters-file", str(st.filters),
         "--resilient", "--recover", "--max-lock", LOCK_EXPIRY,
         "--max-delete", str(profile.max_delete),
         "--conflict-resolve", profile.conflict_resolve, "--color", "NEVER",
         # one attempt per run: with --ignore-times a retry re-sends *every* queued
         # file; the next run retries anyway (--resilient), single requests are still
         # retried (--low-level-retries). rclone_flags can override this.
         "--retries", "1"]
    if resync:
        a += ["--resync-mode", profile.resync_mode]
    else:
        # bisync detects same-size edits (via local mtimes), but the copy step
        # then compares sizes only when the remote cannot store mtimes (web.de
        # WebDAV) and would skip them -- force the transfers bisync decided on.
        a.append("--ignore-times")
        if profile.check_access:
            a.append("--check-access")
    # A dry-run moves nothing, but with backup dirs set rclone reports every file it
    # would overwrite ("skipped: move") -- so the preview can count replacements
    # even when backups are off for the real run.
    if profile.backups or dry_run:
        a += ["--backup-dir1", str(profile.local / BACKUP_DIR / stamp),
              "--backup-dir2", remote_join(profile.remote, f"{BACKUP_DIR}/{stamp}")]
    if profile.sync_empty_dirs:
        a.append("--create-empty-src-dirs")
    if force:
        a.append("--force")
    if dry_run:
        a.append("--dry-run")
    return a + profile.rclone_flags + extra


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
RE_DETECT = re.compile(r"^- (Path[12])\s+(File is new|File was deleted|File changed:.*?)\s+- (.+)$")
RE_QCOPY = re.compile(r"^- (Path[12])\s+Queue copy to (Path[12])\s+- (.+)$")
RE_QDEL = re.compile(r"^- (Path[12])\s+Queue delete\s+- (.+)$")
RE_BOTH = re.compile(r"^- WARNING\s+New or changed in both paths\s+- (.+)$")
RE_RESYNC_DIR = re.compile(r"^- (Path[12])\s+Resync is copying files to\s+- (Path[12])")
RE_COPIED = re.compile(r"^Copied \((?:new|replaced existing)\)")
RE_LOCKED = re.compile(r"^(?:\d{4}/\d\d/\d\d \d\d:\d\d:\d\d )?Failed to bisync"
                       r"(?: with \d+ errors: last error was)?: prior lock file found")
RE_CONFLICT_COPY = re.compile(r"^(.+)\.conflict\d+$")
RE_FAILED = re.compile(r"^.+?: Failed to [a-z ]+: (.+)$")
RE_SUMMARY = re.compile(r"^Path[12]:\s+(\d+) changes:")
RE_STATS_COUNT = re.compile(r"^Transferred:\s+(\d+) / \d+,", re.MULTILINE)   # file count, not bytes
IGNORED_WARNINGS = ("bisync is IN BETA", "Modtime compare was requested")


@dataclass
class Report:
    """What bisync planned (dry-run) or did (real run), parsed from its log."""
    upload: dict[str, str] = field(default_factory=dict)       # path -> reason
    download: dict[str, str] = field(default_factory=dict)
    delete_remote: list[str] = field(default_factory=list)
    delete_local: list[str] = field(default_factory=list)
    # original path -> [(conflict copy, "local"/"server" = whose version it holds)]
    conflicts: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)
    replaced: set[str] = field(default_factory=set)   # existing files that get overwritten
    stats_transfers: int = 0      # file count from bisync's own statistics
    symlinks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: str = ""
    plan_lines: list[str] = field(default_factory=list)   # bisync's own wording
    bisync_changes: int = 0       # from bisync's "PathN: X changes" summary
    no_changes: bool = False      # bisync said "No changes found"
    success: bool = False
    needs_resync: bool = False
    lock_error: bool = False
    safety_abort: bool = False
    empty_side: bool = False
    empty_baseline: bool = False  # bisync cannot continue from an empty prior listing
    failed: dict[str, str] = field(default_factory=dict)   # path -> reason
    interrupted: bool = False

    @property
    def n_actions(self) -> int:
        return (len(self.upload) + len(self.download) + len(self.delete_remote)
                + len(self.delete_local) + len(self.conflicts))


def unquote(path: str) -> str:
    """bisync prints names containing quotes or backslashes Go-quoted ("a\\"b")."""
    if len(path) >= 2 and path[0] == path[-1] == '"':
        for parse in (json.loads, ast.literal_eval):
            try:
                value = parse(path)
            except (ValueError, SyntaxError):
                continue
            if isinstance(value, str):      # '"a","b"' is a tuple, not a quoted name
                return value
    return path


def _relativizer(local: Path, remote: str):
    prefixes = {"Path1": [str(local).rstrip("/") + "/"],
                "Path2": sorted({remote if remote.endswith((":", "/")) else remote + "/",
                                 remote + "/", remote.rstrip("/") + "/"}, key=len, reverse=True)}

    def rel(side: str, path: str) -> str:
        path = unquote(path)
        for pre in prefixes[side]:
            if path.startswith(pre):
                return path[len(pre):]
        return path
    return rel


@dataclass
class Msg:
    """One rclone log entry; `text` is "object: msg" without ANSI codes."""
    level: str
    text: str
    obj: str = ""
    size: int | None = None       # exact bytes (msg itself shows e.g. "48.828Ki")
    skipped: str = ""             # dry-run action: copy, move (= backup), ...
    msg: str = ""                 # rclone's own wording, without the object name


def log_messages(lines) -> list[Msg]:
    """Entries of rclone --use-json-log lines; non-JSON lines count as errors."""
    out = []
    for line in lines:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        try:
            o = json.loads(line)
            msg, obj = ANSI.sub("", str(o.get("msg", ""))), str(o.get("object") or "")
            size = o.get("size")
            out.append(Msg(str(o.get("level", "")), f"{obj}: {msg}" if obj else msg, obj,
                           size if isinstance(size, int) else None, str(o.get("skipped") or ""),
                           msg))
        except (ValueError, AttributeError):
            out.append(Msg("raw", ANSI.sub("", line), msg=ANSI.sub("", line)))
    return out


def parse_report(messages: list[Msg], local: Path, remote: str) -> Report:
    rep, rel = Report(), _relativizer(local, remote)
    detect: dict[tuple[str, str], str] = {}
    both: set[str] = set()
    copies: list[tuple[str, str]] = []          # (target side, rel path)
    resync_target = None
    for entry in messages:
        level, text = entry.level, entry.text.strip()
        # Files can be *named* like bisync's messages ("Failed to bisync.txt", "- Path2
        # Queue delete - x"), and names show up as the object and at the end of plan
        # lines.  So match rclone's own wording only, anchored at its start: `own` is
        # the message without the object name, `plain` a message that has no object
        # at all -- which is how bisync logs its plan and status lines.
        own = entry.msg.strip()
        plain = "" if entry.obj else own
        if entry.skipped and entry.obj:               # dry-run, structured fields
            if entry.skipped == "copy":
                if entry.size is not None:
                    rep.sizes[entry.obj] = entry.size
                if resync_target:
                    copies.append((resync_target, entry.obj))
            elif entry.skipped == "move":             # old version moved to the backup
                rep.replaced.add(entry.obj)
        elif m := RE_DETECT.match(plain):
            kind = m[2]
            detect[(m[1], unquote(m[3]))] = ("new" if kind == "File is new" else
                                    "deleted" if kind == "File was deleted" else
                                    kind.replace("File ", "", 1))
        elif m := RE_QCOPY.match(plain):
            copies.append((m[2], rel(m[2], m[3])))
        elif m := RE_QDEL.match(plain):
            (rep.delete_remote if m[1] == "Path2" else rep.delete_local).append(rel(m[1], m[2]))
        elif m := RE_BOTH.match(plain):
            both.add(unquote(m[1]))
        elif m := RE_RESYNC_DIR.match(plain):
            resync_target = m[2]
        elif entry.obj and resync_target and RE_COPIED.match(own):
            copies.append((resync_target, entry.obj))
        elif entry.obj and own.startswith("Can't follow symlink") \
                and entry.obj not in rep.symlinks:
            rep.symlinks.append(entry.obj)
        if m := RE_SUMMARY.match(plain):
            rep.bisync_changes += int(m[1])
        if plain.startswith("- ") and "Do queued copies" not in plain:
            rep.plan_lines.append(plain)
        if plain == "No changes found":
            rep.no_changes = True
        if plain == "Bisync successful":
            rep.success = True
        if plain.startswith("Bisync aborted. Must run --resync"):
            rep.needs_resync = True
        if RE_LOCKED.match(plain):
            rep.lock_error = True
        # logged as object "Safety abort" + "too many deletes ...", or as one message
        if plain.startswith("Safety abort: ") or (entry.obj == "Safety abort"
                                                  and own.startswith("too many deletes")):
            rep.safety_abort = True
        if plain.startswith("Empty current Path"):
            rep.empty_side = True
        if plain.startswith("Empty prior Path"):
            rep.empty_baseline = True
        if "Transferred:" in plain and "Elapsed time" in plain:
            rep.stats = entry.msg.strip("\n")
            if m := RE_STATS_COUNT.search(rep.stats):
                rep.stats_transfers = int(m[1])
        if level in ("error", "critical", "raw") or plain.startswith("Failed to bisync"):
            if text not in rep.errors:            # rclone repeats them per retry
                rep.errors.append(text)
            if entry.obj and (m := RE_FAILED.match(text)):
                rep.failed.setdefault(entry.obj, m[1].rsplit(": ", 1)[-1])
        elif level in ("warning", "notice") and not RE_BOTH.match(plain) \
                and not any(w in own for w in IGNORED_WARNINGS) \
                and (plain.startswith("- WARNING") if plain.startswith("- ")
                     else "WARNING" in own):
            rep.warnings.append(text)
    copies = list(dict.fromkeys(copies))           # dedupe, keep order
    for target, path in copies:
        if (m := RE_CONFLICT_COPY.match(path)) and m[1] in both:
            # a renamed copy queued to Path2 is the local version and vice versa
            origin = "local" if target == "Path2" else "server"
            rep.conflicts.setdefault(m[1], []).append((path, origin))
    for target, path in copies:
        if path in rep.conflicts or ((m := RE_CONFLICT_COPY.match(path)) and m[1] in both):
            continue        # part of a conflict: the winner's copy is no separate change
        src, other = ("Path1", "Path2") if target == "Path2" else ("Path2", "Path1")
        reason = detect.get((src, path), "resync" if resync_target else "changed")
        if detect.get((other, path)) == "deleted":
            reason = ("restored: deleted on the server but changed locally" if src == "Path1"
                      else "restored: deleted locally but changed on the server")
        (rep.upload if target == "Path2" else rep.download)[path] = reason
    rep.replaced -= rep.conflicts.keys()     # a conflict loser is renamed, not replaced
    return rep


def human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if f < 1024:
            return f"{int(f)} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TiB"


def conflict_text(path: str, copies: list[tuple[str, str]]) -> str:
    """Describe where both versions of a conflicting file ended up."""
    names = ", ".join(f"{Path(c).name} = {origin} version" for c, origin in sorted(copies))
    if len(copies) == 1:                    # resolved: the winner keeps the original name
        winner = "server" if copies[0][1] == "local" else "local"
        return f"changed on both sides: {Path(path).name} = {winner} version, {names}"
    return f"changed on both sides, both kept: {names}"


def show_report(rep: Report, max_show: int | None, planned: bool) -> None:
    verb = ("upload to server", "download to local", "delete on server", "delete locally") \
        if planned else ("uploaded", "downloaded", "deleted on server", "deleted locally")
    sections = [(verb[0], list(rep.upload.items())), (verb[1], list(rep.download.items())),
                (verb[2], [(p, "deleted locally") for p in rep.delete_remote]),
                (verb[3], [(p, "deleted on the server") for p in rep.delete_local]),
                ("conflict", [(p, conflict_text(p, c)) for p, c in rep.conflicts.items()])]
    if rep.n_actions == 0:
        info("  no changes")
    for title, items in sections:
        if not items:
            continue
        items.sort()
        size = sum(rep.sizes.get(p, 0) for p, _ in items)
        size_txt = f"   {human(size):>10}" if size and planned else ""
        info(f"  {title + ':':<22}{len(items):>6} file(s){size_txt}")
        limit = len(items) if max_show is None else max_show
        for p, why in items[:limit]:
            sz = f", {human(rep.sizes[p])}" if p in rep.sizes and planned else ""
            info(f"      {p}   [{why}{sz}]")
        if len(items) > limit:
            info(f"      ... and {len(items) - limit} more (use --list-all)")
    if rep.symlinks:
        info(f"  skipped symlinks:     {len(rep.symlinks):>6} (rclone does not follow them)")
    for w in rep.warnings:
        warn(w)


def pending_conflicts(profile: Profile, st: State) -> list[str]:
    """Conflict copies (*.conflictN) from bisync's last local listing that still
    exist -- reuses bisync's listing instead of scanning the folder again."""
    found = set()
    for lst in st.workdir.glob("*.path1.lst"):
        try:
            lines = lst.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if ".conflict" not in line or '"' not in line:
                continue
            path = unquote(line[line.index('"'):])     # last field: Go-quoted path
            if RE_CONFLICT_COPY.match(path) and (profile.local / path).exists():
                found.add(path)
    return sorted(found)


def remind_conflicts(profile: Profile, st: State, max_show: int | None) -> None:
    pending = pending_conflicts(profile, st)
    if not pending:
        return
    copies = "1 conflict copy waits" if len(pending) == 1 else f"{len(pending)} conflict copies wait"
    lines = [f"warning: {copies} to be merged (the file was changed on both sides):"]
    limit = len(pending) if max_show is None else max_show
    lines += [f"    {p}" for p in pending[:limit]]
    if len(pending) > limit:
        lines.append(f"    ... and {len(pending) - limit} more (use --list-all)")
    lines.append("  Merge each into its original file, then delete the .conflictN copy -- "
                 "until then the copies are synced like normal files.")
    print("\n".join(lines), file=sys.stderr, flush=True)


def show_backups(rc: Rclone, profile: Profile, stamp: str, max_show: int | None) -> None:
    """List what this run moved into the backup folders (on both sides)."""
    if not profile.backups:
        return
    found = []
    for side, root in (("local ", str(profile.local)), ("server", profile.remote)):
        bdir = remote_join(root, f"{BACKUP_DIR}/{stamp}")
        proc = rc.run(["lsf", "-R", "--files-only", *strip_filter_flags(rc.flags),
                       bdir], check=False, add_flags=False)
        files = sorted(proc.stdout.splitlines()) if proc.returncode == 0 else []
        if files:
            found.append((side, tilde(bdir) + "/", files))
    if not found:
        return
    info("Backups of replaced/deleted files (copy a file back to restore it):")
    for side, bdir, files in found:
        info(f"  {side}: {len(files)} file(s) in {bdir}")
        limit = len(files) if max_show is None else max_show
        for f in files[:limit]:
            info(f"      {f}")
        if len(files) > limit:
            info(f"      ... and {len(files) - limit} more (use --list-all)")


def advise(rep: Report, profile: Profile, st: State, resync: bool = False) -> None:
    """Explain failed runs in terms of what the user can do next."""
    for e in rep.errors:
        print(f"rclone: {e}", file=sys.stderr)
    name = profile.name or f"{tilde(profile.local)} {profile.remote}"
    if rep.failed:
        print("\nThese files could not be transferred:", file=sys.stderr)
        for path, why in sorted(rep.failed.items()):
            print(f"    {path}  ({why})", file=sys.stderr)
        print("  Fix the cause (permissions, a file in use, ...) or exclude the files with "
              "a filter rule.", file=sys.stderr)
    text = " ".join(rep.errors).lower()
    if "not a directory" in text or "is a directory" in text:
        print("\nA file on one side has the same name as a folder on the other side. "
              "Rename one of them.", file=sys.stderr)
    if "insufficient storage" in text or " 507" in text or "quota" in text:
        print("\nThe web.de storage seems to be full: free space there (old backups are in "
              f"{remote_join(profile.remote, BACKUP_DIR)}/) or upgrade the plan.",
              file=sys.stderr)
    if rep.safety_abort:
        print(f"\nbisync's safety check stopped the sync (more than {profile.max_delete}% "
              "deletions or all files changed). If that is really intended, review it with:\n"
              f"  webde_sync.py {name} --force", file=sys.stderr)
    if rep.empty_side:
        print("\nOne side is completely empty, so bisync refused to sync (this protects "
              "against an\nunmounted disk or a wrong path wiping the other side). Check "
              f"that {tilde(profile.local)} and\n{profile.remote} are the right folders. "
              "To really empty both, delete the files on the\nother side by hand.",
              file=sys.stderr)
    if rep.lock_error:
        print("\nAnother `rclone bisync` (not started by webde_sync) uses this profile's "
              f"state in {tilde(st.workdir)}. Let it finish and try again.", file=sys.stderr)
    if rep.empty_baseline:
        st.baseline.unlink(missing_ok=True)
        print(f"\nThe baseline was made while both sides were empty, and bisync cannot "
              f"continue from that\n(despite its message, retrying does not help). The next "
              f"run of `webde_sync.py {name}` previews a\nfresh first sync instead.",
              file=sys.stderr)
    if rep.needs_resync and resync:
        print(f"\nThe first sync (resync) did not complete. Fix the cause above and run "
              f"`webde_sync.py {name}` again.", file=sys.stderr)
    elif rep.needs_resync:
        st.baseline.unlink(missing_ok=True)
        print("\nAfter this error bisync needs a fresh first sync (resync). Fix the cause "
              f"above, then run\n`webde_sync.py {name}` interactively: it previews the "
              "resync (nothing gets deleted).\nUnattended -y runs stop until then.",
              file=sys.stderr)


# --------------------------------------------------------------------- actions

def confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes", "j", "ja")
    except EOFError:
        info()
        return False


def preview(rc: Rclone, args: list[str], profile: Profile) -> Report:
    proc = rc.run(args + ["--use-json-log", "-v"], check=False, add_flags=False)
    rep = parse_report(log_messages(proc.stderr.splitlines()), profile.local, profile.remote)
    if proc.returncode != 0 and not rep.errors:
        rep.errors.append(f"rclone bisync --dry-run failed with exit code {proc.returncode}")
    return rep


def wait_for(proc: subprocess.Popen) -> tuple[int, bool]:
    """Wait for rclone; on Ctrl-C (which rclone receives too) keep waiting while
    it stops cleanly.  Returns (exit code, interrupted)."""
    interrupted = False
    while True:
        try:
            return proc.wait(), interrupted
        except KeyboardInterrupt:
            interrupted = True
            print("\ninterrupted -- waiting for rclone to stop cleanly ...", file=sys.stderr)


def execute(rc: Rclone, args: list[str], profile: Profile, st: State, stamp: str) -> Report:
    log_json = st.logs / f"{stamp}.jsonl"
    cmd = rc.cmd(args + ["--log-file", str(log_json), "--use-json-log", "-v"])
    if sys.stdout.isatty():
        cmd.append("--progress")
    proc = subprocess.Popen(cmd, env=rc.env, stdin=subprocess.DEVNULL, pass_fds=rc.pass_fds)
    code, user_interrupt = wait_for(proc)
    try:
        lines = log_json.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    messages = log_messages(lines)
    rep = parse_report(messages, profile.local, profile.remote)
    if code != 0 and not rep.errors:
        rep.errors.append(f"rclone bisync failed with exit code {code}")
    # On Ctrl-C bisync finishes running transfers, saves its state and exits 130.
    rep.interrupted = user_interrupt or code == 130
    rep.success = rep.success and code in (0, 130)
    with open(st.logs / f"{stamp}.log", "w", encoding="utf-8") as f:   # readable copy
        f.writelines(f"{m.level.upper():<7} {m.text}\n" for m in messages)
    log_json.unlink(missing_ok=True)
    for old in sorted(st.logs.glob("*.log"))[:-KEEP_LOGS]:
        old.unlink()
    return rep


def prune_backups(rc: Rclone, profile: Profile, now: datetime) -> None:
    cutoff = now - timedelta(days=profile.keep_backups_days)
    for root in (str(profile.local), profile.remote):
        bdir = remote_join(root, BACKUP_DIR)
        proc = rc.run(["lsf", "--dirs-only", "--max-depth", "1",
                       *strip_filter_flags(rc.flags), bdir], check=False, add_flags=False)
        for name in proc.stdout.splitlines() if proc.returncode == 0 else []:
            name = name.rstrip("/")
            try:
                if datetime.strptime(name, STAMP_FMT).astimezone() < cutoff:
                    rc.run(["purge", *strip_filter_flags(rc.flags), remote_join(bdir, name)],
                           add_flags=False)
                    info(f"pruned old backup {remote_join(bdir, name)}")
            except ValueError:
                continue                # not one of ours, leave it alone
            except SyncError as e:
                warn(str(e))


def verify(rc: Rclone, profile: Profile, st: State, assume_yes: bool,
           max_show: int | None) -> int:
    info("Verify compares the content of every file byte by byte. WebDAV offers no "
         "checksums, so this downloads ALL files once -- slow and heavy on big folders.")
    if not assume_yes and not confirm("Start the full content check?"):
        return EXIT_ABORTED
    result = st.root / "verify.txt"          # inside the private state directory
    result.unlink(missing_ok=True)           # a hard-killed earlier run may have left one
    cmd = rc.cmd(["check", str(profile.local), profile.remote, "--download",
                  "--filter-from", str(st.filters), "--combined", str(result),
                  "--color", "NEVER", *rc.flags])
    if sys.stdout.isatty():
        cmd.append("--progress")
    proc = subprocess.Popen(cmd, env=rc.env, stdin=subprocess.DEVNULL, pass_fds=rc.pass_fds)
    code, interrupted = wait_for(proc)
    try:
        lines = result.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    result.unlink(missing_ok=True)
    if interrupted:
        info("Interrupted -- the content check is incomplete.")
        return 130
    groups: dict[str, list[str]] = {"*": [], "+": [], "-": [], "!": []}
    same = 0
    for line in lines:
        mark, _, path = line.partition(" ")
        if mark == "=":
            same += 1
        elif mark in groups:
            groups[mark].append(path)
    titles = {"*": "content differs", "+": "only local", "-": "only on server",
              "!": "could not be compared"}
    info(f"  {'identical:':<24}{same:>6} file(s)")
    for mark, title in titles.items():
        paths = groups[mark]
        info(f"  {title + ':':<24}{len(paths):>6} file(s)")
        limit = len(paths) if max_show is None else max_show
        for p in paths[:limit]:
            info(f"      {p}")
        if len(paths) > limit:
            info(f"      ... and {len(paths) - limit} more (use --list-all)")
    if code not in (0, 1) or (code == 1 and not any(groups.values())):
        raise SyncError(f"rclone check failed (exit {code}), see its messages above")
    if groups["*"]:
        p = groups["*"][0]
        local_file = shlex.quote(str(profile.local / p))
        info("\nFiles whose content differs were changed without a size change, which "
             "bisync cannot\nsee on WebDAV. Decide per file which copy is right, then sync:\n"
             f"  keep the local copy:  touch {local_file}\n"
             f"  keep the server copy: rclone copyto "
             f"{shlex.quote(remote_join(profile.remote, p))} {local_file}")
    if groups["+"] or groups["-"]:
        info("\nFiles on one side only are normally handled by the next sync (or were "
             "excluded/skipped); run a sync and verify again.")
    if any(groups.values()):
        return EXIT_FAIL
    info("\nBoth sides are identical.")
    return EXIT_OK


# ------------------------------------------------------------------------ main

def non_negative(s: str) -> int:
    n = int(s)
    if n < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return n


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="webde_sync.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Safe two-way sync of a local folder with an rclone remote "
                    "(e.g. web.de) via `rclone bisync`, with preview and confirmation.",
        epilog="Arguments after `--` go to rclone unchanged, e.g. `-- --tpslimit 4` (flags "
               "this\nwrapper controls itself, like --force or --dry-run, are refused "
               "there).\n"
               "Exit codes: 0 ok, 1 sync failed / differences, 2 usage error, "
               "3 aborted by the user.")
    ap.add_argument("target", nargs="*", metavar="PROFILE | LOCAL_DIR REMOTE")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="only show what would change")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="no preview and no questions (for cron); bisync's safety "
                         "checks still apply")
    ap.add_argument("--resync", action="store_true",
                    help="(re)build the baseline: merge both sides, never deletes")
    ap.add_argument("--force", action="store_true",
                    help="override bisync's max-delete / all-files-changed safety abort")
    ap.add_argument("--verify", action="store_true",
                    help="byte-compare both sides (downloads everything)")
    ap.add_argument("--filter", action="append", default=[], metavar="RULE",
                    help="rclone filter rule for ad-hoc runs, e.g. '- .git/**'")
    ap.add_argument("--max-show", type=non_negative, default=25, metavar="N",
                    help="files listed per category (default 25)")
    ap.add_argument("--list-all", action="store_true", help="list every file")
    ap.add_argument("--config", type=Path, metavar="FILE",
                    help=f"profiles file (default {tilde(default_config_path())})")
    ap.add_argument("--rclone", metavar="PATH", help="rclone binary to use")
    ap.add_argument("--list", action="store_true", help="list the configured profiles")
    ap.add_argument("--example-config", action="store_true",
                    help="print an example profiles.toml")
    return ap.parse_args(argv)


def resolve_profile(args: argparse.Namespace, config_path: Path) -> Profile:
    if len(args.target) == 1:
        if args.filter:
            raise UsageError("--filter is for ad-hoc runs; put filters into the profile")
        profiles = load_config(config_path)
        name = args.target[0]
        if name not in profiles:
            have = ", ".join(profiles) or f"none -- create {tilde(config_path)} " \
                                          "(see --example-config)"
            raise UsageError(f"unknown profile {name!r} (configured: {have})")
        return profile_from_dict(name, profiles[name], f"{tilde(config_path)} [{name}]")
    if len(args.target) == 2:
        return profile_from_dict(None, {"local": args.target[0], "remote": args.target[1],
                                        "filters": args.filter}, "command line")
    raise UsageError("expected PROFILE or LOCAL_DIR REMOTE (see --help)")


def run(args: argparse.Namespace, extra: list[str]) -> int:
    config_path = (args.config or default_config_path()).expanduser()
    if args.example_config:
        print(EXAMPLE_CONFIG, end="")
        return EXIT_OK
    if args.list:
        for name, raw in load_config(config_path).items():
            info(f"{name:<16} {raw.get('local', '?')}  <->  {raw.get('remote', '?')}")
        return EXIT_OK
    if args.force and args.yes:
        raise UsageError("--force needs a reviewed preview; it cannot be combined with --yes")
    profile = resolve_profile(args, config_path)
    user_flags = split_short_flags(profile.rclone_flags + extra)
    check_user_flags(user_flags)
    max_show = None if args.list_all else args.max_show

    profile.local = profile.local.resolve()
    if not profile.local.is_dir():
        raise UsageError(f"local folder {tilde(profile.local)} does not exist")
    if profile.local == Path("/"):
        raise UsageError("refusing to sync the whole file system")
    rc = find_rclone(args.rclone)
    rc.flags = general_flags(user_flags)                     # e.g. --config, --timeout
    ensure_config_password(rc)
    check_remote_configured(rc, profile.remote)
    warn_plain_http(rc, profile.remote)
    for mode in (profile.conflict_resolve, profile.resync_mode):
        if mode in ("newer", "older"):
            warn(f"'{mode}' compares modification times, but WebDAV servers like web.de "
                 "report the upload time instead of the file's own time")

    st = state_for(profile, default_state_home())
    lock = ProfileLock(st.root / "lock")
    rc.pass_fds = (lock.fd,)
    try:
        clear_stale_bisync_locks(st)
        filters_text = render_filters(profile, st)
        write_if_changed(st.filters, filters_text)
        if args.verify:
            return verify(rc, profile, st, args.yes, max_show)
        return sync(rc, profile, st, args, extra, filters_text, max_show)
    finally:
        lock.release()


def review(plan: Report, profile: Profile, resync: bool, stamp: str) -> int | None:
    """Ask for confirmation of a previewed plan; None means: go ahead."""
    unclear = plan.n_actions == 0 and (
        plan.stats_transfers > 0 or (not resync and not (
            plan.no_changes or plan.bisync_changes == 0 and not plan.plan_lines)))
    if unclear:
        # bisync plans something this wrapper could not classify (e.g. a changed
        # log format in a newer rclone): never present that as "nothing to do".
        warn("could not fully interpret bisync's plan; its own summary:")
        for line in plan.plan_lines:
            info(f"  {line}")
        if plan.stats:
            info(plan.stats)
        prompt = "Apply bisync's plan as shown above?"
    elif plan.n_actions == 0:
        if not resync:
            info("Everything is in sync.")
            return EXIT_OK
        prompt = "Create the baseline (no files need to be copied)?"
    else:
        deleted = len(plan.delete_remote) + len(plan.delete_local)
        if plan.replaced or deleted:
            what = f"{len(plan.replaced)} file(s) will be replaced and {deleted} deleted"
            if profile.backups:
                info(f"Note: {what}; the old versions are kept in {BACKUP_DIR}/{stamp}/ "
                     "on the side where they change.")
            else:
                warn(f"{what} PERMANENTLY (backups are off in this profile)")
        prompt = f"Apply these {plan.n_actions} change(s)?"
    if not confirm(prompt):
        info("Aborted, nothing was changed.")
        return EXIT_ABORTED
    return None


def report_surprises(plan: Report, done: Report, max_show: int | None) -> None:
    """Warn about actions bisync took that the confirmed preview did not show
    (files changed between preview and sync)."""
    pairs = (("uploaded", done.upload, plan.upload), ("downloaded", done.download, plan.download),
             ("deleted on server", done.delete_remote, plan.delete_remote),
             ("deleted locally", done.delete_local, plan.delete_local),
             ("conflict", done.conflicts, plan.conflicts))
    found = [(title, sorted(set(a) - set(b))) for title, a, b in pairs]
    found = [(title, paths) for title, paths in found if paths]
    if not found:
        return
    lines = [("warning: files changed between the preview and the sync -- bisync also did "
              "(its safety checks still applied):")]
    for title, paths in found:
        limit = len(paths) if max_show is None else max_show
        more = f" ... and {len(paths) - limit} more" if len(paths) > limit else ""
        lines.append(f"    {title}: {', '.join(paths[:limit])}{more}")
    print("\n".join(lines), file=sys.stderr, flush=True)


def unique_stamp(st: State) -> tuple[datetime, str]:
    """Time stamp for this run's backup folder, never the same as the last run's
    (two runs within one second would otherwise share -- and overwrite -- it)."""
    last_file = st.root / "last-stamp"
    try:
        last = last_file.read_text(encoding="utf-8").strip()
    except OSError:
        last = ""
    while True:
        now = datetime.now().astimezone()
        stamp = now.strftime(STAMP_FMT)
        if stamp != last:
            write_if_changed(last_file, stamp + "\n")
            return now, stamp
        time.sleep(0.2)


def ensure_check_file(rc: Rclone, profile: Profile) -> None:
    """Create the check_access marker for a confirmed resync, unless a side has one."""
    marker = profile.local / "RCLONE_TEST"
    if not profile.check_access or marker.exists():
        return
    if rc.run(["lsjson", "--stat", remote_join(profile.remote, "RCLONE_TEST")],
              check=False).returncode == 0:
        return                          # the resync brings the server's marker down
    marker.write_text("Marker file for webde_sync.py check_access -- do not delete.\n",
                      encoding="utf-8")


def sync(rc: Rclone, profile: Profile, st: State, args: argparse.Namespace,
         extra: list[str], filters_text: str, max_show: int | None) -> int:
    resync = args.resync
    problem = baseline_problem(profile, st, filters_text)
    if problem and not resync:
        if args.yes:
            raise SyncError(f"{problem}: a resync is needed -- run once interactively "
                            "(with preview) or pass --resync")
        resync = True
    winner = {"path1": "the local", "path2": "the server"}.get(profile.resync_mode,
                                                               f"the {profile.resync_mode}")
    info(f"profile : {profile.label}")
    info(f"local   : {tilde(profile.local)}")
    info(f"remote  : {profile.remote}")
    info(f"mode    : {'RESYNC' if resync else 'sync'}"
         + (f" ({problem})" if problem and resync else ""))
    if resync:
        loser = ("is backed up" if profile.backups else
                 "is overwritten PERMANENTLY, backups are off")
        info("          Merges both sides: files missing on one side are copied over; for "
             f"files that\n          differ in size {winner} version wins (the other one "
             f"{loser}).\n          Nothing is deleted. Same-size files are assumed equal "
             "(check with --verify).")
    info()

    if not remote_dir_exists(rc, profile.remote):
        if not resync:
            raise SyncError(f"remote folder {profile.remote} not found -- was it renamed or "
                            "deleted? To start over with it, run with --resync")
        if args.dry_run:
            info(f"{profile.remote} does not exist yet; it would be created.")
            return EXIT_OK
        if not args.yes and not confirm(f"Create the remote folder {profile.remote}?"):
            return EXIT_ABORTED
        rc.run(["mkdir", profile.remote])

    now, stamp = unique_stamp(st)
    common = {"resync": resync, "force": args.force, "stamp": stamp, "extra": extra}
    plan = None
    if not args.yes or args.dry_run:
        plan = preview(rc, bisync_args(profile, st, dry_run=True, **common), profile)
        info("Overview" + (" (resync)" if resync else ""))
        info("========")
        show_report(plan, max_show, planned=True)
        info()
        remind_conflicts(profile, st, max_show)
        if plan.errors:
            advise(plan, profile, st, resync)
            return EXIT_FAIL
        if args.dry_run:
            info("--dry-run: nothing was changed.")
            return EXIT_OK
        if resync and plan.n_actions == 0 and not local_has_files(rc, profile, st):
            info(NOTHING_YET)       # (nothing planned + nothing local = server empty too)
            return EXIT_OK
        if (answer := review(plan, profile, resync, stamp)) is not None:
            return answer
    if resync:
        ensure_check_file(rc, profile)
        clear_stale_listings(st)             # --resync rebuilds the listings anyway

    info("Syncing ...")
    rep = execute(rc, bisync_args(profile, st, dry_run=False, **common), profile, st, stamp)
    info()
    if rep.stats:
        info(rep.stats)
    if rep.success and resync:
        if listing_is_empty(st):    # bisync could not continue from an empty baseline
            st.baseline.unlink(missing_ok=True)
            info(NOTHING_YET)
            info(f"log: {tilde(st.logs / (stamp + '.log'))}")
            return EXIT_OK
        write_baseline(profile, st, filters_text, rc.version())
    log_note = f"log: {tilde(st.logs / (stamp + '.log'))}"
    if rep.interrupted:            # the queued lists are plans, not completed transfers
        if resync and not rep.success:
            info("Interrupted -- the resync did not finish, so the next run repeats it "
                 "(with a preview).")
        else:
            info("Interrupted -- bisync finished the running transfers and saved its state. "
                 "Run again to sync the rest (no --resync needed).")
        show_backups(rc, profile, stamp, max_show)
        info(log_note)
        return 130
    if not rep.success:
        advise(rep, profile, st, resync)
        show_backups(rc, profile, stamp, max_show)
        info(f"\n{log_note}")
        return EXIT_FAIL
    info("Done" + (" -- baseline created." if resync else "."))
    show_report(rep, max_show, planned=False)
    show_backups(rc, profile, stamp, max_show)
    if plan is not None and not resync:
        report_surprises(plan, rep, max_show)
    if rep.conflicts or args.yes:  # new conflicts, or no preview has shown the old ones
        remind_conflicts(profile, st, max_show)
    if profile.backups and profile.keep_backups_days:
        prune_backups(rc, profile, now)
    info(log_note)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    args = parse_args(argv)
    try:
        return run(args, extra)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except (SyncError, OSError) as e:       # OSError: unwritable state dir, full disk, ...
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
