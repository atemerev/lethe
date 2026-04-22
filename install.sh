#!/usr/bin/env bash
#
# Lethe Installer
# Usage: curl -fsSL https://lethe.gg/install | bash
#
# Native install with OS-level write sandbox (Landlock on Linux, Seatbelt on macOS).
# The agent can read the entire filesystem but only write to ~/.lethe/ and /tmp.
# Disable sandbox with LETHE_NO_SANDBOX=1 if needed.
#
# Supports multiple LLM providers: OpenRouter, Anthropic, OpenAI
#

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Config
REPO_URL="https://github.com/atemerev/lethe.git"
REPO_OWNER="atemerev"
REPO_NAME="lethe"
LETHE_HOME="${LETHE_HOME:-$HOME/.lethe}"
CONFIG_DIR="$LETHE_HOME/config"
DETECTED_PROVIDERS=()
DEFERRED_OPENAI_OAUTH_LOGIN=0
LOCAL_CHECKOUT=0

# Detect if running from an existing lethe checkout
_detect_install_dir() {
    if [ -n "${LETHE_INSTALL_DIR:-}" ]; then
        INSTALL_DIR="$LETHE_INSTALL_DIR"
        return
    fi
    local script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
    if [ -f "$script_dir/pyproject.toml" ] && grep -q 'name = "lethe"' "$script_dir/pyproject.toml" 2>/dev/null; then
        INSTALL_DIR="$script_dir"
        LOCAL_CHECKOUT=1
    else
        INSTALL_DIR="$LETHE_HOME/install"
    fi
}
_detect_install_dir

# Provider defaults
# NOTE: claude-max disabled - Anthropic blocked third-party OAuth in Jan 2026
provider_desc() {
    case "$1" in
        openrouter) echo "OpenRouter (recommended - access to all models)" ;;
        anthropic) echo "Anthropic (API key or Claude subscription token)" ;;
        openai) echo "OpenAI (API key or ChatGPT subscription OAuth for Codex)" ;;
        *) echo "" ;;
    esac
}

provider_key_env() {
    case "$1" in
        openrouter) echo "OPENROUTER_API_KEY" ;;
        anthropic) echo "ANTHROPIC_API_KEY" ;;
        openai) echo "OPENAI_API_KEY" ;;
        *) echo "" ;;
    esac
}

provider_model_default() {
    case "$1" in
        openrouter) echo "openrouter/moonshotai/kimi-k2.6" ;;
        anthropic) echo "claude-opus-4-6" ;;
        openai) echo "gpt-5.4" ;;
        *) echo "" ;;
    esac
}

provider_model_aux_default() {
    case "$1" in
        openrouter) echo "openrouter/google/gemini-3-flash-preview" ;;
        anthropic) echo "claude-haiku-4-5" ;;
        openai) echo "gpt-5.4-nano" ;;
        *) echo "" ;;
    esac
}

provider_key_url() {
    case "$1" in
        openrouter) echo "https://openrouter.ai/keys" ;;
        anthropic) echo "https://console.anthropic.com/settings/keys" ;;
        openai) echo "https://platform.openai.com/api-keys" ;;
        *) echo "" ;;
    esac
}

print_header() {
    echo -e "${BLUE}"
    echo "╔═══════════════════════════════════════════════════════════╗"
    echo "║                                                           ║"
    echo "║   █░░ █▀▀ ▀█▀ █░█ █▀▀                                     ║"
    echo "║   █▄▄ ██▄ ░█░ █▀█ ██▄                                     ║"
    echo "║                                                           ║"
    echo "║   Autonomous Executive Assistant                           ║"
    echo "║                                                           ║"
    echo "╚═══════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

prompt_read() {
    local prompt="$1"
    local var_name="$2"
    local value
    printf "%s" "$prompt" > /dev/tty
    IFS= read -r value < /dev/tty
    eval "$var_name=\$value"
}

detect_os() {
    case "$(uname -s)" in
        Linux*)
            if grep -qi microsoft /proc/version 2>/dev/null; then
                echo "wsl"
            else
                echo "linux"
            fi
            ;;
        Darwin*) echo "mac" ;;
        *) echo "unknown" ;;
    esac
}

