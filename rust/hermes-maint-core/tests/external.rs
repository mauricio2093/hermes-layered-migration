//! The translation between layers:
//!
//! ```text
//! Task -> ExternalTask -> ChildSpec -> supervise() -> ChildResult
//!      -> TaskReport -> TaskResult -> run outcome -> exit code -> state.json
//! ```
//!
//! The supervisor's own behaviour is not retested here -- that is closed. What
//! is tested is every hand-off between the two, and that nothing large or
//! sensitive survives the trip to disk.
//!
//! No task registered in production uses any of this.

mod common;

use std::fs;

use std::time::Duration;

use common::{fixture, fixture_link, Scratch};
use hermes_maint_core::aggregate;
use hermes_maint_core::paths::Paths;
use hermes_maint_core::state::{
    clamp_detail, Outcome as RunOutcome, State, TaskOutcome, TaskResult, MAX_DETAIL_CHARS,
    MAX_STATE_BYTES,
};
use hermes_maint_core::supervisor::{Captured, ChildResult, ChildSpec, Outcome, Refusal};
use hermes_maint_core::task::{registry, Task, TaskContext};
use hermes_maint_core::tasks::backup_freshness::format_iso8601;
use hermes_maint_core::tasks::external::{interpret, ExternalTask, EXCERPT_CHARS};
use hermes_maint_core::{Exit, Runner, Trigger};

// --- building an ExternalTask over the fixture --------------------------------

fn external(id: &'static str, scratch: &Scratch, args: &[&'static str]) -> ExternalTask {
    external_with(id, scratch, args, 30_000, 1_000)
}

