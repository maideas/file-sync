# filesync.py

Two-way sync between a folder on a Linux PC and any [rclone](https://rclone.org)
remote -- web.de Online-Speicher, Nextcloud, Google Drive, S3, an SFTP server,
... -- with a preview of every change and a confirmation before anything
happens.

[`filesync.py`](filesync.py) is an **on-demand / scheduled** wrapper around
`rclone bisync` (not a background file-watching daemon like Dropbox or
Syncthing: it runs when you invoke it or from cron / a systemd timer).
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

> **Sync is not a backup.** Two-way sync intentionally propagates edits and
> deletions between sides. The `.file-sync-backup/` folder is a short-term
> undo safety net (default 30 days, stored inside the synced folder), not a
> substitute for versioned offline backups.

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

### 1. Install `filesync.py`

[`filesync.py`](filesync.py) is a single self-contained script using only the
Python standard library (no `pip install` or virtual environment needed). Run
it directly from the repo as `./filesync.py`, or put it on your `$PATH`:

```bash
mkdir -p ~/.local/bin
ln -s "$(pwd)/filesync.py" ~/.local/bin/filesync   # or copy it there
```

### 2. Add the rclone remote

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

**Remote path syntax:** On WebDAV, Google Drive, and Nextcloud, `cloud:Docs`
and `cloud:/Docs` refer to the same folder, and `cloud:` is the remote root.
On `sftp:`, `cloud:Docs` is relative to your remote home directory while
`cloud:/Docs` is an absolute filesystem path (`/Docs`).

**Client-side encryption (optional):** If you want files encrypted before they
leave your PC, add an rclone `crypt` remote pointing at e.g.
`cloud:/encrypted` and use that `crypt` remote in your profile. `filesync.py`
follows wrapped remotes (`crypt`, `alias`, `chunker`, `compress`) when
auto-detecting modification-time and checksum support.

rclone stores passwords only *obscured* (scrambled, not encrypted) in
`~/.config/rclone/rclone.conf`. To encrypt the whole file, choose
`s) Set configuration password` in the `rclone config` main menu.
`filesync.py` then asks for that password once per run. For unattended runs,
set `RCLONE_PASSWORD_COMMAND` (e.g. `"pass show rclone"`).

Check the connection:

```bash
rclone lsd cloud:
```

### 3. Create a profile

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

**Filter rules are rclone rules, not `.gitignore` syntax:** every rule starts
with `- ` (exclude) or `+ ` (include), directories need `/**` (e.g.
`- node_modules/**`, not `node_modules`), and a leading `/` anchors to the
synced root (e.g. `- /build/**`). Adding an exclude rule stops syncing
matching files -- it does **not** delete copies that are already on the
server. Changing filter rules (or `local`/`remote`) requires one resync, which
the script detects and offers on the next interactive run.

#### Profile examples

All profiles go into the same `profiles.toml` -- one `[section]` per folder
pair, run by name (`filesync.py webde`). A few typical ones:

```toml
[webde]                          # web.de Online-Speicher (WebDAV)
local  = "~/Documents"
remote = "webde:/Documents"

filters = [
  "- .git/**",
  "- .venv/**",
  "- *.tmp",
  "- ~$*",                       # Office lock files
]

# webde's WebDAV stores no modification times and offers no checksums;
# filesync.py auto-detects this and turns on ignore_times + verify_download
# (same-size edits still transfer). Explicit (redundant, for clarity):
ignore_times    = true
verify_download = true

# be gentle with the WebDAV server
rclone_flags = ["--tpslimit", "4", "--transfers", "2"]

[webde-encrypted]                # web.de with client-side encryption
local  = "~/SecretDocs"
remote = "webde-crypt:"           # crypt remote wrapping webde:/encrypted
filters = ["- *.tmp"]            # verify_download auto-on: crypt has no
                                 # usable checksums

[nextcloud]                     # Nextcloud / ownCloud
local  = "~/Projects"
remote = "nextcloud:Projects"
filters = [
  "- /node_modules/**",           # leading / anchors to the synced root
  "- /build/**",
  "- /__pycache__/**",
  "- *.pyc",
]

[gdrive]                         # Google Drive
local  = "~/Pictures/Phone"
remote = "gdrive:PhoneBackup"
conflict_resolve = "larger"      # keep the bigger copy on conflicts (photos)
sync_empty_dirs   = false        # Drive has no real empty dirs

[docs-sftp]                      # SFTP server
local  = "~/Documents"
remote = "myserver:/srv/docs"     # absolute path (mind the slash after ':')
check_access = true              # guard against unmounted folders

[test]                           # small, cautious profile to start with
local  = "~/sync-test"
remote = "webde:/sync-test"
max_delete        = 5            # abort if >5% of files would be deleted
check_access      = true
keep_backups_days = 30           # default `backups = true` already applies

[fritz]                          # NAS / SMB share, e.g. a FRITZ!Box USB drive:
                                 # `rclone config` -> type `smb`,
                                 # host `fritz.box`, user/password of a NAS user;
                                 # the share name goes into the path
local  = "~/Documents"
remote = "fritznas:USB-STICK/Documents"

# smb serves real modification times (change detection works without
# ignore_times) but no checksums: --verify auto-detects and downloads every
# file once. Shares are case-insensitive: do not create `a.txt` and `A.txt`
# side by side.
check_access = true              # guard against the NAS being unreachable
filters = ["- .git/**", "- *.tmp"]
```

Two things to keep in mind when adapting these:

- every remote (`webde`, `webde-crypt`, `nextcloud`, `gdrive`, `myserver`,
  `fritznas`) must first be created with `rclone config` and exists in
  `rclone listremotes`
- do not point two profiles at the same folder pair -- each gets its own
  bisync state, which breaks change detection (see [Files](#files))

### 4. Preview and run your first sync

Before pointing a profile at important folders, try a quick smoke test on a
small throwaway folder (one resync, an edit, an overwrite, and a deletion to
see `.file-sync-backup/` in action). Then preview and sync your profile:

```bash
./filesync.py docs -n    # preview only -- changes nothing
./filesync.py docs       # preview, confirm [y/N], and run the initial resync
```

### 5. Update and uninstall

**Update:** `filesync.py` is a single self-contained script -- update the repo
checkout (or the copy on your `$PATH`) and run `filesync.py --list` once to
confirm it still reads the profiles. Profiles, state, logs, and backups are
not touched by an update; a changed rclone version is recorded in the next
baseline.

**Uninstall:** nothing runs in the background, so removing the tool is enough:
the symlink or copy on your `$PATH` (e.g. `~/.local/bin/filesync`), the cron
or systemd timer entry if any, `~/.config/file-sync/`, and
`~/.local/state/file-sync/`. Leftover `.file-sync-backup/` folders inside the
synced folders can be deleted (on the remote they count against its quota
until then); no other tool reads them. The synced files themselves stay as
they are on both sides.

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

| Option | Meaning |
| --- | --- |
| `-n`, `--dry-run` | Show the preview and exit without changing anything |
| `-y`, `--yes` | Skip preview and prompts (for cron); safety checks still apply, cannot be combined with `--force` |
| `--resync` | Merge both sides and rebuild the baseline (never deletes) |
| `--force` | Override bisync's `max_delete` / all-files-changed safety abort (interactive preview required) |
| `--verify` | Compare both sides (checksums if the remote supports them, full download otherwise) |
| `--filter RULE` | Filter rule for ad-hoc runs (repeatable; profiles use `filters` in `profiles.toml`) |
| `--max-show N` | Maximum files listed per category in output (default `25`) |
| `--list-all` | List every file without truncating |
| `--config FILE` | Profiles file (overrides `$FILESYNC_CONFIG` and default path) |
| `--rclone PATH` | `rclone` binary to use (overrides `$FILESYNC_RCLONE`) |
| `--list` | List configured profiles |
| `--example-config` | Print a starter `profiles.toml` |

Some rclone flags are set by the wrapper and are refused in `rclone_flags` and
after `--`, because they would silently override its preview, baseline or
report: `--force`, `--resync` (`-1`), `--resync-mode`, `--dry-run` (`-n`),
`--interactive` (`-i`), `--filters-file`, `--backup-dir1`, `--backup-dir2`,
`--workdir`, `--log-file` and `--use-json-log`. Matching `RCLONE_*`
environment variables (such as `RCLONE_FORCE`, `RCLONE_DRY_RUN`,
`RCLONE_RESYNC`, `RCLONE_LOG_LEVEL`, `RCLONE_QUIET`) are likewise stripped so
the environment cannot override these safeguards. Do not pass `-q`/`--quiet`
or `--log-level` after `--` either: the wrapper passes `-v` to parse bisync's
log, and rclone rejects combining them. Use the wrapper's own options and
profile keys instead (`--force`, `--resync`, `-n`, `resync_mode`, `filters`,
`backups`).

### First run

The first run of a profile is a *resync*: both sides are merged, files that
exist on only one side are copied to the other, and nothing is deleted. If a
file exists on both sides with different sizes, the `resync_mode` side wins
(default: local), and the other version is backed up. If both sides already
contain a file with the same name and **same size**, `--resync` assumes they
are identical without reading their contents -- if you are merging two
pre-existing copies of a folder, run `--verify` once afterwards to catch any
same-size differences. A missing remote folder is created after asking. `-y`
refuses to do a first run unless `--resync` is given explicitly.

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

What the preview categories mean (the report after a run uses the same
categories in past tense -- `uploaded`, `downloaded`, `deleted`):

| Category | What happened since the last run | What the sync does |
| --- | --- | --- |
| `upload to server` | file added or changed **locally** | copies it to the remote |
| `download to local` | file added or changed **on the server** | copies it to the PC |
| `delete on server` | file **deleted locally** | deletes it on the remote (its old copy lands in the server's `.file-sync-backup/<time>/`) |
| `delete locally` | file **deleted on the server** | deletes it locally (backed up first) |
| `conflict` | file changed **on both sides** | keeps both versions as `.conflictN` copies next to the file |

Long lists are truncated to `--max-show` files per category (default 25) with
`... and N more (use --list-all)` -- pass `--list-all` to see every file.

### Conflicts and backups

These are two separate mechanisms:

| Situation | What happens | What you do |
| --- | --- | --- |
| File changed on **both** sides | Both versions are kept *next to the file* as `.conflictN`, and both are synced. With `conflict_resolve = "none"` the original name disappears; with e.g. `"local"` the winner keeps the name and the loser becomes `.conflict1`. | Merge into the original file, delete the `.conflictN` copies. The script warns on every run until you do. |
| File changed or deleted on **one** side | The other side is updated; its old version moves to `.file-sync-backup/<time>/` on that side. | Nothing; restore from there if needed. Pruned after `keep_backups_days`. |

### Common tasks

- **Restore an overwritten or deleted file:** Look in
  `<local>/.file-sync-backup/<time>/` (if it was changed/deleted locally) or
  `<remote>/.file-sync-backup/<time>/` (if it was changed/deleted on the
  server) and copy the file back to its original path; the next sync
  propagates it.
- **Approve a legitimate mass deletion:** When bisync stops at `max_delete`
  (or "all files changed"), review the exact deletion list with
  `./filesync.py docs --force` and confirm `[y/N]`.
- **Recover when cron stops needing a resync:** Fix the underlying error shown
  in the log (e.g. file permissions or a file/folder name clash), then run
  `./filesync.py docs` once interactively to review and complete the resync.
  Unattended `-y` runs resume normally afterwards.
- **Rename a profile or repoint its folders:** The state directory is keyed by
  the profile name (`<name>-<hash>`, see [Files](#files)), so renaming a
  `[section]` starts a fresh state: the next run offers a resync like a first
  run (both sides merged, nothing deleted). After confirming it once, remove
  the old `~/.local/state/file-sync/<old-name>-<hash>/` to avoid confusion.

### Troubleshooting

Most failures print a message naming the cause; these are the ones that
usually need explaining on a first run:

| Message / symptom | Cause | What to do |
| --- | --- | --- |
| `another filesync run for this profile is in progress` | the profile lock is held -- a run is active, or a killed one left it | check for a running `filesync.py` / `rclone` process; the lock is a `flock` that dies with its process, stale bisync locks are removed by the next run |
| `... : a resync is needed -- run once interactively` | first sync of the profile, changed `local`/`remote`/`filters`, or missing listings after a crash | run interactively once and confirm the resync; unattended `-y` runs refuse until then |
| `remote folder ... not found -- was it renamed or deleted?` | the remote folder vanished (renamed elsewhere, or a typo in the profile) | fix the profile, or run with `--resync` to recreate it from the local side |
| `bisync's safety check stopped the sync` | more than `max_delete`% of files would be deleted, or all files changed | if intended, review the list with `./filesync.py docs --force` (interactive preview required) |
| `One side is completely empty` | an empty side looks like an unmounted disk or a wrong path wiping the other | check mount and paths; bisync refuses to run until both sides look real |
| `Another rclone bisync (not started by filesync) uses this profile's state` | a manual `rclone bisync` against the same state directory | let it finish; do not run manual bisync on filesync profiles |
| `rclone not found` / `rclone v1.xx is too old` | missing or outdated binary (`rclone version` to check) | install the current release as described under [Requirements](#requirements) |
| `cannot access <remote>` | remote not configured, or credentials expired | `rclone listremotes`, `rclone config`; test with `rclone lsd <remote>:` |
| `A file on one side has the same name as a folder on the other side` | file/folder name clash between the sides | rename one of them, then one interactive run (a resync) |
| `The remote storage seems to be full` | quota exceeded (HTTP 507) | free space on the remote -- old backups are in `<remote>/.file-sync-backup/` |

### Unattended runs

```cron
0 * * * *  $HOME/.local/bin/filesync docs -y >> $HOME/.local/state/file-sync/cron.log 2>&1
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

> **Do not mix profiles and ad-hoc runs on the same folder pair** (or define
> two profiles pointing at the same folders): each gets its own state
> directory, baseline, and lock, so running both breaks change tracking and
> allows concurrent runs against the same folders.

## Development

```bash
make test           # unit + integration tests with rclone v1.66.0 and the current release
make lint           # ruff
make fetch-rclone   # download and SHA-256-verify the test binaries into build/rclone/
```

The integration tests in [`tests/test_filesync.py`](tests/test_filesync.py)
run the script against `rclone serve webdav` with a `vendor = other` client
remote -- like web.de, that server cannot store modification times and
reports upload times (this exercises the `--ignore-times` path). Additional
scenarios sync through a `local`-type remote with real modification times
(checking that same-size edits work *without* `--ignore-times`) and through a
`crypt` remote stacked on top of WebDAV.
The WebDAV server is always rclone v1.66.0 (`FILESYNC_TEST_SERVER`), because
`rclone serve webdav` v1.76.0 sometimes stalls for minutes when a file is
moved into a new folder -- a bug in the test server only, not in bisync. The
tests cover the first run, two-way changes, same-size edits, conflicts (kept
and resolved), conflict reminders, backup layout and listing, previews of
files >= 1 KiB, overwrite warnings without backups, `check_access`, special
file names, unreadable files, file/folder name clashes, the whole drive as
remote, rclone upgrades, empty sides, rclone flags via `--` (including backup
pruning under filter flags, refused wrapper-owned flags, combined short flags,
`RCLONE_*` environment guards, and the filter flag list against
`rclone help flags`), file names that imitate rclone messages or contain
terminal escape sequences, stale-listing and orphan-log cleanup, http
warnings, remote capability auto-detection (including `crypt` and wrapped
remotes), changes between preview and sync, safety aborts, `--verify`,
profiles, backup pruning, locking, Ctrl-C (also during a resync), and an
encrypted rclone config.

The script was reviewed externally at each stage -- the earlier, self-written
sync engine, the first `rclone bisync` rewrite, and the current wrapper;
those reviews are preserved in git history.
