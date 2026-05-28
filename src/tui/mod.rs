//! Terminal UI client for Lethe. Renders the agent's transcript, tool calls,
//! actor tree, and todos in a ratatui frame and streams updates over the
//! HTTP+SSE API.
//!
//! Entry point: `tui::run(connect, token)`. Wired up as the `lethe tui`
//! subcommand. Designed to mirror the pi-mono harness shape (transcript +
//! sidebar + editor + footer) without taking on its Node toolchain.

pub mod app;
pub mod autocomplete;
pub mod client;
pub mod events;
pub mod markdown;
pub mod state;
pub mod view;

pub use app::run;