check_command() { command -v "$1" >/dev/null 2>&1; }

get_env_value() {
    local key="$1"
    local file="$2"
    [ -f "$file" ] || return 1
    grep -E "^${key}=" "$file" 2>/dev/null | head -n1 | cut -d= -f2-
}

maybe_sudo() {
    if [[ "$(id -u)" -eq 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

has_detected_provider() {
    local needle="$1"
    local p
    for p in "${DETECTED_PROVIDERS[@]-}"; do
        if [ "$p" = "$needle" ]; then
            return 0
        fi
    done
    return 1
}

# Detect existing API keys
detect_api_keys() {
    DETECTED_PROVIDERS=()

    if [ -n "${OPENROUTER_API_KEY:-}" ]; then
        DETECTED_PROVIDERS+=("openrouter")
    fi
    if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        DETECTED_PROVIDERS+=("anthropic")
    fi
    if [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ] && ! has_detected_provider "anthropic"; then
        DETECTED_PROVIDERS+=("anthropic")
    fi
    if [ -n "${OPENAI_API_KEY:-}" ]; then
        DETECTED_PROVIDERS+=("openai")
    fi
    if [ -n "${OPENAI_AUTH_TOKEN:-}" ] && ! has_detected_provider "openai"; then
        DETECTED_PROVIDERS+=("openai")
    fi

    # Also check .env file if it exists (parse only, never source)
    if [ -f "$CONFIG_DIR/.env" ]; then
        local cfg_or_key
        local cfg_an_key
        local cfg_oa_key
        local cfg_oa_auth
        cfg_or_key="$(get_env_value "OPENROUTER_API_KEY" "$CONFIG_DIR/.env" || true)"
        cfg_an_key="$(get_env_value "ANTHROPIC_API_KEY" "$CONFIG_DIR/.env" || true)"
        cfg_an_auth="$(get_env_value "ANTHROPIC_AUTH_TOKEN" "$CONFIG_DIR/.env" || true)"
        cfg_oa_key="$(get_env_value "OPENAI_API_KEY" "$CONFIG_DIR/.env" || true)"
        cfg_oa_auth="$(get_env_value "OPENAI_AUTH_TOKEN" "$CONFIG_DIR/.env" || true)"
        if [ -n "${cfg_or_key:-}" ] && ! has_detected_provider "openrouter"; then
            DETECTED_PROVIDERS+=("openrouter")
        fi
        if [ -n "${cfg_an_key:-}" ] && ! has_detected_provider "anthropic"; then
            DETECTED_PROVIDERS+=("anthropic")
        fi
        if [ -n "${cfg_an_auth:-}" ] && ! has_detected_provider "anthropic"; then
            DETECTED_PROVIDERS+=("anthropic")
        fi
        if [ -n "${cfg_oa_key:-}" ] && ! has_detected_provider "openai"; then
            DETECTED_PROVIDERS+=("openai")
        fi
        if [ -n "${cfg_oa_auth:-}" ] && ! has_detected_provider "openai"; then
            DETECTED_PROVIDERS+=("openai")
        fi
    fi
}

prompt_provider() {
    echo ""
    echo -e "${YELLOW}Select your LLM provider:${NC}"
    echo ""

    # Show detected keys
    if [ ${#DETECTED_PROVIDERS[@]} -gt 0 ]; then
        echo -e "${GREEN}Detected API keys for: ${DETECTED_PROVIDERS[*]}${NC}"
        echo ""
    fi

    local i=1
    for provider in openrouter anthropic openai; do
        local desc
        desc="$(provider_desc "$provider")"
        local detected=""
        if has_detected_provider "$provider"; then
            detected="${GREEN}[key found]${NC}"
        fi
        echo -e "  $i) $desc $detected"
        ((i++))
    done
    echo ""

    local default_choice=1
    if has_detected_provider "anthropic"; then
        default_choice=2
    elif has_detected_provider "openai"; then
        default_choice=3
    fi

    prompt_read "Choose provider [1-3, default=$default_choice]: " choice
    choice=${choice:-$default_choice}

    case $choice in
        1) SELECTED_PROVIDER="openrouter" ;;
        2) SELECTED_PROVIDER="anthropic" ;;
        3) SELECTED_PROVIDER="openai" ;;
        *) SELECTED_PROVIDER="openrouter" ;;
    esac

    success "Selected: $SELECTED_PROVIDER"
}

prompt_model() {
    local default_model
    local default_aux
    default_model="$(provider_model_default "$SELECTED_PROVIDER")"
    default_aux="$(provider_model_aux_default "$SELECTED_PROVIDER")"

    echo ""
    echo -e "${BLUE}Main model${NC} (for conversations):"
    echo -e "  Default: ${CYAN}$default_model${NC}"
    prompt_read "  Press Enter for default, or enter custom: " custom_model

    if [ -n "$custom_model" ]; then
        SELECTED_MODEL="$custom_model"
    else
        SELECTED_MODEL="$default_model"
    fi

    echo ""
    echo -e "${BLUE}Auxiliary model${NC} (for heartbeats, summarization - cheaper/faster):"
    echo -e "  Default: ${CYAN}$default_aux${NC}"
    prompt_read "  Press Enter for default, or enter custom: " custom_aux

    if [ -n "$custom_aux" ]; then
        SELECTED_MODEL_AUX="$custom_aux"
    else
        SELECTED_MODEL_AUX="$default_aux"
    fi

    success "Main model: $SELECTED_MODEL"
    success "Aux model: $SELECTED_MODEL_AUX"

    # Optional: Custom API base URL
    echo ""
    echo -e "${BLUE}Custom API URL${NC} (for local/compatible providers like Ollama, vLLM, etc.):"
    echo -e "  Leave empty to use default provider URL"
    prompt_read "  Custom API base URL (or Enter to skip): " custom_api_base

    if [ -n "$custom_api_base" ]; then
        SELECTED_API_BASE="$custom_api_base"
        success "API base: $SELECTED_API_BASE"
    else
        SELECTED_API_BASE=""
    fi
}

prompt_api_key() {
    local key_name
    local key_url
    key_name="$(provider_key_env "$SELECTED_PROVIDER")"
    key_url="$(provider_key_url "$SELECTED_PROVIDER")"

    if [[ "$SELECTED_PROVIDER" == "anthropic" ]]; then
        echo ""
        echo -e "${YELLOW}Anthropic auth mode:${NC}"
        echo "  1) API key (pay-per-token)"
        echo "  2) Claude subscription token (Claude Code)"
        echo ""
        prompt_read "Choose [1-2, default=2]: " auth_choice
        auth_choice=${auth_choice:-2}
        if [[ "$auth_choice" == "1" ]]; then
            key_name="ANTHROPIC_API_KEY"
            key_url="https://console.anthropic.com/settings/keys"
        else
            key_name="ANTHROPIC_AUTH_TOKEN"
            key_url="Run: claude setup-token"
        fi
    elif [[ "$SELECTED_PROVIDER" == "openai" ]]; then
        echo ""
        echo -e "${YELLOW}OpenAI auth mode:${NC}"
        echo "  1) API key (pay-per-token)"
        echo "  2) ChatGPT subscription OAuth (Codex)"
        echo ""
        prompt_read "Choose [1-2, default=2]: " auth_choice
        auth_choice=${auth_choice:-2}
        if [[ "$auth_choice" == "1" ]]; then
            key_name="OPENAI_API_KEY"
            key_url="https://platform.openai.com/api-keys"
        else
            key_name="OPENAI_AUTH_TOKEN"
            key_url="Run: uv run lethe oauth-login openai"
        fi
    fi

    # Check if already have key in environment or existing config
    local existing_key=""
    local key_source=""

    # First check environment
    case $key_name in
        OPENROUTER_API_KEY) existing_key="${OPENROUTER_API_KEY:-}" ;;
        ANTHROPIC_API_KEY) existing_key="${ANTHROPIC_API_KEY:-}" ;;
        ANTHROPIC_AUTH_TOKEN) existing_key="${ANTHROPIC_AUTH_TOKEN:-}" ;;
        OPENAI_API_KEY) existing_key="${OPENAI_API_KEY:-}" ;;
        OPENAI_AUTH_TOKEN) existing_key="${OPENAI_AUTH_TOKEN:-}" ;;
    esac
    [ -n "$existing_key" ] && key_source="environment"

    # Then check existing config
    if [ -z "$existing_key" ] && [ -f "$CONFIG_DIR/.env" ]; then
        existing_key="$(grep "^$key_name=" "$CONFIG_DIR/.env" 2>/dev/null | cut -d= -f2- || true)"
        [ -n "$existing_key" ] && key_source="$CONFIG_DIR/.env"
    fi

    if [ -n "$existing_key" ]; then
        local masked_key="${existing_key:0:12}...${existing_key: -4}"
        echo ""
        echo -e "${GREEN}Found existing $key_name${NC}"
        echo "   Source: $key_source"
        echo "   Key: $masked_key"
        echo ""
        echo "  1) Use existing key"
        echo "  2) Enter a new key"
        echo ""
        prompt_read "Choose [1-2, default=1]: " choice
        choice=${choice:-1}

        if [[ "$choice" == "1" ]]; then
            API_KEY="$existing_key"
            SELECTED_AUTH_KEY_NAME="$key_name"
            return
        fi
    fi

    echo ""
    echo -e "${BLUE}$key_name required${NC}"
    if [[ "$key_name" == "ANTHROPIC_AUTH_TOKEN" ]]; then
        echo "   Step 1: Run 'claude setup-token' in another terminal and complete login."
        if check_command claude; then
            prompt_read "   Run 'claude setup-token' now? [Y/n]: " run_setup
            run_setup=${run_setup:-Y}
            if [[ "$run_setup" =~ ^[Yy] ]]; then
                claude setup-token || warn "claude setup-token did not complete. You can run it manually and continue."
            fi
        else
            echo "   Claude CLI not found. Install Claude Code CLI, run 'claude setup-token', then paste token below."
        fi
        echo "   Step 2: Paste your ANTHROPIC_AUTH_TOKEN."
    elif [[ "$key_name" == "OPENAI_AUTH_TOKEN" ]]; then
        echo "   Recommended: run OpenAI OAuth after install:"
        echo "     cd $INSTALL_DIR && uv run lethe oauth-login openai"
        echo "   You can paste OPENAI_AUTH_TOKEN now, or leave blank to configure after install."
        echo ""
        prompt_read "   OPENAI_AUTH_TOKEN (optional now): " API_KEY
        if [ -z "$API_KEY" ]; then
            DEFERRED_OPENAI_OAUTH_LOGIN=1
            SELECTED_AUTH_KEY_NAME="$key_name"
            return
        fi
        SELECTED_AUTH_KEY_NAME="$key_name"
        return
    else
        echo "   Get your key at: $key_url"
    fi
    echo ""
    prompt_read "   $key_name: " API_KEY
    if [ -z "$API_KEY" ]; then
        error "$key_name is required"
    fi
    SELECTED_AUTH_KEY_NAME="$key_name"
}

