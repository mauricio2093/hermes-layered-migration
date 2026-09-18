//! Lock, state and run lifecycle for `hermes-maint`.
//!
//! This crate deliberately knows nothing about Hermes. It does not open
//! `state.db`, it does not talk to the gateway, it opens no socket and it
//! makes no network call. It acquires a lock, reconciles its own state, frames
//! a run, and writes the result down.
//!
//! Tasks run in process. Spawning children, enforcing deadlines and signalling
//! process groups are not here: that is a slice of its own, and it should not
//! be debugged in the same commit as a task's arithmetic.

pub mod exit;
pub mod lock;
pub mod log;
pub mod paths;
pub mod run;
pub mod state;
pub mod supervisor;
pub mod task;
pub mod tasks;

pub use exit::Exit;
pub use lock::{Lock, LockError};
pub use run::{aggregate, Runner, Trigger};
pub use state::{Outcome, State, SCHEMA};
pub use task::{Observation, Task, TaskContext, TaskError, TaskReport};

/// Seconds since the Unix epoch.
///
/// A clock before 1970 is not worth a `Result` here: it clamps to 0, which is
/// visibly wrong in a report rather than silently wrong in arithmetic.
#[must_use]
pub fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}
