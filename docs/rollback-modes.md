# Rollback — soft and hard

Two modes, and the distinction matters more than it looks.

```
soft   (default)         old code + current database
hard   (--hard)          old code + pre-cutover database
```

**Hard is never an automatic consequence of soft failing.** If the service
does not come up after a soft rollback, the script reports it, keeps every
artifact, and stops. Escalating by itself would turn one problem into two.

## Why soft is the default

The pre-cutover database is hours older than the live one. Restoring it
automatically trades a code rollback for **silently losing everything written
since**. A rollback that loses data is not a rollback.

Soft is only the default because it was [proven to
work](soft-rollback-proof.md) — not because it is convenient.

## Fail-closed, before anything is touched

The script aborts *before* modifying the system when: any required artifact is
missing, the live database fails `integrity_check`, the detected schema is not
the expected one (override is explicit), or — in hard mode — no verified backup
at the target schema exists.

Every one of those was exercised. Two examples that actually fired during
testing: a removed unit file, and a corrupted backup. Neither stopped the
service or touched a database.

## Snapshots

Taken with SQLite's backup API — **never `cp` on a live database**, which
produces a torn file whose corruption `cp` cannot detect. Each snapshot is
verified for `integrity_check`, expected schema and non-zero size before the
run continues.

Snapshots are never deleted automatically, and the pre-cutover backup is never
overwritten. In hard mode the current database is **moved aside, not removed**,
and only after its snapshot has been validated.

The target backup is chosen as the **most recent** one matching the target
schema, not a hardcoded path: an older backup loses more state for no reason.

## Reversible in both directions

Before touching anything, the run writes `recovery.env` with the current unit,
config and snapshot paths — the material to go back to the newer release:

```
newer release
   ↓ soft
older release + current database
   ↓ recovery.env
newer release + current database
```

and for hard, the displaced database is kept alongside the snapshot, so the
newer schema remains recoverable.

## Health check

By response, not by `is-active`. A service with `Restart=always` shows
`active` while crash-looping, so the check compares the PID before and after a
delay: a changed PID means it restarted, which is not health.

## Testing

Exercised in an isolated environment with its own throwaway service, never
against production: dry-run leaving nothing behind, missing artifact, wrong
schema, soft, hard, recovery from the snapshot, repeated runs, and a corrupted
backup. All eight behaved as specified.