fn external_with(
    id: &'static str,
    scratch: &Scratch,
    args: &[&'static str],
    timeout_ms: u64,
    grace_ms: u64,
) -> ExternalTask {
    let program = fixture_link(scratch);
    let args: Vec<String> = args.iter().map(|s| (*s).to_string()).collect();
    ExternalTask::new(
        id,
        "a fixture that exists only for tests",
        Box::new(move |paths: &Paths| {
            ChildSpec::new(
                "fixture",
                program.clone(),
                paths.hermes_home(),
                Duration::from_millis(timeout_ms),
                Duration::from_millis(grace_ms),
            )
            .args(&args)
        }),
    )
}

fn run_task(task: &dyn Task, scratch: &Scratch) -> hermes_maint_core::TaskReport {
    let paths = scratch.paths();
    task.run(&TaskContext { paths: &paths })
        .expect("an external task reports, it does not error")
}

// --- 2: the mapping, as pure arithmetic ---------------------------------------

fn child(outcome: Outcome, exit: Option<i32>, signal: Option<i32>) -> ChildResult {
    ChildResult {
        id: "probe".into(),
        outcome,
        pid: Some(1234),
        exit_code: exit,
        signal,
        duration: Duration::from_millis(1500),
        timed_out: false,
        term_sent: false,
        kill_sent: false,
        stdout: Captured::default(),
        stderr: Captured::default(),
    }
}

#[test]
fn exit_zero_is_ok() {
    let r = interpret(&child(Outcome::Exited, Some(0), None), true);
    assert_eq!(r.outcome, TaskOutcome::Ok);
    assert_eq!(r.exit, Some(0));
    assert_eq!(r.signal, None);
}

#[test]
fn a_nonzero_exit_is_failed_and_keeps_its_code() {
    let r = interpret(&child(Outcome::Exited, Some(7), None), true);
    assert_eq!(r.outcome, TaskOutcome::Failed);
    assert_eq!(
        r.exit,
        Some(7),
        "the code is the diagnosis; it must survive"
    );
}

#[test]
fn a_deadline_is_a_timeout_not_a_failure() {
    let r = interpret(&child(Outcome::TimedOut, None, Some(libc::SIGKILL)), true);
    assert_eq!(r.outcome, TaskOutcome::Timeout);
    assert_eq!(r.signal, Some(libc::SIGKILL));
}

#[test]
fn an_unexpected_signal_is_a_failure() {
    // A segfault or the OOM killer: a malfunction, not a deadline.
    let r = interpret(&child(Outcome::Signalled, None, Some(libc::SIGSEGV)), true);
    assert_eq!(r.outcome, TaskOutcome::Failed);
    assert_eq!(r.signal, Some(libc::SIGSEGV));
}

/// The two that go different ways, and the reason they do.
#[test]
fn a_refusal_is_skipped_but_a_spawn_failure_is_failed() {
    let refused = interpret(
        &child(Outcome::Refused(Refusal::ProgramIsSymlink), None, None),
        true,
    );
    assert_eq!(
        refused.outcome,
        TaskOutcome::Skipped,
        "pre-flight refusing is the check working: nothing ran, on purpose"
    );

    let broken = interpret(
        &child(Outcome::SpawnFailed("ENOMEM".into()), None, None),
        true,
    );
    assert_eq!(
        broken.outcome,
        TaskOutcome::Failed,
        "everything agreed it should run and the machinery broke: that is a \
         malfunction, not a decision"
    );
}

// --- 4, 5: what reaches disk ---------------------------------------------------

fn with_output(stdout: &str, stderr: &str) -> ChildResult {
    let mut c = child(Outcome::Exited, Some(1), None);
    c.stdout = Captured {
        bytes: stdout.as_bytes().to_vec(),
        total_bytes: stdout.len() as u64,
        truncated: false,
        complete: true,
    };
    c.stderr = Captured {
        bytes: stderr.as_bytes().to_vec(),
        total_bytes: stderr.len() as u64,
        truncated: false,
        complete: true,
    };
    c
}

#[test]
fn the_excerpt_prefers_stderr() {
    let r = interpret(&with_output("progress line", "the actual error"), true);
    assert!(r.detail.contains("the actual error"), "{}", r.detail);
    assert!(!r.detail.contains("progress line"), "{}", r.detail);
}

#[test]
fn the_excerpt_falls_back_to_stdout() {
    let r = interpret(&with_output("all it said", ""), true);
    assert!(r.detail.contains("all it said"), "{}", r.detail);
}

#[test]
fn the_excerpt_is_one_line() {
    let r = interpret(&with_output("", "line one\nline two\r\nline three"), true);
    assert!(!r.detail.contains('\n'), "{}", r.detail);
    assert!(
        r.detail.contains("line one line two line three"),
        "{}",
        r.detail
    );
}

#[test]
fn output_bytes_are_counted_even_though_the_bytes_are_not_kept() {
    let mut c = child(Outcome::Exited, Some(0), None);
    c.stdout = Captured {
        bytes: b"tail only".to_vec(),
        total_bytes: 4 * 1024 * 1024,
        truncated: true,
        complete: true,
    };
    c.stderr = Captured {
        bytes: Vec::new(),
        total_bytes: 1024,
        truncated: false,
        complete: true,
    };
    let r = interpret(&c, true);
    assert_eq!(
        r.output_bytes,
        Some(4 * 1024 * 1024 + 1024),
        "the one fact about the output that survives sampling"
    );
}

/// The boundary that matters: the supervisor holds 64 KiB per stream in
/// memory; what crosses to `state.json` is a couple of hundred characters.
#[test]
fn a_huge_child_output_cannot_reach_the_state_file() {
    let noise = "x".repeat(64 * 1024);
    let r = interpret(&with_output(&noise, &noise), true);
    assert!(
        r.detail.chars().count() <= MAX_DETAIL_CHARS,
        "detail was {} chars",
        r.detail.chars().count()
    );
    assert_eq!(
        clamp_detail(&r.detail),
        r.detail,
        "already clamped on the way out"
    );
}

#[test]
fn the_excerpt_keeps_the_tail() {
    let long: String = (0..EXCERPT_CHARS * 3)
        .map(|i| char::from(b'a' + (i % 26) as u8))
        .collect();
    let r = interpret(&with_output("", &long), true);
    let last: String = long
        .chars()
        .rev()
        .take(20)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    assert!(
        r.detail.contains(&last),
        "the end is what says what it was doing: {}",
        r.detail
    );
}

/// For a child whose output might carry a token or a private path.
#[test]
fn output_excerpting_can_be_turned_off_entirely() {
    let secret = "token=SUPER-SECRET-VALUE";
    let r = interpret(&with_output("", secret), false);
    assert!(
        !r.detail.contains("SUPER-SECRET"),
        "nothing from the child may be written down: {}",
        r.detail
    );
    assert_eq!(r.exit, Some(1), "the structured evidence is still kept");
    assert!(r.output_bytes.unwrap() > 0);
}

#[test]
fn a_task_can_opt_out_of_excerpting() {
    let scratch = Scratch::new("ext-noexcerpt");
    let task = external("quiet", &scratch, &["echo-err", "token=abc123"]).without_output_excerpt();
    let report = run_task(&task, &scratch);
    assert!(!report.detail.contains("abc123"), "{}", report.detail);
}

// --- 6: through the real fixture ------------------------------------------------

#[test]
fn a_fixture_that_exits_zero_reports_ok() {
    let scratch = Scratch::new("ext-ok");
    let report = run_task(&external("probe", &scratch, &["exit", "0"]), &scratch);
    assert_eq!(report.outcome, TaskOutcome::Ok);
    assert_eq!(report.exit, Some(0));
}

#[test]
fn a_fixture_that_exits_seven_reports_failed() {
    let scratch = Scratch::new("ext-7");
    let report = run_task(&external("probe", &scratch, &["exit", "7"]), &scratch);
    assert_eq!(report.outcome, TaskOutcome::Failed);
    assert_eq!(report.exit, Some(7));
    assert!(report.detail.contains("exit 7"), "{}", report.detail);
}

#[test]
fn stdout_reaches_the_report() {
    let scratch = Scratch::new("ext-stdout");
    let report = run_task(
        &external("probe", &scratch, &["echo-out", "hello"]),
        &scratch,
    );
    assert!(report.detail.contains("hello"), "{}", report.detail);
    assert_eq!(report.output_bytes, Some(5));
}

#[test]
fn stderr_reaches_the_report() {
    let scratch = Scratch::new("ext-stderr");
    let report = run_task(
        &external("probe", &scratch, &["echo-err", "broken"]),
        &scratch,
    );
    assert!(report.detail.contains("broken"), "{}", report.detail);
}

#[test]
fn a_fixture_past_its_deadline_reports_timeout() {
    let scratch = Scratch::new("ext-term");
    let task = external_with("probe", &scratch, &["sleep-forever"], 300, 2000);
    let report = run_task(&task, &scratch);
    assert_eq!(report.outcome, TaskOutcome::Timeout);
    assert_eq!(report.signal, Some(libc::SIGTERM));
}

#[test]
fn a_fixture_that_ignores_sigterm_still_reports_timeout() {
    let scratch = Scratch::new("ext-kill");
    let task = external_with("probe", &scratch, &["ignore-term"], 300, 300);
    let report = run_task(&task, &scratch);
    assert_eq!(
        report.outcome,
        TaskOutcome::Timeout,
        "how hard it was to stop does not change what happened"
    );
    assert_eq!(report.signal, Some(libc::SIGKILL));
}

#[test]
fn a_refused_program_reports_skipped() {
    let scratch = Scratch::new("ext-refused");
    let missing = scratch.root().join("not-here");
    let task = ExternalTask::new(
        "probe",
        "points at nothing",
        Box::new(move |paths: &Paths| {
            ChildSpec::new(
                "missing",
                missing.clone(),
                paths.hermes_home(),
                Duration::from_secs(5),
                Duration::from_millis(100),
            )
        }),
    );
    let report = run_task(&task, &scratch);
    assert_eq!(report.outcome, TaskOutcome::Skipped);
    assert_eq!(report.exit, None);
    assert!(report.detail.contains("refused"), "{}", report.detail);
}

// --- 3: aggregation ---------------------------------------------------------------

fn result(id: &str, outcome: TaskOutcome) -> TaskResult {
    TaskResult {
        id: id.into(),
        outcome,
        exit: None,
        signal: None,
        duration_s: 0,
        output_bytes: None,
        detail: None,
    }
}

#[test]
fn the_run_outcome_follows_a_stated_precedence() {
    use TaskOutcome::{Degraded, Failed, Ok as TOk, Skipped, Timeout};
    let cases: &[(&[TaskOutcome], RunOutcome, Exit)] = &[
        (&[], RunOutcome::Ok, Exit::Ok),
        (&[TOk, TOk], RunOutcome::Ok, Exit::Ok),
        (&[TOk, Degraded], RunOutcome::Degraded, Exit::Degraded),
        (&[TOk, Failed], RunOutcome::Partial, Exit::Partial),
        (&[TOk, Skipped], RunOutcome::Partial, Exit::Partial),
        (&[TOk, Timeout], RunOutcome::Timeout, Exit::Timeout),
        // A missing observation outranks a bad one: a check that did not
        // complete tells you less than one that did.
        (&[Degraded, Failed], RunOutcome::Partial, Exit::Partial),
        (&[Degraded, Skipped], RunOutcome::Partial, Exit::Partial),
        // Something had to be killed: the most urgent thing in the report.
        (
            &[Degraded, Failed, Timeout],
            RunOutcome::Timeout,
            Exit::Timeout,
        ),
    ];

    for (outcomes, expected, exit) in cases {
        let tasks: Vec<TaskResult> = outcomes
            .iter()
            .enumerate()
            .map(|(i, o)| result(&format!("t{i}"), *o))
            .collect();
        let got = aggregate(&tasks);
        assert_eq!(got, *expected, "for {outcomes:?}");
        assert_eq!(got.exit(), *exit, "for {outcomes:?}");
    }
}

/// A run whose verdict depended on task order would change when someone
/// reordered the registry.
#[test]
fn aggregation_does_not_depend_on_the_order_tasks_ran_in() {
    use TaskOutcome::{Degraded, Failed, Ok as TOk, Skipped, Timeout};
    let all = [TOk, Degraded, Failed, Skipped, Timeout];

    // Every rotation, and the reverse of each.
    for start in 0..all.len() {
        let mut order: Vec<TaskOutcome> = all
            .iter()
            .cycle()
            .skip(start)
            .take(all.len())
            .copied()
            .collect();
        let forward: Vec<TaskResult> = order
            .iter()
            .enumerate()
            .map(|(i, o)| result(&format!("t{i}"), *o))
            .collect();
        order.reverse();
        let backward: Vec<TaskResult> = order
            .iter()
            .enumerate()
            .map(|(i, o)| result(&format!("t{i}"), *o))
            .collect();

        assert_eq!(aggregate(&forward), RunOutcome::Timeout);
        assert_eq!(aggregate(&backward), aggregate(&forward));
    }
}

// --- 7: the whole path, end to end ---------------------------------------------------

/// A verified backup dated *age_seconds* ago, so `backup-freshness` has
/// something real to be happy about.
fn plant_backup(scratch: &Scratch, age_seconds: i64) {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let dir = scratch
        .root()
        .join("backups")
        .join("independiente")
        .join("fixture");
    fs::create_dir_all(&dir).unwrap();
    fs::write(
        dir.join("state.json"),
        format!(
            r#"{{"timestamp":"{}","backup":{{"created":true,"archive_integrity":true,
                 "database_integrity":true,"manifest_integrity":true,
                 "restore_verified":true}},"backup_verified":true}}"#,
            format_iso8601(now - age_seconds)
        ),
    )
    .unwrap();
}

