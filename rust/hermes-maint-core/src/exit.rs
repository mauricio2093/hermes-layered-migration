//! Exit codes.
//!
//! Distinguishable on purpose: the journal should be queryable without
//! parsing prose. The unit declares `SuccessExitStatus=3`, so a contended
//! lock -- which is the lock working -- never shows up in `systemctl --failed`.

/// The process exit codes `hermes-maint` is allowed to return.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(i32)]
pub enum Exit {
    /// Everything ran and every check was fine.
    Ok = 0,
    /// A bug in `hermes-maint` itself.
    Internal = 1,
    /// Bad arguments, or configuration that could not be read.
    Misuse = 2,
    /// Another run holds the lock. Not a failure; see the module docs.
    LockBusy = 3,
    /// At least one task failed or was skipped.
    Partial = 4,
    /// At least one task was killed on its deadline.
    Timeout = 5,
    /// Everything ran; a health check reports degraded.
    Degraded = 6,
    /// The state file was written by a newer build. Distinct from `Misuse`
    /// on purpose: "you ran an old binary against new state" is a different
    /// problem from "you mistyped `--trigger`", and the whole reason these
    /// codes exist is to tell such things apart without parsing prose.
    IncompatibleState = 7,
}

impl Exit {
    #[must_use]
    pub const fn code(self) -> i32 {
        self as i32
    }

    /// Short label for logs. Not parsed by anything; the numeric code is the
    /// contract.
    #[must_use]
    pub const fn label(self) -> &'static str {
        match self {
            Exit::Ok => "ok",
            Exit::Internal => "internal-error",
            Exit::Misuse => "misuse",
            Exit::LockBusy => "lock-busy",
            Exit::Partial => "partial",
            Exit::Timeout => "timeout",
            Exit::Degraded => "degraded",
            Exit::IncompatibleState => "incompatible-state",
        }
    }
}
