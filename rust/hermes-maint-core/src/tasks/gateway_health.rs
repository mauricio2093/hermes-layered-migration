//! Is the gateway's systemd unit in the state this host expects?
//!
//! The first task that runs an external program against the real system, and
//! it is still **strictly read-only**: one `systemctl --user show`, which
//! queries and prints. There is no `start`, no `stop`, no `restart`, no
//! `enable`, no `daemon-reload`, and no code path that could construct one --
//! the subcommand and its arguments are compiled in.
//!
//! # Why the exit code is not the answer
//!
//! This task exists because of a fact verified on the machine before any of it
//! was written:
//!
//! ```text
//! $ systemctl --user show no-such-unit.service --property=LoadState ...
//! LoadState=not-found
//! ActiveState=inactive
//! SubState=dead
//! $ echo $?
//! 0
//! ```
//!
//! `systemctl` succeeded. It did exactly what it was asked. The *news* is bad.
//! A task that read the exit status alone would report a missing gateway as
//! perfectly healthy, which is the precise failure mode this whole design is
//! supposed to avoid.
//!
//! So: the process working and the service being healthy are two different
//! questions, answered in two different places -- [`supervise`] answers the
//! first, [`judge_service_state`] answers the second.

use std::collections::BTreeMap;
use std::path::PathBuf;

use crate::paths::Paths;
use crate::state::{clamp_detail, TaskOutcome};
use crate::supervisor::{ChildResult, ChildSpec, Outcome};
use crate::task::{Observation, TaskReport};
use crate::tasks::external::ExternalTask;

/// Verified on this host: a regular file, `root:root`, mode `755`, not a
/// symlink -- so it passes the pre-flight, including the rule that accepts a
/// root-owned program this user cannot write.
pub const SYSTEMCTL: &str = "/usr/bin/systemctl";

/// The real unit name, read off the running system rather than assumed:
/// `systemctl --user list-unit-files` reports it `enabled`, its
/// `FragmentPath` is under `~/.config/systemd/user/`, and its `MainPID` is the
/// gateway process.
pub const UNIT: &str = "hermes-gateway.service";

/// The healthy state, observed rather than guessed.
pub const HEALTHY: (&str, &str, &str) = ("loaded", "active", "running");

/// `show` only. Nothing here changes anything.
pub const ARGS: &[&str] = &[
    "--user",
    "show",
    UNIT,
    "--property=LoadState",
    "--property=ActiveState",
    "--property=SubState",
    "--no-pager",
];

/// Generous: a single `show` is milliseconds, and a slow one means the user
/// manager is in trouble -- which is worth waiting a moment to find out.
pub const TIMEOUT_SECONDS: u64 = 15;
pub const GRACE_SECONDS: u64 = 3;

/// The one variable this child needs, **derived and not inherited**.
///
/// `systemctl --user` has to reach the user manager's bus. With the
/// supervisor's minimal environment it cannot, and says so:
///
/// ```text
/// Failed to connect to user scope bus via local transport:
/// $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined
/// ```
///
/// Verified on this host that `XDG_RUNTIME_DIR` alone is enough. It is
/// computed from the effective uid rather than copied from our own
/// environment: ours might be absent under a timer, stale, or someone else's.
#[must_use]
pub fn runtime_dir() -> PathBuf {
    // SAFETY: `geteuid` takes no arguments and cannot fail.
    let uid = unsafe { libc::geteuid() };
    PathBuf::from(format!("/run/user/{uid}"))
}

// --- the parsed answer -------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceState {
    pub load_state: String,
    pub active_state: String,
    pub sub_state: String,
}

/// The output could not be understood. Distinct from "the service is unwell":
/// one means the observation failed, the other means it succeeded and the news
/// is bad.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParseError {
    /// A property `systemctl` was asked for is not in the output at all.
    Missing(&'static str),
    /// A property appears twice with different values. `systemctl` does not do
    /// this; something that does is not `systemctl`, so nothing here is
    /// trusted.
    Contradictory(&'static str),
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ParseError::Missing(k) => write!(f, "{k} missing from the output"),
            ParseError::Contradictory(k) => write!(f, "{k} reported twice with different values"),
        }
    }
}

const REQUIRED: [&str; 3] = ["LoadState", "ActiveState", "SubState"];

/// Parse `KEY=VALUE` lines.
///
/// Tolerant about **shape** and strict about **content**: any order, with or
/// without a trailing newline, and lines that are not `KEY=VALUE` or carry a
/// key we did not ask for are ignored -- `systemctl` may grow new properties
/// and that must not break anything. But a required key that is absent, or
/// present twice with different values, is a parse error rather than a guess.
///
/// A required key present with an **empty** value is not a parse error: the
/// structure is intact and the content is simply a state we cannot call
/// healthy, which [`judge_service_state`] handles.
pub fn parse(text: &str) -> Result<ServiceState, ParseError> {
    let mut seen: BTreeMap<&str, &str> = BTreeMap::new();

    for line in text.lines() {
        let line = line.trim_end_matches('\r');
        let Some((key, value)) = line.split_once('=') else {
            continue; // not a property line
        };
        let Some(&required) = REQUIRED.iter().find(|r| **r == key) else {
            continue; // a property we did not ask about
        };
        match seen.insert(required, value) {
            Some(previous) if previous != value => return Err(ParseError::Contradictory(required)),
            _ => {}
        }
    }

    let get = |k: &'static str| seen.get(k).copied().ok_or(ParseError::Missing(k));
    Ok(ServiceState {
        load_state: get("LoadState")?.to_string(),
        active_state: get("ActiveState")?.to_string(),
        sub_state: get("SubState")?.to_string(),
    })
}

