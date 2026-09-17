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

    #[allow(dead_code)]
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