fn full_run(scratch: &Scratch, probe: ExternalTask) -> (Exit, State) {
    let paths = scratch.paths();
    let mut runner = Runner::start(paths.clone(), Trigger::Timer, false).expect("start");

    let mut tasks = registry();
    tasks.push(Box::new(probe));
    runner.run_tasks(&tasks);

    let exit = runner.finish();
    let (state, _) = State::load(&paths).expect("load");
    (exit, state)
}

#[test]
fn every_layer_hands_off_correctly() {
    let scratch = Scratch::new("ext-e2e");
    plant_backup(&scratch, 3600);

    let (exit, state) = full_run(
        &scratch,
        external("probe", &scratch, &["echo-out", "all good"]),
    );

    assert_eq!(exit, Exit::Ok, "{:?}", state.history[0]);
    let run = &state.history[0];
    assert_eq!(run.outcome, Some(RunOutcome::Ok));

    let ids: Vec<&str> = run.tasks.iter().map(|t| t.id.as_str()).collect();
    assert_eq!(
        ids,
        ["disk-space", "backup-freshness", "probe"],
        "in registry order, with the external one appended"
    );
    for t in &run.tasks {
        assert_eq!(t.outcome, TaskOutcome::Ok, "{} said {:?}", t.id, t.outcome);
    }

    let probe = run.tasks.iter().find(|t| t.id == "probe").unwrap();
    assert_eq!(probe.exit, Some(0), "the exit status reached state.json");
    assert_eq!(probe.signal, None);
    assert_eq!(probe.output_bytes, Some(8));
    assert!(probe.detail.as_ref().unwrap().contains("all good"));

    let in_process = run.tasks.iter().find(|t| t.id == "disk-space").unwrap();
    assert_eq!(
        in_process.exit, None,
        "an in-process task has no exit status"
    );
    assert_eq!(in_process.output_bytes, None);
}

