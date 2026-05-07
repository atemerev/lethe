#!/usr/bin/env bash
#
# Lethe Container Setup
#
# Creates an isolated container for Lethe using the platform-native tool:
#   Linux:  podman (rootless OCI container)
#   macOS:  apple/container (requires macOS 26+)
#
# Usage: ./scripts/container-setup.sh [--rebuild]
#

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

prompt_read() {
    local prompt="$1"
    local var_name="$2"
    local value
    printf "\e[?2004l" > /dev/tty  # disable bracketed paste
    printf "%s" "$prompt" > /dev/tty
    IFS= read -r value < /dev/tty
    value="$(printf '%s' "$value" | tr -d '\r' | sed 's/[^[:print:]]//g' | xargs)"
    eval "$var_name=\$value"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
LETHE_HOME="${LETHE_HOME:-$HOME/.lethe}"
CONTAINER_NAME="lethe"
MOUNTS_CONF="$LETHE_HOME/config/mounts.conf"
REBUILD=0

for arg in "$@"; do
    case "$arg" in
        --rebuild) REBUILD=1 ;;
        --help|-h)
            echo "Usage: $0 [--rebuild]"
            echo ""
            echo "Sets up Lethe in an isolated container."
            echo "  --rebuild    Force rebuild even if container exists"
            echo ""
            echo "Linux: uses podman (rootless)"
            echo "macOS: uses apple/container (OCI image)"
            exit 0
            ;;
    esac
done

detect_os() {
    case "$(uname -s)" in
        Linux*)  echo "linux" ;;
        Darwin*) echo "mac" ;;
        *)       echo "unknown" ;;
    esac
}

# Map host arch to OCI arch names. Without this, apple/container has been
# observed to default to linux/amd64 on Apple Silicon, producing an x86_64
# rootfs that runs under emulation. Always pass it explicitly.
detect_arch() {
    case "$(uname -m)" in
        arm64|aarch64) echo "arm64" ;;
        x86_64|amd64)  echo "amd64" ;;
        *)             warn "Unknown host arch '$(uname -m)', defaulting to amd64"; echo "amd64" ;;
    esac
}

ARCH="$(detect_arch)"
PLATFORM="linux/${ARCH}"

