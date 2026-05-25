use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::json;
use thiserror::Error;

use crate::memory::message_metadata::{MessageKind, MessageVisibility, annotate_value};

mod formatting;
pub use formatting::{
    image_mime_type_from_path, is_emoji_only_reply, split_telegram_messages, telegram_parse_mode,
};
use formatting::{
    error_payload, expand_tilde, filename_from_url, image_extension_for_mime,
    is_invalid_reaction_error, safe_file_name,
};

#[derive(Debug, Error)]
pub enum TelegramError {
    #[error("telegram bot token is required")]
    MissingToken,
    #[error("telegram api error: {0}")]
    Api(String),
    #[error(transparent)]
    Http(#[from] reqwest::Error),
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

pub type TelegramResult<T> = Result<T, TelegramError>;

#[derive(Clone)]
pub struct TelegramClient {
    token: String,
    allowed_user_ids: Vec<i64>,
    http: reqwest::Client,
}

impl TelegramClient {
    pub fn new(token: impl Into<String>, allowed_user_ids: Vec<i64>) -> TelegramResult<Self> {
        let token = token.into();
        if token.trim().is_empty() {
            return Err(TelegramError::MissingToken);
        }
        Ok(Self {
            token,
            allowed_user_ids,
            http: reqwest::Client::new(),
        })
    }

    pub fn user_allowed(&self, user_id: i64) -> bool {
        self.allowed_user_ids.is_empty() || self.allowed_user_ids.contains(&user_id)
    }

    pub async fn get_updates(
        &self,
        offset: Option<i64>,
        timeout_seconds: u64,
    ) -> TelegramResult<Vec<TelegramUpdate>> {
        let mut request = self.http.get(self.method_url("getUpdates")).query(&[
            ("timeout", timeout_seconds.to_string()),
            (
                "allowed_updates",
                "[\"message\",\"message_reaction\"]".to_string(),
            ),
        ]);
        if let Some(offset) = offset {
            request = request.query(&[("offset", offset.to_string())]);
        }
        let response = request
            .send()
            .await?
            .error_for_status()?
            .json::<TelegramResponse<Vec<TelegramUpdate>>>()
            .await?;
        response.into_result()
    }

    pub async fn send_message(&self, chat_id: i64, text: &str) -> TelegramResult<i64> {
        let response = self
            .http
            .post(self.method_url("sendMessage"))
            .json(&SendMessageRequest {
                chat_id,
                text,
                disable_web_page_preview: true,
            })
            .send()
            .await?
            .error_for_status()?
            .json::<TelegramResponse<SentTelegramMessage>>()
            .await?;
        response.into_result().map(|message| message.message_id)
    }

    pub async fn send_chat_action(&self, chat_id: i64, action: &str) -> TelegramResult<bool> {
        let response = self
            .http
            .post(self.method_url("sendChatAction"))
            .json(&SendChatActionRequest { chat_id, action })
            .send()
            .await?
            .error_for_status()?
            .json::<TelegramResponse<bool>>()
            .await?;
        response.into_result()
    }

    pub async fn set_message_reaction(
        &self,
        chat_id: i64,
        message_id: i64,
        emoji: &str,
    ) -> TelegramResult<bool> {
        let emoji = emoji.trim();
        if emoji.is_empty() {
            return Ok(false);
        }
        let response = self
            .http
            .post(self.method_url("setMessageReaction"))
            .json(&SetReactionRequest {
                chat_id,
                message_id,
                reaction: vec![ReactionTypeEmoji {
                    reaction_type: "emoji",
                    emoji,
                }],
            })
            .send()
            .await?
            .error_for_status()?
            .json::<TelegramResponse<bool>>()
            .await?;
        match response.into_result() {
            Ok(value) => Ok(value),
            Err(TelegramError::Api(message)) if is_invalid_reaction_error(&message) => Ok(false),
            Err(error) => Err(error),
        }
    }

    pub async fn get_file(&self, file_id: &str) -> TelegramResult<TelegramFileInfo> {
        let response = self
            .http
            .get(self.method_url("getFile"))
            .query(&[("file_id", file_id)])
            .send()
            .await?
            .error_for_status()?
            .json::<TelegramResponse<TelegramFileInfo>>()
            .await?;
        response.into_result()
    }

    pub async fn download_file(&self, file_path: &str) -> TelegramResult<Vec<u8>> {
        let bytes = self
            .http
            .get(format!(
                "https://api.telegram.org/file/bot{}/{}",
                self.token, file_path
            ))
            .send()
            .await?
            .error_for_status()?
            .bytes()
            .await?;
        Ok(bytes.to_vec())
    }

    fn method_url(&self, method: &str) -> String {
        format!("https://api.telegram.org/bot{}/{}", self.token, method)
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct TelegramUpdate {
    pub update_id: i64,
    #[serde(default)]
    pub message: Option<TelegramMessage>,
    #[serde(default)]
    pub message_reaction: Option<TelegramReactionUpdate>,
}

impl TelegramUpdate {
    pub fn incoming_text(&self) -> Option<IncomingTelegramText> {
        let message = self.message.as_ref()?;
        let from = message.from.as_ref()?;
        let text = message.text.as_ref()?.trim();
        if text.is_empty() {
            return None;
        }
        Some(IncomingTelegramText {
            update_id: self.update_id,
            chat_id: message.chat.id,
            user_id: from.id,
            message_id: message.message_id,
            text: text.to_string(),
        })
    }

    pub fn incoming_reaction(&self) -> Option<IncomingTelegramReaction> {
        let reaction = self.message_reaction.as_ref()?;
        let user = reaction.user.as_ref()?;
        let emojis = reaction
            .new_reaction
            .iter()
            .filter_map(|reaction| reaction.emoji.as_deref())
            .filter(|emoji| !emoji.trim().is_empty())
            .map(str::to_string)
            .collect::<Vec<_>>();
        if emojis.is_empty() {
            return None;
        }
        Some(IncomingTelegramReaction {
            update_id: self.update_id,
            chat_id: reaction.chat.id,
            user_id: user.id,
            message_id: reaction.message_id,
            emojis,
        })
    }

    pub fn incoming_audio(&self) -> Option<IncomingTelegramAudio> {
        let message = self.message.as_ref()?;
        let from = message.from.as_ref()?;
        let (media, is_voice) = if let Some(voice) = message.voice.as_ref() {
            (voice, true)
        } else {
            (message.audio.as_ref()?, false)
        };
        let file_name = media.file_name.clone().unwrap_or_else(|| {
            format!(
                "telegram_{}_{}.ogg",
                if is_voice { "voice" } else { "audio" },
                media.file_id
            )
        });
        let mime_type = media.mime_type.clone().or_else(|| {
            if is_voice {
                Some("audio/ogg".to_string())
            } else {
                None
            }
        });
        Some(IncomingTelegramAudio {
            update_id: self.update_id,
            chat_id: message.chat.id,
            user_id: from.id,
            message_id: message.message_id,
            file_id: media.file_id.clone(),
            file_name,
            mime_type,
            duration: media.duration,
            file_size: media.file_size,
            is_voice,
            caption: message.caption.clone().unwrap_or_default(),
        })
    }

    pub fn incoming_photo(&self) -> Option<IncomingTelegramPhoto> {
        let message = self.message.as_ref()?;
        let from = message.from.as_ref()?;
        let photo = message
            .photo
            .as_ref()?
            .iter()
            .max_by_key(|photo| photo.width.saturating_mul(photo.height))?;
        Some(IncomingTelegramPhoto {
            update_id: self.update_id,
            chat_id: message.chat.id,
            user_id: from.id,
            message_id: message.message_id,
            file_id: photo.file_id.clone(),
            file_unique_id: photo.file_unique_id.clone(),
            width: photo.width,
            height: photo.height,
            file_size: photo.file_size,
            caption: message.caption.clone().unwrap_or_default(),
        })
    }

    pub fn incoming_document(&self) -> Option<IncomingTelegramDocument> {
        let message = self.message.as_ref()?;
        let from = message.from.as_ref()?;
        let document = message.document.as_ref()?;
        Some(IncomingTelegramDocument {
            update_id: self.update_id,
            chat_id: message.chat.id,
            user_id: from.id,
            message_id: message.message_id,
            file_id: document.file_id.clone(),
            file_name: safe_file_name(
                document
                    .file_name
                    .as_deref()
                    .filter(|value| !value.trim().is_empty())
                    .unwrap_or("telegram_document"),
            ),
            mime_type: document.mime_type.clone(),
            file_size: document.file_size,
            caption: message.caption.clone().unwrap_or_default(),
        })
    }

    pub fn incoming_sticker(&self) -> Option<IncomingTelegramSticker> {
        let message = self.message.as_ref()?;
        let from = message.from.as_ref()?;
        let sticker = message.sticker.as_ref()?;
        Some(IncomingTelegramSticker {
            update_id: self.update_id,
            chat_id: message.chat.id,
            user_id: from.id,
            message_id: message.message_id,
            file_id: sticker.file_id.clone(),
            file_unique_id: sticker.file_unique_id.clone(),
            emoji: sticker.emoji.clone(),
            set_name: sticker.set_name.clone(),
            is_animated: sticker.is_animated,
            is_video: sticker.is_video,
            width: sticker.width,
            height: sticker.height,
            file_size: sticker.file_size,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct TelegramReactionUpdate {
    pub chat: TelegramChat,
    pub message_id: i64,
    #[serde(default)]
    pub user: Option<TelegramUser>,
    #[serde(default)]
    pub new_reaction: Vec<TelegramReaction>,
    #[serde(default)]
    pub old_reaction: Vec<TelegramReaction>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct TelegramReaction {
    #[serde(default, rename = "type")]
    pub reaction_type: String,
    #[serde(default)]
    pub emoji: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct TelegramMessage {
    pub message_id: i64,
    pub chat: TelegramChat,
    #[serde(default)]
    pub from: Option<TelegramUser>,
    #[serde(default)]
    pub text: Option<String>,
    #[serde(default)]
    pub caption: Option<String>,
    #[serde(default)]
    pub photo: Option<Vec<TelegramPhotoSize>>,
    #[serde(default)]
    pub voice: Option<TelegramAudioMedia>,
    #[serde(default)]
    pub audio: Option<TelegramAudioMedia>,
    #[serde(default)]
    pub document: Option<TelegramDocumentMedia>,
    #[serde(default)]
    pub sticker: Option<TelegramStickerMedia>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct TelegramAudioMedia {
    pub file_id: String,
    #[serde(default)]
    pub file_unique_id: Option<String>,
    #[serde(default)]
    pub duration: Option<i64>,
    #[serde(default)]
    pub mime_type: Option<String>,
    #[serde(default)]
    pub file_size: Option<i64>,
    #[serde(default)]
    pub file_name: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct TelegramPhotoSize {
    pub file_id: String,
    #[serde(default)]
    pub file_unique_id: Option<String>,
    pub width: i64,
    pub height: i64,
    #[serde(default)]
    pub file_size: Option<i64>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct TelegramDocumentMedia {
    pub file_id: String,
    #[serde(default)]
    pub file_unique_id: Option<String>,
    #[serde(default)]
    pub file_name: Option<String>,
    #[serde(default)]
    pub mime_type: Option<String>,
    #[serde(default)]
    pub file_size: Option<i64>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct TelegramStickerMedia {
    pub file_id: String,
    #[serde(default)]
    pub file_unique_id: Option<String>,
    #[serde(default)]
    pub emoji: Option<String>,
    #[serde(default)]
    pub set_name: Option<String>,
    #[serde(default)]
    pub is_animated: bool,
    #[serde(default)]
    pub is_video: bool,
    #[serde(default)]
    pub width: Option<i64>,
    #[serde(default)]
    pub height: Option<i64>,
    #[serde(default)]
    pub file_size: Option<i64>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct TelegramChat {
    pub id: i64,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct TelegramUser {
    pub id: i64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IncomingTelegramText {
    pub update_id: i64,
    pub chat_id: i64,
    pub user_id: i64,
    pub message_id: i64,
    pub text: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IncomingTelegramReaction {
    pub update_id: i64,
    pub chat_id: i64,
    pub user_id: i64,
    pub message_id: i64,
    pub emojis: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IncomingTelegramAudio {
    pub update_id: i64,
    pub chat_id: i64,
    pub user_id: i64,
    pub message_id: i64,
    pub file_id: String,
    pub file_name: String,
    pub mime_type: Option<String>,
    pub duration: Option<i64>,
    pub file_size: Option<i64>,
    pub is_voice: bool,
    pub caption: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IncomingTelegramPhoto {
    pub update_id: i64,
    pub chat_id: i64,
    pub user_id: i64,
    pub message_id: i64,
    pub file_id: String,
    pub file_unique_id: Option<String>,
    pub width: i64,
    pub height: i64,
    pub file_size: Option<i64>,
    pub caption: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IncomingTelegramDocument {
    pub update_id: i64,
    pub chat_id: i64,
    pub user_id: i64,
    pub message_id: i64,
    pub file_id: String,
    pub file_name: String,
    pub mime_type: Option<String>,
    pub file_size: Option<i64>,
    pub caption: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IncomingTelegramSticker {
    pub update_id: i64,
    pub chat_id: i64,
    pub user_id: i64,
    pub message_id: i64,
    pub file_id: String,
    pub file_unique_id: Option<String>,
    pub emoji: Option<String>,
    pub set_name: Option<String>,
    pub is_animated: bool,
    pub is_video: bool,
    pub width: Option<i64>,
    pub height: Option<i64>,
    pub file_size: Option<i64>,
}

impl IncomingTelegramAudio {
    pub fn content_with_transcript(&self, transcript: &str) -> String {
        let label = if self.is_voice {
            "voice message"
        } else {
            "audio message"
        };
        let mut content = format!("[Transcribed {label}: {}]", transcript.trim());
        if !self.caption.trim().is_empty() {
            content.push_str("\nCaption: ");
            content.push_str(self.caption.trim());
        }
        content
    }

    pub fn metadata(&self, provider: &str, model: &str) -> serde_json::Value {
        annotate_value(
            json!({
                "source": "telegram_audio",
                "chat_id": self.chat_id,
                "user_id": self.user_id,
                "message_id": self.message_id,
                "file_id": self.file_id,
                "file_name": self.file_name,
                "mime_type": self.mime_type,
                "duration": self.duration,
                "file_size": self.file_size,
                "is_voice": self.is_voice,
                "is_audio": !self.is_voice,
                "transcription_provider": provider,
                "transcription_model": model,
            }),
            MessageVisibility::UserVisible,
            MessageKind::TelegramMedia,
            "telegram",
        )
    }
}

impl IncomingTelegramPhoto {
    pub fn content_text(&self) -> String {
        if self.caption.trim().is_empty() {
            format!("[Telegram photo: {}x{}]", self.width, self.height)
        } else {
            self.caption.trim().to_string()
        }
    }

    pub fn attachment_name(&self, content_type: &str) -> String {
        let extension = image_extension_for_mime(content_type).unwrap_or("jpg");
        format!("telegram_photo_{}.{}", self.file_id, extension)
    }

    pub fn metadata(&self, content_type: &str) -> serde_json::Value {
        annotate_value(
            json!({
                "source": "telegram_photo",
                "chat_id": self.chat_id,
                "user_id": self.user_id,
                "message_id": self.message_id,
                "file_id": self.file_id,
                "file_unique_id": self.file_unique_id,
                "width": self.width,
                "height": self.height,
                "file_size": self.file_size,
                "mime_type": content_type,
                "has_caption": !self.caption.trim().is_empty(),
            }),
            MessageVisibility::UserVisible,
            MessageKind::TelegramMedia,
            "telegram",
        )
    }
}

impl IncomingTelegramDocument {
    pub fn content_with_path(&self, file_path: &Path) -> String {
        let mut content = format!("[Received file: {}]", file_path.display());
        if !self.caption.trim().is_empty() {
            content.push('\n');
            content.push_str(self.caption.trim());
        }
        content
    }

    pub fn metadata(&self, file_path: &Path) -> serde_json::Value {
        annotate_value(
            json!({
                "source": "telegram_document",
                "chat_id": self.chat_id,
                "user_id": self.user_id,
                "message_id": self.message_id,
                "file_id": self.file_id,
                "file_name": self.file_name,
                "file_path": file_path.display().to_string(),
                "mime_type": self.mime_type,
                "file_size": self.file_size,
            }),
            MessageVisibility::UserVisible,
            MessageKind::TelegramMedia,
            "telegram",
        )
    }
}

impl IncomingTelegramSticker {
    pub fn content(&self) -> String {
        let mut parts = Vec::new();
        if let Some(emoji) = self
            .emoji
            .as_deref()
            .filter(|value| !value.trim().is_empty())
        {
            parts.push(format!("emoji=\"{emoji}\""));
        }
        if let Some(set_name) = self
            .set_name
            .as_deref()
            .filter(|value| !value.trim().is_empty())
        {
            parts.push(format!("set=\"{set_name}\""));
        }
        parts.push(format!("type={}", self.kind()));
        if let (Some(width), Some(height)) = (self.width, self.height) {
            parts.push(format!("size={width}x{height}"));
        }
        parts.push("visual description unavailable".to_string());
        format!("[Sticker received: {}]", parts.join(", "))
    }

    pub fn metadata(&self) -> serde_json::Value {
        annotate_value(
            json!({
                "source": "telegram_sticker",
                "chat_id": self.chat_id,
                "user_id": self.user_id,
                "message_id": self.message_id,
                "file_id": self.file_id,
                "file_unique_id": self.file_unique_id,
                "emoji": self.emoji,
                "set_name": self.set_name,
                "is_animated": self.is_animated,
                "is_video": self.is_video,
                "width": self.width,
                "height": self.height,
                "file_size": self.file_size,
            }),
            MessageVisibility::UserVisible,
            MessageKind::TelegramMedia,
            "telegram",
        )
    }

    fn kind(&self) -> &'static str {
        if self.is_video {
            "video"
        } else if self.is_animated {
            "animated"
        } else {
            "static"
        }
    }
}


impl IncomingTelegramReaction {
    pub fn content(&self) -> String {
        format!(
            "[Telegram reaction added: {} on message {}]",
            self.emojis.join(" "),
            self.message_id
        )
    }

    pub fn metadata(&self) -> serde_json::Value {
        annotate_value(
            json!({
                "source": "telegram_reaction",
                "chat_id": self.chat_id,
                "user_id": self.user_id,
                "message_id": self.message_id,
                "reaction_new": self.emojis,
            }),
            MessageVisibility::UserVisible,
            MessageKind::TelegramReaction,
            "telegram",
        )
    }
}

#[derive(Debug, Deserialize)]
struct TelegramResponse<T> {
    ok: bool,
    #[serde(default)]
    result: Option<T>,
    #[serde(default)]
    description: Option<String>,
}

impl<T> TelegramResponse<T> {
    fn into_result(self) -> TelegramResult<T> {
        if self.ok {
            self.result
                .ok_or_else(|| TelegramError::Api("missing result".to_string()))
        } else {
            Err(TelegramError::Api(
                self.description
                    .unwrap_or_else(|| "unknown telegram error".to_string()),
            ))
        }
    }
}

#[derive(Debug, Serialize)]
struct SendMessageRequest<'a> {
    chat_id: i64,
    text: &'a str,
    disable_web_page_preview: bool,
}

#[derive(Debug, Serialize)]
struct SendChatActionRequest<'a> {
    chat_id: i64,
    action: &'a str,
}

#[derive(Debug, Default, Deserialize)]
struct SentTelegramMessage {
    message_id: i64,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Deserialize)]
pub struct TelegramFileInfo {
    pub file_id: String,
    #[serde(default)]
    pub file_unique_id: Option<String>,
    #[serde(default)]
    pub file_size: Option<i64>,
    pub file_path: String,
}

#[derive(Debug, Serialize)]
struct SetReactionRequest<'a> {
    chat_id: i64,
    message_id: i64,
    reaction: Vec<ReactionTypeEmoji<'a>>,
}

#[derive(Debug, Serialize)]
struct ReactionTypeEmoji<'a> {
    #[serde(rename = "type")]
    reaction_type: &'a str,
    emoji: &'a str,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum VisibleTelegramChannel {
    Reaction,
    Reply,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PendingReaction {
    pub chat_id: i64,
    pub message_id: i64,
    pub emoji: String,
}

#[derive(Debug)]
pub struct TelegramTurnGuard {
    pending_reactions: Vec<PendingReaction>,
    forced_channel: Option<VisibleTelegramChannel>,
}

impl TelegramTurnGuard {
    pub fn new() -> Self {
        Self {
            pending_reactions: Vec::new(),
            forced_channel: None,
        }
    }

    pub fn with_forced_channel(channel: VisibleTelegramChannel) -> Self {
        Self {
            pending_reactions: Vec::new(),
            forced_channel: Some(channel),
        }
    }

    pub fn queue_pending_reaction(&mut self, chat_id: i64, message_id: i64, emoji: &str) {
        self.pending_reactions.push(PendingReaction {
            chat_id,
            message_id,
            emoji: emoji.to_string(),
        });
    }

    pub fn has_pending_reactions(&self) -> bool {
        !self.pending_reactions.is_empty()
    }

    pub fn drain_pending_reactions(&mut self) -> Vec<PendingReaction> {
        std::mem::take(&mut self.pending_reactions)
    }

    pub fn choose_visible_channel(&self) -> VisibleTelegramChannel {
        self.forced_channel.unwrap_or_else(|| {
            let nanos = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|duration| duration.subsec_nanos())
                .unwrap_or(0);
            if nanos.is_multiple_of(2) {
                VisibleTelegramChannel::Reaction
            } else {
                VisibleTelegramChannel::Reply
            }
        })
    }
}

impl Default for TelegramTurnGuard {
    fn default() -> Self {
        Self::new()
    }
}

pub type SharedTelegramTurnGuard = Arc<Mutex<TelegramTurnGuard>>;

const TELEGRAM_TOOL_TYPING_REFRESH_SECONDS: u64 = 3;

/// [`crate::tools::registry::TurnObserver`] that keeps the Telegram "typing"
/// indicator alive for the duration of every tool call.
#[derive(Clone, Debug)]
pub struct TelegramTypingObserver {
    token: String,
    chat_id: i64,
}

impl TelegramTypingObserver {
    pub fn new(token: impl Into<String>, chat_id: i64) -> Self {
        Self {
            token: token.into(),
            chat_id,
        }
    }
}

impl crate::tools::registry::TurnObserver for TelegramTypingObserver {
    fn wrap_tool_call<'a>(
        &'a self,
        _name: &'a str,
        inner: crate::tools::registry::BoxToolFuture<'a>,
    ) -> crate::tools::registry::BoxToolFuture<'a> {
        let token = self.token.clone();
        let chat_id = self.chat_id;
        Box::pin(async move {
            let Ok(client) = TelegramClient::new(token, Vec::new()) else {
                return inner.await;
            };
            let _ = client.send_chat_action(chat_id, "typing").await;
            let typing_task = tokio::spawn(typing_refresh_loop(client, chat_id));
            let output = inner.await;
            typing_task.abort();
            let _ = typing_task.await;
            output
        })
    }
}

async fn typing_refresh_loop(client: TelegramClient, chat_id: i64) {
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(
            TELEGRAM_TOOL_TYPING_REFRESH_SECONDS,
        ))
        .await;
        if let Err(error) = client.send_chat_action(chat_id, "typing").await {
            tracing::debug!(chat_id, error = %error, "telegram tool typing action failed");
            return;
        }
    }
}

#[derive(Clone, Debug)]
pub struct TelegramToolContext {
    pub token: String,
    pub chat_id: i64,
    pub last_message_id: Option<i64>,
    pub guard: Option<SharedTelegramTurnGuard>,
    pub dry_run: bool,
}

impl crate::tools::registry::MessageEgress for TelegramToolContext {
    fn send_message(&self, text: &str, parse_mode: &str) -> String {
        Self::send_message(self, text, parse_mode)
    }
    fn send_file(&self, file_path_or_url: &str, caption: &str, as_document: bool) -> String {
        Self::send_file(self, file_path_or_url, caption, as_document)
    }
    fn react(&self, emoji: &str, message_id: i64) -> String {
        Self::react(self, emoji, message_id)
    }
}

impl TelegramToolContext {
    pub fn send_message(&self, text: &str, parse_mode: &str) -> String {
        let text = text.trim();
        if text.is_empty() {
            return error_payload("Telegram message text is required.");
        }
        let parse_mode = telegram_parse_mode(parse_mode);
        if self.dry_run {
            return serde_json::to_string_pretty(&json!({
                "success": true,
                "message_id": 0,
                "chat_id": self.chat_id,
                "parse_mode": parse_mode,
            }))
            .unwrap();
        }
        match send_message_blocking(&self.token, self.chat_id, text, parse_mode) {
            Ok(message_id) => serde_json::to_string_pretty(&json!({
                "success": true,
                "message_id": message_id,
                "chat_id": self.chat_id,
            }))
            .unwrap(),
            Err(error) => error_payload(&error.to_string()),
        }
    }

    pub fn send_file(&self, file_path_or_url: &str, caption: &str, as_document: bool) -> String {
        let plan = match TelegramFilePlan::from_source(file_path_or_url, as_document) {
            Ok(plan) => plan,
            Err(error) => return error_payload(&error),
        };
        if self.dry_run {
            return serde_json::to_string_pretty(&json!({
                "success": true,
                "type": plan.send_type.as_str(),
                "filename": plan.filename,
                "chat_id": self.chat_id,
                "message_id": 0,
                "method": plan.send_type.method(),
                "source": if plan.is_url { "url" } else { "local" },
            }))
            .unwrap();
        }
        let result = if plan.is_url {
            send_file_url_blocking(&self.token, self.chat_id, &plan, caption)
        } else {
            send_file_path_blocking(&self.token, self.chat_id, &plan, caption)
        };
        match result {
            Ok(message_id) => serde_json::to_string_pretty(&json!({
                "success": true,
                "type": plan.send_type.as_str(),
                "filename": plan.filename,
                "chat_id": self.chat_id,
                "message_id": message_id,
            }))
            .unwrap(),
            Err(error) => error_payload(&error.to_string()),
        }
    }

    pub fn react(&self, emoji: &str, message_id: i64) -> String {
        let target_message_id = if message_id > 0 {
            Some(message_id)
        } else {
            self.last_message_id
        };
        let Some(target_message_id) = target_message_id else {
            return error_payload("Telegram context not set or no message to react to.");
        };
        let emoji = emoji.trim();
        if emoji.is_empty() {
            return error_payload("Telegram reaction emoji is required.");
        }
        if let Some(guard) = &self.guard {
            match guard.lock() {
                Ok(mut guard) => {
                    guard.queue_pending_reaction(self.chat_id, target_message_id, emoji);
                    return serde_json::to_string_pretty(&json!({
                        "success": true,
                        "queued": true,
                        "emoji": emoji,
                        "message_id": target_message_id,
                    }))
                    .unwrap();
                }
                Err(error) => {
                    return error_payload(&format!("Telegram turn guard poisoned: {error}"));
                }
            }
        }

        let success =
            set_message_reaction_blocking(&self.token, self.chat_id, target_message_id, emoji)
                .unwrap_or(false);
        serde_json::to_string_pretty(&json!({
            "success": success,
            "emoji": emoji,
            "message_id": target_message_id,
        }))
        .unwrap()
    }
}

pub fn send_message_blocking(
    token: &str,
    chat_id: i64,
    text: &str,
    parse_mode: Option<&'static str>,
) -> TelegramResult<i64> {
    let mut payload = json!({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": true,
    });
    if let Some(parse_mode) = parse_mode {
        payload["parse_mode"] = json!(parse_mode);
    }
    let response = reqwest::blocking::Client::new()
        .post(format!("https://api.telegram.org/bot{token}/sendMessage"))
        .json(&payload)
        .send()?
        .error_for_status()?
        .json::<TelegramResponse<SentTelegramMessage>>()?;
    response.into_result().map(|message| message.message_id)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TelegramFileSendType {
    Photo,
    Animation,
    Video,
    Voice,
    Audio,
    Document,
}

impl TelegramFileSendType {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Photo => "photo",
            Self::Animation => "animation",
            Self::Video => "video",
            Self::Voice => "voice",
            Self::Audio => "audio",
            Self::Document => "document",
        }
    }

    fn method(self) -> &'static str {
        match self {
            Self::Photo => "sendPhoto",
            Self::Animation => "sendAnimation",
            Self::Video => "sendVideo",
            Self::Voice => "sendVoice",
            Self::Audio => "sendAudio",
            Self::Document => "sendDocument",
        }
    }

    fn field(self) -> &'static str {
        self.as_str()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelegramFilePlan {
    pub original: String,
    pub filename: String,
    pub path: Option<PathBuf>,
    pub is_url: bool,
    pub send_type: TelegramFileSendType,
}

impl TelegramFilePlan {
    pub fn from_source(source: &str, as_document: bool) -> Result<Self, String> {
        let source = source.trim();
        if source.is_empty() {
            return Err("Telegram file path or URL is required.".to_string());
        }
        let is_url = source.starts_with("http://") || source.starts_with("https://");
        let (filename, path) = if is_url {
            (filename_from_url(source), None)
        } else {
            let path = expand_tilde(source);
            if !path.exists() {
                return Err(format!("File not found: {}", path.display()));
            }
            let filename = path
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("file")
                .to_string();
            (filename, Some(path))
        };
        let ext = Path::new(&filename)
            .extension()
            .and_then(|value| value.to_str())
            .unwrap_or_default()
            .to_ascii_lowercase();
        let send_type = if as_document {
            TelegramFileSendType::Document
        } else {
            match ext.as_str() {
                "jpg" | "jpeg" | "png" | "webp" | "bmp" => TelegramFileSendType::Photo,
                "gif" => TelegramFileSendType::Animation,
                "mp4" | "avi" | "mov" | "mkv" | "webm" => TelegramFileSendType::Video,
                "ogg" => TelegramFileSendType::Voice,
                "mp3" | "wav" | "flac" | "m4a" => TelegramFileSendType::Audio,
                _ => TelegramFileSendType::Document,
            }
        };
        Ok(Self {
            original: source.to_string(),
            filename,
            path,
            is_url,
            send_type,
        })
    }
}

fn send_file_url_blocking(
    token: &str,
    chat_id: i64,
    plan: &TelegramFilePlan,
    caption: &str,
) -> TelegramResult<i64> {
    let mut payload = json!({
        "chat_id": chat_id,
        plan.send_type.field(): plan.original,
    });
    if !caption.trim().is_empty() {
        payload["caption"] = json!(caption.trim());
    }
    let response = reqwest::blocking::Client::new()
        .post(format!(
            "https://api.telegram.org/bot{token}/{}",
            plan.send_type.method()
        ))
        .json(&payload)
        .send()?
        .error_for_status()?
        .json::<TelegramResponse<SentTelegramMessage>>()?;
    response.into_result().map(|message| message.message_id)
}

fn send_file_path_blocking(
    token: &str,
    chat_id: i64,
    plan: &TelegramFilePlan,
    caption: &str,
) -> TelegramResult<i64> {
    let Some(path) = &plan.path else {
        return Err(TelegramError::Api("missing local file path".to_string()));
    };
    let mut form = reqwest::blocking::multipart::Form::new()
        .text("chat_id", chat_id.to_string())
        .part(
            plan.send_type.field().to_string(),
            reqwest::blocking::multipart::Part::file(path)?,
        );
    if !caption.trim().is_empty() {
        form = form.text("caption", caption.trim().to_string());
    }
    let response = reqwest::blocking::Client::new()
        .post(format!(
            "https://api.telegram.org/bot{token}/{}",
            plan.send_type.method()
        ))
        .multipart(form)
        .send()?
        .error_for_status()?
        .json::<TelegramResponse<SentTelegramMessage>>()?;
    response.into_result().map(|message| message.message_id)
}

pub fn set_message_reaction_blocking(
    token: &str,
    chat_id: i64,
    message_id: i64,
    emoji: &str,
) -> TelegramResult<bool> {
    let emoji = emoji.trim();
    if emoji.is_empty() {
        return Ok(false);
    }
    let response = reqwest::blocking::Client::new()
        .post(format!(
            "https://api.telegram.org/bot{token}/setMessageReaction"
        ))
        .json(&SetReactionRequest {
            chat_id,
            message_id,
            reaction: vec![ReactionTypeEmoji {
                reaction_type: "emoji",
                emoji,
            }],
        })
        .send()?
        .error_for_status()?
        .json::<TelegramResponse<bool>>()?;
    match response.into_result() {
        Ok(value) => Ok(value),
        Err(TelegramError::Api(message)) if is_invalid_reaction_error(&message) => Ok(false),
        Err(error) => Err(error),
    }
}


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_text_update_and_authorization() {
        let raw = r#"{
            "update_id": 42,
            "message": {
                "message_id": 5,
                "chat": {"id": 100},
                "from": {"id": 7},
                "text": " hello "
            }
        }"#;
        let update: TelegramUpdate = serde_json::from_str(raw).unwrap();
        let incoming = update.incoming_text().unwrap();
        assert_eq!(incoming.update_id, 42);
        assert_eq!(incoming.chat_id, 100);
        assert_eq!(incoming.user_id, 7);
        assert_eq!(incoming.message_id, 5);
        assert_eq!(incoming.text, "hello");

        let client = TelegramClient::new("token", vec![7]).unwrap();
        assert!(client.user_allowed(7));
        assert!(!client.user_allowed(8));
    }

    #[test]
    fn parses_reaction_update_as_synthetic_context() {
        let raw = r#"{
            "update_id": 43,
            "message_reaction": {
                "chat": {"id": 100},
                "message_id": 5,
                "user": {"id": 7},
                "old_reaction": [],
                "new_reaction": [{"type": "emoji", "emoji": "🔥"}]
            }
        }"#;
        let update: TelegramUpdate = serde_json::from_str(raw).unwrap();
        let incoming = update.incoming_reaction().unwrap();

        assert_eq!(incoming.update_id, 43);
        assert_eq!(incoming.chat_id, 100);
        assert_eq!(incoming.user_id, 7);
        assert_eq!(incoming.message_id, 5);
        assert_eq!(incoming.emojis, vec!["🔥"]);
        assert!(incoming.content().contains("Telegram reaction added"));
        assert_eq!(incoming.metadata()["reaction_new"][0], "🔥");
    }

    #[test]
    fn parses_voice_update_for_transcription() {
        let raw = r#"{
            "update_id": 44,
            "message": {
                "message_id": 6,
                "chat": {"id": 100},
                "from": {"id": 7},
                "caption": "context",
                "voice": {
                    "file_id": "voice-file",
                    "file_unique_id": "voice-unique",
                    "duration": 3,
                    "mime_type": "audio/ogg",
                    "file_size": 1234
                }
            }
        }"#;
        let update: TelegramUpdate = serde_json::from_str(raw).unwrap();
        let incoming = update.incoming_audio().unwrap();

        assert!(incoming.is_voice);
        assert_eq!(incoming.chat_id, 100);
        assert_eq!(incoming.user_id, 7);
        assert_eq!(incoming.message_id, 6);
        assert_eq!(incoming.file_id, "voice-file");
        assert_eq!(incoming.file_name, "telegram_voice_voice-file.ogg");
        assert_eq!(incoming.mime_type.as_deref(), Some("audio/ogg"));
        assert_eq!(incoming.duration, Some(3));
        assert_eq!(incoming.file_size, Some(1234));
        assert_eq!(
            incoming.content_with_transcript("hello"),
            "[Transcribed voice message: hello]\nCaption: context"
        );
        let metadata = incoming.metadata("auto", "default");
        assert_eq!(metadata["is_voice"], true);
        assert_eq!(metadata["is_audio"], false);
        assert_eq!(metadata["file_name"], "telegram_voice_voice-file.ogg");
    }

    #[test]
    fn parses_audio_update_with_filename() {
        let raw = r#"{
            "update_id": 45,
            "message": {
                "message_id": 7,
                "chat": {"id": 101},
                "from": {"id": 8},
                "audio": {
                    "file_id": "audio-file",
                    "file_unique_id": "audio-unique",
                    "duration": 10,
                    "mime_type": "audio/mpeg",
                    "file_name": "song.mp3",
                    "file_size": 5678
                }
            }
        }"#;
        let update: TelegramUpdate = serde_json::from_str(raw).unwrap();
        let incoming = update.incoming_audio().unwrap();

        assert!(!incoming.is_voice);
        assert_eq!(incoming.chat_id, 101);
        assert_eq!(incoming.user_id, 8);
        assert_eq!(incoming.file_name, "song.mp3");
        assert_eq!(incoming.mime_type.as_deref(), Some("audio/mpeg"));
        assert_eq!(
            incoming.content_with_transcript("music"),
            "[Transcribed audio message: music]"
        );
    }

    #[test]
    fn parses_photo_update_for_multimodal_turn() {
        let raw = r#"{
            "update_id": 46,
            "message": {
                "message_id": 8,
                "chat": {"id": 102},
                "from": {"id": 9},
                "caption": "what is on this bench?",
                "photo": [
                    {
                        "file_id": "small-photo",
                        "file_unique_id": "small-unique",
                        "width": 90,
                        "height": 90,
                        "file_size": 111
                    },
                    {
                        "file_id": "large-photo",
                        "file_unique_id": "large-unique",
                        "width": 1280,
                        "height": 720,
                        "file_size": 222
                    }
                ]
            }
        }"#;
        let update: TelegramUpdate = serde_json::from_str(raw).unwrap();
        let incoming = update.incoming_photo().unwrap();

        assert_eq!(incoming.update_id, 46);
        assert_eq!(incoming.chat_id, 102);
        assert_eq!(incoming.user_id, 9);
        assert_eq!(incoming.file_id, "large-photo");
        assert_eq!(incoming.width, 1280);
        assert_eq!(incoming.height, 720);
        assert_eq!(incoming.content_text(), "what is on this bench?");
        assert_eq!(
            incoming.attachment_name("image/png"),
            "telegram_photo_large-photo.png"
        );
        assert_eq!(image_mime_type_from_path("photos/image.webp"), "image/webp");
        assert_eq!(incoming.metadata("image/png")["source"], "telegram_photo");
    }

    #[test]
    fn parses_document_update_with_safe_filename() {
        let raw = r#"{
            "update_id": 47,
            "message": {
                "message_id": 8,
                "chat": {"id": 102},
                "from": {"id": 9},
                "caption": "please review",
                "document": {
                    "file_id": "doc-file",
                    "file_unique_id": "doc-unique",
                    "file_name": "../report.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 999
                }
            }
        }"#;
        let update: TelegramUpdate = serde_json::from_str(raw).unwrap();
        let incoming = update.incoming_document().unwrap();

        assert_eq!(incoming.chat_id, 102);
        assert_eq!(incoming.user_id, 9);
        assert_eq!(incoming.file_id, "doc-file");
        assert_eq!(incoming.file_name, "report.pdf");
        assert_eq!(incoming.mime_type.as_deref(), Some("application/pdf"));
        assert_eq!(incoming.file_size, Some(999));
        assert!(
            incoming
                .content_with_path(Path::new("/tmp/report.pdf"))
                .contains("please review")
        );
    }

    #[test]
    fn parses_sticker_update_as_text_context() {
        let raw = r#"{
            "update_id": 48,
            "message": {
                "message_id": 9,
                "chat": {"id": 103},
                "from": {"id": 10},
                "sticker": {
                    "file_id": "sticker-file",
                    "file_unique_id": "sticker-unique",
                    "emoji": "🔥",
                    "set_name": "hot",
                    "is_animated": false,
                    "is_video": true,
                    "width": 512,
                    "height": 512,
                    "file_size": 321
                }
            }
        }"#;
        let update: TelegramUpdate = serde_json::from_str(raw).unwrap();
        let incoming = update.incoming_sticker().unwrap();

        assert_eq!(incoming.chat_id, 103);
        assert_eq!(incoming.user_id, 10);
        assert_eq!(incoming.file_id, "sticker-file");
        assert!(incoming.content().contains("emoji=\"🔥\""));
        assert!(incoming.content().contains("type=video"));
        assert_eq!(incoming.metadata()["is_video"], true);
    }

    #[test]
    fn emoji_only_reply_accepts_emoji_and_rejects_text() {
        for value in ["👍", "❤️", "🔥🔥", "👨‍👩‍👧‍👦", "👍🏻", "🇺🇦"]
        {
            assert!(is_emoji_only_reply(value), "{value}");
        }
        for value in ["", "   ", "👍!", "thanks 👍", "<3", "reply ❤️"] {
            assert!(!is_emoji_only_reply(value), "{value}");
        }
    }

    #[test]
    fn turn_guard_queues_drains_and_can_force_channel() {
        let mut guard = TelegramTurnGuard::with_forced_channel(VisibleTelegramChannel::Reaction);
        guard.queue_pending_reaction(99, 42, "🔥");
        guard.queue_pending_reaction(99, 43, "👍");

        assert!(guard.has_pending_reactions());
        assert_eq!(
            guard.choose_visible_channel(),
            VisibleTelegramChannel::Reaction
        );
        assert_eq!(
            guard.drain_pending_reactions(),
            vec![
                PendingReaction {
                    chat_id: 99,
                    message_id: 42,
                    emoji: "🔥".to_string(),
                },
                PendingReaction {
                    chat_id: 99,
                    message_id: 43,
                    emoji: "👍".to_string(),
                },
            ]
        );
        assert!(!guard.has_pending_reactions());
    }

    #[test]
    fn telegram_tool_context_queues_reaction_when_guarded() {
        let guard = Arc::new(Mutex::new(TelegramTurnGuard::new()));
        let context = TelegramToolContext {
            token: "token".to_string(),
            chat_id: 99,
            last_message_id: Some(42),
            guard: Some(guard.clone()),
            dry_run: true,
        };

        let payload = context.react("🔥", 77);
        let value: serde_json::Value = serde_json::from_str(&payload).unwrap();
        assert_eq!(value["success"], true);
        assert_eq!(value["queued"], true);
        assert_eq!(value["message_id"], 77);

        let pending = guard.lock().unwrap().drain_pending_reactions();
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].chat_id, 99);
        assert_eq!(pending[0].message_id, 77);
        assert_eq!(pending[0].emoji, "🔥");
    }

    #[test]
    fn parse_mode_maps_supported_values() {
        assert_eq!(telegram_parse_mode("markdown"), Some("MarkdownV2"));
        assert_eq!(telegram_parse_mode("MarkdownV2"), Some("MarkdownV2"));
        assert_eq!(telegram_parse_mode("html"), Some("HTML"));
        assert_eq!(telegram_parse_mode("plain"), None);
        assert_eq!(telegram_parse_mode(""), None);
    }

    #[test]
    fn file_plan_selects_send_type_from_extension() {
        let cases = [
            (
                "https://example.com/image.png",
                false,
                TelegramFileSendType::Photo,
            ),
            (
                "https://example.com/anim.gif",
                false,
                TelegramFileSendType::Animation,
            ),
            (
                "https://example.com/movie.mp4",
                false,
                TelegramFileSendType::Video,
            ),
            (
                "https://example.com/voice.ogg",
                false,
                TelegramFileSendType::Voice,
            ),
            (
                "https://example.com/song.mp3",
                false,
                TelegramFileSendType::Audio,
            ),
            (
                "https://example.com/report.pdf",
                false,
                TelegramFileSendType::Document,
            ),
            (
                "https://example.com/image.png",
                true,
                TelegramFileSendType::Document,
            ),
        ];

        for (source, as_document, expected) in cases {
            let plan = TelegramFilePlan::from_source(source, as_document).unwrap();
            assert_eq!(plan.send_type, expected);
            assert!(plan.is_url);
        }
    }

    #[test]
    fn telegram_tool_context_dry_runs_message_and_file() {
        let tmp = tempfile::tempdir().unwrap();
        let file = tmp.path().join("image.png");
        std::fs::write(&file, "not real image bytes").unwrap();
        let context = TelegramToolContext {
            token: "token".to_string(),
            chat_id: 99,
            last_message_id: Some(42),
            guard: None,
            dry_run: true,
        };

        let message: serde_json::Value =
            serde_json::from_str(&context.send_message("hello", "markdown")).unwrap();
        assert_eq!(message["success"], true);
        assert_eq!(message["chat_id"], 99);
        assert_eq!(message["parse_mode"], "MarkdownV2");

        let file_payload: serde_json::Value =
            serde_json::from_str(&context.send_file(file.to_str().unwrap(), "caption", false))
                .unwrap();
        assert_eq!(file_payload["success"], true);
        assert_eq!(file_payload["type"], "photo");
        assert_eq!(file_payload["filename"], "image.png");
    }

    #[test]
    fn split_messages_uses_typed_envelope_and_size_limit() {
        let chunks = split_telegram_messages(r#"{"messages":["one","two"]}"#);
        assert_eq!(chunks, vec!["one", "two"]);

        let chunks = split_telegram_messages(
            "```json\n{\"messages\":[{\"text\":\"one\"},{\"message\":\"two\"}]}\n```",
        );
        assert_eq!(chunks, vec!["one", "two"]);

        let chunks = split_telegram_messages("one\n---\ntwo");
        assert_eq!(chunks, vec!["one\n---\ntwo"]);

        let chunks = split_telegram_messages("one\n\n two");
        assert_eq!(chunks, vec!["one", "two"]);

        let chunks = split_telegram_messages("```\none\n\n two\n```");
        assert_eq!(chunks, vec!["```\none\n\n two\n```"]);

        let long = "x".repeat(5000);
        let chunks = split_telegram_messages(&long);
        assert_eq!(chunks.len(), 2);
        assert!(chunks.iter().all(|chunk| chunk.len() <= 4096));
    }

    #[test]
    fn telegram_response_reports_api_errors() {
        let response: TelegramResponse<serde_json::Value> =
            serde_json::from_str(r#"{"ok":false,"description":"Forbidden"}"#).unwrap();
        assert!(matches!(
            response.into_result().unwrap_err(),
            TelegramError::Api(message) if message == "Forbidden"
        ));
    }
}
