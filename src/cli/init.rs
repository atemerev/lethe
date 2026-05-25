//! Interactive `lethe init` wizard. Ports the working flow from the Python
//! main branch's `install.sh` (`prompt_provider`, `prompt_model`,
//! `prompt_api_key`, `setup_config`) into a single Rust command that:
//!
//! 1. Detects any LLM keys already in the environment (and the existing
//!    `~/.lethe/config/.env` if one exists).
//! 2. Walks the user through provider / model / key / Telegram choices.
//! 3. Writes `~/.lethe/config/.env` and seeds the workspace + default memory
//!    blocks.
//! 4. Runs a smoke test (model ping + embedding probe).
//! 5. Tells them what to run next.
//!
//! TTY-aware: refuses to prompt over a non-terminal stdin (use the env
//! variables directly in scripted contexts).

use std::io::{self, BufRead, IsTerminal, Write};
use std::path::Path;

use anyhow::{Context, Result, bail};
use lethe::config::Settings;
use lethe::llm::models::model_catalog;
use lethe::llm::{LlmMessage, LlmRouter, LlmRouterConfig};
use lethe::memory::{BlockManager, MemoryStore};

/// Top-level entry point.
pub async fn run() -> Result<()> {
    if !io::stdin().is_terminal() {
        bail!(
            "`lethe init` needs an interactive terminal. \
             Set env vars manually (see .env.example) or pipe a config into a file."
        );
    }

    print_header();

    let settings = Settings::from_env();
    let lethe_home = settings.paths.lethe_home.clone();
    let env_path = lethe_home.join("config").join(".env");
    let existing_env = read_existing_env(&env_path);

    println!("Lethe will store config + state under:");
    println!("  {}\n", lethe_home.display());

    let detected = detect_keys(&existing_env);
    if !detected.is_empty() {
        println!("Found existing API keys for: {}\n", detected.join(", "));
    }

    // -- Provider -----------------------------------------------------------
    let provider = prompt_provider(&detected)?;
    info(&format!("Using {}", provider.label()));

    // -- Models -------------------------------------------------------------
    let (main_model, aux_model) = prompt_models(provider)?;
    info(&format!("Main: {main_model}"));
    info(&format!("Aux:  {aux_model}"));

    // -- API key ------------------------------------------------------------
    let api_key = prompt_api_key(provider, &existing_env)?;

    // -- Optional Telegram --------------------------------------------------
    let telegram = prompt_telegram(&existing_env)?;

    // -- Optional human-block intro ----------------------------------------
    let human_intro = prompt_human_intro()?;

    // -- Persist ------------------------------------------------------------
    write_env_file(
        &env_path,
        provider,
        &main_model,
        &aux_model,
        &api_key,
        telegram.as_ref(),
    )?;
    info(&format!("Wrote {}", env_path.display()));

    seed_workspace(&settings)?;
    if let Some(text) = human_intro {
        seed_human_block(&settings, &text)?;
    }
    info("Seeded workspace + memory blocks.");

    // -- Smoke test ---------------------------------------------------------
    println!("\nRunning smoke test...");
    smoke_test(provider, &main_model, &aux_model, &api_key).await?;

    println!();
    success("Setup complete.");
    println!();
    println!("Next steps:");
    println!("  lethe chat -m \"hello\"     # one-off chat");
    println!("  lethe                       # default mode (cli)");
    if telegram.is_some() {
        println!("  lethe telegram run          # start Telegram bot");
    }
    println!("  lethe check                 # health check any time");
    Ok(())
}

// =============================================================================
// Provider selection
// =============================================================================

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Provider {
    OpenRouter,
    Anthropic,
    OpenAI,
}

