//! `gateway-service-health`: the first task that runs a real external program.
//!
//! The unhealthy cases are exercised **only through parser fixtures**. Nothing
//! here stops, starts, restarts or otherwise touches the gateway -- producing
//! a failed service in order to test that we notice it would be a worse idea
//! than the bug it is looking for.

mod common;

use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

use common::{fixture_link, Scratch};
use hermes_maint_core::paths::Paths;
use hermes_maint_core::state::{State, TaskOutcome};
use hermes_maint_core::supervisor::ChildSpec;
use hermes_maint_core::task::{registry, Observation, Task, TaskContext};
use hermes_maint_core::tasks::backup_freshness::format_iso8601;
use hermes_maint_core::tasks::external::ExternalTask;
use hermes_maint_core::tasks::gateway_health::{
    interpret_gateway, judge_service_state, parse, runtime_dir, task, ParseError, ServiceState,
    ARGS, HEALTHY, SYSTEMCTL, UNIT,
};
use hermes_maint_core::{Exit, Runner, Trigger};

// --- 1 to 10: the parser and the verdict, on fixtures ------------------------

fn state(load: &str, active: &str, sub: &str) -> ServiceState {
    ServiceState {
        load_state: load.into(),
        active_state: active.into(),
        sub_state: sub.into(),
    }
}

fn verdict(text: &str) -> Observation {
    judge_service_state(&parse(text).expect("this fixture must parse"))
}

#[test]
fn the_healthy_triple_is_ok() {
    let o = verdict("LoadState=loaded\nActiveState=active\nSubState=running\n");
    assert!(matches!(o, Observation::Ok(_)), "{o:?}");
    assert!(o.detail().contains(UNIT));
    assert!(o.detail().contains("active (running)"), "{}", o.detail());
}

#[test]
fn the_healthy_triple_is_the_one_observed_on_this_host() {
    // Pinned so the constant and the parser cannot drift apart.
    let s = state(HEALTHY.0, HEALTHY.1, HEALTHY.2);
    assert!(matches!(judge_service_state(&s), Observation::Ok(_)));
    assert_eq!(HEALTHY, ("loaded", "active", "running"));
}

#[test]
fn a_stopped_service_is_degraded() {
    let o = verdict("LoadState=loaded\nActiveState=inactive\nSubState=dead\n");
    let Observation::Degraded(detail) = o else {
        panic!("a stopped gateway is not healthy");
    };
    assert!(detail.contains("not running"), "{detail}");
    assert!(detail.contains("ActiveState=inactive"), "{detail}");
}

#[test]
fn a_failed_service_is_degraded() {
    let o = verdict("LoadState=loaded\nActiveState=failed\nSubState=failed\n");
    let Observation::Degraded(detail) = o else {
        panic!("expected degraded");
    };
    assert!(detail.contains("has failed"), "{detail}");
}

/// A unit this host expects and does not have. The precondition holds -- this
/// machine is supposed to run that gateway -- so its absence is a finding, not
/// a reason to skip.
#[test]
fn a_missing_unit_is_degraded_not_skipped() {
    // Exactly what systemctl prints for an unknown unit, verified on the host.
    let text = "LoadState=not-found\nActiveState=inactive\nSubState=dead\n";
    let o = verdict(text);
    let Observation::Degraded(detail) = o else {
        panic!("a missing gateway must be reported, not skipped");
    };
    assert!(detail.contains("does not exist"), "{detail}");
}

#[test]
fn a_service_in_transition_is_degraded() {
    for (active, sub) in [
        ("activating", "start"),
        ("deactivating", "stop"),
        ("reloading", "reload"),
    ] {
        let o = verdict(&format!(
            "LoadState=loaded\nActiveState={active}\nSubState={sub}\n"
        ));
        let Observation::Degraded(detail) = o else {
            panic!("{active} must not read as healthy");
        };
        assert!(detail.contains("in transition"), "{detail}");
    }
}

#[test]
fn a_masked_or_unloadable_unit_is_degraded() {
    for (load, expect) in [("masked", "masked"), ("error", "could not be loaded")] {
        let o = verdict(&format!(
            "LoadState={load}\nActiveState=inactive\nSubState=dead\n"
        ));
        let Observation::Degraded(detail) = o else {
            panic!("expected degraded for {load}");
        };
        assert!(detail.contains(expect), "{detail}");
    }
}

