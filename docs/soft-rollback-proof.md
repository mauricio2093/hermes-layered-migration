# Soft rollback — functional proof

The [schema audit](downgrade-audit.md) proved the shapes line up. This proves
the old release actually *works* on the new database.

Both were needed. Structural compatibility says no column vanished; it says
nothing about whether the old code's queries still run.

## Method

A consistent copy of the live database, taken with SQLite's backup API — never
a file copy of a database being written to, and never the live file:

```
copy      schema_version 30 · integrity ok · 25 tables · 77 sessions · 7111 messages
```

The old release's own interpreter and virtualenv, pointed at the copy.

## Reads and writes

| Operation | Result |
|---|---|
| open the v30 database | OK |
| `schema_version` after opening | **still 30** — not downgraded, not altered |
| list sessions | OK |
| open an existing session | OK |
| read that session's messages | OK |
| create a session | OK |
| append a user message | OK |
| append an assistant message | OK |
| read both back | OK |
| `message_count` updated on the session | OK |
| full-text search finds the new text | OK |
| `integrity_check` after writing | **ok** |

The most telling line is the second: opening a newer database did **not** make
the old code rewrite the version or attempt a downgrade. It simply used it.

## The old release's own test suite

Run twice — against its own fixtures, then with the environment pointed at the
v30 copy:

```
baseline          381 passed, 1 failed
on the v30 copy   381 passed, 1 failed   ← identical
```

The single failure is pre-existing and unrelated: a full-text search projection
test that fails on the release's own fixtures too. **It is not a v30 symptom.**

The live database was untouched throughout: unchanged modification time,
`schema_version` still 30.

## Verdict

```
structural compatibility   confirmed
functional compatibility   confirmed
```

**Soft rollback is safe**: the old release runs on the current database. The
older database stays a last resort, not the default path — restoring it would
trade a code rollback for losing everything written since it was taken.

## What this does not cover

The gateway was not started against the copy: two pollers on one bot token
collide, so it cannot be exercised in parallel with a live one. The state layer
underneath it — which is what the schema change touched — is covered.
