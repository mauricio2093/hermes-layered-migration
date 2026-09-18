//! Running a child process without losing control of it.
//!
//! Nothing here runs a real maintenance script: this slice exists to get the
//! mechanism right against deliberately inocuous fixtures, before anything
//! that matters is handed to it.
//!
//! ```text
//! preflight -> spawn (own process group) -> drain both pipes concurrently
//!           -> wait until the deadline
//!           -> SIGTERM the group -> grace -> SIGKILL the group
//!           -> wait ALWAYS
//! ```
//!
//! Three invariants the rest of the module exists to keep:
//!
//! 1. **No shell, ever.** A program and an argv vector, never a string handed
//!    to `/bin/sh`. There is no code path that can construct one.
//! 2. **"SIGTERM sent" is not "process finished".** Only `wait` says that, and
//!    it is always called, on every path, so nothing is left a zombie.
//! 3. **The pipes are drained while the child runs**, not after it exits. A
//!    child that fills a pipe blocks in `write`; a parent that waits before
//!    reading never gets to unblock it. That is a deadlock, and it is the
//!    classic way supervisors like this one hang at 03:00.

use std::collections::VecDeque;
use std::ffi::{OsStr, OsString};
use std::io::Read;
use std::os::unix::fs::MetadataExt;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

// --- limits -----------------------------------------------------------------

/// Kept per stream. A misbehaving child can print gigabytes; holding them in
/// memory would turn its bug into ours.
pub const MAX_CAPTURE_BYTES: usize = 64 * 1024;

/// **The tail is kept, not the head.** A child killed on its deadline has its
/// most recent activity at the end; a failing script's error message is at the
/// end; and the first kilobyte of a long run is usually a banner. The total
/// byte count is recorded separately, so nothing about the size is lost.
pub const KEEP_TAIL: bool = true;

/// How long to wait for a pipe to reach EOF after the child has been reaped.
/// Normally instant: killing the process group closes the write ends. See
/// [`Captured::complete`] for the case where it does not.
const DRAIN_GRACE: Duration = Duration::from_secs(2);

/// A `SIGKILL`ed process is reaped promptly unless it is stuck in an
/// uninterruptible kernel operation. This bounds that wait so the supervisor
/// itself can never hang forever.
const REAP_GRACE: Duration = Duration::from_secs(10);

/// The child's entire environment. Nothing is inherited.
///
/// The parent's environment holds API keys, tokens, `SSH_AUTH_SOCK`, cloud
/// credentials and every Hermes variable. A child that needs one of those
/// should be given it deliberately, one variable at a time, with a reason --
/// not handed all of them because inheriting was easier.
pub const BASE_ENV: &[(&str, &str)] = &[
    (
        "PATH",
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    ),
    // Deterministic output: a locale-dependent child produces
    // locale-dependent logs, and then a report means different things on
    // different machines.
    ("LC_ALL", "C"),
    ("LANG", "C"),
    ("TZ", "UTC"),
    ("NO_COLOR", "1"),
];

// --- what to run ------------------------------------------------------------

/// A structured, compiled-in description of a child. There is no field here
/// that can carry a command string.
///
/// Configuration will eventually be able to enable or disable a task and move
/// its timeout within bounds. It will never be able to supply `program`,
/// `argv` or `cwd`: those come from the registry, which is Rust source.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChildSpec {
    /// Stable identifier, for logs and state.
    pub id: String,
    /// Absolute path to the executable. Never looked up on `PATH`.
    pub program: PathBuf,
    /// Passed through literally. Nothing interprets these -- not a shell, not
    /// this module.
    pub argv: Vec<OsString>,
    /// Working directory. Must already exist.
    pub cwd: PathBuf,
    /// How long the child may run before it is asked to stop.
    pub timeout: Duration,
    /// How long it has between `SIGTERM` and `SIGKILL`.
    pub grace: Duration,
    /// Variables added on top of [`BASE_ENV`], for a child that genuinely
    /// needs one.
    ///
    /// Compiled in, like everything else on a spec, and **derived rather than
    /// inherited**: the point is not to reach into the parent's environment
    /// for a value, it is to construct the one value this child needs. A task
    /// that wants `XDG_RUNTIME_DIR` computes it from the effective uid; it
    /// does not copy ours, which might be missing, stale or someone else's.
    pub env: Vec<(OsString, OsString)>,
}

