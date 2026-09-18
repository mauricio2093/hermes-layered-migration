//! The task pipeline, and the first real task.

mod common;

use common::TempHome;
use hermes_maint_core::state::{Outcome, State, TaskOutcome, MAX_DETAIL_CHARS};
use hermes_maint_core::task::{registry, Observation, Task, TaskContext, TaskError, TaskReport};
use hermes_maint_core::tasks::disk_space::{
    human_bytes, judge, statvfs, Usage, MIN_FREE_BYTES, MIN_FREE_FRACTION, MIN_FREE_INODE_FRACTION,
};
use hermes_maint_core::{Exit, Runner, Trigger};

// --- thresholds, as pure arithmetic -----------------------------------------

const TB: u64 = 1024 * 1024 * 1024 * 1024;

/// A filesystem with no reserved blocks, so `usable` equals its size. The
/// reserve is exercised separately.
fn usage(total: u64, free: u64) -> Usage {
    Usage {
        size_bytes: total,
        usable_bytes: total,
        free_bytes: free,
        total_inodes: 1_000_000,
        free_inodes: 900_000,
    }
}

#[test]
fn a_roomy_disk_is_ok() {
    let o = judge(&usage(TB, TB / 2));
    assert!(matches!(o, Observation::Ok(_)), "{o:?}");
    assert!(o.detail().contains("free of"));
}

#[test]
fn a_small_absolute_amount_is_degraded_however_big_the_disk() {
    // 1 GiB free on a 1 TiB disk: the percentage says 0.1%, but the floor is
    // the check that matters when a disk is huge.
    let o = judge(&usage(TB, 1024 * 1024 * 1024));
    let Observation::Degraded(detail) = o else {
        panic!("expected degraded");
    };
    assert!(detail.contains("floor"), "{detail}");
}

#[test]
fn a_small_percentage_is_degraded_however_big_the_free_amount() {
    // 40 GiB free on a 1 TiB disk: far above the absolute floor, and still
    // only 4% -- on a large disk the floor arrives much too late.
    let total = TB;
    let free = (total as f64 * 0.04) as u64;
    assert!(
        free > MIN_FREE_BYTES,
        "the floor must not be what fires here"
    );
    let Observation::Degraded(detail) = judge(&usage(total, free)) else {
        panic!("expected degraded");
    };
    assert!(detail.contains("free"), "{detail}");
    assert!(!detail.contains("floor"), "wrong reason: {detail}");
}

#[test]
fn exhausted_inodes_are_degraded_even_with_bytes_to_spare() {
    // The confusing failure: plenty of room, writes fail anyway.
    let u = Usage {
        size_bytes: TB,
        usable_bytes: TB,
        free_bytes: TB / 2,
        total_inodes: 1_000_000,
        free_inodes: 1_000,
    };
    let Observation::Degraded(detail) = judge(&u) else {
        panic!("expected degraded");
    };
    assert!(detail.contains("inodes"), "{detail}");
}

#[test]
fn a_filesystem_without_inode_accounting_is_not_penalised() {
    let u = Usage {
        size_bytes: TB,
        usable_bytes: TB,
        free_bytes: TB / 2,
        total_inodes: 0,
        free_inodes: 0,
    };
    assert_eq!(u.free_inode_fraction(), None);
    let o = judge(&u);
    assert!(matches!(o, Observation::Ok(_)), "{o:?}");
    assert!(!o.detail().contains("inodes"), "{o:?}");
}

#[test]
fn the_boundary_is_not_degraded() {
    // Exactly at the thresholds is fine; below them is not. Worth pinning so
    // an off-by-one does not turn into a nightly false alarm.
    let total = TB;
    // `ceil`, so the value really is at or just above the threshold: rounding
    // down would put the test a byte under it and prove the opposite.
    let free = ((total as f64) * MIN_FREE_FRACTION).ceil() as u64;
    assert!(free > MIN_FREE_BYTES);
    let u = Usage {
        size_bytes: total,
        usable_bytes: total,
        free_bytes: free,
        total_inodes: 1_000_000,
        free_inodes: (1_000_000.0 * MIN_FREE_INODE_FRACTION).ceil() as u64,
    };
    assert!(matches!(judge(&u), Observation::Ok(_)));
}

