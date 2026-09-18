# systemd integration — manual, no timer yet

The binary is installed and the service works. **No timer exists**, and none
will until this has been run by hand enough times to be boring. Handing a
schedule to something unproven is how a maintenance job becomes a nightly
surprise.

```
release build → ~/.local/bin/hermes-maint → hermes-maint.service
              → systemctl --user start → 3 observations → state.json
```

---

## 1. The artifact

```
built    target/release/hermes-maint   544440 bytes, stripped
sha256   53d075bd07ce63bab303ea101ee7970f5069ca074e97a8a96c2c60efa5baf05e
installed ~/.local/bin/hermes-maint    mode 0755, owner mauricio
sha256   53d075bd07ce63bab303ea101ee7970f5069ca074e97a8a96c2c60efa5baf05e
```

Installed with `install -m 0755`, which sets the mode explicitly. That matters
here: this host runs `umask 002`, so a plain `cp` would have produced `0775` —
group-writable, which is exactly what the tool's own pre-flight refuses in the
programs *it* runs.

A regular file, not a symlink. Built from the tree at
`v0.21.0-hermes-maint-gateway-health`, with no uncommitted changes.

## 2. `--trigger timer` in the unit

The CLI defaults to `manual`. A service without the flag would record every
scheduled run as though a person had typed it.

```ini
ExecStart=%h/.local/bin/hermes-maint run --trigger timer
```

The service **is** the entrypoint the timer will activate. Starting it by hand
now rehearses exactly that, so these runs are recorded as `trigger=timer` — and
that is correct, not a bug. A person running the tool directly uses
`hermes-maint run --trigger manual`, and `state.json` distinguishes the two.
One unit, no template, no second file.

## 3. No dependency on the gateway

Deliberately absent: `Wants=`, `Requires=`, `PartOf=` on
`hermes-gateway.service`.

One of the three observations is whether that gateway is healthy. A dependency
would have systemd start it before we could look — and a maintenance run that
quietly fixes the thing it was sent to observe reports on a world it created.
The task must be able to say *the gateway is down*.

## 4. `TimeoutStartSec=300`

A last-resort fuse, **not** a substitute for the supervisor's deadlines. The
Rust supervisor owns its children's timeouts and always reaps them; systemd's
timeout exists only so a bug in `hermes-maint` itself cannot hang forever.

```
external child (systemctl)   15s timeout + 3s grace   = 18s
disk-space                   statvfs                  ≈ 0
backup-freshness             a few small JSON reads   ≈ 0
tasks run sequentially                        worst  ≈ 18s
```

300 leaves roughly a sixteenfold margin for a far slower disk while still
bounding a wedged process. **If it ever fires**, the run is left open and the
next run reconciles it as `Interrupted` — demonstrated below.

## 5. Hardening: what survived contact with reality

Every directive was applied **one at a time** and run for real. The list in the
original design did not survive intact, and the two failures are worth more
than the eleven successes.

### 5.1 Applied

```
NoNewPrivileges=yes          RestrictSUIDSGID=yes
RestrictRealtime=yes         RestrictNamespaces=yes
LockPersonality=yes          MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX
UMask=0077                   MemoryMax=128M
```

`RestrictAddressFamilies=AF_UNIX` was an open question: `gateway-service-health`
needs the user bus. Verified — the bus is a Unix socket, the task still works,
and internet sockets are refused outright.

Each one is confirmed present in `systemctl --user show`, not merely written in
the file.

### 5.2 Removed: unsupported in an unprivileged user unit

```
ProtectKernelModules=yes
```

The service dies before `ExecStart`:

```
hermes-maint.service: Failed at step CAPABILITIES spawning
    /home/mauricio/.local/bin/hermes-maint: Operation not permitted
status=218/CAPABILITIES
```

It implies `CapabilityBoundingSet=~CAP_SYS_MODULE`, and dropping a bounding-set
capability needs `CAP_SETPCAP`, which an unprivileged user manager does not
have.

**`systemd-analyze --user security` marked this directive ✓.** Static analysis
sees a well-formed file; only running it finds out. That is precisely why the
list was applied one at a time instead of pasted in.

### 5.3 Removed: they break the observation

```
ProtectSystem=strict    PrivateTmp=yes        ProtectKernelTunables=yes
ProtectControlGroups=yes  ProtectHome=yes     ReadWritePaths=
PrivateNetwork=yes
```

All of them fail the same way:

```
error: gateway-service-health: could not run systemctl:
       the program is owned by uid 65534
```

The cause, measured rather than guessed:

```
no sandboxing directive      uid_map = 0 0 4294967295    systemctl uid = 0
any of the above             uid_map = 1000 1000 1       systemctl uid = 65534
```

To apply filesystem sandboxing for an **unprivileged** user manager, systemd
sets up a user namespace that maps only the invoking uid. Inside it every
unmapped owner — root included — appears as `65534` (nobody). So
`/usr/bin/systemctl`, genuinely `root:root 0755`, looks nobody-owned, and the
supervisor's pre-flight correctly refuses to execute a program with a foreign
owner.

**The pre-flight rule is not being weakened to win a score.** It is doing its
job with the information it has; inside that namespace it simply cannot tell a
root-owned binary from a stranger's. Reconciling the two is a real design
question — it would mean teaching the pre-flight about namespace mapping, or
accepting an unmapped owner when the file is unwritable — and it belongs in a
slice of its own, with its own argument, not smuggled in here.

`PrivateNetwork=yes` is the one that stings: this tool makes no network call
and the guarantee would have been free. It triggers the same user namespace.