impl ChildSpec {
    #[must_use]
    pub fn new(
        id: impl Into<String>,
        program: impl Into<PathBuf>,
        cwd: impl Into<PathBuf>,
        timeout: Duration,
        grace: Duration,
    ) -> Self {
        Self {
            id: id.into(),
            program: program.into(),
            argv: Vec::new(),
            cwd: cwd.into(),
            timeout,
            grace,
            env: Vec::new(),
        }
    }

    /// Add one variable on top of [`BASE_ENV`]. See [`ChildSpec::env`].
    #[must_use]
    pub fn env(mut self, key: impl AsRef<OsStr>, value: impl AsRef<OsStr>) -> Self {
        self.env
            .push((key.as_ref().to_os_string(), value.as_ref().to_os_string()));
        self
    }

    #[must_use]
    pub fn arg(mut self, a: impl AsRef<OsStr>) -> Self {
        self.argv.push(a.as_ref().to_os_string());
        self
    }

    #[must_use]
    pub fn args<I, S>(mut self, args: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<OsStr>,
    {
        for a in args {
            self.argv.push(a.as_ref().to_os_string());
        }
        self
    }
}

// --- preflight ---------------------------------------------------------------

/// Why a child was refused before it was started.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Refusal {
    ProgramMissing,
    ProgramIsSymlink,
    ProgramNotRegularFile,
    ProgramNotExecutable {
        mode: u32,
    },
    /// Writable by group or other. A maintenance program anyone can rewrite is
    /// a hole the moment anything privileged runs it.
    ProgramWritableByOthers {
        mode: u32,
    },
    /// Owned by neither the invoking user nor root.
    ProgramForeignOwner {
        uid: u32,
    },
    ProgramUnreadable(String),
    CwdMissing,
    CwdNotADirectory,
    CwdIsSymlink,
}

impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Refusal::ProgramMissing => f.write_str("the program does not exist"),
            Refusal::ProgramIsSymlink => f.write_str("the program is a symlink"),
            Refusal::ProgramNotRegularFile => f.write_str("the program is not a regular file"),
            Refusal::ProgramNotExecutable { mode } => {
                write!(f, "the program is not executable (mode {mode:o})")
            }
            Refusal::ProgramWritableByOthers { mode } => {
                write!(
                    f,
                    "the program is writable by group or other (mode {mode:o})"
                )
            }
            Refusal::ProgramForeignOwner { uid } => {
                write!(f, "the program is owned by uid {uid}")
            }
            Refusal::ProgramUnreadable(e) => write!(f, "the program could not be examined: {e}"),
            Refusal::CwdMissing => f.write_str("the working directory does not exist"),
            Refusal::CwdNotADirectory => f.write_str("the working directory is not a directory"),
            Refusal::CwdIsSymlink => f.write_str("the working directory is a symlink"),
        }
    }
}

