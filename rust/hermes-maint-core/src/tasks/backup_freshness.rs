//! Is there recent evidence of a backup that was actually **verified**?
//!
//! The distinction this task exists for, learned the expensive way in this
//! project: *"a recent backup exists"* and *"a restorable backup exists"* are
//! different claims, and only the second is worth anything at 03:00.
//!
//! So `mtime` is not consulted. Neither is the directory name, nor the
//! presence of a `.tar.gz`. A half-written archive has a perfectly fresh
//! `mtime`. The only thing read is `state.json`, which the backup script
//! writes *after* every one of its checks has run -- including extracting the
//! archive to a temporary directory and validating its contents.
//!
//! Read-only throughout: no shell, no subprocess, no network, no privileges,
//! no `state.db`, and nothing under `backups/` is ever written or moved.
//!
//! The full reasoning, case by case, is in `docs/task-backup-freshness.md`.

use std::path::Path;

use serde::Deserialize;

use crate::task::{Observation, Task, TaskContext, TaskError, TaskReport};

/// Where backups live, relative to `HERMES_HOME`. Compiled in: configuration
/// may never point this somewhere else. A settable path would hand whatever
/// can write the config the power to aim this scan at an arbitrary directory,
/// which is a trust boundary bought for no benefit.
pub const BACKUP_SUBPATH: [&str; 2] = ["backups", "independiente"];

/// The file that decides everything.
pub const EVIDENCE_FILE: &str = "state.json";

/// One missed nightly run of slack; two is an alarm.
pub const DEFAULT_MAX_AGE_SECONDS: i64 = 48 * 3600;

/// Enough for ordinary clock jitter and an NTP correction. Beyond it, a
/// backup claiming to be from the future is a clock problem or a corrupt
/// file, not an extremely fresh backup.
pub const FUTURE_TOLERANCE_SECONDS: i64 = 5 * 60;

/// `state.json` is a few hundred bytes. A megabyte already means something
/// else is going on, and parsing it would turn that into a memory problem.
pub const MAX_EVIDENCE_BYTES: u64 = 64 * 1024;

/// A pathological directory must not turn a maintenance run into a disk scan.
pub const MAX_CANDIDATES: usize = 10_000;

// --- what a candidate turned out to be --------------------------------------

/// Why a directory is not usable evidence. None of these is fatal: one bad
/// backup must not blind the task to a good one beside it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reject {
    /// Not a directory at all.
    NotADirectory,
    /// A symlink where a backup directory belongs. Not followed: it would let
    /// anything able to write here choose what this task reads, and point it
    /// outside the hierarchy entirely.
    Symlink,
    /// No `state.json` -- typically the backup script died before its final
    /// step, which is exactly what an incomplete backup looks like.
    NoEvidence,
    /// `state.json` exists but is not a regular file, is too large, or could
    /// not be read.
    UnreadableEvidence(String),
    /// It parsed as JSON but is not the evidence this task understands.
    MalformedEvidence(String),
    /// It is evidence, and it says the backup was not verified.
    NotVerified,
    /// The declared timestamp could not be understood.
    BadTimestamp(String),
    /// Past [`MAX_CANDIDATES`].
    TooMany,
}

impl std::fmt::Display for Reject {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Reject::NotADirectory => f.write_str("not a directory"),
            Reject::Symlink => f.write_str("a symlink, not followed"),
            Reject::NoEvidence => f.write_str("no state.json (incomplete backup)"),
            Reject::UnreadableEvidence(e) => write!(f, "state.json unreadable: {e}"),
            Reject::MalformedEvidence(e) => write!(f, "state.json malformed: {e}"),
            Reject::NotVerified => f.write_str("backup_verified is not true"),
            Reject::BadTimestamp(e) => write!(f, "unusable timestamp: {e}"),
            Reject::TooMany => f.write_str("beyond the candidate limit"),
        }
    }
}

/// A backup whose evidence says every check passed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Verified {
    /// The directory's own name. Used for reporting only -- never parsed for
    /// a date, and never used to decide anything.
    pub name: String,
    /// The instant declared inside `state.json`, as Unix seconds.
    pub at: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Rejected {
    pub name: String,
    pub why: Reject,
}

