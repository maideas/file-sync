#!/usr/bin/env python3
"""Two-way sync for an rclone remote (e.g. the web.de Online-Speicher via WebDAV),
with a preview of what would happen and a confirmation prompt before doing anything.

Usage:
    webde_sync.py LOCAL_DIR REMOTE [options]

    LOCAL_DIR  local folder to keep in sync
    REMOTE     rclone remote path, e.g. "webde:/" or "webde:/Backup"
               (a plain local directory works too, which is handy for testing)

What it does:
  1. reads the file listings of both sides via `rclone lsjson --recursive`
  2. compares them against each other and against a state file from the
     previous run, and classifies every file:
       - upload   (new / changed)   files copied to the server
       - download (new / changed)   files copied from the server
       - delete (server / local)    files removed on one side since the last
                                     sync, propagated to the other side
       - conflict                  changed on both sides with matching
                                     timestamps; never touched automatically
  3. prints an overview (counts and file lists)
  4. asks for confirmation and runs the sync via `rclone copy` / `rclone delete`

Files are compared by size and modification time (--mtime-tolerance, default 2 s).
WebDAV (web.de) does not expose checksums, so files whose size differs while their
mtimes match are reported as conflicts and skipped rather than guessed at.

Deletions are only propagated after an explicit extra confirmation (`--prune`
answers that prompt with yes; without it the rest still syncs).
The first run has no baseline state yet and therefore never deletes anything.

Requires: rclone >= 1.52.  See README.md for the web.de remote setup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

STATE_VERSION = 1
FMT_TIME = "%Y-%m-%d %H:%M:%S"


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- rclone I/O

def rclone(argv: list[str]) -> subprocess.CompletedProcess:
    """Run rclone, die on failure."""
    cmd = ["rclone", *argv]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"`{' '.join(cmd)}` failed (exit {proc.returncode}):\n{proc.stderr.strip()}")
    return proc


def parse_modtime(s: str | None) -> datetime | None:
    """Parse rclone's RFC 3339 timestamps defensively."""
    if not s:
        return None
    s = s.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    s = re.sub(r"\.(\d{6})\d+", r".\1", s)  # clamp fractional seconds
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


def listing(path: str, excludes: list[str], skip: Path | None = None) -> dict[str, tuple[int, datetime | None]]:
    """Return {relative posix path: (size, modtime)} for all files under path."""
    proc = rclone(["lsjson", "--recursive", path, *sum((["--exclude", e] for e in excludes), [])])
    try:
        entries = json.loads(proc.stdout)
    except json.JSONDecodeError:
        die(f"cannot parse `rclone lsjson` output for {path}")
    skip_abs = skip.resolve() if skip else None
    files: dict[str, tuple[int, datetime | None]] = {}
    for e in entries:
        if e.get("isDir") or e.get("isdir"):
            continue
        rel = e["path"]
        if skip_abs is not None:
            p = (Path(path) / rel).resolve()
            if p == skip_abs:
                continue  # e.g. the state file if it lives inside LOCAL_DIR
        files[rel] = (int(e.get("size") or 0), parse_modtime(e.get("modTime") or e.get("modtime")))
    return files


# ---------------------------------------------------------------- state file

def default_state_path(local: Path, remote: str) -> Path:
    key = hashlib.sha1(f"{local}\n{remote}".encode()).hexdigest()[:16]
    return Path.home() / ".cache" / "websync" / f"{key}.json"


