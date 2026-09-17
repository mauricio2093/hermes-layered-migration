//! `backup-freshness`: evidence, not `mtime`.
//!
//! Every test builds its own throwaway `HERMES_HOME`. Nothing here can reach
//! the real one, and nothing here writes inside a backup.

mod common;

use std::fs;
use std::path::{Path, PathBuf};

use common::TempHome;
use hermes_maint_core::task::Observation;
use hermes_maint_core::tasks::backup_freshness::{
    format_iso8601, human_duration, judge, parse_iso8601, scan, Reject, Scan, Verified,
    DEFAULT_MAX_AGE_SECONDS, FUTURE_TOLERANCE_SECONDS,
};

/// A fixed instant, so nothing depends on when the suite runs.
const NOW_ISO: &str = "2026-09-17T12:00:00-05:00";

fn now() -> i64 {
    parse_iso8601(NOW_ISO).expect("the reference instant must parse")
}

// --- fixtures ---------------------------------------------------------------

fn backups_dir(home: &TempHome) -> PathBuf {
    home.root().join("backups").join("independiente")
}

/// A backup directory carrying the exact evidence the real script writes.
fn write_backup(home: &TempHome, name: &str, timestamp: &str, flags: [bool; 5]) -> PathBuf {
    let dir = backups_dir(home).join(name);
    fs::create_dir_all(&dir).unwrap();
    let verified = flags.iter().all(|f| *f);
    let body = format!(
        r#"{{
  "timestamp": "{timestamp}",
  "archive": "/somewhere/hermes-home-{name}.tar.gz",
  "size_bytes": 115597590,
  "backup": {{
    "created": {},
    "archive_integrity": {},
    "database_integrity": {},
    "manifest_integrity": {},
    "restore_verified": {}
  }},
  "backup_verified": {verified}
}}"#,
        flags[0], flags[1], flags[2], flags[3], flags[4]
    );
    fs::write(dir.join("state.json"), body).unwrap();
    // The rest of a real backup, so the task is seen to ignore it.
    fs::write(dir.join("MANIFEST.sha256"), "deadbeef  ./state.json\n").unwrap();
    fs::write(
        dir.join(format!("hermes-home-{name}.tar.gz")),
        b"not really a tarball",
    )
    .unwrap();
    dir
}

const ALL_OK: [bool; 5] = [true, true, true, true, true];

fn verified(home: &TempHome, name: &str, timestamp: &str) -> PathBuf {
    write_backup(home, name, timestamp, ALL_OK)
}

fn observe(home: &TempHome) -> Observation {
    let s = scan(home.root()).expect("scan");
    judge(&s, now(), DEFAULT_MAX_AGE_SECONDS)
}

// --- 1, 2, 12: the happy path and the threshold ------------------------------

#[test]
fn a_recent_verified_backup_is_ok() {
    let home = TempHome::new("bf-recent");
    verified(&home, "20260917-090000", "2026-09-17T09:00:00-05:00");

    let o = observe(&home);
    assert!(matches!(o, Observation::Ok(_)), "{o:?}");
    assert!(o.detail().contains("3.0h old"), "{}", o.detail());
}

#[test]
fn a_verified_backup_past_the_threshold_is_degraded() {
    let home = TempHome::new("bf-old");
    verified(&home, "20260914-090000", "2026-09-14T09:00:00-05:00");

    let o = observe(&home);
    assert!(matches!(o, Observation::Degraded(_)), "{o:?}");
    assert!(o.detail().contains("threshold"), "{}", o.detail());
}

#[test]
fn a_backup_exactly_at_the_threshold_is_still_ok() {
    let home = TempHome::new("bf-boundary");
    // Exactly 48 hours before the reference instant.
    verified(&home, "20260915-120000", "2026-09-15T12:00:00-05:00");

    let s = scan(home.root()).unwrap();
    assert_eq!(s.verified.len(), 1);
    assert_eq!(now() - s.verified[0].at, DEFAULT_MAX_AGE_SECONDS);

    let o = judge(&s, now(), DEFAULT_MAX_AGE_SECONDS);
    assert!(
        matches!(o, Observation::Ok(_)),
        "the comparison is `>`, not `>=`: an off-by-one here is a nightly \
         false alarm -- {o:?}"
    );

    // And one second past it is not.
    let o = judge(&s, now() + 1, DEFAULT_MAX_AGE_SECONDS);
    assert!(matches!(o, Observation::Degraded(_)), "{o:?}");
}

// --- 3: nothing to look at ---------------------------------------------------

#[test]
fn a_missing_backup_directory_is_skipped_not_degraded() {
    let home = TempHome::new("bf-none");
    let o = observe(&home);
    assert!(
        matches!(o, Observation::Skipped(_)),
        "nothing was inspected, so there is no observation to report -- {o:?}"
    );
}