prompt_telegram() {
    echo ""
    echo -e "${YELLOW}Telegram Configuration:${NC}"
    echo ""

    # Check for existing Telegram config
    local existing_token=""
    local existing_user_id=""
    local config_source=""

    if [ -f "$CONFIG_DIR/.env" ]; then
        existing_token="$(grep "^TELEGRAM_BOT_TOKEN=" "$CONFIG_DIR/.env" 2>/dev/null | cut -d= -f2- || true)"
        existing_user_id="$(grep "^TELEGRAM_ALLOWED_USER_IDS=" "$CONFIG_DIR/.env" 2>/dev/null | cut -d= -f2- || true)"
        [ -n "$existing_token" ] && config_source="$CONFIG_DIR/.env"
    fi

    # If existing config found, offer to reuse
    if [ -n "$existing_token" ] && [ -n "$existing_user_id" ]; then
        echo -e "${GREEN}Found existing Telegram configuration in $config_source${NC}"
        echo "   Bot Token: ${existing_token:0:10}...${existing_token: -5}"
        echo "   User ID: $existing_user_id"
        echo ""
        prompt_read "   Use existing configuration? [Y/n]: " reuse_config
        reuse_config=${reuse_config:-Y}

        if [[ "$reuse_config" =~ ^[Yy] ]]; then
            TELEGRAM_TOKEN="$existing_token"
            TELEGRAM_USER_ID="$existing_user_id"
            success "Using existing Telegram configuration"
            return
        fi
        echo ""
    fi

    echo -e "${BLUE}1. Telegram Bot Token${NC}"
    echo "   Create a bot: message @BotFather on Telegram -> /newbot -> copy the token"
    echo ""
    prompt_read "   Telegram Bot Token: " TELEGRAM_TOKEN
    if [ -z "$TELEGRAM_TOKEN" ]; then
        error "Telegram token is required"
    fi
    echo ""

    echo -e "${BLUE}2. Your Telegram User ID${NC}"
    echo "   Message @userinfobot on Telegram - it replies with your ID (a number like 123456789)"
    echo "   This restricts the bot to only respond to you."
    echo ""
    prompt_read "   Telegram User ID: " TELEGRAM_USER_ID
    if [ -z "$TELEGRAM_USER_ID" ]; then
        error "Telegram User ID is required (security: prevents strangers from using your bot)"
    fi
}

