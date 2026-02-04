#!/usr/bin/env bash
#
# Lethe Installer (local-first architecture)
# Usage: curl -fsSL https://lethe.gg/install | bash
#
# Default: Container mode (Docker/Podman) - safe, limited to ~/lethe/
# Options:
#   --unsafe    Native install with full system access
#
# Supports multiple LLM providers: OpenRouter, Anthropic, OpenAI
#

set -e

# Require bash 4+ for associative arrays
if [[ "${BASH_VERSINFO[0]}" -lt 4 ]]; then
    echo "Error: This script requires bash 4.0 or later."
    echo "On macOS, install with: brew install bash"
    echo "Then run: /opt/homebrew/bin/bash install.sh"
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

# Install mode: "container" (safe, default) or "native" (unsafe)
INSTALL_MODE="container"

# Provider defaults
# NOTE: claude-max disabled - Anthropic blocked third-party OAuth in Jan 2026
declare -A PROVIDERS=(
    ["openrouter"]="OpenRouter (recommended - access to all models)"
    ["anthropic"]="Anthropic API (Claude models with API key)"
    ["openai"]="OpenAI (GPT models)"
)
declare -A PROVIDER_KEYS=(
    ["openrouter"]="OPENROUTER_API_KEY"
    ["anthropic"]="ANTHROPIC_API_KEY"
    ["openai"]="OPENAI_API_KEY"
)
declare -A PROVIDER_MODELS=(
    ["openrouter"]="openrouter/moonshotai/kimi-k2.5-0127"
    ["anthropic"]="claude-opus-4-5-20251101"
    ["openai"]="gpt-5.2"
)
declare -A PROVIDER_MODELS_AUX=(
    ["openrouter"]="openrouter/moonshotai/kimi-k2.5-0127"
    ["anthropic"]="claude-haiku-4-5-20251001"
    ["openai"]="gpt-5.2-mini"
)
declare -A PROVIDER_URLS=(
    ["openrouter"]="https://openrouter.ai/keys"
    ["anthropic"]="https://console.anthropic.com/settings/keys"
    ["openai"]="https://platform.openai.com/api-keys"
)

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

maybe_sudo() {
    if [[ "$(id -u)" -eq 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
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
    if [ -n "${OPENAI_API_KEY:-}" ]; then
        DETECTED_PROVIDERS+=("openai")
    fi
    
    # Also check .env file if it exists
    if [ -f "$CONFIG_DIR/.env" ]; then
        source "$CONFIG_DIR/.env" 2>/dev/null || true
        if [ -n "${OPENROUTER_API_KEY:-}" ] && [[ ! " ${DETECTED_PROVIDERS[*]} " =~ " openrouter " ]]; then
            DETECTED_PROVIDERS+=("openrouter")
        fi
        if [ -n "${ANTHROPIC_API_KEY:-}" ] && [[ ! " ${DETECTED_PROVIDERS[*]} " =~ " anthropic " ]]; then
            DETECTED_PROVIDERS+=("anthropic")
        fi
        if [ -n "${OPENAI_API_KEY:-}" ] && [[ ! " ${DETECTED_PROVIDERS[*]} " =~ " openai " ]]; then
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
        local desc="${PROVIDERS[$provider]}"
        local detected=""
        if [[ " ${DETECTED_PROVIDERS[*]} " =~ " $provider " ]]; then
            detected="${GREEN}[key found]${NC}"
        fi
        echo -e "  $i) $desc $detected"
        ((i++))
    done
    echo ""
    
    local default_choice=1
    if [[ " ${DETECTED_PROVIDERS[*]} " =~ " anthropic " ]]; then
        default_choice=2
    elif [[ " ${DETECTED_PROVIDERS[*]} " =~ " openai " ]]; then
        default_choice=3
    fi
    
    read -p "Choose provider [1-3, default=$default_choice]: " choice < /dev/tty
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
    local default_model="${PROVIDER_MODELS[$SELECTED_PROVIDER]}"
    local default_aux="${PROVIDER_MODELS_AUX[$SELECTED_PROVIDER]}"
    
    echo ""
    echo -e "${BLUE}Main model${NC} (for conversations):"
    echo -e "  Default: ${CYAN}$default_model${NC}"
    read -p "  Press Enter for default, or enter custom: " custom_model < /dev/tty
    
    if [ -n "$custom_model" ]; then
        SELECTED_MODEL="$custom_model"
    else
        SELECTED_MODEL="$default_model"
    fi
    
    echo ""
    echo -e "${BLUE}Auxiliary model${NC} (for heartbeats, summarization - cheaper/faster):"
    echo -e "  Default: ${CYAN}$default_aux${NC}"
    read -p "  Press Enter for default, or enter custom: " custom_aux < /dev/tty
    
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
    read -p "  Custom API base URL (or Enter to skip): " custom_api_base < /dev/tty
    
    if [ -n "$custom_api_base" ]; then
        SELECTED_API_BASE="$custom_api_base"
        success "API base: $SELECTED_API_BASE"
    else
        SELECTED_API_BASE=""
    fi
}

prompt_api_key() {
    local key_name="${PROVIDER_KEYS[$SELECTED_PROVIDER]}"
    local key_url="${PROVIDER_URLS[$SELECTED_PROVIDER]}"
    
    # Check if already have key in environment or existing config
    local existing_key=""
    local key_source=""
    
    # First check environment
    case $SELECTED_PROVIDER in
        openrouter) existing_key="${OPENROUTER_API_KEY:-}" ;;
        anthropic) existing_key="${ANTHROPIC_API_KEY:-}" ;;
        openai) existing_key="${OPENAI_API_KEY:-}" ;;
    esac
    [ -n "$existing_key" ] && key_source="environment"
    
    # Then check existing config files
    if [ -z "$existing_key" ] && [ -f "$CONFIG_DIR/container.env" ]; then
        existing_key=$(grep "^$key_name=" "$CONFIG_DIR/container.env" 2>/dev/null | cut -d= -f2-)
        [ -n "$existing_key" ] && key_source="$CONFIG_DIR/container.env"
    fi
    if [ -z "$existing_key" ] && [ -f "$CONFIG_DIR/.env" ]; then
        existing_key=$(grep "^$key_name=" "$CONFIG_DIR/.env" 2>/dev/null | cut -d= -f2-)
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
        read -p "Choose [1-2, default=1]: " choice < /dev/tty
        choice=${choice:-1}
        
        if [[ "$choice" == "1" ]]; then
            API_KEY="$existing_key"
            return
        fi
    fi
    
    echo ""
    echo -e "${BLUE}$key_name required${NC}"
    echo "   Get your key at: $key_url"
    echo ""
    read -p "   $key_name: " API_KEY < /dev/tty
    if [ -z "$API_KEY" ]; then
        error "$key_name is required"
    fi
}

