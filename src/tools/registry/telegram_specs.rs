use serde_json::Value;

use crate::tools::registry::ToolRegistry;
use crate::tools::registry::args::{bool_arg, i64_arg, string_arg, string_arg_default};
use crate::tools::registry::egress::NO_EGRESS_ERROR;
use crate::tools::spec::{ToolCategory, ToolDef, ToolExecutor, p_bool, p_int, p_str, p_str_req};

fn exec_telegram_send_message(registry: &ToolRegistry<'_>, args: &Value) -> String {
    match registry.message_egress() {
        Some(egress) => egress.send_message(
            &string_arg(args, "text"),
            &string_arg_default(args, "parse_mode", ""),
        ),
        None => NO_EGRESS_ERROR.to_string(),
    }
}

fn exec_telegram_send_file(registry: &ToolRegistry<'_>, args: &Value) -> String {
    match registry.message_egress() {
        Some(egress) => egress.send_file(
            &string_arg(args, "file_path_or_url"),
            &string_arg_default(args, "caption", ""),
            bool_arg(args, "as_document", false),
        ),
        None => NO_EGRESS_ERROR.to_string(),
    }
}

fn exec_telegram_react(registry: &ToolRegistry<'_>, args: &Value) -> String {
    match registry.message_egress() {
        Some(egress) => egress.react(
            &string_arg_default(args, "emoji", "👍"),
            i64_arg(args, "message_id", 0),
        ),
        None => NO_EGRESS_ERROR.to_string(),
    }
}

pub const TOOL_DEFS: &[ToolDef] = &[
    ToolDef {
        name: "telegram_send_message",
        description: "Send an extra Telegram message during a long task.",
        params: &[
            p_str_req("text", "Message text."),
            p_str("parse_mode", "markdown, html, or empty."),
        ],
        category: ToolCategory::Transport,
        execute: ToolExecutor::Sync(exec_telegram_send_message),
    },
    ToolDef {
        name: "telegram_send_file",
        description: "Send a file, image, video, audio, or URL to the chat.",
        params: &[
            p_str_req("file_path_or_url", "Local path or HTTP(S) URL."),
            p_str("caption", "Caption."),
            p_bool("as_document", "Force document upload."),
        ],
        category: ToolCategory::Transport,
        execute: ToolExecutor::Sync(exec_telegram_send_file),
    },
    ToolDef {
        name: "telegram_react",
        description: "React to the user's last Telegram message.",
        params: &[
            p_str("emoji", "Emoji."),
            p_int("message_id", "Message id (0 = last inbound)."),
        ],
        category: ToolCategory::Transport,
        execute: ToolExecutor::Sync(exec_telegram_react),
    },
];