def load_state(path: Path) -> dict[str, tuple[int, datetime | None]] | None:
    """Return {path: (size, modtime)} from the last sync, or None on first run."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        files = data.get("files", {})
        return {
            p: (int(v.get("size") or 0), parse_modtime(v.get("modtime")))
            for p, v in files.items()
        }
    except (json.JSONDecodeError, OSError, ValueError):
        print(f"warning: state file {path} unreadable -- treating this as a first run",
              file=sys.stderr)
        return None


def save_state(path: Path, local: Path, remote: str,
               files: dict[str, tuple[int, datetime | None]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": STATE_VERSION,
        "local": str(local),
        "remote": remote,
        "last_sync": datetime.now().astimezone().isoformat(timespec="seconds"),
        "files": {
            p: {"size": s, "modtime": m.isoformat() if m else None}
            for p, (s, m) in sorted(files.items())
        },
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- diff logic

@dataclass
class Plan:
    upload_new: list[str]
    upload_changed: list[str]
    download_new: list[str]
    download_changed: list[str]
    delete_remote: list[str]   # gone locally  -> remove on server
    delete_local: list[str]    # gone on server -> remove local copy
    conflicts: list[str]
    unchanged: int


def plan_sync(local: dict, remote: dict, state: dict | None, tol: float) -> Plan:
    p = Plan([], [], [], [], [], [], [], 0)
    for path in sorted(set(local) | set(remote)):
        in_l, in_r = path in local, path in remote
        if in_l and in_r:
            ls, lm = local[path]
            rs, rm = remote[path]
            tl = lm.timestamp() if lm else None
            tr = rm.timestamp() if rm else None
            if tl is None or tr is None:
                # missing timestamp: only sync if identical size
                (p.unchanged.__class__ and (p.unchanged := p.unchanged + 1) if ls == rs
                 else p.conflicts.append(path))
                continue
            if ls == rs and abs(tl - tr) <= tol:
                p.unchanged += 1
            elif abs(tl - tr) <= tol:
                p.conflicts.append(path)          # same mtime, different size
            elif tl > tr:
                p.upload_changed.append(path)     # local is newer
            else:
                p.download_changed.append(path)  # remote is newer
        elif in_l:
            if state and path in state:
                p.delete_local.append(path)       # server copy vanished
            else:
                p.upload_new.append(path)
        else:
            if state and path in state:
                p.delete_remote.append(path)      # local copy vanished
            else:
                p.download_new.append(path)
    return p


# ---------------------------------------------------------------- output / IO

def human(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def fmt_mtime(m: datetime | None) -> str:
    return m.astimezone().strftime(FMT_TIME) if m else "?"


def show(name: str, paths: list[str], side: dict, max_show: int | None) -> None:
    total = sum(side[p][0] for p in paths if p in side)
    print(f"  {name + ':':<22}{len(paths):>5} file(s)   {human(total):>10}")
    if not paths:
        return
    limit = max_show if max_show is not None else len(paths)
    for p in paths[:limit]:
        size, mtime = side.get(p, (0, None))
        print(f"      {p}   [{human(size)}, {fmt_mtime(mtime)}]")
    if len(paths) > limit:
        print(f"      ... and {len(paths) - limit} more (use --list-all)")


def confirm(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes", "j", "ja")


# ---------------------------------------------------------------- execution

def files_from_tmp(paths: list[str]) -> str:
    fd, name = tempfile.mkstemp(prefix="websync-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(paths) + "\n")
    return name


def rclone_copy(src: str, dst: str, paths: list[str], excludes: list[str]) -> None:
    ff = files_from_tmp(paths)
    try:
        cmd = ["copy", src, dst, "--files-from", ff]
        cmd += sum((["--exclude", e] for e in excludes), [])
        print(f"  -> rclone copy {src!r} -> {dst!r} ({len(paths)} file(s))")
        rclone(cmd)
    finally:
        os.unlink(ff)


def rclone_delete(remote: str, paths: list[str]) -> None:
    ff = files_from_tmp(paths)
    try:
        print(f"  -> rclone delete on {remote!r} ({len(paths)} file(s))")
        rclone(["delete", remote, "--files-from", ff])
        rclone(["rmdirs", remote])
    finally:
        os.unlink(ff)


def delete_local(local: Path, paths: list[str]) -> None:
    print(f"  -> deleting {len(paths)} local file(s)")
    for rel in paths:
        f = local / rel
        if f.is_file():
            f.unlink()
        d = f.parent
        while d != local and d.is_dir():
            try:
                d.rmdir()  # only succeeds when empty
            except OSError:
                break
            d = d.parent


# ---------------------------------------------------------------- main

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="webde_sync.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__.split("What it does:")[0],
        epilog=__doc__.split("What it does:")[1],
    )
    ap.add_argument("local", type=Path, metavar="LOCAL_DIR",
                    help="local directory to keep in sync")
    ap.add_argument("remote", metavar="REMOTE",
                    help="rclone path, e.g. webde:/Backup")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="apply transfers without asking (deletions still need --prune)")
    ap.add_argument("--prune", action="store_true",
                    help="also propagate deletions without asking")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="only show the overview, change nothing, ask nothing")
    ap.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                    help="rclone exclude pattern (repeatable), e.g. --exclude '.git/**'")
    ap.add_argument("--max-show", type=int, default=25, metavar="N",
                    help="max files listed per category (default 25)")
    ap.add_argument("--list-all", action="store_true",
                    help="list every file instead of truncating")
    ap.add_argument("--mtime-tolerance", type=float, default=2.0, metavar="SECONDS",
                    help="mtime skew tolerated between sides (default 2)")
    ap.add_argument("--state", type=Path, metavar="FILE", default=None,
                    help="state file (default: ~/.cache/websync/<key>.json)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    local = args.local.resolve()
    if not local.is_dir():
        die(f"local directory {local} does not exist")
    if shutil.which("rclone") is None:
        die("rclone not found in PATH -- install it first (e.g. `sudo apt install rclone`)")

    state_path = args.state or default_state_path(local, args.remote)
    state = load_state(state_path)
    first_run = state is None

    print(f"local : {local}")
    print(f"remote: {args.remote}")
    print(f"state : {state_path}"
          + (f" (last sync {getattr(state, '')})" if False else ""))
    if first_run:
        print("       (first sync: no baseline state yet, deletions cannot be detected)")
    print()

    loc = listing(str(local), args.exclude, skip=state_path)
    rem = listing(args.remote, args.exclude)
    plan = plan_sync(loc, rem, state, args.mtime_tolerance)

    print("Overview")
    print("========")
    for name, paths, side in (
        ("upload new", plan.upload_new, loc),
        ("upload changed", plan.upload_changed, loc),
        ("download new", plan.download_new, rem),
        ("download changed", plan.download_changed, rem),
        ("delete on server", plan.delete_remote, loc),
        ("delete locally", plan.delete_local, rem),
        ("conflict (skipped)", plan.conflicts, loc),
    ):
        show(name, paths, side, None if args.list_all else args.max_show)
    print(f"  {'unchanged:':<22}{plan.unchanged:>5} file(s)")
    print()

    n_transfers = (len(plan.upload_new) + len(plan.upload_changed)
                   + len(plan.download_new) + len(plan.download_changed))
    n_deletes = len(plan.delete_remote) + len(plan.delete_local)

    if args.dry_run:
        print("--dry-run: nothing was changed, no state was written.")
        return 0
    if n_transfers == 0 and n_deletes == 0 and not plan.conflicts:
        save_state(state_path, local, args.remote,
                   {p: s for p, s in loc.items() if p in rem})
        print("Everything is in sync. (state updated)")
        return 0

    if plan.conflicts:
        print("WARNING: conflicting files (changed on both sides with identical"
              " timestamps) are skipped -- resolve them manually:")
        for p in plan.conflicts:
            print(f"  ! {p}  local={human(loc[p][0])} vs remote={human(rem[p][0])}")
        print()

    if not args.yes:
        if not confirm(f"Transfer {n_transfers} file(s)?"):
            print("Aborted, nothing was changed.")
            return 1

    print()
    print("Syncing ...")
    if plan.upload_new or plan.upload_changed:
        rclone_copy(str(local), args.remote, plan.upload_new + plan.upload_changed, args.exclude)
    if plan.download_new or plan.download_changed:
        rclone_copy(args.remote, str(local), plan.download_new + plan.download_changed, args.exclude)

    if n_deletes:
        if args.prune or (args.yes and confirm(
                f"Propagate {n_deletes} deletion(s) ({len(plan.delete_remote)} on server,"
                f" {len(plan.delete_local)} locally)?")):
            if plan.delete_remote:
                rclone_delete(args.remote, plan.delete_remote)
            if plan.delete_local:
                delete_local(local, plan.delete_local)
        else:
            print(f"Deletions skipped ({n_deletes} file(s) stay where they are).")

    # rebuild state from what is on both sides now, plus skipped deletions
    loc2 = listing(str(local), args.exclude, skip=state_path)
    rem2 = listing(args.remote, args.exclude)
    new_state = {p: s for p, s in loc2.items() if p in rem2}
    for p in plan.delete_local:            # kept locally, gone on server
        if p not in new_state and p in loc2:
            new_state[p] = loc2[p]
    for p in plan.delete_remote:            # kept on server, gone locally
        if p not in new_state and p in rem2:
            new_state[p] = rem2[p]
    save_state(state_path, local, args.remote, new_state)
    print(f"Done. (state written to {state_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())