//! The run lifecycle: take the lock, frame a run, close it, exit.
//!
//! There are no tasks in this slice. That is not an omission -- a run that
//! executes nothing still has to get the lock, the reconciliation, the
//! persistence and the exit code exactly right, and those are much easier to
//! prove correct while there is no real work to confuse them with.

use std::fmt;

use crate::exit::Exit;
use crate::lock::{Lock, LockError};
use crate::paths::Paths;
use crate::state::{clamp_detail, LoadError, Origin, Outcome, Run, State, TaskOutcome, TaskResult};
use crate::task::{Task, TaskContext, TaskReport};

/// What started this run. An allowlist, not free text: the value is recorded
/// in state and shown in reports, and unvalidated input has no business there.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Trigger {
    /// `hermes-maint.timer`.
    Timer,
    /// A person, at a terminal.
    Manual,
}

impl Trigger {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Trigger::Timer => "timer",
            Trigger::Manual => "manual",
        }
    }

    pub fn parse(s: &str) -> Result<Self, String> {
        match s {
            "timer" => Ok(Trigger::Timer),
            "manual" => Ok(Trigger::Manual),
            other => Err(format!(
                "unknown trigger {other:?}; expected \"timer\" or \"manual\""
            )),
        }
    }
}

impl fmt::Display for Trigger {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Why a run could not start.
#[derive(Debug)]
pub enum StartError {
    /// Another run holds the lock. Not a failure.
    LockBusy,
    /// Arguments that do not make sense.
    Misuse(String),
    /// The state file was written by a newer build. Its own code (7), because
    /// it is not a mistake the caller made at the command line.
    IncompatibleState(String),
    /// Something under us broke.
    Io(std::io::Error),
}

impl StartError {
    #[must_use]
    pub const fn exit(&self) -> Exit {
        match self {
            StartError::LockBusy => Exit::LockBusy,
            StartError::Misuse(_) => Exit::Misuse,
            StartError::IncompatibleState(_) => Exit::IncompatibleState,
            StartError::Io(_) => Exit::Internal,
        }
    }
}

impl fmt::Display for StartError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            StartError::LockBusy => f.write_str("another run is already in progress"),
            StartError::Misuse(m) | StartError::IncompatibleState(m) => f.write_str(m),
            StartError::Io(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for StartError {}

/// An open run. Holds the lock for as long as it exists.
#[derive(Debug)]
pub struct Runner {
    paths: Paths,
    // Never read after `start`; it exists to hold the lock open. Dropping the
    // `Runner` closes it, which is what releases the lock.
    _lock: Lock,
    state: State,
    run: Run,
    dry_run: bool,
}

impl Runner {
    /// Take the lock, reconcile previous state, and open a new run.
    ///
    /// The open run is persisted **before** any work begins. That is what
    /// makes an interruption detectable at all: if the process dies from here
    /// on, the next start finds a run with no `finished_at` and records it as
    /// interrupted.
    pub fn start(paths: Paths, trigger: Trigger, dry_run: bool) -> Result<Self, StartError> {
        paths.ensure_dir().map_err(StartError::Io)?;

        // Every entry point takes the lock, dry-run included: a dry-run that
        // read state while a real run rewrote it would report fiction.
        let mut lock = match Lock::acquire(paths.lock_file()) {
            Ok(l) => l,
            Err(LockError::Busy { path }) => {
                crate::info!(
                    "lock held by another run ({}), nothing to do",
                    path.display()
                );
                return Err(StartError::LockBusy);
            }
            Err(LockError::Io { path, source }) => {
                return Err(StartError::Io(std::io::Error::new(
                    source.kind(),
                    format!("{}: {source}", path.display()),
                )))
            }
        };

        let started_at = crate::now();
        if !dry_run {
            lock.write_diagnostics(trigger.as_str(), started_at);
        }

        let (mut state, origin) = State::load(&paths).map_err(|e| match e {
            LoadError::FutureSchema { .. } => StartError::IncompatibleState(e.to_string()),
            LoadError::Io(io) => StartError::Io(io),
        })?;

        match &origin {
            Origin::Fresh => crate::info!("no previous state; starting fresh"),
            Origin::Existing => {}
            Origin::Quarantined { moved_to, .. } => {
                crate::warn!(
                    "previous state was unusable and was kept at {}",
                    moved_to.display()
                );
            }
        }

        if let Some(id) = state.reconcile() {
            crate::warn!("run {id} never closed; recorded as interrupted");
        }

        let run = Run {
            id: state.next_run_id,
            started_at,
            finished_at: None,
            trigger: trigger.as_str().to_string(),
            outcome: None,
            tasks: Vec::new(),
        };
        state.next_run_id = state.next_run_id.saturating_add(1);
        state.last_run = Some(run.clone());

        if dry_run {
            crate::info!(
                "dry-run: would open run {} and write {}",
                run.id,
                paths.state_file().display()
            );
        } else {
            state.save(&paths).map_err(StartError::Io)?;
        }

        crate::info!(
            "run {} open (trigger={trigger}{})",
            run.id,
            if dry_run { ", dry-run" } else { "" }
        );

        Ok(Self {
            paths,
            _lock: lock,
            state,
            run,
            dry_run,
        })
    }

    /// Record a finished task.
    pub fn record_task(&mut self, result: TaskResult) {
        self.run.tasks.push(result);
    }

    /// Run every task in order, recording each result.
    ///
    /// Sequential on purpose: the tasks are I/O-bound against the same disk on
    /// a 6 GB machine, so concurrency would buy nothing and would make the
    /// ordering, the failure semantics and -- once deadlines exist -- the
    /// timeout accounting harder to reason about.
    ///
    /// **A failing task does not abort the run.** The other observations are
    /// independent and still wanted; losing four because the fifth broke would
    /// be the wrong trade at 03:00.
    pub fn run_tasks(&mut self, tasks: &[Box<dyn Task>]) {
        if self.dry_run {
            for task in tasks {
                crate::info!("dry-run: would run {} -- {}", task.id(), task.describe());
            }
            if tasks.is_empty() {
                crate::info!("dry-run: no tasks registered");
            }
            return;
        }

        // Cloned so the context's borrow does not collide with recording
        // results into `self` inside the loop.
        let paths = self.paths.clone();
        let ctx = TaskContext { paths: &paths };
        for task in tasks {
            // Monotonic: a clock step mid-task must not change a recorded
            // duration, for the same reason it must not move a deadline.
            let started = std::time::Instant::now();
            let report = match task.run(&ctx) {
                Ok(r) => r,
                // `Err` means the task's own machinery broke, which is a
                // failure of the check, not an observation.
                Err(e) => TaskReport::new(TaskOutcome::Failed, e.to_string()),
            };
            let duration_s = started.elapsed().as_secs();
            let detail = clamp_detail(&report.detail);

            match report.outcome {
                TaskOutcome::Ok => crate::info!("{}: {detail}", task.id()),
                TaskOutcome::Degraded | TaskOutcome::Skipped => {
                    crate::warn!("{}: {detail}", task.id());
                }
                _ => crate::error!("{}: {detail}", task.id()),
            }

            self.record_task(TaskResult {
                id: task.id().to_string(),
                outcome: report.outcome,
                exit: report.exit,
                signal: report.signal,
                duration_s,
                output_bytes: report.output_bytes,
                detail: Some(detail),
            });
        }
    }

    #[must_use]
    pub fn run_id(&self) -> u64 {
        self.run.id
    }

    #[must_use]
    pub fn dry_run(&self) -> bool {
        self.dry_run
    }

    #[must_use]
    pub fn state(&self) -> &State {
        &self.state
    }

    /// The outcome implied by the tasks recorded so far.
    ///
    /// Precedence is deliberate: a timeout outranks a failure because
    /// something was killed, and a failure outranks a degraded observation
    /// because a task that did not finish tells you less than one that did.
    #[must_use]
    pub fn outcome(&self) -> Outcome {
        aggregate(&self.run.tasks)
    }

    /// Close the run, persist, and hand back the process exit code.
    ///
    /// Consumes the runner, so the lock is released immediately afterwards and
    /// there is no way to record a task against a closed run.
    pub fn finish(mut self) -> Exit {
        let outcome = self.outcome();
        let finished_at = crate::now();

        self.run.finished_at = Some(finished_at);
        self.run.outcome = Some(outcome);
        let duration = self.run.duration_s().unwrap_or(0);
        let id = self.run.id;

        self.state.last_run = None;
        self.state.push_history(self.run.clone());

        if self.dry_run {
            crate::info!("dry-run: would close run {id} as {outcome:?}; nothing was written");
            // A dry-run reports what a real run *would* exit with.
            return outcome.exit();
        }

        if let Err(e) = self.state.save(&self.paths) {
            // The work happened; only the bookkeeping failed. That is an
            // internal error and it must not be reported as success, because
            // the next run will believe this one never finished.
            crate::error!("run {id} finished but its state could not be written: {e}");
            return Exit::Internal;
        }

        crate::info!("run {id} closed as {outcome:?} in {duration}s");
        outcome.exit()
    }
}

/// The run's outcome, from the tasks that ran.
///
/// A pure function over the recorded results, and **order-independent**: it
/// takes the maximum of a total ordering rather than folding in whatever
/// sequence the tasks happened to run in. A run whose verdict depended on task
/// order would be a run whose verdict changed when someone reordered the
/// registry.
///
/// ```text
/// Timeout  >  Interrupted  >  Partial  >  Degraded  >  Ok
/// ```
///
/// The ordering is a claim about how much attention each deserves:
///
/// - **Timeout** outranks everything because something had to be killed, and
///   a process that would not stop is the most urgent thing in the report.
/// - **Partial** (a task failed or was skipped) outranks **Degraded** because
///   a check that did not complete tells you less than one that did. A
///   degraded observation is information; a missing one is a gap.
/// - **Ok** is the identity: a run with no tasks at all is `Ok`.
#[must_use]
pub fn aggregate(tasks: &[TaskResult]) -> Outcome {
    tasks
        .iter()
        .map(|t| match t.outcome {
            TaskOutcome::Timeout => Outcome::Timeout,
            TaskOutcome::Failed | TaskOutcome::Skipped => Outcome::Partial,
            TaskOutcome::Degraded => Outcome::Degraded,
            TaskOutcome::Ok => Outcome::Ok,
        })
        .fold(Outcome::Ok, rank_max)
}

fn rank_max(a: Outcome, b: Outcome) -> Outcome {
    if rank(b) > rank(a) {
        b
    } else {
        a
    }
}

const fn rank(o: Outcome) -> u8 {
    match o {
        Outcome::Ok => 0,
        Outcome::Degraded => 1,
        Outcome::Partial => 2,
        Outcome::Interrupted => 3,
        Outcome::Timeout => 4,
    }
}
