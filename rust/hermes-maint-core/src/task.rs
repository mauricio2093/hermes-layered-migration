//! What a task is.
//!
//! Tasks in this slice run **in process**. Nothing here spawns a child,
//! enforces a deadline or signals a process group -- that is the next slice,
//! and mixing it into this one would mean debugging `statvfs` arithmetic and
//! `SIGTERM` escalation in the same commit.
//!
//! The consequence is a deliberately small vocabulary: a task observes
//! something and reports `Ok` or `Degraded`, or it fails. `Skipped` and
//! `Timeout` exist in the state schema because the child supervisor will need
//! them; no task can produce them yet.

use std::fmt;

use crate::paths::Paths;
use crate::state::TaskOutcome;

/// What a task saw. The string is a short, human-readable summary that ends up
/// in the journal and in `state.json`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Observation {
    /// Looked, nothing wrong.
    Ok(String),
    /// Looked, and something is not fine. **Reporting only** -- no task in
    /// this design acts on what it finds.
    Degraded(String),
    /// Did not look, because the precondition for looking was absent.
    ///
    /// Distinct from `Degraded` on purpose: "there is no backup system on this
    /// machine" is not an observation about backups, and reporting it as one
    /// would be inventing a finding. Distinct from a `TaskError` too -- the
    /// task worked exactly as intended; there was simply nothing to inspect.
    Skipped(String),
}

impl Observation {
    #[must_use]
    pub fn detail(&self) -> &str {
        match self {
            Observation::Ok(d) | Observation::Degraded(d) | Observation::Skipped(d) => d,
        }
    }
}

/// A task could not complete. Distinct from `Degraded`: degraded means the
/// check ran and the news is bad, failed means the check did not run.
///
/// Collapsing the two is how monitoring starts lying.
#[derive(Debug)]
pub struct TaskError(pub String);

impl fmt::Display for TaskError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for TaskError {}

/// What a task is given. Nothing else is reachable from here on purpose: no
/// network handle, no database, no process spawner.
#[derive(Debug, Clone, Copy)]
pub struct TaskContext<'a> {
    pub paths: &'a Paths,
}

/// What a task hands back: the verdict, plus whatever structured evidence it
/// has.
///
/// [`Observation`] is the vocabulary of an **in-process** check, which can
/// only be fine, not fine, or unable to look. A task backed by a child process
/// has more to say -- it can fail, it can be killed on a deadline, and it has
/// an exit status -- so this is the wider type the trait returns. In-process
/// tasks build one with `.into()`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TaskReport {
    pub outcome: TaskOutcome,
    pub detail: String,
    /// Exit status, for a task backed by a process.
    pub exit: Option<i32>,
    /// The signal that ended it, when one did.
    pub signal: Option<i32>,
    /// Total bytes the child wrote across both streams.
    pub output_bytes: Option<u64>,
}

impl TaskReport {
    #[must_use]
    pub fn new(outcome: TaskOutcome, detail: impl Into<String>) -> Self {
        Self {
            outcome,
            detail: detail.into(),
            exit: None,
            signal: None,
            output_bytes: None,
        }
    }
}

impl From<Observation> for TaskReport {
    fn from(o: Observation) -> Self {
        let outcome = match &o {
            Observation::Ok(_) => TaskOutcome::Ok,
            Observation::Degraded(_) => TaskOutcome::Degraded,
            Observation::Skipped(_) => TaskOutcome::Skipped,
        };
        Self::new(outcome, o.detail())
    }
}

pub trait Task {
    /// Stable identifier. It is recorded in state, so it must not change
    /// casually -- history would stop lining up.
    fn id(&self) -> &'static str;

    /// One line about what it looks at, for `--list-tasks` and the journal.
    fn describe(&self) -> &'static str;

    /// `Err` is reserved for the task's own machinery breaking -- it becomes
    /// `Failed`. A child process that exits non-zero is not an error here: it
    /// is a perfectly successful observation that something went wrong, and it
    /// comes back as `Ok(TaskReport { outcome: Failed, .. })` with its exit
    /// status intact.
    fn run(&self, ctx: &TaskContext<'_>) -> Result<TaskReport, TaskError>;
}

/// The built-in tasks, in the order they run.
///
/// Cheap and local first: if the disk is full, that is worth seeing before
/// anything that reads more files.
///
/// A compiled-in registry, not a configured one. Configuration may eventually
/// enable, disable or re-time a task; it may **never** introduce a command
/// string or a path, which closes the whole injection class rather than
/// filtering it.
#[must_use]
pub fn registry() -> Vec<Box<dyn Task>> {
    vec![
        Box::new(crate::tasks::disk_space::DiskSpace::default()),
        Box::new(crate::tasks::backup_freshness::BackupFreshness::default()),
    ]
}