/// The case that made the three-field shape necessary: ext4 reserves ~5% of
/// itself for root. Counting that reserve in the denominator while excluding
/// it from the numerator reports a disk as fuller than `df` says it is.
#[test]
fn reserved_blocks_do_not_make_a_healthy_disk_look_full() {
    let size = TB;
    let reserve = size / 20; // the classic 5%
    let used = size / 10;
    let free = size - reserve - used;
    let u = Usage {
        size_bytes: size,
        usable_bytes: used + free,
        free_bytes: free,
        total_inodes: 1_000_000,
        free_inodes: 900_000,
    };

    // Against the whole size this reads as 85% free. Against what is actually
    // usable -- used plus available, which is what `df` divides by -- it is
    // 89.5%. The difference is the reserve, and it is not ours to worry about.
    let naive = free as f64 / size as f64;
    assert!((naive - 0.85).abs() < 0.001, "{naive}");
    assert!(
        (u.free_fraction() - 0.8947).abs() < 0.001,
        "{}",
        u.free_fraction()
    );
    assert!(
        u.free_fraction() > naive,
        "the reserve must not count against us"
    );
    assert!(matches!(judge(&u), Observation::Ok(_)));
}

#[test]
fn an_empty_filesystem_does_not_divide_by_zero() {
    let u = Usage {
        size_bytes: 0,
        usable_bytes: 0,
        free_bytes: 0,
        total_inodes: 0,
        free_inodes: 0,
    };
    assert_eq!(u.free_fraction(), 1.0);
    let _ = judge(&u);
}

#[test]
fn byte_sizes_read_like_a_human_wrote_them() {
    assert_eq!(human_bytes(0), "0 B");
    assert_eq!(human_bytes(512), "512 B");
    assert_eq!(human_bytes(1024), "1.0 KiB");
    assert_eq!(human_bytes(1536), "1.5 KiB");
    assert_eq!(human_bytes(3 * 1024 * 1024 * 1024), "3.0 GiB");
}

// --- the syscall ------------------------------------------------------------

#[test]
fn statvfs_reports_something_plausible_about_this_machine() {
    let home = TempHome::new("statvfs");
    let u = statvfs(home.root()).expect("statvfs on a directory that exists");
    assert!(u.size_bytes > 0, "a real filesystem has a size");
    assert!(u.free_bytes <= u.usable_bytes, "free cannot exceed usable");
    assert!(
        u.usable_bytes <= u.size_bytes,
        "reserved blocks mean usable never exceeds the size"
    );
    assert!(u.free_fraction() > 0.0 && u.free_fraction() <= 1.0);
}

#[test]
fn statvfs_on_a_missing_path_is_an_error_not_a_panic() {
    let err = statvfs(std::path::Path::new("/definitely/not/here/at/all"));
    assert!(err.is_err());
}

// --- the pipeline ----------------------------------------------------------

struct Fake {
    id: &'static str,
    result: Result<Observation, String>,
}

impl Task for Fake {
    fn id(&self) -> &'static str {
        self.id
    }
    fn describe(&self) -> &'static str {
        "a task that exists only in tests"
    }
    fn run(&self, _ctx: &TaskContext<'_>) -> Result<TaskReport, TaskError> {
        self.result.clone().map(TaskReport::from).map_err(TaskError)
    }
}

fn ok(id: &'static str, detail: &str) -> Box<dyn Task> {
    Box::new(Fake {
        id,
        result: Ok(Observation::Ok(detail.to_string())),
    })
}

fn degraded(id: &'static str, detail: &str) -> Box<dyn Task> {
    Box::new(Fake {
        id,
        result: Ok(Observation::Degraded(detail.to_string())),
    })
}

fn failing(id: &'static str, why: &str) -> Box<dyn Task> {
    Box::new(Fake {
        id,
        result: Err(why.to_string()),
    })
}

