use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

pub type BoxToolFuture<'a> = Pin<Box<dyn Future<Output = String> + Send + 'a>>;

/// Transport-side hook called around each tool execution. Lets a transport
/// (e.g. Telegram) keep its "typing" indicator alive for the duration of a
/// long tool call without the agent loop having to know anything about it.
///
/// `on_tool_start` / `on_tool_end` are no-ops by default; the TUI/HTTP API
/// implements them to surface tool execution to clients as SSE events.
pub trait TurnObserver: Send + Sync {
    fn wrap_tool_call<'a>(&'a self, name: &'a str, inner: BoxToolFuture<'a>) -> BoxToolFuture<'a>;

    fn on_tool_start(&self, _name: &str, _call_id: &str, _args_preview: &str) {}

    fn on_tool_end(
        &self,
        _name: &str,
        _call_id: &str,
        _success: bool,
        _output_preview: &str,
        _duration_ms: u128,
    ) {
    }

    /// Streamed assistant token chunk. The default impl does nothing so
    /// non-streaming transports (Telegram) don't pay any cost.
    fn on_assistant_delta(&self, _content: &str) {}
}

/// Convenience alias used inside `ToolRuntime`.
pub type SharedTurnObserver = Arc<dyn TurnObserver>;
