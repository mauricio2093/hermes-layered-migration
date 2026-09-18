# hermes-maint — design before code

This document closed the boundary of the first slice **before** `cargo new`,
because the cheapest time to delete a responsibility is while it is still a
paragraph.

**Status: approved.** The architecture below is decided, not proposed:

```
systemd.timer
    ↓
hermes-maint          binary: runs once, exits
    ↓
hermes-maint-core     library: lock, state, supervision
    ↓
registered maintenance tasks
    ↓
exit
```

`hermesd` is not being built. The name is reserved for a future phase with a
real need for residency or events; see §1.1.

---

## 0. The facts this design is built on

Measured on the host, not assumed:

| Fact | Value | Why it matters |
|---|---|---|
| Total RAM | 6304 MB | tight; 339 MB free at the time of writing |
| Gateway | `hermes-gateway.service`, **user** unit, active | maintenance runs as the same user, not root |
| `Linger` | `yes` | user units run with no login session — a 03:00 user timer fires |
| `sleep.target` | **masked** | the machine never suspends; 03:00 is genuinely reachable |
| Timezone | `America/Bogota` (-05) | `OnCalendar` is local time by default; no conversion needed |
| Existing cron | one entry, 07:00, anchors | the 03:00 slot is free |
| Existing scripts | `backup-hermes-home.sh`, `rollback.sh`, `scan-secrets.sh`, `nightly-low-power.sh` | these are the children; they already exist and already work |

The last row is the most important one. **The first slice writes no maintenance
logic.** Every maintenance behaviour already exists as a tested shell script.
The slice is the thing that runs them predictably and writes down what happened.

---

## 1. Recommended architecture

```
systemd.timer (03:00, Persistent=true)
      │
      ▼
hermes-maint.service   Type=oneshot
      │
      ▼
hermes-maint  (Rust, runs, reports, exits)
      │
      ├── flock ── single instance
      ├── task registry (compiled in, not configurable)
      ├── child supervision + timeouts
      └── state.json (atomic write)
      │
      ▼
existing scripts in ~/.hermes/scripts/
```

**Decided: option B — a timer waking a short-lived binary.**

### 1.1 The name — decided

If it is not resident, calling it `hermesd` is inaccurate, and inaccurate names
cost real time two years later when someone looks for a running process that was
never there.

- **binary: `hermes-maint`** — what this actually is.
- **`hermesd` is reserved** for a future phase in which a real need for
  residency or event handling exists. Not earned yet, so not used yet.

The internal crate layout makes the rename a deployment change, not a rewrite:

```
hermes-maint-core/   library: tasks, lock, state, supervision, health
hermes-maint/        binary: parse args, run once, exit
```

A resident daemon, if ever needed, is a second thin binary over the same
library — a loop and a scheduler around `core::run_once()`. Nothing in the
first slice forecloses it.

---

## 2. Timer vs resident daemon

Evaluated on the axes requested, against this machine, not against a generic one.

| | A — resident `hermesd` | B — timer + oneshot |
|---|---|---|
| **RAM** | 24h of residency for ~minutes of work. Even a lean Rust process holds RSS, an allocator arena and its mappings all day. On a 6.3 GB box with 339 MB free, that is paid for nothing. | **0 bytes for 99.98% of the day.** |
| **Complexity** | Must implement: a scheduler, clock-jump handling, DST/NTP-step correctness, catch-up after downtime, its own restart policy, signal handling, log rotation pressure, leak-free long-running state. | systemd already implements all of that, in C, tested by everyone. |
| **Reboot recovery** | Own catch-up logic: persist "next due", compare on boot, decide whether a missed run still applies. Must be written and, worse, tested. | `Persistent=true`. One line. A missed run fires shortly after boot. |
| **Locking** | Needed anyway — a manual run can race the internal scheduler. | Needed anyway — a manual run can race the timer. **Not a differentiator.** |
| **Observability** | The schedule lives inside the process. `systemctl list-timers` shows nothing. Answering "when does it next run?" requires asking the daemon, which means building an interface to ask it. | `systemctl list-timers` answers it. Each run is its own journal unit invocation with its own `InvocationID`, exit code and duration, queryable with `journalctl -u`. |
| **Reliability** | Failure modes accumulate with uptime: leaks, stale handles, a wedged scheduler thread, a held lock after a panic in a thread rather than the process. | The process is young every time. A crash is contained to one run; the next run starts from a clean address space. |
| **Future expansion** | Genuinely better *if* the work becomes event-driven or sub-minute. This is the one axis where A wins. | Wrong shape for event-driven work. But that work is not in this slice, and the library split above keeps the door open. |
| **Uninstall** | Stop process, disable unit, remove units, remove state, confirm nothing survived. | `systemctl --user disable --now hermes-maint.timer`, delete two unit files, delete one state directory. Nothing is running to forget about. |
| **systemd fit** | systemd supervises a process that duplicates systemd's scheduler. | systemd does what it is for. |