/// Everything the filesystem had to say. Separated from the decision so the
/// decision can be tested as pure arithmetic, the way `disk-space` is.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Scan {
    pub directory_present: bool,
    pub verified: Vec<Verified>,
    pub rejected: Vec<Rejected>,
}

// --- the decision, pure -----------------------------------------------------

/// The whole verdict, with no filesystem and no clock of its own.
#[must_use]
pub fn judge(scan: &Scan, now: i64, max_age_seconds: i64) -> Observation {
    if !scan.directory_present {
        // Nothing was inspected. Calling that "degraded" would be inventing an
        // observation about a backup system that is not here.
        return Observation::Skipped(format!(
            "no backup directory at {}; nothing to check",
            BACKUP_SUBPATH.join("/")
        ));
    }

    let Some(newest) = scan.verified.iter().max_by_key(|b| b.at) else {
        // The directory exists, so backups are a thing here. Having none that
        // is trustworthy is a finding, not a missing precondition.
        return Observation::Degraded(if scan.rejected.is_empty() {
            "the backup directory is empty: no backup has ever completed here".to_string()
        } else {
            format!(
                "no verified backup among {} candidate{}: {}",
                scan.rejected.len(),
                plural(scan.rejected.len()),
                summarise(&scan.rejected)
            )
        });
    };

    let context = format!(
        "{} verified backup{}{}",
        scan.verified.len(),
        plural(scan.verified.len()),
        if scan.rejected.is_empty() {
            String::new()
        } else {
            format!(", {} rejected", scan.rejected.len())
        }
    );

    if newest.at > now + FUTURE_TOLERANCE_SECONDS {
        // Never report a negative age. "-3 hours old" is the kind of output
        // that makes a person stop reading the report.
        let ahead = newest.at - now;
        return Observation::Degraded(format!(
            "the newest verified backup declares a timestamp {} in the future; \
             treating it as untrustworthy rather than fresh ({context})",
            human_duration(ahead)
        ));
    }

    // Within tolerance but marginally ahead: clamp rather than go negative.
    let age = (now - newest.at).max(0);

    // `>` and not `>=`: a backup exactly at the threshold is still fine. An
    // off-by-one here is a nightly false alarm.
    if age > max_age_seconds {
        Observation::Degraded(format!(
            "newest verified backup is {} old, over the {} threshold ({context})",
            human_duration(age),
            human_duration(max_age_seconds)
        ))
    } else {
        Observation::Ok(format!(
            "newest verified backup is {} old ({context})",
            human_duration(age)
        ))
    }
}

fn plural(n: usize) -> &'static str {
    if n == 1 {
        ""
    } else {
        "s"
    }
}

/// A compact tally of why candidates were rejected. Counts rather than a list,
/// so a directory with two hundred failures does not produce two hundred
/// lines.
fn summarise(rejected: &[Rejected]) -> String {
    let mut counts: Vec<(String, usize)> = Vec::new();
    for r in rejected {
        let key = r.why.to_string();
        match counts.iter_mut().find(|(k, _)| *k == key) {
            Some((_, n)) => *n += 1,
            None => counts.push((key, 1)),
        }
    }
    counts
        .into_iter()
        .map(|(k, n)| if n == 1 { k } else { format!("{k} (x{n})") })
        .collect::<Vec<_>>()
        .join("; ")
}

#[must_use]
pub fn human_duration(seconds: i64) -> String {
    let s = seconds.max(0);
    match s {
        0..=89 => format!("{s}s"),
        90..=5399 => format!("{:.0}m", s as f64 / 60.0),
        5400..=172_799 => format!("{:.1}h", s as f64 / 3600.0),
        _ => format!("{:.1}d", s as f64 / 86400.0),
    }
}

// --- reading the evidence ---------------------------------------------------

/// The parts of `state.json` this task understands. Every field is required:
/// a file missing one of them is not the evidence the backup script writes.
#[derive(Debug, Deserialize)]
struct Evidence {
    timestamp: String,
    backup_verified: bool,
    backup: Flags,
}

#[derive(Debug, Deserialize)]
struct Flags {
    created: bool,
    archive_integrity: bool,
    database_integrity: bool,
    manifest_integrity: bool,
    restore_verified: bool,
}

