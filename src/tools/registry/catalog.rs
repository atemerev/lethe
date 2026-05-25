use genai::chat::Tool;

use crate::tools::spec::{ToolCategory, ToolDef};
use crate::tools::{browser, filesystem, image, shell, web};

use super::ToolRegistry;
use super::{actor_specs, builtin_specs, telegram_specs};

/// All tool descriptors known to the runtime, in declaration order.
pub fn all_defs() -> impl Iterator<Item = &'static ToolDef> {
    filesystem::TOOL_DEFS
        .iter()
        .chain(image::TOOL_DEFS.iter())
        .chain(shell::TOOL_DEFS.iter())
        .chain(web::TOOL_DEFS.iter())
        .chain(browser::TOOL_DEFS.iter())
        .chain(builtin_specs::TOOL_DEFS.iter())
        .chain(actor_specs::TOOL_DEFS.iter())
        .chain(telegram_specs::TOOL_DEFS.iter())
}

pub fn find_def(name: &str) -> Option<&'static ToolDef> {
    let name = name.trim();
    all_defs().find(|def| def.name == name)
}

impl<'a> ToolRegistry<'a> {
    pub fn tools(&self) -> Vec<Tool> {
        all_defs()
            .filter(|def| self.def_is_visible(def))
            .map(ToolDef::to_genai_tool)
            .collect()
    }

    /// A def is visible (offered to the model in any form) when its category is
    /// compatible with the currently attached runtime contexts. CortexOnly is
    /// requestable from anywhere — it just isn't loaded initially in subagents.
    pub(super) fn def_is_visible(&self, def: &ToolDef) -> bool {
        match def.category {
            ToolCategory::Initial | ToolCategory::Requestable | ToolCategory::CortexOnly => true,
            ToolCategory::Actor => self.runtime.actor.is_some(),
            ToolCategory::ActorSubagent => self
                .runtime
                .actor
                .as_ref()
                .is_some_and(|context| context.is_subagent),
            ToolCategory::Transport => {
                self.runtime.telegram.is_some() || self.runtime.client.is_some()
            }
        }
    }

    /// A def is "initial" (loaded without `request_tool`) when both its
    /// category is initial-like AND any required runtime context is present.
    pub(super) fn def_is_initial(&self, def: &ToolDef) -> bool {
        match def.category {
            ToolCategory::Initial => true,
            ToolCategory::Requestable => false,
            ToolCategory::CortexOnly => !self.is_subagent_context(),
            ToolCategory::Actor => self.runtime.actor.is_some(),
            ToolCategory::ActorSubagent => self
                .runtime
                .actor
                .as_ref()
                .is_some_and(|context| context.is_subagent),
            ToolCategory::Transport => {
                self.runtime.telegram.is_some() || self.runtime.client.is_some()
            }
        }
    }

    fn is_subagent_context(&self) -> bool {
        self.runtime
            .actor
            .as_ref()
            .is_some_and(|context| context.is_subagent)
    }
}
