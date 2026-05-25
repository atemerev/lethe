use std::collections::BTreeMap;
use std::sync::OnceLock;

use serde::{Deserialize, Serialize};

const EMBEDDED_MODEL_CATALOG: &str = include_str!("../../config/model_catalog.json");
const EMBEDDED_CONTEXT_LIMITS: &str =
    include_str!("../../config/model_context_limits.json");

pub type ModelCatalog = BTreeMap<String, BTreeMap<String, Vec<ModelEntry>>>;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ModelEntry(pub String, pub String, pub String);

impl ModelEntry {
    pub fn name(&self) -> &str {
        &self.0
    }

    pub fn model_id(&self) -> &str {
        &self.1
    }

    pub fn price(&self) -> &str {
        &self.2
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ProviderInfo {
    pub provider: String,
    pub label: String,
    pub auth: String,
}

static MODEL_CATALOG: OnceLock<ModelCatalog> = OnceLock::new();

pub fn model_catalog() -> &'static ModelCatalog {
    MODEL_CATALOG.get_or_init(load_embedded_catalog)
}

pub fn available_providers() -> Vec<ProviderInfo> {
    available_providers_with(|key| std::env::var_os(key).is_some_and(|value| !value.is_empty()))
}

pub fn available_providers_with(mut env_has: impl FnMut(&str) -> bool) -> Vec<ProviderInfo> {
    let catalog = model_catalog();
    provider_auth_options()
        .iter()
        .filter(|(provider, _)| catalog.contains_key(*provider))
        .flat_map(|(provider, auth_options)| {
            auth_options
                .iter()
                .filter(|(env_var, _)| env_has(env_var))
                .map(|(_, auth)| ProviderInfo {
                    provider: (*provider).to_string(),
                    label: provider_label(provider, auth),
                    auth: (*auth).to_string(),
                })
                .collect::<Vec<_>>()
        })
        .collect()
}

/// Per-model context window (tokens), as declared in
/// `config/model_context_limits.json`. Returns `None` for unknown model ids
/// — callers should fall back to a configured env default.
pub fn context_limit_for_model(model_id: &str) -> Option<u64> {
    static CONTEXT_LIMITS: OnceLock<BTreeMap<String, u64>> = OnceLock::new();
    let map = CONTEXT_LIMITS.get_or_init(|| {
        let raw = serde_json::from_str::<serde_json::Value>(EMBEDDED_CONTEXT_LIMITS).ok();
        let Some(serde_json::Value::Object(mut object)) = raw else {
            return BTreeMap::new();
        };
        object.retain(|key, _| !key.starts_with('_'));
        object
            .into_iter()
            .filter_map(|(key, value)| value.as_u64().map(|tokens| (key, tokens)))
            .collect()
    });
    let key = model_id.trim();
    map.get(key).copied()
}

pub fn provider_for_model(model_id: &str) -> Option<&'static str> {
    let model_id = model_id.trim();
    if model_id.is_empty() {
        return None;
    }
    for (provider, groups) in model_catalog() {
        for entries in groups.values() {
            if entries.iter().any(|entry| entry.model_id() == model_id) {
                return Some(provider.as_str());
            }
        }
    }
    provider_for_model_fallback(model_id)
}

fn load_embedded_catalog() -> ModelCatalog {
    let raw = serde_json::from_str::<serde_json::Value>(EMBEDDED_MODEL_CATALOG).ok();
    let Some(serde_json::Value::Object(mut object)) = raw else {
        return ModelCatalog::new();
    };
    object.retain(|key, _| !key.starts_with('_'));
    serde_json::from_value(serde_json::Value::Object(object)).unwrap_or_default()
}

fn provider_for_model_fallback(model_id: &str) -> Option<&'static str> {
    let lower = model_id.to_ascii_lowercase();
    if lower.starts_with("openrouter/") {
        Some("openrouter")
    } else if lower.contains("claude") {
        Some("anthropic")
    } else if lower.contains("gpt") {
        Some("openai")
    } else {
        None
    }
}

fn provider_auth_options() -> &'static [(&'static str, &'static [(&'static str, &'static str)])] {
    &[
        ("openrouter", &[("OPENROUTER_API_KEY", "API")]),
        ("anthropic", &[("ANTHROPIC_API_KEY", "API")]),
        ("openai", &[("OPENAI_API_KEY", "API")]),
    ]
}

fn provider_label(provider: &str, auth: &str) -> String {
    let base = match provider {
        "openrouter" => "OpenRouter",
        "anthropic" => "Anthropic",
        "openai" => "OpenAI",
        _ => provider,
    };
    if provider == "openrouter" {
        return base.to_string();
    }
    let suffix = match auth {
        "API" => "API key",
        _ => auth,
    };
    format!("{base} ({suffix})")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedded_catalog_strips_metadata_and_loads_models() {
        let catalog = model_catalog();

        assert!(!catalog.contains_key("_updated"));
        assert!(catalog.contains_key("openrouter"));
        assert!(
            catalog["openrouter"]["main"]
                .iter()
                .any(|entry| entry.model_id().starts_with("openrouter/"))
        );
    }

    #[test]
    fn provider_lookup_uses_catalog_then_fallbacks() {
        assert_eq!(provider_for_model("claude-haiku-4-5"), Some("anthropic"));
        assert_eq!(
            provider_for_model("openrouter/openai/gpt-5.4-nano"),
            Some("openrouter")
        );
        assert_eq!(provider_for_model("gpt-future"), Some("openai"));
        assert_eq!(provider_for_model("unknown-model"), None);
    }

    #[test]
    fn available_providers_follow_configured_auth_order() {
        let available =
            available_providers_with(|key| matches!(key, "ANTHROPIC_API_KEY" | "OPENAI_API_KEY"));

        assert_eq!(
            available,
            vec![
                ProviderInfo {
                    provider: "anthropic".to_string(),
                    label: "Anthropic (API key)".to_string(),
                    auth: "API".to_string(),
                },
                ProviderInfo {
                    provider: "openai".to_string(),
                    label: "OpenAI (API key)".to_string(),
                    auth: "API".to_string(),
                },
            ]
        );
    }
}
