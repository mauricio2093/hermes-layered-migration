//! `hermes-maint` -- scheduled maintenance for a Hermes install.
//!
//! Runs once, reports, exits. It is not a daemon: `systemd.timer` owns the
//! schedule, which is why there is no scheduler in here and never should be.
//!
//! This slice registers **no tasks**. It can take the lock, reconcile its own
//! state, open a run, close it, do a dry-run, and exit with a defined code.
//! Nothing else. The point of a first binary that is almost useless is that
//! "almost useless" is small enough to get completely right.

use std::process::ExitCode;

use hermes_maint_core::paths::Paths;
use hermes_maint_core::{Exit, Runner, Trigger};

const USAGE: &str = "\
hermes-maint -- scheduled maintenance for a Hermes install

USAGE:
    hermes-maint run [OPTIONS]

OPTIONS:
    --trigger <timer|manual>   what started this run (default: manual)
    --dry-run                  report what a real run would do, write nothing
    -h, --help                 this text
    -V, --version              version

EXIT CODES:
    0  ok
    1  internal error
    2  misuse -- bad arguments, or state written by a newer version
    3  lock busy -- another run holds it (NOT a failure; the unit declares
       SuccessExitStatus=3 so a working lock never reaches `systemctl --failed`)
    4  partial -- a task failed or was skipped
    5  timeout -- a task was killed on its deadline
    6  degraded -- everything ran, a health check reports degraded

NOTE:
    --dry-run still takes the lock, because a dry-run that read state while a
    real run rewrote it would report fiction. Taking the lock may create the
    lock file; that is the only thing a dry-run writes. It never writes state
    and never executes a child.
";

fn main() -> ExitCode {
    let exit = real_main();
    ExitCode::from(u8::try_from(exit.code()).unwrap_or(1))
}

fn real_main() -> Exit {
    let args: Vec<String> = std::env::args().skip(1).collect();

    match parse(&args) {
        Ok(Action::Help) => {
            print!("{USAGE}");
            Exit::Ok
        }
        Ok(Action::Version) => {
            println!("hermes-maint {}", env!("CARGO_PKG_VERSION"));
            Exit::Ok
        }
        Ok(Action::Run { trigger, dry_run }) => run(trigger, dry_run),
        Err(message) => {
            eprintln!("error: {message}\n");
            eprint!("{USAGE}");
            Exit::Misuse
        }
    }
}

fn run(trigger: Trigger, dry_run: bool) -> Exit {
    let paths = match Paths::from_env() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("error: could not resolve HERMES_HOME: {e}");
            return Exit::Misuse;
        }
    };

    match Runner::start(paths, trigger, dry_run) {
        // A contended lock is the lock working. One line, no alarm.
        Err(e) if matches!(e.exit(), Exit::LockBusy) => e.exit(),
        Err(e) => {
            eprintln!("error: {e}");
            e.exit()
        }
        // No tasks are registered, so a run opens and closes with nothing in
        // between. When the first task arrives it goes here, and only here.
        Ok(runner) => runner.finish(),
    }
}

enum Action {
    Run { trigger: Trigger, dry_run: bool },
    Help,
    Version,
}

fn parse(args: &[String]) -> Result<Action, String> {
    let mut iter = args.iter().map(String::as_str);

    let Some(first) = iter.next() else {
        return Ok(Action::Help);
    };

    match first {
        "-h" | "--help" => return Ok(Action::Help),
        "-V" | "--version" => return Ok(Action::Version),
        "run" => {}
        other => return Err(format!("unknown command {other:?}")),
    }

    let mut trigger = Trigger::Manual;
    let mut dry_run = false;

    while let Some(arg) = iter.next() {
        match arg {
            "--dry-run" => dry_run = true,
            "--trigger" => {
                let value = iter
                    .next()
                    .ok_or_else(|| "--trigger needs a value".to_string())?;
                trigger = Trigger::parse(value)?;
            }
            other => {
                if let Some(value) = other.strip_prefix("--trigger=") {
                    trigger = Trigger::parse(value)?;
                } else {
                    return Err(format!("unknown option {other:?}"));
                }
            }
        }
    }

    Ok(Action::Run { trigger, dry_run })
}