install_dependencies() {
    local OS=$(detect_os)
    info "Detected OS: $OS"

    # Install curl if missing
    if ! check_command curl; then
        info "Installing curl..."
        if [[ "$OS" == "mac" ]]; then
            if ! check_command brew; then
                error "Homebrew not found. Install Homebrew first: https://brew.sh"
            fi
            brew install curl
        elif check_command apt-get; then
            maybe_sudo apt-get update -y && maybe_sudo apt-get install -y curl
        elif check_command dnf; then
            maybe_sudo dnf install -y curl
        fi
    fi
    success "curl found"

    # Install git if missing
    if ! check_command git; then
        info "Installing git..."
        if [[ "$OS" == "mac" ]]; then
            if ! check_command brew; then
                error "Homebrew not found. Install Homebrew first: https://brew.sh"
            fi
            brew install git
        elif check_command apt-get; then
            maybe_sudo apt-get install -y git
        elif check_command dnf; then
            maybe_sudo dnf install -y git
        fi
    fi
    success "git found"

    # Install Node.js/npm if missing (needed for agent-browser)
    if ! check_command npm; then
        info "Installing Node.js..."
        if [[ "$OS" == "mac" ]]; then
            if ! check_command brew; then
                error "Homebrew not found. Install Homebrew first: https://brew.sh"
            fi
            brew install node
        elif check_command apt-get; then
            maybe_sudo apt-get install -y nodejs npm
        elif check_command dnf; then
            maybe_sudo dnf install -y nodejs npm
        fi
    fi

    # Install agent-browser for browser automation
    if ! check_command agent-browser; then
        info "Installing agent-browser..."
        npm install -g agent-browser 2>/dev/null || maybe_sudo npm install -g agent-browser
        info "Installing browser dependencies..."
        agent-browser install --with-deps 2>/dev/null || maybe_sudo agent-browser install --with-deps
    fi
    success "agent-browser found"

    # Install uv
    if ! check_command uv; then
        info "Installing uv (Python package manager)..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
        export PATH="$HOME/.cargo/bin:$PATH"
    fi
    success "uv found"

    # Check Python
    if ! uv python list 2>/dev/null | grep -q "3.1[1-9]"; then
        info "Installing Python 3.12 via uv..."
        uv python install 3.12
    fi
    success "Python 3.11+ available"

    # Capture uv path for service setup
    UV_BIN="$(command -v uv || true)"
    if [ -z "${UV_BIN:-}" ]; then
        error "uv is not in PATH after installation"
    fi
    UV_BIN_DIR="$(dirname "$UV_BIN")"
}

