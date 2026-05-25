use std::collections::HashMap;

use serde_json::Value;

use crate::config::Settings;
use crate::memory::message_metadata::MessageMetadata;
use crate::memory::messages::StoredMessage;

const RECENT_TOOL_CONTEXT_GROUPS: usize = 2;
const OLD_TOOL_RESULT_PREVIEW_LINES: usize = 5;
const OLD_TOOL_RESULT_PREVIEW_CHARS: usize = 2_000;
const TOOL_CONTEXT_MIN_CHARS: usize = 64 * 1024;
const TOOL_CONTEXT_MAX_CHARS: usize = 400_000;
const TOOL_CONTEXT_SHARE_NUMERATOR: usize = 3;
const TOOL_CONTEXT_SHARE_DENOMINATOR: usize = 10;
const SEARCH_RESULT_SKIP_TOOLS: &[&str] = &["conversation_search", "archival_search"];

#[derive(Clone, Debug)]
struct ToolCallRecord {
    id: String,
    name: String,
    arguments: String,
}

#[derive(Clone, Debug)]
struct ToolResultRecord {
    call_id: String,
    name: String,
    content: String,
}

#[derive(Clone, Debug)]
struct ToolHistoryGroup {
    created_at: String,
    assistant_text: String,
    calls: Vec<ToolCallRecord>,
    results: Vec<ToolResultRecord>,
}

/// Build the `<recent_tool_context>` block injected into the system prompt.
/// Captures the last two assistant tool turns so the model sees the freshest
/// commands and results, with the latest turn shown in full and earlier turns
/// previewed.
pub(super) fn recent_tool_context_for_turn(
    recent: &[StoredMessage],
    settings: &Settings,
) -> Option<String> {
    let mut groups = Vec::new();
    let mut current: Option<ToolHistoryGroup> = None;
    let mut inside_internal_turn = false;

    for message in recent {
        let internal = MessageMetadata::from_value(Some(&message.metadata)).is_internal();
        if message.role.is_user() {
            inside_internal_turn = internal;
            if let Some(group) = current.take() {
                groups.push(group);
            }
            continue;
        }
        if inside_internal_turn || internal {
            continue;
        }

        if message.role.is_assistant() {
            let calls = tool_calls_from_metadata(&message.metadata);
            if let Some(group) = current.take() {
                groups.push(group);
            }
            if !calls.is_empty() {
                current = Some(ToolHistoryGroup {
                    created_at: message.created_at.clone(),
                    assistant_text: message.content.clone(),
                    calls,
                    results: Vec::new(),
                });
            }
        } else if message.role.is_tool() {
            if let Some(group) = current.as_mut()
                && let Some(result) = tool_result_from_message(message)
                && !SEARCH_RESULT_SKIP_TOOLS.contains(&result.name.as_str())
            {
                group.results.push(result);
            }
        } else if let Some(group) = current.take() {
            groups.push(group);
        }
    }
    if let Some(group) = current {
        groups.push(group);
    }

    groups.retain(|group| {
        group
            .calls
            .iter()
            .any(|call| !SEARCH_RESULT_SKIP_TOOLS.contains(&call.name.as_str()))
            && !group.results.is_empty()
    });
    if groups.is_empty() {
        return None;
    }

    let start = groups.len().saturating_sub(RECENT_TOOL_CONTEXT_GROUPS);
    let selected = &groups[start..];
    let latest_index = selected.len().saturating_sub(1);
    let mut parts = vec![format!(
        "<recent_tool_context groups=\"{}\">",
        selected.len()
    )];
    for (index, group) in selected.iter().enumerate() {
        parts.push(format_tool_history_group(group, index == latest_index));
    }
    parts.push("</recent_tool_context>".to_string());

    Some(cap_context_text(
        &parts.join("\n"),
        tool_context_budget_chars(settings),
        "recent tool context",
    ))
}

