"""Tests for filesync.py.

Unit tests cover the bisync log parser and the profile/argument handling.
Integration tests run the real script against a real `rclone serve webdav`
server (fixed version, $FILESYNC_TEST_SERVER) whose client remote uses
`vendor = other` -- like web.de, it cannot
store modification times, so the server reports upload times.  They run once
per rclone binary listed in $FILESYNC_TEST_RCLONE (space separated; `make
test` downloads v1.66.0 and the current release) and are skipped otherwise.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "filesync.py"
sys.path.insert(0, str(ROOT))
import filesync as ws

RCLONES = [p for p in os.environ.get("FILESYNC_TEST_RCLONE", "").split() if Path(p).is_file()]
# The WebDAV test server is one fixed binary: `rclone serve webdav` v1.76.0 itself
# stalls for minutes on some MOVEs into new folders (a server bug, not bisync's).
SERVER = os.environ.get("FILESYNC_TEST_SERVER") or (RCLONES[0] if RCLONES else "")


# ------------------------------------------------------------------ unit tests

def jl(msg: str, level: str = "notice", obj: str | None = None, **fields) -> str:
    d = {"level": level, "msg": msg, **fields}
    if obj:
        d["object"] = obj
    return json.dumps(d)


def skipped(action: str, obj: str, size: int) -> str:
    """A dry-run entry as rclone logs it (sizes >= 1 KiB are shown with units)."""
    shown = str(size) if size < 1024 else f"{size / 1024:.3f}Ki"
    return jl(f"Skipped {action} as --dry-run is set (size {shown})", obj=obj,
              skipped=action, size=size)


def parse(lines, local="/data/local", remote="cloud:sync"):
    return ws.parse_report(ws.log_messages(lines), Path(local), remote)


def test_parse_normal_run_with_ansi_and_absolute_local_paths():
    rep = parse([
        jl("- \x1b[36mPath1\x1b[0m    File changed: size (larger), time (newer) - a b - c.txt"),
        jl("- Path1    File was deleted          - gone.txt"),
        jl("- Path2    File is new               - sub/new.txt"),
        jl("Path1:    2 changes:    0 new,    1 modified,    1 deleted", "info"),
        jl("- Path1    Queue copy to Path2       - cloud:sync/a b - c.txt"),
        jl("- Path2    Queue delete              - cloud:sync/gone.txt"),
        jl("- Path2    Queue copy to Path1       - /data/local/sub/new.txt"),
        skipped("copy", "sub/new.txt", 50000),
        skipped("move", "a b - c.txt", 1500),
        skipped("copy", "a b - c.txt", 2048),
        jl("Can't follow symlink without -L/--copy-links", "warning", obj="link"),
        jl("Bisync successful", "info"),
    ])
    assert rep.upload == {"a b - c.txt": "changed: size (larger), time (newer)"}
    assert rep.download == {"sub/new.txt": "new"}
    assert rep.delete_remote == ["gone.txt"] and rep.delete_local == []
    assert rep.sizes == {"sub/new.txt": 50000, "a b - c.txt": 2048}
    assert rep.replaced == {"a b - c.txt"}
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
        jl("- Path1    Queue copy to Path2       - cloud:sync/edit.txt.conflict1"),
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


def test_parse_resolved_conflict_names_the_winner_and_is_counted_once():
    rep = parse([
        jl("- Path1    File changed: size (larger) - d/edit.txt"),
        jl("- WARNING  New or changed in both paths       - d/edit.txt"),
        jl("- Path1    Queue copy to Path2       - cloud:sync/d/edit.txt"),
        jl("- Path2    Queue copy to Path1       - /data/local/d/edit.txt.conflict3"),
        skipped("copy", "d/edit.txt", 11),
        skipped("move", "d/edit.txt", 13),
    ])
    assert rep.conflicts == {"d/edit.txt": [("d/edit.txt.conflict3", "server")]}
    assert rep.upload == {} and rep.download == {} and rep.n_actions == 1
    assert rep.replaced == set()
    assert ws.conflict_text("d/edit.txt", rep.conflicts["d/edit.txt"]) == (
        "changed on both sides: edit.txt = local version, edit.txt.conflict3 = server version")


def test_parse_resync_copies_large_files_and_errors():
    rep = parse([
        jl("- Path2    Resync is copying files to         - Path1", "info"),
        skipped("copy", "remote.txt", 1),
        jl("- Path1    Resync is copying files to         - Path2", "info"),
        skipped("copy", "photo.jpg", 50000),
        jl("Copied (new)", "info", obj="local.txt"),
        jl("Bisync aborted. Must run --resync to recover.", "error"),
        "panic: something not json",
    ], remote="cloud:")
    assert rep.download == {"remote.txt": "resync"}
    assert rep.upload == {"photo.jpg": "resync", "local.txt": "resync"}
    assert rep.sizes["photo.jpg"] == 50000
    assert rep.needs_resync and len(rep.errors) == 2


def test_parse_stats_transfer_count():
    rep = parse([jl("\nTransferred:   \t    2.955 KiB / 2.955 KiB, 100%, 0 B/s, ETA -\n"
                    "Checks:                 2 / 2, 100%\nTransferred:            3 / 3, 100%\n"
                    "Elapsed time:         0.0s\n", "info")])
    assert rep.stats_transfers == 3


def test_general_flags_drop_bisync_only_flags():
    flags = ["--tpslimit", "4", "--compare", "size", "--conflict-loser=num", "--resilient",
             "--config", "/x.conf", "-1", "--max-lock", "2m", "-v"]
    assert ws.general_flags(flags) == ["--tpslimit", "4", "--config", "/x.conf", "-v"]


def test_strip_filter_flags_drops_filters_keeps_the_rest():
    flags = ["--exclude", "*.log", "--max-age=2y", "--include-from", "/f", "--min-size",
             "10k", "--tpslimit", "4", "--config", "/x.conf", "-v"]
    assert ws.strip_filter_flags(flags) == ["--tpslimit", "4", "--config", "/x.conf", "-v"]
    flags = ["-f", "- *.log", "--exclude-if-present", ".nosync", "--ignore-case", "-f=+ a",
             "--metadata-exclude", "a=b", "--hash-filter=1/2", "--files-from0", "/f",
             "--delete-excluded", "--bwlimit", "2M"]
    assert ws.strip_filter_flags(flags) == ["--bwlimit", "2M"]


def test_flag_values_are_not_taken_for_flags():
    flags = ["--max-depth", "-1", "--tpslimit", "4", "--filter", "--resilient", "-P"]
    assert ws.general_flags(flags) == flags          # -1 and --resilient are values here
    assert ws.strip_filter_flags(ws.general_flags(flags)) == ["--tpslimit", "4", "-P"]
    ws.check_user_flags(ws.split_short_flags(flags))  # ... and -1 is not --resync


@pytest.mark.parametrize("flags, expected", [
    (["-vn"], ["-v", "-n"]), (["-1v", "-P"], ["-1", "-v", "-P"]),
    (["-vn=true"], ["-v", "-n=true"]), (["-vn=false"], ["-v", "-n=false"]),
    (["-v1=true"], ["-v", "-1=true"]), (["-vi=false"], ["-v", "-i=false"]),
    (["-f- *.log", "-vv"], ["-f", "- *.log", "-v", "-v"]),
    (["-vf", "-n"], ["-v", "-f", "-n"]),             # -n is the filter rule here
    (["-vf=- x"], ["-v", "-f=- x"]),
    (["-vf- *=foo"], ["-v", "-f", "- *=foo"]),
    (["-vf- *.LOG", "-11", "- *.pdf"], ["-v", "-f", "- *.LOG", "-11", "- *.pdf"]),
    (["-f=- x", "--exclude", "-vn", "-5d"], ["-f=- x", "--exclude", "-vn", "-5d"]),
    (["--bwlimit", "2M", "--dry-run=true"], ["--bwlimit", "2M", "--dry-run=true"]),
])
def test_split_short_flags(flags, expected):
    assert ws.split_short_flags(flags) == expected


@pytest.mark.parametrize("flags", [
    ["--force"], ["--tpslimit", "4", "--force"], ["--resync"], ["-1"], ["--resync-mode", "path2"],
    ["--resync-mode=path2"], ["--dry-run"], ["--dry-run=true"], ["-n"], ["-vn"], ["-vn=true"],
    ["-v1=true"], ["-vi=false"], ["-i"],
    ["--workdir", "/x"], ["--filters-file=/x"], ["--backup-dir1", "/x"], ["--backup-dir2=r:x"],
    ["--log-file", "/x"], ["--use-json-log=false"], ["-f", "- *.log", "-n"],
])
def test_wrapper_owned_flags_are_refused(flags):
    with pytest.raises(ws.UsageError, match="is not accepted in rclone_flags"):
        ws.check_user_flags(ws.split_short_flags(flags))


def test_safe_display_escapes_control_chars_and_preview_output(capsys):
    assert ws.safe_display("ümläut ß.txt") == "ümläut ß.txt"
    assert ws.safe_display('a\x1b[2K\rFAKE\x1b]0;pwn\x07\x9b.txt') == (
        r"a\x1b[2K\rFAKE\x1b]0;pwn\x07\x9b.txt")
    rep = parse([
        jl('- Path2    Queue copy to Path1       - '
           '"/data/local/a\\x1b[2K\\rFAKE\\x1b]0;pwn\\x07.txt"'),
    ])
    ws.show_report(rep, max_show=25, planned=True)
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\r" not in out and "\x07" not in out
    assert r"a\x1b[2K\rFAKE\x1b]0;pwn\x07.txt" in out


def test_rclone_strips_owned_env_vars(monkeypatch):
    monkeypatch.setenv("RCLONE_FORCE", "true")
    monkeypatch.setenv("RCLONE_DRY_RUN", "true")
    monkeypatch.setenv("RCLONE_RESYNC", "true")
    monkeypatch.setenv("RCLONE_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("RCLONE_CONFIG", "/tmp/my.conf")
    rc = ws.Rclone("rclone")
    for k in ("RCLONE_FORCE", "RCLONE_DRY_RUN", "RCLONE_RESYNC", "RCLONE_LOG_LEVEL"):
        assert k not in rc.env
    assert rc.env["RCLONE_CONFIG"] == "/tmp/my.conf"


@pytest.mark.parametrize("flags", [
    ["--tpslimit", "4", "--bwlimit", "2M"], ["--filter", "--force"], ["-f", "-n"], ["-vf", "-n"],
    ["--exclude=-n"], ["--max-depth", "-1"], ["--max-delete", "10"], ["-vv", "-P"],
])
def test_other_flags_and_flag_values_are_accepted(flags):
    ws.check_user_flags(ws.split_short_flags(flags))


def test_unquote_returns_names_only():
    assert ws.unquote('"quo\\"te"') == 'quo"te'
    assert ws.unquote('"tag\\U000e0041"') == "tag\U000e0041"   # Go escape unknown to JSON
    assert ws.unquote('"a","b"') == '"a","b"'                   # a name, not a tuple
    assert ws.unquote('"12"') == "12" and ws.unquote("plain.txt") == "plain.txt"


def test_file_names_that_look_like_bisync_messages_change_nothing():
    rep = parse([
        jl("- Path2    File is new               - Must run --resync.txt"),
        jl("- Path2    File is new               - WARNING Safety abort: Bisync successful"),
        jl("- Path2    Queue copy to Path1       - /data/local/Must run --resync.txt"),
        skipped("copy", "Failed to bisync.txt", 2),
        skipped("copy", "WARNING.txt", 2),
        jl("Copied (new)", "info", obj="Bisync successful"),
        jl("Copied (new)", "info", obj="Safety abort"),
        jl("Copied (new)", "info", obj="Safety abort: all files were changed.txt"),
        jl("Copied (new)", "info", obj="- Path2    Queue delete    - victim.txt"),
        jl("Copied (new)", "info", obj="Empty prior Path1 listing"),
        jl("Copied (new)", "info", obj="Failed to bisync: prior lock file found"),
        jl("Copied (new)", "info", obj="Path1:    7 changes"),
        jl("Can't follow symlink without -L/--copy-links", "notice", obj='"a","b"'),
    ])
    assert not (rep.success or rep.needs_resync or rep.safety_abort or rep.lock_error
                or rep.empty_baseline or rep.errors or rep.warnings or rep.bisync_changes)
    assert rep.delete_remote == [] and rep.delete_local == []
    assert rep.download == {"Must run --resync.txt": "new"}
    assert rep.symlinks == ['"a","b"']


def test_real_status_messages_are_recognised():
    """The wording and object fields as rclone v1.66 and v1.76 log them."""
    assert parse([jl('too many deletes (>50%, 5 of 6) on Path1 "/l/". Run with --force if '
                     'desired.', "error", obj="Safety abort")]).safety_abort
    assert parse([jl('Safety abort: all files were changed on Path1 "/l/". Run with --force '
                     'if desired.', "error")]).safety_abort
    for line in ("2026/11/23 23:51:52 Failed to bisync: prior lock file found: /s/a..b.lck",
                 jl("Failed to bisync: prior lock file found: /s/a..b.lck")):
        rep = parse([line])
        assert rep.lock_error and rep.errors
    assert parse([jl("Empty current Path1 listing. Cannot sync to an empty directory: /s/x",
                     "error")]).empty_side
    assert parse([jl("Empty prior Path1 listing. Cannot sync to an empty directory: /s/x",
                     "error")]).empty_baseline
    assert parse([jl("Bisync aborted. Must run --resync to recover.", "error")]).needs_resync
    assert parse([jl("Failed to bisync: too many deletes")]).errors
    assert parse([jl("WARNING: something new", "notice")]).warnings
    assert parse([jl("- WARNING  Something else             - x.txt")]).warnings


def test_config_and_path_errors_are_usage_errors(tmp_path):
    conf = tmp_path / "profiles.toml"
    conf.write_bytes(b'[p]\nlocal = "/x"\nremote = "r:"\n# caf\xe9\n')    # not UTF-8
    with pytest.raises(ws.UsageError, match="cannot read config"):
        ws.load_config(conf)
    with pytest.raises(ws.UsageError, match="home directory"):
        ws.profile_from_dict("p", {"local": "~no_such_user_xyz/d", "remote": "r:"}, "t")
    rules = tmp_path / "rules"
    rules.write_bytes(b"- caf\xe9*\n")
    p = ws.Profile(name="p", local=tmp_path, remote="r:", filters_file=rules)
    with pytest.raises(ws.UsageError, match="filters_file"):
        ws.render_filters(p, ws.State(tmp_path / "st"))


def test_clear_stale_listings_drops_listings_but_not_locks(tmp_path):
    st = ws.State(tmp_path)
    st.workdir.mkdir(parents=True)
    for name in ("old..pair.path1.lst", "old..pair.path2.lst", "new..pair.path1.lst.new",
                 "keep.lck"):
        (st.workdir / name).write_text("x\n")
    ws.clear_stale_listings(st)
    assert [p.name for p in st.workdir.iterdir()] == ["keep.lck"]


@pytest.mark.parametrize("url, plain", [
    ("http://webdav.example.org", True), ("HTTP://10.0.0.5:8080/dav", True),
    ("http://127.evil.example", True), ("http://127.0.0.1@evil.example/dav", True),
    ("https://webdav.smartdrive.web.de", False), ("http://127.0.0.1:18080", False),
    ("http://localhost/dav", False), ("http://[::1]:80", False), ("", False),
])
def test_plain_http_url(url, plain):
    assert ws.plain_http_url(url) is plain


@pytest.mark.parametrize("remote", [
    ":webdav,url=https://x,pass=abc:", "cloud,pass=abc:/x",
    ":s3,secret_access_key=abc:b", ":sftp,key=abc:",
    ":webdav,url=https://user:mysecret@webdav.example.com/dav:",
    ":http,url=https://u:p@example.com:",
])
def test_inline_credentials_are_rejected(remote):
    with pytest.raises(ws.UsageError, match="credentials"):
        ws.check_remote_configured(None, remote)


def test_stale_lock_with_reused_pid_is_removed(tmp_path, capsys):
    st = ws.State(tmp_path)
    st.workdir.mkdir()
    lck = st.workdir / "x.lck"
    lck.write_text(json.dumps({"PID": str(os.getpid())}))   # alive, but not rclone
    assert not ws.is_rclone_bisync(os.getpid())
    ws.clear_stale_bisync_locks(st)
    assert not lck.exists() and "stale" in capsys.readouterr().err


def test_pending_conflicts_come_from_bisync_listing(tmp_path):
    local = tmp_path / "local"
    (local / "d").mkdir(parents=True)
    for name in ("d/a.txt.conflict1", 'q"x.conflict2'):
        (local / name).write_text("x")
    st = ws.State(tmp_path / "st")
    st.workdir.mkdir(parents=True)
    (st.workdir / "l..r.path1.lst").write_text(
        '# bisync listing v1 from 2026-11-23\n'
        '-        1 - - 2026-11-23T19:25:59.000000000+0000 "d/a.txt.conflict1"\n'
        '-        1 - - 2026-11-23T19:25:59.000000000+0000 "q\\"x.conflict2"\n'
        '-        1 - - 2026-11-23T19:25:59.000000000+0000 "gone.txt.conflict1"\n'
        '-        1 - - 2026-11-23T19:25:59.000000000+0000 "plain.txt"\n')
    p = ws.Profile(name="p", local=local, remote="r:")
    assert ws.pending_conflicts(p, st) == ["d/a.txt.conflict1", 'q"x.conflict2']


def test_parse_quoted_names_and_failed_files():
    rep = parse([
        jl('- Path1    File is new               - "quo\\"te.txt"'),
        jl('- Path1    Queue copy to Path2       - "cloud:sync/quo\\"te.txt"'),
        jl('- Path1    Queue copy to Path2       - "cloud:sync/back\\\\slash.txt"'),
        skipped("copy", 'quo"te.txt', 9),
        jl("Failed to copy: failed to open source object: open /l/s.txt: permission denied",
           "error", obj="s.txt"),
        jl("Failed to copy: failed to open source object: open /l/s.txt: permission denied",
           "error", obj="s.txt"),
    ])
    assert rep.upload == {'quo"te.txt': "new", "back\\slash.txt": "changed"}
    assert rep.sizes == {'quo"te.txt': 9}
    assert rep.failed == {"s.txt": "permission denied"} and len(rep.errors) == 1


@pytest.mark.parametrize("given, expected", [
    ("cloud:Docs/", "cloud:Docs"), ("cloud:/Docs//", "cloud:/Docs"), ("cloud:/", "cloud:/"),
    ("cloud:", "cloud:"), ("cloud:Docs", "cloud:Docs")])
def test_remote_trailing_slashes_are_normalized(given, expected):
    assert ws.profile_from_dict(None, {"local": "/x", "remote": given}, "t").remote == expected


def test_backup_stamp_differs_from_last_run(tmp_path, monkeypatch):
    st = ws.State(tmp_path)
    times = iter([ws.datetime(2026, 1, 1, 12, 0, 0, 100), ws.datetime(2026, 1, 1, 12, 0, 0, 900),
                  ws.datetime(2026, 1, 1, 12, 0, 1, 50)])

    class FakeDatetime(ws.datetime):
        @classmethod
        def now(cls, tz=None):
            return next(times)
    monkeypatch.setattr(ws, "datetime", FakeDatetime)
    monkeypatch.setattr(ws.time, "sleep", lambda s: None)
    (tmp_path / "last-stamp").write_text("2026-01-01_120000\n")
    assert ws.unique_stamp(st)[1] == "2026-01-01_120001"
    assert (tmp_path / "last-stamp").read_text().strip() == "2026-01-01_120001"


def test_relativizer_handles_remote_root_forms():
    for remote, shown in (("cloud:", "cloud:/x/y"), ("cloud:/", "cloud:/x/y"),
                          ("cloud:/Docs/", "cloud:/Docs/x/y"), ("cloud:Docs", "cloud:Docs/x/y")):
        assert ws._relativizer(Path("/l"), remote)("Path2", shown) == "x/y"


def test_remote_join():
    assert ws.remote_join("cloud:", "b") == "cloud:b"
    assert ws.remote_join("cloud:/", "b") == "cloud:/b"
    assert ws.remote_join("cloud:/Docs", "b") == "cloud:/Docs/b"


@pytest.mark.parametrize("raw, err", [
    ({"local": "/x"}, "'remote' is required"),
    ({"local": "/x", "remote": "r:", "max_delte": 5}, "unknown key"),
    ({"local": "/x", "remote": "r:", "max_delete": True}, "type int"),
    ({"local": "/x", "remote": "r:", "max_delete": 101}, "percentage"),
    ({"local": "/x", "remote": "r:", "filters": ["ok", 3]}, "list of strings"),
    ({"local": "/x", "remote": "r:", "ignore_times": "yes"}, "type bool"),
    ({"local": "/x", "remote": "r:", "verify_download": 1}, "type bool"),
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
    st = ws.State(tmp_path / ".local/state/file-sync/p")
    lines = ws.render_filters(p, st).splitlines()
    assert lines[1] == f"- /{ws.BACKUP_DIR}/**"
    assert lines[2] == "- /.local/state/file-sync/p/**" and lines[3] == "+ **"


def test_bisync_args_ignore_times_only_outside_resync(tmp_path):
    p = ws.Profile(name="p", local=tmp_path, remote="r:/x", check_access=True,
                   ignore_times=True)
    st = ws.State(tmp_path / "st")
    normal = ws.bisync_args(p, st, resync=False, dry_run=True, force=False, stamp="S", extra=[])
    resync = ws.bisync_args(p, st, resync=True, dry_run=False, force=True, stamp="S", extra=["-q"])
    assert "--ignore-times" in normal and "--check-access" in normal and "--dry-run" in normal
    assert "--ignore-times" not in resync and "--resync-mode" in resync and "--force" in resync
    assert resync[-1] == "-q"
    assert resync[resync.index("--backup-dir2") + 1] == f"r:/x/{ws.BACKUP_DIR}/S"


def test_bisync_args_no_ignore_times_for_mtime_capable_remotes(tmp_path):
    p = ws.Profile(name="p", local=tmp_path, remote="r:/x")
    st = ws.State(tmp_path / "st")
    for ignore in (None, False):        # None = auto-detected later, False = explicit
        p.ignore_times = ignore
        args = ws.bisync_args(p, st, resync=False, dry_run=False, force=False,
                             stamp="S", extra=[])
        assert "--ignore-times" not in args


def test_remote_features_autodetects_ignore_times_and_verify_download(tmp_path, capsys):
    if not RCLONES:
        pytest.skip("set FILESYNC_TEST_RCLONE (or use `make test`) for rclone-based tests")
    conf = tmp_path / "rclone.conf"
    conf.write_text(
        "[wdav]\ntype = webdav\nurl = http://webdav.example.org/dav\n"
        "[disk]\ntype = local\n"
        "[my cloud]\ntype = local\n"
        "[wdav-crypt]\ntype = crypt\nremote = wdav:/enc\n"
        "[disk-crypt]\ntype = crypt\nremote = disk:/enc\n"
        "[wdav-alias]\ntype = alias\nremote = wdav:/sub\n"
    )
    rc = ws.Rclone(RCLONES[0])
    rc.env["RCLONE_CONFIG"] = str(conf)
    ws.check_remote_configured(rc, "my cloud:/x")                # spaces in remote name
    assert (ws.remote_features(rc, "wdav:/x").mtime,
            ws.remote_features(rc, "wdav:/x").hashes) == (False, False)
    assert ws.remote_features(rc, "disk:/x").mtime and ws.remote_features(rc, "disk:/x").hashes
    assert not ws.remote_features(rc, ":webdav,url=https://x:").mtime
    assert (ws.remote_features(rc, ":webdav:/x").mtime,
            ws.remote_features(rc, ":webdav:/x").hashes) == (False, False)
    # Wrapped remotes inherit mtime/hash limitations and crypt never has hashes
    assert (ws.remote_features(rc, "wdav-crypt:/x").mtime,
            ws.remote_features(rc, "wdav-crypt:/x").hashes) == (False, False)
    assert (ws.remote_features(rc, "disk-crypt:/x").mtime,
            ws.remote_features(rc, "disk-crypt:/x").hashes) == (True, False)
    assert (ws.remote_features(rc, "wdav-alias:/x").mtime,
            ws.remote_features(rc, "wdav-alias:/x").hashes) == (False, False)
    # Plain http warning follows wrapped remotes and inline overrides on named remotes
    ws.warn_plain_http(rc, "wdav-crypt:/x")
    assert "plain http://" in capsys.readouterr().err
    ws.warn_plain_http(rc, "disk,url=http://evil.example:/x")
    assert "plain http://" in capsys.readouterr().err
    p = ws.profile_from_dict("p", {"local": "/x", "remote": "wdav:/x"}, "t")
    ws.resolve_remote_options(p, ws.remote_features(rc, p.remote))
    assert p.ignore_times is True and p.verify_download is True
    p = ws.profile_from_dict("p", {"local": "/x", "remote": "disk:/x"}, "t")
    ws.resolve_remote_options(p, ws.remote_features(rc, p.remote))
    assert p.ignore_times is False and p.verify_download is False
    p = ws.profile_from_dict("p", {"local": "/x", "remote": "wdav:/x",
                                   "ignore_times": False}, "t")
    ws.resolve_remote_options(p, ws.remote_features(rc, p.remote))
    assert p.ignore_times is False and p.verify_download is True  # explicit wins


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
        conf.write_text(f"[cloud]\ntype = webdav\nurl = http://127.0.0.1:{port}\n"
                        "vendor = other\n[disk]\ntype = local\n")
        self.env = dict(os.environ, RCLONE_CONFIG=str(conf), FILESYNC_RCLONE=rclone,
                        XDG_STATE_HOME=str(tmp / "state"), XDG_CONFIG_HOME=str(tmp / "cfg"))
        for k in ("RCLONE_CONFIG_PASS", "RCLONE_PASSWORD_COMMAND", "FILESYNC_CONFIG"):
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
        return self.run(str(self.local), "cloud:sync", *args, answers=answers)

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
        pytest.skip("set FILESYNC_TEST_RCLONE (or use `make test`) for integration tests")
    e = Env(tmp_path, request.param)
    yield e
    e.close()


def test_mtime_capable_remote_syncs_same_size_edits_without_ignore_times(env):
    srv = env.tmp / "srv2" / "sync"
    srv.mkdir(parents=True)
    pair = (str(env.local), f"disk:{srv}")
    for i in range(5):                    # enough files that one edit is no "all changed"
        env.put(env.local, f"keep{i}.txt", f"keep {i}\n")
    env.put(env.local, "edit.txt", "old text")
    r = env.run(*pair, "--resync", "-y")
    assert r.returncode == 0, r.stdout + r.stderr
    env.put(env.local, "edit.txt", "new text")      # same size, changed mtime
    r = env.run(*pair, "-y")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (srv / "edit.txt").read_text() == "new text"
    (srv / "edit.txt").write_text("sec text")       # same size, remote-side edit
    r = env.run(*pair, "-y")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (env.local / "edit.txt").read_text() == "sec text"
    assert not (env.local / "edit.txt.conflict1").exists()   # detected, not conflicted


def test_first_run_needs_interactive_resync_and_creates_remote(env):
    import shutil
    shutil.rmtree(env.server / "sync")
    env.put(env.local, "a.txt", "a\n")
    (env.local / "photo.jpg").write_bytes(os.urandom(50_000))
    r = env.sync("-y")
    assert r.returncode == 1 and "resync is needed" in r.stderr
    r = env.sync(answers="y\ny\n")                     # create folder, apply
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RESYNC" in r.stdout and "a.txt   [resync" in r.stdout
    assert "photo.jpg   [resync, 48.8 KiB]" in r.stdout and "Apply these 2 change(s)" in r.stdout
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
    (cfg / "profiles.toml").write_text(f'[p]\nlocal = "{env.local}"\nremote = "cloud:sync"\n'
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
    assert "Apply these 1 change(s)" in r.stdout and "will be replaced" not in r.stdout
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
    assert "rclone copyto cloud:sync/doc.txt" in r.stdout
    r = env.sync("--verify", "-y", "--max-show", "0")
    assert "... and 1 more (use --list-all)" in r.stdout


def test_profile_mode_filters_and_resync_on_filter_change(env):
    cfg = env.tmp / "cfg" / ws.APP
    cfg.mkdir(parents=True)
    conf = cfg / "profiles.toml"
    conf.write_text(f'[docs]\nlocal = "{env.local}"\nremote = "cloud:sync"\n'
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


@pytest.mark.parametrize("flags", [
    ("--exclude", "*.log"), ("-f", "- *.log"), ("--exclude-if-present", ".nosync"),
    ("--ignore-case", "-vf- *.LOG"),
], ids=lambda f: f[0])
def test_backup_pruning_survives_filter_flags_after_double_dash(env, flags):
    env.baseline()
    old = (datetime.now().astimezone() - timedelta(days=40)).strftime(ws.STAMP_FMT)
    for side in (env.local, env.server / "sync"):
        env.put(side, f"{ws.BACKUP_DIR}/{old}/x.txt", "old\n")
    env.put(env.local, "keep0.txt", "changed\n")
    r = env.sync("--", *flags)                   # for bisync; must not reach purge/lsf
    assert r.returncode == 0, r.stdout + r.stderr
    assert "warning" not in r.stderr, r.stderr
    assert "Backups of" in r.stdout               # the backup listing is not filtered away
    for side in (env.local, env.server / "sync"):
        assert not (side / ws.BACKUP_DIR / old).exists()


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
    assert env.run("/nonexistent", "cloud:sync").returncode == ws.EXIT_USAGE
    r = env.run(str(env.local), "nowhere:sync")
    assert r.returncode == ws.EXIT_USAGE and "not configured" in r.stderr
    r = env.run(str(env.local), "local-dir")
    assert r.returncode == ws.EXIT_USAGE and "NAME:path" in r.stderr
    assert env.run("nosuchprofile").returncode == ws.EXIT_USAGE
    r = env.run(str(env.local), ":webdav,url=http://x,pass=abc:")
    assert r.returncode == ws.EXIT_USAGE and "credentials" in r.stderr
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
    p = subprocess.Popen([sys.executable, str(SCRIPT), str(env.local), "cloud:sync", "-y", "--",
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


def write_profile(env: Env, name: str, **keys) -> None:
    cfg = env.tmp / "cfg" / ws.APP
    cfg.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k} = {json.dumps(v)}\n" for k, v in keys.items())
    with open(cfg / "profiles.toml", "a") as f:
        f.write(f'[{name}]\nlocal = "{env.local}"\nremote = "cloud:sync"\n{body}')


def test_resync_overwrite_is_announced_and_permanent_without_backups(env):
    write_profile(env, "nb", backups=False)
    env.put(env.local, "doc.txt", "the local version\n")
    env.put(env.server, "sync/doc.txt", "server\n")
    r = env.run("nb", answers="y\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "overwritten PERMANENTLY" in r.stdout
    assert "1 file(s) will be replaced and 0 deleted PERMANENTLY" in r.stderr
    assert env.remote("doc.txt").read_text() == "the local version\n"
    assert not (env.server / "sync" / ws.BACKUP_DIR).exists()


def test_check_access_marker_is_created_only_after_confirmation(env):
    write_profile(env, "ca", check_access=True)
    env.put(env.local, "a.txt", "a\n")
    assert env.run("ca", "-n").returncode == 0
    assert env.run("ca", answers="n\n").returncode == ws.EXIT_ABORTED
    assert not (env.local / "RCLONE_TEST").exists()
    r = env.run("ca", answers="y\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (env.local / "RCLONE_TEST").exists() and env.remote("RCLONE_TEST").exists()
    env.put(env.local, "b.txt", "b\n")
    r = env.run("ca", "-y")                            # normal run with --check-access
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.remote("b.txt").exists()


def test_rclone_flags_after_double_dash_reach_every_rclone_call(env):
    conf = Path(env.env.pop("RCLONE_CONFIG"))
    env.env["HOME"] = str(env.tmp)                     # no default config to fall back to
    env.put(env.local, "a.txt", "a\n")
    r = env.sync("--resync", "-y", "--", "--config", str(conf))
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.remote("a.txt").exists()


def test_plain_http_remote_is_warned_about(env):
    conf = Path(env.env["RCLONE_CONFIG"])
    conf.write_text(conf.read_text() + "[plain]\ntype = webdav\n"
                    "url = http://nonexistent.invalid\nvendor = other\n")
    r = env.run(str(env.local), "plain:x", "-n", "--", "--contimeout", "1s",
                "--low-level-retries", "1", "--retries", "1")
    assert "plain http://" in r.stderr
    assert "plain http://" not in env.sync("-n").stderr   # 127.0.0.1 is fine


def test_changes_between_preview_and_sync_are_reported(env):
    env.baseline()
    env.put(env.local, "planned.txt", "p\n")
    p = subprocess.Popen([sys.executable, str(SCRIPT), str(env.local), "cloud:sync"],
                         env=env.env, text=True, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    seen = ""
    while not seen.endswith("[y/N] "):
        ch = p.stdout.read(1)
        assert ch, seen
        seen += ch
    env.put(env.local, "late.txt", "l\n")              # appears after the preview
    out, err = p.communicate("y\n", timeout=60)
    assert p.returncode == 0, seen + out + err
    assert "files changed between the preview and the sync" in err
    assert "uploaded: late.txt" in err and env.remote("late.txt").exists()


def test_ctrl_c_during_resync_says_it_will_repeat(env):
    import signal
    for name in ("big1.bin", "big2.bin"):
        (env.local / name).write_bytes(os.urandom(6_000_000))
    p = subprocess.Popen([sys.executable, str(SCRIPT), str(env.local), "cloud:sync",
                          "--resync", "-y", "--", "--bwlimit", "1M", "--transfers", "1"],
                         env=env.env, text=True, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, start_new_session=True)
    time.sleep(3)
    os.killpg(p.pid, signal.SIGINT)
    out = p.communicate(timeout=60)[0]
    assert p.returncode == 130, out
    assert "the resync did not finish, so the next run repeats it" in out
    r = env.sync("-y")
    assert r.returncode == 1 and "resync is needed" in r.stderr
    r = env.sync(answers="y\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.remote("big2.bin").read_bytes() == (env.local / "big2.bin").read_bytes()


def test_first_run_with_both_sides_empty_creates_no_dead_baseline(env):
    r = env.sync()                                       # interactive: no question asked
    assert r.returncode == 0 and "Nothing to sync yet" in r.stdout and "[y/N]" not in r.stdout
    r = env.sync("--resync", "-y")                       # unattended: same outcome
    assert r.returncode == 0 and "Nothing to sync yet" in r.stdout
    env.put(env.local, "first.txt", "1\n")
    r = env.sync()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RESYNC" in r.stdout and env.remote("first.txt").exists()
    env.put(env.local, "second.txt", "2\n")
    r = env.sync("-y")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.remote("second.txt").exists()


def test_empty_baseline_left_by_older_versions_is_cleared(env):
    env.baseline()
    st_root = next((env.tmp / "state" / ws.APP).glob("adhoc-*"))
    for lst in (st_root / "bisync").glob("*.path1.lst"):  # what an empty resync leaves
        lst.write_text("# bisync listing v1 from 2026-01-01\n")
    assert (st_root / "baseline.json").exists()
    r = env.sync("-y")
    assert r.returncode == 1 and "fresh first sync" in r.stderr
    assert not (st_root / "baseline.json").exists()
    r = env.sync()
    assert r.returncode == 0 and "RESYNC" in r.stdout, r.stdout + r.stderr


def test_resync_clears_stale_listings_of_an_old_path_pair(env):
    env.baseline()
    workdir = next((env.tmp / "state" / ws.APP).glob("adhoc-*")) / "bisync"
    stale = workdir / "old..pair.path1.lst"
    stale.write_text('-        1 - - 2026-01-01T00:00:00Z "old.txt"\n')
    r = env.sync("--resync", "-y")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not stale.exists()                     # leftovers of another pair are gone
    assert list(workdir.glob("*.path1.lst"))      # the current pair's listing remains


def test_special_file_names_sync_and_show_correctly(env):
    env.baseline()
    names = ["with space.txt", 'quo"te.txt', "hash#1.txt", "pct%20.txt", "ümläut ß.txt",
             " lead.txt", "dash - x.txt", "back\\slash.txt", "new\nline.txt"]
    for n in names:
        env.put(env.local, n, n + "\n")
    r = env.sync()
    assert r.returncode == 0, r.stdout + r.stderr
    for n in ('quo"te.txt', "back\\slash.txt", "ümläut ß.txt", " lead.txt"):
        assert f"      {n}   [new, " in r.stdout, n
    assert "cloud:sync/" not in r.stdout.split("Apply")[0]      # no raw quoted paths
    for n in names:
        assert env.remote(n).read_text() == n + "\n", n
    assert "Everything is in sync." in env.sync().stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read everything")
def test_unreadable_file_is_named_and_not_retried(env):
    env.baseline()
    env.put(env.local, "ok.txt", "ok\n")
    env.put(env.local, "secret.txt", "s\n")
    (env.local / "secret.txt").chmod(0)
    try:
        r = env.sync("-y")
        assert r.returncode == 1
        assert "These files could not be transferred" in r.stderr
        assert r.stderr.count("secret.txt  (permission denied)") == 1
        assert "fresh first sync (resync)" in r.stderr
        assert not (env.server / "sync" / ws.BACKUP_DIR).exists()   # no re-upload by retries
        assert "resync is needed" in env.sync("-y").stderr          # cron stops ...
    finally:
        (env.local / "secret.txt").chmod(0o644)
    r = env.sync(answers="y\n")                                     # ... until fixed + resync
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.remote("secret.txt").exists() and env.remote("ok.txt").exists()


def test_file_folder_name_clash_gets_a_hint(env):
    for i in range(5):
        env.put(env.local, f"keep{i}.txt", "k\n")
    env.put(env.local, "x", "file\n")
    env.put(env.server, "sync/x/inner.txt", "dir\n")
    r = env.sync(answers="y\n")
    assert r.returncode == 1 and "same name as a folder" in r.stderr
    assert "first sync (resync) did not complete" in r.stderr
    (env.local / "x").rename(env.local / "x.txt")
    r = env.sync(answers="y\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (env.local / "x" / "inner.txt").exists() and env.remote("x.txt").exists()


def test_whole_drive_as_remote(env):
    for i in range(5):
        env.put(env.local, f"k{i}.txt", f"{i}\n")
    assert env.run(str(env.local), "cloud:", "--resync", "-y").returncode == 0
    time.sleep(1.1)
    env.put(env.local, "k0.txt", "changed!\n")
    r = env.run(str(env.local), "cloud:", "-y")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (env.server / ws.BACKUP_DIR).is_dir() and (env.server / "k0.txt").read_text() == "changed!\n"


def test_trailing_slash_uses_the_same_baseline(env):
    env.baseline()
    r = env.run(str(env.local), "cloud:sync/", "-n")
    assert r.returncode == 0 and "RESYNC" not in r.stdout and "no changes" in r.stdout


def test_baseline_survives_rclone_upgrade(env):
    old, new = (str(Path(SERVER).with_name(n)) for n in ("rclone-v1.66.0", "rclone-v1.76.0"))
    if not (Path(old).is_file() and Path(new).is_file()):
        pytest.skip("needs rclone v1.66.0 and v1.76.0 in build/rclone")
    env.env["FILESYNC_RCLONE"] = old
    env.baseline()
    env.env["FILESYNC_RCLONE"] = new
    env.put(env.local, "after-upgrade.txt", "u\n")
    r = env.sync("-y")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.remote("after-upgrade.txt").exists()


def test_local_empty_first_run_downloads_everything(env):
    env.put(env.server, "sync/a.txt", "a\n")
    env.put(env.server, "sync/d/b.txt", "b\n")
    r = env.sync()
    assert r.returncode == 0 and "download to local:" in r.stdout, r.stdout + r.stderr
    assert (env.local / "d" / "b.txt").read_text() == "b\n"


def filter_group_flags(rclone: str) -> dict[str, bool]:
    """The flags in the "Filter" group of `rclone help flags`: name -> takes a value."""
    out = subprocess.run([rclone, "--config", os.devnull, "help", "flags"], check=True,
                         capture_output=True, text=True).stdout
    flags: dict[str, bool] = {}
    for line in out.split("Flags for filtering directory listings", 1)[1].splitlines()[1:]:
        m = re.match(r"\s+(?:(-\w), )?(--[\w-]+)( \S+)?  ", line)
        if m:
            flags.update({name: bool(m[3]) for name in m.group(1, 2) if name})
        elif flags:
            break                                  # end of the group
    return flags


@pytest.mark.parametrize("rclone", RCLONES or [None],
                         ids=lambda p: Path(p).name if p else "no-rclone")
def test_filter_flag_sets_cover_rclones_filter_group(rclone):
    if rclone is None:
        pytest.skip("set FILESYNC_TEST_RCLONE (or use `make test`)")
    flags = filter_group_flags(rclone)
    assert len(flags) > 15 and flags["--filter"] and flags["-f"] and not flags["--ignore-case"]
    with_value = {name for name, takes_value in flags.items() if takes_value}
    assert with_value <= ws.FILTER_VALUE_FLAGS, with_value - ws.FILTER_VALUE_FLAGS
    assert set(flags) - with_value <= ws.FILTER_BOOL_FLAGS
    assert not with_value & ws.FILTER_BOOL_FLAGS


def test_wrapper_owned_flags_cannot_be_passed_through(env):
    write_profile(env, "forced", rclone_flags=["--tpslimit", "50", "--force"])
    env.baseline(6)
    for p in sorted(env.local.glob("keep*.txt"))[1:]:
        p.unlink()                                     # 5 of 6 files gone
    for r in (env.sync("-y", "--", "--force"), env.run("forced", "-y"),
              env.sync("-y", "--", "--resync-mode", "path2"), env.sync("-y", "--", "-1")):
        assert r.returncode == ws.EXIT_USAGE and "is not accepted" in r.stderr, r.stderr
    assert len(list(env.remote("").glob("keep*.txt"))) == 6
    assert env.sync("-y").returncode == 1              # the safety abort still stands
    for flags in (["--dry-run"], ["-vn"], ["-vn=true"], ["-v1=true"]):
        r = env.sync("--", *flags)
        assert r.returncode == ws.EXIT_USAGE and "is not accepted" in r.stderr, r.stderr
        assert "Done" not in r.stdout


def test_rclone_env_vars_cannot_bypass_safety_guards(env):
    env.baseline(6)
    for p in sorted(env.local.glob("keep*.txt"))[1:]:
        p.unlink()                                     # 5 of 6 files gone
    old_env = dict(env.env)
    try:
        env.env["RCLONE_FORCE"] = "true"
        r = env.sync("-y")
        assert r.returncode == 1 and "--force" in r.stderr
        assert len(list(env.remote("").glob("keep*.txt"))) == 6
    finally:
        env.env = old_env
    # Restore files and verify RCLONE_DRY_RUN cannot turn a real sync into a no-op
    for i in range(1, 6):
        env.put(env.local, f"keep{i}.txt", f"keep {i}\n")
    env.put(env.local, "real.txt", "real\n")
    try:
        env.env["RCLONE_DRY_RUN"] = "true"
        r = env.sync("-y")
        assert r.returncode == 0, r.stdout + r.stderr
        assert env.remote("real.txt").read_text() == "real\n"
    finally:
        env.env = old_env


def test_crypt_over_webdav_syncs_same_size_edits_and_verifies_content(env):
    pw = subprocess.run([env.rclone, "obscure", "test-pass"],
                        capture_output=True, text=True, check=True).stdout.strip()
    (env.server / "enc").mkdir()
    conf = Path(env.env["RCLONE_CONFIG"])
    conf.write_text(conf.read_text() +
                    f"[cloud-crypt]\ntype = crypt\nremote = cloud:enc\npassword = {pw}\n")
    for i in range(5):
        env.put(env.local, f"keep{i}.txt", f"keep {i}\n")
    env.put(env.local, "same.txt", "AAAA\n")
    r = env.run(str(env.local), "cloud-crypt:", "--resync", "-y")
    assert r.returncode == 0, r.stdout + r.stderr
    time.sleep(1.1)
    # Same-size local edit must reach encrypted WebDAV (auto ignore_times through crypt)
    env.put(env.local, "same.txt", "BBBB\n")
    r = env.run(str(env.local), "cloud-crypt:", "-y")
    assert r.returncode == 0, r.stdout + r.stderr
    # --verify must auto-download and confirm both sides match
    r = env.run(str(env.local), "cloud-crypt:", "--verify", "-y")
    assert r.returncode == 0 and "Both sides are identical." in r.stdout, r.stdout + r.stderr
    # Same-size divergence (e.g. local mutated without syncing) must be caught by --verify
    env.put(env.local, "same.txt", "CCCC\n")
    r = env.run(str(env.local), "cloud-crypt:", "--verify", "-y")
    assert r.returncode == 1 and "content differs:" in r.stdout and "same.txt" in r.stdout
    # Explicitly forcing verify_download = false on a hashless crypt remote is refused
    cfg = env.tmp / "cfg" / ws.APP
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "profiles.toml").write_text(
        f'[cc]\nlocal = "{env.local}"\nremote = "cloud-crypt:"\nverify_download = false\n')
    r = env.run("cc", "--verify", "-y")
    assert r.returncode == 1 and "offers no checksums" in r.stderr


def test_orphan_jsonl_logs_are_cleaned_up(env):
    env.baseline()
    logs = next((env.tmp / "state" / ws.APP).glob("adhoc-*")) / "logs"
    orphan = logs / "2020-01-01_000000.jsonl"
    orphan.write_text('{"level":"info","msg":"stale"}\n')
    r = env.sync("-y")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not orphan.exists()


def test_file_names_that_look_like_rclone_messages_sync_normally(env):
    env.baseline()
    names = ["Failed to bisync.txt", "Must run --resync.txt", "Bisync successful.txt",
             "WARNING.txt", "Safety abort", "- Path2    Queue delete    - victim.txt"]
    for n in names:
        env.put(env.server, "sync/" + n, "x\n")
    (env.local / '"a","b"').symlink_to("/nonexistent")  # used to crash the log parser
    r = env.sync()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Apply these 6 change(s)" in r.stdout and "delete on server" not in r.stdout
    assert "skipped symlinks:          1" in r.stdout
    assert r.stderr == ""
    for n in names:
        assert (env.local / n).read_text() == "x\n", n
    for n in names:
        env.put(env.local, n, "changed\n")             # real run without a preview
    r = env.sync("-y")
    assert r.returncode == 0 and r.stderr == "", r.stdout + r.stderr
    assert "deleted" not in r.stdout.split("Backups of")[0]
    assert env.remote("Failed to bisync.txt").read_text() == "changed\n"
    assert "Everything is in sync." in env.sync().stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write everywhere")
def test_unwritable_state_dir_is_an_error_not_a_traceback(env):
    ro = env.tmp / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    env.env["XDG_STATE_HOME"] = str(ro)
    try:
        r = env.sync("-n")
    finally:
        ro.chmod(0o700)
    assert r.returncode == ws.EXIT_FAIL and r.stderr.startswith("error: ")
    assert "Traceback" not in r.stderr and "Permission denied" in r.stderr