get_latest_release() {
    local latest
    latest="$(curl -fsSL "https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/releases/latest" 2>/dev/null | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/' || true)"
    if [ -z "$latest" ]; then
        echo "main"
    else
        echo "$latest"
    fi
}

clone_or_detect_repo() {
    if [[ "$LOCAL_CHECKOUT" == "1" ]]; then
        info "Using local checkout at $INSTALL_DIR"
        cd "$INSTALL_DIR"
        local version
        version="$(git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD 2>/dev/null || echo "dev")"
        success "Repository ready ($version, local checkout)"
        return
    fi

    local version=$(get_latest_release)
    info "Installing Lethe $version..."

    if [ -d "$INSTALL_DIR/.git" ]; then
        info "Updating existing installation..."
        cd "$INSTALL_DIR"
        git fetch origin --tags
        if [ "$version" != "main" ]; then
            git checkout "$version"
        else
            git checkout main
            git pull origin main
        fi
    else
        info "Cloning Lethe..."
        git clone "$REPO_URL" "$INSTALL_DIR"
        cd "$INSTALL_DIR"
        if [ "$version" != "main" ]; then
            git checkout "$version"
        fi
    fi
    success "Repository ready ($version)"
}

setup_config() {
    mkdir -p "$CONFIG_DIR"

    local key_name="${SELECTED_AUTH_KEY_NAME:-$(provider_key_env "$SELECTED_PROVIDER")}"
    local api_key_line=""

    if [[ -n "$key_name" && -n "$API_KEY" ]]; then
        api_key_line="$key_name=$API_KEY"
    fi

    cat > "$CONFIG_DIR/.env" << EOF
# Lethe Configuration
# Generated by installer on $(date)

# Telegram
TELEGRAM_BOT_TOKEN=$TELEGRAM_TOKEN
TELEGRAM_ALLOWED_USER_IDS=$TELEGRAM_USER_ID

# LLM Provider
LLM_PROVIDER=$SELECTED_PROVIDER
LLM_MODEL=$SELECTED_MODEL
LLM_MODEL_AUX=$SELECTED_MODEL_AUX
LLM_API_BASE=$SELECTED_API_BASE
$api_key_line

# Paths
LETHE_HOME=$LETHE_HOME

# Optional: Heartbeat interval (seconds, default 900 = 15 min)
# HEARTBEAT_INTERVAL=900
# HEARTBEAT_ENABLED=true

# Optional: Hippocampus (memory recall)
# HIPPOCAMPUS_ENABLED=true
EOF

    # Symlink to install dir so lethe finds the .env
    ln -sf "$CONFIG_DIR/.env" "$INSTALL_DIR/.env"

    success "Configuration saved to $CONFIG_DIR/.env"
}