> A methodological note. The first bisection put `ReadWritePaths=` in *every*
> variant including the control, so every case failed and the result looked
> like "nothing works". `ReadWritePaths` alone creates the namespace. The
> control has to be a real control.

### 5.4 Still out: `ProtectHome`

Everything this tool does is in `$HOME` by definition. It is out for the same
reason as ever, and now also for the namespace reason above.

### 5.5 Not attempted

`SystemCallFilter=@system-service` would be the next meaningful gain. It is not
in this slice's brief and was not applied.

## 6. `MemoryMax=128M`, measured first

```
peak RSS of the process        ~7.8–8.1 MB   /usr/bin/time -v, five runs
peak of the whole cgroup       ~2.0 MB       MemoryPeak, three runs
```

The two differ because most of the binary's resident pages are file-backed and
already in page cache, charged to whoever faulted them in first. The cgroup
number is the one `MemoryMax` governs.

128M is about sixty times the measured cgroup peak — unreachable by any
legitimate run, and still a bound on a runaway allocation on a host with 6.3 GB
of RAM and a few hundred megabytes free. Being killed there leaves an open run,
which the next run reconciles as `Interrupted`: a safe failure.

## 7. `SuccessExitStatus=3`, and only 3

Proven under real contention, with this shell holding the lock on its own file
descriptor — no child process, no polling, no timing guess:

```
A) the binary directly      exit 3   "lock held by another run, nothing to do"
B) the same, via systemd    Result=success · not in `systemctl --failed`
                            state.json byte-identical before and after
C) contrast, same unit      exit 4 → Failed with result 'exit-code'
                                     status=4/NOPERMISSION
```

C is the important half. **4, 5, 6 and 7 are findings and must look like
problems.** A contended lock is the lock working; a partial run, a timeout, a
degraded service and incompatible state are not. Nothing else will ever join
that list.

## 8. Interrupted runs reconcile

Demonstrated with a real `SIGKILL`, in a disposable `HERMES_HOME`, never
production. The kill is synchronised on a real artifact — `state.json` existing
means the run is open and persisted — rather than on a sleep:

```
run abierto en disco: id 1  finished_at = None  outcome = None
warn: run 1 never closed; recorded as interrupted
historia: id 1 interrupted  finished_at = None
```

`finished_at` stays `None`: we do not know when it died, and inventing a
timestamp would be worse than admitting the gap.

## 9. What was actually run

```
$ hermes-maint run --trigger manual --dry-run    # against the real HERMES_HOME
```

Created `~/.hermes/hermes-maint/` (`0700`) and an empty `lock` (`0600`), and no
`state.json`. That is the documented exception: taking the lock is mandatory
even for a dry-run, because a dry-run reading state under a real run would
report fiction. A dry-run **lists** the three tasks and executes none, so the
observations themselves come from the real run below.

```
$ hermes-maint run --trigger manual              # direct, outside systemd
info: disk-space: 824.0 GiB free of 868.2 GiB usable (94.9%), inodes 98.8% free
info: backup-freshness: newest verified backup is 31.0h old (2 verified backups)
info: gateway-service-health: hermes-gateway.service active (running)
info: run 1 closed as Ok in 0s
exit=0
```

```
$ systemctl --user start hermes-maint.service    # the timer's entrypoint
Result=success · ExecMainStatus=0 · ActiveState=inactive · SubState=dead
run 8, trigger=timer, three tasks Ok
```

`inactive (dead)` afterwards is correct for `Type=oneshot`: there is nothing
left running to be active.

Permissions on the real state:

```
~/.hermes/hermes-maint        0700
~/.hermes/hermes-maint/state.json   0600   schema 1
~/.hermes/hermes-maint/lock         0600
```

## 10. A finding, reported and not fixed

```
backup-freshness: newest verified backup is 31.1h old
```

Under the 48-hour threshold, so `Ok` — but the most recent verified backup is
from 2026-09-16 18:22. It crosses the threshold on 2026-09-18 around 18:22, and
`backup-freshness` will start reporting `Degraded`, taking the run to exit 6.

That is the task working. Nothing here runs a backup, and nothing here will.

## 11. Production, before and after

```
gateway     ActiveState=active  SubState=running  MainPID=1163669  NRestarts=0
            identical to the baseline
gateway unit sha256 unchanged
backups     same two directories, both state.json hashes unchanged
timers      none; hermes-maint.timer does not exist
```

Everything written to production, and nothing else:

```
~/.local/bin/hermes-maint                       new, 0755
~/.config/systemd/user/hermes-maint.service     new, 0644
~/.hermes/hermes-maint/{state.json,lock}        new, 0600 under a 0700 dir
systemctl --user daemon-reload
```

## 12. Uninstall

Rehearsed end to end under parallel names, so the approved installation was not
disturbed:

```bash
rm ~/.config/systemd/user/hermes-maint.service
systemctl --user daemon-reload
systemctl --user reset-failed hermes-maint.service   # only if it ever failed
rm ~/.local/bin/hermes-maint
rm -rf ~/.hermes/hermes-maint
```

Verified afterwards: no binary, no unit, no state, nothing loaded in systemd,
no timer, no resident process. There is nothing in `/etc`, nothing root-owned,
no crontab entry and no shell-profile hook to find.

## 13. Not done

```
hermes-maint.timer        does not exist
OnCalendar                not set
systemctl --user enable   never run
03:00                     nothing is scheduled
```

The service has no `[Install]` section, so `enable` fails loudly rather than
quietly wiring it to a target. Scheduling is the next slice.
