# The child-process supervisor

Running a program without losing control of it. Nothing in this slice runs a
real maintenance script: the mechanism is proven against fixtures first,
because the failure modes here are the kind that only show up at 03:00.

```
preflight → spawn (own process group) → drain both pipes concurrently
          → wait until the deadline
          → SIGTERM the group → grace → SIGKILL the group
          → wait ALWAYS
```

---

## 1. No shell, ever

The supervisor takes a **structured, compiled-in spec** — program, argv, cwd,
timeout, grace. There is no field that can carry a command string and no code
path that builds one.

```rust
ChildSpec { id, program, argv, cwd, timeout, grace }
```

Configuration will eventually be able to enable or disable a task and move its
timeout within bounds. It will never supply `program`, `argv` or `cwd`: those
come from a registry that is Rust source, reviewed and compiled.

This is enforced twice — by the type, and by a test that strips the comments
from the module and asserts that the code never names `/bin/sh`, `/bin/bash`,
`/usr/bin/env` or `-c`, and that the only `Command::new` in it is
`Command::new(&spec.program)`.

## 2. Process group

Set as an attribute of the spawn (`process_group(0)`), so the child is already
leading its own group **before** `exec`. Doing it from the parent afterwards is
a race: the child may have exec'd, or spawned a grandchild, first.

Signals go to the group, never to the pid. A script's grandchildren are exactly
the processes that survive a careless kill.

The test does not infer this. A fixture spawns a grandchild which records its
own pid **and process group** to a file and then blocks; after the timeout, the
test asserts the recorded group equals the child's pid — proving they shared a
group — and then polls until the grandchild's pid no longer exists.

## 3. Timeout, and what counts as finished

```
wait(timeout)
  ├─ finished  → record exit status
  └─ still alive
       SIGTERM to the group
       wait(grace)
         ├─ finished  → record
         └─ still alive
              SIGKILL to the group
              wait(bounded)         ← always
```

**"SIGTERM sent" is never "process finished".** Only `wait` says that. The
child is moved into a thread whose only job is to `wait`, so it is reaped on
every path, including both kill paths, and nothing is left a zombie. The test
asserts this the only way that means anything: a second `waitpid` must fail
with `ECHILD`.

The final wait after `SIGKILL` is bounded, so a process wedged in an
uninterruptible kernel operation cannot hang the supervisor itself.

## 4. Monotonic deadline

`Instant`, never `SystemTime`. An NTP step during a child must not lengthen or
shorten its timeout.

Stepping the system clock needs root, so the test checks the two things that
can be checked without it: the measured duration is at least the deadline, and
the module's own source contains `Instant::now()` and none of `SystemTime`,
`UNIX_EPOCH` or `crate::now()`. That second check is unusual, and it is there
because this is a property that a future edit could silently lose.

## 5. Pre-flight, and the race it does not close

Before spawning, all of:

| check | why |
|---|---|
| exists | otherwise the error arrives as a spawn failure with less context |
| not a symlink | following one lets whoever can write the directory choose what gets executed |
| regular file | not a directory, not a device |
| executable | `mode & 0o111` |
| **not group- or other-writable** | a program anyone can rewrite is a hole the moment anything privileged runs it |
| owned by this user or root | root-owned is how a package manager installs one, and a root-owned file this user cannot write is not a way in |
| cwd exists, is a directory, is not a symlink | |

Failing any of them means **no spawn at all**. Nothing is "fixed": permissions
are not adjusted, directories are not created. The result is a `Refused`
outcome carrying the specific reason, with no pid and zero duration.

### 5.1 TOCTOU — stated plainly

**The time-of-check/time-of-use race is not closed, and the design does not
pretend otherwise.** Checking a path and executing it are two operations.
Between them the file could in principle be replaced.

Closing it properly would mean holding an `O_PATH` descriptor from the check
through to `fexecve`, which `std::process::Command` cannot express.
Reimplementing fork/exec by hand to get it would cost the atomic process-group
creation and the pipe handling this module depends on — and would add a large
amount of unsafe code around the most security-sensitive operation here.

That trade was not made, for reasons specific to this program:

- every path comes from a **compiled-in registry**, not from configuration, a
  file, or anything a user typed;
- the checks above already require the file to be un-writable by group and
  other, and owned by this user or root;
- so the only actor who can win the race is one who can already write files
  owned by this user — who, per the design's threat model, can equally well
  edit the systemd unit, the binary, or `.bashrc`, and has no need of a race.

The window is real. It is simply not the weakest thing in the picture, and
writing "validated" next to it would be the actual danger.

