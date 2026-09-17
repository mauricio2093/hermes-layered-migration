//! Operational state: one JSON file, owned entirely by this tool.
//!
//! It is emphatically **not** `state.db`. That database belongs to Hermes, and
//! opening it would buy a permanent dependency on its schema, its migrations,
//! its locking, its lifecycle and its future compatibility -- in exchange for
//! nothing. The operational state of maintenance is a small domain of its own,
//! and it costs one file to keep sovereign.

use std::fs::{File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::paths::{Paths, FILE_MODE};

/// Our own schema number, starting at 1.
///
/// It has nothing to do with upstream's `SCHEMA_VERSION` and never will.
pub const SCHEMA: u32 = 1;

/// How many finished runs to keep. A state file that grows without bound is a
/// slow-motion disk failure.
pub const MAX_HISTORY: usize = 30;

/// Refuse to parse anything larger. The real file is a couple of kilobytes;
/// a megabyte already means something has gone wrong, and parsing it would
/// only turn a disk problem into a memory problem.
pub const MAX_STATE_BYTES: u64 = 1 << 20;

/// How a run ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Outcome {
    /// Everything ran, everything was fine.
    Ok,
    /// At least one task failed or was skipped.
    Partial,
    /// At least one task was killed on its deadline.
    Timeout,
    /// Everything ran; a health check reports degraded.
    Degraded,
    /// The process went away before closing the run. Set by reconciliation on
    /// the *next* start, never by the run itself -- a run that could record
    /// this would not have been interrupted.
    Interrupted,
}

impl Outcome {
    #[must_use]
    pub const fn exit(self) -> crate::Exit {
        match self {
            Outcome::Ok => crate::Exit::Ok,
            Outcome::Partial => crate::Exit::Partial,
            Outcome::Timeout => crate::Exit::Timeout,
            Outcome::Degraded => crate::Exit::Degraded,
            // Never returned by a live run; it describes a previous one.
            Outcome::Interrupted => crate::Exit::Partial,
        }
    }
}

/// How one task ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TaskOutcome {
    Ok,
    /// Ran and exited non-zero.
    Failed,
    /// Never ran: pre-flight validation refused it (missing, wrong owner,
    /// writable by others, not executable).
    Skipped,
    /// Killed on its deadline.
    Timeout,
    /// Ran fine and reports that something it observed is not fine.
    Degraded,
}

/// One task's result. No task exists yet; the shape is settled so that adding
/// the first one is not also a state migration.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TaskResult {
    pub id: String,
    pub outcome: TaskOutcome,
    /// The child's exit status, when it ran far enough to have one.
    #[serde(default)]
    pub exit: Option<i32>,
    pub duration_s: u64,
}

/// One run.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Run {
    pub id: u64,
    pub started_at: u64,
    /// `None` while the run is open -- and still `None` afterwards if the
    /// process died, because we do not know when it stopped and inventing a
    /// timestamp would be worse than admitting the gap.
    #[serde(default)]
    pub finished_at: Option<u64>,
    pub trigger: String,
    #[serde(default)]
    pub outcome: Option<Outcome>,
    #[serde(default)]
    pub tasks: Vec<TaskResult>,
}

impl Run {
    #[must_use]
    pub fn duration_s(&self) -> Option<u64> {
        self.finished_at.map(|f| f.saturating_sub(self.started_at))
    }
}

/// The whole file.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct State {
    pub schema: u32,
    /// Run ids are a counter rather than a timestamp: two runs in the same
    /// second would otherwise collide, and a counter reads better in a report.
    #[serde(default = "first_run_id")]
    pub next_run_id: u64,
    /// The run currently open, if any.
    #[serde(default)]
    pub last_run: Option<Run>,
    /// Finished runs, newest first, bounded by [`MAX_HISTORY`].
    #[serde(default)]
    pub history: Vec<Run>,
}

const fn first_run_id() -> u64 {
    1
}

impl Default for State {
    fn default() -> Self {
        Self {
            schema: SCHEMA,
            next_run_id: first_run_id(),
            last_run: None,
            history: Vec::new(),
        }
    }
}

/// Where the loaded state came from. The caller logs this; nothing branches on
/// it except the human reading the journal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Origin {
    /// No state file yet. First run on this machine.
    Fresh,
    /// Loaded and parsed.
    Existing,
    /// The file was unusable and was moved aside rather than overwritten.
    Quarantined { moved_to: PathBuf, reason: String },
}

#[derive(Debug)]
pub enum LoadError {
    /// The file declares a schema we do not understand. **Refuse to run.**
    ///
    /// Quarantining would be wrong here: the file is not corrupt, it is from a
    /// newer version, and moving it aside to start fresh would silently
    /// destroy state that a future `hermes-maint` would have read correctly.
    /// This is the one case where stopping is the safe option.
    FutureSchema {
        found: u32,
        supported: u32,
    },
    Io(io::Error),
}