impl Flags {
    /// Every independent check passed.
    const fn all(&self) -> bool {
        self.created
            && self.archive_integrity
            && self.database_integrity
            && self.manifest_integrity
            && self.restore_verified
    }
}

/// Walk the backup directory and classify every entry.
pub fn scan(hermes_home: &Path) -> std::io::Result<Scan> {
    let base = BACKUP_SUBPATH
        .iter()
        .fold(hermes_home.to_path_buf(), |p, part| p.join(part));

    let entries = match std::fs::read_dir(&base) {
        Ok(e) => e,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Ok(Scan {
                directory_present: false,
                ..Scan::default()
            })
        }
        // Anything else -- permissions, I/O -- means the check could not run,
        // which is neither "ok" nor "degraded".
        Err(e) => return Err(e),
    };

    let mut scan = Scan {
        directory_present: true,
        ..Scan::default()
    };

    for (n, entry) in entries.enumerate() {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();

        if n >= MAX_CANDIDATES {
            scan.rejected.push(Rejected {
                name,
                why: Reject::TooMany,
            });
            continue;
        }

        // `symlink_metadata` does not traverse: a symlink is seen as a
        // symlink, which is the whole point.
        let meta = match entry.path().symlink_metadata() {
            Ok(m) => m,
            Err(e) => {
                scan.rejected.push(Rejected {
                    name,
                    why: Reject::UnreadableEvidence(e.to_string()),
                });
                continue;
            }
        };

        if meta.file_type().is_symlink() {
            scan.rejected.push(Rejected {
                name,
                why: Reject::Symlink,
            });
            continue;
        }
        if !meta.is_dir() {
            scan.rejected.push(Rejected {
                name,
                why: Reject::NotADirectory,
            });
            continue;
        }

        // One component, joined to a known base. Nothing read from a file is
        // ever used as a path.
        match classify(&base.join(entry.file_name())) {
            Ok(at) => scan.verified.push(Verified { name, at }),
            Err(why) => scan.rejected.push(Rejected { name, why }),
        }
    }

    Ok(scan)
}

/// Read one backup's evidence and decide whether it counts.
fn classify(dir: &Path) -> Result<i64, Reject> {
    let file = dir.join(EVIDENCE_FILE);

    let meta = match file.symlink_metadata() {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Err(Reject::NoEvidence),
        Err(e) => return Err(Reject::UnreadableEvidence(e.to_string())),
    };
    if !meta.is_file() {
        // Includes a symlink: the evidence must be a real file sitting where
        // the backup script put it.
        return Err(Reject::UnreadableEvidence("not a regular file".to_string()));
    }
    if meta.len() > MAX_EVIDENCE_BYTES {
        return Err(Reject::UnreadableEvidence(format!(
            "{} bytes, over the {MAX_EVIDENCE_BYTES} cap",
            meta.len()
        )));
    }

    let raw =
        std::fs::read_to_string(&file).map_err(|e| Reject::UnreadableEvidence(e.to_string()))?;
    let evidence: Evidence =
        serde_json::from_str(&raw).map_err(|e| Reject::MalformedEvidence(e.to_string()))?;

    // Both the summary AND every flag. They can only disagree if the file was
    // edited or corrupted afterwards -- and the summary is exactly the field
    // an editor would flip. Agreement costs one `&&`.
    if !evidence.backup_verified || !evidence.backup.all() {
        return Err(Reject::NotVerified);
    }

    parse_iso8601(&evidence.timestamp)
        .ok_or_else(|| Reject::BadTimestamp(evidence.timestamp.clone()))
}

// --- the task ---------------------------------------------------------------

#[derive(Debug, Clone, Copy)]
pub struct BackupFreshness {
    /// Configuration may eventually set this. It may never set a path or a
    /// command. No configuration file exists yet; this is where one would
    /// land without touching the logic.
    pub max_age_seconds: i64,
}

impl Default for BackupFreshness {
    fn default() -> Self {
        Self {
            max_age_seconds: DEFAULT_MAX_AGE_SECONDS,
        }
    }
}