### 5.2 A rule that bites in practice

This machine runs with `umask 002`, so **every freshly built binary comes out
`0775`** — group-writable — and the pre-flight refuses it. The real maintenance
scripts are `0700` and pass. The tests link or copy the fixture and fix its
mode explicitly rather than weakening the rule, and one test pins the
behaviour so a future lax `umask` cannot quietly re-open the hole.

## 6. Output: bounded, and the tail is what is kept

```
64 KiB per stream
```

**The tail, not the head.** A child killed on its deadline has its most recent
activity at the end; a failing script's error is at the end; the first kilobyte
of a long run is usually a banner. The total byte count is recorded separately,
so nothing about the size is lost.

```
Captured { bytes, total_bytes, truncated, complete }
```

`truncated` means the cap was reached. **Truncation is never itself a
failure** — a talkative child is not a failing one, and there is a test that
says so.

`complete` means the pipe reached EOF. `false` means something still held the
write end after the child was reaped — a descendant that escaped the process
group — and the supervisor stopped waiting rather than hang.

The fixture's filler is deterministic (byte *i* is `b'a' + (i % 26)`), so the
tests assert exactly which window survived, not merely that something did.

## 7. Pipes and the deadlock

```
child writes a lot  →  pipe fills  →  child blocks in write()
parent waits for child  →  never reads  →  nobody moves
```

This is the classic way a supervisor like this one hangs, and it is why both
pipes are drained on their own threads **starting before the parent waits**,
not after.

Two small threads, not an async runtime. The work is two blocking reads; a
runtime would add a dependency, a scheduler and a great deal of machinery to
solve a problem that two `std::thread::spawn` calls solve exactly.

Tested with 4 MiB flooding **both** streams at once — the arrangement that
deadlocks if either stream is drained after the other, or after `wait`.

## 8. Environment

The child inherits **nothing**. Its entire environment is:

```
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
LC_ALL=C   LANG=C   TZ=UTC   NO_COLOR=1
```

The parent's environment holds API keys, tokens, `SSH_AUTH_SOCK`, cloud
credentials and every Hermes variable. A child that needs one of those should
be given it deliberately, one variable at a time, with a reason — not handed
all of them because inheriting was easier.

`LC_ALL=C` and `TZ=UTC` are not tidiness: a locale-dependent child produces
locale-dependent output, and then a report means different things on different
machines.

The test asserts the child's environment is *exactly* that list, and names
`HOME`, `USER`, `SSH_AUTH_SOCK`, `HERMES_HOME`, `AWS_SECRET_ACCESS_KEY` and
`OPENAI_API_KEY` — all present in the process running the test — as absent.

## 9. Results that stay distinguishable

```rust
enum Outcome { Exited, TimedOut, Signalled, SpawnFailed(String), Refused(Refusal) }
```

with `exit_code`, `signal`, `duration`, `timed_out`, `term_sent`, `kill_sent`,
`pid` and both captured streams.

`exit 1`, killed by `SIGTERM`, killed by `SIGKILL` after ignoring `SIGTERM`,
timed out, failed to spawn and refused before spawning are six different
situations with six different responses. Collapsing them into "failed" throws
away exactly the information that decides what to do next.

## 10. Stable tests, not lucky ones

Where a property depends on ordering, the tests synchronise rather than sleep:

- the grandchild **writes a file** before its parent announces readiness, so
  "the grandchild exists" is established, not hoped for;
- the timeout tests use a fixture that blocks forever, so it is alive at the
  deadline whatever the scheduler does — there is no race to lose;
- the grace-period test asserts the *total* elapsed time is at least
  `timeout + grace`, rather than timing the escalation itself.

### 10.1 A race the tests themselves had

The first version copied the fixture with `fs::copy` and executed the copy.
That failed about two runs in fifteen with `ETXTBSY`.

`fs::copy` holds the destination open for writing. A sibling test thread that
forks during that window hands the descriptor to its transient child, and
`exec` of a file some process has open for writing fails. The fix is not a
retry: scratch directories were moved to `CARGO_TARGET_TMPDIR` — the same
filesystem as the build output — and the fixture is **hard-linked** rather than
copied. A hard link never opens anything for writing, so the window does not
exist. Twenty-five consecutive clean runs.

## 11. Not in this slice

```
no real maintenance script     no registry integration
no systemd unit                no task that spawns anything
```

`disk-space` and `backup-freshness` remain in-process. The first external task
comes next, and it will be a deliberately inocuous one before anything real is
handed to this.