impl std::fmt::Display for LoadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LoadError::FutureSchema { found, supported } => write!(
                f,
                "state file declares schema {found}, this build understands {supported}; \
                 refusing to overwrite state written by a newer version"
            ),
            LoadError::Io(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for LoadError {}

impl From<io::Error> for LoadError {
    fn from(e: io::Error) -> Self {
        LoadError::Io(e)
    }
}

impl State {
    /// Read the state, tolerating everything except a schema from the future.
    ///
    /// A missing file is normal. A malformed or oversized one is moved aside
    /// -- never silently overwritten, never fatal -- and the run proceeds from
    /// a fresh state, because losing history is a smaller harm than a
    /// maintenance run that refuses to happen.
    pub fn load(paths: &Paths) -> Result<(Self, Origin), LoadError> {
        let file = paths.state_file();
        let mut handle = match File::open(&file) {
            Ok(h) => h,
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                return Ok((Self::default(), Origin::Fresh))
            }
            Err(e) => return Err(e.into()),
        };

        let size = handle.metadata()?.len();
        if size > MAX_STATE_BYTES {
            let reason = format!("state file is {size} bytes, over the {MAX_STATE_BYTES} cap");
            let moved_to = quarantine(paths, &reason)?;
            return Ok((Self::default(), Origin::Quarantined { moved_to, reason }));
        }

        let mut raw = String::with_capacity(size as usize);
        match handle.read_to_string(&mut raw) {
            Ok(_) => {}
            Err(e) => {
                let reason = format!("state file is not readable text: {e}");
                let moved_to = quarantine(paths, &reason)?;
                return Ok((Self::default(), Origin::Quarantined { moved_to, reason }));
            }
        }

        // Peek at the schema before trusting the rest of the shape: a newer
        // version may have changed fields we would otherwise fail to parse and
        // then wrongly quarantine.
        if let Ok(peek) = serde_json::from_str::<SchemaPeek>(&raw) {
            if peek.schema > SCHEMA {
                return Err(LoadError::FutureSchema {
                    found: peek.schema,
                    supported: SCHEMA,
                });
            }
        }

        match serde_json::from_str::<State>(&raw) {
            Ok(state) => Ok((state, Origin::Existing)),
            Err(e) => {
                let reason = format!("state file is not valid state: {e}");
                let moved_to = quarantine(paths, &reason)?;
                Ok((Self::default(), Origin::Quarantined { moved_to, reason }))
            }
        }
    }

    /// Close an open run left behind by a process that died.
    ///
    /// Returns the id of the run that was reconciled, if there was one. It
    /// does **not** attempt to resume: resuming a half-finished backup is
    /// worse than starting a new one.
    pub fn reconcile(&mut self) -> Option<u64> {
        let mut run = self.last_run.take()?;
        if run.finished_at.is_some() {
            // Already closed, merely not yet rotated into history. Nothing was
            // interrupted, so nothing is reported.
            self.push_history(run);
            return None;
        }
        let id = run.id;
        run.outcome = Some(Outcome::Interrupted);
        self.push_history(run);
        Some(id)
    }

    /// Move a finished run into history, newest first, bounded.
    pub fn push_history(&mut self, run: Run) {
        self.history.insert(0, run);
        self.history.truncate(MAX_HISTORY);
    }

    /// Persist atomically.
    ///
    /// The full sequence, because skipping any step of it is how "atomic"
    /// writes turn out not to be:
    ///
    /// 1. create a temp file in the **same directory** -- `rename(2)` is only
    ///    atomic within one filesystem;
    /// 2. write;
    /// 3. flush and `fsync` the temp file, so the bytes are on disk and not
    ///    just in page cache -- without this, a power loss can land the new
    ///    name on top of empty blocks;
    /// 4. `rename` over the target, which is atomic;
    /// 5. `fsync` the **directory**, so the rename itself is durable.
    ///
    /// After a crash at any point: the previous complete version, or the new
    /// complete version. Never a partial write.
    pub fn save(&self, paths: &Paths) -> io::Result<()> {
        paths.ensure_dir()?;
        let target = paths.state_file();
        let tmp = paths
            .dir()
            .join(format!("state.json.tmp.{}", std::process::id()));

        let body = serde_json::to_vec_pretty(self)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;

        // Scoped so the temp file is closed before the rename.
        let written = (|| -> io::Result<()> {
            let mut f = OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .mode(FILE_MODE)
                .open(&tmp)?;
            f.write_all(&body)?;
            f.write_all(b"\n")?;
            f.flush()?;
            f.sync_all()
        })();

        if let Err(e) = written {
            let _ = std::fs::remove_file(&tmp);
            return Err(e);
        }

        if let Err(e) = std::fs::rename(&tmp, &target) {
            let _ = std::fs::remove_file(&tmp);
            return Err(e);
        }

        // Durability of the rename itself. On Linux, `fsync` on a directory
        // descriptor is the supported way to do this.
        File::open(paths.dir())?.sync_all()
    }
}

#[derive(Deserialize)]
struct SchemaPeek {
    schema: u32,
}

/// Move an unusable state file aside. Never overwrites an existing
/// quarantine: the first corruption is usually the interesting one.
fn quarantine(paths: &Paths, reason: &str) -> io::Result<PathBuf> {
    let from = paths.state_file();
    let stamp = crate::now();
    let mut to = paths.dir().join(format!("state.json.corrupt.{stamp}"));
    let mut n = 1;
    while to.exists() {
        to = paths.dir().join(format!("state.json.corrupt.{stamp}.{n}"));
        n += 1;
    }
    std::fs::rename(&from, &to)?;
    crate::warn!("state quarantined to {}: {reason}", to.display());
    Ok(to)
}