fn tool_calls_from_metadata(metadata: &Value) -> Vec<ToolCallRecord> {
    metadata
        .get("tool_calls")
        .and_then(Value::as_array)
        .map(|calls| {
            calls
                .iter()
                .filter_map(|call| {
                    let id = call
                        .get("id")
                        .or_else(|| call.get("call_id"))
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string();
                    let name = call
                        .get("function")
                        .and_then(|function| function.get("name"))
                        .or_else(|| call.get("fn_name"))
                        .or_else(|| call.get("name"))
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .trim()
                        .to_string();
                    if name.is_empty() {
                        return None;
                    }
                    let arguments = call
                        .get("function")
                        .and_then(|function| function.get("arguments"))
                        .or_else(|| call.get("fn_arguments"))
                        .or_else(|| call.get("arguments"))
                        .map(format_jsonish)
                        .unwrap_or_default();
                    Some(ToolCallRecord {
                        id,
                        name,
                        arguments,
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

fn tool_result_from_message(message: &StoredMessage) -> Option<ToolResultRecord> {
    let name = message
        .metadata
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("tool")
        .trim()
        .to_string();
    if name.is_empty() {
        return None;
    }
    Some(ToolResultRecord {
        call_id: message
            .metadata
            .get("tool_call_id")
            .or_else(|| message.metadata.get("call_id"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        name,
        content: message.content.clone(),
    })
}

fn format_tool_history_group(group: &ToolHistoryGroup, full_latest: bool) -> String {
    let mut results_by_id = HashMap::<&str, Vec<&ToolResultRecord>>::new();
    let mut unmatched_results = Vec::new();
    for result in &group.results {
        if result.call_id.trim().is_empty() {
            unmatched_results.push(result);
        } else {
            results_by_id
                .entry(result.call_id.as_str())
                .or_default()
                .push(result);
        }
    }

    let mut parts = vec![format!("<tool_turn timestamp=\"{}\">", group.created_at)];
    if !group.assistant_text.trim().is_empty() {
        parts.push(format!(
            "<assistant_tool_prelude>\n{}\n</assistant_tool_prelude>",
            group.assistant_text.trim()
        ));
    }

    for call in &group.calls {
        if SEARCH_RESULT_SKIP_TOOLS.contains(&call.name.as_str()) {
            continue;
        }
        parts.push(format!(
            "<tool_call name=\"{}\" id=\"{}\">",
            call.name, call.id
        ));
        if !call.arguments.trim().is_empty() {
            parts.push(format!("<arguments>\n{}\n</arguments>", call.arguments));
        }
        let mut attached = results_by_id
            .remove(call.id.as_str())
            .unwrap_or_default()
            .into_iter()
            .collect::<Vec<_>>();
        if attached.is_empty() && group.calls.len() == 1 {
            attached.append(&mut unmatched_results);
        } else {
            let mut index = 0;
            while index < unmatched_results.len() {
                if unmatched_results[index].name == call.name {
                    attached.push(unmatched_results.remove(index));
                } else {
                    index += 1;
                }
            }
        }
        for result in attached {
            parts.push(format_tool_result(result, full_latest));
        }
        parts.push("</tool_call>".to_string());
    }

    for results in results_by_id.into_values() {
        for result in results {
            parts.push(format_tool_result(result, full_latest));
        }
    }
    for result in unmatched_results {
        parts.push(format_tool_result(result, full_latest));
    }

    parts.push("</tool_turn>".to_string());
    parts.join("\n")
}

fn format_tool_result(result: &ToolResultRecord, full_latest: bool) -> String {
    let original_chars = result.content.chars().count();
    let original_lines = result.content.lines().count().max(1);
    let (content, mode) = if full_latest {
        (result.content.clone(), "full")
    } else {
        (preview_tool_result(&result.content), "preview")
    };
    format!(
        "<tool_result name=\"{}\" mode=\"{}\" chars=\"{}\" lines=\"{}\">\n{}\n</tool_result>",
        result.name, mode, original_chars, original_lines, content
    )
}

fn preview_tool_result(content: &str) -> String {
    let lines = content.lines().collect::<Vec<_>>();
    let mut preview = if lines.len() > OLD_TOOL_RESULT_PREVIEW_LINES {
        format!(
            "{}\n[... {} more lines skipped]",
            lines[..OLD_TOOL_RESULT_PREVIEW_LINES].join("\n"),
            lines.len() - OLD_TOOL_RESULT_PREVIEW_LINES
        )
    } else {
        content.to_string()
    };
    if preview.chars().count() > OLD_TOOL_RESULT_PREVIEW_CHARS {
        preview = format!(
            "{}\n[... {} chars skipped]",
            take_chars(&preview, OLD_TOOL_RESULT_PREVIEW_CHARS),
            preview.chars().count() - OLD_TOOL_RESULT_PREVIEW_CHARS
        );
    }
    preview
}

fn tool_context_budget_chars(settings: &Settings) -> usize {
    let proportional = settings
        .llm
        .llm_context_limit
        .saturating_mul(4)
        .saturating_mul(TOOL_CONTEXT_SHARE_NUMERATOR)
        / TOOL_CONTEXT_SHARE_DENOMINATOR;
    proportional.clamp(TOOL_CONTEXT_MIN_CHARS, TOOL_CONTEXT_MAX_CHARS)
}

fn cap_context_text(value: &str, max_chars: usize, label: &str) -> String {
    let chars = value.chars().count();
    if chars <= max_chars {
        return value.to_string();
    }
    let keep = max_chars.saturating_sub(200).max(1);
    let tail_probe = value
        .chars()
        .rev()
        .take(2_000)
        .collect::<String>()
        .chars()
        .rev()
        .collect::<String>()
        .to_ascii_lowercase();
    let has_error_tail = [
        "error",
        "exception",
        "failed",
        "fatal",
        "traceback",
        "panic",
        "exit code",
    ]
    .iter()
    .any(|needle| tail_probe.contains(needle));
    let head_share = if has_error_tail { 60 } else { 70 };
    let head_chars = keep.saturating_mul(head_share) / 100;
    let tail_chars = keep.saturating_sub(head_chars);
    format!(
        "{}\n\n[... {} chars truncated from {label} ...]\n\n{}",
        take_chars(value, head_chars),
        chars.saturating_sub(keep),
        take_last_chars(value, tail_chars)
    )
}

fn take_chars(value: &str, limit: usize) -> String {
    value.chars().take(limit).collect()
}

fn take_last_chars(value: &str, limit: usize) -> String {
    let mut chars = value.chars().rev().take(limit).collect::<Vec<_>>();
    chars.reverse();
    chars.into_iter().collect()
}

fn format_jsonish(value: &Value) -> String {
    match value {
        Value::String(value) => value.clone(),
        other => serde_json::to_string(other).unwrap_or_default(),
    }
}
