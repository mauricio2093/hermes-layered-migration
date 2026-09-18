# The timer

```
systemd --user → hermes-maint.timer → hermes-maint.service
               → hermes-maint run --trigger timer
               → disk-space · backup-freshness · gateway-service-health
```

No new tasks, no changed semantics, no remediation. The only thing added is a
schedule.

---

## 1. `Persistent=true`, measured rather than read

Four cases, tested on this host with disposable fixture units before the real
timer existed. systemd 259.5.

| case | setup | result |
|---|---|---|
| **A** | fresh timer, calendar point in the future | no run; `NEXT` set correctly |
| **B** | stamp **older** than the last elapse (run missed while inactive) | **catch-up fires immediately** on activation |
| **C** | fresh timer, no stamp, calendar point **already passed today** | **no catch-up**; schedules the next occurrence |
| **D** | stamp **newer** than the last elapse | no re-run |

The rule underneath all four: `Persistent=true` compares a **stamp file**
against the last calendar elapse.

```
~/.local/share/systemd/timers/stamp-hermes-maint.timer
```

And the detail that explains case C: **systemd writes that stamp when the
timer unit is activated**, not only when the service fires. A freshly enabled
timer therefore stamps "now" and has no missed window to catch up from.

That mattered here. Before running `enable --now` the prediction was *no
catch-up*, for two independent reasons — no stamp existed, and 03:00 was still
an hour away. `state.json` was byte-identical afterwards, so nothing fired.

## 2. The unit

```ini
[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
RandomizedDelaySec=300
AccuracySec=1m
```

**`Unit=` is deliberately absent.** The timer and the service share a stem, so
systemd derives it. Declaring it would create a second source of truth that
can silently disagree with the filename.

**No `OnBootSec`, `OnStartupSec` or `OnUnitActiveSec`.** `Persistent=true`
already expresses the whole catch-up policy; a second trigger would make "when
does this run" a question with more than one answer.

**No `FixedRandomDelay`.** It exists in this version, and it would pin the
offset to a stable per-machine value. Nothing coordinates with this timer, so
there is no reason to want the same offset every day. The unit stays small.

**No `WakeSystem`.** This is a user unit, and on this host `sleep.target`,
`suspend.target` and `hibernate.target` are all masked — the machine does not
suspend. **This timer promises nothing about waking a host with a different
power policy**; such a host would need its own decision.

## 3. The real window

`RandomizedDelaySec=300` means the run lands somewhere in a five-minute window
after the calendar point, and `AccuracySec=1m` lets systemd shift it a little
further to coalesce with other timers.

```
nominal   03:00:00
window    03:00:00 – 03:05:00, plus up to a minute of coalescing
observed  NEXT = 03:01:56   (a 116-second draw)
```

So **"03:00" is a label, not a promise**, and no test should demand an exact
second. The delay is there so that everything scheduled on this machine does
not start at the same instant.

## 4. State after enabling

```
$ systemctl --user list-timers hermes-maint.timer
NEXT                           LEFT     LAST PASSED UNIT               ACTIVATES
Fri 2026-09-18 03:01:56 -05    1h 8min  -    -      hermes-maint.timer hermes-maint.service
```

```
UnitFileState=enabled
ActiveState=active
SubState=waiting
Persistent=yes
RandomizedDelayUSec=5min
AccuracyUSec=1min
```

`LAST` and `PASSED` are empty because it has not fired yet. That is the honest
state, and it is why the next section exists.

## 5. Installed and verified ≠ first 03:00 observed

These are different facts and are reported as different facts.

```
timer installed, enabled, verified, NEXT correct     yes
activation chain timer → service → entrypoint        yes, proven (below)
first natural 03:00 run observed                     NOT YET
```

The session ran at 01:53; the first real firing is at 03:01:56, over an hour
away. Waiting was not reasonable, and the calendar was **not** moved to make
the test convenient. `03:00 proven` will be true after it happens, and not
before.

### 5.1 What *was* proven

Since there was no catch-up, the activation chain was demonstrated with a
parallel fixture: a copy of the production service differing only in
`HERMES_HOME`, driven by a 20-second timer.

