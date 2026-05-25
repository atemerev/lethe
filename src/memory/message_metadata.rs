use serde_json::{Map, Value, json};

pub const VISIBILITY_KEY: &str = "lethe_visibility";
pub const MESSAGE_KIND_KEY: &str = "lethe_message_kind";
pub const SOURCE_KEY: &str = "lethe_source";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MessageVisibility {
    UserVisible,
    Internal,
}

impl MessageVisibility {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::UserVisible => "user_visible",
            Self::Internal => "internal",
        }
    }

    fn parse(value: &str) -> Option<Self> {
        match value.trim() {
            "user_visible" | "visible" | "external" => Some(Self::UserVisible),
            "internal" | "system" | "background" => Some(Self::Internal),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MessageKind {
    Chat,
    Heartbeat,
    ActorUpdate,
    TelegramMedia,
    TelegramReaction,
}

impl MessageKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Chat => "chat",
            Self::Heartbeat => "heartbeat",
            Self::ActorUpdate => "actor_update",
            Self::TelegramMedia => "telegram_media",
            Self::TelegramReaction => "telegram_reaction",
        }
    }

    fn parse(value: &str) -> Option<Self> {
        match value.trim() {
            "chat" | "telegram_text" => Some(Self::Chat),
            "heartbeat" | "background_heartbeat" => Some(Self::Heartbeat),
            "actor_update" => Some(Self::ActorUpdate),
            "telegram_media" | "telegram_audio" | "telegram_photo" | "telegram_document"
            | "telegram_sticker" => Some(Self::TelegramMedia),
            "telegram_reaction" => Some(Self::TelegramReaction),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct MessageMetadata {
    pub visibility: MessageVisibility,
    pub kind: Option<MessageKind>,
    pub has_tool_calls: bool,
}

impl MessageMetadata {
    pub fn from_value(value: Option<&Value>) -> Self {
        let Some(Value::Object(map)) = value else {
            return Self::default();
        };
        Self::from_map(map)
    }

    pub fn from_map(map: &Map<String, Value>) -> Self {
        let kind = metadata_string(map, MESSAGE_KIND_KEY)
            .and_then(|value| MessageKind::parse(&value))
            .or_else(|| metadata_string(map, "source").and_then(|value| MessageKind::parse(&value)))
            .or_else(|| metadata_string(map, "kind").and_then(|value| MessageKind::parse(&value)));

        let visibility = metadata_string(map, VISIBILITY_KEY)
            .and_then(|value| MessageVisibility::parse(&value))
            .unwrap_or_else(|| legacy_visibility(map, kind));

        let has_tool_calls = map
            .get("tool_calls")
            .and_then(Value::as_array)
            .is_some_and(|calls| !calls.is_empty());

        Self {
            visibility,
            kind,
            has_tool_calls,
        }
    }

    pub fn is_internal(self) -> bool {
        self.visibility == MessageVisibility::Internal
    }

    pub fn has_tool_calls(self) -> bool {
        self.has_tool_calls
    }
}

impl Default for MessageMetadata {
    fn default() -> Self {
        Self {
            visibility: MessageVisibility::UserVisible,
            kind: None,
            has_tool_calls: false,
        }
    }
}

pub fn metadata_value(
    visibility: MessageVisibility,
    kind: MessageKind,
    source: &'static str,
) -> Value {
    let mut map = Map::new();
    annotate_map(&mut map, visibility, kind, source);
    Value::Object(map)
}

pub fn annotate_map(
    map: &mut Map<String, Value>,
    visibility: MessageVisibility,
    kind: MessageKind,
    source: &'static str,
) {
    map.insert(VISIBILITY_KEY.to_string(), json!(visibility.as_str()));
    map.insert(MESSAGE_KIND_KEY.to_string(), json!(kind.as_str()));
    map.insert(SOURCE_KEY.to_string(), json!(source));
}

pub fn annotate_value(
    value: Value,
    visibility: MessageVisibility,
    kind: MessageKind,
    source: &'static str,
) -> Value {
    match value {
        Value::Object(mut map) => {
            annotate_map(&mut map, visibility, kind, source);
            Value::Object(map)
        }
        value => {
            let mut map = Map::new();
            map.insert("metadata".to_string(), value);
            annotate_map(&mut map, visibility, kind, source);
            Value::Object(map)
        }
    }
}

fn legacy_visibility(map: &Map<String, Value>, kind: Option<MessageKind>) -> MessageVisibility {
    if kind == Some(MessageKind::Heartbeat) {
        return MessageVisibility::Internal;
    }

    if metadata_string(map, "source")
        .as_deref()
        .is_some_and(|source| matches!(source, "heartbeat" | "background_heartbeat" | "system"))
    {
        return MessageVisibility::Internal;
    }

    MessageVisibility::UserVisible
}

fn metadata_string(map: &Map<String, Value>, key: &str) -> Option<String> {
    map.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn typed_metadata_drives_visibility() {
        let value = metadata_value(
            MessageVisibility::Internal,
            MessageKind::Heartbeat,
            "heartbeat",
        );

        let metadata = MessageMetadata::from_value(Some(&value));

        assert!(metadata.is_internal());
        assert_eq!(metadata.kind, Some(MessageKind::Heartbeat));
    }

    #[test]
    fn legacy_heartbeat_metadata_is_internal() {
        let value = json!({"source": "heartbeat"});

        assert!(MessageMetadata::from_value(Some(&value)).is_internal());
    }
}
