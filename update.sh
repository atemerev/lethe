#!/usr/bin/env bash
#
# Lethe Update Script
# Checks for updates and applies them
#
# Usage: curl -fsSL https://lethe.gg/update | bash
#

set -euo pipefail

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
LETHE_HOME="${LETHE_HOME:-$HOME/.lethe}"
CONFIG_DIR="$LETHE_HOME/config"

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
    local latest
    latest="$(curl -fsSL "https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/releases/latest" 2>/dev/null | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/' || true)"
    if [ -z "$latest" ]; then
        echo "main"
    else
        echo "$latest"
    fi
}

detect_install_mode() {
    # Root user: always use system-level service
    if [[ "$(id -u)" -eq 0 ]]; then
        if [ -f "$HOME/.config/systemd/user/lethe.service" ]; then
            rm -f "$HOME/.config/systemd/user/lethe.service"
        fi
        if [ -f "/etc/systemd/system/lethe.service" ]; then
            echo "native-systemd-system"
            return
        fi
        echo "native-systemd-system-new"
        return
    fi

    # Non-root: check for systemd user service
    if [ -f "$HOME/.config/systemd/user/lethe.service" ]; then
        echo "native-systemd-user"
        return
    fi

    # Check for launchd service (Mac)
    if [ -f "$HOME/Library/LaunchAgents/com.lethe.agent.plist" ]; then
        echo "native-launchd"
        return
    fi

    echo "unknown"
}

