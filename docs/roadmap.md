# Roadmap — next work order

Written at a deliberate stopping point. Start here; do not redo settled work.

## Settled, do not repeat

- The schema audit in [downgrade-audit.md](downgrade-audit.md). Structural
  compatibility is proven.
- `busy_input_mode: steer` is applied and live — typing during a run no longer
  aborts it. This already existed upstream; it was configuration, not a gap.
- **Never take an upstream `SCHEMA_VERSION` number.** The day upstream ships
  its own next version, two different schemas would share one number.

## 1 · Prove the soft rollback functionally

Structural compatibility is not functional compatibility. Copy the current
database consistently, run the old release against **the copy**, and exercise:
startup, session read/write, new sessions, messages, commands, history, and the
gateway if it can be isolated. Run the old release's own state tests against it.

Never touch the live database.

## 2 · Rewrite the rollback with two explicit modes

**Soft** — restore code, unit and config; keep the current database when the
functional test confirms compatibility.

**Hard** — only when soft fails or is explicitly requested. Snapshot the current
database consistently *first*, keep it timestamped, then restore the older one.

Print what is happening, in these terms:

    Code: newer -> older
    DB:   keeping schema 30

    Code: newer -> older
    DB:   schema 30 -> schema 25
    Current DB preserved at: ...

Fail closed when any required artifact is missing.

## 3 · A layer-2 schema of our own

Inside the same database, never in a loose JSON file, and never touching
upstream's migrations:

    CREATE TABLE IF NOT EXISTS hermes_layer2_schema (
        component TEXT PRIMARY KEY,
        version   INTEGER NOT NULL
    );

Two independent version lines: upstream advances freely, ours starts at 1.
Centralize creation in one module — not ten modules each running their own
`CREATE TABLE`. Idempotent, transactional, concurrency-safe.

## 4 · Adaptive `/` command ranking

    layer2_command_usage(command, surface, use_count, score,
                         last_used_at, score_updated_at)
    PRIMARY KEY (command, surface)

Exponential decay, 30-day half-life. **Decay on read as well as on write** —
this is the part that is easy to get wrong. Decaying only on use leaves an
abandoned command frozen at its old score forever, since nothing ever touches
the row again:

    on write:  new_score = stored * 0.5^(elapsed_days/30) + 1
    on read:   effective = stored * 0.5^(elapsed_days/30)

The read-side decay need not be persisted. Keep `use_count` as history, out of
the ranking. Add a deterministic tiebreak so the menu does not shuffle for no
reason.

## 5 · CLI

Order the completer by effective score. No stars, no numbers. New and rarely
used commands stay discoverable. A contextual bonus is allowed **only** from
signals that already exist and are reliable, and is never persisted.

## 6 · Telegram, as a separate surface

The menu is set through the Bot API, with a size cap. Do not call it after
every command: compute the order, compare against what was last published, and
update only on a material change. Startup plus an infrequent refresh is enough.
Respect scopes, and never drop a required command for a low score.

## Status

Steps 1 through 6 are closed; see [status.md](status.md). **Nothing is written
in Rust yet and `hermesd` does not exist.**

## 7 · Then, and only then, the Rust slice

A small resident daemon owning the 03:00 maintenance run: lock, scheduler,
process execution, timeout, exit codes, health checks, logging, state, dry-run.
**Observe and report before it may restart anything.** No FFI yet — processes
and files only. It must be deletable with no trace.

Do not migrate the gateway, Telegram, agents, skills, the router, or
busy-session handling.

### The unresolved dependency

A resident daemon does **not** yet let the ~400 MB Python gateway sleep. While
Telegram ingress arrives through that gateway, nothing else can wake it. Before
claiming that saving, weigh honestly: the daemon receiving ingress itself, a
thin webhook in front of it, a separate small ingress process, or simply
accepting that the gateway stays resident.

Document the dependency now; do not port ingress to chase the number.
