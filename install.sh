#!/usr/bin/env zsh
#
# Lethe Installer (local-first architecture)
# Usage: curl -fsSL https://lethe.gg/install | zsh
#
# Default: Container mode (Docker/Podman) - safe, limited to ~/lethe/
# Options:
#   --unsafe    Native install with full system access
#
# Supports multiple LLM providers: OpenRouter, Anthropic, OpenAI
#

set -euo pipefail

# Compatible shells: zsh (preferred on macOS) and bash
if [[ -n "${ZSH_VERSION:-}" ]]; then
    :
elif [[ -n "${BASH_VERSION:-}" ]]; then
    :
else
    echo "Error: Unsupported shell. Run with zsh or bash."
    exit 1
fi

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
INSTALL_DIR="${LETHE_INSTALL_DIR:-$HOME/.lethe}"
CONFIG_DIR="${LETHE_CONFIG_DIR:-$HOME/.config/lethe}"
WORKSPACE_DIR="${LETHE_WORKSPACE_DIR:-$HOME/lethe}"
DETECTED_PROVIDERS=()
DEFERRED_OPENAI_OAUTH_LOGIN=0

# Install mode: "container" (safe, default) or "native" (unsafe)
INSTALL_MODE="container"

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
        openrouter) echo "openrouter/moonshotai/kimi-k2.5-0127" ;;
        anthropic) echo "claude-opus-4-5-20251101" ;;
        openai) echo "gpt-5.2" ;;
        *) echo "" ;;
    esac
}

provider_model_aux_default() {
    case "$1" in
        openrouter) echo "openrouter/moonshotai/kimi-k2.5-0127" ;;
        anthropic) echo "claude-haiku-4-5-20251001" ;;
        openai) echo "gpt-5.2" ;;
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
    # Parse KEY=value from env files without executing them.
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
        local default_auth_choice=2
        if [[ "$INSTALL_MODE" == "container" ]]; then
            # Container mode has no host uv/python bootstrap, so API key is safer as default.
            default_auth_choice=1
        fi
        prompt_read "Choose [1-2, default=$default_auth_choice]: " auth_choice
        auth_choice=${auth_choice:-$default_auth_choice}
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
    
    # Then check existing config files
    if [ -z "$existing_key" ] && [ -f "$CONFIG_DIR/container.env" ]; then
        existing_key="$(grep "^$key_name=" "$CONFIG_DIR/container.env" 2>/dev/null | cut -d= -f2- || true)"
        [ -n "$existing_key" ] && key_source="$CONFIG_DIR/container.env"
    fi
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
            if [[ "$INSTALL_MODE" == "container" ]]; then
                error "OPENAI_AUTH_TOKEN is required in container mode. Use OpenAI API key auth, or run installer in native mode for deferred OAuth login."
            fi
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
    
    for config_file in "$CONFIG_DIR/container.env" "$CONFIG_DIR/.env" ".env"; do
        if [ -f "$config_file" ]; then
            [ -z "$existing_token" ] && existing_token="$(grep "^TELEGRAM_BOT_TOKEN=" "$config_file" 2>/dev/null | cut -d= -f2- || true)"
            [ -z "$existing_user_id" ] && existing_user_id="$(grep "^TELEGRAM_ALLOWED_USER_IDS=" "$config_file" 2>/dev/null | cut -d= -f2- || true)"
            [ -n "$existing_token" ] && [ -z "$config_source" ] && config_source="$config_file"
        fi
    done
    
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
    echo "   Create a bot: message @BotFather on Telegram → /newbot → copy the token"
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

    # Container mode only needs host-side git/curl + container runtime.
    # Keep local machine clean: skip Node/agent-browser/uv/Python installs.
    if [[ "$INSTALL_MODE" == "container" ]]; then
        info "Container mode: skipping local Node.js, agent-browser, uv, and Python setup"
        return 0
    fi
    
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

    # Capture uv path for service setup (works for both root and non-root installs)
    UV_BIN="$(command -v uv || true)"
    if [ -z "${UV_BIN:-}" ]; then
        error "uv is not in PATH after installation"
    fi
    UV_BIN_DIR="$(dirname "$UV_BIN")"
}

get_latest_release() {
    # Fetch latest release tag from GitHub API
    local latest
    latest="$(curl -fsSL "https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/releases/latest" 2>/dev/null | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/' || true)"
    if [ -z "$latest" ]; then
        # Fallback to main if no releases
        echo "main"
    else
        echo "$latest"
    fi
}

