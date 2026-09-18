//! The child-process supervisor, against fixtures that exist only for this.
//!
//! No system command is used as a fixture: `sleep`, `yes` and `cat` differ
//! between distributions, some are shell builtins, and none can be asked to
//! ignore `SIGTERM` or to report its own process group. `hm-fixture` can.
//!
//! Nothing here runs a real maintenance script.

use std::ffi::OsString;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use hermes_maint_core::supervisor::{
    preflight, supervise, ChildSpec, Outcome, Refusal, BASE_ENV, MAX_CAPTURE_BYTES,
};

const FIXTURE: &str = env!("CARGO_BIN_EXE_hm-fixture");

/// Scratch space on the **same filesystem as the build output**.
///
/// `CARGO_TARGET_TMPDIR` rather than `/tmp`, so that `hard_link` works between
/// the two. That matters more than it looks; see [`program`].
struct Scratch {
    root: PathBuf,
}

static COUNTER: AtomicU32 = AtomicU32::new(0);

impl Scratch {
    fn new(label: &str) -> Self {
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let root = PathBuf::from(env!("CARGO_TARGET_TMPDIR"))
            .join(format!("sup-{}-{label}-{n}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).expect("scratch");
        Self { root }
    }

    fn root(&self) -> &Path {
        &self.root
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

/// The built fixture, with permissions the pre-flight accepts.
///
/// This machine runs with `umask 002`, so a freshly built binary comes out
/// `0775` -- group-writable -- and the pre-flight refuses it. That is the
/// policy working, not a test problem: a program anyone in the group can
/// rewrite has no business running unattended. The real maintenance scripts
/// are `0700`, so they pass. The mode is fixed once here rather than by
/// weakening the rule.
fn fixture() -> &'static Path {
    static READY: OnceLock<PathBuf> = OnceLock::new();
    READY.get_or_init(|| {
        let p = PathBuf::from(FIXTURE);
        fs::set_permissions(&p, fs::Permissions::from_mode(0o755)).expect("chmod the fixture");
        p
    })
}

/// A second name for the fixture inside a scratch directory.
///
/// A **hard link**, not a copy, and the reason is a real race rather than
/// tidiness. `fs::copy` holds the destination open for writing; a sibling test
/// thread that forks during that window hands the descriptor to its transient
/// child, and `exec` of a file some process has open for writing fails with
/// `ETXTBSY`. That reproduced here at roughly two runs in fifteen. A hard link
/// never opens anything for writing, so the window does not exist.
fn program(scratch: &Scratch) -> PathBuf {
    let p = scratch.root().join("hm-fixture");
    // Idempotent: a test may build several specs from one scratch directory.
    if !p.exists() {
        fs::hard_link(fixture(), &p).expect("link the fixture");
    }
    p
}

/// Generous where it does not matter: these children are expected to exit on
/// their own, so the timeout should never be what ends them.
fn spec(scratch: &Scratch, args: &[&str]) -> ChildSpec {
    ChildSpec::new(
        "fixture",
        program(scratch),
        scratch.root(),
        Duration::from_secs(30),
        Duration::from_secs(1),
    )
    .args(args)
}

/// Short where it is the point.
fn timed_spec(scratch: &Scratch, args: &[&str], timeout_ms: u64, grace_ms: u64) -> ChildSpec {
    ChildSpec::new(
        "fixture",
        program(scratch),
        scratch.root(),
        Duration::from_millis(timeout_ms),
        Duration::from_millis(grace_ms),
    )
    .args(args)
}

/// A copy, for the tests that change the mode. These specs are always refused,
/// so the copy is never executed and `ETXTBSY` cannot apply.
fn copy_with_mode(scratch: &Scratch, name: &str, mode: u32) -> PathBuf {
    let p = scratch.root().join(name);
    fs::copy(fixture(), &p).expect("copy");
    fs::set_permissions(&p, fs::Permissions::from_mode(mode)).expect("chmod");
    p
}

// --- 1, 2: exit status -------------------------------------------------------

#[test]
fn a_child_that_exits_zero_succeeds() {
    let home = Scratch::new("sup-ok");
    let r = supervise(&spec(&home, &["exit", "0"]));

    assert_eq!(r.outcome, Outcome::Exited);
    assert_eq!(r.exit_code, Some(0));
    assert_eq!(r.signal, None);
    assert!(!r.timed_out && !r.term_sent && !r.kill_sent);
    assert!(r.succeeded());
    assert!(r.pid.is_some());
}

#[test]
fn a_nonzero_exit_is_reported_not_collapsed() {
    let home = Scratch::new("sup-fail");
    let r = supervise(&spec(&home, &["exit", "3"]));

    assert_eq!(
        r.outcome,
        Outcome::Exited,
        "it ran; it just did not like it"
    );
    assert_eq!(r.exit_code, Some(3));
    assert_eq!(r.signal, None);
    assert!(!r.succeeded());
    assert!(r.summary().contains("exit 3"), "{}", r.summary());
}

// --- 3, 4: capture -----------------------------------------------------------

#[test]
fn stdout_is_captured() {
    let home = Scratch::new("sup-out");
    let r = supervise(&spec(&home, &["echo-out", "hello", "from", "the", "child"]));

    assert_eq!(r.stdout.text(), "hello from the child");
    assert_eq!(r.stdout.total_bytes, 20);
    assert!(!r.stdout.truncated);
    assert!(r.stdout.complete);
    assert!(r.stderr.bytes.is_empty());
}

#[test]
fn stderr_is_captured_separately() {
    let home = Scratch::new("sup-err");
    let r = supervise(&spec(&home, &["echo-err", "something", "went", "wrong"]));

    assert_eq!(r.stderr.text(), "something went wrong");
    assert!(r.stdout.bytes.is_empty(), "the streams must not be merged");
}

// --- 5, 6, 20: too much output ------------------------------------------------

/// Byte `i` of the fixture's flood is `b'a' + (i % 26)`, so exactly which
/// slice survived truncation is checkable rather than approximate.
fn expected_byte(i: usize) -> u8 {
    b'a' + (i % 26) as u8
}

fn assert_is_tail(bytes: &[u8], total: usize) {
    assert_eq!(bytes.len(), MAX_CAPTURE_BYTES, "the cap is the cap");
    let first_kept = total - MAX_CAPTURE_BYTES;
    assert_eq!(
        bytes[0],
        expected_byte(first_kept),
        "the kept window must start where the tail starts"
    );
    assert_eq!(
        *bytes.last().unwrap(),
        expected_byte(total - 1),
        "the last byte the child wrote must survive: it is the one that says \
         what it was doing when it stopped"
    );
}

#[test]
fn a_flood_on_stdout_is_truncated_to_the_tail_without_deadlocking() {
    let home = Scratch::new("sup-flood-out");
    let total = MAX_CAPTURE_BYTES * 3 + 777;
    let started = Instant::now();
    let r = supervise(&spec(&home, &["flood-out", &total.to_string()]));

    assert_eq!(
        r.exit_code,
        Some(0),
        "it must finish, not wedge on a full pipe"
    );
    assert!(started.elapsed() < Duration::from_secs(20), "no deadlock");
    assert_eq!(r.stdout.total_bytes, total as u64);
    assert!(r.stdout.truncated);
    assert!(r.stdout.complete);
    assert_is_tail(&r.stdout.bytes, total);
}

#[test]
fn a_flood_on_stderr_is_truncated_to_the_tail_without_deadlocking() {
    let home = Scratch::new("sup-flood-err");
    let total = MAX_CAPTURE_BYTES * 2 + 13;
    let r = supervise(&spec(&home, &["flood-err", &total.to_string()]));

    assert_eq!(r.exit_code, Some(0));
    assert_eq!(r.stderr.total_bytes, total as u64);
    assert!(r.stderr.truncated);
    assert_is_tail(&r.stderr.bytes, total);
}

/// The classic hang: both pipes fill at once. Draining one after the other,
/// or either of them after `wait`, deadlocks here.
#[test]
fn both_pipes_flooding_at_once_does_not_deadlock() {
    let home = Scratch::new("sup-flood-both");
    let total = 4 * 1024 * 1024;
    let started = Instant::now();
    let r = supervise(&spec(&home, &["flood-both", &total.to_string()]));

    assert_eq!(r.exit_code, Some(0), "{}", r.summary());
    assert!(!r.timed_out, "a deadlock would have shown up as a timeout");
    assert!(started.elapsed() < Duration::from_secs(20));
    assert_eq!(r.stdout.total_bytes, total as u64);
    assert_eq!(r.stderr.total_bytes, total as u64);
    assert_is_tail(&r.stdout.bytes, total);
    assert_is_tail(&r.stderr.bytes, total);
}

#[test]
fn truncating_output_is_not_itself_a_failure() {
    let home = Scratch::new("sup-trunc-ok");
    let r = supervise(&spec(
        &home,
        &["flood-out", &(MAX_CAPTURE_BYTES * 2).to_string()],
    ));
    assert!(r.succeeded(), "a talkative child is not a failing one");
}

// --- 7, 8: deadlines ----------------------------------------------------------

#[test]
fn a_child_past_its_deadline_gets_sigterm() {
    let home = Scratch::new("sup-timeout");
    // The fixture blocks forever, so it is alive at the deadline whatever the
    // scheduler does. There is no race to lose here.
    let r = supervise(&timed_spec(&home, &["sleep-forever"], 300, 2000));

    assert_eq!(r.outcome, Outcome::TimedOut);
    assert!(r.timed_out);
    assert!(r.term_sent, "SIGTERM first");
    assert!(
        !r.kill_sent,
        "a child that respects SIGTERM must not be SIGKILLed"
    );
    assert_eq!(r.signal, Some(libc::SIGTERM));
    assert_eq!(r.exit_code, None);
}

#[test]
fn a_child_that_ignores_sigterm_gets_sigkill() {
    let home = Scratch::new("sup-kill");
    let r = supervise(&timed_spec(&home, &["ignore-term"], 300, 300));

    assert_eq!(r.outcome, Outcome::TimedOut);
    assert!(r.term_sent && r.kill_sent, "escalation, in that order");
    assert_eq!(r.signal, Some(libc::SIGKILL));
    assert!(
        r.summary().contains("SIGKILL after SIGTERM"),
        "{}",
        r.summary()
    );
}

/// "SIGTERM sent" is not "process finished". The grace period must actually be
/// waited out before escalating, and the truth must come from `wait`.
#[test]
fn the_grace_period_is_respected_before_escalating() {
    let home = Scratch::new("sup-grace");
    let grace_ms = 600;
    let started = Instant::now();
    let r = supervise(&timed_spec(&home, &["ignore-term"], 200, grace_ms));
    let elapsed = started.elapsed();

    assert!(r.kill_sent);
    assert!(
        elapsed >= Duration::from_millis(200 + grace_ms),
        "escalated after {elapsed:?}, before the grace period had elapsed"
    );
}

// --- 9: the process group ------------------------------------------------------

/// The demonstration, not an inference: a grandchild records its own pid and
/// process group, and the test checks both that it shared the group and that
/// it is gone afterwards.
#[test]
fn a_grandchild_shares_the_group_and_dies_with_it() {
    let home = Scratch::new("sup-group");
    let record = home.root().join("grandchild.txt");

    let r = supervise(&timed_spec(
        &home,
        &["spawn-grandchild", record.to_str().unwrap()],
        400,
        400,
    ));
    assert!(r.timed_out);

    let recorded = fs::read_to_string(&record).expect("the grandchild must have recorded itself");
    let mut parts = recorded.split_whitespace();
    let grandchild_pid: i32 = parts.next().unwrap().parse().unwrap();
    let grandchild_pgid: i32 = parts.next().unwrap().parse().unwrap();

    assert_eq!(
        grandchild_pgid,
        r.pid.unwrap() as i32,
        "the grandchild must be in the child's process group, or signalling the \
         group would never have reached it"
    );
    assert_ne!(
        grandchild_pid,
        r.pid.unwrap() as i32,
        "it must really be a separate process"
    );

    // And it is actually gone. Killing only the parent pid would leave it here.
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if !process_exists(grandchild_pid) {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "grandchild {grandchild_pid} survived the group kill"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

// --- 10: reaping ----------------------------------------------------------------

#[test]
fn the_child_is_always_reaped() {
    let home = Scratch::new("sup-reap");
    for args in [vec!["exit", "0"], vec!["exit", "7"]] {
        let r = supervise(&spec(&home, &args));
        assert_no_such_child(r.pid.unwrap());
    }
    // And on both kill paths, where forgetting is easiest.
    let r = supervise(&timed_spec(&home, &["sleep-forever"], 200, 1000));
    assert_no_such_child(r.pid.unwrap());
    let r = supervise(&timed_spec(&home, &["ignore-term"], 200, 200));
    assert_no_such_child(r.pid.unwrap());
}

/// Already reaped: a second `waitpid` must fail with `ECHILD`, not hand back a
/// status that was sitting in the table.
fn assert_no_such_child(pid: u32) {
    let mut status = 0;
    // SAFETY: `waitpid` with WNOHANG does not block and only inspects this
    // process's children.
    let rc = unsafe { libc::waitpid(pid as libc::pid_t, &mut status, libc::WNOHANG) };
    assert_eq!(rc, -1, "pid {pid} was still waitable: it was left a zombie");
    assert_eq!(
        std::io::Error::last_os_error().raw_os_error(),
        Some(libc::ECHILD),
        "pid {pid} should be unknown to us by now"
    );
}

fn process_exists(pid: i32) -> bool {
    // SAFETY: signal 0 performs the permission and existence checks without
    // sending anything.
    unsafe { libc::kill(pid, 0) == 0 }
}

// --- 11: the clock ---------------------------------------------------------------

/// The deadline must not be able to move when the civil clock does. Stepping
/// the system clock needs root, so this checks the two things that can be
/// checked without it: the elapsed time is measured monotonically, and no
/// wall-clock type appears in the module at all.
#[test]
fn the_deadline_uses_a_monotonic_clock() {
    let home = Scratch::new("sup-clock");
    let r = supervise(&timed_spec(&home, &["sleep-forever"], 400, 400));
    assert!(
        r.duration >= Duration::from_millis(400),
        "measured {:?}, less than the deadline it waited out",
        r.duration
    );
    assert!(r.duration < Duration::from_secs(30));

    let source = fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/src/supervisor.rs"))
        .expect("read the supervisor's own source");
    assert!(
        source.contains("Instant::now()"),
        "the deadline must come from a monotonic clock"
    );
    for wall_clock in ["SystemTime", "UNIX_EPOCH", "crate::now()"] {
        assert!(
            !source.contains(wall_clock),
            "{wall_clock} has no business in a deadline: an NTP step must not \
             lengthen or shorten a child's timeout"
        );
    }
}

// --- 12 to 16: pre-flight ---------------------------------------------------------

fn refusal(home: &Scratch, program: PathBuf, cwd: PathBuf) -> Refusal {
    let s = ChildSpec::new(
        "bad",
        program,
        cwd,
        Duration::from_secs(5),
        Duration::from_millis(100),
    );
    let r = supervise(&s);
    let _ = home;
    match r.outcome {
        Outcome::Refused(why) => {
            assert!(r.pid.is_none(), "nothing may have been spawned");
            assert_eq!(r.duration, Duration::ZERO);
            why
        }
        other => panic!("expected a refusal, got {other:?}"),
    }
}

#[test]
fn a_missing_program_is_refused() {
    let home = Scratch::new("pf-missing");
    let root = home.root().to_path_buf();
    assert_eq!(
        refusal(&home, root.join("not-here"), root.clone()),
        Refusal::ProgramMissing
    );
}

/// The same rule as everywhere else in this project: following it would let
/// whoever can write the directory choose what gets executed.
#[test]
fn a_symlinked_program_is_refused_even_when_the_target_is_fine() {
    let home = Scratch::new("pf-symlink");
    let link = home.root().join("link-to-fixture");
    std::os::unix::fs::symlink(fixture(), &link).unwrap();
    let root = home.root().to_path_buf();
    assert_eq!(refusal(&home, link, root), Refusal::ProgramIsSymlink);
}

#[test]
fn a_non_executable_program_is_refused() {
    let home = Scratch::new("pf-noexec");
    let copy = copy_with_mode(&home, "copy", 0o644);
    let root = home.root().to_path_buf();
    assert!(matches!(
        refusal(&home, copy, root),
        Refusal::ProgramNotExecutable { .. }
    ));
}

/// A maintenance program anyone can rewrite is a hole the moment anything
/// privileged runs it.
#[test]
fn a_world_writable_program_is_refused() {
    let home = Scratch::new("pf-writable");
    let copy = copy_with_mode(&home, "copy", 0o777);
    let root = home.root().to_path_buf();
    assert!(matches!(
        refusal(&home, copy, root),
        Refusal::ProgramWritableByOthers { .. }
    ));
}

/// The rule is not theoretical on this machine: with `umask 002` every
/// freshly built binary is group-writable, including the fixture.
#[test]
fn a_freshly_built_binary_is_refused_under_a_lax_umask() {
    let home = Scratch::new("pf-umask");
    let copy = copy_with_mode(&home, "as-built", 0o775);
    let root = home.root().to_path_buf();
    assert!(matches!(
        refusal(&home, copy, root),
        Refusal::ProgramWritableByOthers { .. }
    ));
}

#[test]
fn a_group_writable_program_is_refused() {
    let home = Scratch::new("pf-gwritable");
    let copy = copy_with_mode(&home, "copy", 0o775);
    let root = home.root().to_path_buf();
    assert!(matches!(
        refusal(&home, copy, root),
        Refusal::ProgramWritableByOthers { .. }
    ));
}

#[test]
fn a_directory_is_not_a_program() {
    let home = Scratch::new("pf-dir");
    let root = home.root().to_path_buf();
    assert_eq!(
        refusal(&home, root.clone(), root.clone()),
        Refusal::ProgramNotRegularFile
    );
}

#[test]
fn a_missing_working_directory_is_refused() {
    let home = Scratch::new("pf-nocwd");
    let root = home.root().to_path_buf();
    let good = program(&home);
    assert_eq!(
        refusal(&home, good, root.join("nowhere")),
        Refusal::CwdMissing
    );
}

#[test]
fn a_file_as_a_working_directory_is_refused() {
    let home = Scratch::new("pf-filecwd");
    let file = home.root().join("a-file");
    fs::write(&file, b"not a directory").unwrap();
    let good = program(&home);
    assert_eq!(refusal(&home, good, file), Refusal::CwdNotADirectory);
}

#[test]
fn preflight_alone_spawns_nothing() {
    let home = Scratch::new("pf-pure");
    let good = ChildSpec::new(
        "ok",
        program(&home),
        home.root(),
        Duration::from_secs(1),
        Duration::from_millis(10),
    );
    assert_eq!(preflight(&good), Ok(()));
}

// --- 17: the environment -----------------------------------------------------------

#[test]
fn the_child_inherits_nothing_from_us() {
    let home = Scratch::new("sup-env");
    let r = supervise(&spec(&home, &["print-env"]));
    assert_eq!(r.exit_code, Some(0));

    let text = r.stdout.text().into_owned();
    let mut seen: Vec<String> = text
        .lines()
        .filter_map(|l| l.split('=').next())
        .map(str::to_string)
        .collect();
    seen.sort();

    let mut expected: Vec<String> = BASE_ENV.iter().map(|(k, _)| (*k).to_string()).collect();
    expected.sort();
    assert_eq!(
        seen, expected,
        "the child's environment is exactly BASE_ENV"
    );

    // Named explicitly, because these are the ones that matter. Every one of
    // them is present in the process running this test.
    for leaked in [
        "HOME",
        "USER",
        "LOGNAME",
        "SSH_AUTH_SOCK",
        "SHELL",
        "HERMES_HOME",
        "AWS_SECRET_ACCESS_KEY",
        "OPENAI_API_KEY",
    ] {
        assert!(
            !seen.iter().any(|k| k == leaked),
            "{leaked} must not reach a child by accident"
        );
    }
    assert!(
        seen.iter().any(|k| k == "PATH"),
        "a controlled PATH is still needed"
    );
}

// --- 18, 19: argv and paths ----------------------------------------------------------

/// Nothing interprets these -- not a shell, not this module. They arrive
/// exactly as written.
#[test]
fn argv_arrives_literally_however_it_looks() {
    let home = Scratch::new("sup-argv");
    let awkward = [
        "--dry-run",
        "-rf",
        "/",
        "--",
        "--trigger=timer",
        "$(whoami)",
        "`id`",
        "; echo pwned",
        "&& rm -rf /",
        "| cat",
        "*",
        "~",
        "a b c",
        "  leading and trailing  ",
        "'single'",
        "\"double\"",
        "back\\slash",
        "€ñ→",
        "--=--",
        "",
    ];
    let mut s = spec(&home, &["print-argv"]);
    s.argv.extend(awkward.iter().map(OsString::from));

    let r = supervise(&s);
    assert_eq!(r.exit_code, Some(0));

    let text = r.stdout.text().into_owned();
    let got: Vec<&str> = text.lines().collect();
    assert_eq!(
        got.as_slice(),
        awkward.as_slice(),
        "every argument must survive byte for byte"
    );
}

#[test]
fn paths_with_spaces_work() {
    let home = Scratch::new("sup-spaces");
    let dir = home.root().join("a directory with spaces");
    fs::create_dir_all(&dir).unwrap();
    let program = dir.join("hm fixture link");
    fs::hard_link(fixture(), &program).unwrap();

    let s = ChildSpec::new(
        "spaced",
        &program,
        &dir,
        Duration::from_secs(10),
        Duration::from_millis(100),
    )
    .args(["print-cwd"]);

    let r = supervise(&s);
    assert_eq!(r.exit_code, Some(0), "{}", r.summary());
    assert_eq!(
        Path::new(r.stdout.text().trim()),
        canonical(&dir),
        "the working directory must be the one asked for"
    );
}

fn canonical(p: &Path) -> PathBuf {
    p.canonicalize().unwrap_or_else(|_| p.to_path_buf())
}

// --- the spec itself ------------------------------------------------------------------

/// There is no field on a spec that can carry a command string, and no code
/// path that builds one. This is the structural half of "no shell, ever".
#[test]
fn nothing_in_the_supervisor_can_reach_a_shell() {
    let source =
        fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/src/supervisor.rs")).unwrap();
    // The prose talks about shells; the code must never name one, so the
    // comments are stripped before looking.
    let code: String = source
        .lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n");
    for forbidden in [
        "/bin/sh",
        "/bin/bash",
        "/usr/bin/env",
        "Command::new(\"",
        "\"-c\"",
    ] {
        assert!(
            !code.contains(forbidden),
            "{forbidden:?} appears in the supervisor: the only program it may \
             run is the one in the spec"
        );
    }
    assert!(
        code.contains("Command::new(&spec.program)"),
        "the program must come from the spec and nowhere else"
    );
}
