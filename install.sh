#!/usr/bin/env bash
#
# Lethe Installer
# Usage: curl -fsSL https://raw.githubusercontent.com/atemerev/lethe/main/install.sh | bash
#
# Options:
#   --contained    Install in Docker/Podman container (safer)
#   --native       Install directly on host (default)
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Config
REPO_URL="https://github.com/atemerev/lethe.git"
INSTALL_DIR="${LETHE_INSTALL_DIR:-$HOME/.lethe}"
CONFIG_DIR="${LETHE_CONFIG_DIR:-$HOME/.config/lethe}"

# Parse args
INSTALL_MODE="native"
for arg in "$@"; do
    case $arg in
        --contained)
            INSTALL_MODE="contained"
            shift
            ;;
        --native)
            INSTALL_MODE="native"
            shift
            ;;
        --help|-h)
            echo "Lethe Installer"
            echo ""
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --native      Install directly on host (default)"
            echo "  --contained   Install in Docker/Podman container (safer)"
            echo "  --help        Show this help"
            exit 0
            ;;
    esac
done

print_header() {
    echo -e "${BLUE}"
    echo "╔═══════════════════════════════════════════════════════════╗"
    echo "║                                                           ║"
    echo "║   █░░ █▀▀ ▀█▀ █░█ █▀▀                                     ║"
    echo "║   █▄▄ ██▄ ░█░ █▀█ ██▄                                     ║"
    echo "║                                                           ║"
    echo "║   Autonomous Executive Assistant                          ║"
    echo "║                                                           ║"
    echo "╚═══════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
    exit 1
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
        Darwin*)
            echo "mac"
            ;;
        *)
            echo "unknown"
            ;;
    esac
}

check_command() {
    command -v "$1" >/dev/null 2>&1
}

is_root() {
    [[ "$(id -u)" -eq 0 ]]
}

maybe_sudo() {
    if is_root; then
        "$@"
    else
        sudo "$@"
    fi
}

