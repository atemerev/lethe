use serde_json::json;

pub(super) fn tool_error_payload(message: &str) -> String {
    serde_json::to_string_pretty(&json!({
        "success": false,
        "error": message,
    }))
    .unwrap()
}
