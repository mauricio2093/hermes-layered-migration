//! A throwaway HERMES_HOME per test.
//!
//! Hand-rolled rather than pulling in a temp-file crate: twenty lines is
//! cheaper to audit than a dependency, and these tests must never be able to
//! reach the real `~/.hermes`.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};

use hermes_maint_core::paths::Paths;

static COUNTER: AtomicU32 = AtomicU32::new(0);

pub struct TempHome {
    root: PathBuf,
}

// Each test binary compiles this module separately, so not every helper is
// used by every one of them.
#[allow(dead_code)]
impl TempHome {
    pub fn new(label: &str) -> Self {
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!(
            "hermes-maint-test-{}-{label}-{n}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).expect("create temp home");
        Self { root }
    }

    pub fn paths(&self) -> Paths {
        Paths::under(&self.root)
    }

    pub fn root(&self) -> &Path {
        &self.root
    }
}

impl Drop for TempHome {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

/// Mode bits, for asserting that nothing is readable by group or other.
#[allow(dead_code)]
pub fn mode_of(path: &Path) -> u32 {
    use std::os::unix::fs::MetadataExt;
    std::fs::metadata(path).expect("metadata").mode() & 0o777
}

// --- scratch on the build filesystem -----------------------------------------

use std::sync::OnceLock;

/// Like [`TempHome`], but under `CARGO_TARGET_TMPDIR` -- the same filesystem as
/// the build output, so [`fixture_link`] can hard-link into it.
#[allow(dead_code)]
pub struct Scratch {
    root: PathBuf,
}

#[allow(dead_code)]
impl Scratch {
    pub fn new(label: &str) -> Self {
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let root = PathBuf::from(env!("CARGO_TARGET_TMPDIR"))
            .join(format!("hm-{}-{label}-{n}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).expect("scratch");
        Self { root }
    }

    pub fn paths(&self) -> Paths {
        Paths::under(&self.root)
    }

    pub fn root(&self) -> &Path {
        &self.root
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

/// The built fixture, with a mode the pre-flight accepts.
///
/// `umask 002` on this machine leaves a fresh build group-writable, which the
/// pre-flight correctly refuses. Fixed here once rather than by weakening the
/// rule.
#[allow(dead_code)]
pub fn fixture() -> &'static Path {
    static READY: OnceLock<PathBuf> = OnceLock::new();
    READY.get_or_init(|| {
        use std::os::unix::fs::PermissionsExt;
        let p = PathBuf::from(env!("CARGO_BIN_EXE_hm-fixture"));
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).expect("chmod");
        p
    })
}

/// A hard link to the fixture inside *scratch*.
///
/// A link and not a copy: `fs::copy` holds the destination open for writing,
/// and a sibling thread that forks in that window hands the descriptor to its
/// transient child, so `exec` fails with `ETXTBSY`.
#[allow(dead_code)]
pub fn fixture_link(scratch: &Scratch) -> PathBuf {
    let p = scratch.root().join("hm-fixture");
    if !p.exists() {
        std::fs::hard_link(fixture(), &p).expect("link the fixture");
    }
    p
}