/// Validate a spec without running anything.
///
/// # What this does not do
///
/// It does **not** close the time-of-check/time-of-use race, and pretending
/// otherwise would be worse than the race itself. Checking a path and then
/// executing it are two operations; between them the file could in principle
/// be replaced.
///
/// Closing it properly would mean holding an `O_PATH` descriptor from the
/// check through to `fexecve`, which `std::process::Command` cannot express --
/// and reimplementing fork/exec by hand would cost the atomic process-group
/// creation and the pipe handling that this module depends on. That trade is
/// not worth making here, for reasons that are specific and worth stating:
///
/// - every path comes from a **compiled-in registry**, not from configuration,
///   a file, or anything a user typed;
/// - the checks below already require the file to be un-writable by group and
///   other, and owned by this user or root;
/// - so the only actor who can win the race is one who can already write files
///   owned by this user -- who, per the design's threat model, can equally well
///   edit the systemd unit, the binary, or `.bashrc`, and does not need a race.
///
/// The window is real. It is just not the weakest thing in the picture.
pub fn preflight(spec: &ChildSpec) -> Result<(), Refusal> {
    // `symlink_metadata` does not traverse, so a symlink is seen as one.
    let meta = match spec.program.symlink_metadata() {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Err(Refusal::ProgramMissing),
        Err(e) => return Err(Refusal::ProgramUnreadable(e.to_string())),
    };

    if meta.file_type().is_symlink() {
        return Err(Refusal::ProgramIsSymlink);
    }
    if !meta.is_file() {
        return Err(Refusal::ProgramNotRegularFile);
    }

    let mode = meta.mode() & 0o7777;
    if mode & 0o111 == 0 {
        return Err(Refusal::ProgramNotExecutable { mode });
    }
    if mode & 0o022 != 0 {
        return Err(Refusal::ProgramWritableByOthers { mode });
    }

    // SAFETY: `geteuid` takes no arguments and cannot fail.
    let me = unsafe { libc::geteuid() };
    let owner = meta.uid();
    // Root-owned programs are accepted: that is how a package manager installs
    // one, and a root-owned file this user cannot write is not a way in.
    if owner != me && owner != 0 {
        return Err(Refusal::ProgramForeignOwner { uid: owner });
    }

    match spec.cwd.symlink_metadata() {
        Ok(m) if m.file_type().is_symlink() => Err(Refusal::CwdIsSymlink),
        Ok(m) if m.is_dir() => Ok(()),
        Ok(_) => Err(Refusal::CwdNotADirectory),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err(Refusal::CwdMissing),
        Err(e) => Err(Refusal::ProgramUnreadable(e.to_string())),
    }
}

// --- what came back -----------------------------------------------------------

/// One captured stream.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Captured {
    /// At most [`MAX_CAPTURE_BYTES`]; the **tail** of what was written.
    pub bytes: Vec<u8>,
    /// Everything the child wrote, including what was dropped.
    pub total_bytes: u64,
    /// The cap was reached and earlier output was discarded.
    pub truncated: bool,
    /// The pipe reached EOF. `false` means something still held the write end
    /// after the child was reaped -- a descendant that escaped the process
    /// group -- and the supervisor stopped waiting rather than hang.
    pub complete: bool,
}

impl Captured {
    #[must_use]
    pub fn text(&self) -> std::borrow::Cow<'_, str> {
        String::from_utf8_lossy(&self.bytes)
    }
}

/// How a child ended. Kept distinct on purpose: collapsing these into "failed"
/// throws away exactly the information that decides what to do next.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    /// Ran to completion. `exit_code` says how it went.
    Exited,
    /// Outlived its deadline and was stopped. `term_sent` and `kill_sent` say
    /// how hard that was.
    TimedOut,
    /// Died on a signal the supervisor did not send -- a segfault, an external
    /// `kill`, the OOM killer.
    Signalled,
    /// Never started.
    SpawnFailed(String),
    /// Never started, on purpose.
    Refused(Refusal),
}

#[derive(Debug, Clone)]
pub struct ChildResult {
    pub id: String,
    pub outcome: Outcome,
    /// The child's pid, for diagnostics. Also its process group id, since the
    /// child leads its own group.
    pub pid: Option<u32>,
    /// `None` when the child died on a signal or never ran.
    pub exit_code: Option<i32>,
    /// The signal that ended it, whoever sent it.
    pub signal: Option<i32>,
    /// Measured with a monotonic clock.
    pub duration: Duration,
    pub timed_out: bool,
    pub term_sent: bool,
    pub kill_sent: bool,
    pub stdout: Captured,
    pub stderr: Captured,
}