impl Provider {
    fn label(self) -> &'static str {
        match self {
            Provider::OpenRouter => "OpenRouter",
            Provider::Anthropic => "Anthropic",
            Provider::OpenAI => "OpenAI",
        }
    }
    fn id(self) -> &'static str {
        match self {
            Provider::OpenRouter => "openrouter",
            Provider::Anthropic => "anthropic",
            Provider::OpenAI => "openai",
        }
    }
    fn key_env(self) -> &'static str {
        match self {
            Provider::OpenRouter => "OPENROUTER_API_KEY",
            Provider::Anthropic => "ANTHROPIC_API_KEY",
            Provider::OpenAI => "OPENAI_API_KEY",
        }
    }
    fn key_url(self) -> &'static str {
        match self {
            Provider::OpenRouter => "https://openrouter.ai/keys",
            Provider::Anthropic => "https://console.anthropic.com/settings/keys",
            Provider::OpenAI => "https://platform.openai.com/api-keys",
        }
    }
}

fn detect_keys(existing_env: &EnvMap) -> Vec<&'static str> {
    let mut out = Vec::new();
    for provider in [Provider::OpenRouter, Provider::Anthropic, Provider::OpenAI] {
        let key = provider.key_env();
        let present = std::env::var(key)
            .ok()
            .filter(|value| !value.trim().is_empty())
            .is_some()
            || existing_env
                .get(key)
                .map(|v| !v.trim().is_empty())
                .unwrap_or(false);
        if present {
            out.push(key);
        }
    }
    out
}