setup_lethe_dirs() {
    mkdir -p "$LETHE_HOME/workspace"
    mkdir -p "$LETHE_HOME/workspace/notes"
    mkdir -p "$LETHE_HOME/workspace/skills"
    mkdir -p "$LETHE_HOME/workspace/projects"
    mkdir -p "$LETHE_HOME/data"
    mkdir -p "$LETHE_HOME/data/memory"
    mkdir -p "$LETHE_HOME/credentials"
    mkdir -p "$LETHE_HOME/cache"
    mkdir -p "$LETHE_HOME/logs"
    mkdir -p "$LETHE_HOME/config"
    success "Directory structure created at $LETHE_HOME"
}

setup_service() {
    local OS=$(detect_os)

    if [[ "$OS" == "linux" || "$OS" == "wsl" ]]; then
        setup_systemd
    elif [[ "$OS" == "mac" ]]; then
        setup_launchd
    else
        warn "Unknown OS, skipping service setup"
        echo "Run manually: cd $INSTALL_DIR && uv run lethe"
    fi
}

setup_systemd() {
    # Root user: use system-level service
    if [[ "$(id -u)" -eq 0 ]]; then
        setup_systemd_system
        return
    fi

    mkdir -p "$HOME/.config/systemd/user"

    # Enable lingering so user services run without login session
    if command -v loginctl &>/dev/null; then
        info "Enabling user lingering (allows service to run without login)..."
        maybe_sudo loginctl enable-linger "$(whoami)" 2>/dev/null || true
    fi

    if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
        export XDG_RUNTIME_DIR="/run/user/$(id -u)"
        if [ ! -d "$XDG_RUNTIME_DIR" ]; then
            warn "XDG_RUNTIME_DIR not available. You may need to log in directly (not via su/sudo)."
        fi
    fi

    cat > "$HOME/.config/systemd/user/lethe.service" << EOF
[Unit]
Description=Lethe Autonomous AI Agent
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$UV_BIN run lethe
Restart=always
RestartSec=10
Environment="PATH=$UV_BIN_DIR:/usr/local/bin:/usr/bin:/bin"
Environment="LETHE_HOME=$LETHE_HOME"

[Install]
WantedBy=default.target
EOF

    if ! systemctl --user daemon-reload 2>/dev/null; then
        warn "Could not connect to systemd user bus."
        echo ""
        echo "  Option 1: Fix systemd session"
        echo "    - Log out and log back in, or"
        echo "    - Run: machinectl shell $(whoami)@"
        echo "    - Then: systemctl --user enable --now lethe"
        echo ""
        echo "  Option 2: Run manually (without systemd)"
        echo "    cd $INSTALL_DIR && uv run lethe"
        echo ""
        echo "  Option 3: Run in background with screen/tmux"
        echo "    screen -dmS lethe bash -c 'cd $INSTALL_DIR && uv run lethe'"
        echo "    # Attach: screen -r lethe"
        echo ""
        return
    fi

    systemctl --user enable lethe
    systemctl --user start lethe

    success "Systemd service installed and started"
    info "View logs: journalctl --user -u lethe -f"
}

