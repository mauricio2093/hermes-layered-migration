//! The lock, including the property it exists for: it cannot go stale.

mod common;

use std::ffi::CString;
use std::time::{Duration, Instant};

use common::{mode_of, TempHome};
use hermes_maint_core::lock::{Lock, LockError};

#[test]
fn a_free_lock_is_acquired() {
    let home = TempHome::new("free");
    let paths = home.paths();
    paths.ensure_dir().unwrap();

    let lock = Lock::acquire(paths.lock_file()).expect("should acquire");
    assert_eq!(lock.path(), paths.lock_file());
}

#[test]
fn a_held_lock_is_busy_not_an_error() {
    let home = TempHome::new("busy");
    let paths = home.paths();
    paths.ensure_dir().unwrap();

    let _first = Lock::acquire(paths.lock_file()).expect("first should acquire");
    // flock is per open file description, so a second open in this same
    // process contends exactly as another process would.
    match Lock::acquire(paths.lock_file()) {
        Err(LockError::Busy { .. }) => {}
        other => panic!("expected Busy, got {other:?}"),
    }
}

#[test]
fn dropping_the_lock_releases_it() {
    let home = TempHome::new("drop");
    let paths = home.paths();
    paths.ensure_dir().unwrap();

    {
        let _held = Lock::acquire(paths.lock_file()).expect("acquire");
        assert!(matches!(
            Lock::acquire(paths.lock_file()),
            Err(LockError::Busy { .. })
        ));
    }
    Lock::acquire(paths.lock_file()).expect("released on drop");
}

#[test]
fn the_lock_file_is_not_readable_by_anyone_else() {
    let home = TempHome::new("mode");
    let paths = home.paths();
    paths.ensure_dir().unwrap();

    let _lock = Lock::acquire(paths.lock_file()).expect("acquire");
    assert_eq!(mode_of(&paths.lock_file()) & 0o077, 0, "lock file");
    assert_eq!(mode_of(paths.dir()) & 0o077, 0, "state directory");
}

#[test]
fn diagnostics_do_not_affect_the_lock() {
    let home = TempHome::new("diag");
    let paths = home.paths();
    paths.ensure_dir().unwrap();

    let mut lock = Lock::acquire(paths.lock_file()).expect("acquire");
    lock.write_diagnostics("manual", 1_700_000_000);

    let body = std::fs::read_to_string(paths.lock_file()).unwrap();
    assert!(body.contains("trigger=manual"), "{body}");
    assert!(
        body.contains(&format!("pid={}", std::process::id())),
        "{body}"
    );

    // Still held: the contents are a note for a human, not the lock itself.
    assert!(matches!(
        Lock::acquire(paths.lock_file()),
        Err(LockError::Busy { .. })
    ));
}

/// The headline property. A PID file would be stale here; `flock` is not.
///
/// A child takes the lock and is then `SIGKILL`ed -- no unwinding, no
/// destructor, no cleanup of any kind. The kernel closes the descriptor, and
/// the lock is free.
#[test]
fn a_killed_holder_leaves_no_stale_lock() {
    let home = TempHome::new("kill");
    let paths = home.paths();
    paths.ensure_dir().unwrap();
    let lock_path = paths.lock_file();

    // Everything the child needs is prepared before the fork: after forking in
    // a threaded process, only async-signal-safe work is sound, so the child
    // allocates nothing.
    let c_path = CString::new(lock_path.to_str().unwrap()).unwrap();

    // A pipe, not polling, for the handshake. Polling from the parent would
    // race the child for the very lock the test is about, and the parent would
    // sometimes win.
    let mut fds = [0 as libc::c_int; 2];
    assert_eq!(unsafe { libc::pipe(fds.as_mut_ptr()) }, 0, "pipe failed");
    let (read_fd, write_fd) = (fds[0], fds[1]);

    // SAFETY: the child calls only close/open/flock/write/pause/_exit and
    // never returns into the test harness.
    let pid = unsafe { libc::fork() };
    assert!(pid >= 0, "fork failed");

    if pid == 0 {
        unsafe {
            libc::close(read_fd);
            let fd = libc::open(c_path.as_ptr(), libc::O_RDWR | libc::O_CREAT, 0o600);
            if fd < 0 {
                libc::_exit(11);
            }
            if libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) != 0 {
                libc::_exit(12);
            }
            // Announce that the lock is held, then wait to be killed.
            let byte = b"1";
            if libc::write(write_fd, byte.as_ptr().cast(), 1) != 1 {
                libc::_exit(13);
            }
            loop {
                libc::pause();
            }
        }
    }

    unsafe { libc::close(write_fd) };
    let mut buf = [0u8; 1];
    // SAFETY: `read_fd` is a valid descriptor owned by this process.
    let got = unsafe { libc::read(read_fd, buf.as_mut_ptr().cast(), 1) };
    unsafe { libc::close(read_fd) };
    assert_eq!(got, 1, "the child never reported holding the lock");

    assert!(
        matches!(Lock::acquire(&lock_path), Err(LockError::Busy { .. })),
        "the child holds it, so we must not be able to"
    );

    // The hardest possible death: no destructors, no cleanup, no chance to
    // remove a PID file.
    unsafe {
        libc::kill(pid, libc::SIGKILL);
        let mut status = 0;
        libc::waitpid(pid, &mut status, 0);
    }

    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match Lock::acquire(&lock_path) {
            Ok(_) => break,
            Err(e) => {
                assert!(Instant::now() < deadline, "lock never released: {e}");
                std::thread::sleep(Duration::from_millis(10));
            }
        }
    }
}