#[test]
fn an_external_failure_makes_the_run_partial() {
    let scratch = Scratch::new("ext-e2e-fail");
    plant_backup(&scratch, 3600);
    let (exit, state) = full_run(&scratch, external("probe", &scratch, &["exit", "7"]));

    assert_eq!(exit, Exit::Partial);
    assert_eq!(state.history[0].outcome, Some(RunOutcome::Partial));
    let probe = state.history[0]
        .tasks
        .iter()
        .find(|t| t.id == "probe")
        .unwrap();
    assert_eq!(probe.outcome, TaskOutcome::Failed);
    assert_eq!(probe.exit, Some(7));
}

#[test]
fn an_external_timeout_makes_the_run_time_out() {
    let scratch = Scratch::new("ext-e2e-timeout");
    plant_backup(&scratch, 3600);
    let probe = external_with("probe", &scratch, &["sleep-forever"], 300, 1000);
    let (exit, state) = full_run(&scratch, probe);

    assert_eq!(exit, Exit::Timeout);
    assert_eq!(state.history[0].outcome, Some(RunOutcome::Timeout));
    let probe = state.history[0]
        .tasks
        .iter()
        .find(|t| t.id == "probe")
        .unwrap();
    assert_eq!(probe.outcome, TaskOutcome::Timeout);
    assert_eq!(probe.signal, Some(libc::SIGTERM));
}