/// The verdict, as a pure function over the parsed fields.
///
/// **Healthy is exactly one triple**, the one observed on a working host:
/// `loaded` / `active` / `running`. Everything else is `Degraded`.
///
/// That asymmetry is deliberate. A state we have never seen before is not
/// evidence of health, and defaulting an unknown value to `Ok` would mean a
/// future systemd substate silently turning a broken gateway into a green
/// report. The cost of being wrong the other way is one line in a log.
///
/// Nothing here is ever `Failed`: by the time this function is called, the
/// observation has already succeeded.
#[must_use]
pub fn judge_service_state(state: &ServiceState) -> Observation {
    let (load, active, sub) = (
        state.load_state.as_str(),
        state.active_state.as_str(),
        state.sub_state.as_str(),
    );

    if (load, active, sub) == HEALTHY {
        return Observation::Ok(format!("{UNIT} active (running)"));
    }

    // A more useful sentence for the cases worth recognising by name. Each one
    // still ends up Degraded; only the wording differs.
    let reason = match (load, active) {
        ("not-found", _) => {
            // A unit this host expects and does not have. That is a finding,
            // not a missing precondition -- see `docs/`.
            "the unit does not exist".to_string()
        }
        ("masked", _) => "the unit is masked".to_string(),
        ("error" | "bad-setting", _) => "the unit file could not be loaded".to_string(),
        (_, "failed") => format!("the service has failed ({sub})"),
        (_, "inactive") => format!("the service is not running ({sub})"),
        (_, "activating" | "deactivating" | "reloading") => {
            format!("the service is in transition ({active}/{sub})")
        }
        ("loaded", "active") => format!("active but in an unexpected substate ({sub})"),
        _ => "in a state this build does not recognise".to_string(),
    };

    Observation::Degraded(format!(
        "{UNIT} {reason}: LoadState={load} ActiveState={active} SubState={sub}"
    ))
}

// --- turning a finished child into a task report -------------------------------

/// This task's own reading of a supervised child.
///
/// It diverges from the generic [`crate::tasks::external::interpret`] in two
/// places, both on purpose:
///
/// | child | generic | here | why |
/// |---|---|---|---|
/// | exited 0 | `Ok` | **parse and judge** | the whole point: `systemctl` exits 0 while reporting a missing unit |
/// | refused by pre-flight | `Skipped` | **`Failed`** | this host demonstrably runs systemd -- it is how the gateway runs -- so being unable to execute `systemctl` is a broken observation, not an absent precondition |
///
/// A timeout stays `Timeout`: `systemctl show` hanging is its own distinct
/// symptom, and the run's exit code should say so.
#[must_use]
pub fn interpret_gateway(result: &ChildResult) -> TaskReport {
    let failed = |detail: String| TaskReport {
        outcome: TaskOutcome::Failed,
        detail: clamp_detail(&detail),
        exit: result.exit_code,
        signal: result.signal,
        output_bytes: Some(result.stdout.total_bytes + result.stderr.total_bytes),
    };

    match &result.outcome {
        Outcome::TimedOut => TaskReport {
            outcome: TaskOutcome::Timeout,
            detail: clamp_detail(&format!("could not observe {UNIT}: {}", result.summary())),
            exit: result.exit_code,
            signal: result.signal,
            output_bytes: Some(result.stdout.total_bytes + result.stderr.total_bytes),
        },
        Outcome::Refused(why) => failed(format!("could not run systemctl: {why}")),
        Outcome::SpawnFailed(e) => failed(format!("could not run systemctl: {e}")),
        Outcome::Signalled => failed(format!("systemctl was killed: {}", result.summary())),
        Outcome::Exited if result.exit_code != Some(0) => {
            // The commonest real case: the user manager could not be reached.
            // Its explanation is on stderr and is worth a short excerpt.
            let why = result.stderr.text();
            let why = why.trim().lines().next().unwrap_or("no explanation given");
            failed(format!(
                "systemctl exited {}: {why}",
                result.exit_code.unwrap_or(-1)
            ))
        }
        Outcome::Exited => {
            let text = result.stdout.text();
            match parse(&text) {
                Err(e) => failed(format!("could not interpret systemctl output: {e}")),
                Ok(state) => {
                    let observation = judge_service_state(&state);
                    let outcome = match &observation {
                        Observation::Ok(_) => TaskOutcome::Ok,
                        Observation::Degraded(_) => TaskOutcome::Degraded,
                        // Unreachable: judge never skips. Treated as degraded
                        // rather than silently promoted.
                        Observation::Skipped(_) => TaskOutcome::Degraded,
                    };
                    TaskReport {
                        outcome,
                        // Built from the parsed fields, never from raw output:
                        // there is no reason to carry kilobytes of properties
                        // into a file that keeps thirty runs.
                        detail: clamp_detail(observation.detail()),
                        exit: result.exit_code,
                        signal: None,
                        output_bytes: Some(result.stdout.total_bytes + result.stderr.total_bytes),
                    }
                }
            }
        }
    }
}

/// The registered task.
#[must_use]
pub fn task() -> ExternalTask {
    ExternalTask::new(
        "gateway-service-health",
        "the state systemd reports for the gateway unit",
        Box::new(|paths: &Paths| {
            ChildSpec::new(
                "systemctl-show",
                SYSTEMCTL,
                paths.hermes_home(),
                std::time::Duration::from_secs(TIMEOUT_SECONDS),
                std::time::Duration::from_secs(GRACE_SECONDS),
            )
            .args(ARGS)
            .env("XDG_RUNTIME_DIR", runtime_dir())
        }),
    )
    .with_interpreter(Box::new(interpret_gateway))
}