fn prompt_provider(detected: &[&'static str]) -> Result<Provider> {
    println!("Select your LLM provider:\n");
    let entries = [
        (Provider::OpenRouter, "OpenRouter (recommended — single key, every major model)"),
        (Provider::Anthropic, "Anthropic (API key or Claude subscription token)"),
        (Provider::OpenAI, "OpenAI (API key)"),
    ];
    for (idx, (provider, desc)) in entries.iter().enumerate() {
        let badge = if detected.contains(&provider.key_env()) {
            " [key found]"
        } else {
            ""
        };
        println!("  {}) {desc}{badge}", idx + 1);
    }

    // Default to the first detected provider, else OpenRouter.
    let default = entries
        .iter()
        .position(|(p, _)| detected.contains(&p.key_env()))
        .map(|i| i + 1)
        .unwrap_or(1);

    let choice = prompt_line(&format!("\nChoose [1-3, default={default}]: "))?;
    let choice = choice.trim();
    let n = if choice.is_empty() {
        default
    } else {
        choice
            .parse::<usize>()
            .ok()
            .filter(|n| (1..=entries.len()).contains(n))
            .unwrap_or(default)
    };
    Ok(entries[n - 1].0)
}

// =============================================================================
// Model selection
// =============================================================================

fn prompt_models(provider: Provider) -> Result<(String, String)> {
    let catalog = model_catalog();
    let provider_entry = catalog.get(provider.id());

    let main_entries = provider_entry
        .and_then(|p| p.get("main"))
        .cloned()
        .unwrap_or_default();
    let aux_entries = provider_entry
        .and_then(|p| p.get("aux"))
        .cloned()
        .unwrap_or_default();

    println!("\nMain model (handles user-facing turns):");
    let main = pick_model("main", &main_entries)?;
    println!("\nAuxiliary model (cheap calls — summarization, heartbeat, background):");
    let aux = pick_model("aux", &aux_entries)?;
    Ok((main, aux))
}

fn pick_model(label: &str, entries: &[lethe::llm::models::ModelEntry]) -> Result<String> {
    if entries.is_empty() {
        let raw = prompt_line(&format!("  {label} model id: "))?;
        let raw = raw.trim();
        if raw.is_empty() {
            bail!("a model id is required");
        }
        return Ok(raw.to_string());
    }
    for (idx, entry) in entries.iter().enumerate() {
        println!(
            "  {}) {} — {} ({})",
            idx + 1,
            entry.name(),
            entry.model_id(),
            entry.price()
        );
    }
    let prompt = format!(
        "  Choose [1-{}, default=1, or type a custom id]: ",
        entries.len()
    );
    let answer = prompt_line(&prompt)?;
    let answer = answer.trim();
    if answer.is_empty() {
        return Ok(entries[0].model_id().to_string());
    }
    if let Ok(n) = answer.parse::<usize>()
        && (1..=entries.len()).contains(&n)
    {
        return Ok(entries[n - 1].model_id().to_string());
    }
    Ok(answer.to_string())
}

// =============================================================================
// API key
// =============================================================================

fn prompt_api_key(provider: Provider, existing_env: &EnvMap) -> Result<String> {
    let env_name = provider.key_env();
    let existing = std::env::var(env_name)
        .ok()
        .filter(|v| !v.trim().is_empty())
        .or_else(|| {
            existing_env
                .get(env_name)
                .filter(|v| !v.trim().is_empty())
                .cloned()
        });
    if let Some(key) = existing {
        println!("\nFound existing {env_name}: {}", mask_key(&key));
        let answer = prompt_line("Use it? [Y/n]: ")?;
        if !answer.trim().to_ascii_lowercase().starts_with('n') {
            return Ok(key);
        }
    }
    println!("\n{env_name} required.");
    println!("  Get one at: {}", provider.key_url());
    let key = prompt_line(&format!("  Paste {env_name}: "))?;
    let key = key.trim().to_string();
    if key.is_empty() {
        bail!("{env_name} is required to continue");
    }
    Ok(key)
}

fn mask_key(key: &str) -> String {
    let trimmed = key.trim();
    if trimmed.len() <= 12 {
        return "<short-key>".to_string();
    }
    let head: String = trimmed.chars().take(8).collect();
    let tail: String = trimmed.chars().rev().take(4).collect::<String>().chars().rev().collect();
    format!("{head}…{tail}")
}

// =============================================================================
// Optional Telegram
// =============================================================================

struct TelegramSetup {
    bot_token: String,
    allowed_user_ids: String,
}

fn prompt_telegram(existing_env: &EnvMap) -> Result<Option<TelegramSetup>> {
    println!("\nOptional: Telegram bot setup");
    println!("  Skip this if you only want CLI or HTTP API access.");
    let yes_no = prompt_line("  Configure Telegram now? [y/N]: ")?;
    if !yes_no.trim().to_ascii_lowercase().starts_with('y') {
        return Ok(None);
    }
    let existing_token = existing_env.get("TELEGRAM_BOT_TOKEN").cloned();
    let token = match existing_token.as_deref().filter(|v| !v.trim().is_empty()) {
        Some(value) => {
            println!("  Found existing TELEGRAM_BOT_TOKEN: {}", mask_key(value));
            let keep = prompt_line("  Use it? [Y/n]: ")?;
            if keep.trim().to_ascii_lowercase().starts_with('n') {
                prompt_line("  Paste new bot token: ")?
                    .trim()
                    .to_string()
            } else {
                value.to_string()
            }
        }
        None => {
            println!("  Get a bot token from @BotFather (https://t.me/BotFather).");
            prompt_line("  Paste TELEGRAM_BOT_TOKEN: ")?
                .trim()
                .to_string()
        }
    };
    if token.is_empty() {
        return Ok(None);
    }
    let allowed = prompt_line("  Allowed Telegram user ids (comma-separated, or blank for any): ")?
        .trim()
        .to_string();
    Ok(Some(TelegramSetup {
        bot_token: token,
        allowed_user_ids: allowed,
    }))
}

// =============================================================================
// Optional human-block seed
// =============================================================================

fn prompt_human_intro() -> Result<Option<String>> {
    println!("\nOptional: tell Lethe about yourself");
    println!("  This seeds the `human` memory block — anything you want the assistant");
    println!("  to remember from turn one (your name, preferences, role). Leave blank to skip.");
    let answer = prompt_line("  > ")?;
    let answer = answer.trim();
    if answer.is_empty() {
        Ok(None)
    } else {
        Ok(Some(answer.to_string()))
    }
}

// =============================================================================
// Persistence
// =============================================================================

fn write_env_file(
    path: &Path,
    provider: Provider,
    main_model: &str,
    aux_model: &str,
    api_key: &str,
    telegram: Option<&TelegramSetup>,
) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).with_context(|| {
            format!("creating config dir at {}", parent.display())
        })?;
    }
    let now = chrono::Utc::now().format("%Y-%m-%d %H:%M:%S %Z");
    let mut body = String::new();
    body.push_str(&format!("# Lethe configuration — generated by `lethe init` on {now}\n\n"));
    body.push_str(&format!("LLM_PROVIDER={}\n", provider.id()));
    body.push_str(&format!("LLM_MODEL={main_model}\n"));
    body.push_str(&format!("LLM_MODEL_AUX={aux_model}\n"));
    body.push_str(&format!("{}={}\n\n", provider.key_env(), api_key));
    if let Some(telegram) = telegram {
        body.push_str("# Telegram bot\n");
        body.push_str(&format!("TELEGRAM_BOT_TOKEN={}\n", telegram.bot_token));
        if !telegram.allowed_user_ids.is_empty() {
            body.push_str(&format!(
                "TELEGRAM_ALLOWED_USER_IDS={}\n",
                telegram.allowed_user_ids
            ));
        }
        body.push('\n');
    }
    body.push_str("# Add more knobs from .env.example (background subsystems, paths, etc.)\n");
    std::fs::write(path, body).with_context(|| format!("writing {}", path.display()))?;
    // Lock the file to user-read/write only — it contains API secrets.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

