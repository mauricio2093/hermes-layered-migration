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
use crate::state::{LoadError, Origin, Outcome, Run, State, TaskOutcome, TaskResult};

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
    /// State from a newer version, or arguments that do not make sense.
    Misuse(String),
    /// Something under us broke.
    Io(std::io::Error),
}

impl StartError {
    #[must_use]
    pub const fn exit(&self) -> Exit {
        match self {
            StartError::LockBusy => Exit::LockBusy,
            StartError::Misuse(_) => Exit::Misuse,
            StartError::Io(_) => Exit::Internal,
        }
    }
}

impl fmt::Display for StartError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            StartError::LockBusy => f.write_str("another run is already in progress"),
            StartError::Misuse(m) => f.write_str(m),
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
            LoadError::FutureSchema { .. } => StartError::Misuse(e.to_string()),
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

    /// Record a finished task. Nothing calls this yet -- the registry is empty
    /// -- but the outcome arithmetic below is what it feeds, and that is worth
    /// having settled and tested before the first real task arrives.
    pub fn record_task(&mut self, result: TaskResult) {
        self.run.tasks.push(result);
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
        let mut worst = Outcome::Ok;
        for t in &self.run.tasks {
            let candidate = match t.outcome {
                TaskOutcome::Timeout => Outcome::Timeout,
                TaskOutcome::Failed | TaskOutcome::Skipped => Outcome::Partial,
                TaskOutcome::Degraded => Outcome::Degraded,
                TaskOutcome::Ok => continue,
            };
            worst = rank_max(worst, candidate);
        }
        worst
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