/// The conservative default, and the reason for it: a state we have never seen
/// is not evidence of health.
#[test]
fn an_unknown_state_is_degraded_never_promoted_to_ok() {
    let o = verdict("LoadState=loaded\nActiveState=quantum\nSubState=undecided\n");
    let Observation::Degraded(detail) = o else {
        panic!("an unrecognised state must never read as healthy");
    };
    assert!(detail.contains("does not recognise"), "{detail}");

    // Including the near miss: active, but not running.
    let o = verdict("LoadState=loaded\nActiveState=active\nSubState=exited\n");
    assert!(matches!(o, Observation::Degraded(_)), "{o:?}");
    assert!(o.detail().contains("unexpected substate"), "{}", o.detail());
}

#[test]
fn an_empty_value_is_degraded_not_a_parse_error() {
    // The structure is intact; the content is simply not something we can call
    // healthy.
    let parsed = parse("LoadState=loaded\nActiveState=\nSubState=dead\n").expect("parses");
    assert_eq!(parsed.active_state, "");
    assert!(matches!(
        judge_service_state(&parsed),
        Observation::Degraded(_)
    ));
}

#[test]
fn a_missing_property_is_a_parse_error() {
    // The observation failed; it did not observe something bad.
    assert_eq!(
        parse("LoadState=loaded\nActiveState=active\n"),
        Err(ParseError::Missing("SubState"))
    );
    // Empty input: the first required property, reported deterministically.
    assert_eq!(parse(""), Err(ParseError::Missing("LoadState")));
}

#[test]
fn contradictory_properties_are_a_parse_error() {
    assert_eq!(
        parse("LoadState=loaded\nActiveState=active\nActiveState=failed\nSubState=running\n"),
        Err(ParseError::Contradictory("ActiveState")),
        "systemctl does not do this; something that does is not systemctl"
    );
    // The same value twice is harmless.
    assert!(
        parse("LoadState=loaded\nLoadState=loaded\nActiveState=active\nSubState=running").is_ok()
    );
}

#[test]
fn junk_and_unexpected_properties_are_ignored() {
    let text = "\
=leading equals
not a property line
LoadState=loaded

Description=Hermes Agent Gateway
ActiveState=active
MainPID=1234
SubState=running
FragmentPath=/somewhere/with=an=equals/sign
";
    let parsed = parse(text).expect("a new systemd property must not break this");
    assert_eq!(parsed, state("loaded", "active", "running"));
}

#[test]
fn field_order_does_not_matter() {
    let forward = parse("LoadState=loaded\nActiveState=active\nSubState=running").unwrap();
    let backward = parse("SubState=running\nActiveState=active\nLoadState=loaded").unwrap();
    assert_eq!(forward, backward);
}

#[test]
fn a_trailing_newline_is_optional() {
    let with = parse("LoadState=loaded\nActiveState=active\nSubState=running\n").unwrap();
    let without = parse("LoadState=loaded\nActiveState=active\nSubState=running").unwrap();
    let crlf = parse("LoadState=loaded\r\nActiveState=active\r\nSubState=running\r\n").unwrap();
    assert_eq!(with, without);
    assert_eq!(with, crlf);
}

// --- the constants are what was verified on the host --------------------------

#[test]
fn the_command_contains_no_action() {
    assert_eq!(ARGS[0], "--user");
    assert_eq!(ARGS[1], "show", "show is the only subcommand allowed here");
    for forbidden in [
        "start",
        "stop",
        "restart",
        "reload",
        "enable",
        "disable",
        "mask",
        "kill",
        "daemon-reload",
        "set-property",
        "edit",
    ] {
        assert!(
            !ARGS.contains(&forbidden),
            "{forbidden} has no business in a read-only observation"
        );
    }
    assert!(ARGS.contains(&"--no-pager"));
}

#[test]
fn systemctl_is_where_the_preflight_can_accept_it() {
    let meta = fs::symlink_metadata(SYSTEMCTL).expect("systemctl must exist on this host");
    assert!(
        !meta.file_type().is_symlink(),
        "the pre-flight refuses symlinks"
    );
    assert!(meta.is_file());
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    let mode = meta.permissions().mode() & 0o7777;
    assert_eq!(mode & 0o022, 0, "must not be writable by group or other");
    assert!(mode & 0o111 != 0, "must be executable");
    // Root-owned, which the pre-flight accepts alongside files owned by us.
    assert!(meta.uid() == 0 || meta.uid() == unsafe { libc::geteuid() });
}

#[test]
fn the_runtime_dir_is_derived_not_inherited() {
    let derived = runtime_dir();
    assert_eq!(
        derived,
        PathBuf::from(format!("/run/user/{}", unsafe { libc::geteuid() }))
    );
    // Deliberately not read from our own environment: under a timer it may be
    // absent, and copying a stale one would be worse than computing it.
    if let Ok(inherited) = std::env::var("XDG_RUNTIME_DIR") {
        assert_eq!(derived, PathBuf::from(inherited), "and it agrees here");
    }
}