# ---------------------------------------------------------------------------
# Directory mount configuration
# ---------------------------------------------------------------------------
prompt_mounts() {
    echo ""
    echo -e "${YELLOW}Directory access${NC}"
    echo ""
    echo "  Lethe runs in an isolated container. By default it can only"
    echo "  access its own data directory (~/.lethe)."
    echo ""
    echo "  You can give it access to additional directories on your system."
    echo "  These will appear at the same path inside the container"
    echo "  (under /home/lethe/)."
    echo ""

    # Suggest common directories
    local suggested_dirs=()
    for dir in Documents Downloads; do
        if [[ -d "$HOME/$dir" ]]; then
            suggested_dirs+=("$dir")
        fi
    done

    SELECTED_MOUNTS=()

    if [[ ${#suggested_dirs[@]} -gt 0 ]]; then
        echo -e "  ${BLUE}Detected directories:${NC}"
        for dir in "${suggested_dirs[@]}"; do
            prompt_read "    Mount ~/$dir? [Y/n]: " answer
            answer=${answer:-Y}
            if [[ "$answer" =~ ^[Yy] ]]; then
                SELECTED_MOUNTS+=("$dir")
                success "  ~/$dir"
            fi
        done
    fi

    # Custom directories
    echo ""
    echo "  You can also add custom directories (e.g. Projects, Music)."
    echo "  Leave blank to finish."
    echo ""
    while true; do
        prompt_read "  Additional directory (relative to ~, or absolute path): " custom
        custom="${custom%/}"  # strip trailing slash
        [[ -z "$custom" ]] && break

        # Resolve to absolute path
        local abs_path
        if [[ "$custom" = /* ]]; then
            abs_path="$custom"
        else
            abs_path="$HOME/$custom"
        fi

        if [[ ! -d "$abs_path" ]]; then
            warn "  $abs_path does not exist, skipping"
            continue
        fi

        SELECTED_MOUNTS+=("$custom")
        success "  $abs_path"
    done

    # Save mount config
    mkdir -p "$(dirname "$MOUNTS_CONF")"
    {
        echo "# Lethe container mount configuration"
        echo "# Format: directory_name (relative to \$HOME, or absolute path)"
        echo "# Edit and re-run container-setup.sh to apply changes."
        for mount in "${SELECTED_MOUNTS[@]}"; do
            echo "$mount"
        done
    } > "$MOUNTS_CONF"

    if [[ ${#SELECTED_MOUNTS[@]} -eq 0 ]]; then
        info "No additional directories mounted (Lethe can only access ~/.lethe)"
    else
        echo ""
        success "${#SELECTED_MOUNTS[@]} directories will be mounted"
    fi
}

load_mounts() {
    SELECTED_MOUNTS=()
    if [[ -f "$MOUNTS_CONF" ]]; then
        while IFS= read -r line; do
            [[ -z "$line" || "$line" = \#* ]] && continue
            SELECTED_MOUNTS+=("$line")
        done < "$MOUNTS_CONF"
    fi
}

# Resolve a mount entry to host_path:container_path
resolve_mount() {
    local entry="$1"
    local host_path container_path

    if [[ "$entry" = /* ]]; then
        host_path="$entry"
        container_path="/home/lethe${entry}"
    else
        host_path="$HOME/$entry"
        container_path="/home/lethe/$entry"
    fi

    echo "$host_path:$container_path"
}

# ---------------------------------------------------------------------------
# Stop and remove any existing Lethe services (native or container)
# ---------------------------------------------------------------------------
cleanup_old_services() {
    local found=0

    # Old native systemd user service
    if [[ -f "$HOME/.config/systemd/user/lethe.service" ]]; then
        info "Stopping old native service (systemd user)..."
        systemctl --user stop lethe 2>/dev/null || true
        systemctl --user disable lethe 2>/dev/null || true
        rm -f "$HOME/.config/systemd/user/lethe.service"
        systemctl --user daemon-reload 2>/dev/null || true
        success "Old native service removed"
        found=1
    fi

    # Old native systemd system service
    if [[ -f "/etc/systemd/system/lethe.service" ]]; then
        info "Stopping old native service (systemd system)..."
        sudo systemctl stop lethe 2>/dev/null || true
        sudo systemctl disable lethe 2>/dev/null || true
        sudo rm -f "/etc/systemd/system/lethe.service"
        sudo systemctl daemon-reload 2>/dev/null || true
        success "Old native system service removed"
        found=1
    fi

    # Old nspawn container service
    if [[ -f "/etc/systemd/system/lethe-container.service" ]]; then
        info "Stopping old nspawn container service..."
        sudo systemctl stop lethe-container 2>/dev/null || true
        sudo systemctl disable lethe-container 2>/dev/null || true
        sudo rm -f "/etc/systemd/system/lethe-container.service"
        sudo rm -f "/etc/systemd/nspawn/lethe.nspawn"
        sudo systemctl daemon-reload 2>/dev/null || true
        success "Old nspawn container service removed"
        found=1
    fi

    # Existing podman container service
    if [[ -f "$HOME/.config/systemd/user/lethe-container.service" ]]; then
        info "Stopping existing podman container service..."
        systemctl --user stop lethe-container 2>/dev/null || true
        systemctl --user disable lethe-container 2>/dev/null || true
        rm -f "$HOME/.config/systemd/user/lethe-container.service"
        systemctl --user daemon-reload 2>/dev/null || true
        success "Old podman container service removed"
        found=1
    fi

    # Stop and remove existing podman container
    if command -v podman &>/dev/null && podman container exists "$CONTAINER_NAME" 2>/dev/null; then
        info "Removing existing podman container..."
        podman stop "$CONTAINER_NAME" 2>/dev/null || true
        podman rm "$CONTAINER_NAME" 2>/dev/null || true
        success "Old podman container removed"
        found=1
    fi

    # Old native launchd service
    if [[ -f "$HOME/Library/LaunchAgents/com.lethe.agent.plist" ]]; then
        info "Stopping old native service (launchd)..."
        launchctl unload "$HOME/Library/LaunchAgents/com.lethe.agent.plist" 2>/dev/null || true
        rm -f "$HOME/Library/LaunchAgents/com.lethe.agent.plist"
        success "Old native launchd service removed"
        found=1
    fi

    # Existing container launchd service
    if [[ -f "$HOME/Library/LaunchAgents/com.lethe.container.plist" ]]; then
        info "Stopping existing container service (launchd)..."
        launchctl unload "$HOME/Library/LaunchAgents/com.lethe.container.plist" 2>/dev/null || true
        rm -f "$HOME/Library/LaunchAgents/com.lethe.container.plist"
        success "Old container launchd service removed"
        found=1
    fi

    if [[ "$found" == "1" ]]; then
        echo ""
    fi
}

# ---------------------------------------------------------------------------
# macOS: build OCI image via apple/container
# ---------------------------------------------------------------------------
ensure_container_system() {
    if ! container system status &>/dev/null; then
        info "Starting container system service..."
        container system start
        success "Container system service started"
    fi
}

build_image_apple() {
    command -v container >/dev/null 2>&1 || error "'container' CLI not found. Install: brew install container (requires macOS 26+)"
    ensure_container_system
    info "Building container image (arch: $ARCH)..."
    container build --arch "$ARCH" -t lethe:latest -f "$REPO_DIR/Containerfile" "$REPO_DIR"
    success "Container image built"
}

# ---------------------------------------------------------------------------
# Linux: podman (rootless)
# ---------------------------------------------------------------------------
setup_podman() {
    command -v podman >/dev/null 2>&1 || error "podman not found. Install it with your package manager (e.g. apt install podman, dnf install podman)"

    if podman image exists lethe:latest 2>/dev/null && [[ "$REBUILD" == "0" ]]; then
        info "Image lethe:latest already exists (use --rebuild to recreate)"
    else
        info "Building container image (platform: $PLATFORM)..."
        podman build --platform "$PLATFORM" -t lethe:latest -f "$REPO_DIR/Containerfile" "$REPO_DIR"
        success "Container image built"
    fi

    # Enable lingering so user services run without login session
    if command -v loginctl &>/dev/null; then
        loginctl enable-linger "$(whoami)" 2>/dev/null \
            || sudo loginctl enable-linger "$(whoami)" 2>/dev/null \
            || warn "Could not enable lingering — service may stop when you log out"
    fi

    # Create systemd user service
    local podman_bin
    podman_bin="$(command -v podman)"
    mkdir -p "$HOME/.config/systemd/user"

    local svc="$HOME/.config/systemd/user/lethe-container.service"
    {
        echo "[Unit]"
        echo "Description=Lethe Autonomous AI Agent (podman)"
        echo "After=network-online.target"
        echo ""
        echo "[Service]"
        echo "Type=simple"
        echo "ExecStartPre=-$podman_bin rm -f $CONTAINER_NAME"
        printf "ExecStart=$podman_bin run --rm --name $CONTAINER_NAME"
        printf " \\\\\n    --userns=keep-id"
        printf " \\\\\n    --security-opt label=disable"
        printf " \\\\\n    --env LETHE_HOME=/home/lethe/.lethe"
        printf " \\\\\n    -v $LETHE_HOME:/home/lethe/.lethe"
        printf " \\\\\n    -v $LETHE_HOME/config/.env:/opt/lethe/.env:ro"
        for mount in "${SELECTED_MOUNTS[@]}"; do
            local pair
            pair=$(resolve_mount "$mount")
            printf " \\\\\n    -v $pair"
        done
        printf " \\\\\n    lethe:latest\n"
        echo "ExecStop=$podman_bin stop $CONTAINER_NAME"
        echo "Restart=always"
        echo "RestartSec=10"
        echo ""
        echo "[Install]"
        echo "WantedBy=default.target"
    } > "$svc"

    systemctl --user daemon-reload
    systemctl --user enable lethe-container
    systemctl --user start lethe-container
    success "Podman container started"
    echo ""
    echo "  Image:     lethe:latest"
    echo "  Container: $CONTAINER_NAME"
    echo "  Service:   ~/.config/systemd/user/lethe-container.service"
    echo ""
    echo "  Commands:"
    echo "    Start:   systemctl --user start lethe-container"
    echo "    Stop:    systemctl --user stop lethe-container"
    echo "    Logs:    journalctl --user -u lethe-container -f"
    echo "    Shell:   podman exec -it $CONTAINER_NAME /bin/bash"
    echo "    Root:    podman exec -u 0 -it $CONTAINER_NAME /bin/bash"
}

# ---------------------------------------------------------------------------
# macOS: apple/container
# ---------------------------------------------------------------------------
setup_apple_container() {
    local container_bin
    container_bin="$(command -v container)"

    if container image ls 2>/dev/null | grep -q "lethe" && [[ "$REBUILD" == "0" ]]; then
        info "Image lethe:latest already exists (use --rebuild to recreate)"
    else
        build_image_apple
    fi

    # Create launch script
    local launch_script="$LETHE_HOME/run-container.sh"
    {
        echo '#!/usr/bin/env bash'
        printf '"%s" system status &>/dev/null || "%s" system start\n' "$container_bin" "$container_bin"
        printf 'exec "%s" run \\\n' "$container_bin"
        printf '    --arch %s \\\n' "$ARCH"
        printf '    --memory 4G \\\n'
        printf '    --env LETHE_HOME=/home/lethe/.lethe \\\n'
        printf '    --volume "%s:/home/lethe/.lethe" \\\n' "$LETHE_HOME"
        printf '    --volume "%s/config/.env:/opt/lethe/.env:ro" \\\n' "$LETHE_HOME"
        for mount in "${SELECTED_MOUNTS[@]}"; do
            local pair
            pair=$(resolve_mount "$mount")
            printf '    --volume "%s" \\\n' "$pair"
        done
        echo '    lethe:latest'
    } > "$launch_script"
    chmod +x "$launch_script"

    _write_launchd_plist "$launch_script" "$(dirname "$container_bin")"

    launchctl load "$HOME/Library/LaunchAgents/com.lethe.container.plist"
    success "apple/container started"
    echo ""
    echo "  Launcher:  $launch_script"
    echo "  Service:   ~/Library/LaunchAgents/com.lethe.container.plist"
    echo ""
    echo "  Commands:"
    echo "    Start:   launchctl load ~/Library/LaunchAgents/com.lethe.container.plist"
    echo "    Stop:    launchctl unload ~/Library/LaunchAgents/com.lethe.container.plist"
    echo "    Logs:    tail -f $LETHE_HOME/logs/container.log"
    echo "    Shell:   container run --arch $ARCH --volume $LETHE_HOME:/home/lethe/.lethe -it lethe:latest /bin/bash"
}

# ---------------------------------------------------------------------------
# macOS: podman (fallback for Intel Macs / pre-Sequoia)
# ---------------------------------------------------------------------------
setup_podman_mac() {
    command -v podman >/dev/null 2>&1 || error "podman not found. Install: brew install podman"
    local podman_bin
    podman_bin="$(command -v podman)"

    # Ensure podman machine is initialized and running
    if ! podman machine inspect &>/dev/null; then
        info "Initializing podman machine..."
        podman machine init --cpus 2 --memory 4096
    fi
    if ! podman machine inspect --format '{{.State}}' 2>/dev/null | grep -qi "running"; then
        info "Starting podman machine..."
        podman machine start
    fi
    success "Podman machine running"

    if podman image exists lethe:latest 2>/dev/null && [[ "$REBUILD" == "0" ]]; then
        info "Image lethe:latest already exists (use --rebuild to recreate)"
    else
        info "Building container image (platform: $PLATFORM)..."
        podman build --platform "$PLATFORM" -t lethe:latest -f "$REPO_DIR/Containerfile" "$REPO_DIR"
        success "Container image built"
    fi

    # Create launch script
    local launch_script="$LETHE_HOME/run-container.sh"
    {
        echo '#!/usr/bin/env bash'
        printf '"%s" machine inspect --format '\''{{.State}}'\'' 2>/dev/null | grep -qi running || "%s" machine start\n' "$podman_bin" "$podman_bin"
        printf 'exec "%s" run --rm --name lethe \\\n' "$podman_bin"
        printf '    --userns=keep-id \\\n'
        printf '    --env LETHE_HOME=/home/lethe/.lethe \\\n'
        printf '    -v "%s:/home/lethe/.lethe" \\\n' "$LETHE_HOME"
        printf '    -v "%s/config/.env:/opt/lethe/.env:ro" \\\n' "$LETHE_HOME"
        for mount in "${SELECTED_MOUNTS[@]}"; do
            local pair
            pair=$(resolve_mount "$mount")
            printf '    -v "%s" \\\n' "$pair"
        done
        echo '    lethe:latest'
    } > "$launch_script"
    chmod +x "$launch_script"

    _write_launchd_plist "$launch_script" "$(dirname "$podman_bin")"

    launchctl load "$HOME/Library/LaunchAgents/com.lethe.container.plist"
    success "Podman container started"
    echo ""
    echo "  Launcher:  $launch_script"
    echo "  Service:   ~/Library/LaunchAgents/com.lethe.container.plist"
    echo ""
    echo "  Commands:"
    echo "    Start:   launchctl load ~/Library/LaunchAgents/com.lethe.container.plist"
    echo "    Stop:    launchctl unload ~/Library/LaunchAgents/com.lethe.container.plist"
    echo "    Logs:    tail -f $LETHE_HOME/logs/container.log"
    echo "    Shell:   podman exec -it lethe /bin/bash"
    echo "    Root:    podman exec -u 0 -it lethe /bin/bash"
}

# ---------------------------------------------------------------------------
# Shared: write launchd plist for macOS container service
# ---------------------------------------------------------------------------
_write_launchd_plist() {
    local launch_script="$1"
    local bin_dir="$2"

    mkdir -p "$HOME/Library/LaunchAgents"
    mkdir -p "$LETHE_HOME/logs"
    cat > "$HOME/Library/LaunchAgents/com.lethe.container.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.lethe.container</string>
    <key>ProgramArguments</key>
    <array>
        <string>$launch_script</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LETHE_HOME/logs/container.log</string>
    <key>StandardErrorPath</key>
    <string>$LETHE_HOME/logs/container.error.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$bin_dir:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
EOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    echo -e "${BLUE}Lethe Container Setup${NC}"
    echo ""

    # Ensure lethe home and dirs exist
    mkdir -p "$LETHE_HOME"/{config,data,logs,workspace,cache,credentials}

    # Mount configuration
    if [[ -f "$MOUNTS_CONF" && "$REBUILD" == "0" ]]; then
        info "Using existing mount config: $MOUNTS_CONF"
        load_mounts
        if [[ ${#SELECTED_MOUNTS[@]} -gt 0 ]]; then
            echo "  Mounted directories:"
            for mount in "${SELECTED_MOUNTS[@]}"; do
                local pair
                pair=$(resolve_mount "$mount")
                echo "    ${pair//:/ → }"
            done
        fi
        echo ""
        prompt_read "Reconfigure mounts? [y/N]: " reconf
        if [[ "$reconf" =~ ^[Yy] ]]; then
            prompt_mounts
        fi
    else
        prompt_mounts
    fi

    # Stop and remove any previous services before installing new one
    cleanup_old_services

    # Platform-specific setup
    local os=$(detect_os)
    case "$os" in
        linux)
            info "Platform: Linux (podman)"
            setup_podman
            ;;
        mac)
            if command -v container >/dev/null 2>&1; then
                info "Platform: macOS (apple/container)"
                setup_apple_container
            elif command -v podman >/dev/null 2>&1; then
                info "Platform: macOS (podman)"
                setup_podman_mac
            else
                error "No container runtime found. Install apple/container (macOS 26+: brew install container) or podman (brew install podman)"
            fi
            ;;
        *)
            error "Unsupported platform: $(uname -s)"
            ;;
    esac

    echo ""
    success "Container setup complete"
    echo ""
    echo "  Lethe home:  $LETHE_HOME → /home/lethe/.lethe"
    if [[ ${#SELECTED_MOUNTS[@]} -gt 0 ]]; then
        echo "  Directories:"
        for mount in "${SELECTED_MOUNTS[@]}"; do
            local pair
            pair=$(resolve_mount "$mount")
            echo "    ${pair//:/ → }"
        done
    fi
    echo "  Mounts config: $MOUNTS_CONF"
    echo ""
    echo "  Host filesystem is isolated except for the directories above."
}

main "$@"