```
01:54:15  Started hm-timer-proof.timer
01:54:20  Starting hm-timer-proof.service - Hermes scheduled maintenance observer...
01:54:21  info: run 1 open (trigger=timer)
01:54:21  info: disk-space: 2.0 GiB free of 3.1 GiB usable (65.5%)
01:54:21  info: backup-freshness: newest verified backup is 6s old (1 verified backup)
01:54:21  info: gateway-service-health: hermes-gateway.service active (running)
01:54:21  info: run 1 closed as Ok in 0s
01:54:21  Finished hm-timer-proof.service
```

`InvocationID=1cd6f921726841b193d827de47c27c80`, `Result=success`, and a run in
`state.json` with `trigger=timer`. The fixture, its stamp and its state were
then removed completely.

The production timer keeps `03:00` and nothing else.

## 6. A failing service does not stop its timer

Tested with a fixture whose service always fails, fired every 15 seconds:

```
service       4 consecutive failures, present in `systemctl --failed`
timer         active, NEXT still scheduled, kept firing
```

This matters because `hermes-maint.service` is **supposed** to fail on a
finding:

```
0 success   3 success (contended lock)
4 failed    5 failed    6 failed    7 failed
```

If `backup-freshness` crosses its threshold, the service goes red and the timer
carries on. Nothing is painted green to keep a dashboard quiet, and no
`Restart=` or `OnFailure=` was added — no retries, no notifications, not in
this slice.

## 7. `backup-freshness` right now

Reported, not fixed. No backup was run.

```
newest verified backup   2026-09-16 18:22:49 -05
age now                  31.5 h            → Ok
crosses 48 h             2026-09-18 18:22:49 -05
age at the next 03:00    32.6 h            → Ok
age at the 03:00 after   56.6 h            → Degraded, service exits 6
```

So the first scheduled run should be green, and the one after it should not be
— unless a backup happens in between. That is the observation working.

## 8. Linger and power

```
Linger=yes            unchanged; a user manager exists at 03:00 with no session
sleep.target          masked
suspend.target        masked
hibernate.target      masked
```

Linger was re-confirmed, not changed. Without it, a user timer on a machine
with nobody logged in would have no manager to fire it.

## 9. Uninstall — the stamp counts

`Persistent=true` leaves state outside the unit file, so removing the `.timer`
is no longer enough. Rehearsed end to end under parallel names, with the
production installation untouched:

```bash
systemctl --user disable --now hermes-maint.timer
systemctl --user clean --what=state hermes-maint.timer   # removes the stamp
rm ~/.config/systemd/user/hermes-maint.timer
rm ~/.config/systemd/user/hermes-maint.service
systemctl --user daemon-reload
systemctl --user reset-failed hermes-maint.service hermes-maint.timer
rm ~/.local/bin/hermes-maint
rm -rf ~/.hermes/hermes-maint
```

Verified afterwards that none of these survive: the binary, the two unit files,
the `timers.target.wants` symlink, **the persistent stamp**, the state
directory, anything loaded in systemd, any timer, any process.

`clean --what=state` was confirmed to delete
`~/.local/share/systemd/timers/stamp-*` — checked before and after.

## 10. Hardening is not reopened

The service's directive set stays exactly as `v0.22.0` left it. The evidence
for why `ProtectSystem`, `PrivateTmp`, `ProtectKernelTunables`,
`ProtectControlGroups`, `ProtectHome`, `ReadWritePaths`, `PrivateNetwork` and
`ProtectKernelModules` are absent is in
[`systemd-integration.md`](systemd-integration.md) §5. The supervisor's
pre-flight ownership rule is likewise untouched.

Reconciling filesystem sandboxing with executing a root-owned tool is a real
question and a separate backlog item. It is not this slice.

## 11. What changed in production

```
new   ~/.config/systemd/user/hermes-maint.timer                  0644
new   ~/.config/systemd/user/timers.target.wants/hermes-maint.timer   symlink
new   ~/.local/share/systemd/timers/stamp-hermes-maint.timer     0 bytes
      systemctl --user daemon-reload
```

Everything else is byte-identical to the baseline taken before enabling: the
gateway's `ActiveState`, `SubState`, `MainPID` and `NRestarts`, both backup
`state.json` hashes, `hermes-maint`'s own `state.json`, and the installed
binary's hash.

Enabling the timer executed nothing.
