#!/usr/bin/env bash
#
# Lethe Uninstaller
# Removes Lethe and all associated files
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

INSTALL_DIR="${LETHE_INSTALL_DIR:-$HOME/.lethe}"
CONFIG_DIR="${LETHE_CONFIG_DIR:-$HOME/.config/lethe}"

info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
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

echo -e "${RED}"
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║                  LETHE UNINSTALLER                        ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo -e "${NC}"
WORKSPACE_DIR="${LETHE_WORKSPACE_DIR:-$HOME/lethe}"

echo ""
echo "This will remove:"
echo "  - Container (if using safe mode)"
echo "  - System service (if using native mode)"
echo "  - Installation directory: $INSTALL_DIR"
echo ""
echo "This will NOT remove:"
echo "  - Your config: $CONFIG_DIR"
echo "  - Your workspace: $WORKSPACE_DIR"
echo ""
read -p "Are you sure you want to uninstall Lethe? [y/N] " -n 1 -r < /dev/tty
echo ""

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

OS=$(detect_os)

# Stop and remove systemd user service (Linux/WSL)
if [ -f "$HOME/.config/systemd/user/lethe.service" ]; then
    info "Stopping systemd user service..."
    systemctl --user stop lethe 2>/dev/null || true
    systemctl --user disable lethe 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/lethe.service"
    systemctl --user daemon-reload 2>/dev/null || true
    success "Systemd user service removed"
fi

# Stop and remove systemd system service (root installs)
if [ -f "/etc/systemd/system/lethe.service" ]; then
    info "Stopping systemd system service..."
    sudo systemctl stop lethe 2>/dev/null || systemctl stop lethe 2>/dev/null || true
    sudo systemctl disable lethe 2>/dev/null || systemctl disable lethe 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/lethe.service" || rm -f "/etc/systemd/system/lethe.service"
    sudo systemctl daemon-reload 2>/dev/null || systemctl daemon-reload 2>/dev/null || true
    success "Systemd system service removed"
fi

# Stop and remove launchd service (Mac)
if [ -f "$HOME/Library/LaunchAgents/com.lethe.agent.plist" ]; then
    info "Stopping launchd service..."
    launchctl unload "$HOME/Library/LaunchAgents/com.lethe.agent.plist" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/com.lethe.agent.plist"
    success "Launchd service removed"
fi

# Stop and remove container
if command -v podman &>/dev/null; then
    if podman ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^lethe$'; then
        info "Removing Podman container..."
        podman stop lethe 2>/dev/null || true
        podman rm lethe 2>/dev/null || true
        podman rmi lethe:latest 2>/dev/null || true
        success "Podman container removed"
    fi
fi

if command -v docker &>/dev/null; then
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^lethe$'; then
        info "Removing Docker container..."
        docker stop lethe 2>/dev/null || true
        docker rm lethe 2>/dev/null || true
        docker rmi lethe:latest 2>/dev/null || true
        success "Docker container removed"
    fi
fi

# Remove installation directory
if [ -d "$INSTALL_DIR" ]; then
    info "Removing installation directory..."
    rm -rf "$INSTALL_DIR"
    success "Installation directory removed"
fi

echo ""
success "Lethe has been uninstalled."
echo ""
echo "Your config is preserved at:"
echo "  $CONFIG_DIR - API tokens and settings"
echo ""
echo "To reinstall:"
echo "  curl -fsSL https://lethe.gg/install | bash"