clone_repo() {
    local version=$(get_latest_release)
    info "Installing Lethe $version..."
    
    if [ -d "$INSTALL_DIR" ]; then
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
    
    # Only add API key line if not using OAuth provider
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

# Paths (workspace at ~/lethe for both native and container modes)
WORKSPACE_DIR=$WORKSPACE_DIR
MEMORY_DIR=$WORKSPACE_DIR/data/memory

# Optional: Heartbeat interval (seconds, default 900 = 15 min)
# HEARTBEAT_INTERVAL=900
# HEARTBEAT_ENABLED=true

# Optional: Hippocampus (memory recall)
# HIPPOCAMPUS_ENABLED=true
EOF

    # Symlink to install dir
    ln -sf "$CONFIG_DIR/.env" "$INSTALL_DIR/.env"
    
    success "Configuration saved to $CONFIG_DIR/.env"
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
    # Root user: use system-level service (no user bus available)
    if [[ "$(id -u)" -eq 0 ]]; then
        setup_systemd_system
        return
    fi
    
    # Non-root: use user-level service
    mkdir -p "$HOME/.config/systemd/user"
    
    # Enable lingering so user services run without login session
    if command -v loginctl &>/dev/null; then
        info "Enabling user lingering (allows service to run without login)..."
        maybe_sudo loginctl enable-linger "$(whoami)" 2>/dev/null || true
    fi
    
    # Ensure XDG_RUNTIME_DIR is set (needed for systemd --user)
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

[Install]
WantedBy=default.target
EOF

    # Try to start the service, with helpful error message if it fails
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
    # System-level service for root installations
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
    <string>$HOME/Library/Logs/lethe.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/lethe.error.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$UV_BIN_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
EOF

    launchctl unload "$HOME/Library/LaunchAgents/com.lethe.agent.plist" 2>/dev/null || true
    launchctl load "$HOME/Library/LaunchAgents/com.lethe.agent.plist"
    
    success "Launchd service installed and started"
    info "View logs: tail -f ~/Library/Logs/lethe.log"
}

install_deps_and_run() {
    cd "$INSTALL_DIR"
    info "Installing Python dependencies..."
    uv sync
    success "Dependencies installed"
}

ensure_podman_ready_on_mac() {
    if ! check_command podman; then
        return 1
    fi

    if podman info >/dev/null 2>&1; then
        return 0
    fi

    local machine_name=""
    machine_name="$(podman machine list --quiet 2>/dev/null | head -n 1 || true)"

    if [[ -z "${machine_name:-}" ]]; then
        info "Initializing Podman machine..."
        podman machine init >/dev/null 2>&1 || true
        machine_name="$(podman machine list --quiet 2>/dev/null | head -n 1 || true)"
    fi

    if [[ -z "${machine_name:-}" ]]; then
        warn "Podman is installed, but no Podman machine is available."
        return 1
    fi

    info "Starting Podman machine ($machine_name)..."
    if ! podman machine start "$machine_name"; then
        warn "Failed to start Podman machine ($machine_name)."
        return 1
    fi

    local i
    for i in 1 2 3 4 5; do
        if podman info >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done

    warn "Podman machine started but socket is still not reachable."
    return 1
}

detect_container_runtime() {
    local OS=$(detect_os)
    local has_podman=false
    local has_docker=false
    
    command -v podman &>/dev/null && has_podman=true
    command -v docker &>/dev/null && has_docker=true

    # Check if Podman backend is actually reachable
    if $has_podman; then
        if ! podman info &>/dev/null; then
            if [[ "$OS" == "mac" ]]; then
                warn "Podman command found but machine/socket is not ready"
                ensure_podman_ready_on_mac || has_podman=false
            else
                warn "Podman command found but daemon not reachable"
                has_podman=false
            fi
        fi
    fi
    
    # Check if Docker daemon is actually reachable
    if $has_docker; then
        if ! docker info &>/dev/null; then
            warn "Docker command found but daemon not reachable"
            if [[ -n "$DOCKER_HOST" ]]; then
                warn "DOCKER_HOST is set to: $DOCKER_HOST"
                warn "This may be pointing to a non-running Docker Desktop"
                echo ""
                echo "  Try one of:"
                echo "    1. Start Docker Desktop"
                echo "    2. Run: unset DOCKER_HOST"
                echo "    3. Run: export DOCKER_HOST=unix:///var/run/docker.sock"
                echo ""
            else
                warn "Try: sudo systemctl start docker"
            fi
            has_docker=false
        fi
    fi
    
    if $has_podman && $has_docker; then
        # Both available - offer selection
        echo ""
        echo -e "${YELLOW}Multiple container runtimes detected:${NC}"
        if [[ "$OS" == "mac" ]]; then
            echo "  1) Docker (recommended on macOS)"
            echo "  2) Podman"
            echo ""
            prompt_read "Select container runtime [1]: " choice
            choice=${choice:-1}
            if [[ "$choice" == "2" ]]; then
                CONTAINER_CMD="podman"
            else
                CONTAINER_CMD="docker"
            fi
        else
            echo "  1) Podman (recommended for rootless)"
            echo "  2) Docker"
            echo ""
            prompt_read "Select container runtime [1]: " choice
            choice=${choice:-1}
            if [[ "$choice" == "2" ]]; then
                CONTAINER_CMD="docker"
            else
                CONTAINER_CMD="podman"
            fi
        fi
        success "Using $CONTAINER_CMD"
    elif $has_podman; then
        CONTAINER_CMD="podman"
        if [[ "$OS" == "mac" ]]; then
            warn "Using Podman on macOS. Depending on machine settings, Podman may request Rosetta."
        fi
    elif $has_docker; then
        CONTAINER_CMD="docker"
    else
        CONTAINER_CMD=""
    fi
}

