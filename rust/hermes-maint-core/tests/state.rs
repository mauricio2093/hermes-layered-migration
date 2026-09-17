//! State: survives crashes, tolerates garbage, refuses the future.

mod common;

use common::{mode_of, TempHome};
use hermes_maint_core::state::{
    LoadError, Origin, Outcome, Run, State, TaskOutcome, TaskResult, MAX_HISTORY, MAX_STATE_BYTES,
    SCHEMA,
};

fn closed_run(id: u64) -> Run {
    Run {
        id,
        started_at: 1_700_000_000 + id,
        finished_at: Some(1_700_000_100 + id),
        trigger: "timer".into(),
        outcome: Some(Outcome::Ok),
        tasks: vec![TaskResult {
            id: "example".into(),
            outcome: TaskOutcome::Ok,
            exit: Some(0),
            duration_s: 3,
        }],
    }
}

#[test]
fn a_missing_file_is_a_fresh_start_not_a_failure() {
    let home = TempHome::new("fresh");
    let (state, origin) = State::load(&home.paths()).expect("load");
    assert_eq!(origin, Origin::Fresh);
    assert_eq!(state, State::default());
    assert_eq!(state.schema, SCHEMA);
}

#[test]
fn a_saved_state_reads_back_identical() {
    let home = TempHome::new("roundtrip");
    let paths = home.paths();

    let mut written = State {
        next_run_id: 7,
        ..Default::default()
    };
    written.push_history(closed_run(6));
    written.save(&paths).expect("save");

    let (read, origin) = State::load(&paths).expect("load");
    assert_eq!(origin, Origin::Existing);
    assert_eq!(read, written);
}

#[test]
fn the_state_file_is_not_readable_by_anyone_else() {
    let home = TempHome::new("perm");
    let paths = home.paths();
    State::default().save(&paths).expect("save");
    assert_eq!(mode_of(&paths.state_file()) & 0o077, 0, "state file");
    assert_eq!(mode_of(paths.dir()) & 0o077, 0, "state directory");
}

#[test]
fn saving_leaves_no_temporary_behind() {
    let home = TempHome::new("tmp");
    let paths = home.paths();
    State::default().save(&paths).expect("save");

    let leftovers: Vec<_> = std::fs::read_dir(paths.dir())
        .unwrap()
        .filter_map(Result::ok)
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| n.contains(".tmp"))
        .collect();
    assert!(leftovers.is_empty(), "temporaries left: {leftovers:?}");
}

/// The guarantee the atomic write buys: whatever was there before is still
/// completely there until the new version is completely there.
#[test]
fn a_failed_parse_never_destroys_the_previous_file() {
    let home = TempHome::new("preserve");
    let paths = home.paths();
    paths.ensure_dir().unwrap();
    std::fs::write(paths.state_file(), "{ this is not json").unwrap();
    let before = std::fs::read(paths.state_file()).unwrap();

    let (state, origin) = State::load(&paths).expect("load");
    assert_eq!(state, State::default());

    let Origin::Quarantined { moved_to, .. } = origin else {
        panic!("expected quarantine, got {origin:?}");
    };
    assert!(moved_to.exists(), "quarantined file must be kept");
    assert_eq!(
        std::fs::read(&moved_to).unwrap(),
        before,
        "quarantined bytes must be untouched"
    );
    assert!(
        !paths.state_file().exists(),
        "the bad file was moved, not copied"
    );
}

#[test]
fn an_oversized_file_is_quarantined_rather_than_parsed() {
    let home = TempHome::new("huge");
    let paths = home.paths();
    paths.ensure_dir().unwrap();
    let blob = vec![b'x'; (MAX_STATE_BYTES + 1) as usize];
    std::fs::write(paths.state_file(), &blob).unwrap();

    let (state, origin) = State::load(&paths).expect("load");
    assert_eq!(state, State::default());
    assert!(matches!(origin, Origin::Quarantined { .. }));
}

#[test]
fn two_corruptions_do_not_overwrite_each_other() {
    let home = TempHome::new("twice");
    let paths = home.paths();
    paths.ensure_dir().unwrap();

    std::fs::write(paths.state_file(), "first garbage").unwrap();
    let (_, a) = State::load(&paths).expect("load");
    std::fs::write(paths.state_file(), "second garbage").unwrap();
    let (_, b) = State::load(&paths).expect("load");

    let (Origin::Quarantined { moved_to: p1, .. }, Origin::Quarantined { moved_to: p2, .. }) =
        (a, b)
    else {
        panic!("both loads should quarantine");
    };
    assert_ne!(
        p1, p2,
        "the first corruption is usually the interesting one"
    );
    assert_eq!(std::fs::read_to_string(&p1).unwrap(), "first garbage");
    assert_eq!(std::fs::read_to_string(&p2).unwrap(), "second garbage");
}

/// State from a newer version is not corrupt. Quarantining it would silently
/// destroy something a future build would read correctly, so this is the one
/// case that refuses to run.
#[test]
fn state_from_a_newer_version_stops_the_run() {
    let home = TempHome::new("future");
    let paths = home.paths();
    paths.ensure_dir().unwrap();
    std::fs::write(
        paths.state_file(),
        format!(r#"{{"schema": {}, "something_new": true}}"#, SCHEMA + 1),
    )
    .unwrap();

    match State::load(&paths) {
        Err(LoadError::FutureSchema { found, supported }) => {
            assert_eq!(found, SCHEMA + 1);
            assert_eq!(supported, SCHEMA);
        }
        other => panic!("expected FutureSchema, got {other:?}"),
    }
    assert!(
        paths.state_file().exists(),
        "a newer version's state must be left exactly where it was"
    );
}

#[test]
fn an_open_run_is_reconciled_as_interrupted() {
    let mut state = State {
        last_run: Some(Run {
            id: 42,
            started_at: 1_700_000_000,
            finished_at: None,
            trigger: "timer".into(),
            outcome: None,
            tasks: Vec::new(),
        }),
        ..Default::default()
    };

    assert_eq!(state.reconcile(), Some(42));
    assert!(state.last_run.is_none());
    assert_eq!(state.history[0].outcome, Some(Outcome::Interrupted));
    assert_eq!(
        state.history[0].finished_at, None,
        "we do not know when it died, and inventing a timestamp would be worse"
    );
}

#[test]
fn reconciling_a_clean_state_reports_nothing() {
    let mut state = State::default();
    assert_eq!(state.reconcile(), None);
    assert!(state.history.is_empty());
}

#[test]
fn history_is_bounded() {
    let mut state = State::default();
    for id in 0..(MAX_HISTORY as u64 + 25) {
        state.push_history(closed_run(id));
    }
    assert_eq!(state.history.len(), MAX_HISTORY);
    assert_eq!(state.history[0].id, MAX_HISTORY as u64 + 24, "newest first");
}

#[test]
fn unknown_fields_are_ignored_rather_than_fatal() {
    let home = TempHome::new("unknown");
    let paths = home.paths();
    paths.ensure_dir().unwrap();
    std::fs::write(
        paths.state_file(),
        format!(r#"{{"schema": {SCHEMA}, "next_run_id": 3, "a_field_from_later": 1}}"#),
    )
    .unwrap();

    let (state, origin) = State::load(&paths).expect("load");
    assert_eq!(origin, Origin::Existing);
    assert_eq!(state.next_run_id, 3);
}
