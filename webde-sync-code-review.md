# Code Review and Evaluation of `webde_sync.py`

This document reviews [`webde_sync.py`](webde_sync.py) and evaluates whether it provides a **secure**, **complete**, and **easy-to-use** `rclone` wrapper for bidirectional file synchronization between a Linux PC and a web.de cloud drive (Online-Speicher via WebDAV).

## Verdict

**Not ready for real use.** In its current form, [`webde_sync.py`](webde_sync.py) will crash immediately when run against `rclone`, silently loses data on concurrent two-way edits, conflicts fundamentally with how web.de WebDAV handles modification timestamps, and lacks essential data-safety guardrails.

| Criterion | Assessment | Summary |
| --- | --- | --- |
| **Security & Safety** | Insufficient | Path traversal and potential infinite loop in local deletion; unsafe remote `rmdirs`; no concurrency lock, backup/trash, or mass-deletion guardrail; state stored in `~/.cache` with default permissions. |
| **Completeness** | Incomplete | Fails on real `rclone lsjson` output; breaks on web.de WebDAV `mtime` semantics; misses true three-way conflict detection; no WebDAV tuning, symlinks, empty-directory handling, or saved profiles. |
| **Ease of Use** | Mixed | Clear overview concept, but zero transfer progress output, broken interactive deletion prompts, swapped preview sizes, confusing `0 file(s)` prompts, and missing setup documentation. |

---

## 1. Critical Bugs (Broken Functionality & Data Loss)

### 1.1 Crash on `rclone lsjson` JSON keys

In `listing()` ([`webde_sync.py`](webde_sync.py), lines 95–103), the parser expects camelCase or lowercase keys (`isDir`/`isdir`, `path`, `size`, `modTime`/`modtime`):

```python
for e in entries:
    if e.get("isDir") or e.get("isdir"):
        continue
    rel = e["path"]
    ...
    files[rel] = (int(e.get("size") or 0), parse_modtime(e.get("modTime") or e.get("modtime")))
```

`rclone lsjson` outputs **PascalCase** keys (`Path`, `Size`, `ModTime`, `IsDir`). On any non-empty directory, `e["path"]` raises `KeyError: 'path'` immediately, and `IsDir`, `Size`, and `ModTime` are never matched.

### 1.2 Inverted interactive deletion prompt

In `main()` ([`webde_sync.py`](webde_sync.py), lines 383–392):

```python
if n_deletes:
    if args.prune or (args.yes and confirm(
            f"Propagate {n_deletes} deletion(s) ({len(plan.delete_remote)} on server,"
            f" {len(plan.delete_local)} locally)?")):
```

- **Default interactive mode** (`-y` and `--prune` both unset): `args.yes` is `False`, so `confirm(...)` is never called and deletions are always silently skipped.
- **Non-interactive transfers** (`-y` set without `--prune`): contrary to the `--help` text (*"apply transfers without asking (deletions still need `--prune`)"*), the script blocks on an interactive `confirm(...)` prompt.

### 1.3 Swapped sides in the deletion preview

In `main()` ([`webde_sync.py`](webde_sync.py), lines 343–344):

```python
("delete on server", plan.delete_remote, loc),
("delete locally", plan.delete_local, rem),
```

`plan.delete_remote` lists files present only on `rem` (already missing from `loc`), while `plan.delete_local` lists files present only in `loc` (already missing from `rem`). Because `loc` and `rem` are swapped, `show()` never finds any of these entries and reports every deletion as `[0 B, ?]` with a `0 B` total.

### 1.4 True two-way conflicts are not detected (silent data loss)

In `plan_sync()` ([`webde_sync.py`](webde_sync.py), lines 167–184), the `if in_l and in_r:` branch completely ignores the baseline `state` file:

- If a file is edited on Linux **and** also edited on web.de (or another device) since the last sync, the script silently overwrites the older edit with the newer one instead of flagging a conflict.
- If a file is intentionally rolled back locally to an older `mtime` (e.g. via backup restore or version control), the unmodified remote copy silently overwrites the local file.
- Only files with differing sizes and matching `mtime` values (within `--mtime-tolerance`, default `2.0` seconds) are currently detected as conflicts.

### 1.5 Invalid `--files-from` + `--exclude` combination and filename mangling

In `files_from_tmp()`, `rclone_copy()`, and `rclone_delete()` ([`webde_sync.py`](webde_sync.py), lines 237–262):

