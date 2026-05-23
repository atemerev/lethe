use genai::chat::Tool;
use serde_json::{Value, json};

pub(super) fn tool(name: &str, description: &str, schema: Value) -> Tool {
    Tool::new(name)
        .with_description(description)
        .with_schema(schema)
}

pub(super) fn schema<const N: usize, const M: usize>(
    properties: [(&str, Value); N],
    required: [&str; M],
) -> Value {
    let mut props = serde_json::Map::new();
    for (name, schema) in properties {
        props.insert(name.to_string(), schema);
    }
    let required = required.into_iter().collect::<Vec<_>>();
    json!({
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": false,
    })
}

pub(super) fn string_schema(description: &str) -> Value {
    json!({"type": "string", "description": description})
}

pub(super) fn integer_schema(description: &str) -> Value {
    json!({"type": "integer", "description": description})
}

pub(super) fn bool_schema(description: &str) -> Value {
    json!({"type": "boolean", "description": description})
}

pub(super) fn array_string_schema(description: &str) -> Value {
    json!({"type": "array", "items": {"type": "string"}, "description": description})
}

pub(super) fn enum_schema<const N: usize>(description: &str, values: [&str; N]) -> Value {
    let values = values.into_iter().collect::<Vec<_>>();
    json!({"type": "string", "description": description, "enum": values})
}