#[test]
fn the_real_registry_runs_and_lands_in_state() {
    let home = TempHome::new("registry");
    let paths = home.paths();

    let mut runner = Runner::start(paths.clone(), Trigger::Timer, false).expect("start");
    let tasks = registry();
    let ids: Vec<_> = tasks.iter().map(|t| t.id()).collect();
    assert_eq!(
        ids,
        ["disk-space", "backup-freshness"],
        "cheap and local first"
    );
    assert_eq!(
        ids.iter().collect::<std::collections::HashSet<_>>().len(),
        ids.len(),
        "task ids are recorded in state, so they must be unique"
    );

    runner.run_tasks(&tasks);
    // This throwaway home has no backups, so backup-freshness is skipped and
    // the run is partial. That is the honest answer, not a failure.
    assert_eq!(runner.finish(), Exit::Partial);

    let (state, _) = State::load(&paths).expect("load");
    let recorded: Vec<_> = state.history[0]
        .tasks
        .iter()
        .map(|t| t.id.as_str())
        .collect();
    assert_eq!(recorded, ids, "every registered task ran, in order");

    let disk = &state.history[0].tasks[0];
    assert_eq!(disk.exit, None, "an in-process task has no exit status");
    assert!(disk.detail.as_ref().unwrap().contains("free of"));
}

#[test]
fn a_degraded_task_makes_the_run_exit_six() {
    let home = TempHome::new("degraded");
    let paths = home.paths();
    let mut runner = Runner::start(paths.clone(), Trigger::Timer, false).expect("start");
    runner.run_tasks(&[ok("fine", "all good"), degraded("worrying", "nearly full")]);
    assert_eq!(runner.finish(), Exit::Degraded);

    let (state, _) = State::load(&paths).expect("load");
    assert_eq!(state.history[0].outcome, Some(Outcome::Degraded));
    assert_eq!(state.history[0].tasks[1].outcome, TaskOutcome::Degraded);
}

#[test]
fn a_failing_task_does_not_stop_the_others() {
    let home = TempHome::new("failing");
    let paths = home.paths();
    let mut runner = Runner::start(paths.clone(), Trigger::Timer, false).expect("start");
    runner.run_tasks(&[
        failing("broken", "the check could not run"),
        ok("after", "still ran"),
    ]);
    assert_eq!(runner.finish(), Exit::Partial);

    let (state, _) = State::load(&paths).expect("load");
    let ids: Vec<_> = state.history[0]
        .tasks
        .iter()
        .map(|t| t.id.as_str())
        .collect();
    assert_eq!(
        ids,
        ["broken", "after"],
        "an independent task must still run"
    );
    assert_eq!(state.history[0].tasks[0].outcome, TaskOutcome::Failed);
    assert_eq!(state.history[0].tasks[1].outcome, TaskOutcome::Ok);
}

/// Failed and degraded must not collapse into each other: one means the check
/// did not run, the other means it ran and the news is bad.
#[test]
fn a_failed_check_is_not_a_degraded_one() {
    let home = TempHome::new("distinct");
    let mut runner = Runner::start(home.paths(), Trigger::Timer, false).expect("start");
    runner.run_tasks(&[failing("broken", "cannot read the filesystem")]);
    assert_eq!(runner.outcome(), Outcome::Partial);
    assert_ne!(runner.outcome(), Outcome::Degraded);
    runner.finish();
}

#[test]
fn a_dry_run_executes_no_task() {
    let home = TempHome::new("dry-tasks");
    let paths = home.paths();
    let mut runner = Runner::start(paths.clone(), Trigger::Manual, true).expect("start");
    runner.run_tasks(&[degraded("would-be-degraded", "nearly full")]);
    assert_eq!(
        runner.outcome(),
        Outcome::Ok,
        "a dry-run reports no observations because it made none"
    );
    assert_eq!(runner.finish(), Exit::Ok);
    assert!(!paths.state_file().exists());
}

#[test]
fn a_talkative_task_cannot_grow_the_state_file() {
    let home = TempHome::new("talkative");
    let paths = home.paths();
    let mut runner = Runner::start(paths.clone(), Trigger::Timer, false).expect("start");
    runner.run_tasks(&[ok("verbose", &"x".repeat(10_000))]);
    runner.finish();

    let (state, _) = State::load(&paths).expect("load");
    let detail = state.history[0].tasks[0].detail.as_ref().unwrap();
    assert_eq!(detail.chars().count(), MAX_DETAIL_CHARS);
}
