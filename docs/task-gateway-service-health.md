# Task: `gateway-service-health`

The first task that runs an external program against the real system, and
still strictly read-only: one `systemctl --user show`, which queries and
prints.

```
gateway-service-health → ExternalTask → supervise() → systemctl --user show
                       → parse → judge_service_state → Ok | Degraded | Failed
```

---

## 1. What was discovered, not assumed

Read off the running machine before any code was written:

| | |
|---|---|
| unit | **`hermes-gateway.service`** |
| scope | **user** unit (`systemctl --user`), `enabled` |
| fragment | `~/.config/systemd/user/hermes-gateway.service` |
| owns the gateway | its `MainPID` is the `hermes_cli.main gateway run` process |
| healthy state | `LoadState=loaded` · `ActiveState=active` · `SubState=running` |
| `systemctl` | `/usr/bin/systemctl` — regular file, not a symlink, `root:root`, mode `755` |

The binary passes the supervisor's pre-flight unchanged, including the rule
that accepts a **root-owned** program this user cannot write — the case that
rule was written for.

There is also a *system* unit called `hermes-agent.service` (the Onion router).
It is a different thing and is not what this task observes.

No unit was modified.

## 2. Why the exit code is not the answer

This is the reason the task exists. Verified on the host:

```
$ systemctl --user show no-such-unit.service --property=LoadState ...
LoadState=not-found
ActiveState=inactive
SubState=dead
$ echo $?
0
```

`systemctl` succeeded. It did exactly what it was asked. The **news** is bad.

A task that read the exit status alone would report a missing gateway as
perfectly healthy — which is precisely the failure this whole design exists to
avoid. So the process working and the service being healthy are two different
questions, answered in two different places: the supervisor answers the first,
`judge_service_state` answers the second.

## 3. The command

```
/usr/bin/systemctl --user show hermes-gateway.service \
    --property=LoadState --property=ActiveState --property=SubState --no-pager
```

Absolute path, compiled in. The unit name is a compiled-in constant, not a
configuration value. `show` is the only subcommand, and a test asserts that the
argument list contains none of `start`, `stop`, `restart`, `reload`, `enable`,
`disable`, `mask`, `kill`, `daemon-reload`, `set-property` or `edit`.

### 3.1 One environment variable, derived rather than inherited

The supervisor gives a child nothing but `PATH`, `LC_ALL`, `LANG`, `TZ` and
`NO_COLOR`. With exactly that, `systemctl --user` fails:

```
Failed to connect to user scope bus via local transport:
$DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined
```

Verified that `XDG_RUNTIME_DIR` alone is enough. It is added to the spec as the
one variable this child needs — and it is **computed from the effective uid**
(`/run/user/<euid>`), not copied from our own environment. Under a timer ours
may be absent; a stale or foreign value would be worse than no value. Deriving
it means the child depends on nothing the parent happened to be started with.

This is the extension point the supervisor's design anticipated: *"a child that
needs one of those should be given it deliberately, one variable at a time,
with a reason."*

## 4. The verdict

**Healthy is exactly one triple** — the one observed on a working host:

```
loaded / active / running   →  Ok
everything else             →  Degraded
```

The asymmetry is deliberate. A state nobody has seen before is not evidence of
health, and defaulting an unknown value to `Ok` would let a future systemd
substate turn a broken gateway into a green report. The cost of being wrong the
other way is one line in a log.

Recognised cases get a better sentence — *the unit does not exist*, *the unit
is masked*, *the service has failed (failed)*, *the service is not running
(dead)*, *the service is in transition (activating/start)*, *active but in an
unexpected substate (exited)* — but every one of them is still `Degraded`.

### 4.1 A missing unit is `Degraded`, not `Skipped`

`LoadState=not-found` is a finding, not an absent precondition. This host is
supposed to run that gateway; discovering that it does not is exactly the
observation the task is for.

## 5. Failing to observe ≠ observing a failure

```
Failed   could not run systemctl · could not reach the user manager
         (systemctl exits non-zero) · output that cannot be interpreted ·
         spawn failure · killed by a signal
Timeout  systemctl itself hung
```

This task's interpreter diverges from the generic one in two places, on
purpose:

| child | generic | here | why |
|---|---|---|---|
| exited 0 | `Ok` | **parse and judge** | §2 |
| refused by pre-flight | `Skipped` | **`Failed`** | this host demonstrably runs systemd — it is how the gateway runs — so being unable to execute `systemctl` is a broken observation, not an absent precondition |

A timeout stays `Timeout` rather than collapsing into `Failed`: `systemctl show`
hanging is its own symptom, and the run's exit code should say so.

## 6. The parser

Tolerant about **shape**, strict about **content**.

Ignored: any line that is not `KEY=VALUE`, and any property that was not asked
for — systemd may grow new ones and that must not break anything. Field order
does not matter; a trailing newline is optional; CRLF is handled.

Refused: a required property **absent** (`ParseError::Missing`), or present
**twice with different values** (`ParseError::Contradictory`). `systemctl` does
not contradict itself; something that does is not `systemctl`, so nothing in
that output is trusted.

A required property present with an **empty** value is *not* a parse error: the
structure is intact and the content is simply a state that cannot be called
healthy, so it takes the conservative `Degraded` path.

## 7. What reaches `state.json`

The detail is built from the **parsed fields**, never from raw output:

```
gateway-service-health  ok  exit=0  signal=None  bytes=53
    hermes-gateway.service active (running)
```

An unfiltered `systemctl show` is hundreds of lines; there is no reason to
carry them into a file that keeps thirty runs. A test feeds the task 500
irrelevant properties and asserts the persisted detail stays under 120 bytes
and contains none of them, while `output_bytes` still records the size.

## 8. How the unhealthy cases are tested

**Never by stopping the gateway.** Producing a failed service in order to check
that we notice it would be a worse idea than the bug it is looking for.

- The verdict is a pure function over three strings, so `inactive`, `failed`,
  `not-found`, `activating`, `masked` and unknown states are all fixtures.
- The three global exit paths (`0`, `6`, `4`) are driven by the test fixture
  binary standing in for `systemctl` and printing whatever the test wants,
  interpreted by the **real** interpreter.
- One integration test runs the real query against the real unit, checks that
  the task's verdict matches what `systemctl` told the test directly, and
  captures `ActiveState`, `SubState`, `MainPID` and `NRestarts` before and
  after to confirm nothing moved. It skips itself, with a note, on a host with
  no usable user manager.

## 9. Manual verification

```
$ HERMES_HOME=<disposable> hermes-maint run --trigger manual
info: disk-space: 2.0 GiB free of 3.1 GiB usable (65.5%), inodes 83.0% free
info: backup-freshness: newest verified backup is 0s old (1 verified backup)
info: gateway-service-health: hermes-gateway.service active (running)
info: run 1 closed as Ok in 0s
exit=0
```

`state.json`: **1035 bytes** for the whole run.

Production before and after: `ActiveState=active`, `SubState=running`,
`MainPID=1163669`, `NRestarts=0` — identical. `~/.hermes/hermes-maint` still
does not exist; `hermes-maint` has never been run against the real
`HERMES_HOME`.

## 10. Not in this task

```
no restart   no start   no stop   no enable   no daemon-reload
no config-supplied unit name      no shell    no sudo    no network
```

It looks, and it writes down what it saw.
