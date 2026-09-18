# Status

**Read this block before anything else.**

```
Rust implemented        YES        hermes-maint, v0.23.0
hermesd resident        NO         and not planned; see below
systemd timer           ACTIVE     daily, 03:00 (+0–300s), user unit
tasks                   disk-space · backup-freshness · gateway-service-health
auto-remediation        NO         every task observes and reports, nothing acts
Onion integrated        NO         zero Rust touches the router
Layer 2 in production   NO         still on branch feat/layer2-schema
```

Last updated 2026-09-18, after the timer's first natural firing.

## What runs unattended today

One thing: `hermes-maint`, once a day, from a systemd **user** timer.

```
03:00 (+ up to 300s of randomised delay)
  → hermes-maint.service
  → hermes-maint run --trigger timer
  → three read-only observations
  → ~/.hermes/hermes-maint/state.json
```

First natural firing observed: **2026-09-18 03:01:10**, `InvocationID
f306c907e2ab46f48f9b30dcfa888c81`, run 11, `trigger=timer`, all three `Ok`,
`Result=success`.

It has **no capacity to act**. It cannot start, stop or restart a service,
cannot run a backup, cannot update anything and has no network. Giving it any
of that is a separate decision that has not been taken.

### The distance from the original goal

This project began with *"un sistema completo autoactualizado… cronjobs de las
12 am… backup en caso de falla"*. What exists at 03:00 is three observations.
**The update orchestrator does not exist**, and nothing automatic runs a
backup. That was deliberate — observe before acting — but the gap is real and
should not be mistaken for an oversight.

## `hermesd` is not being built

The name is reserved. The binary is `hermes-maint` and it is **not resident**:
`systemd.timer` owns the schedule, which is why there is no scheduler in the
code. The comparison that decided it, and the three conditions that would
reverse it, are in [`hermesd-design.md`](hermesd-design.md) §2.

## What was built before Rust

Nothing below has changed; it is the ground the Rust work stands on.

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

## Security posture — stated precisely

Vague wording here is worse than none, so the two axes are separated.

**HEAD of both public repositories:** no credentials, no private or VPN
addresses, no personal home paths, no non-noreply e-mail, no wireless
credentials.

**History of both public repositories:** the same, **except** three
low-sensitivity identifiers — a laptop brand, a CPU model and a first name —
which remain in three commits of the router repository and are documented
below. They are not credentials and grant no access.

**The two VPN addresses found during this pass were never in either public
repository.** They lived in a private configuration repository that has no
remote and was never published; its HEAD is now parameterised too. An earlier
summary conflated the two, which is the kind of ambiguity that makes a clean
report useless.

### Known limits of the scanning

The scanner looks for credentials, not topology. Both of those addresses were
found by a manual grep, not by the tool. An `--internal` mode for private and
VPN ranges, hostnames and network blocks would close that gap and does not
exist yet.

No `pip-audit` or `osv-scanner` was available, so the dependency review was
manual: advisories were checked by reading, and reachability was confirmed by
inspecting which APIs the code actually calls. That is weaker than a scanner
against a live advisory database.

## Backlog carried into the next phase

| Id | Item | Blocking? |
|---|---|---|
| `SEC-DEPENDENCIES-001` | Run a real dependency audit (`pip-audit` / `osv-scanner`) against both repositories. The review so far was manual. | No |
| `SEC-SCANNER-002` | Add an `--internal` mode for private/VPN addresses, hostnames and network blocks. | No |
| `SEC-SCANNER-003` | `scan-secrets.sh --tree-only` scans `git ls-tree HEAD`, **not** the working tree, despite its name. Running it before committing therefore scans the *previous* commit — which is how a finding in `v0.20.0` went unreported until the slice after. The correct order until it is fixed is commit → scan HEAD → scan history. | No |
| `SEC-SANDBOX-004` | Reconcile systemd's filesystem sandboxing with the supervisor's pre-flight ownership rule. Any of `ProtectSystem`, `PrivateTmp`, `ProtectKernelTunables`, `ProtectControlGroups`, `ProtectHome`, `ReadWritePaths` or `PrivateNetwork` makes an unprivileged user manager create a user namespace mapping only our uid, so root-owned `/usr/bin/systemctl` appears owned by 65534 and the pre-flight correctly refuses it. Measured, not assumed — see [`systemd-integration.md`](systemd-integration.md) §5.3. Resolving it means teaching the pre-flight about namespace mapping, or accepting an unmapped owner when the file is unwritable. **The pre-flight is not to be weakened to win a hardening score.** | No |
| `SEC-SYSCALL-005` | `SystemCallFilter=@system-service` was never evaluated. It is the next meaningful hardening gain for the service. | No |
| `BACKUP-DECL-006` | `backup-hermes-home.sh` declares 6 databases and 5 files in `scripts/`; the installation now has **8 and 8**. `cron/deliveries.db` and `shared-state.db` arrived with Hermes 0.21.3 and are captured by nothing. The invariant fails the backup loudly rather than shipping an incomplete one, which is correct — but **no verified backup can be produced until the declarations are updated consciously**. | **Yes — blocks a fresh verified backup** |
| `PI-FUNCTIONAL-001` | Exercise the stdin prompt change and the guardrails check against a live Pi. Both were verified structurally; neither has run end to end. Onion is currently `inactive (dead)` and the two fixes live uncommitted in the working tree of `~/.hermes/../hermes-agent`. | No |
| `ONION-DECIDE-007` | `hermes-agent.service` is `enabled` but `dead`. Either it comes back or its unit should be disabled, so a reboot does not revive it unexpectedly. Nothing observes it today — `gateway-service-health` watches the Hermes gateway, a different unit. | No |
| `FOSSIL-RETIRE-008` | `~/.hermes/hermes-agent`, the pre-cutover checkout, still occupies **3.8 GB**. Retire only after auditing what is unique versus reproducible (venv, caches, builds), and only once Layer 2 has been stable for a while. | No |

---

# What happened to "hermesd"

**It was not built, and that was the right call.** The boundary agreed in
advance described a resident daemon owning the 03:00 run. Comparing it against
a `systemd.timer` waking a short-lived binary — on RAM, complexity, reboot
recovery, observability, reliability, uninstall and systemd fit — the timer won
on every axis except future event-driven work, which does not exist yet.

So what shipped is `hermes-maint`: single-instance lock, own state, child
supervision, timeouts, exit codes, dry-run, and **no scheduler**, because
systemd already has one. The full comparison and the three conditions that
would reverse it are in [`hermesd-design.md`](hermesd-design.md) §2.

Delivered across `v0.16.0` … `v0.23.0`:

| | |
|---|---|
| `flock` single-instance | released by the kernel even on `SIGKILL`; proven by forking, handshaking on a pipe, and killing the holder |
| state v1 | temp-in-same-dir → fsync → rename → fsync the directory; `0600` under `0700` |
| child supervisor | own process group, pipes drained concurrently, `SIGTERM` → grace → `SIGKILL`, always reaped |
| three observations | disk, backup evidence, gateway unit |
| systemd | service and timer, hardening applied one directive at a time and measured |

Not in any of it: PyO3, the gateway, Telegram, the router, agents, skills,
command ranking, ESP32, any new ingress path — and no capacity to act.

## The saving that is not yet available

A resident daemon does **not** yet let the ~400 MB Python gateway sleep. While
chat ingress arrives through that gateway, nothing else can wake it. That
saving belongs to a later phase, after deciding who receives ingress while
Python is asleep — and the options there (the daemon itself, a thin webhook, a
separate ingress process, or simply accepting a resident gateway) have not been
weighed yet.

Do not claim the number before that question is answered.
