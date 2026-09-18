//! A deliberately inocuous child, used only by the supervisor's tests.
//!
//! Real commands from the system are not good fixtures: `sleep`, `yes` and
//! `cat` vary between distributions, some are shell builtins, and none of them
//! can be asked to ignore `SIGTERM` or to report its own process group. This
//! binary does exactly what a test needs and nothing else.
//!
//! It never reads a file, never opens a socket and never runs another program
//! except itself.

use std::io::Write;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mode = args.first().map(String::as_str).unwrap_or("help");

    match mode {
        // --- exits ---------------------------------------------------------
        "exit" => {
            let code: i32 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
            std::process::exit(code);
        }

        // --- output --------------------------------------------------------
        "echo-out" => print!("{}", args[1..].join(" ")),
        "echo-err" => eprint!("{}", args[1..].join(" ")),

        // Deterministic filler: byte i of the stream is `b'a' + (i % 26)`, so a
        // test can assert exactly which slice survived truncation.
        "flood-out" => flood(bytes(&args), true, false),
        "flood-err" => flood(bytes(&args), false, true),
        "flood-both" => flood(bytes(&args), true, true),

        // --- the parts that have to still be alive at the deadline ---------
        "sleep-forever" => {
            announce_ready();
            park();
        }
        "ignore-term" => {
            // SAFETY: installing SIG_IGN for SIGTERM is async-signal-safe and
            // affects only this process.
            unsafe {
                libc::signal(libc::SIGTERM, libc::SIG_IGN);
            }
            announce_ready();
            park();
        }

        // --- the process group demonstration --------------------------------
        //
        // Spawns a grandchild which records its own pid and process group, so
        // the test can prove the grandchild shared the group and then died --
        // rather than inferring it from the parent's death.
        "spawn-grandchild" => {
            let record = args[1].clone();
            let me = std::env::current_exe().expect("current exe");
            // Deliberately never waited on: the point of the fixture is that
            // the grandchild outlives this process's attention and is ended by
            // the supervisor's signal to the whole process group. This process
            // is killed too, so nothing is leaked.
            #[allow(clippy::zombie_processes)]
            let _grandchild = std::process::Command::new(me)
                .args(["record-and-sleep", &record])
                .spawn()
                .expect("spawn grandchild");
            // Wait until the grandchild has really recorded itself: a test that
            // depends on ordering must synchronise, not hope.
            let path = std::path::Path::new(&record);
            while !path.exists() {
                std::thread::sleep(std::time::Duration::from_millis(5));
            }
            announce_ready();
            park();
        }
        "record-and-sleep" => {
            // SAFETY: `getpgrp` takes no arguments and cannot fail.
            let pgid = unsafe { libc::getpgrp() };
            std::fs::write(&args[1], format!("{} {}\n", std::process::id(), pgid)).expect("record");
            park();
        }

        // --- introspection ---------------------------------------------------
        "print-argv" => {
            // One per line, so arguments containing spaces stay distinguishable.
            for a in &args[1..] {
                println!("{a}");
            }
        }
        "print-env" => {
            let mut vars: Vec<(String, String)> = std::env::vars().collect();
            vars.sort();
            for (k, v) in vars {
                println!("{k}={v}");
            }
        }
        "print-cwd" => {
            println!("{}", std::env::current_dir().expect("cwd").display());
        }

        other => {
            eprintln!("hm-fixture: unknown mode {other:?}");
            std::process::exit(64);
        }
    }
}

fn bytes(args: &[String]) -> usize {
    args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0)
}

fn flood(total: usize, out: bool, err: bool) {
    const CHUNK: usize = 8192;
    let mut stdout = std::io::stdout().lock();
    let mut stderr = std::io::stderr().lock();
    let mut written = 0usize;
    while written < total {
        let n = CHUNK.min(total - written);
        let chunk: Vec<u8> = (written..written + n)
            .map(|i| b'a' + (i % 26) as u8)
            .collect();
        if out {
            stdout.write_all(&chunk).expect("stdout");
        }
        if err {
            stderr.write_all(&chunk).expect("stderr");
        }
        written += n;
    }
    stdout.flush().expect("flush stdout");
    stderr.flush().expect("flush stderr");
}

/// Tell the supervisor's captured stdout that the interesting state has been
/// reached. The tests do not read it, but it makes a stuck fixture obvious.
fn announce_ready() {
    println!("ready");
    std::io::stdout().flush().expect("flush");
}

/// Block forever without spinning. `pause` returns on any signal, so the loop
/// is what makes "ignore SIGTERM" actually mean it.
fn park() -> ! {
    loop {
        // SAFETY: `pause` takes no arguments and only suspends this thread.
        unsafe {
            libc::pause();
        }
    }
}