install_container_runtime() {
    local OS=$(detect_os)
    
    if [[ -n "$CONTAINER_CMD" ]]; then
        if [[ "$CONTAINER_CMD" == "podman" && "$OS" == "mac" ]]; then
            ensure_podman_ready_on_mac || error "Podman is installed but not ready. Run 'podman machine init' and 'podman machine start'."
        fi
        success "$CONTAINER_CMD found"
        return 0
    fi
    
    info "No container runtime found. Installing..."
    
    if [[ "$OS" == "mac" ]]; then
        if check_command brew; then
            info "Installing Podman via Homebrew..."
            brew install podman
            info "Initializing Podman machine..."
            podman machine init 2>/dev/null || true
            info "Starting Podman machine..."
            if ! podman machine start; then
                warn "Podman machine failed to start automatically."
                warn "On Apple Silicon, Podman may require Rosetta depending on VM/image settings."
                error "Run 'podman machine start' manually and rerun installer."
            fi
            CONTAINER_CMD="podman"
        else
            error "Homebrew not found. Install Homebrew first: https://brew.sh"
        fi
    elif [[ "$OS" == "linux" || "$OS" == "wsl" ]]; then
        if check_command apt-get; then
            maybe_sudo apt-get update -y
            maybe_sudo apt-get install -y podman
            CONTAINER_CMD="podman"
        elif check_command dnf; then
            maybe_sudo dnf install -y podman
            CONTAINER_CMD="podman"
        else
            error "Please install Docker or Podman manually"
        fi
    else
        error "Please install Docker or Podman manually"
    fi
    
    success "$CONTAINER_CMD installed"
}

