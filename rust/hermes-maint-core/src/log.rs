//! Logging, such as it is.
//!
//! Everything goes to stderr, which systemd captures into the journal with the
//! unit's identifier already attached. There is no log file to rotate, no
//! logging framework and no configuration -- adding any of those would be
//! solving a problem this binary does not have.
//!
//! Child output is never interpolated into these lines; when tasks exist it
//! will be captured as bounded, delimited data.

use std::io::Write;

fn emit(level: &str, args: std::fmt::Arguments<'_>) {
    // A failed write to stderr must not take the run down: if the journal is
    // gone, the work is still worth doing.
    let mut err = std::io::stderr().lock();
    let _ = writeln!(err, "{level}: {args}");
}

pub fn info(args: std::fmt::Arguments<'_>) {
    emit("info", args);
}

pub fn warn(args: std::fmt::Arguments<'_>) {
    emit("warn", args);
}

pub fn error(args: std::fmt::Arguments<'_>) {
    emit("error", args);
}

#[macro_export]
macro_rules! info {
    ($($arg:tt)*) => { $crate::log::info(format_args!($($arg)*)) };
}

#[macro_export]
macro_rules! warn {
    ($($arg:tt)*) => { $crate::log::warn(format_args!($($arg)*)) };
}

#[macro_export]
macro_rules! error {
    ($($arg:tt)*) => { $crate::log::error(format_args!($($arg)*)) };
}