### 2.1 The honest case for A

A wins when the trigger is an event rather than a clock: reacting to a gateway
crash within seconds, watching a socket, holding warm state between runs. If
that requirement arrives, A becomes correct — and that is precisely why the
scheduler must not be tangled into the task logic.

It has not arrived. Every task in the first slice is "at 03:00, look at things
and write down what you saw." Choosing residency for it would be paying the
full cost of A for none of its benefit.

### 2.2 What would change the decision

Written down now so the reversal is evidence-driven and not a mood:

1. A task needs to react in **seconds** to an event, not minutes to a clock.
2. Per-run startup cost becomes material (it will not: this is milliseconds).
3. Something genuinely needs to be held in memory between runs and cannot be
   reconstructed from disk.

Absent all three, B stays.

---

## 3. State model

**One file. Not a database. Not `state.db`.**

```
~/.hermes/hermes-maint/
├── state.json        last-run state, atomically replaced
├── lock              flock target, empty content is fine
└── reports/          one report per run, rotated by count
```

`state.db` ownership is explicitly out of scope. The Rust binary **never opens
it** — not read, not write, not to check a schema version.

The reason is ownership, not fear of corruption. A single reader would not
corrupt anything, and SQLite handles multiple writers perfectly well. The point
is that `state.db` **belongs to Hermes**, and the moment `hermes-maint` opens it
it acquires a dependency on things it does not control:

- its **schema**, which upstream changes when it likes;
- its **migrations**, which would have to be understood and tolerated;
- its **locking** and journal mode;
- its **lifecycle** — when it is created, moved, restored or replaced;
- its **future compatibility**, forever.

That is a standing coupling to another project's internals in exchange for
nothing. The operational state of maintenance is a domain of its own, it is
small, and it costs one JSON file to keep sovereign.

### 3.1 Shape

```json
{
  "schema": 1,
  "last_run": {
    "started_at": 1758000000,
    "finished_at": 1758000142,
    "trigger": "timer",
    "outcome": "ok",
    "tasks": [
      {"id": "backup",       "outcome": "ok",      "exit": 0, "duration_s": 96},
      {"id": "disk_report",  "outcome": "ok",      "exit": 0, "duration_s": 2},
      {"id": "health_probe", "outcome": "degraded","exit": 6, "duration_s": 4}
    ]
  },
  "history": [ /* last N run summaries, bounded */ ]
}
```

### 3.2 Rules

- **Atomic write.** The full sequence, because skipping any step of it is how
  "atomic" writes turn out not to be:

  ```
  1. create a temp file in the SAME directory   (same filesystem — rename()
                                                 is only atomic within one)
  2. write the serialised state to it
  3. flush + fsync the temp file                (data on disk, not in cache)
  4. rename(temp, state.json)                   (atomic replacement)
  5. fsync the directory                        (the rename itself durable)
  ```

  Restrictive permissions throughout: the file is created `0600` and the
  directory is `0700`, so the temp file is never briefly world-readable.

  The guarantee this buys, after a crash or a power loss at **any** point:

  ```
  state.json = the previous complete version
          or
  state.json = the new complete version

  never a partial write
  ```

  Step 3 is what makes it true rather than probably true — a rename is atomic
  with respect to the directory, but without the fsync the file's *contents*
  may still be in page cache, and a power loss can land the new name on top of
  empty blocks. Step 5 is what makes the rename itself survive.