impl ChildResult {
    fn refused(spec: &ChildSpec, why: Refusal) -> Self {
        Self {
            id: spec.id.clone(),
            outcome: Outcome::Refused(why),
            pid: None,
            exit_code: None,
            signal: None,
            duration: Duration::ZERO,
            timed_out: false,
            term_sent: false,
            kill_sent: false,
            stdout: Captured::default(),
            stderr: Captured::default(),
        }
    }

    /// Ran and exited zero.
    #[must_use]
    pub fn succeeded(&self) -> bool {
        matches!(self.outcome, Outcome::Exited) && self.exit_code == Some(0)
    }

    /// A one-line summary for a log.
    #[must_use]
    pub fn summary(&self) -> String {
        let how = match &self.outcome {
            Outcome::Exited => match self.exit_code {
                Some(0) => "ok".to_string(),
                Some(c) => format!("exit {c}"),
                None => "exited".to_string(),
            },
            Outcome::TimedOut => {
                let how = if self.kill_sent {
                    "SIGKILL after SIGTERM"
                } else if self.term_sent {
                    "SIGTERM"
                } else {
                    "stopped"
                };
                format!("timed out, {how}")
            }
            Outcome::Signalled => format!("killed by signal {}", self.signal.unwrap_or(0)),
            Outcome::SpawnFailed(e) => format!("could not start: {e}"),
            Outcome::Refused(r) => format!("refused: {r}"),
        };
        format!("{} {how} in {:.1}s", self.id, self.duration.as_secs_f64())
    }
}

// --- running it ---------------------------------------------------------------

/// Run a child to completion, or stop it trying.
///
/// Always returns: a refusal and a spawn failure are results, not errors, so
/// there is one shape for the caller to record.
#[must_use]
pub fn supervise(spec: &ChildSpec) -> ChildResult {
    if let Err(why) = preflight(spec) {
        return ChildResult::refused(spec, why);
    }

    let mut command = Command::new(&spec.program);
    command
        .args(&spec.argv)
        .current_dir(&spec.cwd)
        // Nothing is inherited. See BASE_ENV.
        .env_clear()
        // A maintenance task must never be able to block on a prompt at 03:00.
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        // Set as an attribute of the spawn itself, so the child is already in
        // its own group before `exec`. Doing it from the parent afterwards is
        // a race: the child may have exec'd, or spawned a grandchild, first.
        .process_group(0);
    for (k, v) in BASE_ENV {
        command.env(k, v);
    }
    // Applied after the base, so a spec can override as well as add.
    for (k, v) in &spec.env {
        command.env(k, v);
    }

    // The clock starts at the spawn and is monotonic: an NTP step during a
    // child must not lengthen or shorten its deadline.
    let started = Instant::now();

    let mut child = match command.spawn() {
        Ok(c) => c,
        Err(e) => {
            let mut r = ChildResult::refused(spec, Refusal::ProgramMissing);
            r.outcome = Outcome::SpawnFailed(e.to_string());
            return r;
        }
    };

    let pid = child.id();
    let out_rx = drain_in_background(child.stdout.take());
    let err_rx = drain_in_background(child.stderr.take());

    // The child is moved into a thread that does nothing but `wait`. That is
    // what reaps it -- on every path below, including both kill paths.
    let (status_tx, status_rx) = mpsc::channel();
    let waiter = std::thread::spawn(move || {
        let status = child.wait();
        let _ = status_tx.send(status);
    });

    let mut term_sent = false;
    let mut kill_sent = false;
    let mut timed_out = false;

    let status = match status_rx.recv_timeout(spec.timeout) {
        Ok(s) => Some(s),
        Err(mpsc::RecvTimeoutError::Timeout) => {
            timed_out = true;
            // The whole group, not the pid: a script's grandchildren are the
            // processes that outlive a careless kill.
            signal_group(pid, libc::SIGTERM);
            term_sent = true;

            match status_rx.recv_timeout(spec.grace) {
                Ok(s) => Some(s),
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    signal_group(pid, libc::SIGKILL);
                    kill_sent = true;
                    // Bounded, so the supervisor itself cannot hang forever on
                    // a process wedged in the kernel.
                    status_rx.recv_timeout(REAP_GRACE).ok()
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => None,
            }
        }
        Err(mpsc::RecvTimeoutError::Disconnected) => None,
    };

    let duration = started.elapsed();
    let _ = waiter.join();

    let (exit_code, signal) = match &status {
        Some(Ok(s)) => (s.code(), s.signal()),
        _ => (None, None),
    };

    let outcome = if timed_out {
        Outcome::TimedOut
    } else if signal.is_some() {
        Outcome::Signalled
    } else {
        Outcome::Exited
    };

    ChildResult {
        id: spec.id.clone(),
        outcome,
        pid: Some(pid),
        exit_code,
        signal,
        duration,
        timed_out,
        term_sent,
        kill_sent,
        stdout: collect(out_rx),
        stderr: collect(err_rx),
    }
}