detect_install_dir() {
    # 1. Check environment variable
    if [ -n "${LETHE_INSTALL_DIR:-}" ] && [ -d "$LETHE_INSTALL_DIR/.git" ]; then
        echo "$LETHE_INSTALL_DIR"
        return
    fi

    # 2. Check if running from within install dir
    local script_dir="$(cd "$(dirname "$0")" && pwd)"
    if [ -f "$script_dir/pyproject.toml" ] && grep -q "lethe" "$script_dir/pyproject.toml" 2>/dev/null; then
        echo "$script_dir"
        return
    fi

    # 3. Check systemd system service for WorkingDirectory (root installs)
    if [ -f "/etc/systemd/system/lethe.service" ]; then
        local wd
        wd="$(grep "WorkingDirectory=" "/etc/systemd/system/lethe.service" 2>/dev/null | cut -d= -f2 || true)"
        if [ -n "$wd" ] && [ -d "$wd/.git" ]; then
            echo "$wd"
            return
        fi
    fi

    # 4. Check systemd user service for WorkingDirectory
    if [ -f "$HOME/.config/systemd/user/lethe.service" ]; then
        local wd
        wd="$(grep "WorkingDirectory=" "$HOME/.config/systemd/user/lethe.service" 2>/dev/null | cut -d= -f2 || true)"
        if [ -n "$wd" ] && [ -d "$wd/.git" ]; then
            echo "$wd"
            return
        fi
    fi

    # 5. Check launchd plist for WorkingDirectory
    if [ -f "$HOME/Library/LaunchAgents/com.lethe.agent.plist" ]; then
        local wd
        wd="$(grep -A1 "WorkingDirectory" "$HOME/Library/LaunchAgents/com.lethe.agent.plist" 2>/dev/null | tail -1 | sed 's/.*<string>\(.*\)<\/string>.*/\1/' || true)"
        if [ -n "$wd" ] && [ -d "$wd/.git" ]; then
            echo "$wd"
            return
        fi
    fi

    # 6. Default install location
    if [ -d "$LETHE_HOME/install/.git" ] && [ -f "$LETHE_HOME/install/pyproject.toml" ]; then
        echo "$LETHE_HOME/install"
        return
    fi

    # Legacy location
    if [ -d "$HOME/.lethe/.git" ] && [ -f "$HOME/.lethe/pyproject.toml" ]; then
        echo "$HOME/.lethe"
        return
    fi

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

update_native() {
    local install_dir="$1"
    local target_version="$2"

    cd "$install_dir"

    local stash_ref=""
    local stash_label=""
    local dirty=""
    dirty="$(git status --porcelain 2>/dev/null || true)"
    if [ -n "$dirty" ]; then
        stash_label="lethe-update-backup-$(date +%Y%m%d-%H%M%S)"
        warn "Local changes detected. Creating safety stash before update..."
        git stash push --include-untracked -m "$stash_label" >/dev/null
        stash_ref="$(git stash list --format='%gd %s' | awk -v label="$stash_label" '$0 ~ label {print $1; exit}')"
        if [ -n "$stash_ref" ]; then
            success "Backup created: $stash_ref"
        else
            warn "Backup stash created, but reference lookup failed (label: $stash_label)"
        fi
    fi

    restore_stash_on_failure() {
        if [ -z "$stash_ref" ]; then
            return
        fi
        warn "Update failed. Attempting to restore local changes from $stash_ref..."
        if git stash pop --index "$stash_ref" >/dev/null 2>&1; then
            success "Local changes restored from $stash_ref"
        else
            warn "Could not auto-restore stash cleanly. Backup remains in stash list ($stash_ref)."
            warn "Restore manually with: git -C $install_dir stash pop --index $stash_ref"
        fi
    }

    info "Fetching updates..."
    if ! git fetch origin --tags; then
        restore_stash_on_failure
        return 1
    fi

    if [ "$target_version" != "main" ]; then
        if ! git checkout "$target_version"; then
            restore_stash_on_failure
            return 1
        fi
    else
        if ! git checkout main; then
            restore_stash_on_failure
            return 1
        fi
        if ! git pull origin main; then
            restore_stash_on_failure
            return 1
        fi
    fi

    # Update dependencies
    info "Updating dependencies..."
    if command -v uv &>/dev/null; then
        if ! uv sync; then
            restore_stash_on_failure
            return 1
        fi
    fi
    if [ -n "$stash_ref" ]; then
        warn "Local pre-update changes were backed up to $stash_ref"
        warn "Reapply manually if needed: git -C $install_dir stash pop --index $stash_ref"
    fi

    success "Code updated to $target_version"
}

main() {
    print_header

    local install_mode=$(detect_install_mode)
    local latest_version=$(get_latest_release)
    local current_version="unknown"

    echo "  Install mode:   $install_mode"

    case "$install_mode" in
        native-systemd-system|native-systemd-system-new|native-systemd-user|native-launchd)
            local install_dir=$(detect_install_dir)

            if [ -z "$install_dir" ] || [ ! -d "$install_dir" ]; then
                error "Could not find Lethe installation directory. Set LETHE_INSTALL_DIR or LETHE_HOME."
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

            info "Update available: $current_version -> $latest_version"
            echo ""

            update_native "$install_dir" "$latest_version"

            # Migrate aux model: gemini-flash -> qwen3-coder-next (v0.4.1+)
            local env_file="$CONFIG_DIR/.env"
            if [ -f "$env_file" ] && grep -q "gemini.*flash" "$env_file"; then
                info "Migrating aux model: gemini-flash -> qwen3-coder-next"
                sed -i.bak 's|LLM_MODEL_AUX=.*gemini.*flash.*|LLM_MODEL_AUX=openrouter/qwen/qwen3-coder-next|' "$env_file"
                success "Aux model updated"
            fi

            if [[ "$install_mode" == "native-systemd-system" ]]; then
                info "Restarting systemd system service..."
                systemctl restart lethe
                success "Service restarted!"
                echo ""
                echo "  View logs: journalctl -u lethe -f"
            elif [[ "$install_mode" == "native-systemd-system-new" ]]; then
                info "Creating systemd system service..."
                local uv_bin
                local uv_bin_dir
                uv_bin="$(command -v uv || true)"
                if [ -z "${uv_bin:-}" ]; then
                    error "uv not found in PATH; cannot create systemd service"
                fi
                uv_bin_dir="$(dirname "$uv_bin")"
                cat > "/etc/systemd/system/lethe.service" << EOF
[Unit]
Description=Lethe Autonomous AI Agent
After=network.target

[Service]
Type=simple
WorkingDirectory=$install_dir
ExecStart=$uv_bin run lethe
Restart=always
RestartSec=10
Environment="PATH=$uv_bin_dir:/usr/local/bin:/usr/bin:/bin"
Environment="HOME=/root"
Environment="LETHE_HOME=$LETHE_HOME"

[Install]
WantedBy=multi-user.target
EOF
                systemctl daemon-reload
                systemctl enable lethe
                systemctl start lethe
                success "System service created and started!"
                echo ""
                echo "  View logs: journalctl -u lethe -f"
            elif [[ "$install_mode" == "native-systemd-user" ]]; then
                info "Restarting systemd user service..."
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
                echo "  View logs: tail -f $LETHE_HOME/logs/lethe.log"
            fi
            echo ""
            success "Update complete! ($current_version -> $latest_version)"
            ;;

        *)
            error "Could not detect Lethe installation. Is Lethe installed?"
            ;;
    esac
}

main "$@"