- **Own schema number**, starting at 1. It has nothing to do with upstream
  `SCHEMA_VERSION` and never will. (The layer-2 work already established this
  rule; the same reasoning applies.)
- **Defensive read**: size cap before parsing, unknown fields ignored, a
  malformed file is *quarantined* (renamed to `state.json.corrupt.<ts>`) and
  treated as "no previous state" — never silently overwritten, never fatal.
- **Bounded history**: a fixed number of entries. A state file that grows
  without bound is a slow-motion disk failure.
- **No secrets.** Task ids, exit codes, durations, timestamps, and a short
  bounded summary of what a task observed. Not command output, not
  environment, not paths outside `~/.hermes`.

### 3.3 The state directory must be on a local filesystem

Everything proven about the lock and the state file assumes local Linux
filesystem semantics:

- `flock` behaviour across processes, and its release when a descriptor closes;
- `rename` being atomic within the directory;
- `fsync` on a file and on a directory meaning what it says.

**On NFS or CIFS, none of these can be assumed.** `flock` may be emulated,
silently local to one client, or mapped onto POSIX locks with different
semantics; `fsync` durability depends on the server and the mount options.

So: `HERMES_HOME` -- and therefore `~/.hermes/hermes-maint/` -- lives on a
local filesystem. If that ever stops being true, the guarantees in this
document have to be re-derived rather than assumed, and the single-instance
lock is the first thing that would need rethinking.

---

## 4. Lock

```
flock(~/.hermes/hermes-maint/lock, LOCK_EX | LOCK_NB)
```

Held for the entire run, released by the kernel on exit — including on
`SIGKILL`, on a panic, and on OOM. This is the whole reason for choosing
`flock` over a PID file: **there is no stale lock**. A PID file after a hard
kill requires liveness heuristics, and liveness heuristics are where
single-instance logic goes wrong.

The file's *contents* (pid, start time, trigger) are written for humans reading
a diagnostic, and are never consulted to decide whether the lock is held.

**Lock busy is not an error.** Another run is already doing the work; that is
the lock functioning. Exit 3, log one line, do not alarm.

This has to be declared to systemd as well, or the exit code is only half the
decision: the unit sets **`SuccessExitStatus=3`**. Without it, every contended
run would leave a failed unit behind, and `systemctl --failed` — the one place
a human looks to find out whether anything is wrong — would fill up with
evidence that everything is working. A monitoring surface that cries wolf gets
ignored, and then it is worth nothing on the night it is right.

`flock` is advisory and per-file-description, so the rule is absolute: **every**
entry point — timer, manual, dry-run — takes the lock. Dry-run included, because
a dry-run that reads state while a real run rewrites it reports fiction.

---

## 5. Child process lifecycle

The children are existing shell scripts. The parent's job is to run them
without becoming a new attack surface and without ever losing track of them.

### 5.1 Spawning

- **No shell.** Direct `exec`-style spawn with an argv vector. No string is
  ever handed to `/bin/sh`.
- **Absolute paths only**, from a compiled-in registry.
- **Sanitised environment**: cleared, then a minimal explicit set (`PATH`,
  `HOME`, `HERMES_HOME`, `LANG`, and `DRY_RUN` when applicable). The parent's
  environment is not inherited wholesale.
- **Own process group** (`setsid`), so a timeout can signal the whole tree. A
  shell script spawns children; killing only the shell orphans them, and
  orphaned children of a backup script are exactly the processes that corrupt
  a backup.
- **stdin from `/dev/null`.** A maintenance task must never be able to block on
  a prompt at 03:00.
