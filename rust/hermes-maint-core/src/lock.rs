//! Single-instance lock.
//!
//! `flock(2)` with `LOCK_EX | LOCK_NB`, held for the whole run and released by
//! the kernel when the file descriptor closes -- which includes a clean exit,
//! a panic, `SIGKILL` and the OOM killer.
//!
//! That is the entire reason for choosing `flock` over a PID file: **there is
//! no such thing as a stale lock here.** A PID file after a hard kill has to
//! be disambiguated with liveness heuristics, and liveness heuristics are
//! where single-instance logic goes wrong -- a recycled PID makes a live lock
//! look dead, and the second instance runs.
//!
//! The lock file's *contents* are written for a human reading a diagnostic.
//! They are never consulted to decide whether the lock is held.

use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};

use crate::paths::FILE_MODE;

/// Why a lock could not be taken.
#[derive(Debug)]
pub enum LockError {
    /// Another run holds it. This is the lock working, not a failure.
    Busy { path: PathBuf },
    /// The lock file could not be opened or locked for some other reason.
    Io { path: PathBuf, source: io::Error },
}

impl std::fmt::Display for LockError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LockError::Busy { path } => {
                write!(f, "another run holds {}", path.display())
            }
            LockError::Io { path, source } => {
                write!(f, "could not lock {}: {source}", path.display())
            }
        }
    }
}

impl std::error::Error for LockError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            LockError::Busy { .. } => None,
            LockError::Io { source, .. } => Some(source),
        }
    }
}

/// An exclusive lock, released when this value is dropped.
#[derive(Debug)]
pub struct Lock {
    // Held purely for its file descriptor. Closing it releases the lock.
    file: File,
    path: PathBuf,
}

impl Lock {
    /// Try to take the lock without blocking.
    ///
    /// Never waits: a run that queues behind another run would eventually
    /// stampede, and at 03:00 nobody is watching. Contention means the work is
    /// already being done.
    pub fn acquire(path: impl AsRef<Path>) -> Result<Self, LockError> {
        let path = path.as_ref().to_path_buf();
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(FILE_MODE)
            .open(&path)
            .map_err(|source| LockError::Io {
                path: path.clone(),
                source,
            })?;

        flock_nb(&file).map_err(|source| match source.raw_os_error() {
            // On Linux EWOULDBLOCK and EAGAIN are the same value, so matching
            // one matches both. This is the "someone else has it" case.
            Some(libc::EWOULDBLOCK) => LockError::Busy { path: path.clone() },
            _ => LockError::Io {
                path: path.clone(),
                source,
            },
        })?;

        Ok(Self { file, path })
    }

    /// Record who holds it, for whoever reads the file while debugging.
    ///
    /// Written only after the lock is held, so this never races another
    /// holder. Failure is not fatal: a diagnostic that cannot be written is a
    /// worse diagnostic, not a worse lock.
    pub fn write_diagnostics(&mut self, trigger: &str, started_at: u64) {
        let pid = std::process::id();
        let body = format!("pid={pid}\ntrigger={trigger}\nstarted_at={started_at}\n");
        let mut write = || -> io::Result<()> {
            self.file.set_len(0)?;
            self.file.write_all(body.as_bytes())?;
            self.file.flush()
        };
        if let Err(e) = write() {
            crate::warn!("could not annotate the lock file: {e}");
        }
    }

    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }
}

// No `Drop` implementation, on purpose.
//
// Closing the file releases the lock, and `File` already does that. What this
// type must NOT do is unlink the lock file: between another process opening
// that path and taking its lock, an unlink would let a third process create a
// fresh file at the same path and lock *that* -- two holders, one path. The
// file is cheap; it stays.

fn flock_nb(file: &File) -> io::Result<()> {
    let fd = file.as_raw_fd();
    loop {
        // SAFETY: `fd` is a valid, open descriptor owned by `file`, which
        // outlives this call.
        let rc = unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) };
        if rc == 0 {
            return Ok(());
        }
        let err = io::Error::last_os_error();
        if err.kind() == io::ErrorKind::Interrupted {
            continue;
        }
        return Err(err);
    }
}
