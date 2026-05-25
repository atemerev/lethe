//! Pure utility functions for Telegram payload handling: message chunking,
//! parse-mode normalization, emoji detection, MIME guessing, and filename
//! sanitization. No I/O — extracted out of `telegram.rs` so the long-poll
//! module stays focused on transport plumbing.

use std::path::{Path, PathBuf};

use serde_json::json;

use crate::llm::response_format::message_envelope_segments;

/// Map a Telegram parse-mode hint to the API value, returning `None` for
/// "no formatting".
pub fn telegram_parse_mode(value: &str) -> Option<&'static str> {
    match value.trim().to_ascii_lowercase().as_str() {
        "markdown" | "markdownv2" => Some("MarkdownV2"),
        "html" => Some("HTML"),
        _ => None,
    }
}

/// Guess a MIME type from a Telegram file path's extension. Defaults to JPEG
/// because Telegram photo files frequently lack a real extension.
pub fn image_mime_type_from_path(file_path: &str) -> &'static str {
    match file_path
        .rsplit('.')
        .next()
        .unwrap_or_default()
        .to_ascii_lowercase()
        .as_str()
    {
        "png" => "image/png",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "jpg" | "jpeg" => "image/jpeg",
        _ => "image/jpeg",
    }
}

pub(super) fn image_extension_for_mime(content_type: &str) -> Option<&'static str> {
    match content_type.trim().to_ascii_lowercase().as_str() {
        "image/png" => Some("png"),
        "image/gif" => Some("gif"),
        "image/webp" => Some("webp"),
        "image/jpeg" | "image/jpg" => Some("jpg"),
        _ => None,
    }
}

/// True when the entire reply is one or more emoji (possibly with skin-tone
/// modifiers and ZWJ joiners), used to decide whether to react vs message.
pub fn is_emoji_only_reply(text: &str) -> bool {
    let stripped = text.trim();
    if stripped.is_empty() {
        return false;
    }
    let mut saw_emoji = false;
    for ch in stripped.chars() {
        if ch.is_whitespace() || ch == '\u{200d}' || ch == '\u{fe0f}' {
            continue;
        }
        let code = ch as u32;
        if (0x1F3FB..=0x1F3FF).contains(&code) {
            continue;
        }
        if is_emoji_base_char(code) {
            saw_emoji = true;
            continue;
        }
        return false;
    }
    saw_emoji
}

fn is_emoji_base_char(code: u32) -> bool {
    (0x1F1E6..=0x1F1FF).contains(&code)
        || (0x1F300..=0x1FAFF).contains(&code)
        || (0x2600..=0x27BF).contains(&code)
}

pub(super) fn is_invalid_reaction_error(message: &str) -> bool {
    message.to_ascii_uppercase().contains("REACTION_INVALID")
}

pub(super) fn filename_from_url(url: &str) -> String {
    let without_query = url.split('?').next().unwrap_or(url);
    without_query
        .rsplit('/')
        .next()
        .filter(|name| !name.trim().is_empty())
        .unwrap_or("file")
        .to_string()
}

pub(super) fn safe_file_name(raw: &str) -> String {
    Path::new(raw)
        .file_name()
        .and_then(|name| name.to_str())
        .map(str::trim)
        .filter(|name| !name.is_empty() && *name != "." && *name != "..")
        .unwrap_or("telegram_document")
        .to_string()
}

pub(super) fn expand_tilde(path: &str) -> PathBuf {
    if let Some(rest) = path.strip_prefix("~/")
        && let Some(home) = std::env::var_os("HOME")
    {
        return PathBuf::from(home).join(rest);
    }
    PathBuf::from(path)
}

pub(super) fn error_payload(message: &str) -> String {
    serde_json::to_string_pretty(&json!({
        "success": false,
        "error": message,
    }))
    .unwrap()
}

/// Chunk a long message into Telegram-sized (4096-char) pieces while
/// respecting paragraph and code-block boundaries.
pub fn split_telegram_messages(text: &str) -> Vec<String> {
    const LIMIT: usize = 4096;
    let mut chunks = Vec::new();
    for segment in telegram_message_segments(text) {
        let mut current = String::new();
        for line in segment.lines() {
            let additional = if current.is_empty() {
                line.len()
            } else {
                line.len() + 1
            };
            if !current.is_empty() && current.len() + additional > LIMIT {
                chunks.push(current);
                current = String::new();
            }
            if line.len() > LIMIT {
                if !current.is_empty() {
                    chunks.push(std::mem::take(&mut current));
                }
                let mut part = String::new();
                for ch in line.chars() {
                    if part.len() + ch.len_utf8() > LIMIT {
                        chunks.push(part);
                        part = String::new();
                    }
                    part.push(ch);
                }
                if !part.is_empty() {
                    chunks.push(part);
                }
            } else {
                if !current.is_empty() {
                    current.push('\n');
                }
                current.push_str(line);
            }
        }
        if !current.trim().is_empty() {
            chunks.push(current);
        }
    }
    if chunks.is_empty() {
        Vec::new()
    } else {
        chunks
    }
}

fn telegram_message_segments(text: &str) -> Vec<String> {
    message_envelope_segments(text).unwrap_or_else(|| split_paragraph_segments(text))
}

fn split_paragraph_segments(text: &str) -> Vec<String> {
    let mut segments = Vec::new();
    let mut current = Vec::new();
    let mut in_code_block = false;
    for line in text.lines() {
        if line.trim_start().starts_with("```") {
            in_code_block = !in_code_block;
        }
        if !in_code_block && line.trim().is_empty() {
            let segment = current.join("\n").trim().to_string();
            if !segment.is_empty() {
                segments.push(segment);
            }
            current.clear();
        } else {
            current.push(line);
        }
    }
    let segment = current.join("\n").trim().to_string();
    if !segment.is_empty() {
        segments.push(segment);
    }
    if segments.is_empty() && !text.trim().is_empty() {
        segments.push(text.trim().to_string());
    }
    segments
}
