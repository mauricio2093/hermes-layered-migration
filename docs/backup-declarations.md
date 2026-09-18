# `BACKUP-DECL-006` — two databases nobody was backing up

**Closed 2026-09-18.**

Hermes 0.21.3 introduced two SQLite databases. `backup-hermes-home.sh` declares
what it captures, so anything undeclared is excluded from the tar (which drops
`*.db`) *and* absent from `db-consistent/`. Both would have been missing from
every backup, silently, while the backup still called itself verified.

It did not. The invariant fired:

```
✗ ABORT: 8 bases en disco vs 6 declaradas
      cron/deliveries.db
      shared-state.db
✗ scripts/   8/5
backup_verified: False
```

That check exists because `anchors/data/phone.db` once slipped through the same
way. This is the second time it has paid for itself.

---

## What the two databases are

Inspected read-only before anything was changed; their hashes were identical
afterwards.

| | `shared-state.db` | `cron/deliveries.db` |
|---|---|---|
| size | 143360 B | 28672 B |
| mode | `0644` | `0600` |
| journal | WAL | WAL |
| `integrity_check` | ok | ok |
| tables | 14, all `hosted_room*` | `deliveries`, `delivery_tombstones` |
| rows | 0 — schema present, no data yet | 2 deliveries |

`shared-state.db` holds Hosted Rooms coordination; `cron/deliveries.db` is the
durable cron delivery queue and its tombstones. Both are durable state, and
both now go through **the same path as every other database** — a consistent
copy taken with `Connection.backup()` into `db-consistent/`, an
`integrity_check` on that copy, and validation during the temporary restore.
No exception was carved out for them, and neither is ever copied hot into the
tar.

## The three extra scripts

`scripts/` had grown from 5 to 8. Rather than trust the count, the five that
were declared were read out of the tar of the **last verified backup**
(`20260916-182215`):

```
backup-hermes-home.sh  battery-guard.py  herdr-hermes-onion.sh
nightly-low-power.sh   scan-secrets.sh
```

So the three additions are exactly:

| | |
|---|---|
| `cutover.sh` | the live gateway cutover, fossil → clean 0.21.3 |
| `push-limpio.sh` | publishing the cleaned history |
| `rollback.sh` | **the recovery tool** — nothing has a stronger claim to being inside the backup |

All three are hand-written bash, `0700`, owned by the user, executable, and
carry no secrets. None is temporary, generated, a cache, a dump or a test
artifact. The count was raised to 8 only after that was established, one file
at a time.

## The change

```diff
-declare -A EXPECT=( [plugins]=6 [scripts]=5 ... )
+declare -A EXPECT=( [plugins]=6 [scripts]=8 ... )

-DBS=(state.db verification_evidence.db kanban.db
-     cron/executions.db cron/notepad.db anchors/data/phone.db)
+DBS=(state.db verification_evidence.db kanban.db shared-state.db
+     cron/executions.db cron/deliveries.db cron/notepad.db anchors/data/phone.db)
```

and, in the generated `RESTORE.md`, the two new `cp` lines plus a `mkdir -p`
for `cron/` and `anchors/data/`. The tar does carry those directory entries —
checked — so the `mkdir` is belt and braces for a destination where they have
not materialised.

No other count was touched. No other part of the procedure was changed.

## The result

```
20260918-144610

expected_databases=8   consistent_backups=8   integrity_check ok on all 8
scripts/ 8/8   plugins/ 6/6   patches/ 6/6   anchors/ 27/27   voice-samples/ 7/7

created            true
archive_integrity  true
database_integrity true
manifest_integrity true
restore_verified   true
backup_verified    TRUE
```

Verified independently of the script afterwards: `shared-state.db` inside
`db-consistent/` has all 14 `hosted_room*` tables and passes
`integrity_check`; `cron_deliveries.db` has `deliveries` (2 rows) and
`delivery_tombstones`; the tar contains **zero** `*.db`, `*.db-wal` or
`*.db-shm` entries; `MANIFEST.sha256` verifies across 13 files; `RESTORE.md`
lists all 8 databases; the directory is `0700` and every file in it `0600`,
with nothing readable by group or other.

`backup-freshness` then picked it up on its own:

```
newest verified backup is 39s old (3 verified backups, 1 rejected)
```

## The failed attempt is kept

`20260918-132101` — 152 MB, missing two databases, `backup_verified: false`.

It was **not** deleted. It labels itself correctly, the tooling already rejects
it without being told to, and there are 823 GiB free. Deleting a backup
artifact is not reversible and there is no pressure to do it. Its own
`RESTORE.md` lists only six databases, so anyone reading it would restore an
incomplete installation — which is why it stays marked rather than tidied away.

## One thing observed and not acted on

Five of the eight live databases are mode `0644`:

```
0600  state.db  ·  cron/deliveries.db
0644  verification_evidence.db  ·  kanban.db  ·  shared-state.db
      cron/executions.db  ·  cron/notepad.db  ·  anchors/data/phone.db
```

That is how upstream Hermes creates them and predates this work; the two new
databases did not introduce it. The **copies inside the backup are all `0600`**
under a `0700` directory. Nothing was changed: altering permissions on live
Hermes databases is not part of closing this item.
