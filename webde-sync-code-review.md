# Code Review and Evaluation of `webde_sync.py`

This document reviews the `rclone bisync` rewrite of [`webde_sync.py`](webde_sync.py) (commit `05fb4e3`, along with [`README.md`](README.md), [`Makefile`](Makefile), and [`tests/test_webde_sync.py`](tests/test_webde_sync.py)) and evaluates whether it provides a **secure**, **complete**, and **easy-to-use** wrapper for bidirectional file synchronization between a Linux PC and a web.de cloud drive (Online-Speicher via WebDAV).

> **Note:** The earlier review of the superseded, self-written sync engine (commit `6658238`) is preserved in git history (commit `7ee89f4`).

## Verdict

**Architecturally solid and very close to production-ready, with one major preview bug to fix before daily use.**

Delegating synchronization to `rclone bisync` (`>= 1.66`) eliminates the data-loss and WebDAV-timestamp flaws of the initial version. `make lint` passes cleanly and all **49 tests** in [`tests/test_webde_sync.py`](tests/test_webde_sync.py) pass against `rclone v1.66.0` and `v1.76.0` using a local `rclone serve webdav` (`vendor = other`) server.

| Criterion | Assessment | Summary |
| --- | --- | --- |
| **Security & Safety** | **Strong** | Three-way sync via `bisync`, per-run backups on both sides with retention pruning, keep-both conflicts + recurring reminders, `--max-delete` & empty-side aborts, `flock` inherited by `rclone`, `0700` state dirs in `$XDG_STATE_HOME`, encrypted `rclone.conf` support, vanished-remote check, `--force` blocked with `-y`. A few smaller edge cases remain (PID reuse in stale-lock cleanup, `RCLONE_TEST` created during `--dry-run`). |
| **Completeness** | **Very Good** | TOML profiles, `--ignore-times` for WebDAV same-size edits, `--verify` byte check, `--resilient --recover` Ctrl-C resume, readable per-run logs, version & remote checks, clear [`README.md`](README.md). One major gap: `rclone` dry-run size parsing breaks on every file `>= 1 KiB`. |
| **Ease of Use** | **Good** | Clean CLI, helpful error advice, live `--progress` on TTY, English/German `[y/N]` prompt (`y`/`yes`/`j`/`ja`), clear conflict & backup summaries. Slightly hurt by the `>= 1 KiB` preview bug and silent `--verify` downloads. |

---

## 1. Real Bugs Found (Fix Before Daily Use)

### 1.1 Major (verified live): `RE_SKIP_COPY` fails on every file `>= 1 KiB`

In [`webde_sync.py`](webde_sync.py) (lines 471 and 561–564):

```python
RE_SKIP_COPY = re.compile(r"^(.+): Skipped copy as --dry-run is set \(size (\d+)\)$")
```

```python
elif m := RE_SKIP_COPY.match(text):
    rep.sizes[m[1]] = int(m[2])
    if resync_target:
        copies.append((resync_target, m[1]))
```

For files of `1024` bytes or larger, `rclone` formats the log message with human units — e.g. `Skipped copy as --dry-run is set (size 48.828Ki)` (`Ki`, `Mi`, `Gi`, ...) — rather than raw digits.

- **Normal preview impact:** File sizes and total transfer sizes omit every file `>= 1 KiB`.
- **First-run / `--resync` preview impact:** During `--resync --dry-run`, files are *only* added to the preview list inside `RE_SKIP_COPY`. Testing a `50 KB` file with `rclone v1.76.0 --resync-mode path1 --dry-run` yields `upload={}, download={}, n_actions=0, sizes={}`. On a first run over real documents or photos (all `>= 1 KiB`), the overview prints `no changes` and prompts `Create the baseline (no files need to be copied)? [y/N]` — and then uploads every file once confirmed.
- **Why tests missed it:** Every preview test in [`tests/test_webde_sync.py`](tests/test_webde_sync.py) used tiny files (`< 1 KiB`); only `test_ctrl_c_stops_cleanly_and_next_run_completes` created multi-MB files and ran with `-y` (skipping the preview).
- **Recommended fix:** `rclone --use-json-log` already emits structured `"size": 50000` (exact integer bytes) and `"skipped": "copy"` fields in the JSON payload. Preserve those fields in `log_messages()` instead of regex-parsing formatted units out of `msg`, and add a preview test with files `>= 1 KiB`.

### 1.2 Moderate: `check_access = true` mutates the local folder during `--dry-run` and before confirmation

In `sync()` ([`webde_sync.py`](webde_sync.py), lines 968–970):

```python
if resync and profile.check_access and not (profile.local / "RCLONE_TEST").exists():
    (profile.local / "RCLONE_TEST").write_text(
        "Marker file for webde_sync.py check_access -- do not delete.\n", encoding="utf-8")
```

- Runs *before* the `args.dry_run` return (line 985) and *before* user confirmation (line 1015), so `--resync --dry-run` or declining the prompt still creates `RCLONE_TEST` on disk despite reporting `nothing was changed`.
- Can plant a marker file into an unmounted/empty mountpoint if `--resync` is invoked on it, or create a competing `RCLONE_TEST` when onboarding a second PC to a remote that already has one.
- **Recommended fix:** Defer creating `RCLONE_TEST` until after confirmation (and skip if the remote already contains `RCLONE_TEST`).

### 1.3 Moderate: `clear_stale_bisync_locks()` can wedge permanently on PID reuse

In `clear_stale_bisync_locks()` ([`webde_sync.py`](webde_sync.py), lines 361–380):