- **stdout/stderr captured**, bounded, written to the run report. A task that
  emits a gigabyte does not get to fill the disk.

### 5.2 Pre-flight validation, per task, before spawning

Fail-closed. If any of these does not hold, the task is skipped and recorded as
`skipped`, and the run continues:

1. The path exists and is a **regular file** (not a symlink to elsewhere, not a
   directory, not a device).
2. It is **owned by the invoking user**.
3. It is **not group- or world-writable**. A world-writable maintenance script
   is a root-equivalent hole the moment anything privileged touches it.
4. It is executable.

### 5.3 Sequencing

Tasks run **sequentially** in the first slice. Concurrency buys nothing here —
the box has 6 GB and the tasks are I/O-bound against the same disk — and it
would make the timeout, the report ordering and the failure semantics harder to
reason about for zero benefit.

---

## 6. Timeouts

Two layers, and they are not redundant.

| Layer | Value | Purpose |
|---|---|---|
| **Per task**, enforced in Rust | per-task, from the registry | kill one wedged task, **keep the run and the report** |
| **Whole run**, `TimeoutStartSec` in the unit | generous, > sum of task budgets | last resort if the parent itself wedges |

The per-task timeout is the one that matters. Relying on systemd alone would
kill the parent too, losing the report — and the report is the product of this
slice.

### 6.1 Escalation

```
deadline reached
  → SIGTERM to the process group
  → grace period
  → SIGKILL to the process group
  → record outcome=timeout, exit=5, continue to the next task
```

A timed-out task does not abort the run. The other tasks are independent and
their observations are still wanted.

---

## 7. Health model

**Observe and report. Nothing else.**

This is the constraint the user set, and it is written into the design rather
than left to discipline: the first slice contains **no code path that stops,
starts, restarts or kills any service.** Not the gateway, not Onion, not
KoboldCpp. The only processes it signals are its own children, and only to
enforce their timeouts.

Checks are read-only:

- gateway unit `ActiveState` / `NRestarts` / `MainPID` — via `systemctl show`,
  parsed, never acted on
- disk free under `~/.hermes`
- age and size of the most recent backup
- presence and age of expected artifacts
- last-run outcome from `state.json`

Each yields `ok` / `degraded` / `unknown`. `unknown` is a first-class result —
"the check could not run" is different from "the thing is broken", and
collapsing them is how monitoring starts lying.

A degraded health check makes the run report it and exit 6. It does not
"remediate". Remediation is a later slice with its own design and its own
argument, and it does not get to sneak in as a convenience.

---

## 8. Failure and recovery model

Every scenario requested, with the decided behaviour.

| Scenario | Behaviour |
|---|---|
| **Normal run** | Lock acquired → tasks run in order → report written → `state.json` replaced atomically → exit 0. Journal carries one invocation with duration and exit code. |
| **A child fails** (non-zero exit) | Recorded as `failed` with its exit code and captured output. **The run continues** to the remaining tasks. Run outcome becomes `partial`, exit 4. Independent observations are not lost because one of them failed. |
| **Timeout** | SIGTERM → grace → SIGKILL to the process group. Task recorded `timeout`, exit 5. Run continues. Nothing is left running. |
| **The Rust process dies** (crash, OOM, SIGKILL) | Kernel releases the flock; no stale lock. `state.json` is either the previous run's or the new one's, never half-written. The next timer firing starts clean. Partial work done by children is whatever those scripts' own idempotence guarantees — which is why they, not the parent, own that property. |
| **Reboot mid-task** | Same as above, plus: `Persistent=true` makes the timer fire shortly after boot if the window was missed. On start, if `state.json` has a `started_at` with no `finished_at`, the previous run is recorded as `interrupted` and the new run proceeds. It does not attempt to resume — resuming a half-finished backup is worse than starting one. |
| **Lock busy** | Exit 3, one log line, no report, no state change, no alert. The unit declares `SuccessExitStatus=3`, so systemd records it as a **normal termination**: a correctly working lock must never appear in `systemctl --failed`. |
| **Script missing** | Caught by pre-flight §5.2. Task recorded `skipped` with a reason. The run continues and exits 4 (partial). A missing maintenance script is a real problem that must be visible — but it is not a reason to skip the other tasks. |
| **Insufficient permissions** | Same path: pre-flight fails, task `skipped` with the specific reason (not owned / writable by others / not executable). Never "retry with sudo". There is no sudo path in this design (§10). |

