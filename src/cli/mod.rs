//! CLI subcommand handlers. Defines per-command argument types and their
//! implementations, separated from `main.rs` so the binary's root only owns
//! top-level Clap parsing and dispatch.

pub mod backup;
pub mod handlers;
pub mod init;
pub mod telegram_loop;
