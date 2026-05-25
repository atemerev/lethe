use std::env;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub enum RuntimeMode {
    Telegram,
    Api,
    Cli,
}

impl RuntimeMode {
    pub fn parse(raw: impl AsRef<str>) -> Self {
        match raw.as_ref().trim().to_ascii_lowercase().as_str() {
            "telegram" => Self::Telegram,
            "api" => Self::Api,
            _ => Self::Cli,
        }
    }
}

/// All on-disk locations the runtime needs. Centralizes path layout so other
/// modules only depend on the slice they care about.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Paths {
    pub lethe_home: PathBuf,
    pub config_dir: PathBuf,
    pub workspace_dir: PathBuf,
    pub memory_dir: PathBuf,
    pub db_path: PathBuf,
    pub credentials_dir: PathBuf,
    pub cache_dir: PathBuf,
    pub logs_dir: PathBuf,
    pub notes_dir: PathBuf,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct LlmConfig {
    pub openrouter_api_key: String,
    pub openai_api_key: String,
    pub llm_model: String,
    pub llm_model_aux: String,
    pub llm_provider: String,
    pub llm_api_base: String,
    pub llm_context_limit: usize,
}

impl LlmConfig {
    pub fn effective_aux_model(&self) -> &str {
        if self.llm_model_aux.trim().is_empty() {
            &self.llm_model
        } else {
            &self.llm_model_aux
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct TelegramConfig {
    pub bot_token: String,
    pub allowed_user_ids: Vec<i64>,
    pub transcription_enabled: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ApiServerConfig {
    pub token: String,
    pub host: String,
    pub port: u16,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct TranscriptionConfig {
    pub provider: String,
    pub model: String,
    pub language: String,
    pub local_command: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct BackgroundConfig {
    pub actors_enabled: bool,
    pub hippocampus_enabled: bool,
    pub curator_enabled: bool,
    pub heartbeat_enabled: bool,
    pub heartbeat_interval_seconds: u64,
    pub debounce_seconds: f64,
    pub proactive_max_per_day: u32,
    pub proactive_cooldown_minutes: u32,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Settings {
    pub agent_name: String,
    pub mode: RuntimeMode,
    pub paths: Paths,
    pub llm: LlmConfig,
    pub telegram: TelegramConfig,
    pub api: ApiServerConfig,
    pub transcription: TranscriptionConfig,
    pub background: BackgroundConfig,
}

impl Settings {
    pub fn from_env() -> Self {
        let _ = dotenvy::dotenv();

        let lethe_home = env_path("LETHE_HOME").unwrap_or_else(default_lethe_home);
        let _ = dotenvy::from_path(lethe_home.join("config").join(".env"));
        let workspace_dir =
            env_path("WORKSPACE_DIR").unwrap_or_else(|| lethe_home.join("workspace"));
        let data_dir = lethe_home.join("data");
        let memory_dir = env_path("MEMORY_DIR").unwrap_or_else(|| data_dir.join("memory"));

        let paths = Paths {
            config_dir: env_path("LETHE_CONFIG_DIR").unwrap_or_else(|| PathBuf::from("config")),
            workspace_dir: workspace_dir.clone(),
            memory_dir: memory_dir.clone(),
            db_path: env_path("DB_PATH").unwrap_or_else(|| data_dir.join("lethe.db")),
            credentials_dir: env_path("CREDENTIALS_DIR")
                .unwrap_or_else(|| workspace_dir.join("../credentials")),
            cache_dir: env_path("CACHE_DIR").unwrap_or_else(|| workspace_dir.join("../cache")),
            logs_dir: env_path("LOGS_DIR").unwrap_or_else(|| workspace_dir.join("../logs")),
            notes_dir: env_path("NOTES_DIR").unwrap_or_else(|| workspace_dir.join("notes")),
            lethe_home,
        };

        Self {
            agent_name: env_string("LETHE_AGENT_NAME", "lethe"),
            mode: RuntimeMode::parse(env_string("LETHE_MODE", "cli")),
            telegram: TelegramConfig {
                bot_token: env_string("TELEGRAM_BOT_TOKEN", ""),
                allowed_user_ids: env_i64_list("TELEGRAM_ALLOWED_USER_IDS"),
                transcription_enabled: env_bool("TELEGRAM_TRANSCRIPTION_ENABLED", true),
            },
            api: ApiServerConfig {
                token: env_string("LETHE_API_TOKEN", ""),
                host: env_string("LETHE_API_HOST", "127.0.0.1"),
                port: env_u16("LETHE_API_PORT", 8080),
            },
            llm: LlmConfig {
                openrouter_api_key: env_string("OPENROUTER_API_KEY", ""),
                openai_api_key: env_string("OPENAI_API_KEY", ""),
                llm_model: env_string("LLM_MODEL", ""),
                llm_model_aux: env_string("LLM_MODEL_AUX", ""),
                llm_provider: env_string("LLM_PROVIDER", ""),
                llm_api_base: env_string("LLM_API_BASE", ""),
                llm_context_limit: env_usize("LLM_CONTEXT_LIMIT", 100_000),
            },
            transcription: TranscriptionConfig {
                provider: env_string("TRANSCRIPTION_PROVIDER", ""),
                model: env_string("TRANSCRIPTION_MODEL", ""),
                language: env_string("TRANSCRIPTION_LANGUAGE", ""),
                local_command: env_string("TRANSCRIPTION_LOCAL_COMMAND", "whisper"),
            },
            background: BackgroundConfig {
                actors_enabled: env_bool("ACTORS_ENABLED", true),
                hippocampus_enabled: env_bool("HIPPOCAMPUS_ENABLED", true),
                curator_enabled: env_bool("CURATOR_ENABLED", true),
                heartbeat_enabled: env_bool("HEARTBEAT_ENABLED", true),
                heartbeat_interval_seconds: env_u64("HEARTBEAT_INTERVAL", 60 * 60),
                debounce_seconds: env_f64("DEBOUNCE_SECONDS", 5.0),
                proactive_max_per_day: env_u32("PROACTIVE_MAX_PER_DAY", 4),
                proactive_cooldown_minutes: env_u32("PROACTIVE_COOLDOWN_MINUTES", 60),
            },
            paths,
        }
    }

    pub fn effective_aux_model(&self) -> &str {
        self.llm.effective_aux_model()
    }
}

fn default_lethe_home() -> PathBuf {
    env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".lethe")
}

fn env_string(key: &str, default: &str) -> String {
    env::var(key)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn env_path(key: &str) -> Option<PathBuf> {
    env::var_os(key)
        .map(PathBuf::from)
        .filter(|path| !path.as_os_str().is_empty())
}

fn env_bool(key: &str, default: bool) -> bool {
    match env_string(key, "").to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => true,
        "0" | "false" | "no" | "off" => false,
        _ => default,
    }
}

fn env_usize(key: &str, default: usize) -> usize {
    env_string(key, "")
        .parse::<usize>()
        .ok()
        .filter(|value| *value > 0)
        .unwrap_or(default)
}

fn env_u64(key: &str, default: u64) -> u64 {
    env_string(key, "")
        .parse::<u64>()
        .ok()
        .filter(|value| *value > 0)
        .unwrap_or(default)
}

fn env_u32(key: &str, default: u32) -> u32 {
    env_string(key, "").parse::<u32>().ok().unwrap_or(default)
}

fn env_u16(key: &str, default: u16) -> u16 {
    env_string(key, "").parse::<u16>().ok().unwrap_or(default)
}

fn env_f64(key: &str, default: f64) -> f64 {
    env_string(key, "")
        .parse::<f64>()
        .ok()
        .filter(|value| value.is_finite() && *value >= 0.0)
        .unwrap_or(default)
}

fn env_i64_list(key: &str) -> Vec<i64> {
    env_string(key, "")
        .split(',')
        .filter_map(|part| part.trim().parse::<i64>().ok())
        .collect()
}

/// Minimal Settings instance for tests and examples. Always available so
/// integration tests in the binary crate can use it (cfg(test)-gated items
/// in the lib are invisible to the binary).
pub fn test_settings(root: &std::path::Path) -> Settings {
    Settings {
        agent_name: "lethe".to_string(),
        mode: RuntimeMode::Cli,
        paths: Paths {
            lethe_home: root.to_path_buf(),
            config_dir: root.join("config"),
            workspace_dir: root.join("workspace"),
            memory_dir: root.join("data/memory"),
            db_path: root.join("data/lethe.db"),
            credentials_dir: root.join("credentials"),
            cache_dir: root.join("cache"),
            logs_dir: root.join("logs"),
            notes_dir: root.join("workspace/notes"),
        },
        llm: LlmConfig {
            openrouter_api_key: String::new(),
            openai_api_key: String::new(),
            llm_model: "test-model".to_string(),
            llm_model_aux: String::new(),
            llm_provider: String::new(),
            llm_api_base: String::new(),
            llm_context_limit: 100_000,
        },
        telegram: TelegramConfig {
            bot_token: String::new(),
            allowed_user_ids: vec![],
            transcription_enabled: true,
        },
        api: ApiServerConfig {
            token: String::new(),
            host: "127.0.0.1".to_string(),
            port: 8080,
        },
        transcription: TranscriptionConfig {
            provider: String::new(),
            model: String::new(),
            language: String::new(),
            local_command: "whisper".to_string(),
        },
        background: BackgroundConfig {
            actors_enabled: true,
            hippocampus_enabled: true,
            curator_enabled: true,
            heartbeat_enabled: true,
            heartbeat_interval_seconds: 3600,
            debounce_seconds: 5.0,
            proactive_max_per_day: 4,
            proactive_cooldown_minutes: 60,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runtime_mode_defaults_to_cli_for_unknown_values() {
        assert_eq!(RuntimeMode::parse("api"), RuntimeMode::Api);
        assert_eq!(RuntimeMode::parse("telegram"), RuntimeMode::Telegram);
        assert_eq!(RuntimeMode::parse(""), RuntimeMode::Cli);
        assert_eq!(RuntimeMode::parse("weird"), RuntimeMode::Cli);
    }

    #[test]
    fn effective_aux_model_falls_back_to_main() {
        let mut settings = test_settings(std::path::Path::new("/tmp/lethe"));
        settings.llm.llm_model = "gpt-5".to_string();
        assert_eq!(settings.effective_aux_model(), "gpt-5");
        settings.llm.llm_model_aux = "gpt-5-mini".to_string();
        assert_eq!(settings.effective_aux_model(), "gpt-5-mini");
    }
}
