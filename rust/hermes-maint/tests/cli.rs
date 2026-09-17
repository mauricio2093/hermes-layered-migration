//! End to end: the exit codes are the contract, so they are tested through
//! the real binary rather than through the library.

use std::path::PathBuf;
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU32, Ordering};

use hermes_maint_core::lock::Lock;
use hermes_maint_core::paths::Paths;
use hermes_maint_core::state::SCHEMA;

const BIN: &str = env!("CARGO_BIN_EXE_hermes-maint");

static COUNTER: AtomicU32 = AtomicU32::new(0);

struct TempHome {
    root: PathBuf,
}

impl TempHome {
    fn new(label: &str) -> Self {
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!(
            "hermes-maint-cli-{}-{label}-{n}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).expect("temp home");
        Self { root }
    }

    fn paths(&self) -> Paths {
        Paths::under(&self.root)
    }

    /// Run the real binary against this home, with a cleared environment so a
    /// stray `HERMES_HOME` on the developer's machine cannot reach the test.
    fn run(&self, args: &[&str]) -> Output {
        Command::new(BIN)
            .args(args)
            .env_clear()
            .env("HERMES_HOME", &self.root)
            .output()
            .expect("spawn hermes-maint")
    }
}

impl Drop for TempHome {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

fn code(out: &Output) -> i32 {
    out.status
        .code()
        .expect("the process must not be signalled")
}

#[test]
fn a_plain_run_succeeds_and_leaves_state() {
    let home = TempHome::new("ok");
    let out = home.run(&["run"]);
    assert_eq!(code(&out), 0, "{}", String::from_utf8_lossy(&out.stderr));
    assert!(home.paths().state_file().exists());
}

#[test]
fn repeated_runs_accumulate_history_without_overlapping() {
    let home = TempHome::new("repeat");
    for _ in 0..3 {
        assert_eq!(code(&home.run(&["run", "--trigger", "timer"])), 0);
    }
    let body = std::fs::read_to_string(home.paths().state_file()).unwrap();
    let state: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(state["next_run_id"], 4);
    assert_eq!(state["history"].as_array().unwrap().len(), 3);
    assert!(state["last_run"].is_null(), "no run may be left open");
}

#[test]
fn a_dry_run_writes_nothing() {
    let home = TempHome::new("dry");
    let out = home.run(&["run", "--dry-run"]);
    assert_eq!(code(&out), 0);
    assert!(!home.paths().state_file().exists());
}

/// Exit 3, and the reason the unit declares `SuccessExitStatus=3`: a working
/// lock must never look like a failure.
#[test]
fn a_held_lock_exits_three() {
    let home = TempHome::new("busy");
    let paths = home.paths();
    paths.ensure_dir().unwrap();
    let _held = Lock::acquire(paths.lock_file()).expect("hold the lock");

    let out = home.run(&["run", "--trigger", "timer"]);
    assert_eq!(code(&out), 3, "{}", String::from_utf8_lossy(&out.stderr));
    assert!(
        !paths.state_file().exists(),
        "a blocked run must not touch state"
    );
}

#[test]
fn state_from_a_newer_version_exits_seven_and_is_preserved() {
    let home = TempHome::new("future");
    let paths = home.paths();
    paths.ensure_dir().unwrap();
    let planted = format!(r#"{{"schema": {}}}"#, SCHEMA + 1);
    std::fs::write(paths.state_file(), &planted).unwrap();

    let out = home.run(&["run"]);
    assert_eq!(
        code(&out),
        7,
        "incompatible state gets its own code: it is not a typo at the prompt"
    );
    assert_eq!(
        std::fs::read_to_string(paths.state_file()).unwrap(),
        planted,
        "state written by a newer version must be left alone"
    );
}

#[test]
fn bad_arguments_exit_two() {
    let home = TempHome::new("args");
    for args in [
        vec!["run", "--nope"],
        vec!["run", "--trigger", "cron"],
        vec!["run", "--trigger"],
        vec!["sudo-everything"],
    ] {
        let out = home.run(&args);
        assert_eq!(code(&out), 2, "for {args:?}");
    }
}

#[test]
fn help_and_version_succeed() {
    let home = TempHome::new("help");
    for args in [vec!["--help"], vec!["-h"], vec!["--version"], vec![]] {
        let out = home.run(&args);
        assert_eq!(code(&out), 0, "for {args:?}");
    }
    let out = home.run(&["--version"]);
    assert!(String::from_utf8_lossy(&out.stdout).starts_with("hermes-maint "));
}

/// The design says this binary opens no socket and makes no network call.
/// Nothing here can prove a negative, but the absence of any networking
/// symbol in the dependency tree is a cheap, real check.
#[test]
fn nothing_in_the_build_talks_to_a_network() {
    let manifest = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml"))
        .expect("read manifest");
    for forbidden in ["reqwest", "hyper", "tokio", "ureq", "curl"] {
        assert!(
            !manifest.contains(forbidden),
            "{forbidden} has no business in a maintenance binary"
        );
    }
}