install_homebrew() {
    if check_command brew; then
        success "Homebrew found"
        return 0
    fi
    
    info "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    
    # Add to PATH for this session
    if [[ -f "/opt/homebrew/bin/brew" ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -f "/usr/local/bin/brew" ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
    elif [[ -f "/home/linuxbrew/.linuxbrew/bin/brew" ]]; then
        eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"
    fi
    success "Homebrew installed"
}

install_git() {
    if check_command git; then
        success "git found"
        return 0
    fi
    
    info "Installing git..."
    OS=$(detect_os)
    
    if [[ "$OS" == "mac" ]]; then
        install_homebrew
        brew install git
    elif [[ "$OS" == "linux" ]] || [[ "$OS" == "wsl" ]]; then
        if check_command apt-get; then
            maybe_sudo apt-get update -y
            maybe_sudo apt-get install -y git
        elif check_command dnf; then
            maybe_sudo dnf install -y git
        elif check_command yum; then
            maybe_sudo yum install -y git
        elif check_command pacman; then
            maybe_sudo pacman -S --noconfirm git
        else
            # Fallback to Linuxbrew
            install_homebrew
            brew install git
        fi
    fi
    success "git installed"
}

install_curl() {
    if check_command curl; then
        return 0
    fi
    
    info "Installing curl..."
    OS=$(detect_os)
    
    if [[ "$OS" == "mac" ]]; then
        install_homebrew
        brew install curl
    elif [[ "$OS" == "linux" ]] || [[ "$OS" == "wsl" ]]; then
        if check_command apt-get; then
            maybe_sudo apt-get update -y
            maybe_sudo apt-get install -y curl
        elif check_command dnf; then
            maybe_sudo dnf install -y curl
        elif check_command yum; then
            maybe_sudo yum install -y curl
        elif check_command pacman; then
            maybe_sudo pacman -S --noconfirm curl
        fi
    fi
    success "curl installed"
}

# Detect container runtime
detect_container_runtime() {
    if check_command podman; then
        echo "podman"
    elif check_command docker; then
        echo "docker"
    else
        echo ""
    fi
}

prompt_tokens() {
    echo ""
    echo -e "${YELLOW}Lethe needs a few things to get started:${NC}"
    echo ""
    
    echo -e "${BLUE}1. Telegram Bot Token${NC}"
    echo "   Create a bot: message @BotFather on Telegram → /newbot → copy the token"
    echo ""
    read -p "   Telegram Bot Token: " TELEGRAM_TOKEN < /dev/tty
    if [ -z "$TELEGRAM_TOKEN" ]; then
        error "Telegram token is required"
    fi
    echo ""
    
    echo -e "${BLUE}2. Letta API Key${NC}"
    echo "   Sign up at https://app.letta.com → Settings → API Keys → Create new key"
    echo ""
    read -p "   Letta API Key: " LETTA_KEY < /dev/tty
    if [ -z "$LETTA_KEY" ]; then
        error "Letta API key is required"
    fi
    echo ""
    
    echo -e "${BLUE}3. Your Telegram User ID${NC}"
    echo "   Message @userinfobot on Telegram - it replies with your ID (a number like 123456789)"
    echo "   This restricts the bot to only respond to you."
    echo ""
    read -p "   Telegram User ID: " TELEGRAM_USER_ID < /dev/tty
    if [ -z "$TELEGRAM_USER_ID" ]; then
        error "Telegram User ID is required (security: prevents strangers from using your bot)"
    fi
}

install_native() {
    OS=$(detect_os)
    info "Detected OS: $OS"
    
    # Install dependencies if missing
    info "Checking dependencies..."
    
    install_curl
    install_git
    
    # Install uv (manages Python versions too)
    if ! check_command uv; then
        info "Installing uv (Python package manager)..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
        if ! check_command uv; then
            export PATH="$HOME/.cargo/bin:$PATH"
        fi
    fi
    success "uv found"
    
    # Check Python version, install via uv if needed
    NEED_PYTHON=false
    if ! check_command python3; then
        NEED_PYTHON=true
    else
        PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
        MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)
        if [ "$MAJOR" -lt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 11 ]); then
            NEED_PYTHON=true
            info "Python $PYTHON_VERSION found, but 3.11+ required"
        fi
    fi
    
    if [ "$NEED_PYTHON" = true ]; then
        info "Installing Python 3.12 via uv..."
        uv python install 3.12
        success "Python 3.12 installed"
    else
        success "Python $PYTHON_VERSION found"
    fi
    
    # Clone or update repo
    if [ -d "$INSTALL_DIR" ]; then
        info "Updating existing installation..."
        cd "$INSTALL_DIR"
        git pull
    else
        info "Cloning Lethe..."
        git clone "$REPO_URL" "$INSTALL_DIR"
        cd "$INSTALL_DIR"
    fi
    success "Repository ready"
    
    # Create venv and install deps
    info "Installing dependencies..."
    cd "$INSTALL_DIR"
    uv sync
    success "Dependencies installed"
    
    # Create config directory
    mkdir -p "$CONFIG_DIR"
    
    # Prompt for tokens
    prompt_tokens
    
    # Create .env file
    cat > "$INSTALL_DIR/.env" << EOF
TELEGRAM_BOT_TOKEN=$TELEGRAM_TOKEN
LETTA_API_KEY=$LETTA_KEY
ALLOWED_USER_IDS=$TELEGRAM_USER_ID
EOF
    chmod 600 "$INSTALL_DIR/.env"
    success "Configuration saved"
    
    # Set up service based on OS
    SERVICE_SETUP=false
    case $OS in
        linux|wsl)
            if check_command systemctl; then
                setup_systemd
                SERVICE_SETUP=true
            else
                warn "systemd not available - skipping service setup"
            fi
            ;;
        mac)
            setup_launchd
            SERVICE_SETUP=true
            ;;
    esac
    
    echo ""
    success "Lethe installed successfully!"
    echo ""
    
    if [ "$SERVICE_SETUP" = true ]; then
        if [ "$OS" = "mac" ]; then
            echo "Commands:"
            echo "  Start:   launchctl start com.lethe.agent"
            echo "  Stop:    launchctl stop com.lethe.agent"
            echo "  Logs:    tail -f ~/Library/Logs/lethe.log"
        else
            echo "Commands:"
            echo "  Start:   systemctl --user start lethe"
            echo "  Stop:    systemctl --user stop lethe"
            echo "  Logs:    journalctl --user -u lethe -f"
            echo "  Status:  systemctl --user status lethe"
        fi
    else
        echo "To run Lethe manually:"
        echo "  cd $INSTALL_DIR && uv run lethe"
    fi
    echo ""
    echo "Now message your bot on Telegram!"
}

