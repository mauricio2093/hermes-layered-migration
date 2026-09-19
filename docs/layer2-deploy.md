# Layer 2 in production

Deployed **2026-09-18 19:04**. Observation window open; nothing further should
be changed in the gateway until it has run for a while.

---

## The finding that came first

Before anything was restarted, the gateway's checkout was inspected:

```
gateway process started   2026-09-16 21:30:13
branch checked out        2026-09-16 23:43:44   ← 2h 13min later
```

The running process (PID 1163669) had loaded `main`. The working tree it runs
from had been sitting on `feat/layer2-schema` for nearly two days.

**The deployment was armed and not fired.** A crash, a reboot, or systemd's own
`Restart=always` would have loaded Layer 2 with nobody deciding to — and the
first anyone would have known is a Telegram menu behaving differently. That
made doing it deliberately more urgent, not less.

## What was done first

| | |
|---|---|
| verified backup | `20260918-144610`, 4 hours old, all five flags |
| timer observed | first natural firing at 03:01:10 already behind us |
| branch re-validated | `tests/layer2/` 53 passed · touched suites 101 passed |
| modules import | both components register cleanly in the production venv |
| consistent DB copy | `Connection.backup()`, `integrity_check ok`, 25 tables, `schema_version` 30 |
| rollback written down | before the restart, not after |

### The rollback

```bash
cd ~/.hermes/hermes-oficial-clean
git checkout main
systemctl --user restart hermes-gateway.service
```

**No database rollback is needed.** Layer 2 only *adds* tables; `main` does not
know them and ignores them, and `schema_version` is never touched. That is the
soft-rollback property proven months of slices ago, now relied on for real.

## The restart

```
19:04:53  Stopping
19:04:58  Started                    new MainPID 1282588
19:05:36  [Telegram] Connected to Telegram (polling mode)
```

Four seconds of downtime on chat ingress.

One thing worth recording: the **old** process exited `status=1/FAILURE` on
`SIGTERM`. That happened on the way out, in the pre-Layer-2 code, before
anything new was loaded. It is not caused by this deployment, and it is not
investigated here.

## Layer 2 ran on its own, two seconds after connecting

Not triggered by hand. The gateway did it:

```
19:05:36   [Telegram] Connected to Telegram (polling mode)
19:05:37   telegram_command_menu  migrated to v1
19:05:38   menu published, fingerprint 3fdd8a5b45db9cf3…
```

The full Telegram path executed end to end in production: the rendered payload
was computed, `should_publish` found no previous fingerprint, `setMyCommands`
was called, and `mark_published` recorded it — which is exactly the sequence
designed so that a hundred command uses do not mean a hundred Bot API calls.

## Database after

```
27 tables (was 25)
  hermes_layer2_schema          telegram_command_menu v1
  layer2_telegram_menu_state    default → 3fdd8a5b45db9cf3…

schema_version   30   unchanged, as it must be
integrity_check  ok
```

### `layer2_command_usage` does not exist yet, and that is correct

The read path does **not** create schema. `rank_commands` returns the original
order when there is nothing stored, without writing; only
`record_command_use` calls `ensure_ready`. A completer that ran DDL on every
keystroke would be a worse bug than a menu that takes a day to start learning.

So the usage table appears the first time a command is actually executed, on
either surface. It was not forced into existence, and no usage was invented to
make it appear.

Verified separately that the read path works against the live database:

```
ranked([...]) → unchanged order (nothing learned yet)
SQLite reads: 1, then still 1 on the second call — the cache holds
```

## State now

```
gateway    active/running   MainPID 1282588   NRestarts 0
code       feat/layer2-schema @ 3480077eb6
timer      active, LAST 03:01:10, NEXT Sat 2026-09-19 03:00:19
hermes-maint  run 14, three observations Ok
backup     20260918-144610 intact
```

## What has not been observed yet

- A command actually being used, so `layer2_command_usage` appears and the
  ranking starts learning.
- A second Telegram menu evaluation, to confirm the fingerprint gate suppresses
  a republish when nothing changed.
- A full day of normal use.

None of that can be manufactured honestly. It needs the system to be used.
