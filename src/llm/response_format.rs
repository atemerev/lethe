use serde_json::Value;

pub fn message_envelope_segments(text: &str) -> Option<Vec<String>> {
    let payload = strip_json_code_fence(text.trim());
    let value = serde_json::from_str::<Value>(&payload).ok()?;
    let messages = match value {
        Value::Array(values) => collect_message_values(&values),
        Value::Object(object) => {
            let array = ["telegram_messages", "messages", "message_chunks"]
                .into_iter()
                .find_map(|key| object.get(key).and_then(Value::as_array));
            if let Some(values) = array {
                collect_message_values(values)
            } else {
                object
                    .get("message")
                    .or_else(|| object.get("text"))
                    .and_then(Value::as_str)
                    .map(|message| vec![message.trim().to_string()])
                    .unwrap_or_default()
            }
        }
        Value::String(message) => vec![message.trim().to_string()],
        _ => Vec::new(),
    };

    let messages = messages
        .into_iter()
        .map(|message| message.trim().to_string())
        .filter(|message| !message.is_empty())
        .collect::<Vec<_>>();
    (!messages.is_empty()).then_some(messages)
}

pub fn normalize_message_envelope(text: &str) -> Option<String> {
    message_envelope_segments(text).map(|messages| messages.join("\n\n"))
}

fn collect_message_values(values: &[Value]) -> Vec<String> {
    values
        .iter()
        .filter_map(|value| match value {
            Value::String(message) => Some(message.as_str()),
            Value::Object(object) => object
                .get("text")
                .or_else(|| object.get("message"))
                .and_then(Value::as_str),
            _ => None,
        })
        .map(str::to_string)
        .collect()
}

fn strip_json_code_fence(text: &str) -> String {
    let Some(rest) = text.strip_prefix("```") else {
        return text.to_string();
    };
    let Some(first_newline) = rest.find('\n') else {
        return text.to_string();
    };
    let info = rest[..first_newline].trim();
    if !info.is_empty() && !info.eq_ignore_ascii_case("json") {
        return text.to_string();
    }
    let body = rest[first_newline + 1..].trim();
    let Some(body) = body.strip_suffix("```") else {
        return text.to_string();
    };
    body.trim().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_message_envelope_variants() {
        assert_eq!(
            message_envelope_segments(r#"{"messages":["one","two"]}"#).unwrap(),
            vec!["one", "two"]
        );
        assert_eq!(
            message_envelope_segments(
                "```json\n{\"messages\":[{\"text\":\"one\"},{\"message\":\"two\"}]}\n```",
            )
            .unwrap(),
            vec!["one", "two"]
        );
    }
}