setup_systemd_system() {
    info "Installing system-level service (running as root)..."

    cat > "/etc/systemd/system/lethe.service" << EOF
[Unit]
Description=Lethe Autonomous AI Agent
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$UV_BIN run lethe
Restart=always
RestartSec=10
Environment="PATH=$UV_BIN_DIR:/usr/local/bin:/usr/bin:/bin"
Environment="HOME=/root"
Environment="LETHE_HOME=$LETHE_HOME"

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable lethe
    systemctl start lethe

    success "System service installed and started"
    info "View logs: journalctl -u lethe -f"
}

setup_launchd() {
    mkdir -p "$HOME/Library/LaunchAgents"

    cat > "$HOME/Library/LaunchAgents/com.lethe.agent.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.lethe.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UV_BIN</string>
        <string>run</string>
        <string>lethe</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$INSTALL_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LETHE_HOME/logs/lethe.log</string>
    <key>StandardErrorPath</key>
    <string>$LETHE_HOME/logs/lethe.error.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$UV_BIN_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>LETHE_HOME</key>
        <string>$LETHE_HOME</string>
    </dict>
</dict>
</plist>
EOF

    launchctl unload "$HOME/Library/LaunchAgents/com.lethe.agent.plist" 2>/dev/null || true
    launchctl load "$HOME/Library/LaunchAgents/com.lethe.agent.plist"

    success "Launchd service installed and started"
    info "View logs: tail -f $LETHE_HOME/logs/lethe.log"
}

install_deps_and_run() {
    cd "$INSTALL_DIR"
    info "Installing Python dependencies..."
    uv sync
    success "Dependencies installed"
}

