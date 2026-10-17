# `BACKUP-DECL-007` — names instead of counts, SQLite by content, and the host

**Implemented and tested 2026-10-16. Not installed:** the production copy of the
script (`~/.hermes/scripts/backup-hermes-home.sh`) and the declarations file
(`~/.hermes/backup-declarations.sh`) are unchanged. See "What needs
authorization" below.

## What the daily backup found

`hermes-maint` reported Degraded every night from 2026-10-14 on: the newest
verified backup was 15 days old. Nothing ran a backup automatically. A manual
run would also have been rejected:

```
✗ ABORT: plugins no coincide con lo declarado   no declarado: orca-status
✗ ABORT: scripts no coincide con lo declarado   no declarado: lang-guard.py
✗ anchors/   30/27
```

Each item was audited before being declared. No count was raised to make the
backup pass.

| item | what it is | verdict |
|---|---|---|
| `plugins/orca-status` | Installed on 4 Oct by the Orca app (`/usr/bin/orca`), marked "Managed by Orca", and **enabled**. It forwards session events, including prompts and responses, to `http://127.0.0.1:<port>/hook/hermes` only, and only when `ORCA_AGENT_HOOK_PORT`/`TOKEN` and `ORCA_PANE_KEY` are set. The gateway has none of them (checked in `/proc/<pid>/environ`), so there it is a no-op. It contains no secrets. | declare |
| `scripts/lang-guard.py` | Appeared on 10 Oct. A Hermes cron job runs it (`cron/jobs.json`). It reads `state.db` read-only and warns when the assistant answers in another script. It keeps its cursor in `cache/`. Handwritten Python with no secrets. Its mode is 0664, not 0700 like the others. | declare |
| `anchors/` 27 → 30 | Three new anchor documents in UTF-8 text (two from 3 Oct, one from 12 Oct). Found by diffing against the tar of the last verified backup (2026-09-30). Their names live only in the local declarations file. | declare |

The audit found two more things.

**`state.db.bak-3504`** is a 49 MB SQLite file, a copy that a Hermes migration
made on 2026-09-28 (schema 30, 100 sessions, 8,442 messages, `quick_check` ok).
No inventory knew about it, because the search only looked for `*.db`. The tar
carried it as an ordinary file and nothing checked it. It is now declared as
**archived** (`DECL_SQLITE_COLD`). It still travels in the tar, but it is
opened with `immutable=1` and passes `integrity_check` before and after the
restore.

**What lives outside `HERMES_HOME` was not in the daily backup.**

| piece | where it lived |
|---|---|
| The 14 carried commits (Layer 2) | only on this disk; only 4 Telegram patches and `hermes_layer2/` are public |
| The `hermes_hro` wheel | only in `hermes-control/dist/`, which is in no git repository |
| The `10-legacy-venv-launch.conf` drop-in that keeps the gateway alive | not backed up |
| `pip freeze`, the crontab, the launchers | not backed up |

The daily backup restored the data, but not the Hermes that uses it.

## The change

1. **`patches/`, `anchors/` and `voice-samples/` by name**, like `plugins/` and
   `scripts/` since 2026-09-27. The new lists are `DECL_PATCHES`,
   `DECL_ANCHORS` and `DECL_VOICE`. An undeclared name aborts, and so does a
   declared name that has disappeared. A **missing list** also aborts: it never
   counts as an empty one. The check runs in the main shell, because in a
   `$(…)` subshell the `decl_ok=false` would have been lost silently.

2. **SQLite inventory by content.** A file counts as a database if it starts
   with the `SQLite format 3` header, whatever its name (`x.sqlite`,
   `state.db.bak-N`). The search covers **exactly** what the tar covers.
   Previously the tar included `hermes-agent/` but the search skipped it, so a
   database in there would have gone missing without warning. Every database
   is either live (`DBS`, consistent copy) or archived (`DECL_SQLITE_COLD`).
   Anything else aborts. An archived database with a non-empty `-wal` or a
   `-journal` aborts too, because it has a writer.

3. **Exclusions by exact path, never by pattern.** The live databases
   declared inside the content directories (`anchors/data/phone.db`) are left
   out of the manifest by their exact path. A `cache.db` that is *not* a
   database still goes in the manifest; since the tar does not carry it, the
   restore reports it missing. A first draft excluded `*.db` and created
   exactly the silent drop it was meant to prevent; a test now keeps it from
   coming back.

