# External tasks: the translation between layers

```
Task → ExternalTask → ChildSpec → supervise() → ChildResult
     → TaskReport → TaskResult → run outcome → exit code → state.json
```

`ExternalTask` is a **translator, not a second supervisor**. It builds a static
spec, hands the entire lifecycle to `supervise()`, and turns the result into
the task vocabulary. It does not know how to kill a process, drain a pipe,
create a process group, reap a child or compute a deadline — and it must never
learn.

> **No task in the production registry uses this.** The registry is still
> `disk-space` and `backup-freshness`, both in process. A test asserts that,
> and also that the `registry()` function's own source never mentions
> `ExternalTask` — because a probe command that runs forever at 03:00 because
> it was once useful for development is exactly the kind of thing nobody ever
> removes.

---

## 1. The mapping

Compared against the real enums rather than assumed:

| `ChildResult` | `TaskOutcome` | why |
|---|---|---|
| `Exited`, code 0 | `Ok` | |
| `Exited`, non-zero | `Failed` | the variant's own documentation reads "ran and exited non-zero" |
| `TimedOut` | `Timeout` | "killed on its deadline", exactly |
| `Signalled` | `Failed` | a segfault, an external `kill` or the OOM killer is a malfunction, not a deadline |
| `SpawnFailed` | `Failed` | see below |
| `Refused` | `Skipped` | see below |

### 1.1 The two that go different ways

`Skipped` is documented as *"never ran: pre-flight validation refused it
(missing, wrong owner, writable by others, not executable)"*. That is not an
approximation of a refusal — the variant was written for exactly this case. A
refusal is the pre-flight **working**: a deliberate, correct decision not to
run something.

A spawn failure is the opposite. Everything agreed the child should run and the
machinery broke — `ENOMEM`, a process limit, `ETXTBSY`, a file replaced between
the check and the `exec`. Nothing decided against it. That is a malfunction and
it earns the noisier verdict, `Failed`.

They differ only in the run's exit code by accident — both roll up to
`Partial` — but they differ in what a person should do next, which is what the
distinction is for.

### 1.2 `Degraded` is deliberately unreachable

An external child has no way to say *"I looked and something is wrong"*. That
would need a convention — a reserved exit code, or a line on stdout — and
inventing one before a real task needs it would be guessing at an interface.
The slice that introduces the first real external observation decides it, with
a concrete task in front of it.

## 2. Run outcome precedence

Aggregation is a pure function over the recorded results, and it is
**order-independent**: it takes the maximum of a total ordering rather than
folding in whatever sequence the tasks ran. A run whose verdict changed when
someone reordered the registry would be a bug that only appears after a
refactor.

```
Timeout  >  Interrupted  >  Partial  >  Degraded  >  Ok
```

The ordering is a claim about how much attention each deserves:

- **Timeout** outranks everything: something had to be killed, and a process
  that would not stop is the most urgent line in the report.
- **Partial** outranks **Degraded**: a check that did not complete tells you
  *less* than one that did. A degraded observation is information; a missing
  one is a gap, and a gap can hide anything.
- **Ok** is the identity — a run with no tasks is `Ok`.

Exit codes follow: `0`, `4` (partial), `5` (timeout), `6` (degraded).

## 3. What is persisted, and what is not

This is the boundary that matters, and it is not the supervisor's boundary.

```
supervisor capture  :  64 KiB per stream, in memory, transient
state.json          :  outcome, exit, signal, duration, byte count,
                       and a summary of at most 240 characters
```

### 3.1 The arithmetic that decided it

If the full capture were persisted:

```
30 runs × 1 external task × (64 KiB + 64 KiB)   =  3.75 MiB
30 runs × 3 external tasks                      = 11.25 MiB
30 runs × 5 external tasks                      = 18.75 MiB
```

and JSON escaping makes each of those larger. But the decisive number is not
any of them — it is this:

```
MAX_STATE_BYTES = 1 MiB
```

One run with one external task carrying a full capture is ~128 KiB. **After
about eight runs the state file would exceed its own cap, be quarantined on the
next read, and take the entire history with it.** Persisting the capture would
not merely make the file large; it would make the file destroy itself.

What is persisted instead, per task:

```
outcome · exit · signal · duration_s · output_bytes · detail (≤ 240 chars)
```

Measured, not estimated: a run whose child printed 8 MiB across both streams
produces a state file **under 8 KiB**, and thirty such runs stay far under the
cap. There is a test that asserts both.

`output_bytes` exists so that the one fact about the output that survives
sampling is not lost: whether the child was quiet or wrote four megabytes.

`timed_out` is **not** persisted separately — it is exactly
`outcome == Timeout`, and a field that can disagree with another field will
eventually disagree with it.

Adding `signal` and `output_bytes` did not change the schema version: both are
optional with defaults, so an older file still reads and a newer one is
ignored field-by-field by an older build.

### 3.2 Secrets

**A transient capture is not permission to write something down.**

A future external program might print an API key, a token, a cookie, a session
identifier or a private path — by accident, in a stack trace, in a debug line
someone left in. The supervisor has to capture output to diagnose anything at
all, but that is 64 KiB living in memory for the length of one run. Copying it
into a file that persists for thirty runs is a different act with different
consequences, and the two must not be conflated because one made the other
convenient.

So:

- the persisted `detail` is at most **240 characters**, of which at most 160
  come from the child;
- the excerpt is a **single line**, taken from the **tail** of `stderr` — where
  a program says what went wrong — falling back to `stdout` when `stderr` is
  empty;
- a task whose child may print something sensitive calls
  **`without_output_excerpt()`**, and then *nothing* the child wrote is
  recorded — only the structured fields. There is a test that plants a
  token-shaped string and asserts it never reaches the report.

This does not make output safe. It bounds how much of it can escape, and gives
a task a way to opt out entirely. A real task that handles secrets should use
that, and should be reviewed on the assumption that the excerpt is public.

## 4. Configuration cannot reach any of this

`ExternalTask` holds a boxed closure that builds the `ChildSpec` at run time,
when `HERMES_HOME` is known. It is constructed in Rust. There is no path by
which a configuration file supplies a program, an argv or a working directory —
the same rule as everywhere else in this design, enforced by the type rather
than by a check.

## 5. Not in this slice

```
no real maintenance script     no systemd unit
no probe in the registry       no first external observation
```

The next slice chooses which real, read-only external observation goes first —
and that choice is a design decision, not a leftover from development.
