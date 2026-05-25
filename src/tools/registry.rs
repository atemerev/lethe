use std::collections::HashSet;

use crate::actor::ActorRuntime;
use crate::interfaces::telegram::TelegramToolContext;
use crate::memory::MemoryStore;
use crate::tools::browser::BrowserTools;
use crate::tools::filesystem::FileTools;
use crate::tools::image::ImageTools;
use crate::tools::shell::ShellTools;
use crate::tools::web::WebTools;

mod actor_specs;
pub(crate) mod args;
mod builtin_specs;
mod catalog;
mod client;
mod dispatch;
mod egress;
mod observer;
mod payload;
mod telegram_specs;

pub use egress::MessageEgress;
pub use observer::{BoxToolFuture, SharedTurnObserver, TurnObserver};

pub use catalog::find_def;
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

#[derive(Clone, Default)]
pub struct ToolRuntime {
    pub telegram: Option<TelegramToolContext>,
    pub client: Option<ClientToolContext>,
    pub actor: Option<ActorToolContext>,
    pub observer: Option<SharedTurnObserver>,
    pub requested_tools: Vec<String>,
}

impl std::fmt::Debug for ToolRuntime {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ToolRuntime")
            .field("telegram", &self.telegram.is_some())
            .field("client", &self.client.is_some())
            .field("actor", &self.actor.is_some())
            .field("observer", &self.observer.is_some())
            .field("requested_tools", &self.requested_tools)
            .finish()
    }
}

#[derive(Clone)]
pub struct ToolRegistry<'a> {
    pub(crate) memory: &'a MemoryStore,
    pub(crate) files: FileTools,
    pub(crate) image: ImageTools,
    pub(crate) shell: &'a ShellTools,
    pub(crate) web: WebTools,
    pub(crate) browser: BrowserTools,
    pub(crate) runtime: ToolRuntime,
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

    pub fn tools_for_active(&self, active_tools: &HashSet<String>) -> Vec<genai::chat::Tool> {
        self.tools()
            .into_iter()
            .filter(|tool| {
                self.is_initial_tool(&tool.name) || active_tools.contains(tool.name.as_str())
            })
            .collect()
    }

    pub fn tool_is_available(&self, name: &str) -> bool {
        let name = name.trim();
        find_def(name).is_some_and(|def| self.def_is_visible(def))
    }

    pub fn tool_is_active(&self, name: &str, active_tools: &HashSet<String>) -> bool {
        self.is_initial_tool(name) || active_tools.contains(name)
    }

    pub fn turn_observer(&self) -> Option<&SharedTurnObserver> {
        self.runtime.observer.as_ref()
    }

    pub fn requestable_tool_names(&self) -> Vec<String> {
        let mut names = catalog::all_defs()
            .filter(|def| self.def_is_visible(def) && !self.def_is_initial(def))
            .filter(|def| def.name != "request_tool")
            .map(|def| def.name.to_string())
            .collect::<Vec<_>>();
        names.sort();
        names.dedup();
        names
    }

    /// One-line per tool directory for the system prompt: `name — description`.
    /// Lists every tool the agent could `request_tool` for in the current
    /// context. Cheaper than loading full JSON schemas for tools the model may
    /// never use.
    pub fn requestable_tools_directory(&self) -> String {
        requestable_tools_directory_for(&self.runtime)
    }
}

/// Shape inputs for [`requestable_tools_directory_for_shape`]. Lets callers
/// build the directory without constructing a `ToolRegistry` or `ToolRuntime`
/// (the registry needs a `MemoryStore` and `ShellTools` it doesn't use just to
/// list tool names; the runtime needs an `ActorRuntime` that isn't always
/// available at prompt-build time).
#[derive(Clone, Copy, Debug, Default)]
pub struct ToolContextShape {
    pub has_actor: bool,
    pub is_subagent: bool,
    pub has_transport: bool,
}

pub fn requestable_tools_directory_for(runtime: &ToolRuntime) -> String {
    requestable_tools_directory_for_shape(ToolContextShape {
        has_actor: runtime.actor.is_some(),
        is_subagent: runtime
            .actor
            .as_ref()
            .is_some_and(|context| context.is_subagent),
        has_transport: runtime.telegram.is_some() || runtime.client.is_some(),
    })
}

pub fn requestable_tools_directory_for_shape(shape: ToolContextShape) -> String {
    use crate::tools::spec::ToolCategory;
    let ToolContextShape {
        has_actor,
        is_subagent,
        has_transport,
    } = shape;

    let visible = |def: &crate::tools::spec::ToolDef| match def.category {
        ToolCategory::Initial | ToolCategory::Requestable | ToolCategory::CortexOnly => true,
        ToolCategory::Actor => has_actor,
        ToolCategory::ActorSubagent => is_subagent,
        ToolCategory::Transport => has_transport,
    };
    let initial = |def: &crate::tools::spec::ToolDef| match def.category {
        ToolCategory::Initial => true,
        ToolCategory::Requestable => false,
        ToolCategory::CortexOnly => !is_subagent,
        ToolCategory::Actor => has_actor,
        ToolCategory::ActorSubagent => is_subagent,
        ToolCategory::Transport => has_transport,
    };

    let mut lines = catalog::all_defs()
        .filter(|def| visible(def) && !initial(def))
        .filter(|def| def.name != "request_tool")
        .map(|def| format!("- {} — {}", def.name, def.description))
        .collect::<Vec<_>>();
    lines.sort();
    lines.dedup();
    lines.join("\n")
}

impl<'a> ToolRegistry<'a> {
    pub(super) fn is_initial_tool(&self, name: &str) -> bool {
        find_def(name).is_some_and(|def| self.def_is_initial(def))
    }
}