4. **`host/`: host state.**

   | file | content |
   |---|---|
   | `carried.bundle` | carried commits, checked with `git bundle verify` |
   | `prod-refs.txt`, `prod-status.txt` | refs and status of the production checkout |
   | `venv-freeze.txt` | `pip freeze` of the production venv |
   | `wheels/` | wheels installed from a local file, with sha256 equal to the installed one |
   | `systemd/` | `hermes-*` units and the gateway drop-ins |
   | `crontab.txt` | the user crontab |
   | `launchers.txt` | sha and owner of `~/.local/bin/hermes`, `hermes-maint` and the root helper |
   | `repos.txt` | state of the repositories, including unpushed work |

   Root's secrets (the TOTP key, sudoers) are **not** copied; only their
   metadata is recorded. They are re-provisioned after a restore.

5. **`state.json` gains `host_state`.** `backup_verified` is still the AND of
   every flag, so a backup without host state is not verified. `hermes-maint`
   reads the five flags it knows and ignores the extra one. This was tested
   by running `hermes-maint` against the new backup in a temporary
   `HERMES_HOME`: "1 verified backup", `Ok`.

6. The tar and the inventory share a **single coverage definition**
   (`EXCLUDE_TOP`, `EXCLUDE_SUB`). The independent verifier
   (`updater/hermes_updater/verify.py`) keeps its own copy, and a test
   requires the two to match.

The independent verifier checks all of the above with its own code. It does
not read the script's verdict.

## Evidence (2026-10-16)

Isolated environment: a copy of the real `~/.hermes` with the tar's coverage
and its databases copied hot through the SQLite API, plus the proposed
declarations. The code and the units were read from the real system,
read-only.

```
script            backup_verified: True   (6/6 flags, host_state included)
inventory         11 SQLite databases: 10 live + 1 archived
declarations      plugins 5 · scripts 11 · patches 6 · anchors 30 · voice-samples 7
host              14 carried commits in carried.bundle (git bundle verify ok)
                  pip freeze 114 · hermes_hro wheel = installed sha256
independent       8/8 PASS: manifest, archive (17273 entries), inventory,
                  11 databases restored (quick+integrity ok), 103 declared files,
                  permissions, host_state, 14 critical paths
hermes-maint      backup-freshness: "1 verified backup" -> Ok
rebuild           18/18 PASS: data + code from the bundle (HEAD and tree identical
                  to production) + wheel + freeze + units; the RESTORED code starts
                  with the RESTORED Layer 3: config check, 3 plugins enabled,
                  SessionDB 125 sessions / 12,488 messages
```

Tests: 28 for the script (`tests/backup`) and 91 for the orchestrator. Of the
targeted mutations, 9/9 are detected; two of them first exposed weak tests,
which were strengthened.

## A side effect, undone

While inspecting `state.db.bak-3504` with `mode=ro`, SQLite created an empty
`-wal` and a `-shm` next to it in production. It does that even read-only when
the database is in WAL mode. The database itself did not change: same sha256,
mtime still 28 Sep. Both files were removed after checking that no process
had them open. The script and the verifier now open archived databases with
`immutable=1`, which creates nothing; a test checks it.

## What needs authorization

Nothing below has been done.

1. Replace `~/.hermes/backup-declarations.sh` with the reviewed proposal, which
   adds `orca-status`, `lang-guard.py`, `DECL_PATCHES`, `DECL_ANCHORS`,
   `DECL_VOICE` and `DECL_SQLITE_COLD`.
2. Copy `scripts/backup-hermes-home.sh` to `~/.hermes/scripts/`. It must go in
   **together** with step 1: the new script without the new lists aborts, and
   the old script with the new lists still counts anchors (30/27).
3. Run a real backup into `~/.hermes/backups/independiente/` (this clears
   hermes-maint's Degraded), then run the independent verifier against it.
4. Optional, as a separate decision: `orca-status` sends prompts to Orca when
   Hermes runs inside an Orca pane. Declaring it is about the backup, not
   about approving that behaviour.