#[test]
fn an_external_refusal_makes_the_run_partial() {
    let scratch = Scratch::new("ext-e2e-skip");
    plant_backup(&scratch, 3600);
    let link = fixture_link(&scratch);
    let not_executable = scratch.root().join("not-executable");
    fs::copy(fixture(), &not_executable).unwrap();
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&not_executable, fs::Permissions::from_mode(0o644)).unwrap();
    }
    let _ = link;

    let probe = ExternalTask::new(
        "probe",
        "a program that will be refused",
        Box::new(move |paths: &Paths| {
            ChildSpec::new(
                "refused",
                not_executable.clone(),
                paths.hermes_home(),
                Duration::from_secs(5),
                Duration::from_millis(100),
            )
        }),
    );
    let (exit, state) = full_run(&scratch, probe);

    assert_eq!(exit, Exit::Partial, "skipped is partial, not success");
    let probe = state.history[0]
        .tasks
        .iter()
        .find(|t| t.id == "probe")
        .unwrap();
    assert_eq!(probe.outcome, TaskOutcome::Skipped);
}

/// A degraded in-process observation alongside a failed external one: the
/// failure wins, and neither depends on which ran first.
#[test]
fn a_degraded_observation_is_outranked_by_a_failure() {
    let scratch = Scratch::new("ext-e2e-degraded");
    // Old enough that backup-freshness degrades.
    plant_backup(&scratch, 10 * 86_400);

    let (exit, state) = full_run(&scratch, external("probe", &scratch, &["exit", "0"]));
    assert_eq!(exit, Exit::Degraded, "degraded alone");

    let scratch2 = Scratch::new("ext-e2e-degraded2");
    plant_backup(&scratch2, 10 * 86_400);
    let (exit2, _) = full_run(&scratch2, external("probe", &scratch2, &["exit", "1"]));
    assert_eq!(
        exit2,
        Exit::Partial,
        "a failure outranks a degraded reading"
    );

    let backup = state.history[0]
        .tasks
        .iter()
        .find(|t| t.id == "backup-freshness")
        .unwrap();
    assert_eq!(backup.outcome, TaskOutcome::Degraded);
}

