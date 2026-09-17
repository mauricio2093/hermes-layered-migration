//! The run lifecycle, including the part that matters most: dry-run writes
//! nothing.

mod common;

use common::TempHome;
use hermes_maint_core::state::{Outcome, State, TaskOutcome, TaskResult};
use hermes_maint_core::{Exit, Runner, Trigger};

fn task(id: &str, outcome: TaskOutcome) -> TaskResult {
    TaskResult {
        id: id.into(),
        outcome,
        exit: Some(0),
        duration_s: 1,
    }
}

#[test]
fn a_run_with_no_tasks_opens_and_closes_cleanly() {
    let home = TempHome::new("empty");
    let paths = home.paths();

    let runner = Runner::start(paths.clone(), Trigger::Manual, false).expect("start");
    assert_eq!(runner.run_id(), 1);
    assert_eq!(runner.finish(), Exit::Ok);

    let (state, _) = State::load(&paths).expect("load");
    assert!(state.last_run.is_none(), "the run must be closed");
    assert_eq!(state.history.len(), 1);
    assert_eq!(state.history[0].outcome, Some(Outcome::Ok));
    assert_eq!(state.history[0].trigger, "manual");
    assert!(state.history[0].finished_at.is_some());
    assert_eq!(state.next_run_id, 2);
}

/// The open run is persisted before any work begins. Without that, an
/// interruption would be indistinguishable from a run that never happened.
#[test]
fn the_open_run_is_on_disk_before_any_work() {
    let home = TempHome::new("open");
    let paths = home.paths();

    let runner = Runner::start(paths.clone(), Trigger::Timer, false).expect("start");
    let (state, _) = State::load(&paths).expect("load while open");
    assert_eq!(state.last_run.as_ref().map(|r| r.id), Some(1));
    assert_eq!(state.last_run.as_ref().unwrap().finished_at, None);
    drop(runner);
}

/// Simulates the process dying mid-run: the runner is dropped without
/// `finish`, leaving an open run, exactly as a crash would.
#[test]
fn the_next_run_reconciles_an_interrupted_one() {
    let home = TempHome::new("interrupted");
    let paths = home.paths();

    drop(Runner::start(paths.clone(), Trigger::Timer, false).expect("first"));

    let second = Runner::start(paths.clone(), Trigger::Timer, false).expect("second");
    assert_eq!(second.run_id(), 2, "ids do not get reused");
    assert_eq!(second.finish(), Exit::Ok);

    let (state, _) = State::load(&paths).expect("load");
    let interrupted = state.history.iter().find(|r| r.id == 1).expect("run 1");
    assert_eq!(interrupted.outcome, Some(Outcome::Interrupted));
    assert!(state.last_run.is_none());
}

#[test]
fn a_second_run_cannot_start_while_one_holds_the_lock() {
    let home = TempHome::new("contended");
    let paths = home.paths();

    let first = Runner::start(paths.clone(), Trigger::Timer, false).expect("first");
    let second = Runner::start(paths.clone(), Trigger::Manual, false);
    match second {
        Err(e) => assert_eq!(e.exit(), Exit::LockBusy),
        Ok(_) => panic!("two runs must never overlap"),
    }
    assert_eq!(first.finish(), Exit::Ok);

    // And once it is free, a run starts normally.
    Runner::start(paths, Trigger::Manual, false)
        .expect("free again")
        .finish();
}

// --- dry-run ---------------------------------------------------------------

#[test]
fn a_dry_run_writes_no_state_at_all() {
    let home = TempHome::new("dry");
    let paths = home.paths();

    let runner = Runner::start(paths.clone(), Trigger::Manual, true).expect("start");
    assert!(runner.dry_run());
    assert_eq!(runner.finish(), Exit::Ok);

    assert!(
        !paths.state_file().exists(),
        "a dry-run must not create the state file"
    );
    // The lock file is the one thing it does create, because taking the lock
    // is mandatory: a dry-run that read state while a real run rewrote it
    // would report fiction.
    assert!(paths.lock_file().exists());
}

#[test]
fn a_dry_run_does_not_disturb_existing_state() {
    let home = TempHome::new("dry-existing");
    let paths = home.paths();

    Runner::start(paths.clone(), Trigger::Timer, false)
        .expect("real run")
        .finish();
    let before = std::fs::read(paths.state_file()).unwrap();

    Runner::start(paths.clone(), Trigger::Manual, true)
        .expect("dry run")
        .finish();

    assert_eq!(
        std::fs::read(paths.state_file()).unwrap(),
        before,
        "a dry-run must leave the state byte-for-byte identical"
    );
}

#[test]
fn a_dry_run_still_takes_the_lock() {
    let home = TempHome::new("dry-lock");
    let paths = home.paths();

    let held = Runner::start(paths.clone(), Trigger::Timer, false).expect("real");
    let dry = Runner::start(paths.clone(), Trigger::Manual, true);
    assert!(
        matches!(dry, Err(ref e) if e.exit() == Exit::LockBusy),
        "a dry-run must queue behind a real run, not read under it"
    );
    held.finish();
}

// --- outcome arithmetic ----------------------------------------------------

#[test]
fn outcome_precedence_is_timeout_then_partial_then_degraded() {
    let cases: &[(&[TaskOutcome], Outcome, Exit)] = &[
        (&[], Outcome::Ok, Exit::Ok),
        (&[TaskOutcome::Ok, TaskOutcome::Ok], Outcome::Ok, Exit::Ok),
        (&[TaskOutcome::Degraded], Outcome::Degraded, Exit::Degraded),
        (&[TaskOutcome::Failed], Outcome::Partial, Exit::Partial),
        (&[TaskOutcome::Skipped], Outcome::Partial, Exit::Partial),
        (&[TaskOutcome::Timeout], Outcome::Timeout, Exit::Timeout),
        (
            &[TaskOutcome::Degraded, TaskOutcome::Failed],
            Outcome::Partial,
            Exit::Partial,
        ),
        (
            &[TaskOutcome::Failed, TaskOutcome::Timeout, TaskOutcome::Ok],
            Outcome::Timeout,
            Exit::Timeout,
        ),
    ];

    for (outcomes, expected, exit) in cases {
        let home = TempHome::new("precedence");
        let mut runner = Runner::start(home.paths(), Trigger::Manual, false).expect("start");
        for (n, o) in outcomes.iter().enumerate() {
            runner.record_task(task(&format!("t{n}"), *o));
        }
        assert_eq!(runner.outcome(), *expected, "for {outcomes:?}");
        assert_eq!(runner.finish(), *exit, "for {outcomes:?}");
    }
}

#[test]
fn recorded_tasks_are_persisted_with_the_run() {
    let home = TempHome::new("tasks");
    let paths = home.paths();

    let mut runner = Runner::start(paths.clone(), Trigger::Timer, false).expect("start");
    runner.record_task(task("one", TaskOutcome::Ok));
    runner.record_task(task("two", TaskOutcome::Degraded));
    assert_eq!(runner.finish(), Exit::Degraded);

    let (state, _) = State::load(&paths).expect("load");
    let ids: Vec<_> = state.history[0].tasks.iter().map(|t| &t.id).collect();
    assert_eq!(ids, ["one", "two"]);
}