- After a hard crash or reboot, if the old PID recorded in `*.lck` has been reassigned to any unrelated running process (e.g. a system daemon causing `PermissionError`, or any running user process where `os.kill(pid, 0)` succeeds), `webde_sync.py` refuses to run until the user deletes the lock file manually — even though `webde_sync.py` already holds the exclusive `ProfileLock` (which is inherited by child `rclone` processes).
- **Recommended fix:** On Linux, check `/proc/<pid>/comm` (or `/proc/<pid>/cmdline`) to verify the running PID is actually `rclone` before treating the lock as live.

---

## 2. Minor Bugs and Edge Cases

- **No overwrite warning during `--resync` ([`webde_sync.py`](webde_sync.py), lines 1005–1014):**
  `replaced` counts only entries where `why.startswith("changed")`, whereas `--resync` sets `reason = "resync"`. Differing files overwritten during `--resync` — even when `backups = false` — never trigger the `PERMANENTLY` warning.
- **Resolved conflicts are double-counted in the preview ([`webde_sync.py`](webde_sync.py), lines 592–603):**
  With `conflict_resolve = "local"` (or `"remote"`), the winning copy is listed under *both* `upload to server` (or `download to local`) and `conflict`, inflating `rep.n_actions` (e.g. `Apply these 2 change(s)?` for a single file) and counting the conflict as a replacement.
- **`verify()` usability and flag issues ([`webde_sync.py`](webde_sync.py), lines 791–831):**
  - Uses `rc.run(...)` (`capture_output=True`), giving **zero progress output** while downloading an entire cloud folder byte-by-byte.
  - Passes `*profile.rclone_flags` directly to `rclone check`; any bisync-only flag in `rclone_flags` (e.g. `--compare`, `--track-renames`, `--conflict-loser`) causes `--verify` to fail with `unknown flag`.
  - Truncates file lists at `max_show` without printing `... and N more (use --list-all)` (lines 813–815).
  - Suggested fix commands (lines 822–824) wrap paths in raw single quotes instead of `shlex.quote(...)`, breaking on filenames containing `'`.
- **Helper `rclone` calls ignore `rclone_flags` and `extra` ([`webde_sync.py`](webde_sync.py), lines 249, 286, 293, 655, 682, 778, 783):**
  `config_is_encrypted`, `check_remote_configured`, `remote_dir_exists`, `pending_conflicts`, `show_backups`, and `prune_backups` do not receive `rclone_flags` or `extra`, so options like `-- --config /alt/rclone.conf` or WebDAV `--tpslimit`/`--timeout` are not applied uniformly.
- **Misleading Ctrl-C message during `--resync` ([`webde_sync.py`](webde_sync.py), lines 1024–1029):**
  If a `--resync` is interrupted before `"Bisync successful"` is logged, `baseline.json` is not written, so the next run *will* still require `--resync` despite printing `(no --resync needed)`.

---

## 3. Security and web.de-Specific Notes

### What is done well

- No `shell=True` anywhere; all subprocesses pass argument lists directly.
- State, bisync listings, and logs live in `$XDG_STATE_HOME/webde-sync/` (`~/.local/state/webde-sync/`) with directory mode `0700`.
- Exclusive per-profile `flock` is inherited by child `rclone` processes via `pass_fds`.
- Encrypted `rclone.conf` is detected automatically, prompts once via `getpass.getpass()`, and sets `RCLONE_ASK_PASSWORD=false` so `rclone` never hangs invisibly.
- Refuses `/` as the local directory, rejects `--force --yes`, refuses silent `-y` first runs or filter changes, and treats a missing remote folder as an error rather than a mass delete.

### Remaining considerations

- **On-the-fly remotes (`:webdav,...:`):** Allowed by `check_remote_configured()` ([`webde_sync.py`](webde_sync.py), line 281); any inline credentials in such a remote string would be printed to stdout and written to `baseline.json` and logs.
- **No HTTPS check:** The script verifies that the remote exists, not that a `webdav` remote uses `https://` rather than plaintext `http://`.
- **Environment password exposure:** `RCLONE_CONFIG_PASS` is placed in the child process environment (`/proc/<pid>/environ`, readable only by the same UID or root on Linux — standard for `rclone`; `RCLONE_PASSWORD_COMMAND` is documented in [`README.md`](README.md) for unattended use).
- **Live web.de validation & rate limits:** `rclone_flags = ["--tpslimit", "4", "--transfers", "2"]` is commented out in `EXAMPLE_CONFIG` ([`webde_sync.py`](webde_sync.py), line 78). Enabling conservative throttling defaults is worth considering for web.de WebDAV, and running one live smoke test (including a replacement/deletion backup `MOVE`) against `https://webdav.smartdrive.web.de` is advisable before syncing primary folders, since [`tests/test_webde_sync.py`](tests/test_webde_sync.py) tests against local `rclone serve webdav`.

---

## 4. Recommended Fix Checklist

1. **Fix `>= 1 KiB` dry-run parsing:** Read structured `size` and `skipped` fields from `rclone --use-json-log` in `log_messages()` / `parse_report()`, and add a preview + `--resync --dry-run` test with files `> 1 KiB`.
2. **Fix `check_access` dry-run side effect:** Create `RCLONE_TEST` only after confirmation (and only when absent on the remote as well).
3. **Harden `clear_stale_bisync_locks()`:** Check `/proc/<pid>/comm` (or `cmdline`) on Linux so PID reuse cannot wedge future runs.
4. **Polish `--verify` and preview edge cases:** Stream progress during `verify()`, add the `... and N more` footer, use `shlex.quote(...)` in advice commands, avoid double-counting resolved conflicts, and warn about `--resync` overwrites when `backups = false`.
