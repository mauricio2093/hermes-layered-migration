//! Where things live.
//!
//! Everything `hermes-maint` owns is under one directory, which is what makes
//! §13 of the design (uninstall) a `rm -rf` of a single path rather than a
//! hunt.

use std::fs::DirBuilder;
use std::io;
use std::os::unix::fs::DirBuilderExt;
use std::path::{Path, PathBuf};

/// Directory mode. Nothing here is other people's business.
pub const DIR_MODE: u32 = 0o700;
/// File mode, applied at creation so a file is never briefly world-readable.
pub const FILE_MODE: u32 = 0o600;

const STATE_DIR_NAME: &str = "hermes-maint";
const STATE_FILE_NAME: &str = "state.json";
const LOCK_FILE_NAME: &str = "lock";

/// Resolved locations for one installation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Paths {
    hermes_home: PathBuf,
    dir: PathBuf,
}

impl Paths {
    /// Resolve from the environment: `HERMES_HOME`, else `~/.hermes`.
    ///
    /// The unit passes `HERMES_HOME` explicitly, exactly as the gateway unit
    /// already does, so the two never disagree about which install they mean.
    pub fn from_env() -> io::Result<Self> {
        let home = match std::env::var_os("HERMES_HOME") {
            Some(v) if !v.is_empty() => PathBuf::from(v),
            _ => {
                let h = std::env::var_os("HOME").ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::NotFound,
                        "neither HERMES_HOME nor HOME is set",
                    )
                })?;
                PathBuf::from(h).join(".hermes")
            }
        };
        Ok(Self::under(home))
    }

    /// Resolve under an explicit Hermes home. Used by tests, which must never
    /// touch the real one.
    #[must_use]
    pub fn under(hermes_home: impl Into<PathBuf>) -> Self {
        let hermes_home = hermes_home.into();
        let dir = hermes_home.join(STATE_DIR_NAME);
        Self { hermes_home, dir }
    }

    #[must_use]
    pub fn hermes_home(&self) -> &Path {
        &self.hermes_home
    }

    /// The one directory this tool owns.
    #[must_use]
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    #[must_use]
    pub fn state_file(&self) -> PathBuf {
        self.dir.join(STATE_FILE_NAME)
    }

    #[must_use]
    pub fn lock_file(&self) -> PathBuf {
        self.dir.join(LOCK_FILE_NAME)
    }

    /// Create the state directory if it is missing, with restrictive mode.
    ///
    /// `mode` only applies to directories this call creates; an existing
    /// directory is left as it is. Silently tightening permissions on a
    /// directory somebody else made would be a surprise, and surprises at
    /// 03:00 are expensive.
    pub fn ensure_dir(&self) -> io::Result<()> {
        if self.dir.is_dir() {
            return Ok(());
        }
        DirBuilder::new()
            .recursive(true)
            .mode(DIR_MODE)
            .create(&self.dir)
    }
}
