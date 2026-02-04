#!/usr/bin/env bash
#
# Lethe Update Script
# Checks for updates and applies them
#
# Usage: curl -fsSL https://lethe.gg/update | bash
#
# Container mode: clones to temp dir, rebuilds, restarts container
# Native mode: updates install dir, restarts service
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Config
REPO_URL="https://github.com/atemerev/lethe.git"
REPO_OWNER="atemerev"
REPO_NAME="lethe"
CONFIG_DIR="${LETHE_CONFIG_DIR:-$HOME/.config/lethe}"

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

print_header() {
    echo -e "${BLUE}"
    echo "╔═══════════════════════════════════════════════════════════╗"
    echo "║                   LETHE UPDATE                            ║"
    echo "╚═══════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

get_latest_release() {
    local latest=$(curl -fsSL "https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/releases/latest" 2>/dev/null | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/')
    if [ -z "$latest" ]; then
        echo "main"
    else
        echo "$latest"
    fi
}

get_container_version() {
    local container_cmd="$1"
    # Try to get version from container label or just return "unknown"
    $container_cmd inspect lethe --format '{{index .Config.Labels "version"}}' 2>/dev/null || echo "unknown"
}

detect_install_mode() {
    # Check container mode FIRST
    if command -v podman &>/dev/null; then
        if podman ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^lethe$'; then
            echo "container-podman"
            return
        fi
    fi
    
    if command -v docker &>/dev/null; then
        if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^lethe$'; then
            echo "container-docker"
            return
        fi
    fi
    
    # Check for systemd service (Linux native)
    if [ -f "$HOME/.config/systemd/user/lethe.service" ]; then
        echo "native-systemd"
        return
    fi
    
    # Check for launchd service (Mac native)
    if [ -f "$HOME/Library/LaunchAgents/com.lethe.agent.plist" ]; then
        echo "native-launchd"
        return
    fi
    
    echo "unknown"
}

detect_install_dir() {
    # Only called for native mode
    
    # 1. Check environment variable
    if [ -n "${LETHE_INSTALL_DIR:-}" ] && [ -d "$LETHE_INSTALL_DIR/.git" ]; then
        echo "$LETHE_INSTALL_DIR"
        return
    fi
    
    # 2. Check if running from within install dir
    local script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "$script_dir/pyproject.toml" ] && grep -q "lethe" "$script_dir/pyproject.toml" 2>/dev/null; then
        echo "$script_dir"
        return
    fi
    
    # 3. Check systemd service for WorkingDirectory
    if [ -f "$HOME/.config/systemd/user/lethe.service" ]; then
        local wd=$(grep "WorkingDirectory=" "$HOME/.config/systemd/user/lethe.service" 2>/dev/null | cut -d= -f2)
        if [ -n "$wd" ] && [ -d "$wd/.git" ]; then
            echo "$wd"
            return
        fi
    fi
    
    # 4. Check launchd plist for WorkingDirectory
    if [ -f "$HOME/Library/LaunchAgents/com.lethe.agent.plist" ]; then
        local wd=$(grep -A1 "WorkingDirectory" "$HOME/Library/LaunchAgents/com.lethe.agent.plist" 2>/dev/null | tail -1 | sed 's/.*<string>\(.*\)<\/string>.*/\1/')
        if [ -n "$wd" ] && [ -d "$wd/.git" ]; then
            echo "$wd"
            return
        fi
    fi
    
    # 5. Default install location (NOT ~/lethe - that's workspace/memory!)
    if [ -d "$HOME/.lethe/.git" ] && [ -f "$HOME/.lethe/pyproject.toml" ]; then
        echo "$HOME/.lethe"
        return
    fi
    
    # Not found
    echo ""
}

get_native_version() {
    local install_dir="$1"
    if [ -d "$install_dir/.git" ]; then
        cd "$install_dir"
        local tag=$(git describe --tags --exact-match 2>/dev/null || echo "")
        if [ -n "$tag" ]; then
            echo "$tag"
        else
            git rev-parse --short HEAD 2>/dev/null || echo "unknown"
        fi
    else
        echo "unknown"
    fi
}

update_container() {
    local container_cmd="$1"
    local latest_version="$2"
    local config_file="$CONFIG_DIR/container.env"
    local workspace_dir="${LETHE_WORKSPACE_DIR:-$HOME/lethe}"
    
    # Clone to temp directory
    local tmp_dir=$(mktemp -d)
    trap "rm -rf $tmp_dir" EXIT
    
    info "Cloning latest version..."
    git clone --depth 1 --branch "$latest_version" "$REPO_URL" "$tmp_dir" 2>/dev/null || \
        git clone --depth 1 "$REPO_URL" "$tmp_dir"
    
    info "Stopping container..."
    $container_cmd stop lethe 2>/dev/null || true
    $container_cmd rm lethe 2>/dev/null || true
    
    info "Rebuilding container image..."
    cd "$tmp_dir"
    $container_cmd build -t lethe:latest --label "version=$latest_version" .
    
    info "Starting container..."
    if [ ! -f "$config_file" ]; then
        error "Config file not found: $config_file"
    fi
    
    if [[ "$container_cmd" == "podman" ]]; then
        $container_cmd run -d \
            --name lethe \
            --restart unless-stopped \
            --userns=keep-id \
            --env-file "$config_file" \
            -v "$workspace_dir:/workspace:Z" \
            lethe:latest
    else
        $container_cmd run -d \
            --name lethe \
            --restart unless-stopped \
            --user "$(id -u):$(id -g)" \
            --env-file "$config_file" \
            -v "$workspace_dir:/workspace" \
            lethe:latest
    fi
    
    success "Container updated and restarted!"
    echo ""
    echo "  View logs: $container_cmd logs -f lethe"
    echo ""
}

update_native() {
    local install_dir="$1"
    local target_version="$2"
    
    cd "$install_dir"
    
    info "Fetching updates..."
    git fetch origin --tags
    
    if [ "$target_version" != "main" ]; then
        git checkout "$target_version"
    else
        git checkout main
        git pull origin main
    fi
    
    # Update dependencies
    info "Updating dependencies..."
    if command -v uv &>/dev/null; then
        uv sync
    fi
    
    success "Code updated to $target_version"
}

main() {
    print_header
    
    # Detect install mode first
    local install_mode=$(detect_install_mode)
    local latest_version=$(get_latest_release)
    local current_version="unknown"
    
    echo "  Install mode:   $install_mode"
    
    case "$install_mode" in
        container-podman|container-docker)
            local container_cmd="${install_mode#container-}"
            current_version=$(get_container_version "$container_cmd")
            
            echo "  Latest version: $latest_version"
            echo ""
            
            info "Updating container..."
            update_container "$container_cmd" "$latest_version"
            success "Update complete!"
            ;;
            
        native-systemd|native-launchd)
            local install_dir=$(detect_install_dir)
            
            if [ -z "$install_dir" ] || [ ! -d "$install_dir" ]; then
                error "Could not find Lethe installation directory"
            fi
            
            current_version=$(get_native_version "$install_dir")
            
            echo "  Install dir:    $install_dir"
            echo "  Current:        $current_version"
            echo "  Latest:         $latest_version"
            echo ""
            
            if [ "$current_version" == "$latest_version" ]; then
                success "Already up to date!"
                exit 0
            fi
            
            info "Update available: $current_version → $latest_version"
            echo ""
            
            update_native "$install_dir" "$latest_version"
            
            if [[ "$install_mode" == "native-systemd" ]]; then
                info "Restarting systemd service..."
                systemctl --user restart lethe
                success "Service restarted!"
                echo ""
                echo "  View logs: journalctl --user -u lethe -f"
            else
                info "Restarting launchd service..."
                launchctl unload "$HOME/Library/LaunchAgents/com.lethe.agent.plist" 2>/dev/null || true
                launchctl load "$HOME/Library/LaunchAgents/com.lethe.agent.plist"
                success "Service restarted!"
                echo ""
                echo "  View logs: tail -f ~/Library/Logs/lethe.log"
            fi
            echo ""
            success "Update complete! ($current_version → $latest_version)"
            ;;
            
        *)
            error "Could not detect Lethe installation. Is Lethe installed?"
            ;;
    esac
}

main "$@"
