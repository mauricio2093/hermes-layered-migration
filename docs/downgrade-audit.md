# Downgrade audit — confirmed state

Point-in-time findings, verified against the live database and both source
trees. **Settled: do not redo this audit.**

## The discovery

Cutting the gateway over to a newer release migrated the state database
**in place**, five versions forward, on first start:

```
pre-cutover backup : schema_version = 25
after cutover      : schema_version = 30
old code expects   : schema_version = 25
```

There is **no downgrade guard**. Every migration is gated `if current_version
< N`, so old code would not notice the database is newer than it is — it would
skip them all and operate on a schema it has never seen.

The rollback script restored the unit, not the database. That gap was invisible
until someone compared the two numbers.

## What 26 → 30 actually changed

Only one migration block exists in that range, and it is not a schema change:
`v30` rebuilds the FTS trigram index so cron sessions and delegate-child
transcripts stop being indexed.

Comparing the two schemas directly:

| | |
|---|---|
| new tables | 3 |
| new columns | 8, across three tables |
| **dropped tables** | **none** |
| **dropped columns** | **none** |

## The check that decides it

Additive is not sufficient. A new `NOT NULL` column **without a DEFAULT** breaks
every `INSERT` written by older code that does not name it. Each was verified
against the live database:

| Column | NOT NULL | DEFAULT |
|---|---|---|
| `messages._compressed_summary` | yes | `0` |
| `sessions.git_metadata_generation` | yes | `0` |
| `sessions.hidden` | yes | `0` |
| `async_delegations.origin_session_id` | yes | `''` |
| the other four | no | — |

All four carry a default. **No old INSERT breaks.**

## Verdict

```
soft rollback   old code + current database     structurally viable
hard rollback   old code + pre-cutover database last resort only
```

**Structural compatibility is proven. Functional compatibility is not.**
Running the old release's state tests against a *copy* of the current database
is the remaining step. Never against the live one.

## Why hard rollback is not the default

The pre-cutover backup is hours older than the live database. Restoring it
automatically would trade a code rollback for **silent loss of everything
written since**. A rollback that loses data is not a rollback.

So the order is: snapshot the current database consistently first, keep it
under a timestamped name, and only then restore an older one — and only when
the soft path has actually failed.
