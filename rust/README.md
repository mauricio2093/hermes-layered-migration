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

## What this slice does

```
acquire a single-instance lock
load and reconcile its own state
open a run
close it
--dry-run
exit with a defined code
```

**No tasks are registered.** A run opens and closes with nothing in between.
That is deliberate: the lock, the reconciliation and the persistence are much
easier to prove correct while there is no real work to confuse them with.

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
```

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
