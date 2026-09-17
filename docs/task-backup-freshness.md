# Task: `backup-freshness`

Read-only. It answers one question: **is there recent evidence of a backup
that was actually verified?** It never creates, moves, deletes, repairs or
even opens a backup archive.

The distinction this task exists for, learned the expensive way in this
project: *"a recent backup exists"* and *"a restorable backup exists"* are
different claims, and only the second one is worth anything at 03:00.

---

## 1. Where it looks

```
$HERMES_HOME/backups/independiente/
```

Derived from `HERMES_HOME`, with the rest of the path compiled in.
**Configuration cannot change it.** A settable `backup_path` would hand
whatever can write the config the power to point a privileged-ish scan at an
arbitrary directory, which is a trust boundary bought for no benefit. If the
location ever needs to move, that is a code change and a review.

Only the **immediate** subdirectories are considered. No recursion: a backup
is a directory at that level, and walking deeper would mean trusting whatever
happens to be nested inside one.

## 2. What is authoritative

```
<backup>/state.json
```

Written by `backup-hermes-home.sh` at its final step, *after* every check has
run. Nothing else in the directory is consulted — not the archive, not
`MANIFEST.sha256`, not `db-consistent/`, and above all not the filesystem
metadata.

**`mtime` is not evidence.** Neither is the directory name, nor the presence
of a `.tar.gz`. A half-written archive has a perfectly fresh `mtime`; a
directory named with today's date proves only that somebody created a
directory. This task never reads any of the three.

## 3. What "verified" means

The backup script derives its verdict from five independent checks:

| flag | what actually produced it |
|---|---|
| `created` | the archive exists and is non-empty |
| `archive_integrity` | `gzip -t` **and** `tar -tzf` both succeeded |
| `database_integrity` | `PRAGMA integrity_check` on the *consistent copies*, plus a count invariant that fails loudly when a new database appears |
| `manifest_integrity` | `sha256sum -c` over every file in the backup |
| `restore_verified` | the archive was **extracted to a temporary directory** and its contents validated: no hot databases smuggled in, expected permissions on secrets, expected file counts per directory, and SQLite objects readable |

`backup_verified` is `all()` of those five. The script never sets it by hand.

`backup-freshness` accepts a backup only when **the top-level
`backup_verified` is true *and* all five flags are independently true.**

Re-deriving rather than trusting the summary is deliberate. The two can only
disagree if the file was edited or corrupted after the fact, and in that case
the summary is exactly the field an editor would flip. Agreement costs one
`&&`; disagreement means the evidence is not trustworthy, and the backup is
rejected.

## 4. Choosing the most recent verified backup

1. Enumerate the immediate subdirectories.
2. For each, read `state.json` and parse it.
3. Discard anything that is not verified, for any reason.
4. Among what survives, take the one with the **greatest declared
   `timestamp`** — the ISO-8601 instant inside `state.json`, not the
   directory name and not the file's `mtime`.

Age is then `now - declared_timestamp`.

## 5. Every case, and what it produces

`Skipped` and `Degraded` are not interchangeable, and the existing vocabulary
already decides which is which:

- **`Skipped`** — the check could not run because its precondition was absent.
- **`Degraded`** — the check ran, and what it found is not fine.

So:

| Situation | Outcome | Why |
|---|---|---|
| A verified backup, younger than the threshold | **Ok** | the thing this task exists to confirm |
| A verified backup, older than the threshold | **Degraded** | the check ran and the news is bad |
| The backups directory **does not exist** | **Skipped** | there is no backup system here to observe. Nothing was checked, so claiming "degraded" would be inventing an observation |
| The directory exists but is **empty** | **Degraded** | backups are configured and there are none. That is a finding, not a missing precondition |
| Backups exist but **none is verified** | **Degraded** | the strongest possible version of the finding: there are things that look like backups and not one of them is trustworthy |
| One backup has **no `state.json`** (the script died before its final step) | rejected candidate | not fatal; it counts toward "none verified" only if nothing else qualifies |
| One backup's `state.json` is **corrupt or unreadable** | rejected candidate | same. A single bad file must not blind the task to a good backup beside it |
| `backup_verified: false`, or any flag false | rejected candidate | exactly what the flag is for |
| **Unrelated directories** (e.g. `config/`, scratch, junk) | ignored | they have no `state.json`, so they fail on their own; they are counted in the summary so the number is never mysterious |
| A **symlink** where a backup directory is expected | rejected candidate | see §7 |
| Several verified backups | the newest wins | §4 |
| The newest backup is **not** verified but an older one is | the older verified one wins | verification is the filter; recency only orders what survives it |
| The directory cannot be read at all (permissions, I/O) | **`TaskError` → Failed** | the check did not run, and that is different from both of the above |

Nothing above modifies anything. The task opens `state.json` read-only and
writes nothing, anywhere, ever.

## 6. Staleness threshold

```
default: 48 hours
```

The backup is intended to run nightly. Forty-eight hours leaves room for
exactly one missed run before the tool starts complaining — long enough that a
single skipped night is not an alarm, short enough that two are.

Configuration may eventually set `enabled` and `max_age_hours`. It may never
set a path or a command. **No configuration file exists yet**; the value is a
field on the task with a default, so that when configuration arrives it has
somewhere to land without changing this task's logic.

The comparison is `>`, not `>=`: a backup exactly at the threshold is still
fine. An off-by-one here is a nightly false alarm.

## 7. Security

```
read-only · no shell · no subprocess · no network · no sudo · no state.db
```

- **Symlinks are not followed.** Each entry is examined with a
  `symlink_metadata` that does not traverse, and anything that is not a real
  directory is rejected with that reason. `state.json` itself must be a
  **regular file**, not a symlink.

  The reason is a trust boundary, not paranoia about the current contents:
  following a symlink would let anything able to write inside the backups
  directory decide which file this task reads, and redirect it outside the
  hierarchy entirely. Refusing costs nothing — the real backup script never
  creates symlinks there.

- **No traversal.** Only direct children are considered, and each one is
  joined to the base path as a single component. Nothing derived from a file's
  contents is ever used as a path.

- **Bounded reads.** `state.json` is size-capped before parsing, and the number
  of directories inspected is capped, so a pathological directory cannot turn
  a maintenance run into a disk-scanning job.

- **Nothing from the backup reaches the outside.** The task reports an age, a
  count and a verdict. It does not record the archive path, the machine's
  paths, or anything else out of the file it read.

## 8. Time

Timestamps are evidence, and evidence can be wrong.

```
declared_timestamp > now + 5 minutes   →   Degraded
```

Five minutes absorbs ordinary clock jitter and NTP correction. Beyond it, a
backup claiming to be from the future is either a clock problem or a corrupted
file, and in both cases the honest report is that it cannot be trusted — not
that it is extremely fresh.

**No negative age is ever computed.** A future-dated backup is reported as
"declares a timestamp N minutes in the future", never as a backup that is
"-3 hours old", which is the kind of output that makes a person stop reading
the report.

A timestamp with no timezone offset is rejected rather than guessed: the
backup script always writes one, so an offset-less timestamp means the file
is not what it claims to be.

## 9. Explicitly not in this task

```
creating a backup        repairing one       deleting an old one
running the backup script                    reading any archive
opening db-consistent/   verifying checksums itself
```

It reads one small JSON file per backup and forms an opinion. Anything that
*acts* belongs to a slice that does not exist yet, and may never.
