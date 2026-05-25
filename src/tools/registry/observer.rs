use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

pub type BoxToolFuture<'a> = Pin<Box<dyn Future<Output = String> + Send + 'a>>;

/// Transport-side hook called around each tool execution. Lets a transport
/// (e.g. Telegram) keep its "typing" indicator alive for the duration of a
/// long tool call without the agent loop having to know anything about it.
pub trait TurnObserver: Send + Sync {
    fn wrap_tool_call<'a>(&'a self, name: &'a str, inner: BoxToolFuture<'a>) -> BoxToolFuture<'a>;
}

/// Convenience alias used inside `ToolRuntime`.
pub type SharedTurnObserver = Arc<dyn TurnObserver>;