// --- 4: the size budget, measured -------------------------------------------------------

/// The arithmetic that decided the boundary: 30 runs of history, several
/// external tasks, 64 KiB per stream each would be megabytes -- and would blow
/// `MAX_STATE_BYTES` after a handful of runs, quarantining the file and losing
/// all the history. So the capture stays in memory and a sample goes to disk.
#[test]
fn a_flooding_child_leaves_the_state_file_small() {
    let scratch = Scratch::new("ext-size");
    plant_backup(&scratch, 3600);
    let probe = external("probe", &scratch, &["flood-both", "4194304"]);

    let (_, _) = full_run(&scratch, probe);

    let on_disk = fs::metadata(scratch.paths().state_file()).unwrap().len();
    assert!(
        on_disk < 8 * 1024,
        "one run with a child that printed 8 MiB produced a {on_disk}-byte state file"
    );

    // And with a full history of such runs it would still be far under the cap.
    let worst_case = on_disk * 30;
    assert!(
        worst_case < MAX_STATE_BYTES,
        "30 runs would be {worst_case} bytes, over the {MAX_STATE_BYTES} cap"
    );
}

#[test]
fn nothing_from_a_flood_survives_in_the_state_file() {
    let scratch = Scratch::new("ext-size-detail");
    plant_backup(&scratch, 3600);
    let (_, state) = full_run(
        &scratch,
        external("probe", &scratch, &["flood-out", "1000000"]),
    );

    let probe = state.history[0]
        .tasks
        .iter()
        .find(|t| t.id == "probe")
        .unwrap();
    assert_eq!(
        probe.output_bytes,
        Some(1_000_000),
        "the size is remembered"
    );
    assert!(
        probe.detail.as_ref().unwrap().chars().count() <= MAX_DETAIL_CHARS,
        "the bytes are not"
    );
}

// --- 8: production is unchanged ------------------------------------------------------------

/// The registry must not grow a development probe. A command that runs forever
/// at 03:00 because it was once useful for writing the supervisor is exactly
/// the kind of thing that never gets removed.
#[test]
fn the_production_registry_contains_no_external_task() {
    let ids: Vec<&str> = registry().iter().map(|t| t.id()).collect();
    assert_eq!(ids, ["disk-space", "backup-freshness"]);

    let source = fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/src/task.rs")).unwrap();
    let registry_fn = source
        .split("pub fn registry()")
        .nth(1)
        .expect("the registry function");
    assert!(
        !registry_fn.contains("ExternalTask"),
        "the production registry must not reference an external task yet"
    );
}

#[test]
fn no_registered_task_spawns_anything() {
    let scratch = Scratch::new("ext-inprocess");
    plant_backup(&scratch, 3600);
    let paths = scratch.paths();
    let ctx = TaskContext { paths: &paths };
    for task in registry() {
        let report = task.run(&ctx).expect("in-process tasks do not error here");
        assert_eq!(
            report.exit,
            None,
            "{} produced an exit status, so it ran a process",
            task.id()
        );
        assert_eq!(report.signal, None);
        assert_eq!(report.output_bytes, None);
    }
}