// --- 11 to 14: against the real system, read-only ------------------------------

/// What `systemctl` says right now, asked directly by the test.
fn observe_directly() -> Option<Vec<(String, String)>> {
    let out = Command::new(SYSTEMCTL)
        .args([
            "--user",
            "show",
            UNIT,
            "-p",
            "LoadState",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "MainPID",
            "-p",
            "NRestarts",
        ])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    Some(
        String::from_utf8_lossy(&out.stdout)
            .lines()
            .filter_map(|l| l.split_once('='))
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect(),
    )
}

fn field(props: &[(String, String)], key: &str) -> String {
    props
        .iter()
        .find(|(k, _)| k == key)
        .map(|(_, v)| v.clone())
        .unwrap_or_default()
}

#[test]
fn the_real_query_runs_and_is_understood() {
    let Some(before) = observe_directly() else {
        eprintln!("skipping: no usable user systemd manager on this host");
        return;
    };

    let scratch = Scratch::new("gw-real");
    let paths = scratch.paths();
    let report = task()
        .run(&TaskContext { paths: &paths })
        .expect("the task reports rather than erroring");

    // Whatever the gateway's state, the *observation* must have worked.
    assert_ne!(
        report.outcome,
        TaskOutcome::Failed,
        "could not observe the unit: {}",
        report.detail
    );
    assert_ne!(report.outcome, TaskOutcome::Timeout);
    assert_eq!(report.exit, Some(0), "systemctl itself must have succeeded");

    // And it must agree with what systemctl told the test directly.
    let expected = state(
        &field(&before, "LoadState"),
        &field(&before, "ActiveState"),
        &field(&before, "SubState"),
    );
    let expected_outcome = match judge_service_state(&expected) {
        Observation::Ok(_) => TaskOutcome::Ok,
        _ => TaskOutcome::Degraded,
    };
    assert_eq!(report.outcome, expected_outcome);

    println!(
        "  observed: LoadState={} ActiveState={} SubState={} -> {:?}",
        expected.load_state, expected.active_state, expected.sub_state, report.outcome
    );

    // --- 14 / §10: the query changed nothing.
    let after = observe_directly().expect("still observable");
    assert_eq!(
        field(&after, "ActiveState"),
        field(&before, "ActiveState"),
        "the observation must not have changed the service"
    );
    assert_eq!(field(&after, "SubState"), field(&before, "SubState"));
    let restarts_before: u64 = field(&before, "NRestarts").parse().unwrap_or(0);
    let restarts_after: u64 = field(&after, "NRestarts").parse().unwrap_or(0);
    assert!(
        restarts_after <= restarts_before,
        "NRestarts went from {restarts_before} to {restarts_after}: something restarted the unit"
    );
    // MainPID is compared loosely on purpose: an unrelated restart during the
    // test would change it, and a test that fails for that reason is noise.
    if field(&after, "MainPID") != field(&before, "MainPID") {
        eprintln!("note: MainPID changed during the test, from an external cause");
    }
}

// --- 12: the three global exit paths, without touching the gateway --------------

fn plant_backup(scratch: &Scratch) {
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
            format_iso8601(now - 3600)
        ),
    )
    .unwrap();
}

/// A `gateway-service-health` whose `systemctl` is the fixture, printing
/// whatever the test wants. The interpreter is the real one.
fn fake_gateway(scratch: &Scratch, args: Vec<String>) -> ExternalTask {
    let program = fixture_link(scratch);
    ExternalTask::new(
        "gateway-service-health",
        "a fixture standing in for systemctl",
        Box::new(move |paths: &Paths| {
            ChildSpec::new(
                "fake-systemctl",
                program.clone(),
                paths.hermes_home(),
                Duration::from_secs(10),
                Duration::from_secs(1),
            )
            .args(&args)
        }),
    )
    .with_interpreter(Box::new(interpret_gateway))
}

fn run_with(scratch: &Scratch, gateway: ExternalTask) -> (Exit, State) {
    let paths = scratch.paths();
    let mut runner = Runner::start(paths.clone(), Trigger::Timer, false).expect("start");
    let tasks: Vec<Box<dyn Task>> = vec![
        Box::new(hermes_maint_core::tasks::disk_space::DiskSpace::default()),
        Box::new(hermes_maint_core::tasks::backup_freshness::BackupFreshness::default()),
        Box::new(gateway),
    ];
    runner.run_tasks(&tasks);
    let exit = runner.finish();
    let (state, _) = State::load(&paths).expect("load");
    (exit, state)
}

