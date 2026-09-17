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

/// What a task saw. The string is a short, human-readable summary that ends up
/// in the journal and in `state.json`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Observation {
    /// Looked, nothing wrong.
    Ok(String),
    /// Looked, and something is not fine. **Reporting only** -- no task in
    /// this design acts on what it finds.
    Degraded(String),
}

impl Observation {
    #[must_use]
    pub fn detail(&self) -> &str {
        match self {
            Observation::Ok(d) | Observation::Degraded(d) => d,
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

pub trait Task {
    /// Stable identifier. It is recorded in state, so it must not change
    /// casually -- history would stop lining up.
    fn id(&self) -> &'static str;

    /// One line about what it looks at, for `--list-tasks` and the journal.
    fn describe(&self) -> &'static str;

    fn run(&self, ctx: &TaskContext<'_>) -> Result<Observation, TaskError>;
}

/// The built-in tasks, in the order they run.
///
/// A compiled-in registry, not a configured one. Configuration may eventually
/// enable, disable or re-time a task; it may **never** introduce a command
/// string or a path, which closes the whole injection class rather than
/// filtering it.
#[must_use]
pub fn registry() -> Vec<Box<dyn Task>> {
    vec![Box::new(crate::tasks::disk_space::DiskSpace::default())]
}