#[test]
fn an_empty_backup_directory_is_degraded() {
    let home = TempHome::new("bf-empty");
    fs::create_dir_all(backups_dir(&home)).unwrap();

    let o = observe(&home);
    assert!(
        matches!(o, Observation::Degraded(_)),
        "backups are configured here and there are none -- {o:?}"
    );
    assert!(o.detail().contains("empty"), "{}", o.detail());
}

// --- 4, 5, 6: candidates that do not count -----------------------------------

#[test]
fn an_incomplete_backup_is_rejected_not_counted() {
    let home = TempHome::new("bf-incomplete");
    // The script died before writing its evidence: archive present, no state.
    let dir = backups_dir(&home).join("20260917-100000");
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join("hermes-home-20260917-100000.tar.gz"), b"partial").unwrap();

    let s = scan(home.root()).unwrap();
    assert!(s.verified.is_empty(), "a tarball is not evidence");
    assert_eq!(s.rejected[0].why, Reject::NoEvidence);
    assert!(matches!(
        judge(&s, now(), DEFAULT_MAX_AGE_SECONDS),
        Observation::Degraded(_)
    ));
}

#[test]
fn a_backup_that_failed_its_own_checks_is_rejected() {
    let home = TempHome::new("bf-unverified");
    // Everything passed except the one that matters most.
    write_backup(
        &home,
        "20260917-100000",
        "2026-09-17T10:00:00-05:00",
        [true, true, true, true, false],
    );

    let s = scan(home.root()).unwrap();
    assert!(s.verified.is_empty());
    assert_eq!(s.rejected[0].why, Reject::NotVerified);
}

/// The summary and the flags can only disagree if the file was edited after
/// the fact -- and the summary is exactly the field an editor would flip.
#[test]
fn a_hand_edited_verdict_does_not_override_the_flags() {
    let home = TempHome::new("bf-edited");
    let dir = backups_dir(&home).join("20260917-100000");
    fs::create_dir_all(&dir).unwrap();
    fs::write(
        dir.join("state.json"),
        r#"{"timestamp":"2026-09-17T10:00:00-05:00",
            "backup":{"created":true,"archive_integrity":true,
                      "database_integrity":true,"manifest_integrity":true,
                      "restore_verified":false},
            "backup_verified":true}"#,
    )
    .unwrap();

    let s = scan(home.root()).unwrap();
    assert!(s.verified.is_empty(), "the flags win over the summary");
    assert_eq!(s.rejected[0].why, Reject::NotVerified);
}

#[test]
fn corrupt_evidence_is_rejected_without_blinding_the_task() {
    let home = TempHome::new("bf-corrupt");
    let bad = backups_dir(&home).join("20260917-110000");
    fs::create_dir_all(&bad).unwrap();
    fs::write(bad.join("state.json"), "{ this is not json").unwrap();
    // A good one right next to it.
    verified(&home, "20260917-090000", "2026-09-17T09:00:00-05:00");

    let s = scan(home.root()).unwrap();
    assert_eq!(
        s.verified.len(),
        1,
        "one bad file must not hide a good backup"
    );
    assert!(matches!(s.rejected[0].why, Reject::MalformedEvidence(_)));
    assert!(matches!(
        judge(&s, now(), DEFAULT_MAX_AGE_SECONDS),
        Observation::Ok(_)
    ));
}

#[test]
fn evidence_missing_a_flag_is_malformed_not_verified() {
    let home = TempHome::new("bf-partial-json");
    let dir = backups_dir(&home).join("20260917-100000");
    fs::create_dir_all(&dir).unwrap();
    fs::write(
        dir.join("state.json"),
        r#"{"timestamp":"2026-09-17T10:00:00-05:00",
            "backup":{"created":true},"backup_verified":true}"#,
    )
    .unwrap();

    let s = scan(home.root()).unwrap();
    assert!(s.verified.is_empty());
    assert!(matches!(s.rejected[0].why, Reject::MalformedEvidence(_)));
}

// --- 7: the future -----------------------------------------------------------

#[test]
fn a_future_timestamp_is_degraded_not_extremely_fresh() {
    let home = TempHome::new("bf-future");
    verified(&home, "20260918-090000", "2026-09-18T09:00:00-05:00");

    let o = observe(&home);
    let Observation::Degraded(detail) = o else {
        panic!("a backup from the future must not read as fresh");
    };
    assert!(detail.contains("future"), "{detail}");
    assert!(
        !detail.contains('-'),
        "no negative age may appear: {detail}"
    );
}