### 8.1 Exit codes

```
0  ok
1  internal error (a bug in hermes-maint itself)
2  misuse — bad arguments or unreadable configuration
3  lock busy — another run holds it (not a failure)
4  partial — at least one task failed or was skipped
5  timeout — at least one task was killed on its deadline
6  degraded — all tasks ran, a health check reports degraded
7  incompatible-state — the state file was written by a newer build
```

Distinguishable codes, so the journal is queryable without parsing prose.

**7 is separate from 2 on purpose.** "You ran an old binary against newer
state" and "you mistyped `--trigger`" are different problems with different
fixes, and collapsing them into one code would waste exactly the property
these codes exist for.

---

## 9. systemd integration

Two files, **user** units — matching `hermes-gateway.service`, which is already
a user unit, and requiring no root anywhere.

```
~/.config/systemd/user/hermes-maint.timer
~/.config/systemd/user/hermes-maint.service
```

Timer:

```ini
[Timer]
OnCalendar=*-*-* 03:00:00
RandomizedDelaySec=300
Persistent=true
AccuracySec=1m
```

**Missed-run policy, stated explicitly:** if the 03:00 run is missed because the
machine was powered off, `Persistent=true` makes systemd run it **at the next
boot**, as soon as the timer unit starts. For this slice that is the desired
behaviour, not a side effect — the tasks are observations and a late observation
is still worth having. `RandomizedDelaySec` applies to the catch-up run too, so
it does not land in the middle of the boot storm.

This is a policy that a later slice may need to revisit: a task that is only
meaningful inside its window (or one that is expensive enough to hurt a machine
that has just booted) would want `Persistent=false` or its own freshness check.
No such task exists yet.

`Persistent=true` covers the machine being off at 03:00. `RandomizedDelaySec`
is cheap insurance against every scheduled thing on the box starting at
exactly the same second. `sleep.target` is masked on this host, so no
`WakeSystem=` is needed — noted so that a future host where it *is* needed does
not inherit a silent assumption.

Service:

```ini
[Service]
Type=oneshot
Environment=HERMES_HOME=%h/.hermes
ExecStart=%h/.local/bin/hermes-maint run --trigger timer
SuccessExitStatus=3
TimeoutStartSec=300
```

**`--trigger timer`, not the default.** The CLI defaults to `manual`, so a
service without the flag would record every scheduled run as if a person had
typed it. The service *is* the timer's entrypoint, and starting it by hand is a
rehearsal of that — so its runs are `trigger=timer` either way. A person
running the tool directly uses `hermes-maint run --trigger manual`. One unit,
two triggers, told apart in the state file rather than by a second unit.

`Type=oneshot` is the honest type: systemd waits for completion and records the
result, and the unit is correctly `inactive (dead)` between runs rather than
pretending to be a service that is down.

> Lesson already paid for in this project: **no trailing comments on directive
> lines.** A `Directive=value  # why` silently voids the directive. Comments go
> on their own line above.

---

## 10. Permissions

- Runs as **mauricio**, as a **user** unit. Not a system unit.
- **No `sudo` anywhere.** Not in the binary, not in a task, not as a fallback.
- **No capabilities**, no setuid, no root-owned files.
- Consequence, stated rather than discovered later: anything needing root —
  `apt`, system units, `/etc` — **cannot be done by this slice.** It can only
  *report* that such work appears to be pending. Wiring an unattended root path
  into a 03:00 job is the kind of decision that has to be made deliberately,
  in daylight, with its own threat model.

