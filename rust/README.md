# hermes-maint

Scheduled maintenance for a Hermes install. Runs once, reports, exits.

**It is not a daemon.** `systemd.timer` owns the schedule, which is why there
is no scheduler in here. The reasoning, and the comparison against a resident
process, is in [`docs/hermesd-design.md`](../docs/hermesd-design.md) — read
that before changing anything structural.

```
hermes-maint-core/   lock, state, run lifecycle
hermes-maint/        binary: parse arguments, run once, exit
```

## What it does

```
acquire a single-instance lock
load and reconcile its own state
open a run
run the registered tasks
close it
--dry-run
exit with a defined code
```

Two tasks are registered:

- **`disk-space`** — reads `statvfs(3)` directly.
- **`backup-freshness`** — age of the most recent backup whose own checks all
  passed. It reads one JSON file per backup and never `mtime`, a directory
  name or the presence of a `.tar.gz`; see
  [`docs/task-backup-freshness.md`](../docs/task-backup-freshness.md).

Both run **in process**. The [child-process
supervisor](../docs/child-supervisor.md) and the [external-task
translation](../docs/external-tasks.md) exist and are tested end to end against
a fixture, but **no registered task spawns anything**. Choosing the first real
external observation is the next slice.

It opens no socket, makes no network call, needs no privileges, and never
touches Hermes' own `state.db`.

## Exit codes

```
0  ok
1  internal error
2  misuse — bad arguments, or state written by a newer version
3  lock busy — another run holds it; NOT a failure
4  partial — a task failed or was skipped
5  timeout — a task was killed on its deadline
6  degraded — everything ran, a health check reports degraded
7  incompatible-state — state written by a newer build
```

7 is separate from 2 because "old binary, new state" and "you mistyped
`--trigger`" are different problems with different fixes.

Exit 3 is why the unit declares `SuccessExitStatus=3`: a lock doing its job
must never appear in `systemctl --failed`.

## Build and test

```bash
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings
cargo build --release
```

Tests never touch the real `~/.hermes`; each one gets a throwaway
`HERMES_HOME`.
