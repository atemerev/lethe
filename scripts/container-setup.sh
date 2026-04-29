#!/usr/bin/env bash
#
# Lethe Container Setup
#
# Creates an isolated container for Lethe using the platform-native tool:
#   Linux:  systemd-nspawn (rootfs via dnf --installroot)
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
MACHINE_NAME="lethe"
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
            echo "Linux: uses systemd-nspawn (rootfs at /var/lib/machines/lethe)"
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

    # Existing container service (from previous container install)
    if [[ -f "/etc/systemd/system/lethe-container.service" ]]; then
        info "Stopping existing container service..."
        sudo systemctl stop lethe-container 2>/dev/null || true
        sudo systemctl disable lethe-container 2>/dev/null || true
        sudo rm -f "/etc/systemd/system/lethe-container.service"
        sudo rm -f "/etc/systemd/nspawn/lethe.nspawn"
        sudo systemctl daemon-reload 2>/dev/null || true
        success "Old container service removed"
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
# Build container image
# ---------------------------------------------------------------------------
ensure_container_system() {
    if ! container system status &>/dev/null; then
        info "Starting container system service..."
        container system start
        success "Container system service started"
    fi
}

build_image() {
    command -v container >/dev/null 2>&1 || error "'container' CLI not found. Install: brew install container (requires macOS 26+)"
    ensure_container_system
    info "Building container image..."
    container build -t lethe:latest -f "$REPO_DIR/Containerfile" "$REPO_DIR"
    success "Container image built"
}

# ---------------------------------------------------------------------------
# Linux: systemd-nspawn
# ---------------------------------------------------------------------------
setup_nspawn() {
    command -v systemd-nspawn >/dev/null 2>&1 || error "systemd-nspawn not found. Install: sudo dnf install systemd-container"
    command -v dnf >/dev/null 2>&1 || error "dnf is required to bootstrap the container rootfs"

    local fedora_release
    fedora_release="$(rpm -E '%{fedora}' 2>/dev/null || echo 43)"
    local rootfs="/var/lib/machines/$MACHINE_NAME"

    if [[ -d "$rootfs" && "$REBUILD" == "0" ]]; then
        info "Container rootfs exists at $rootfs (use --rebuild to recreate)"
    else
        if [[ -d "$rootfs" ]]; then
            info "Removing existing rootfs..."
            sudo rm -rf "$rootfs"
        fi

        info "Bootstrapping Fedora rootfs..."
        sudo mkdir -p "$rootfs"
        sudo dnf --installroot="$rootfs" --releasever="$fedora_release" \
            --setopt=install_weak_deps=False -y \
            install coreutils-single bash python3.12 git-core curl findutils
        sudo dnf --installroot="$rootfs" clean all

        info "Installing uv..."
        sudo systemd-nspawn -D "$rootfs" bash -c \
            'curl -LsSf https://astral.sh/uv/install.sh | sh && ln -sf /root/.local/bin/uv /usr/local/bin/uv'

        info "Creating lethe user..."
        sudo systemd-nspawn -D "$rootfs" useradd -m -d /home/lethe -s /bin/bash lethe 2>/dev/null || true

        info "Copying project files..."
        sudo mkdir -p "$rootfs/opt/lethe"
        sudo cp "$REPO_DIR"/pyproject.toml "$REPO_DIR"/uv.lock "$rootfs/opt/lethe/"
        sudo cp -r "$REPO_DIR"/src "$rootfs/opt/lethe/"

        info "Installing Python dependencies..."
        sudo systemd-nspawn -D "$rootfs" bash -c \
            'cd /opt/lethe && uv sync --frozen && chown -R lethe:lethe /opt/lethe'

        success "Rootfs created at $rootfs"
    fi

    # Generate .nspawn unit with mount config
    local nspawn_file="/etc/systemd/nspawn/$MACHINE_NAME.nspawn"
    sudo mkdir -p /etc/systemd/nspawn

    local bind_lines=""
    # Always mount .lethe
    bind_lines+="Bind=$LETHE_HOME:/home/lethe/.lethe\n"
    # .env into project dir (read-only)
    bind_lines+="BindReadOnly=$LETHE_HOME/config/.env:/opt/lethe/.env\n"
    # User directories
    for mount in "${SELECTED_MOUNTS[@]}"; do
        local pair
        pair=$(resolve_mount "$mount")
        bind_lines+="Bind=${pair}\n"
    done

    sudo tee "$nspawn_file" > /dev/null << EOF
[Exec]
Boot=no
User=lethe
WorkingDirectory=/opt/lethe
Parameters=/usr/local/bin/uv run lethe
Environment=HOME=/home/lethe
Environment=LETHE_HOME=/home/lethe/.lethe

[Files]
$(echo -e "$bind_lines")
[Network]
VirtualEthernet=no
EOF

    # Create systemd service
    sudo tee /etc/systemd/system/lethe-container.service > /dev/null << EOF
[Unit]
Description=Lethe Autonomous AI Agent (container)
After=network.target

[Service]
Type=simple
ExecStart=systemd-nspawn --machine=$MACHINE_NAME --quiet
Restart=always
RestartSec=10
KillMode=mixed

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable lethe-container
    sudo systemctl start lethe-container
    success "nspawn container started"
    echo ""
    echo "  Rootfs:    $rootfs"
    echo "  Config:    $nspawn_file"
    echo "  Service:   lethe-container.service"
    echo ""
    echo "  Commands:"
    echo "    Start:   sudo systemctl start lethe-container"
    echo "    Stop:    sudo systemctl stop lethe-container"
    echo "    Logs:    sudo journalctl -u lethe-container -f"
    echo "    Shell:   sudo systemd-nspawn -M $MACHINE_NAME --user lethe /bin/bash"
    echo "    Install: sudo systemd-nspawn -M $MACHINE_NAME dnf install <pkg>"
}

# ---------------------------------------------------------------------------
# macOS: apple/container
# ---------------------------------------------------------------------------
setup_apple_container() {
    command -v container >/dev/null 2>&1 || error "'container' CLI not found. Install: brew install container (requires macOS 26+)"
    local container_bin
    container_bin="$(command -v container)"

    if container image ls 2>/dev/null | grep -q "lethe" && [[ "$REBUILD" == "0" ]]; then
        info "Image lethe:latest already exists (use --rebuild to recreate)"
    else
        build_image
    fi

    # Create launch script
    local launch_script="$LETHE_HOME/run-container.sh"
    {
        echo '#!/usr/bin/env bash'
        printf '"%s" system status &>/dev/null || "%s" system start\n' "$container_bin" "$container_bin"
        printf 'exec "%s" run \\\n' "$container_bin"
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

    # Create launchd plist
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
        <string>$(dirname "$container_bin"):/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
EOF

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
    echo "    Shell:   container run --volume $LETHE_HOME:/home/lethe/.lethe -it lethe:latest /bin/bash"
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
            info "Platform: Linux (systemd-nspawn)"
            setup_nspawn
            ;;
        mac)
            info "Platform: macOS (apple/container)"
            setup_apple_container
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
    echo "  The container can install software via dnf."
    echo "  Host filesystem is isolated except for the directories above."
}

main "$@"
