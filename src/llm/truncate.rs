use serde::{Deserialize, Serialize};

pub const DEFAULT_MAX_LINES: usize = 2_000;
pub const DEFAULT_MAX_BYTES: usize = 50 * 1024;
pub const GREP_MAX_LINE_LENGTH: usize = 500;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TruncatedBy {
    Lines,
    Bytes,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct TruncationResult {
    pub content: String,
    pub truncated: bool,
    pub truncated_by: Option<TruncatedBy>,
    pub total_lines: usize,
    pub total_bytes: usize,
    pub output_lines: usize,
    pub output_bytes: usize,
    pub last_line_partial: bool,
    pub first_line_exceeds_limit: bool,
    pub max_lines: usize,
    pub max_bytes: usize,
}

pub fn format_size(bytes_count: usize) -> String {
    if bytes_count < 1024 {
        format!("{bytes_count}B")
    } else if bytes_count < 1024 * 1024 {
        format!("{:.1}KB", bytes_count as f64 / 1024.0)
    } else {
        format!("{:.1}MB", bytes_count as f64 / (1024.0 * 1024.0))
    }
}

pub fn truncate_head(content: &str, max_lines: usize, max_bytes: usize) -> TruncationResult {
    let total_bytes = content.len();
    let lines: Vec<&str> = content.split('\n').collect();
    let total_lines = lines.len();

    if total_lines <= max_lines && total_bytes <= max_bytes {
        return result(
            content.to_string(),
            false,
            None,
            total_lines,
            total_bytes,
            total_lines,
            total_bytes,
            false,
            false,
            max_lines,
            max_bytes,
        );
    }

    if lines.first().is_some_and(|line| line.len() > max_bytes) {
        return result(
            String::new(),
            true,
            Some(TruncatedBy::Bytes),
            total_lines,
            total_bytes,
            0,
            0,
            false,
            true,
            max_lines,
            max_bytes,
        );
    }

    let mut output_lines = Vec::new();
    let mut output_bytes = 0;
    let mut truncated_by = TruncatedBy::Lines;

    for (index, line) in lines.iter().enumerate() {
        if index >= max_lines {
            break;
        }

        let line_bytes = line.len() + usize::from(index > 0);
        if output_bytes + line_bytes > max_bytes {
            truncated_by = TruncatedBy::Bytes;
            break;
        }

        output_lines.push(*line);
        output_bytes += line_bytes;
    }

    if output_lines.len() >= max_lines && output_bytes <= max_bytes {
        truncated_by = TruncatedBy::Lines;
    }

    let output_content = output_lines.join("\n");
    let output_bytes = output_content.len();
    result(
        output_content,
        true,
        Some(truncated_by),
        total_lines,
        total_bytes,
        output_lines.len(),
        output_bytes,
        false,
        false,
        max_lines,
        max_bytes,
    )
}

pub fn truncate_tail(content: &str, max_lines: usize, max_bytes: usize) -> TruncationResult {
    let total_bytes = content.len();
    let lines: Vec<&str> = content.split('\n').collect();
    let total_lines = lines.len();

    if total_lines <= max_lines && total_bytes <= max_bytes {
        return result(
            content.to_string(),
            false,
            None,
            total_lines,
            total_bytes,
            total_lines,
            total_bytes,
            false,
            false,
            max_lines,
            max_bytes,
        );
    }

    let mut output_lines: Vec<String> = Vec::new();
    let mut output_bytes = 0;
    let mut truncated_by = TruncatedBy::Lines;
    let mut last_line_partial = false;

    for line in lines.iter().rev() {
        if output_lines.len() >= max_lines {
            break;
        }

        let line_bytes = line.len() + usize::from(!output_lines.is_empty());
        if output_bytes + line_bytes > max_bytes {
            truncated_by = TruncatedBy::Bytes;
            if output_lines.is_empty() {
                let truncated_line = truncate_string_from_end(line, max_bytes);
                output_bytes = truncated_line.len();
                output_lines.insert(0, truncated_line);
                last_line_partial = true;
            }
            break;
        }

        output_lines.insert(0, (*line).to_string());
        output_bytes += line_bytes;
    }

    if output_lines.len() >= max_lines && output_bytes <= max_bytes {
        truncated_by = TruncatedBy::Lines;
    }

    let output_content = output_lines.join("\n");
    let output_bytes = output_content.len();
    result(
        output_content,
        true,
        Some(truncated_by),
        total_lines,
        total_bytes,
        output_lines.len(),
        output_bytes,
        last_line_partial,
        false,
        max_lines,
        max_bytes,
    )
}

pub fn truncate_line(line: &str, max_chars: usize) -> (String, bool) {
    if line.chars().count() <= max_chars {
        return (line.to_string(), false);
    }
    let prefix: String = line.chars().take(max_chars).collect();
    (format!("{prefix}... [truncated]"), true)
}

/// UTF-8 safe truncation that breaks on the last whitespace within the budget
/// and appends an ellipsis when the input is shortened. The returned string is
/// at most `max_chars` characters long (including the ellipsis itself).
pub fn truncate_with_ellipsis(value: &str, max_chars: usize) -> String {
    const ELLIPSIS: &str = "…";
    if max_chars == 0 {
        return String::new();
    }
    let total_chars = value.chars().count();
    if total_chars <= max_chars {
        return value.to_string();
    }
    let ellipsis_chars = ELLIPSIS.chars().count();
    if max_chars <= ellipsis_chars {
        return ELLIPSIS.chars().take(max_chars).collect();
    }
    let budget = max_chars - ellipsis_chars;
    let prefix: String = value.chars().take(budget).collect();
    let trimmed = match prefix.rfind(char::is_whitespace) {
        Some(idx) if idx >= prefix.len() / 2 => prefix[..idx].trim_end().to_string(),
        _ => prefix,
    };
    format!("{trimmed}{ELLIPSIS}")
}

pub fn format_truncation_notice(
    result: &TruncationResult,
    start_line: usize,
    temp_file_path: Option<&str>,
) -> String {
    if !result.truncated {
        return String::new();
    }

    let end_line = start_line + result.output_lines.saturating_sub(1);
    let next_offset = end_line + 1;

    if result.first_line_exceeds_limit {
        let first_line_size = format_size(result.total_bytes / result.total_lines.max(1));
        return format!(
            "[Line {start_line} is ~{first_line_size}, exceeds {} limit. Use bash: sed -n '{start_line}p' <file> | head -c {}]",
            format_size(result.max_bytes),
            result.max_bytes
        );
    }

    if result.last_line_partial {
        return if let Some(path) = temp_file_path {
            format!(
                "[Showing last {} of line {}. Full output: {path}]",
                format_size(result.output_bytes),
                result.total_lines
            )
        } else {
            format!(
                "[Showing last {} of line {}]",
                format_size(result.output_bytes),
                result.total_lines
            )
        };
    }

    let mut notice = if result.truncated_by == Some(TruncatedBy::Lines) {
        format!(
            "[Showing lines {start_line}-{end_line} of {}. Use offset={next_offset} to continue.]",
            result.total_lines
        )
    } else {
        format!(
            "[Showing lines {start_line}-{end_line} of {} ({} limit). Use offset={next_offset} to continue.]",
            result.total_lines,
            format_size(result.max_bytes)
        )
    };

    if let Some(path) = temp_file_path {
        notice.pop();
        notice.push_str(&format!(" Full output: {path}]"));
    }

    notice
}

fn truncate_string_from_end(value: &str, max_bytes: usize) -> String {
    let bytes = value.as_bytes();
    if bytes.len() <= max_bytes {
        return value.to_string();
    }

    let truncated = &bytes[bytes.len() - max_bytes..];
    let mut start = 0;
    while start < truncated.len() && (truncated[start] & 0xC0) == 0x80 {
        start += 1;
    }

    String::from_utf8_lossy(&truncated[start..]).into_owned()
}

#[allow(clippy::too_many_arguments)]
fn result(
    content: String,
    truncated: bool,
    truncated_by: Option<TruncatedBy>,
    total_lines: usize,
    total_bytes: usize,
    output_lines: usize,
    output_bytes: usize,
    last_line_partial: bool,
    first_line_exceeds_limit: bool,
    max_lines: usize,
    max_bytes: usize,
) -> TruncationResult {
    TruncationResult {
        content,
        truncated,
        truncated_by,
        total_lines,
        total_bytes,
        output_lines,
        output_bytes,
        last_line_partial,
        first_line_exceeds_limit,
        max_lines,
        max_bytes,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_sizes() {
        assert_eq!(format_size(100), "100B");
        assert_eq!(format_size(1024), "1.0KB");
        assert_eq!(format_size(1536), "1.5KB");
        assert_eq!(format_size(2 * 1024 * 1024), "2.0MB");
    }

    #[test]
    fn head_returns_content_unchanged_when_within_limits() {
        let content = "line 1\nline 2\nline 3";
        let result = truncate_head(content, DEFAULT_MAX_LINES, DEFAULT_MAX_BYTES);
        assert_eq!(result.content, content);
        assert!(!result.truncated);
        assert_eq!(result.truncated_by, None);
    }

    #[test]
    fn head_truncates_by_lines_or_bytes_without_partial_lines() {
        let lines: Vec<String> = (0..100).map(|index| format!("line {index}")).collect();
        let content = lines.join("\n");
        let by_lines = truncate_head(&content, 50, DEFAULT_MAX_BYTES);
        assert!(by_lines.truncated);
        assert_eq!(by_lines.truncated_by, Some(TruncatedBy::Lines));
        assert_eq!(by_lines.output_lines, 50);
        assert!(by_lines.content.contains("line 49"));
        assert!(!by_lines.content.contains("line 50"));

        let byte_heavy = vec!["x".repeat(100); 100].join("\n");
        let by_bytes = truncate_head(&byte_heavy, 1000, 500);
        assert_eq!(by_bytes.truncated_by, Some(TruncatedBy::Bytes));
        assert!(by_bytes.output_bytes <= 500);

        let second_line_long = format!("short\n{}", "x".repeat(5000));
        let result = truncate_head(&second_line_long, DEFAULT_MAX_LINES, 100);
        assert_eq!(result.content, "short");
    }

    #[test]
    fn head_reports_first_line_exceeding_byte_limit() {
        let result = truncate_head(&"x".repeat(10_000), DEFAULT_MAX_LINES, 1000);
        assert!(result.truncated);
        assert!(result.first_line_exceeds_limit);
        assert_eq!(result.content, "");
    }

    #[test]
    fn tail_keeps_end_and_can_keep_partial_last_line() {
        let lines: Vec<String> = (0..100).map(|index| format!("line {index}")).collect();
        let content = lines.join("\n");
        let result = truncate_tail(&content, 10, DEFAULT_MAX_BYTES);

        assert!(result.truncated);
        assert_eq!(result.truncated_by, Some(TruncatedBy::Lines));
        assert!(result.content.contains("line 99"));
        assert!(result.content.contains("line 90"));
        assert!(!result.content.contains("line 89"));

        let partial = truncate_tail(&"x".repeat(10_000), DEFAULT_MAX_LINES, 500);
        assert!(partial.last_line_partial);
        assert!(partial.output_bytes <= 500);
    }

    #[test]
    fn truncate_line_uses_character_count_and_suffix() {
        let (short, was_truncated) = truncate_line("short", GREP_MAX_LINE_LENGTH);
        assert_eq!(short, "short");
        assert!(!was_truncated);

        let (long, was_truncated) = truncate_line(&"x".repeat(600), 50);
        assert!(was_truncated);
        assert!(long.starts_with(&"x".repeat(50)));
        assert!(long.contains("[truncated]"));
    }

    #[test]
    fn truncation_notice_includes_continuation_and_temp_file() {
        let content = (0..100)
            .map(|index| format!("line {index}"))
            .collect::<Vec<_>>()
            .join("\n");
        let result = truncate_head(&content, 50, DEFAULT_MAX_BYTES);
        let notice = format_truncation_notice(&result, 1, Some("/tmp/output.log"));

        assert!(notice.contains("offset=51"));
        assert!(notice.contains("/tmp/output.log"));
    }

    #[test]
    fn utf8_tail_truncation_does_not_break_characters() {
        let content = format!("{}{}", "x".repeat(100), "🎉".repeat(100));
        let result = truncate_tail(&content, DEFAULT_MAX_LINES, 150);
        assert!(result.content.is_char_boundary(result.content.len()));
        assert!(result.output_bytes <= 150);
    }
}