- `rclone_copy()` passes both `--files-from` and `--exclude`. Combining `--files-from` with filter flags such as `--exclude` is unsupported in `rclone` (resulting in errors in modern versions) and is redundant because `listing()` already applied `--exclude`.
- `--files-from` treats lines starting with `#` or `;` as comments and strips surrounding whitespace; **`--files-from-raw`** should be used for arbitrary filenames.
- Filenames containing a literal newline (`\n`, valid on Linux filesystems) split into multiple lines in `files_from_tmp()`, creating a path-injection risk for `rclone delete`.

### 1.6 Fundamental mismatch with web.de WebDAV `mtime` behavior

web.de Online-Speicher via WebDAV (`https://webdav.web.de`) does not reliably preserve client modification timestamps on upload (it sets `getlastmodified` to the upload time) and exposes no checksums:

- After uploading a local file with an older `mtime`, the remote `ModTime` becomes the upload time (`tr > tl`).
- On the next run, `plan_sync()` classifies every recently uploaded file as `download_changed` and downloads them back over the local files, clobbering local modification timestamps.
- `save_state()` ([`webde_sync.py`](webde_sync.py), lines 131–146 and 397–404) stores only a single `(size, modtime)` tuple taken from `loc2`, so it cannot track asymmetric local vs. remote metadata (or remote ETags) across syncs.

---

## 2. Security and Data-Safety Issues

- **Path traversal and infinite loop in `delete_local()` ([`webde_sync.py`](webde_sync.py), lines 265–277):**
  `rel` comes from remote `rclone lsjson` output and is joined directly via `f = local / rel`. An absolute path (e.g. `/etc/passwd`) discards `local` entirely in `pathlib`, and `..` segments escape `local`. If `f` resolves outside `local`, `while d != local and d.is_dir():` climbs to `/` and loops forever (`Path("/").parent == Path("/")`). Paths must be validated to ensure no `..` traversal and that `(local / rel).resolve().is_relative_to(local)`.
- **Overly broad `rclone rmdirs` ([`webde_sync.py`](webde_sync.py), line 260):**
  `rclone(["rmdirs", remote])` omits `--leave-root` (allowing the remote sync root itself to be deleted if emptied) and removes *all* empty directories on the remote, including intentionally empty folders and folders whose files were excluded via `--exclude`.
- **No mass-deletion safeguard:**
  If a local directory is an unmounted external drive or the remote path is temporarily empty, the script will schedule deleting 100% of the other side's files with no `--max-delete` threshold.
- **No backup or trash safety net:**
  Overwrites and local `f.unlink()` deletions are permanent. Supporting `--backup-dir` (or moving deleted/overwritten files to a backup or trash folder) would prevent irreversible data loss.
- **No concurrency locking:**
  Two simultaneous runs can race on transfers and deletions and overwrite the same fixed temporary state path (`path.with_suffix(path.suffix + ".tmp")` on line 144).
- **State file location and permissions ([`webde_sync.py`](webde_sync.py), lines 109–111 and 131–146):**
  - The default path is `~/.cache/websync/<key>.json`. Per the XDG Base Directory Specification, `~/.cache` is for disposable cache data; clearing it erases deletion history and resurrects previously deleted files. Persistent sync state belongs in `$XDG_STATE_HOME` (`~/.local/state/websync/`).
  - The state file is written with the process's default umask (typically `0644`), exposing the entire cloud file inventory to other local users on multi-user systems; `0600` (with directory mode `0700`) is safer.
- **Encrypted `rclone.conf` incompatibility ([`webde_sync.py`](webde_sync.py), line 65):**
  `rclone()` runs `subprocess.run(cmd, capture_output=True, text=True)`, which can hide interactive password prompts if the user encrypts `rclone.conf` to protect their web.de credentials.
- **No remote or transport validation:**
  The script does not verify that `remote` is a configured `rclone` remote or uses `https://`. A typo omitting `:` (e.g. `webde/Backup` instead of `webde:/Backup`) is silently treated by `rclone` as a local path.

---

## 3. Completeness for Linux ↔ web.de WebDAV

- **Excessive remote listings ([`webde_sync.py`](webde_sync.py), lines 332–333 and 395–396):**
  Every active sync calls `listing()` four times (two full recursive scans locally and two remotely), which is slow and increases rate-limiting risk on WebDAV.
