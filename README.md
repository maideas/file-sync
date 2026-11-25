# filesync.py

Two-way sync between a folder on a Linux PC and any [rclone](https://rclone.org)
remote -- web.de Online-Speicher, Nextcloud, Google Drive, S3, an SFTP server,
... -- with a preview of every change and a confirmation before anything
happens.

[`filesync.py`](filesync.py) is a thin wrapper around `rclone bisync`.
rclone does all the synchronisation work: change detection against the last
run, conflict handling, safety aborts, locking, backups and crash recovery.
The wrapper adds what bisync lacks for everyday use:

- named profiles, so paths and filter rules are never retyped,
- a readable overview (from `bisync --dry-run`) and a `[y/N]` confirmation,
- safe defaults: backups, keep-both conflicts, `--resilient --recover`,
- support for remotes that cannot store file modification times (web.de
  WebDAV): same-size edits still get transferred (see
  [Limitations](#limitations)),
- a guided first run, a content check (`--verify`), and logs.

## Requirements

| What | Version | Note |
| --- | --- | --- |
| Python | ≥ 3.11 | standard library only |
| rclone | ≥ 1.66 | Distribution packages are often older; check with `rclone version` |

If your rclone is too old, install the current release as a user (no root
needed), as described in <https://rclone.org/install/>: download the
`linux-amd64` zip and put the `rclone` binary into `~/.local/bin`. A
different binary can be selected with `--rclone PATH` or `$FILESYNC_RCLONE`.

## Setup

### 1. Add the rclone remote

Run `rclone config` and create a remote for your storage. Example for the
web.de Online-Speicher (requires an app password from the web.de security
settings, see the
[web.de help](https://hilfe.web.de/cloud/netzlaufwerk/index.html)):

| Prompt | Answer |
| --- | --- |
| name | `webde` |
| Storage | `webdav` |
| url | `https://webdav.smartdrive.web.de` |
| vendor | `other` |
| user | your web.de e-mail address |
| password | the app password |

Any other rclone remote works the same way -- a `nextcloud:` (webdav,
`vendor = nextcloud`), `gdrive:`, `s3:`, `sftp:` ... The examples below use a
remote named `cloud:`.

rclone stores passwords only *obscured* (scrambled, not encrypted) in
`~/.config/rclone/rclone.conf`. To encrypt the whole file, choose
`s) Set configuration password` in the `rclone config` main menu.
`filesync.py` then asks for that password once per run. For unattended runs,
set `RCLONE_PASSWORD_COMMAND` (e.g. `"pass show rclone"`).

Check the connection:

```bash
rclone lsd cloud:
```

### 2. Create a profile

```bash
mkdir -p ~/.config/file-sync
./filesync.py --example-config > ~/.config/file-sync/profiles.toml
```

Edit the file: one `[section]` per folder pair.

```toml
[docs]
local  = "~/Documents"
remote = "cloud:/Documents"
filters = ["- .git/**", "- *.tmp"]
```

| Key | Default | Meaning |
| --- | --- | --- |
| `local`, `remote` | required | local folder and rclone path (`NAME:path`) |
| `filters` | `[]` | [rclone filter rules](https://rclone.org/filtering/), first match wins |
| `filters_file` | none | file with further filter rules, appended to `filters` |
| `max_delete` | `50` | bisync aborts if more than this % of files would be deleted |
| `backups` | `true` | keep replaced and deleted files in `.file-sync-backup/<time>/` |
| `keep_backups_days` | `30` | prune older backup folders after a successful run; `0` keeps all |
| `check_access` | `false` | require an `RCLONE_TEST` file on both sides (guards mount points); the first confirmed resync creates it if neither side has one |
| `sync_empty_dirs` | `false` | also create and delete empty directories |
| `conflict_resolve` | `none` | `none` keeps both copies; or `local`, `remote`, `larger`, `smaller`, ... |
| `resync_mode` | `local` | which copy wins for differing files during `--resync` |
| `ignore_times` | auto | force transfers of same-size files. Auto-detected: on for remotes (including wrapped `crypt`/`alias` remotes) that cannot store file modification times (webdav, ftp -- e.g. web.de); off otherwise |
| `verify_download` | auto | `--verify` downloads every file. Auto-detected: on for remotes that offer no checksums (webdav, ftp, crypt, or wrapped chains); off otherwise (also verified against rclone's backend features before trusting a checksum-only check) |
| `rclone_flags` | `[]` | extra rclone flags, e.g. `["--tpslimit", "4"]`: bisync-only flags reach only bisync, and filter flags (`--exclude`, `--min-size`, ...) are kept away from housekeeping calls (pruning, listings, folder checks) so those always work. Flags the wrapper controls itself are refused, see [Usage](#usage) |

The auto-detection reads only the active remote's section (and any wrapped
remotes) from `rclone config show` (in memory only; nothing of it is printed
or stored) and can be overridden per profile with `ignore_times` /
`verify_download`.

Changing the filter rules (or `local`/`remote`) requires one resync. The
script detects this and offers the resync on the next interactive run.

## Usage

```bash
./filesync.py docs            # preview, confirm, sync
./filesync.py docs -n         # preview only
./filesync.py docs -y         # no preview, no questions (cron)
./filesync.py docs --verify   # content check of both sides
./filesync.py --list          # configured profiles
./filesync.py ~/Docs cloud:/Docs --filter '- *.tmp'   # ad-hoc, without profile
./filesync.py docs -- --tpslimit 4 --bwlimit 2M       # extra rclone flags (bisync + most calls)
```

Some rclone flags are set by the wrapper and are refused in `rclone_flags` and
after `--`, because they would silently override its preview, baseline or
report: `--force`, `--resync` (`-1`), `--resync-mode`, `--dry-run` (`-n`),
`--interactive` (`-i`), `--filters-file`, `--backup-dir1`, `--backup-dir2`,
`--workdir`, `--log-file` and `--use-json-log`. Use the wrapper's own options
and profile keys instead (`--force`, `--resync`, `-n`, `resync_mode`,
`filters`, `backups`).

### First run

The first run of a profile is a *resync*: both sides are merged, files that
exist on only one side are copied to the other, and nothing is deleted. If a
file exists on both sides with different sizes, the `resync_mode` side wins
(default: local), and the other version is backed up. A missing remote
folder is created after asking. `-y` refuses to do a first run unless
`--resync` is given explicitly.

With an empty (or new) remote folder, the first run simply uploads every
local file; the preview lists them all with their sizes. If *both* sides are
empty, nothing is synced and no baseline is created (bisync could not
continue from an empty one); the first run that finds files does the first
sync.

### Overview

```text
Overview
========
  download to local:         1 file(s)         19 B
      docs/notes.txt   [changed: size (larger), 19 B]
  delete on server:          1 file(s)
      old.txt   [deleted locally]
  conflict:                  1 file(s)
      docs/2024/plan.txt   [changed on both sides, both kept: plan.txt.conflict1 = local version, plan.txt.conflict2 = server version]

Note: 1 file(s) will be replaced and 1 deleted; the old versions are kept in .file-sync-backup/2026-11-23_213215/ on the side where they change.
Apply these 3 change(s)? [y/N]
```

After the run, the script lists the files that were actually moved into the
backup folders. If conflict copies (`*.conflictN`) exist, it prints a
warning on every run until they are resolved:

```text
Backups of replaced/deleted files (copy a file back to restore it):
  local : 1 file(s) in ~/Documents/.file-sync-backup/2026-11-23_213215/
      docs/notes.txt
  server: 1 file(s) in cloud:/Documents/.file-sync-backup/2026-11-23_213215/
      old.txt
warning: 2 conflict copies wait to be merged (the file was changed on both sides):
    docs/2024/plan.txt.conflict1
    docs/2024/plan.txt.conflict2
```

### Conflicts and backups

These are two separate mechanisms:

| Situation | What happens | What you do |
| --- | --- | --- |
| File changed on **both** sides | Both versions are kept *next to the file* as `.conflictN`, and both are synced. With `conflict_resolve = "none"` the original name disappears; with e.g. `"local"` the winner keeps the name and the loser becomes `.conflict1`. | Merge into the original file, delete the `.conflictN` copies. The script warns on every run until you do. |
| File changed or deleted on **one** side | The other side is updated; its old version moves to `.file-sync-backup/<time>/` on that side. | Nothing; restore from there if needed. Pruned after `keep_backups_days`. |

### Unattended runs

```cron
0 * * * *  $HOME/bin/filesync.py docs -y >> $HOME/.local/state/file-sync/cron.log 2>&1
```

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | success, or everything already in sync |
| 1 | sync failed or could not run (safety abort, resync needed, another run of the profile in progress, rclone missing or too old, state directory not writable, ...), or `--verify` found differences |
| 2 | usage or configuration error |
| 3 | aborted at a prompt |
| 130 | interrupted (Ctrl-C); the next run continues |

## Safety

| Protection | Provided by |
| --- | --- |
| Change detection against the previous run on each side (three-way) | rclone bisync |
| Changed on both sides: keep both versions as `.conflictN` copies | rclone bisync |
| Conflict copies named with their origin; warning on every run until merged | wrapper |
| Replacements and deletions announced before, backed-up files listed after the run | wrapper |
| Deleted on one side but changed on the other: the changed file is restored | rclone bisync |
| Abort if more than `max_delete` % would be deleted, or if all files changed | rclone bisync |
| Abort if one side is completely empty (unmounted disk, wrong path) | rclone bisync |
| Replaced and deleted files are moved to `.file-sync-backup/<time>/` on their side | rclone bisync, enabled by the wrapper |
| Resume after a crash or Ctrl-C without a resync (`--resilient --recover`) | rclone bisync, enabled by the wrapper |
| Preview and confirmation; `--force` only with a reviewed preview, also not via `rclone_flags`, `--`, or `RCLONE_*` environment variables | wrapper |
| One run per profile (`flock`, inherited by rclone); stale bisync locks removed safely | wrapper |
| First run and filter changes are never done silently by `-y` | wrapper |
| Overwrites are counted exactly in the preview, also in a resync and with backups off | wrapper |
| Changes that happened between preview and sync are reported after the run | wrapper |
| Common credential parameters and URL-embedded passwords (`user:pass@host`) inside the REMOTE string are refused (best effort, not a complete list). Plain `http://` remotes (including wrapped remotes) get a warning | wrapper |
| A vanished remote folder is an error, not a reason to delete local files | wrapper |
| File names that imitate rclone's messages (`Failed to bisync.txt`) or contain terminal control sequences do not alter the run's outcome or spoof the preview | wrapper |
| Same-size edits reach remotes without file times (`--ignore-times`, auto-detected) | wrapper |
| State and logs in `~/.local/state/file-sync/` with mode `0700` | wrapper |

Backups keep the directory structure: a replaced `docs/2024/report.txt` on
the server ends up as `.file-sync-backup/<time>/docs/2024/report.txt` on the
server. Each run gets its own `<time>` folder, so older versions are not
overwritten. To restore a file, copy it back from there.

The backup folders `.file-sync-backup/` are excluded from syncing. On the
remote they count against its storage quota until they are pruned after
`keep_backups_days`.

## Limitations

**Remotes without modification times** (webdav, ftp -- web.de among them)
get special treatment, with remaining gaps:

- Their listings show the *upload* time, not the file's own modification
  time. bisync therefore detects changes made *on the server* (web
  interface, phone, other PCs) by size only. A server-side edit that keeps
  the size is invisible to the sync. Run `--verify` occasionally: it
  downloads everything once and lists files whose content differs.
- rclone itself would skip same-size uploads to such a server; the wrapper
  passes `--ignore-times` for the transfers bisync has decided on (per
  remote, auto-detected).
- `newer`/`older` for `conflict_resolve` or `resync_mode` compare upload
  times there and are therefore unreliable; the script warns about them.

**In general:**

- Symlinks are skipped (rclone does not follow them); the overview counts them.
- If an upload is killed hard (not Ctrl-C), the server may keep a partial
  file. The next run treats it as a conflict and keeps both copies; the
  `.conflict1` copy is the complete local file.
- After a Ctrl-C, bisync finishes the transfer in progress and stops. The
  next run syncs the rest; an interrupted first run (resync) is repeated.
- The preview is a plan: if files change before you confirm, bisync acts on
  the current state (its safety checks still apply). The script lists such
  extra actions after the run.
- `--verify` downloads everything on remotes without checksums (auto-detected;
  remotes with checksums are compared without downloads); it shows rclone's
  progress meanwhile.
- A file that cannot be read (permissions) or a file on one side with the
  same name as a folder on the other makes bisync stop with a critical
  error. The script names the files and the cause; after fixing it, one
  interactive run (a resync) is needed. Unattended `-y` runs stop until then.
- Each run makes one transfer attempt (`--retries 1`): with `--ignore-times`
  a retry would re-send every queued file. Failed files are retried by the
  next run; single network requests are still retried by rclone.
- Not verified against the real web.de server: case-insensitive name
  collisions (`a.txt` vs `A.txt`), characters web.de rejects, and the error
  on a full quota (the script recognises HTTP 507 / "quota"). Before
  pointing a profile at primary folders, run one small smoke test against a
  throwaway folder on your remote: a first resync, a two-way edit, an
  overwrite and a deletion (check `.file-sync-backup/<time>/`), and a later
  run that prunes the old backups.

## Files

| Path | Content |
| --- | --- |
| `~/.config/file-sync/profiles.toml` | profiles (`--config` or `$FILESYNC_CONFIG` to override) |
| `~/.local/state/file-sync/<state>/baseline.json` | record of the last resync (paths, filter rules, rclone version) |
| `~/.local/state/file-sync/<state>/bisync/` | bisync's listings and lock |
| `~/.local/state/file-sync/<state>/filters.txt` | generated filter rules (bisync adds `filters.txt.md5`) |
| `~/.local/state/file-sync/<state>/logs/` | one readable log per run (last 30) |
| `~/.local/state/file-sync/<state>/lock` | the wrapper's lock: one run per profile |
| `~/.local/state/file-sync/<state>/last-stamp` | time stamp of the last run, so each run gets its own backup folder |
| `<local>/.file-sync-backup/`, `<remote>/.file-sync-backup/` | backups of replaced and deleted files |

`<state>` is the profile name followed by a short hash of it, e.g.
`docs-46b42b42/` for the profile `docs`; characters other than letters,
digits, `_`, `.` and `-` in the name become `_`. Ad-hoc runs
(`LOCAL_DIR REMOTE`) use `adhoc-<hash>/`, with a hash of the folder pair.
`$XDG_CONFIG_HOME` and `$XDG_STATE_HOME` replace `~/.config` and
`~/.local/state` if they are set.

## Development

```bash
make test           # unit + integration tests with rclone v1.66.0 and the current release
make lint           # ruff
make fetch-rclone   # download the test binaries into build/rclone/
```

The integration tests in [`tests/test_filesync.py`](tests/test_filesync.py)
run the script against `rclone serve webdav` with a `vendor = other` client
remote -- like web.de, that server cannot store modification times and
reports upload times (this exercises the `--ignore-times` path). A second
scenario syncs through a `local`-type remote with real modification times and
checks that same-size edits are detected there *without* `--ignore-times`.
The WebDAV server is always rclone v1.66.0 (`FILESYNC_TEST_SERVER`), because
`rclone serve webdav` v1.76.0 sometimes stalls for minutes when a file is
moved into a new folder -- a bug in the test server only, not in bisync. The
tests cover the first run, two-way changes, same-size edits, conflicts (kept
and resolved), conflict reminders, backup layout and listing, previews of
files >= 1 KiB, overwrite warnings without backups, `check_access`, special
file names, unreadable files, file/folder name clashes, the whole drive as
remote, rclone upgrades, empty sides, rclone flags via `--` (including backup
pruning under filter flags, refused wrapper-owned flags, and the filter flag
list against `rclone help flags`), file names that imitate rclone messages,
stale-listing cleanup on resync, http warnings, remote capability
auto-detection, changes between preview and sync, safety aborts, `--verify`,
profiles, backup pruning, locking, Ctrl-C (also during a resync), and an
encrypted rclone config.

The script was reviewed externally at each stage -- the earlier, self-written
sync engine, the first `rclone bisync` rewrite, and the current wrapper;
those reviews are preserved in git history.
