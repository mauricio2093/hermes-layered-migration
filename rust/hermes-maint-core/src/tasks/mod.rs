//! The built-in tasks. See [`crate::task::registry`] for the order they run in.

pub mod backup_freshness;
pub mod disk_space;
pub mod external;
pub mod gateway_health;