setup_systemd() {
    info "Setting up systemd service..."
    
    mkdir -p "$HOME/.config/systemd/user"
    
    cat > "$HOME/.config/systemd/user/lethe.service" << EOF
[Unit]
Description=Lethe Autonomous AI Agent
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m lethe
Restart=on-failure
RestartSec=10
Environment=PATH=$INSTALL_DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable lethe
    systemctl --user start lethe
    success "Systemd service configured and started"
}

setup_launchd() {
    info "Setting up launchd service..."
    
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
        <string>$INSTALL_DIR/.venv/bin/python</string>
        <string>-m</string>
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
</dict>
</plist>
EOF

    launchctl load "$HOME/Library/LaunchAgents/com.lethe.agent.plist"
    success "Launchd service configured and started"
}

install_contained() {
    info "Installing in container mode (safety first)..."
    
    RUNTIME=$(detect_container_runtime)
    if [ -z "$RUNTIME" ]; then
        echo -e "${RED}[ERROR]${NC} Docker or Podman is required for contained installation."
        echo ""
        echo "Install one of:"
        echo ""
        echo "  Podman (recommended for Linux):"
        echo "    Fedora:       sudo dnf install podman"
        echo "    Ubuntu/Debian: sudo apt install podman"
        echo "    macOS:        brew install podman && podman machine init && podman machine start"
        echo ""
        echo "  Docker:"
        echo "    All platforms: https://docs.docker.com/get-docker/"
        echo ""
        exit 1
    fi
    success "Container runtime: $RUNTIME"
    
    # Create directories
    mkdir -p "$CONFIG_DIR"
    mkdir -p "$INSTALL_DIR"
    mkdir -p "$HOME/lethe/workspace"
    mkdir -p "$HOME/lethe/data"
    
    # Prompt for tokens
    prompt_tokens
    
    # Create .env file
    cat > "$CONFIG_DIR/.env" << EOF
TELEGRAM_BOT_TOKEN=$TELEGRAM_TOKEN
LETTA_API_KEY=$LETTA_KEY
ALLOWED_USER_IDS=$TELEGRAM_USER_ID
EOF
    chmod 600 "$CONFIG_DIR/.env"
    success "Configuration saved"
    
    # Build container
    info "Building container..."
    
    # Create Dockerfile if not exists
    if [ ! -f "$INSTALL_DIR/Dockerfile" ]; then
        mkdir -p "$INSTALL_DIR"
        cat > "$INSTALL_DIR/Dockerfile" << 'EOF'
FROM python:3.12-slim

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl && \
    rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Clone and install
RUN git clone https://github.com/atemerev/lethe.git .
RUN uv sync

# Create data directories
RUN mkdir -p /app/workspace /app/data

VOLUME /app/workspace
VOLUME /app/data

CMD ["uv", "run", "lethe"]
EOF
    fi
    
    $RUNTIME build -t lethe:latest "$INSTALL_DIR"
    success "Container built"
    
    # Create run script (:Z for SELinux compatibility, harmless elsewhere)
    cat > "$INSTALL_DIR/run-lethe.sh" << EOF
#!/bin/bash
$RUNTIME run -d \\
    --name lethe \\
    --restart unless-stopped \\
    --env-file "$CONFIG_DIR/.env" \\
    -v "$HOME/lethe/workspace:/app/workspace:Z" \\
    -v "$HOME/lethe/data:/app/data:Z" \\
    lethe:latest
EOF
    chmod +x "$INSTALL_DIR/run-lethe.sh"
    
    # Start container
    info "Starting container..."
    "$INSTALL_DIR/run-lethe.sh"
    success "Container started"
    
    echo ""
    success "Lethe installed in container mode!"
    echo ""
    echo "Commands:"
    echo "  Start:   $RUNTIME start lethe"
    echo "  Stop:    $RUNTIME stop lethe"
    echo "  Logs:    $RUNTIME logs -f lethe"
    echo "  Shell:   $RUNTIME exec -it lethe bash"
    echo ""
    echo "Container has access to:"
    echo "  ~/lethe/workspace - Put files here for the agent to access"
    echo "  ~/lethe/data      - Databases (managed by Lethe)"
    echo ""
    echo "The container does NOT have access to the rest of your home directory."
}

# Main
print_header

echo "Install mode: $INSTALL_MODE"
echo ""

case $INSTALL_MODE in
    native)
        install_native
        ;;
    contained)
        install_contained
        ;;
    *)
        error "Unknown install mode: $INSTALL_MODE"
        ;;
esac