main() {
    print_header

    # Parse args
    for arg in "$@"; do
        case $arg in
            --help|-h)
                echo "Usage: $0 [OPTIONS]"
                echo ""
                echo "Installs Lethe autonomous AI assistant."
                echo ""
                echo "Options:"
                echo "  --help, -h    Show this help message"
                echo ""
                echo "Environment variables:"
                echo "  LETHE_HOME         Root directory for all Lethe data (default: ~/.lethe)"
                echo "  LETHE_INSTALL_DIR  Where to clone the source (default: \$LETHE_HOME/install)"
                echo "  LETHE_NO_SANDBOX   Set to 1 to disable OS-level write sandbox"
                echo ""
                echo "Supports OpenRouter, Anthropic, and OpenAI as LLM providers."
                exit 0
                ;;
        esac
    done

    # Show sandbox info
    local OS=$(detect_os)
    if [[ "$OS" == "linux" || "$OS" == "wsl" ]]; then
        echo -e "${GREEN}Sandbox${NC}: Landlock (kernel-enforced write restrictions)"
    elif [[ "$OS" == "mac" ]]; then
        echo -e "${GREEN}Sandbox${NC}: Seatbelt (macOS sandbox-exec write restrictions)"
    else
        echo -e "${YELLOW}Sandbox${NC}: not available on this platform"
    fi
    echo -e "  Writes restricted to ${CYAN}$LETHE_HOME${NC} and ${CYAN}/tmp${NC}"
    echo ""

    # Detect existing keys
    detect_api_keys

    # Prompts
    prompt_provider
    prompt_model
    prompt_api_key
    prompt_telegram

    echo ""
    info "Installing Lethe..."
    echo ""

    # Setup
    install_dependencies
    clone_or_detect_repo
    setup_lethe_dirs
    install_deps_and_run
    setup_config

    if [[ "$DEFERRED_OPENAI_OAUTH_LOGIN" == "1" ]]; then
        warn "Skipping service start until OpenAI OAuth is configured."
        warn "Run OAuth login, then start Lethe manually."
    else
        setup_service
    fi

    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Lethe installed successfully!${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo "  Provider:  $SELECTED_PROVIDER"
    echo "  Model:     $SELECTED_MODEL (aux: $SELECTED_MODEL_AUX)"
    echo "  Home:      $LETHE_HOME"
    echo "  Install:   $INSTALL_DIR"
    echo "  Sandbox:   OS-level write restrictions active"
    echo ""
    echo "  Message your bot on Telegram to get started!"
    if [[ "$DEFERRED_OPENAI_OAUTH_LOGIN" == "1" ]]; then
        echo ""
        echo -e "${YELLOW}  OpenAI OAuth pending:${NC}"
        echo "    Run: cd $INSTALL_DIR && $UV_BIN run lethe oauth-login openai"
        echo "    Then start:   cd $INSTALL_DIR && $UV_BIN run lethe"
    fi
    echo ""
    echo "  Useful commands:"
    if [[ "$OS" == "linux" || "$OS" == "wsl" ]]; then
        if [[ "$(id -u)" -eq 0 ]]; then
            echo "    View logs:     journalctl -u lethe -f"
            echo "    Restart:       systemctl restart lethe"
            echo "    Stop:          systemctl stop lethe"
        else
            echo "    View logs:     journalctl --user -u lethe -f"
            echo "    Restart:       systemctl --user restart lethe"
            echo "    Stop:          systemctl --user stop lethe"
        fi
    elif [[ "$OS" == "mac" ]]; then
        echo "    View logs:     tail -f $LETHE_HOME/logs/lethe.log"
        echo "    Restart:       launchctl unload ~/Library/LaunchAgents/com.lethe.agent.plist && launchctl load ~/Library/LaunchAgents/com.lethe.agent.plist"
        echo "    Stop:          launchctl unload ~/Library/LaunchAgents/com.lethe.agent.plist"
    fi
    echo "    Config:        $CONFIG_DIR/.env"
    echo "    Sandbox off:   LETHE_NO_SANDBOX=1"
    echo ""
}

main "$@"