### 10.1 Hardening

On the unit, with the caveat above about comments:

```
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
ReadWritePaths=%h/.hermes
MemoryMax=...
```

Every one of these must be validated with `systemd-analyze verify` and the
resulting exposure score recorded — because in this project a directive set
that *looked* right scored as if it were absent, and only the verifier caught
it. `ProtectHome` is not usable: the work is in `$HOME` by definition.

---

## 11. Threat model (minimal, honest)

The adversary here is not a remote attacker. There is no listening socket, no
network call, no parsing of anything an outsider controls. The realistic
threats are local and mostly self-inflicted.

| Threat | Mitigation |
|---|---|
| A tampered or world-writable script in `~/.hermes/scripts` runs unattended | Pre-flight ownership and permission checks (§5.2), fail-closed |
| Argument or shell injection into a child | No shell, argv vector only, absolute paths from a compiled-in registry |
| **Configuration as a command channel** | Configuration may **enable/disable** a task and set its timeout. It can **never introduce a command string or a path.** The registry is compiled in. This closes the whole class rather than filtering it. |
| Corrupted or hostile `state.json` | Size cap, defensive parse, quarantine-don't-overwrite |
| Log injection / spoofed report lines | Child output is captured as bounded, clearly delimited data — never interpolated into structured log fields |
| Secrets leaking into reports or state | Sanitised child environment; reports record ids, codes and durations. `.env`, tokens and credentials are never read by this binary and never appear in its output |
| Disk exhaustion via runaway output or history | Bounded capture, bounded history, rotated reports |
| Privilege escalation | There is no privileged path to escalate along (§10) |

**Out of the threat model**, deliberately: an attacker who already has the
user's account. At that point they can edit the units, the scripts and the
binary. Defending against that from inside a user-level maintenance tool is
theatre.

---

## 12. Explicit limits

### 12.1 Not in this slice

```
PyO3              Telegram         Discord          gateway
Onion             agents           skills           Pi
command ranking   ESP32            ingress          LLM calls
memory            busy_input_mode  state.db ownership
```

No FFI. No Python interop of any kind. The Rust binary and the Python gateway
communicate through **nothing** in this slice — not a socket, not a database,
not a shared file. They are strangers that happen to live on the same machine.

### 12.2 The ~400 MB is not a success criterion

Stated for the third time in this project, because it is the easiest thing to
drift back toward:

```
chat ingress
     ↓
gateway Python        ← stays resident. Nothing here changes that.
```

While chat ingress arrives through the Python gateway, nothing else can wake
it, and the gateway's memory is the cost of the product working. **This slice
does not reduce RAM and must not be judged on whether it does.**

### 12.3 Success criteria that *do* apply

1. It runs at 03:00 without supervision and can be proven to have run.
2. Two simultaneous invocations never both execute.
3. A wedged task cannot hang the run, and leaves nothing running.
4. A crash or reboot at any point leaves no stale lock and no corrupt state.
5. Every run's outcome is queryable afterwards without guesswork.
6. It never stops, starts or restarts a service.
7. `--dry-run` performs no writes and no child execution, and says exactly what
   a real run would do.
8. Uninstall leaves nothing behind (§13), verifiably.

### 12.4 Deliberately absent

- No network. No listening socket, no outbound request, no telemetry.
- No notifications. Not Telegram, not email. Reporting means the journal, the
  report file and the exit code. Delivery is a later, separate decision.
- No auto-remediation. See §7.
- No self-update. A maintenance tool that updates itself at 03:00 unattended is
  how you lose the tool and the maintenance in one night.

---

## 13. Uninstall

Must be complete, and must be verifiable — a tool that cannot be fully removed
should not be installed on a machine that was, recently, unrecoverable.

```bash
systemctl --user disable --now hermes-maint.timer
rm ~/.config/systemd/user/hermes-maint.timer
rm ~/.config/systemd/user/hermes-maint.service
systemctl --user daemon-reload
rm ~/.local/bin/hermes-maint
rm -rf ~/.hermes/hermes-maint/          # state, lock, reports
```

