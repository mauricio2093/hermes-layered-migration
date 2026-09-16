# What "verified" means

> **Verified means restored and validated by an independent mechanism — never
> read back by the same tool that produced it.**

This rule was not adopted on principle. It was adopted after two backups in a
row reported success and were unusable.

## Case 1 — `git bundle` on a shallow clone

A 622 MB bundle was created without error. `git bundle verify` reported:

    The bundle records a complete history.

Cloning from it failed:

    error: Could not read 12eeadf8ac6eaadc624ac41d920a694d5861c918
    fatal: Failed to traverse parents of commit b007b80c
    fatal: remote did not send all necessary objects

The source was a shallow clone, so the bundle referenced parent objects that
did not exist locally. **The tool that wrote the backup could not detect that
the backup was incomplete.** A raw `tar` of the `.git` directory restored
correctly and is what this toolkit uses instead.

## Case 2 — consistent database copies that never shipped

The first version of `backup-hermes-home.sh` copied each SQLite database with
`Connection.backup()`, ran `PRAGMA integrity_check` on every copy, wrote a
SHA-256 manifest — and then deleted the staging directory holding those copies.

The archive shipped the *live* database files, hot-copied by `tar` while the
gateway was writing to them. It reported `backup_verified: true`.

Two defects, both invisible from inside the tool:

1. The consistent copies were verified and discarded; the archive carried the
   inconsistent ones.
2. The manifest listed the deleted staging files, so it no longer verified
   *after the script finished* — only during.

## The five flags

`backup_verified` is **derived**, never set:

```json
{
  "backup": {
    "created": true,
    "archive_integrity": true,
    "database_integrity": true,
    "manifest_integrity": true,
    "restore_verified": true
  },
  "backup_verified": true
}
```

`backup_verified` is `all(flags.values())`. The script exits non-zero unless
every one is true.

## Content checks, not just extraction

`restore_verified` extracts the archive to a temporary directory and asserts:

- every secret file is present **and still has its original mode** (`600`)
- each external layer has its **expected file count**
- every database passes `integrity_check` from the consistent copy
- the archive contains **no** hot-copied `*.db` at all

The counts are the important part. A structurally valid archive that a bad
glob left half empty extracts perfectly and passes every checksum. Only a
count catches it.

That check has already earned its place twice: it caught a sixth database
nobody had inventoried (`anchors/data/phone.db`), and it caught a fresh clone
silently inflating the archive from 111 MB to 937 MB.

## The database invariant

```
expected_databases = 6
consistent_backups = 6

if consistent_backups != expected_databases:
    ABORT
```

There is no single "Hermes database". When a seventh appears, the pre-flight
must fail loudly rather than quietly leave it out.