/// Signal the child's whole process group.
///
/// The child leads its own group, so its pid is the group id. Errors are
/// ignored deliberately: the only interesting one is `ESRCH`, which means the
/// group is already gone -- exactly what was wanted.
fn signal_group(pid: u32, sig: libc::c_int) {
    // SAFETY: `killpg` takes a group id and a signal; a bad group id returns
    // an error rather than doing anything.
    unsafe {
        libc::killpg(pid as libc::pid_t, sig);
    }
}

/// Start reading a pipe immediately, on its own thread.
///
/// This is the deadlock fix, and it has to happen before the parent starts
/// waiting: a child that fills a pipe blocks in `write`, and a parent that
/// waits before reading never unblocks it.
fn drain_in_background<R>(stream: Option<R>) -> Option<mpsc::Receiver<Captured>>
where
    R: Read + Send + 'static,
{
    let stream = stream?;
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(drain(stream, MAX_CAPTURE_BYTES));
    });
    Some(rx)
}

fn collect(rx: Option<mpsc::Receiver<Captured>>) -> Captured {
    let Some(rx) = rx else {
        return Captured::default();
    };
    match rx.recv_timeout(DRAIN_GRACE) {
        Ok(c) => c,
        // Something still holds the write end after the child was reaped: a
        // descendant that escaped the process group. Report what is known
        // rather than wait forever; the thread ends when the pipe closes.
        Err(_) => Captured {
            complete: false,
            ..Captured::default()
        },
    }
}

/// Read to EOF, keeping only the last `cap` bytes.
fn drain<R: Read>(mut reader: R, cap: usize) -> Captured {
    let mut buf = vec![0u8; 8192];
    let mut keep: VecDeque<u8> = VecDeque::new();
    let mut total: u64 = 0;
    let mut truncated = false;

    loop {
        match reader.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                total += n as u64;
                let chunk = &buf[..n];
                if n >= cap {
                    // This read alone overflows the window: nothing older can
                    // survive it.
                    keep.clear();
                    keep.extend(&chunk[n - cap..]);
                    truncated = true;
                } else {
                    while keep.len() + n > cap {
                        keep.pop_front();
                        truncated = true;
                    }
                    keep.extend(chunk);
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }

    Captured {
        bytes: keep.into_iter().collect(),
        total_bytes: total,
        truncated,
        complete: true,
    }
}

/// Where a program must live for the registry to reference it. Nothing in this
/// slice uses it yet; it is here so the first external task has one place to
/// derive its paths from, rather than inventing another.
#[must_use]
pub fn scripts_dir(hermes_home: &Path) -> PathBuf {
    hermes_home.join("scripts")
}