Properties that make this true, and which constrain the implementation:

- **Everything lives in four known places.** Binary, two units, one state
  directory. Nothing in `/etc`, nothing root-owned, no dotfile edits, no
  crontab entry, no shell profile hook.
- **Nothing is running** to be stopped beyond the timer.
- **No foreign state is mutated.** `state.db` is never opened, the gateway is
  never touched, existing scripts are never modified. Removing `hermes-maint`
  returns the system exactly to its current state.
- Verification is a listing, not a belief: after the steps above,
  `systemctl --user list-timers` shows no `hermes-maint`, and
  `find ~ -name 'hermes-maint*'` is empty.

---

## 13.1 Order of the first tasks

Not arbitrary. Each step is chosen to introduce **one** new thing.

| # | Task | What it introduces | Why here |
|---|---|---|---|
| 1 | **`disk-space`** | the task registry, `Observation`, `TaskResult`, run outcome, exit 0/6 | Read-only, knows nothing about Hermes, no database, no privileges, no network, and essentially no destructive way to fail. It reads `statvfs(3)` directly rather than running `df`, so the first task introduces **no subprocess at all**. |
| 2 | **`backup-freshness`** *(done — see [its own document](task-backup-freshness.md))* | judgement about someone else's artifacts | Deliberately second, because it forces decisions `disk-space` never needs: which directory is authoritative, what counts as a valid backup, whether the date comes from the name or the metadata, how a complete backup is told apart from a half-written one, and what "too old" means. And it must rest on **evidence of a verified backup**, not on `mtime` — this project has already paid for the difference between "a recent backup exists" and "a restorable backup exists". |
| 3 | **child-process supervisor** *(done — see [its own document](child-supervisor.md))* | spawning, process groups, `SIGTERM`/`SIGKILL` escalation, deadlines | Its own slice, exercised against a deliberately harmless external command. Bundling it with a first task would mean debugging arithmetic and signal escalation in the same commit. |
| 3.5 | **the external-task translation** *(done — see [its own document](external-tasks.md))* | `ChildResult` → `TaskResult`, outcome precedence, what reaches disk | Proven against a fixture, with no probe left in the registry. |
| 4 | **`gateway-service-health`** *(done — see [its own document](task-gateway-service-health.md))* | the first external program against the real system | `systemctl --user show`: it queries and prints. Its whole point is that the exit code is not the answer — `systemctl` exits 0 while reporting a missing unit. |
| 5 | **a task that runs an existing maintenance script** | the real thing | Not yet. |

## 14. What happens next

In order, and not before the boundary above is accepted:

1. Install a Rust toolchain (there is none on this host yet).
2. `cargo new` — a workspace with `hermes-maint-core` and `hermes-maint`.
3. Lock, state and dry-run first, with tests, and **no tasks at all**. *(done
   — `v0.16.0-hermes-maint-slice1`)*
4. One real task, chosen for being read-only and boring: `disk-space`. *(done)*
5. `backup-freshness`, on evidence rather than `mtime`. *(done —
   `v0.18.0-hermes-maint-backup-freshness`)*
6. The child-process supervisor, in its own slice. *(done —
   `v0.19.0-hermes-maint-supervisor`)*
6b. The translation from a supervised child to a task result, against a
   fixture. *(done — `v0.20.0-hermes-maint-external-tasks`)*
6c. The first real external observation, read-only. *(done —
   `v0.21.0-hermes-maint-gateway-health`)*
6d. Install the binary and the service, run it by hand. *(done —
   `v0.22.0-hermes-maint-systemd-manual`; see
   [systemd-integration.md](systemd-integration.md))*
7. The units, verified with `systemd-analyze --user verify`. *(done)*
8. Run it by hand, repeatedly, before letting the timer own it. *(done)*
9. Only then, 03:00. **Not yet: no timer exists.**

The first commit was able to do nothing useful and still be correct. That was
the point.
