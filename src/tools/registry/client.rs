use std::fmt;
use std::sync::Arc;
use std::sync::atomic::{AtomicI64, Ordering};

use serde_json::{Value, json};

use crate::interfaces::telegram::{TelegramFilePlan, telegram_parse_mode};

use super::payload::tool_error_payload;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ClientToolEvent {
    pub event: String,
    pub data: Value,
}

#[derive(Clone)]
pub struct ClientToolContext {
    pub chat_id: i64,
    pub last_message_id: Option<i64>,
    emit: Arc<dyn Fn(ClientToolEvent) -> bool + Send + Sync>,
    message_counter: Arc<AtomicI64>,
}

impl fmt::Debug for ClientToolContext {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ClientToolContext")
            .field("chat_id", &self.chat_id)
            .field("last_message_id", &self.last_message_id)
            .finish_non_exhaustive()
    }
}

impl ClientToolContext {
    pub fn new(
        chat_id: i64,
        last_message_id: Option<i64>,
        emit: impl Fn(ClientToolEvent) -> bool + Send + Sync + 'static,
    ) -> Self {
        Self {
            chat_id,
            last_message_id,
            emit: Arc::new(emit),
            message_counter: Arc::new(AtomicI64::new(0)),
        }
    }

    pub fn send_message(&self, text: &str, parse_mode: &str) -> String {
        let text = text.trim();
        if text.is_empty() {
            return tool_error_payload("Client message text is required.");
        }
        let message_id = self.next_message_id();
        let parse_mode = telegram_parse_mode(parse_mode);
        if !(self.emit)(ClientToolEvent {
            event: "text".to_string(),
            data: json!({
                "content": text,
                "parse_mode": parse_mode,
                "message_id": message_id,
                "intermediate": true,
            }),
        }) {
            return tool_error_payload("Client event receiver is unavailable.");
        }
        serde_json::to_string_pretty(&json!({
            "success": true,
            "message_id": message_id,
            "chat_id": self.chat_id,
            "parse_mode": parse_mode,
        }))
        .unwrap()
    }

    pub fn send_file(&self, file_path_or_url: &str, caption: &str, as_document: bool) -> String {
        let plan = match TelegramFilePlan::from_source(file_path_or_url, as_document) {
            Ok(plan) => plan,
            Err(error) => return tool_error_payload(&error),
        };
        let message_id = self.next_message_id();
        let path = plan
            .path
            .as_ref()
            .map(|path| path.display().to_string())
            .unwrap_or_else(|| plan.original.clone());
        if !(self.emit)(ClientToolEvent {
            event: "file".to_string(),
            data: json!({
                "type": plan.send_type.as_str(),
                "path": path,
                "caption": caption,
                "message_id": message_id,
            }),
        }) {
            return tool_error_payload("Client event receiver is unavailable.");
        }
        serde_json::to_string_pretty(&json!({
            "success": true,
            "type": plan.send_type.as_str(),
            "filename": plan.filename,
            "chat_id": self.chat_id,
            "message_id": message_id,
        }))
        .unwrap()
    }

    pub fn react(&self, emoji: &str, message_id: i64) -> String {
        let target_message_id = if message_id > 0 {
            Some(message_id)
        } else {
            self.last_message_id
        };
        let Some(target_message_id) = target_message_id else {
            return tool_error_payload("Client context has no message to react to.");
        };
        let emoji = emoji.trim();
        if emoji.is_empty() {
            return tool_error_payload("Reaction emoji is required.");
        }
        if !(self.emit)(ClientToolEvent {
            event: "reaction".to_string(),
            data: json!({
                "emoji": emoji,
                "message_id": target_message_id,
            }),
        }) {
            return tool_error_payload("Client event receiver is unavailable.");
        }
        serde_json::to_string_pretty(&json!({
            "success": true,
            "emoji": emoji,
            "message_id": target_message_id,
        }))
        .unwrap()
    }

    fn next_message_id(&self) -> i64 {
        self.message_counter.fetch_add(1, Ordering::SeqCst) + 1
    }
}
