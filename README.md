# webde_sync.py

Two-way sync between a folder on a Linux PC and the web.de Online-Speicher
(or any other [rclone](https://rclone.org) remote), with a preview of every
change and a confirmation before anything happens.

[`webde_sync.py`](webde_sync.py) is a thin wrapper around `rclone bisync`.
rclone does all the synchronisation work: change detection against the last
run, conflict handling, safety aborts, locking, backups and crash recovery.
The wrapper adds what bisync lacks for everyday use:

- named profiles, so paths and filter rules are never retyped,
- a readable overview (from `bisync --dry-run`) and a `[y/N]` confirmation,
- safe defaults: backups, keep-both conflicts, `--resilient --recover`,
- a fix that makes same-size edits reach web.de (see [Limitations](#limitations)),
- a guided first run, a full content check (`--verify`), and logs.

## Requirements

| What | Version | Note |
| --- | --- | --- |
| Python | ≥ 3.11 | standard library only |
| rclone | ≥ 1.66 | Distribution packages are often older; check with `rclone version` |

If your rclone is too old, install the current release as a user (no root
needed), as described in <https://rclone.org/install/>: download the
`linux-amd64` zip and put the `rclone` binary into `~/.local/bin`. A
different binary can be selected with `--rclone PATH` or `$WEBDE_SYNC_RCLONE`.

## Setup

### 1. Create a web.de app password

web.de requires an app password for WebDAV access. Create one in the web.de
security settings (*App-Passwörter*), as described in the
[web.de help](https://hilfe.web.de/cloud/netzlaufwerk/index.html).

### 2. Add the rclone remote

Run `rclone config` and create a new remote:

| Prompt | Answer |
| --- | --- |
| name | `webde` |
| Storage | `webdav` |
| url | `https://webdav.smartdrive.web.de` |
| vendor | `other` |
| user | your web.de e-mail address |
| password | the app password |

rclone stores the password only *obscured* (scrambled, not encrypted) in
`~/.config/rclone/rclone.conf`. To encrypt the whole file, choose
`s) Set configuration password` in the `rclone config` main menu.
`webde_sync.py` then asks for that password once per run. For unattended runs,
set `RCLONE_PASSWORD_COMMAND` (e.g. `"pass show rclone"`).

Check the connection:

```bash
rclone lsd webde:
```

### 3. Create a profile

```bash
mkdir -p ~/.config/webde-sync
./webde_sync.py --example-config > ~/.config/webde-sync/profiles.toml
```

Edit the file: one `[section]` per folder pair.

```toml
[docs]
local  = "~/Documents"
remote = "webde:/Documents"
filters = ["- .git/**", "- *.tmp"]
```

| Key | Default | Meaning |
| --- | --- | --- |
| `local`, `remote` | required | local folder and rclone path (`NAME:path`) |
| `filters` | `[]` | [rclone filter rules](https://rclone.org/filtering/), first match wins |
| `filters_file` | none | file with further filter rules, appended to `filters` |
| `max_delete` | `50` | bisync aborts if more than this % of files would be deleted |
| `backups` | `true` | keep replaced and deleted files in `.webde-sync-backup/<time>/` |
| `keep_backups_days` | `30` | prune older backup folders after a successful run; `0` keeps all |
| `check_access` | `false` | require an `RCLONE_TEST` file on both sides (guards mount points); the first confirmed resync creates it if neither side has one |
| `sync_empty_dirs` | `false` | also create and delete empty directories |
| `conflict_resolve` | `none` | `none` keeps both copies; or `local`, `remote`, `larger`, `smaller`, ... |
| `resync_mode` | `local` | which copy wins for differing files during `--resync` |
| `rclone_flags` | `[]` | extra rclone flags, e.g. `["--tpslimit", "4"]`: applied to every rclone call; bisync-only flags only to bisync |

Changing the filter rules (or `local`/`remote`) requires one resync. The
script detects this and offers the resync on the next interactive run.

## Usage

```bash
./webde_sync.py docs            # preview, confirm, sync
./webde_sync.py docs -n         # preview only
./webde_sync.py docs -y         # no preview, no questions (cron)
./webde_sync.py docs --verify   # byte-by-byte comparison of both sides
./webde_sync.py --list          # configured profiles
./webde_sync.py ~/Docs webde:/Docs --filter '- *.tmp'   # ad-hoc, without profile
./webde_sync.py docs -- --tpslimit 4 --bwlimit 2M       # extra rclone flags (all rclone calls)
```

### First run

The first run of a profile is a *resync*: both sides are merged, files that
exist on only one side are copied to the other, and nothing is deleted. If a
file exists on both sides with different sizes, the `resync_mode` side wins
(default: local), and the other version is backed up. A missing remote
folder is created after asking. `-y` refuses to do a first run unless
`--resync` is given explicitly.

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

Note: 1 file(s) will be replaced and 1 deleted; the old versions are kept in .webde-sync-backup/2026-11-23_213215/ on the side where they change.
Apply these 3 change(s)? [y/N]
```

After the run, the script lists the files that were actually moved into the
backup folders. If conflict copies (`*.conflictN`) exist, it prints a
warning on every run until they are resolved:

```text
Backups of replaced/deleted files (copy a file back to restore it):
  local : 1 file(s) in ~/Documents/.webde-sync-backup/2026-11-23_213215/
      docs/notes.txt
  server: 1 file(s) in webde:/Documents/.webde-sync-backup/2026-11-23_213215/
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
| File changed or deleted on **one** side | The other side is updated; its old version moves to `.webde-sync-backup/<time>/` on that side. | Nothing; restore from there if needed. Pruned after `keep_backups_days`. |

### Unattended runs

```cron
0 * * * *  $HOME/bin/webde_sync.py docs -y >> $HOME/.local/state/webde-sync/cron.log 2>&1
```

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | success, or everything already in sync |
| 1 | sync failed, safety abort, or `--verify` found differences |
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
| Replaced and deleted files are moved to `.webde-sync-backup/<time>/` on their side | rclone bisync, enabled by the wrapper |
| Resume after a crash or Ctrl-C without a resync (`--resilient --recover`) | rclone bisync, enabled by the wrapper |
| Preview and confirmation; `--force` only with a reviewed preview | wrapper |
| One run per profile (`flock`, inherited by rclone); stale bisync locks removed safely | wrapper |
| First run and filter changes are never done silently by `-y` | wrapper |
| Overwrites are counted exactly in the preview, also in a resync and with backups off | wrapper |
| Changes that happened between preview and sync are reported after the run | wrapper |
| Credentials inside the REMOTE string are refused; plain `http://` remotes get a warning | wrapper |
| A vanished remote folder is an error, not a reason to delete local files | wrapper |
| Same-size edits are uploaded (`--ignore-times`) | wrapper |
| State and logs in `~/.local/state/webde-sync/` with mode `0700` | wrapper |

Backups keep the directory structure: a replaced `docs/2024/report.txt` on
the server ends up as `.webde-sync-backup/<time>/docs/2024/report.txt` on the
server. Each run gets its own `<time>` folder, so older versions are not
overwritten. To restore a file, copy it back from there.

The backup folders `.webde-sync-backup/` are excluded from syncing. On the
server they count against the web.de quota until they are pruned after
`keep_backups_days`.

## Limitations

- **web.de stores no file times.** Its WebDAV server reports the upload time
  and offers no checksums. bisync therefore detects changes made *on the
  server* (web interface, phone, other PCs) by size only. A server-side edit
  that keeps the size is invisible to the sync. Run `--verify` occasionally:
  it downloads everything once and lists files whose content differs.
- Local changes are detected by size and modification time. rclone itself
  would skip same-size uploads to a server without file times; the wrapper
  passes `--ignore-times` for the transfers bisync has decided on.
- `newer`/`older` for `conflict_resolve` or `resync_mode` compare upload times
  on web.de and are therefore unreliable; the script warns about them.
- Symlinks are skipped (rclone does not follow them); the overview counts them.
- If an upload is killed hard (not Ctrl-C), the server may keep a partial
  file. The next run treats it as a conflict and keeps both copies; the
  `.conflict1` copy is the complete local file.
- After a Ctrl-C, bisync finishes the transfer in progress and stops. The
  next run syncs the rest; an interrupted first run (resync) is repeated.
- The preview is a plan: if files change before you confirm, bisync acts on
  the current state (its safety checks still apply). The script lists such
  extra actions after the run.
- `--verify` downloads everything; it shows rclone's progress meanwhile.

## Files

| Path | Content |
| --- | --- |
| `~/.config/webde-sync/profiles.toml` | profiles (`--config` or `$WEBDE_SYNC_CONFIG` to override) |
| `~/.local/state/webde-sync/<profile>/bisync/` | bisync's listings and lock |
| `~/.local/state/webde-sync/<profile>/filters.txt` | generated filter rules |
| `~/.local/state/webde-sync/<profile>/logs/` | one readable log per run (last 30) |
| `<local>/.webde-sync-backup/`, `<remote>/.webde-sync-backup/` | backups of replaced and deleted files |

## Development

```bash
make test           # unit + integration tests with rclone v1.66.0 and v1.76.0
make lint           # ruff
make fetch-rclone   # download the test binaries into build/rclone/
```

The integration tests in [`tests/test_webde_sync.py`](tests/test_webde_sync.py)
run the script against `rclone serve webdav` with a `vendor = other` client
remote. Like web.de, that server cannot store file times and reports upload
times. The server is always rclone v1.66.0 (`WEBDE_SYNC_TEST_SERVER`), because
`rclone serve webdav` v1.76.0 sometimes stalls for minutes when a file is moved
into a new folder. That is a bug in the test server only, not in bisync. The tests cover the first run, two-way changes, same-size edits,
conflicts (kept and resolved), conflict reminders, backup layout and listing,
previews of files >= 1 KiB, overwrite warnings without backups, `check_access`,
rclone flags via `--`, http warnings, changes between preview and sync,
safety aborts, `--verify`, profiles, backup pruning, locking,
Ctrl-C (also during a resync), and an encrypted rclone config.

[webde-sync-code-review.md](webde-sync-code-review.md) is the review of the
previous, self-written sync engine that this version replaces.
