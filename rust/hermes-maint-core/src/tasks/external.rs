//! A task backed by a child process.
//!
//! This is a translator, not a second supervisor. It builds a static
//! [`ChildSpec`], hands the whole lifecycle to [`supervise`], and turns the
//! [`ChildResult`] into the task vocabulary. It does not know how to kill a
//! process, drain a pipe, create a process group, reap a child or compute a
//! deadline, and it must never learn: that belongs to
//! [`crate::supervisor`] alone.
//!
//! **No task in the production registry uses this yet.** The registry is still
//! `disk-space` and `backup-freshness`, both in process. This exists so that
//! when the first real external observation arrives, the translation between
//! layers is already proven rather than being written at the same time.

use crate::paths::Paths;
use crate::state::{clamp_detail, TaskOutcome};
use crate::supervisor::{supervise, ChildResult, ChildSpec, Outcome};
use crate::task::{Task, TaskContext, TaskError, TaskReport};

/// How much child output is sampled into the persisted detail.
///
/// This is **not** the supervisor's capture limit, and the difference is the
/// point. The supervisor holds 64 KiB per stream in memory, transiently, for
/// this function to look at. What reaches `state.json` is this much, once, per
/// task. See `docs/external-tasks.md` for the arithmetic and for why a secret
/// in a child's output must not be able to reach a file on disk.
pub const EXCERPT_CHARS: usize = 160;

/// Builds the spec at run time, when `HERMES_HOME` is known.
///
/// A boxed closure rather than a configuration value: it is constructed in
/// Rust, so there is no path by which a config file could supply a program, an
/// argv or a working directory.
pub type SpecBuilder = Box<dyn Fn(&Paths) -> ChildSpec + Send + Sync>;

pub struct ExternalTask {
    id: &'static str,
    describe: &'static str,
    build: SpecBuilder,
    excerpt_output: bool,
}

impl std::fmt::Debug for ExternalTask {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ExternalTask")
            .field("id", &self.id)
            .field("excerpt_output", &self.excerpt_output)
            .finish_non_exhaustive()
    }
}

impl ExternalTask {
    #[must_use]
    pub fn new(id: &'static str, describe: &'static str, build: SpecBuilder) -> Self {
        Self {
            id,
            describe,
            build,
            excerpt_output: true,
        }
    }

    /// Keep only the structured fields -- outcome, exit, signal, duration,
    /// byte count -- and persist **none** of the child's output.
    ///
    /// For a child whose output may carry a token, a cookie or a private path.
    /// The supervisor still captures it transiently; nothing writes it down.
    #[must_use]
    pub fn without_output_excerpt(mut self) -> Self {
        self.excerpt_output = false;
        self
    }
}

impl Task for ExternalTask {
    fn id(&self) -> &'static str {
        self.id
    }

    fn describe(&self) -> &'static str {
        self.describe
    }

    fn run(&self, ctx: &TaskContext<'_>) -> Result<TaskReport, TaskError> {
        let spec = (self.build)(ctx.paths);
        Ok(interpret(&supervise(&spec), self.excerpt_output))
    }
}

/// Turn a supervised child's result into the task vocabulary.
///
/// Pure: no filesystem, no clock, no process. The mapping is the whole
/// contract between the two layers, so it is a function that can be tested
/// against a hand-built [`ChildResult`] rather than a behaviour buried in a
/// task.
///
/// | child | task | why |
/// |---|---|---|
/// | exited 0 | `Ok` | |
/// | exited non-zero | `Failed` | the variant's own documentation is "ran and exited non-zero" |
/// | timed out | `Timeout` | "killed on its deadline", exactly |
/// | killed by a signal we did not send | `Failed` | a segfault or the OOM killer is a malfunction, not a deadline |
/// | spawn failed | `Failed` | **not** `Skipped`: nothing decided against running it, the attempt broke |
/// | refused by pre-flight | `Skipped` | |
///
/// The last two are the ones worth arguing about, and they go different ways
/// on purpose. `Skipped` is documented as "never ran: pre-flight validation
/// refused it" -- a deliberate, correct decision not to run, which is exactly
/// what a refusal is. A spawn failure is the opposite: everything agreed the
/// child should run and the machinery broke, which is a malfunction and
/// deserves the noisier verdict.
///
/// `Degraded` is deliberately unreachable here. It would need a convention for
/// a child to say "I looked and something is wrong" -- an exit code, or a line
/// on stdout -- and inventing one before a real task needs it would be
/// guessing. The next slice decides it, with a task in front of it.
#[must_use]
pub fn interpret(result: &ChildResult, excerpt_output: bool) -> TaskReport {
    let outcome = match &result.outcome {
        Outcome::Exited if result.exit_code == Some(0) => TaskOutcome::Ok,
        Outcome::Exited | Outcome::Signalled | Outcome::SpawnFailed(_) => TaskOutcome::Failed,
        Outcome::TimedOut => TaskOutcome::Timeout,
        Outcome::Refused(_) => TaskOutcome::Skipped,
    };

    let mut detail = result.summary();
    if excerpt_output {
        if let Some(sample) = excerpt(result) {
            detail.push_str(" -- ");
            detail.push_str(&sample);
        }
    }

    let total = result.stdout.total_bytes + result.stderr.total_bytes;

    TaskReport {
        outcome,
        // Clamped again here, not only in the runner: this is the boundary the
        // bytes cross on their way to disk.
        detail: clamp_detail(&detail),
        exit: result.exit_code,
        signal: result.signal,
        output_bytes: Some(total),
    }
}

/// The tail of whichever stream is more likely to explain the outcome.
///
/// `stderr` first: that is where a program says what went wrong. Falls back to
/// `stdout` when `stderr` is empty. The tail rather than the head, for the same
/// reason the supervisor keeps the tail -- what a child was doing when it
/// stopped is at the end.
fn excerpt(result: &ChildResult) -> Option<String> {
    let stream = if result.stderr.bytes.is_empty() {
        &result.stdout
    } else {
        &result.stderr
    };
    let text = stream.text();
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return None;
    }

    // One line: a persisted detail with embedded newlines makes a one-line
    // journal entry into several, and a report harder to read.
    let flattened: String = trimmed
        .chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect();
    let collapsed = flattened.split_whitespace().collect::<Vec<_>>().join(" ");

    let count = collapsed.chars().count();
    Some(if count <= EXCERPT_CHARS {
        collapsed
    } else {
        // The tail, consistently with everything else.
        let skip = count - (EXCERPT_CHARS - 1);
        format!(
            "\u{2026}{}",
            collapsed.chars().skip(skip).collect::<String>()
        )
    })
}