#[test]
fn small_clock_skew_is_tolerated() {
    let home = TempHome::new("bf-skew");
    let s = Scan {
        directory_present: true,
        verified: vec![Verified {
            name: "within-tolerance".into(),
            // Marginally ahead, as an NTP correction leaves things.
            at: now() + FUTURE_TOLERANCE_SECONDS - 1,
        }],
        rejected: Vec::new(),
    };
    let o = judge(&s, now(), DEFAULT_MAX_AGE_SECONDS);
    assert!(matches!(o, Observation::Ok(_)), "{o:?}");
    assert!(
        o.detail().contains("0s old"),
        "clamped, never negative: {}",
        o.detail()
    );
    drop(home);
}

// --- 8, 9: choosing among several -------------------------------------------

#[test]
fn the_newest_verified_backup_wins() {
    let home = TempHome::new("bf-several");
    verified(&home, "20260915-090000", "2026-09-15T09:00:00-05:00");
    verified(&home, "20260917-090000", "2026-09-17T09:00:00-05:00");
    verified(&home, "20260916-090000", "2026-09-16T09:00:00-05:00");

    let s = scan(home.root()).unwrap();
    assert_eq!(s.verified.len(), 3);
    let o = judge(&s, now(), DEFAULT_MAX_AGE_SECONDS);
    assert!(matches!(o, Observation::Ok(_)), "{o:?}");
    assert!(o.detail().contains("3.0h old"), "{}", o.detail());
}

/// Verification is the filter; recency only orders what survives it.
#[test]
fn a_newer_unverified_backup_does_not_beat_an_older_verified_one() {
    let home = TempHome::new("bf-mixed");
    verified(&home, "20260916-090000", "2026-09-16T09:00:00-05:00");
    write_backup(
        &home,
        "20260917-110000",
        "2026-09-17T11:00:00-05:00",
        [true, true, false, true, true],
    );

    let s = scan(home.root()).unwrap();
    assert_eq!(s.verified.len(), 1);
    assert_eq!(s.rejected.len(), 1);

    let o = judge(&s, now(), DEFAULT_MAX_AGE_SECONDS);
    // 27 hours, from the older verified one -- not the 1 hour the newer,
    // untrustworthy one claims.
    assert!(matches!(o, Observation::Ok(_)), "{o:?}");
    assert!(o.detail().contains("27.0h old"), "{}", o.detail());
    assert!(o.detail().contains("1 rejected"), "{}", o.detail());
}

// --- 10, 11: things that are not backups -------------------------------------

#[test]
fn unrelated_directories_and_files_are_ignored() {
    let home = TempHome::new("bf-junk");
    verified(&home, "20260917-090000", "2026-09-17T09:00:00-05:00");
    fs::create_dir_all(backups_dir(&home).join("scratch")).unwrap();
    fs::create_dir_all(backups_dir(&home).join(".hidden")).unwrap();
    fs::write(backups_dir(&home).join("notes.txt"), "nothing to see").unwrap();

    let s = scan(home.root()).unwrap();
    assert_eq!(s.verified.len(), 1);
    assert_eq!(s.rejected.len(), 3);
    assert!(s
        .rejected
        .iter()
        .any(|r| r.why == Reject::NotADirectory && r.name == "notes.txt"));
    assert!(matches!(
        judge(&s, now(), DEFAULT_MAX_AGE_SECONDS),
        Observation::Ok(_)
    ));
}

/// A symlink is not followed even when it points at a perfectly valid backup.
/// The point is the trust boundary, not the contents: following it would let
/// anything able to write here choose what this task reads.
#[test]
fn a_symlink_is_rejected_rather_than_followed() {
    let home = TempHome::new("bf-symlink");
    let elsewhere = TempHome::new("bf-symlink-target");
    let real = verified(&elsewhere, "20260917-090000", "2026-09-17T09:00:00-05:00");

    fs::create_dir_all(backups_dir(&home)).unwrap();
    std::os::unix::fs::symlink(&real, backups_dir(&home).join("20260917-090000")).unwrap();

    let s = scan(home.root()).unwrap();
    assert!(
        s.verified.is_empty(),
        "a symlink must not become a verified backup, however valid its target"
    );
    assert_eq!(s.rejected[0].why, Reject::Symlink);
    assert!(matches!(
        judge(&s, now(), DEFAULT_MAX_AGE_SECONDS),
        Observation::Degraded(_)
    ));
}

/// Same rule one level down: the evidence itself must be a real file where the
/// backup script put it.
#[test]
fn a_symlinked_state_file_is_not_trusted() {
    let home = TempHome::new("bf-symlink-state");
    let elsewhere = TempHome::new("bf-symlink-state-target");
    let real = verified(&elsewhere, "20260917-090000", "2026-09-17T09:00:00-05:00");

    let dir = backups_dir(&home).join("20260917-090000");
    fs::create_dir_all(&dir).unwrap();
    std::os::unix::fs::symlink(real.join("state.json"), dir.join("state.json")).unwrap();

    let s = scan(home.root()).unwrap();
    assert!(s.verified.is_empty());
    assert!(matches!(s.rejected[0].why, Reject::UnreadableEvidence(_)));
}

