# Status — consolidated, before a second language

Point-in-time close of the layer-2 block. **Read the first line before anything
else.**

```
Rust implemented:      0
hermesd implemented:   0
```

Nothing in this repository is written in Rust. `hermesd` does not exist. What
was built is the architecture that makes introducing it safe, and the
prerequisites that had to close first.

## What is actually done

| | |
|---|---|
| Soft / hard rollback | proven in an isolated environment, eight cases |
| Old release on the new database | structurally and functionally verified |
| Layer-2 schema | per-component versioning, atomic migrations |
| `command_usage` | frequency + recency, decay on write **and** on read |
| CLI ranking | zero SQLite reads per keystroke |
| Telegram ranking | learning decoupled from publishing |

## Size of the change

```
layer-2 code, ours      9 files   +1296 lines
patches into upstream   5 files   +92 / -3 lines
```

Ninety-two lines touching upstream, against nearly thirteen hundred of our own.
That ratio is the point of the layer model: upstream stays re-clonable.

## Suites

| Suite | base | branch | delta | new regressions |
|---|---|---|---|---|
| state | 1369 passed, 2 failed | 1369 passed, 2 failed | 0 | **0** |
| telegram + gateway + CLI | 774 passed, 1 failed | 867 passed, 1 failed | +93 | **0** |
| layer 2 | — | 53 passed | +53 | — |

Every failure listed is pre-existing: each was reproduced on `main` before
being dismissed, not assumed to be unrelated. The +93 reconciles exactly as
53 layer-2 tests, 38 from the Telegram port and 2 from the config-parsing fix.

## Against a copy of the real database

Never against the live one.

```
upstream schema_version   30 -> 30
layer-2 components        command_usage=1, telegram_command_menu=1
tables                    25 -> 28
sessions / messages       77/7111 -> 77/7111
integrity_check           ok
```

## Fail-open

Layer 2 is an improvement, never a dependency. With the database **absent**,
**corrupt** and **read-only**, all three return upstream's own order, record
nothing, and raise nothing. The menu falls back to what upstream would have
published.

## Versioning

Upstream's `SCHEMA_VERSION` stays at 30 and no number was taken from it. Layer 2
counts separately, per component, so upstream can advance to 31 and beyond with
no collision.

---

# Next phase: hermesd

**Not started.** The boundary, agreed in advance:

A small resident daemon owning the 03:00 maintenance run — single-instance
lock, scheduler, child processes, timeouts, exit codes, health checks, logging,
state, dry-run. It observes and reports before it is ever allowed to restart
anything, and it must be removable without a trace.

Not in the first slice: PyO3, the gateway, Telegram, the router, agents,
skills, command ranking, ESP32, any new ingress path.

## The saving that is not yet available

A resident daemon does **not** yet let the ~400 MB Python gateway sleep. While
chat ingress arrives through that gateway, nothing else can wake it. That
saving belongs to a later phase, after deciding who receives ingress while
Python is asleep — and the options there (the daemon itself, a thin webhook, a
separate ingress process, or simply accepting a resident gateway) have not been
weighed yet.

Do not claim the number before that question is answered.