setup_container() {
    info "Setting up container..."
    
    # Create workspace directory (with write permissions for container user)
    mkdir -p "$WORKSPACE_DIR"
    mkdir -p "$WORKSPACE_DIR/skills"
    mkdir -p "$WORKSPACE_DIR/projects"
    mkdir -p "$WORKSPACE_DIR/.cache/uv"
    chmod 700 "$WORKSPACE_DIR"
    
    # Build image (--load required for Docker buildx to load into local daemon)
    cd "$INSTALL_DIR"
    $CONTAINER_CMD build --load -t lethe:latest .
    
    # Create env file for container
    local key_name="${SELECTED_AUTH_KEY_NAME:-$(provider_key_env "$SELECTED_PROVIDER")}"
    local auth_line=""
    if [[ -n "$key_name" && -n "$API_KEY" ]]; then
        auth_line="$key_name=$API_KEY"
    fi
    
    cat > "$CONFIG_DIR/container.env" << EOF
TELEGRAM_BOT_TOKEN=$TELEGRAM_TOKEN
TELEGRAM_ALLOWED_USER_IDS=$TELEGRAM_USER_ID
LLM_PROVIDER=$SELECTED_PROVIDER
LLM_MODEL=$SELECTED_MODEL
LLM_MODEL_AUX=$SELECTED_MODEL_AUX
LLM_API_BASE=$SELECTED_API_BASE
$auth_line
HEARTBEAT_ENABLED=true
HIPPOCAMPUS_ENABLED=true
EOF
    
    # Stop existing container if running
    $CONTAINER_CMD stop lethe 2>/dev/null || true
    $CONTAINER_CMD rm lethe 2>/dev/null || true
    
    # Run container
    # Podman: --userns=keep-id maps host user
    # Docker rootless: run as root inside, UID mapping is automatic
    # Docker traditional: use gosu entrypoint with HOST_UID/GID
    if [[ "$CONTAINER_CMD" == "podman" ]]; then
        $CONTAINER_CMD run -d \
            --name lethe \
            --restart unless-stopped \
            --userns=keep-id \
            -e UV_CACHE_DIR=/workspace/.cache/uv \
            -e XDG_CACHE_HOME=/workspace/.cache \
            --env-file "$CONFIG_DIR/container.env" \
            -v "$WORKSPACE_DIR:/workspace:Z" \
            lethe:latest
    elif docker info 2>/dev/null | grep -q "rootless"; then
        # Rootless Docker - UID mapping handled automatically
        $CONTAINER_CMD run -d \
            --name lethe \
            --restart unless-stopped \
            -e UV_CACHE_DIR=/workspace/.cache/uv \
            -e XDG_CACHE_HOME=/workspace/.cache \
            --env-file "$CONFIG_DIR/container.env" \
            -v "$WORKSPACE_DIR:/workspace:z" \
            lethe:latest
    else
        # Traditional Docker - use gosu entrypoint for UID mapping
        # Requires sudo for apt-get inside container
        $CONTAINER_CMD run -d \
            --name lethe \
            --restart unless-stopped \
            -e HOST_UID=$(id -u) \
            -e HOST_GID=$(id -g) \
            -e UV_CACHE_DIR=/workspace/.cache/uv \
            -e XDG_CACHE_HOME=/workspace/.cache \
            --env-file "$CONFIG_DIR/container.env" \
            -v "$WORKSPACE_DIR:/workspace" \
            lethe:latest
    fi
    
    success "Container started"
    info "Workspace: $WORKSPACE_DIR"
    info "View logs: $CONTAINER_CMD logs -f lethe"
}

main() {
    print_header
    
    # Parse args
    for arg in "$@"; do
        case $arg in
            --unsafe)
                INSTALL_MODE="native"
                shift
                ;;
            --help|-h)
                echo "Usage: $0 [OPTIONS]"
                echo ""
                echo "Installs Lethe autonomous AI assistant."
                echo ""
                echo "Options:"
                echo "  --unsafe    Native install with full system access"
                echo "              (default: container with access to ~/lethe/ only)"
                echo ""
                echo "Supports OpenRouter, Anthropic, and OpenAI as LLM providers."
                exit 0
                ;;
        esac
    done
    
    # Show install mode
    if [[ "$INSTALL_MODE" == "container" ]]; then
        echo -e "${GREEN}Safe Mode${NC}: Container with access limited to ~/lethe/"
    else
        echo -e "${YELLOW}Unsafe Mode${NC}: Native install with full system access"
    fi
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
    
    # Common setup
    install_dependencies
    clone_repo
    mkdir -p "$CONFIG_DIR"
    
    if [[ "$INSTALL_MODE" == "container" ]]; then
        # Container mode
        detect_container_runtime
        install_container_runtime
        setup_container
        
        echo ""
        echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
        echo -e "${GREEN}  Lethe installed successfully! (Container Mode)${NC}"
        echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
        echo ""
        echo "  Provider: $SELECTED_PROVIDER"
        echo "  Model: $SELECTED_MODEL (aux: $SELECTED_MODEL_AUX)"
        echo "  Workspace: $WORKSPACE_DIR (agent can only access this directory)"
        echo ""
        echo "  Message your bot on Telegram to get started!"
        echo ""
        echo "  Useful commands:"
        echo "    View logs:     $CONTAINER_CMD logs -f lethe"
        echo "    Restart:       $CONTAINER_CMD restart lethe"
        echo "    Stop:          $CONTAINER_CMD stop lethe"
        echo "    Config:        $CONFIG_DIR/container.env"
        echo ""
    else
        # Native mode (unsafe)
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
        echo -e "${YELLOW}  Lethe installed successfully! (Native Mode - Full Access)${NC}"
        echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
        echo ""
        echo "  Provider: $SELECTED_PROVIDER"
        echo "  Model: $SELECTED_MODEL (aux: $SELECTED_MODEL_AUX)"
        echo -e "  ${YELLOW}WARNING: Agent has full system access${NC}"
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
        echo "    View logs:     journalctl --user -u lethe -f"
        echo "    Restart:       systemctl --user restart lethe"
        echo "    Stop:          systemctl --user stop lethe"
        echo "    Config:        $CONFIG_DIR/.env"
        echo ""
    fi
}

main "$@"