// --- 13: it changes nothing --------------------------------------------------

fn snapshot(root: &Path) -> Vec<(PathBuf, u64, std::time::SystemTime, Vec<u8>)> {
    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(&dir).unwrap() {
            let entry = entry.unwrap();
            let meta = entry.path().symlink_metadata().unwrap();
            if meta.is_dir() {
                stack.push(entry.path());
            } else if meta.is_file() {
                out.push((
                    entry.path(),
                    meta.len(),
                    meta.modified().unwrap(),
                    fs::read(entry.path()).unwrap(),
                ));
            }
        }
    }
    out.sort_by(|a, b| a.0.cmp(&b.0));
    out
}

#[test]
fn the_task_modifies_nothing() {
    let home = TempHome::new("bf-readonly");
    verified(&home, "20260917-090000", "2026-09-17T09:00:00-05:00");
    verified(&home, "20260916-090000", "2026-09-16T09:00:00-05:00");
    write_backup(
        &home,
        "20260915-090000",
        "2026-09-15T09:00:00-05:00",
        [true, false, true, true, true],
    );
    let broken = backups_dir(&home).join("broken");
    fs::create_dir_all(&broken).unwrap();
    fs::write(broken.join("state.json"), "{ not json").unwrap();

    let before = snapshot(home.root());
    let _ = scan(home.root()).unwrap();
    let after = snapshot(home.root());

    assert_eq!(before.len(), after.len(), "no file appeared or vanished");
    assert_eq!(before, after, "no file's size, mtime or contents changed");
}

// --- timestamps --------------------------------------------------------------

#[test]
fn the_real_format_parses() {
    // Exactly what the backup script writes, taken from a real state.json.
    let t = parse_iso8601("2026-09-16T18:22:49.963251-05:00").expect("real format");
    let same_instant = parse_iso8601("2026-09-16T23:22:49Z").expect("UTC");
    assert_eq!(t, same_instant, "the offset must actually be applied");
}

#[test]
fn offsets_are_applied_in_the_right_direction() {
    let utc = parse_iso8601("2026-09-17T12:00:00Z").unwrap();
    assert_eq!(parse_iso8601("2026-09-17T07:00:00-05:00").unwrap(), utc);
    assert_eq!(parse_iso8601("2026-09-17T14:00:00+02:00").unwrap(), utc);
    assert_eq!(parse_iso8601("2026-09-17T14:00:00+0200").unwrap(), utc);
}

#[test]
fn a_timestamp_without_an_offset_is_rejected() {
    // Guessing a timezone would silently move the instant by hours.
    assert_eq!(parse_iso8601("2026-09-17T12:00:00"), None);
    assert_eq!(parse_iso8601("2026-09-17T12:00:00.123"), None);
}

#[test]
fn nonsense_timestamps_are_rejected() {
    for bad in [
        "",
        "not a date",
        "2026-13-01T00:00:00Z",
        "2026-09-32T00:00:00Z",
        "2026-09-17T24:00:00Z",
        "2026-09-17T12:60:00Z",
        "2026-09-17T12:00:00+99:00",
        "2026-09-17X12:00:00Z",
        "2026-09-17T12:00:00.Z",
    ] {
        assert_eq!(parse_iso8601(bad), None, "{bad:?} must not parse");
    }
}

#[test]
fn the_epoch_and_a_leap_year_are_right() {
    assert_eq!(parse_iso8601("1970-01-01T00:00:00Z"), Some(0));
    assert_eq!(parse_iso8601("2000-03-01T00:00:00Z"), Some(951_868_800));
    assert_eq!(parse_iso8601("2024-02-29T12:00:00Z"), Some(1_709_208_000));
}

#[test]
fn durations_read_like_a_human_wrote_them() {
    assert_eq!(human_duration(0), "0s");
    assert_eq!(human_duration(-5), "0s", "never negative");
    assert_eq!(human_duration(45), "45s");
    assert_eq!(human_duration(3600), "60m");
    assert_eq!(human_duration(3 * 3600), "3.0h");
    assert_eq!(human_duration(3 * 86400), "3.0d");
}

#[test]
fn the_parser_and_the_formatter_agree() {
    for epoch in [
        0_i64,
        951_868_800,
        1_709_208_000,
        now(),
        now() - 3 * 86_400,
        -86_400,
    ] {
        let text = format_iso8601(epoch);
        assert_eq!(parse_iso8601(&text), Some(epoch), "round trip of {text}");
    }
}