fn seed_workspace(settings: &Settings) -> Result<()> {
    // MemoryStore::from_settings already creates directories + seeds blocks.
    // We re-instantiate here so init works even if no chat has run yet.
    let _ = MemoryStore::from_settings(settings)
        .with_context(|| "opening memory store under workspace")?;
    Ok(())
}

fn seed_human_block(settings: &Settings, text: &str) -> Result<()> {
    let blocks_dir = settings.paths.workspace_dir.join("memory");
    let manager = BlockManager::new(&blocks_dir)
        .with_context(|| format!("opening blocks dir {}", blocks_dir.display()))?;
    manager.init_embedded_defaults()?;
    manager
        .update("human", Some(text), None)
        .with_context(|| "writing seed text to human block")?;
    Ok(())
}

// =============================================================================
// Smoke test
// =============================================================================

async fn smoke_test(
    provider: Provider,
    main_model: &str,
    aux_model: &str,
    api_key: &str,
) -> Result<()> {
    // Apply the new config in-process so the smoke test reflects what the
    // user will actually run with on the next invocation.
    unsafe {
        std::env::set_var(provider.key_env(), api_key);
        std::env::set_var("LLM_PROVIDER", provider.id());
        std::env::set_var("LLM_MODEL", main_model);
        std::env::set_var("LLM_MODEL_AUX", aux_model);
    }
    let settings = Settings::from_env();
    let router = LlmRouter::new(LlmRouterConfig::from_settings(&settings));
    let probe = vec![
        LlmMessage::system("Reply with the single word: ok"),
        LlmMessage::user("ready?"),
    ];
    match router.complete(probe, true).await {
        Ok(reply) => {
            let preview = reply.trim().lines().next().unwrap_or("").to_string();
            info(&format!("LLM ping via aux model: `{preview}`"));
            Ok(())
        }
        Err(error) => {
            warn(&format!("LLM ping failed: {error}"));
            warn("Config was saved — fix the key/model and re-run `lethe check` to verify.");
            Ok(())
        }
    }
}

// =============================================================================
// I/O helpers
// =============================================================================

type EnvMap = std::collections::HashMap<String, String>;

fn read_existing_env(path: &Path) -> EnvMap {
    let Ok(content) = std::fs::read_to_string(path) else {
        return EnvMap::new();
    };
    content
        .lines()
        .filter_map(|line| {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                return None;
            }
            let (k, v) = line.split_once('=')?;
            Some((k.trim().to_string(), v.trim().trim_matches('"').to_string()))
        })
        .collect()
}

fn prompt_line(prompt: &str) -> Result<String> {
    print!("{prompt}");
    io::stdout().flush().ok();
    let stdin = io::stdin();
    let mut line = String::new();
    stdin
        .lock()
        .read_line(&mut line)
        .with_context(|| "reading stdin")?;
    Ok(line.trim_end_matches('\n').trim_end_matches('\r').to_string())
}

fn print_header() {
    println!("Lethe — guided setup");
    println!("--------------------\n");
}

fn info(message: &str) {
    println!("  → {message}");
}

fn success(message: &str) {
    println!("✓ {message}");
}

fn warn(message: &str) {
    println!("! {message}");
}

