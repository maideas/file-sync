"""Tests for webde_sync.py.

Unit tests cover the bisync log parser and the profile/argument handling.
Integration tests run the real script against a real `rclone serve webdav`
server (fixed version, $WEBDE_SYNC_TEST_SERVER) whose client remote uses
`vendor = other` -- like web.de, it cannot
store modification times, so the server reports upload times.  They run once
per rclone binary listed in $WEBDE_SYNC_TEST_RCLONE (space separated; `make
test` downloads v1.66.0 and the current release) and are skipped otherwise.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "webde_sync.py"
sys.path.insert(0, str(ROOT))
import webde_sync as ws

RCLONES = [p for p in os.environ.get("WEBDE_SYNC_TEST_RCLONE", "").split() if Path(p).is_file()]
# The WebDAV test server is one fixed binary: `rclone serve webdav` v1.76.0 itself
# stalls for minutes on some MOVEs into new folders (a server bug, not bisync's).
SERVER = os.environ.get("WEBDE_SYNC_TEST_SERVER") or (RCLONES[0] if RCLONES else "")


# ------------------------------------------------------------------ unit tests

def jl(msg: str, level: str = "notice", obj: str | None = None) -> str:
    d = {"level": level, "msg": msg}
    if obj:
        d["object"] = obj
    return json.dumps(d)


def parse(lines, local="/data/local", remote="webde:sync"):
    return ws.parse_report(ws.log_messages(lines), Path(local), remote)


def test_parse_normal_run_with_ansi_and_absolute_local_paths():
    rep = parse([
        jl("- \x1b[36mPath1\x1b[0m    File changed: size (larger), time (newer) - a b - c.txt"),
        jl("- Path1    File was deleted          - gone.txt"),
        jl("- Path2    File is new               - sub/new.txt"),
        jl("Path1:    2 changes:    0 new,    1 modified,    1 deleted", "info"),
        jl("- Path1    Queue copy to Path2       - webde:sync/a b - c.txt"),
        jl("- Path2    Queue delete              - webde:sync/gone.txt"),
        jl("- Path2    Queue copy to Path1       - /data/local/sub/new.txt"),
        jl("Skipped copy as --dry-run is set (size 12)", obj="sub/new.txt"),
        jl("Can't follow symlink without -L/--copy-links", "warning", obj="link"),
        jl("Bisync successful", "info"),
    ])
    assert rep.upload == {"a b - c.txt": "changed: size (larger), time (newer)"}
    assert rep.download == {"sub/new.txt": "new"}
    assert rep.delete_remote == ["gone.txt"] and rep.delete_local == []
    assert rep.sizes == {"sub/new.txt": 12}
    assert rep.symlinks == ["link"]
    assert rep.success and not rep.errors and rep.bisync_changes == 2


def test_parse_conflicts_restores_and_identical_files():
    rep = parse([
        jl("- Path1    File changed: size (larger) - edit.txt"),
        jl("- Path2    File changed: size (larger) - edit.txt"),
        jl("- Path1    File was deleted          - del.txt"),
        jl("- Path2    File changed: size (larger) - del.txt"),
        jl("- WARNING  New or changed in both paths       - edit.txt"),
        jl("- WARNING  New or changed in both paths       - same.txt"),
        jl("- Path1    Queue copy to Path2       - webde:sync/edit.txt.conflict1"),
        jl("- Path2    Queue copy to Path1       - /data/local/edit.txt.conflict2"),
        jl("- Path2    Queue copy to Path1       - /data/local/del.txt"),
    ])
    assert rep.conflicts == {"edit.txt": [("edit.txt.conflict1", "local"),
                                          ("edit.txt.conflict2", "server")]}
    assert ws.conflict_text("edit.txt", rep.conflicts["edit.txt"]) == (
        "changed on both sides, both kept: edit.txt.conflict1 = local version, "
        "edit.txt.conflict2 = server version")
    assert rep.download == {"del.txt": "restored: deleted locally but changed on the server"}
    assert rep.upload == {} and rep.n_actions == 2


def test_parse_resolved_conflict_names_the_winner():
    rep = parse([
        jl("- WARNING  New or changed in both paths       - d/edit.txt"),
        jl("- Path2    Queue copy to Path1       - /data/local/d/edit.txt.conflict3"),
    ])
    assert rep.conflicts == {"d/edit.txt": [("d/edit.txt.conflict3", "server")]}
    assert ws.conflict_text("d/edit.txt", rep.conflicts["d/edit.txt"]) == (
        "changed on both sides: edit.txt = local version, edit.txt.conflict3 = server version")


def test_parse_resync_copies_and_errors():
    rep = parse([
        jl("- Path2    Resync is copying files to         - Path1", "info"),
        jl("Skipped copy as --dry-run is set (size 1)", obj="remote.txt"),
        jl("- Path1    Resync is copying files to         - Path2", "info"),
        jl("Copied (new)", "info", obj="local.txt"),
        jl("Bisync aborted. Must run --resync to recover.", "error"),
        "panic: something not json",
    ], remote="webde:")
    assert rep.download == {"remote.txt": "resync"} and rep.upload == {"local.txt": "resync"}
    assert rep.needs_resync and len(rep.errors) == 2


def test_relativizer_handles_remote_root_forms():
    for remote, shown in (("webde:", "webde:/x/y"), ("webde:/", "webde:/x/y"),
                          ("webde:/Docs/", "webde:/Docs/x/y"), ("webde:Docs", "webde:Docs/x/y")):
        assert ws._relativizer(Path("/l"), remote)("Path2", shown) == "x/y"


def test_remote_join():
    assert ws.remote_join("webde:", "b") == "webde:b"
    assert ws.remote_join("webde:/", "b") == "webde:/b"
    assert ws.remote_join("webde:/Docs", "b") == "webde:/Docs/b"


@pytest.mark.parametrize("raw, err", [
    ({"local": "/x"}, "'remote' is required"),
    ({"local": "/x", "remote": "r:", "max_delte": 5}, "unknown key"),
    ({"local": "/x", "remote": "r:", "max_delete": True}, "type int"),
    ({"local": "/x", "remote": "r:", "max_delete": 101}, "percentage"),
    ({"local": "/x", "remote": "r:", "filters": ["ok", 3]}, "list of strings"),
    ({"local": "/x", "remote": "r:", "conflict_resolve": "mine"}, "invalid"),
])
def test_profile_validation(raw, err):
    with pytest.raises(ws.UsageError, match=err):
        ws.profile_from_dict("p", raw, "test")


def test_profile_aliases_and_defaults():
    p = ws.profile_from_dict("p", {"local": "~/x", "remote": "r:/x", "resync_mode": "remote",
                                   "conflict_resolve": "local"}, "test")
    assert p.resync_mode == "path2" and p.conflict_resolve == "path1"
    assert p.local == Path.home() / "x" and p.backups and p.max_delete == 50


def test_filters_backup_rule_first_and_state_excluded(tmp_path):
    p = ws.Profile(name="p", local=tmp_path, remote="r:", filters=["+ **"])
    st = ws.State(tmp_path / ".local/state/webde-sync/p")
    lines = ws.render_filters(p, st).splitlines()
    assert lines[1] == f"- /{ws.BACKUP_DIR}/**"
    assert lines[2] == "- /.local/state/webde-sync/p/**" and lines[3] == "+ **"


def test_bisync_args_ignore_times_only_outside_resync(tmp_path):
    p = ws.Profile(name="p", local=tmp_path, remote="r:/x", check_access=True)
    st = ws.State(tmp_path / "st")
    normal = ws.bisync_args(p, st, resync=False, dry_run=True, force=False, stamp="S", extra=[])
    resync = ws.bisync_args(p, st, resync=True, dry_run=False, force=True, stamp="S", extra=["-q"])
    assert "--ignore-times" in normal and "--check-access" in normal and "--dry-run" in normal
    assert "--ignore-times" not in resync and "--resync-mode" in resync and "--force" in resync
    assert resync[-1] == "-q"
    assert resync[resync.index("--backup-dir2") + 1] == f"r:/x/{ws.BACKUP_DIR}/S"


# ----------------------------------------------------------- integration tests

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Env:
    """A local folder, a WebDAV 'cloud' served by rclone, and a runner."""

    def __init__(self, tmp: Path, rclone: str):
        self.tmp, self.rclone = tmp, rclone
        self.local, self.server = tmp / "local", tmp / "server"
        self.local.mkdir()
        (self.server / "sync").mkdir(parents=True)
        port = free_port()
        conf = tmp / "rclone.conf"
        conf.write_text(f"[webde]\ntype = webdav\nurl = http://127.0.0.1:{port}\n"
                        "vendor = other\n")
        self.env = dict(os.environ, RCLONE_CONFIG=str(conf), WEBDE_SYNC_RCLONE=rclone,
                        XDG_STATE_HOME=str(tmp / "state"), XDG_CONFIG_HOME=str(tmp / "cfg"))
        for k in ("RCLONE_CONFIG_PASS", "RCLONE_PASSWORD_COMMAND", "WEBDE_SYNC_CONFIG"):
            self.env.pop(k, None)
        self.proc = subprocess.Popen(
            [SERVER, "serve", "webdav", str(self.server), "--config", os.devnull, "--addr", f"127.0.0.1:{port}",
             "--dir-cache-time", "0s"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return
            time.sleep(0.05)
        raise RuntimeError("rclone serve webdav did not start")

    def close(self):
        self.proc.terminate()
        self.proc.wait(5)

    def run(self, *args: str, answers: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args], input=answers, text=True,
                              capture_output=True, check=False, env=self.env, timeout=120)

    def sync(self, *args: str, answers: str = "y\n") -> subprocess.CompletedProcess:
        return self.run(str(self.local), "webde:sync", *args, answers=answers)

    def put(self, side: Path, rel: str, text: str) -> None:
        f = side / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)

    def remote(self, rel: str) -> Path:
        return self.server / "sync" / rel

    def baseline(self, n: int = 5) -> None:
        for i in range(n):
            self.put(self.local, f"keep{i}.txt", f"keep {i}\n")
        r = self.sync("--resync", "-y")
        assert r.returncode == 0, r.stdout + r.stderr
        time.sleep(1.1)        # WebDAV mtimes have 1 s resolution


@pytest.fixture(params=RCLONES or [None], ids=lambda p: Path(p).name if p else "no-rclone")
def env(request, tmp_path):
    if request.param is None:
        pytest.skip("set WEBDE_SYNC_TEST_RCLONE (or use `make test`) for integration tests")
    e = Env(tmp_path, request.param)
    yield e
    e.close()


def test_first_run_needs_interactive_resync_and_creates_remote(env):
    import shutil
    shutil.rmtree(env.server / "sync")
    env.put(env.local, "a.txt", "a\n")
    r = env.sync("-y")
    assert r.returncode == 1 and "resync is needed" in r.stderr
    r = env.sync(answers="y\ny\n")                     # create folder, apply
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RESYNC" in r.stdout and "a.txt   [resync" in r.stdout
    assert env.remote("a.txt").read_text() == "a\n"
    assert "Everything is in sync." in env.sync().stdout


def test_two_way_changes_same_size_edit_and_backups(env):
    env.put(env.local, "same.txt", "hello\n")
    env.put(env.local, "del.txt", "bye\n")
    env.baseline()
    env.put(env.local, "same.txt", "HELLO\n")           # same size: needs --ignore-times
    (env.local / "del.txt").unlink()
    env.put(env.server, "sync/remote.txt", "from the web\n")
    r = env.sync()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 file(s) will be replaced and 1 deleted" in r.stdout
    assert "Backups of replaced/deleted files" in r.stdout
    assert "same.txt" in r.stdout.split("Backups of")[1]
    assert "del.txt" in r.stdout.split("Backups of")[1]
    assert env.remote("same.txt").read_text() == "HELLO\n"
    assert not env.remote("del.txt").exists()
    assert (env.local / "remote.txt").read_text() == "from the web\n"
    backups = {p.name: p.read_text() for p in env.remote(ws.BACKUP_DIR).rglob("*.txt")}
    assert backups == {"same.txt": "hello\n", "del.txt": "bye\n"}
    assert "Everything is in sync." in env.sync().stdout


def test_backups_keep_the_directory_structure(env):
    env.put(env.local, "docs/2024/tax/report.txt", "v1\n")
    env.put(env.local, "photos/trip/img.jpg", "pic\n")
    env.put(env.local, "docs/notes.txt", "v1\n")
    env.baseline()
    env.put(env.local, "docs/2024/tax/report.txt", "v2 local\n")    # replaced on the server
    (env.local / "photos/trip/img.jpg").unlink()                     # deleted on the server
    env.put(env.server, "sync/docs/notes.txt", "v2 server\n")       # replaced locally
    r = env.sync("-y")
    assert r.returncode == 0, r.stdout + r.stderr

    def backups(root: Path) -> dict[str, str]:
        (stamp,) = (root / ws.BACKUP_DIR).iterdir()
        return {p.relative_to(stamp).as_posix(): p.read_text()
                for p in stamp.rglob("*") if p.is_file()}
    assert backups(env.server / "sync") == {"docs/2024/tax/report.txt": "v1\n",
                                            "photos/trip/img.jpg": "pic\n"}
    assert backups(env.local) == {"docs/notes.txt": "v1\n"}


def test_conflict_keeps_both_and_modified_file_is_not_deleted(env):
    env.put(env.local, "edit.txt", "base\n")
    env.put(env.local, "del.txt", "base\n")
    env.baseline()
    env.put(env.local, "edit.txt", "local edit\n")
    env.put(env.server, "sync/edit.txt", "server edit!\n")
    (env.local / "del.txt").unlink()
    env.put(env.server, "sync/del.txt", "changed on server\n")
    r = env.sync()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "edit.txt.conflict1 = local version" in r.stdout and "restored" in r.stdout
    assert "edit.txt.conflict2 = server version" in r.stdout
    assert "2 conflict copies wait to be merged" in r.stderr
    assert (env.local / "edit.txt.conflict1").read_text() == "local edit\n"
    assert (env.local / "edit.txt.conflict2").read_text() == "server edit!\n"
    assert env.remote("edit.txt.conflict1").exists() and env.remote("edit.txt.conflict2").exists()
    assert (env.local / "del.txt").read_text() == "changed on server\n"
    r = env.sync("-y")                                 # reminded on every run ...
    assert r.returncode == 0 and "edit.txt.conflict1" in r.stderr
    r = env.sync("-n")
    assert "edit.txt.conflict2" in r.stderr
    (env.local / "edit.txt.conflict1").rename(env.local / "edit.txt")   # merged
    (env.local / "edit.txt.conflict2").unlink()
    r = env.sync()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "conflict" not in r.stderr                  # ... until resolved
    assert not env.remote("edit.txt.conflict2").exists()


def test_resolved_conflict_reports_winner_and_loser(env):
    cfg = env.tmp / "cfg" / ws.APP
    cfg.mkdir(parents=True)
    (cfg / "profiles.toml").write_text(f'[p]\nlocal = "{env.local}"\nremote = "webde:sync"\n'
                                       'conflict_resolve = "local"\n')
    for i in range(5):
        env.put(env.local, f"keep{i}.txt", f"keep {i}\n")
    env.put(env.local, "edit.txt", "base\n")
    assert env.run("p", "--resync", "-y").returncode == 0
    time.sleep(1.1)
    env.put(env.local, "edit.txt", "local edit\n")
    env.put(env.server, "sync/edit.txt", "server edit!\n")
    r = env.run("p", answers="y\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "edit.txt = local version, edit.txt.conflict1 = server version" in r.stdout
    assert env.remote("edit.txt").read_text() == "local edit\n"
    assert (env.local / "edit.txt.conflict1").read_text() == "server edit!\n"


def test_empty_side_is_refused(env):
    env.baseline(6)
    for p in env.local.glob("keep*.txt"):
        p.unlink()                                     # e.g. an unmounted disk
    r = env.sync("-y")
    assert r.returncode == 1 and "completely empty" in r.stderr
    assert len(list(env.remote("").glob("keep*.txt"))) == 6


def test_mass_delete_is_blocked_until_forced(env):
    env.baseline(6)
    for p in sorted(env.local.glob("keep*.txt"))[1:]:
        p.unlink()                                     # 5 of 6 files gone
    r = env.sync("-y")
    assert r.returncode == 1 and "--force" in r.stderr
    assert len(list(env.remote("").glob("keep*.txt"))) == 6
    r = env.sync("--force")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 file(s) will be replaced and 5 deleted" in r.stdout
    assert len(list(env.remote("").glob("keep*.txt"))) == 1
    assert len(list(env.remote(ws.BACKUP_DIR).rglob("keep*.txt"))) == 5


def test_dry_run_and_declining_change_nothing(env):
    env.baseline()
    env.put(env.local, "new.txt", "new\n")
    r = env.sync("-n")
    assert r.returncode == 0 and "new.txt   [new" in r.stdout
    r = env.sync(answers="n\n")
    assert r.returncode == ws.EXIT_ABORTED and "nothing was changed" in r.stdout
    assert not env.remote("new.txt").exists()


def test_verify_reports_same_size_server_edit_bisync_cannot_see(env):
    env.put(env.local, "doc.txt", "version A\n")
    env.baseline()
    env.put(env.server, "sync/doc.txt", "version B\n")  # same size, made on the server
    assert "Everything is in sync." in env.sync().stdout   # rclone limitation
    r = env.sync("--verify", "-y")
    assert r.returncode == 1 and "content differs:" in r.stdout and "doc.txt" in r.stdout


def test_profile_mode_filters_and_resync_on_filter_change(env):
    cfg = env.tmp / "cfg" / ws.APP
    cfg.mkdir(parents=True)
    conf = cfg / "profiles.toml"
    conf.write_text(f'[docs]\nlocal = "{env.local}"\nremote = "webde:sync"\n'
                    'filters = ["- *.tmp"]\n')
    env.put(env.local, "a.txt", "a\n")
    env.put(env.local, "junk.tmp", "x\n")
    r = env.run("docs", "--resync", "-y")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.remote("a.txt").exists() and not env.remote("junk.tmp").exists()
    assert "docs" in env.run("--list").stdout
    conf.write_text(conf.read_text().replace("*.tmp", "*.bak"))
    r = env.run("docs", "-y")
    assert r.returncode == 1 and "filter rules changed" in r.stderr
    r = env.run("docs", answers="y\n")
    assert r.returncode == 0 and "RESYNC" in r.stdout and env.remote("junk.tmp").exists()


def test_old_backups_are_pruned(env):
    env.baseline()
    old = (datetime.now().astimezone() - timedelta(days=40)).strftime(ws.STAMP_FMT)
    for side in (env.local, env.server / "sync"):
        env.put(side, f"{ws.BACKUP_DIR}/{old}/x.txt", "old\n")
        env.put(side, f"{ws.BACKUP_DIR}/not-ours/x.txt", "keep\n")
    env.put(env.local, "keep0.txt", "changed\n")
    r = env.sync()
    assert r.returncode == 0, r.stdout + r.stderr
    for side in (env.local, env.server / "sync"):
        names = {p.name for p in (side / ws.BACKUP_DIR).iterdir()}
        assert old not in names and "not-ours" in names


def test_profile_lock_blocks_second_run(env):
    env.baseline()
    lock = next((env.tmp / "state" / ws.APP).glob("adhoc-*")) / "lock"
    with open(lock) as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        r = env.sync("-y")
    assert r.returncode == 1 and "in progress" in r.stderr


def test_remote_folder_vanished_is_not_treated_as_mass_delete(env):
    import shutil
    env.baseline()
    shutil.rmtree(env.server / "sync")
    r = env.sync("-y")
    assert r.returncode == 1 and "not found" in r.stderr
    assert len(list(env.local.glob("keep*.txt"))) == 5


def test_usage_errors(env):
    assert env.run("/nonexistent", "webde:sync").returncode == ws.EXIT_USAGE
    r = env.run(str(env.local), "nowhere:sync")
    assert r.returncode == ws.EXIT_USAGE and "not configured" in r.stderr
    r = env.run(str(env.local), "local-dir")
    assert r.returncode == ws.EXIT_USAGE and "NAME:path" in r.stderr
    assert env.run("nosuchprofile").returncode == ws.EXIT_USAGE
    r = env.sync("--yes", "--force")
    assert r.returncode == ws.EXIT_USAGE and "reviewed preview" in r.stderr


def test_encrypted_config_needs_password_and_works_with_it(env):
    pw_env = dict(env.env, RCLONE_PASSWORD_COMMAND="echo s3cret")
    for tool in RCLONES:                # `config encryption` exists since v1.67
        r = subprocess.run([tool, "config", "encryption", "set"], env=pw_env,
                           capture_output=True, text=True, check=False)
        if r.returncode == 0:
            break
    else:
        pytest.skip("no rclone with `config encryption set` available")
    assert "RCLONE_ENCRYPT_V0:" in Path(env.env["RCLONE_CONFIG"]).read_text()
    env.put(env.local, "a.txt", "a\n")
    r = env.sync("--resync", "-y")
    assert r.returncode == 1 and "RCLONE_PASSWORD_COMMAND" in r.stderr
    env.env = pw_env
    r = env.sync("--resync", "-y")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.remote("a.txt").read_text() == "a\n"


def test_stale_bisync_lock_is_removed(env):
    env.baseline()
    workdir = next((env.tmp / "state" / ws.APP).glob("adhoc-*")) / "bisync"
    dead = subprocess.Popen(["true"])
    dead.wait()
    (workdir / "stale.lck").write_text(json.dumps({"PID": str(dead.pid)}))
    env.put(env.local, "new.txt", "n\n")
    r = env.sync("-y")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "removed a stale bisync lock" in r.stderr and env.remote("new.txt").exists()


def test_ctrl_c_stops_cleanly_and_next_run_completes(env):
    import signal
    env.baseline()
    for name in ("big1.bin", "big2.bin"):
        (env.local / name).write_bytes(os.urandom(6_000_000))
    p = subprocess.Popen([sys.executable, str(SCRIPT), str(env.local), "webde:sync", "-y", "--",
                          "--bwlimit", "1M", "--transfers", "1"], env=env.env, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    time.sleep(3)
    os.killpg(p.pid, signal.SIGINT)                   # what Ctrl-C in a terminal does
    out = p.communicate(timeout=60)[0]
    assert p.returncode == 130 and "Interrupted" in out, out
    r = env.sync("-y")
    assert r.returncode == 0, r.stdout + r.stderr
    for name in ("big1.bin", "big2.bin"):
        assert env.remote(name).read_bytes() == (env.local / name).read_bytes()