fn lines(load: &str, active: &str, sub: &str) -> Vec<String> {
    vec![
        "echo-lines".to_string(),
        format!("LoadState={load}"),
        format!("ActiveState={active}"),
        format!("SubState={sub}"),
    ]
}

#[test]
fn a_healthy_gateway_gives_exit_zero() {
    let scratch = Scratch::new("gw-exit0");
    plant_backup(&scratch);
    let (exit, state) = run_with(
        &scratch,
        fake_gateway(&scratch, lines("loaded", "active", "running")),
    );

    assert_eq!(exit, Exit::Ok);
    let ids: Vec<&str> = state.history[0]
        .tasks
        .iter()
        .map(|t| t.id.as_str())
        .collect();
    assert_eq!(
        ids,
        ["disk-space", "backup-freshness", "gateway-service-health"]
    );
    for t in &state.history[0].tasks {
        assert_eq!(t.outcome, TaskOutcome::Ok, "{}", t.id);
    }
}

#[test]
fn an_inactive_gateway_gives_exit_six() {
    let scratch = Scratch::new("gw-exit6");
    plant_backup(&scratch);
    let (exit, state) = run_with(
        &scratch,
        fake_gateway(&scratch, lines("loaded", "inactive", "dead")),
    );

    assert_eq!(
        exit,
        Exit::Degraded,
        "the service is unwell; the run is not"
    );
    let gw = &state.history[0].tasks[2];
    assert_eq!(gw.outcome, TaskOutcome::Degraded);
    assert_eq!(gw.exit, Some(0), "systemctl succeeded; the news was bad");
    assert!(gw.detail.as_ref().unwrap().contains("not running"));
}

#[test]
fn a_broken_observation_gives_exit_four() {
    let scratch = Scratch::new("gw-exit4");
    plant_backup(&scratch);
    // Stands in for systemctl failing to reach the user manager.
    let broken = fake_gateway(&scratch, vec!["exit".to_string(), "1".to_string()]);
    let (exit, state) = run_with(&scratch, broken);

    assert_eq!(exit, Exit::Partial);
    let gw = &state.history[0].tasks[2];
    assert_eq!(
        gw.outcome,
        TaskOutcome::Failed,
        "failing to observe is not the same as observing a failure"
    );
    assert!(gw.detail.as_ref().unwrap().contains("systemctl exited 1"));
}

#[test]
fn uninterpretable_output_is_a_failed_observation() {
    let scratch = Scratch::new("gw-garbage");
    plant_backup(&scratch);
    let garbage = fake_gateway(
        &scratch,
        vec![
            "echo-out".to_string(),
            "not systemctl output at all".to_string(),
        ],
    );
    let (exit, state) = run_with(&scratch, garbage);

    assert_eq!(exit, Exit::Partial);
    let gw = &state.history[0].tasks[2];
    assert_eq!(gw.outcome, TaskOutcome::Failed);
    assert!(gw.detail.as_ref().unwrap().contains("could not interpret"));
}

// --- 7: nothing bulky reaches state.json -----------------------------------------

#[test]
fn raw_systemctl_output_is_not_persisted() {
    let scratch = Scratch::new("gw-noise");
    plant_backup(&scratch);
    // A realistic `systemctl show` with no property filter is hundreds of
    // lines; this stands in for one.
    let mut args = lines("loaded", "active", "running");
    for i in 0..500 {
        args.push(format!(
            "Irrelevant{i}=some quite long value repeated over and over"
        ));
    }
    let (exit, state) = run_with(&scratch, fake_gateway(&scratch, args));

    assert_eq!(exit, Exit::Ok);
    let gw = &state.history[0].tasks[2];
    let detail = gw.detail.as_ref().unwrap();
    assert!(
        detail.len() < 120,
        "detail was {} bytes: {detail}",
        detail.len()
    );
    assert!(
        !detail.contains("Irrelevant"),
        "raw properties leaked: {detail}"
    );
    assert!(
        gw.output_bytes.unwrap() > 10_000,
        "the size is still remembered"
    );
}

// --- 11: the registry ---------------------------------------------------------------

#[test]
fn the_registry_now_holds_exactly_three_tasks() {
    let ids: Vec<&str> = registry().iter().map(|t| t.id()).collect();
    assert_eq!(
        ids,
        ["disk-space", "backup-freshness", "gateway-service-health"]
    );
}