impl Task for BackupFreshness {
    fn id(&self) -> &'static str {
        "backup-freshness"
    }

    fn describe(&self) -> &'static str {
        "age of the most recent backup whose own checks all passed"
    }

    fn run(&self, ctx: &TaskContext<'_>) -> Result<TaskReport, TaskError> {
        let scan = scan(ctx.paths.hermes_home())
            .map_err(|e| TaskError(format!("could not read the backup directory: {e}")))?;
        Ok(judge(&scan, crate::now() as i64, self.max_age_seconds).into())
    }
}

// --- timestamps -------------------------------------------------------------

/// Parse the subset of ISO 8601 the backup script writes:
/// `YYYY-MM-DDTHH:MM:SS[.ffffff](Z|±HH:MM|±HHMM)`.
///
/// Strict on purpose. A timestamp with no offset is rejected rather than
/// guessed: the script always writes one, so its absence means the file is not
/// what it claims to be, and guessing a timezone would silently move the
/// instant by hours.
#[must_use]
pub fn parse_iso8601(s: &str) -> Option<i64> {
    let b = s.as_bytes();
    if b.len() < 19 || b[4] != b'-' || b[7] != b'-' || b[13] != b':' || b[16] != b':' {
        return None;
    }
    if b[10] != b'T' && b[10] != b' ' {
        return None;
    }

    let year: i64 = s.get(0..4)?.parse().ok()?;
    let month: i64 = s.get(5..7)?.parse().ok()?;
    let day: i64 = s.get(8..10)?.parse().ok()?;
    let hour: i64 = s.get(11..13)?.parse().ok()?;
    let minute: i64 = s.get(14..16)?.parse().ok()?;
    let second: i64 = s.get(17..19)?.parse().ok()?;

    if !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 23
        || minute > 59
        // 60 allows a leap second rather than rejecting a valid instant.
        || second > 60
    {
        return None;
    }

    let mut rest = s.get(19..)?;
    // Fractional seconds are discarded: this task measures in hours.
    if let Some(stripped) = rest.strip_prefix('.') {
        let digits = stripped
            .as_bytes()
            .iter()
            .take_while(|c| c.is_ascii_digit())
            .count();
        if digits == 0 {
            return None;
        }
        rest = stripped.get(digits..)?;
    }

    let offset = parse_offset(rest)?;
    let days = days_from_civil(year, month, day);
    Some(days * 86_400 + hour * 3_600 + minute * 60 + second - offset)
}

/// `Z`, `+HH:MM`, `-HH:MM`, `+HHMM`, `-HHMM`. Anything else, including
/// nothing at all, is not acceptable.
fn parse_offset(s: &str) -> Option<i64> {
    if s == "Z" || s == "z" {
        return Some(0);
    }
    let sign = match s.as_bytes().first()? {
        b'+' => 1,
        b'-' => -1,
        _ => return None,
    };
    let body = s.get(1..)?;
    let (h, m) = match body.len() {
        5 if body.as_bytes()[2] == b':' => (body.get(0..2)?, body.get(3..5)?),
        4 => (body.get(0..2)?, body.get(2..4)?),
        2 => (body, "00"),
        _ => return None,
    };
    let hours: i64 = h.parse().ok()?;
    let minutes: i64 = m.parse().ok()?;
    if hours > 23 || minutes > 59 {
        return None;
    }
    Some(sign * (hours * 3_600 + minutes * 60))
}

/// Days since 1970-01-01 from a civil date. Howard Hinnant's algorithm, which
/// is exact for the whole proleptic Gregorian calendar and needs no table.
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = year - i64::from(month <= 2);
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let doy = (153 * (month + if month > 2 { -3 } else { 9 }) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// Render Unix seconds as `YYYY-MM-DDTHH:MM:SSZ`.
///
/// The task never needs this -- it only reads timestamps. It exists so the
/// parser can be round-tripped, and so tests can build evidence at a chosen
/// instant without shelling out to `date`.
#[must_use]
pub fn format_iso8601(epoch: i64) -> String {
    let days = epoch.div_euclid(86_400);
    let secs = epoch.rem_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z",
        secs / 3600,
        (secs % 3600) / 60,
        secs % 60
    )
}

/// The inverse of [`days_from_civil`], same source.
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (y + i64::from(m <= 2), m, d)
}