- **No WebDAV tuning or passthrough options:**
  web.de WebDAV frequently throttles or drops connections under `rclone`'s default concurrency. There is no way to configure `--transfers`, `--tpslimit`, `--retries`, `--timeout`, or extra `rclone` flags.
- **No saved profiles or `--exclude-from`:**
  Every invocation requires retyping `LOCAL_DIR`, `REMOTE`, and all `--exclude` patterns. Omitting one `--exclude` flag on a single run can accidentally upload `.git/`, `.venv/`, or temporary files.
- **Unhandled filesystem features:**
  Local symlinks are silently ignored by `rclone lsjson` without warning, and empty directories are neither synced nor preserved cleanly.
- **Missing documentation and version check:**
  The docstring ([`webde_sync.py`](webde_sync.py), line 33) states `Requires: rclone >= 1.52` and points to a setup README, yet no version check is implemented and no README exists in the repository.

---

## 4. Usability and Code Quality

- **Zero transfer progress output ([`webde_sync.py`](webde_sync.py), line 65):**
  Because `capture_output=True` buffers all output until `rclone` exits, multi-gigabyte uploads or downloads over WebDAV appear completely frozen until completion.
- **Confusing prompts when `n_transfers == 0` ([`webde_sync.py`](webde_sync.py), lines 371–374):**
  When there are zero transfers but pending deletions or conflicts, the script still prompts `Transfer 0 file(s)? [y/N]`.
- **Negative `--max-show` accepted ([`webde_sync.py`](webde_sync.py), lines 218–219 and 301–302):**
  Passing `--max-show -1` slices `paths[:-1]`, displaying all entries except the last instead of rejecting invalid input.
- **Unnormalized remote key for state path ([`webde_sync.py`](webde_sync.py), line 110):**
  `webde:`, `webde:/`, and `webde://` hash to different state files even though they point to the same remote directory.
- **Dead and obfuscated code:**
  - Unfinished dead code in `main()` ([`webde_sync.py`](webde_sync.py), lines 326–327): `+ (f" (last sync {getattr(state, '')})" if False else "")`.
  - Obfuscated walrus expression in `plan_sync()` ([`webde_sync.py`](webde_sync.py), lines 172–176): `(p.unchanged.__class__ and (p.unchanged := p.unchanged + 1) if ls == rs else p.conflicts.append(path))`.
  - Unhandled `KeyboardInterrupt` during `rclone` execution prints a raw Python traceback instead of exiting cleanly.

---

## 5. Recommendations

### Option A: Wrap `rclone bisync` (Recommended)

Modern `rclone` (especially `>= 1.66`) includes built-in **`rclone bisync`**, which already implements three-way state tracking, conflict resolution strategies, `--max-delete` safety checks, concurrency locking, `--backup-dir`, progress reporting, and WebDAV options (`--compare size`, `--resync`).

Replacing the custom diff engine with a thin Python wrapper that:
1. loads a named sync profile (paths, excludes, WebDAV throttling defaults) from a config file,
2. runs `rclone bisync --dry-run` and renders a concise preview,
3. prompts for confirmation (with extra confirmation when deletions exceed a threshold), and
4. executes `rclone bisync` with live progress output

will be significantly smaller, safer, and more reliable for web.de WebDAV.

### Option B: Fix the Standalone Implementation

If you prefer to keep a standalone diff/plan script, apply at least these fixes before syncing real data:

1. Parse `rclone lsjson`'s PascalCase keys (`Path`, `Size`, `ModTime`, `IsDir`).
2. Store **both** local and remote metadata (plus remote `ETag` if returned) in the state file so `plan_sync()` can perform true three-way diffing (detecting two-way conflicts and handling WebDAV's non-preserved `ModTime`).
3. Fix the swapped `loc`/`rem` arguments in `show()`, the inverted deletion confirmation logic, and the `Transfer 0 file(s)?` prompt.
4. Switch from `--files-from` + `--exclude` to `--files-from-raw` (or stdin `-`), reject embedded newlines, and enforce strict path containment in `delete_local()`.
5. Pass `--leave-root` (or clean up only targeted directories) instead of bare `rclone rmdirs`, add a `--max-delete` limit, add a lockfile, move state to `~/.local/state/websync/` with `0600` permissions (normalizing the remote path), and stream live progress during transfers.
