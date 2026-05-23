use std::collections::HashSet;

use genai::chat::Tool;

use crate::actor::ActorRuntime;
use crate::store::MemoryStore;
use crate::telegram::TelegramToolContext;
use crate::tools::browser::BrowserTools;
use crate::tools::filesystem::FileTools;
use crate::tools::image::ImageTools;
use crate::tools::shell::ShellTools;
use crate::tools::web::WebTools;

mod args;
mod catalog;
mod client;
mod dispatch;
mod payload;
mod schema;

pub use client::{ClientToolContext, ClientToolEvent};

#[cfg(test)]
mod tests;

pub type SharedActorRegistry = ActorRuntime;

#[derive(Clone, Debug)]
pub struct ActorToolContext {
    pub runtime: SharedActorRegistry,
    pub actor_id: String,
    pub is_subagent: bool,
}

#[derive(Clone, Debug, Default)]
pub struct ToolRuntime {
    pub telegram: Option<TelegramToolContext>,
    pub client: Option<ClientToolContext>,
    pub actor: Option<ActorToolContext>,
    pub requested_tools: Vec<String>,
}

#[derive(Clone)]
pub struct ToolRegistry<'a> {
    memory: &'a MemoryStore,
    files: FileTools,
    image: ImageTools,
    shell: &'a ShellTools,
    web: WebTools,
    browser: BrowserTools,
    runtime: ToolRuntime,
}

impl<'a> ToolRegistry<'a> {
    pub fn new(
        memory: &'a MemoryStore,
        workspace_dir: impl Into<std::path::PathBuf>,
        cache_dir: impl Into<std::path::PathBuf>,
        shell: &'a ShellTools,
    ) -> Self {
        Self::with_runtime(
            memory,
            workspace_dir,
            cache_dir,
            shell,
            ToolRuntime::default(),
        )
    }

    pub fn with_runtime(
        memory: &'a MemoryStore,
        workspace_dir: impl Into<std::path::PathBuf>,
        cache_dir: impl Into<std::path::PathBuf>,
        shell: &'a ShellTools,
        runtime: ToolRuntime,
    ) -> Self {
        let workspace_dir = workspace_dir.into();
        let cache_dir = cache_dir.into();
        Self {
            memory,
            files: FileTools::new(workspace_dir.clone()),
            image: ImageTools::new(workspace_dir),
            shell,
            web: WebTools::new(cache_dir.clone()),
            browser: BrowserTools::new(cache_dir),
            runtime,
        }
    }
}

impl<'a> ToolRegistry<'a> {
    pub fn tools_for_active(&self, active_tools: &HashSet<String>) -> Vec<Tool> {
        self.tools()
            .into_iter()
            .filter(|tool| {
                self.is_initial_tool(&tool.name) || active_tools.contains(tool.name.as_str())
            })
            .collect()
    }

    pub fn tool_is_available(&self, name: &str) -> bool {
        let name = name.trim();
        self.tools().iter().any(|tool| tool.name == name)
    }

    pub fn tool_is_active(&self, name: &str, active_tools: &HashSet<String>) -> bool {
        self.is_initial_tool(name) || active_tools.contains(name)
    }

    pub fn telegram_typing_context(&self) -> Option<(String, i64)> {
        self.runtime
            .telegram
            .as_ref()
            .filter(|context| !context.dry_run && !context.token.trim().is_empty())
            .map(|context| (context.token.clone(), context.chat_id))
    }

    pub fn requestable_tool_names(&self) -> Vec<String> {
        let mut names = self
            .tools()
            .into_iter()
            .map(|tool| tool.name)
            .filter(|name| !self.is_initial_tool(name) && name != "request_tool")
            .collect::<Vec<_>>();
        names.sort();
        names.dedup();
        names
    }

    fn is_initial_tool(&self, name: &str) -> bool {
        let base = matches!(
            name,
            "request_tool"
                | "bash"
                | "read_file"
                | "write_file"
                | "edit_file"
                | "list_directory"
                | "grep_search"
                | "memory_read"
                | "memory_update"
                | "conversation_search"
                | "note_search"
                | "note_create"
                | "todo_create"
                | "todo_list"
                | "web_search"
        );
        if base {
            return true;
        }
        if (self.runtime.telegram.is_some() || self.runtime.client.is_some())
            && matches!(
                name,
                "telegram_send_message" | "telegram_send_file" | "telegram_react"
            )
        {
            return true;
        }
        if self.runtime.actor.is_some()
            && matches!(
                name,
                "send_message"
                    | "wait_for_response"
                    | "discover_actors"
                    | "discover_recently_finished"
                    | "spawn_actor"
                    | "spawn_chain"
                    | "ping_actor"
                    | "kill_actor"
                    | "update_task_state"
                    | "get_task_state"
                    | "terminate"
                    | "restart_self"
            )
        {
            return true;
        }
        false
    }
}
