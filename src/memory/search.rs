use std::collections::BTreeSet;

pub fn query_terms(query: &str) -> Vec<String> {
    let mut terms = Vec::new();
    let mut current = String::new();
    for ch in query.to_ascii_lowercase().chars() {
        if ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-') {
            current.push(ch);
        } else if !current.is_empty() {
            terms.push(std::mem::take(&mut current));
        }
    }
    if !current.is_empty() {
        terms.push(current);
    }
    terms
}

pub fn clean_tags(tags: &[String]) -> Vec<String> {
    let mut clean = Vec::new();
    let mut seen = BTreeSet::new();
    for tag in tags {
        let normalized = tag.trim().to_ascii_lowercase();
        if !normalized.is_empty() && seen.insert(normalized.clone()) {
            clean.push(normalized);
        }
    }
    clean
}

pub fn tags_match_any(entry_tags: &[String], filter: &[String]) -> bool {
    if filter.is_empty() {
        return true;
    }
    filter.iter().any(|tag| entry_tags.contains(tag))
}

pub fn search_result_text(text: &str, max_lines: usize) -> String {
    let lines = text.lines().collect::<Vec<_>>();
    if lines.len() <= max_lines {
        return text.to_string();
    }
    format!(
        "{}\n[... {} more lines]",
        lines[..max_lines].join("\n"),
        lines.len() - max_lines
    )
}

pub fn indent_block(text: &str, prefix: &str) -> String {
    if text.is_empty() {
        return prefix.to_string();
    }
    text.lines()
        .map(|line| format!("{prefix}{line}"))
        .collect::<Vec<_>>()
        .join("\n")
}
