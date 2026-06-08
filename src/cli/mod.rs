//! CLI subcommand handlers. Defines per-command argument types and their
//! implementations, separated from `main.rs` so the binary's root only owns
//! top-level Clap parsing and dispatch.

pub mod avatar;
pub mod backup;
pub mod container;
pub mod handlers;
pub mod identity;
pub mod init;
pub mod model;
pub mod prompts;
pub mod service;
pub mod telegram_loop;
pub mod transport;
pub mod transport_supervisor;
pub mod uninstall;
pub mod util;