prompt_telegram() {
    echo ""
    echo -e "${YELLOW}Telegram Configuration:${NC}"
    echo ""
    
    echo -e "${BLUE}1. Telegram Bot Token${NC}"
    echo "   Create a bot: message @BotFather on Telegram → /newbot → copy the token"
    echo ""
    read -p "   Telegram Bot Token: " TELEGRAM_TOKEN < /dev/tty
    if [ -z "$TELEGRAM_TOKEN" ]; then
        error "Telegram token is required"
    fi
    echo ""
    
    echo -e "${BLUE}2. Your Telegram User ID${NC}"
    echo "   Message @userinfobot on Telegram - it replies with your ID (a number like 123456789)"
    echo "   This restricts the bot to only respond to you."
    echo ""
    read -p "   Telegram User ID: " TELEGRAM_USER_ID < /dev/tty
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
}

get_latest_release() {
    # Fetch latest release tag from GitHub API
    local latest=$(curl -fsSL "https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/releases/latest" 2>/dev/null | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/')
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
    
    local key_name="${PROVIDER_KEYS[$SELECTED_PROVIDER]}"
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
ExecStart=$HOME/.local/bin/uv run lethe
Restart=always
RestartSec=10
Environment="PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

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
ExecStart=/root/.local/bin/uv run lethe
Restart=always
RestartSec=10
Environment="PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin"
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
        <string>$HOME/.local/bin/uv</string>
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
        <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
EOF

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

detect_container_runtime() {
    if command -v podman &>/dev/null; then
        CONTAINER_CMD="podman"
    elif command -v docker &>/dev/null; then
        CONTAINER_CMD="docker"
    else
        CONTAINER_CMD=""
    fi
}

install_container_runtime() {
    local OS=$(detect_os)
    
    if [[ -n "$CONTAINER_CMD" ]]; then
        success "$CONTAINER_CMD found"
        return 0
    fi
    
    info "No container runtime found. Installing..."
    
    if [[ "$OS" == "mac" ]]; then
        if check_command brew; then
            brew install podman
            podman machine init
            podman machine start
            CONTAINER_CMD="podman"
        else
            error "Please install Docker Desktop or Podman manually"
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
    chmod 777 "$WORKSPACE_DIR"
    
    # Build image
    cd "$INSTALL_DIR"
    $CONTAINER_CMD build -t lethe:latest .
    
    # Create env file for container
    local key_name="${PROVIDER_KEYS[$SELECTED_PROVIDER]}"
    
    cat > "$CONFIG_DIR/container.env" << EOF
TELEGRAM_BOT_TOKEN=$TELEGRAM_TOKEN
TELEGRAM_ALLOWED_USER_IDS=$TELEGRAM_USER_ID
LLM_PROVIDER=$SELECTED_PROVIDER
LLM_MODEL=$SELECTED_MODEL
LLM_MODEL_AUX=$SELECTED_MODEL_AUX
LLM_API_BASE=$SELECTED_API_BASE
$key_name=$API_KEY
HEARTBEAT_ENABLED=true
HIPPOCAMPUS_ENABLED=true
EOF
    
    # Stop existing container if running
    $CONTAINER_CMD stop lethe 2>/dev/null || true
    $CONTAINER_CMD rm lethe 2>/dev/null || true
    
    # Run container
    # Podman: --userns=keep-id maps container user to host user for file permissions
    # Docker: --user maps to host UID/GID directly
    if [[ "$CONTAINER_CMD" == "podman" ]]; then
        $CONTAINER_CMD run -d \
            --name lethe \
            --restart unless-stopped \
            --userns=keep-id \
            --env-file "$CONFIG_DIR/container.env" \
            -v "$WORKSPACE_DIR:/workspace:Z" \
            lethe:latest
    else
        # Docker - run as host user for file permissions
        $CONTAINER_CMD run -d \
            --name lethe \
            --restart unless-stopped \
            --user "$(id -u):$(id -g)" \
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
        setup_service
        
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
