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
    echo -e "${YELLOW}Lethe needs two API tokens to work:${NC}"
    echo ""
    echo "1. Telegram Bot Token - Create a bot via @BotFather on Telegram"
    echo "   https://t.me/BotFather → /newbot → copy the token"
    echo ""
    echo "2. Letta API Key - Sign up at https://app.letta.com"
    echo "   Settings → API Keys → Create new key"
    echo ""
    
    read -p "Telegram Bot Token: " TELEGRAM_TOKEN
    if [ -z "$TELEGRAM_TOKEN" ]; then
        error "Telegram token is required"
    fi
    
    read -p "Letta API Key: " LETTA_KEY
    if [ -z "$LETTA_KEY" ]; then
        error "Letta API key is required"
    fi
    
    read -p "Your Telegram User ID (for authorization): " TELEGRAM_USER_ID
    if [ -z "$TELEGRAM_USER_ID" ]; then
        warn "No user ID provided - bot will accept messages from anyone!"
    fi
}

install_native() {
    OS=$(detect_os)
    info "Detected OS: $OS"
    
    # Check prerequisites
    info "Checking prerequisites..."
    
    if ! check_command git; then
        error "git is required. Please install it first."
    fi
    success "git found"
    
    if ! check_command python3; then
        error "Python 3 is required. Please install Python 3.11+."
    fi
    
    PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    if [ "$(echo "$PYTHON_VERSION < 3.11" | bc -l 2>/dev/null || echo 1)" = "1" ]; then
        # bc might not exist, try another way
        MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
        MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)
        if [ "$MAJOR" -lt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 11 ]); then
            error "Python 3.11+ is required. Found: $PYTHON_VERSION"
        fi
    fi
    success "Python $PYTHON_VERSION found"
    
    # Install uv if needed
    if ! check_command uv; then
        info "Installing uv (Python package manager)..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
    fi
    success "uv found"
    
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
    case $OS in
        linux|wsl)
            setup_systemd
            ;;
        mac)
            setup_launchd
            ;;
    esac
    
    echo ""
    success "Lethe installed successfully!"
    echo ""
    echo "Commands:"
    echo "  Start:   systemctl --user start lethe"
    echo "  Stop:    systemctl --user stop lethe"
    echo "  Logs:    journalctl --user -u lethe -f"
    echo "  Status:  systemctl --user status lethe"
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
    
    echo ""
    echo "Mac-specific commands:"
    echo "  Start:   launchctl start com.lethe.agent"
    echo "  Stop:    launchctl stop com.lethe.agent"
    echo "  Logs:    tail -f ~/Library/Logs/lethe.log"
}

install_contained() {
    info "Installing in container mode (safety first)..."
    
    RUNTIME=$(detect_container_runtime)
    if [ -z "$RUNTIME" ]; then
        error "Docker or Podman is required for contained installation.
        
Install Docker: https://docs.docker.com/get-docker/
Or Podman: https://podman.io/getting-started/installation"
    fi
    success "Container runtime: $RUNTIME"
    
    # Create directories
    mkdir -p "$CONFIG_DIR"
    mkdir -p "$INSTALL_DIR/workspace"
    mkdir -p "$INSTALL_DIR/data"
    
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

# Workspace for agent
VOLUME /app/workspace
VOLUME /app/data

CMD ["uv", "run", "lethe"]
EOF
    fi
    
    $RUNTIME build -t lethe:latest "$INSTALL_DIR"
    success "Container built"
    
    # Create run script
    cat > "$INSTALL_DIR/run-lethe.sh" << EOF
#!/bin/bash
$RUNTIME run -d \\
    --name lethe \\
    --restart unless-stopped \\
    --env-file "$CONFIG_DIR/.env" \\
    -v "$INSTALL_DIR/workspace:/app/workspace" \\
    -v "$INSTALL_DIR/data:/app/data" \\
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
    echo "The container has access to:"
    echo "  - $INSTALL_DIR/workspace (read/write)"
    echo "  - $INSTALL_DIR/data (databases)"
    echo ""
    echo "It does NOT have access to your home directory or system files."
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
