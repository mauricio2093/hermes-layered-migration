//! Free space on the filesystem holding `HERMES_HOME`.
//!
//! The most boring possible first task, which is exactly why it is first: it
//! is read-only, it knows nothing about Hermes' internals, it needs no
//! database, no privileges and no network, and it has essentially no
//! destructive way to fail.
//!
//! It asks the kernel directly with `statvfs(3)` rather than running `df`.
//! That keeps the first task free of subprocesses, so the child supervisor --
//! process groups, `SIGTERM` escalation, deadlines -- lands in its own slice
//! instead of being debugged at the same time as this arithmetic.

use std::ffi::CString;
use std::path::Path;

use crate::task::{Observation, Task, TaskContext, TaskError, TaskReport};

/// Warn below this many free bytes, whatever the disk's size. On a small
/// filesystem a percentage is useless; this is the floor that matters.
pub const MIN_FREE_BYTES: u64 = 2 * 1024 * 1024 * 1024;

/// Warn below this fraction free, whatever the disk's size. On a large
/// filesystem the absolute floor arrives far too late; 5% free is already a
/// disk worth looking at.
pub const MIN_FREE_FRACTION: f64 = 0.05;

/// Inodes are their own way to run out of disk, and the confusing one: plenty
/// of bytes free and writes still fail. It is the same syscall, so it is free
/// to check.
pub const MIN_FREE_INODE_FRACTION: f64 = 0.05;

/// What the kernel reported. Split out from the syscall so the thresholds can
/// be tested without needing a nearly-full disk.
///
/// Three sizes rather than two, because the obvious two give a percentage that
/// disagrees with `df`. A filesystem like ext4 reserves a slice of itself for
/// root, and that slice is counted in the total but is not available to us:
/// dividing available-to-us by the whole size reports a disk as fuller than
/// `df` says it is, and a report that disagrees with the tool everyone
/// actually runs will be read as a bug.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Usage {
    /// The filesystem's size, reserved blocks included. This is the number
    /// `df` prints under `Size`.
    pub size_bytes: u64,
    /// Used plus available: the part of the filesystem this process can
    /// actually account for. `df` computes its `Use%` against this.
    pub usable_bytes: u64,
    /// Bytes available to an unprivileged process -- `f_bavail`, not
    /// `f_bfree`. The difference is the reserved blocks, which this tool will
    /// never get to use and so must not count.
    pub free_bytes: u64,
    /// Total inodes, or 0 on filesystems that do not report them.
    pub total_inodes: u64,
    pub free_inodes: u64,
}

impl Usage {
    /// Free as a fraction of what is usable, which is what `df`'s `Use%`
    /// complements.
    #[must_use]
    pub fn free_fraction(&self) -> f64 {
        if self.usable_bytes == 0 {
            return 1.0;
        }
        self.free_bytes as f64 / self.usable_bytes as f64
    }

    /// `None` when the filesystem does not report inodes, which is not a
    /// problem -- it is an absence of information, and saying so beats
    /// inventing a number.
    #[must_use]
    pub fn free_inode_fraction(&self) -> Option<f64> {
        if self.total_inodes == 0 {
            return None;
        }
        Some(self.free_inodes as f64 / self.total_inodes as f64)
    }
}

#[derive(Debug, Default)]
pub struct DiskSpace {
    _private: (),
}

impl Task for DiskSpace {
    fn id(&self) -> &'static str {
        "disk-space"
    }

    fn describe(&self) -> &'static str {
        "free space and inodes on the filesystem holding HERMES_HOME"
    }

    fn run(&self, ctx: &TaskContext<'_>) -> Result<TaskReport, TaskError> {
        let usage = statvfs(ctx.paths.hermes_home())
            .map_err(|e| TaskError(format!("could not read filesystem usage: {e}")))?;
        Ok(judge(&usage).into())
    }
}

/// The whole decision, as a pure function. No syscall, no paths, no clock --
/// so the thresholds are testable without a nearly-full disk, and the
/// reasoning is visible in one place.
#[must_use]
pub fn judge(usage: &Usage) -> Observation {
    let mut reasons: Vec<String> = Vec::new();

    if usage.free_bytes < MIN_FREE_BYTES {
        reasons.push(format!("below the {} floor", human_bytes(MIN_FREE_BYTES)));
    }
    let fraction = usage.free_fraction();
    if fraction < MIN_FREE_FRACTION {
        reasons.push(format!("below {:.0}% free", MIN_FREE_FRACTION * 100.0));
    }
    if let Some(inodes) = usage.free_inode_fraction() {
        if inodes < MIN_FREE_INODE_FRACTION {
            reasons.push(format!("inodes at {:.1}% free", inodes * 100.0));
        }
    }

    let summary = format!(
        "{} free of {} usable ({:.1}%){}",
        human_bytes(usage.free_bytes),
        human_bytes(usage.usable_bytes),
        fraction * 100.0,
        match usage.free_inode_fraction() {
            Some(i) => format!(", inodes {:.1}% free", i * 100.0),
            None => String::new(),
        }
    );

    if reasons.is_empty() {
        Observation::Ok(summary)
    } else {
        Observation::Degraded(format!("{summary}; {}", reasons.join(", ")))
    }
}

/// `statvfs(3)` on the filesystem containing *path*.
pub fn statvfs(path: &Path) -> std::io::Result<Usage> {
    let c_path = CString::new(path.as_os_str().as_encoded_bytes()).map_err(|_| {
        std::io::Error::new(std::io::ErrorKind::InvalidInput, "path has a NUL byte")
    })?;

    // SAFETY: `statvfs` fills the struct it is given; `c_path` is a valid,
    // NUL-terminated string that outlives the call. The struct is only read
    // after a success return.
    let stat = unsafe {
        let mut stat = std::mem::MaybeUninit::<libc::statvfs>::uninit();
        if libc::statvfs(c_path.as_ptr(), stat.as_mut_ptr()) != 0 {
            return Err(std::io::Error::last_os_error());
        }
        stat.assume_init()
    };

    // `f_frsize` is the fragment size, which is what the block counts are in.
    // `f_bsize` is the preferred I/O size and is the classic way to get this
    // wrong on filesystems where the two differ.
    let block = if stat.f_frsize == 0 {
        stat.f_bsize
    } else {
        stat.f_frsize
    } as u64;

    let blocks = stat.f_blocks as u64;
    let bfree = stat.f_bfree as u64;
    let bavail = stat.f_bavail as u64;
    // `f_bfree` counts free blocks including the reserved ones; `f_bavail`
    // excludes them. Used is therefore total minus free, and what we can
    // account for is used plus available -- the same arithmetic `df` does.
    let used = blocks.saturating_sub(bfree);

    Ok(Usage {
        size_bytes: blocks.saturating_mul(block),
        usable_bytes: used.saturating_add(bavail).saturating_mul(block),
        free_bytes: bavail.saturating_mul(block),
        total_inodes: stat.f_files as u64,
        free_inodes: stat.f_favail as u64,
    })
}

/// Binary units, because that is what `statvfs` counts in.
#[must_use]
pub fn human_bytes(bytes: u64) -> String {
    const UNITS: [&str; 6] = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    let mut value = bytes as f64;
    let mut unit = 0;
    while value >= 1024.0 && unit < UNITS.len() - 1 {
        value /= 1024.0;
        unit += 1;
    }
    if unit == 0 {
        format!("{bytes} B")
    } else {
        format!("{value:.1} {}", UNITS[unit])
    }
}
