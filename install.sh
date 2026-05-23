#!/usr/bin/env bash
#
# Lethe Rust installer.
# Usage: curl -fsSL https://lethe.gg/install | bash
#
# Installs a native Rust binary into $LETHE_HOME/bin/lethe.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

REPO_URL="${LETHE_REPO_URL:-https://github.com/atemerev/lethe.git}"
REPO_OWNER="${LETHE_REPO_OWNER:-atemerev}"
REPO_NAME="${LETHE_REPO_NAME:-lethe}"
RELEASE_BASE_URL="${LETHE_RELEASE_BASE_URL:-https://github.com/$REPO_OWNER/$REPO_NAME/releases/latest/download}"
LETHE_HOME="${LETHE_HOME:-$HOME/.lethe}"
INSTALL_DIR="${LETHE_INSTALL_DIR:-$LETHE_HOME/install}"
CONFIG_DIR="$LETHE_HOME/config"
ENV_FILE="$CONFIG_DIR/.env"
BIN_DIR="$LETHE_HOME/bin"

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

prompt_read() {
    local prompt="$1"
    local var_name="$2"
    local default="${3:-}"
    local value
    if [ -n "$default" ]; then
        printf "%s [%s]: " "$prompt" "$default" > /dev/tty
    else
        printf "%s: " "$prompt" > /dev/tty
    fi
    IFS= read -r value < /dev/tty
    value="${value:-$default}"
    eval "$var_name=\$value"
}

print_header() {
    echo -e "${BLUE}"
    echo "╔═══════════════════════════════════════════════════════════╗"
    echo "║                     LETHE RUST                            ║"
    echo "║              Local AI assistant runtime                   ║"
    echo "╚═══════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

ensure_cargo() {
    if command -v cargo >/dev/null 2>&1; then
        return
    fi

    warn "Rust/Cargo is not installed."
    if command -v curl >/dev/null 2>&1; then
        info "Installing Rust through rustup..."
        curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs | sh -s -- -y
        # shellcheck disable=SC1091
        . "$HOME/.cargo/env"
    fi

    command -v cargo >/dev/null 2>&1 || error "Install Rust from https://rustup.rs and rerun this installer."
}

ensure_protoc() {
    if command -v protoc >/dev/null 2>&1; then
        return
    fi

    error "protoc is required by LanceDB. Install protobuf-compiler/libprotobuf-dev (Debian/Ubuntu), protobuf-compiler/protobuf-devel (Fedora), or protobuf (Homebrew), then rerun."
}

detect_release_target() {
    local os arch
    os="$(uname -s)"
    arch="$(uname -m)"
    case "$os:$arch" in
        Linux:x86_64|Linux:amd64) echo "x86_64-unknown-linux-gnu" ;;
        Linux:aarch64|Linux:arm64) echo "aarch64-unknown-linux-gnu" ;;
        Darwin:x86_64|Darwin:amd64) echo "x86_64-apple-darwin" ;;
        Darwin:aarch64|Darwin:arm64) echo "aarch64-apple-darwin" ;;
        *) return 1 ;;
    esac
}

install_release_binary() {
    local target url tmp archive binary

    if ! command -v curl >/dev/null 2>&1 || ! command -v tar >/dev/null 2>&1; then
        warn "curl and tar are required for binary install."
        return 1
    fi

    if ! target="$(detect_release_target)"; then
        warn "No binary release target for $(uname -s)/$(uname -m)."
        return 1
    fi

    url="$RELEASE_BASE_URL/lethe-$target.tar.gz"
    tmp="$(mktemp -d)"
    archive="$tmp/lethe.tar.gz"

    info "Downloading binary release: $url"
    if ! curl -fsSL "$url" -o "$archive"; then
        warn "Binary release download failed."
        rm -rf "$tmp"
        return 1
    fi

    if ! tar -xzf "$archive" -C "$tmp"; then
        warn "Binary release archive could not be unpacked."
        rm -rf "$tmp"
        return 1
    fi

    binary="$(find "$tmp" -type f -name lethe -perm -111 | head -n 1)"
    if [ -z "$binary" ]; then
        warn "Binary release archive did not contain an executable lethe binary."
        rm -rf "$tmp"
        return 1
    fi

    mkdir -p "$BIN_DIR"
    cp "$binary" "$BIN_DIR/lethe"
    chmod +x "$BIN_DIR/lethe"
    rm -rf "$tmp"
    success "Installed $BIN_DIR/lethe from binary release"
}

checkout_repo() {
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
    if [ -f "$script_dir/Cargo.toml" ] && grep -q 'name = "lethe"' "$script_dir/Cargo.toml" 2>/dev/null; then
        INSTALL_DIR="$script_dir"
        info "Using local checkout: $INSTALL_DIR"
        return
    fi

    if [ -d "$INSTALL_DIR/.git" ]; then
        info "Updating existing checkout: $INSTALL_DIR"
        git -C "$INSTALL_DIR" pull --ff-only
    else
        info "Cloning Lethe into $INSTALL_DIR"
        mkdir -p "$(dirname "$INSTALL_DIR")"
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
}

provider_defaults() {
    case "$1" in
        openrouter)
            DEFAULT_MODEL="openrouter/moonshotai/kimi-k2.6"
            DEFAULT_AUX="openrouter/google/gemini-3-flash-preview"
            KEY_ENV="OPENROUTER_API_KEY"
            ;;
        anthropic)
            DEFAULT_MODEL="claude-opus-4-6"
            DEFAULT_AUX="claude-haiku-4-5"
            KEY_ENV="ANTHROPIC_API_KEY"
            ;;
        openai)
            DEFAULT_MODEL="gpt-5.4"
            DEFAULT_AUX="gpt-5.4-nano"
            KEY_ENV="OPENAI_API_KEY"
            ;;
        local)
            DEFAULT_MODEL="openai/gemma-4-31B-it-Q8_0.gguf"
            DEFAULT_AUX="$DEFAULT_MODEL"
            KEY_ENV="OPENAI_API_KEY"
            ;;
        *)
            error "Unknown provider: $1"
            ;;
    esac
}

write_env_file() {
    mkdir -p "$CONFIG_DIR" "$LETHE_HOME/workspace" "$LETHE_HOME/data/memory" "$LETHE_HOME/cache" "$LETHE_HOME/logs" "$LETHE_HOME/credentials"

    if [ -f "$ENV_FILE" ]; then
        warn "Config already exists: $ENV_FILE"
        return
    fi

    echo ""
    echo "Select LLM provider:"
    echo "  1) OpenRouter"
    echo "  2) Anthropic"
    echo "  3) OpenAI"
    echo "  4) Local OpenAI-compatible server"
    prompt_read "Provider" PROVIDER_CHOICE "1"

    case "$PROVIDER_CHOICE" in
        1|openrouter) PROVIDER="openrouter" ;;
        2|anthropic) PROVIDER="anthropic" ;;
        3|openai) PROVIDER="openai" ;;
        4|local) PROVIDER="local" ;;
        *) error "Invalid provider selection: $PROVIDER_CHOICE" ;;
    esac

    provider_defaults "$PROVIDER"

    prompt_read "Telegram bot token (blank to configure later)" TELEGRAM_BOT_TOKEN ""
    prompt_read "Telegram allowed user IDs, comma-separated (blank allows all)" TELEGRAM_ALLOWED_USER_IDS ""
    prompt_read "Main model" LLM_MODEL "$DEFAULT_MODEL"
    prompt_read "Aux model" LLM_MODEL_AUX "$DEFAULT_AUX"

    LLM_API_BASE=""
    if [ "$PROVIDER" = "local" ]; then
        prompt_read "Local API base URL" LLM_API_BASE "http://localhost:8090/v1"
        API_KEY="local"
        LLM_PROVIDER="openai"
    else
        prompt_read "$KEY_ENV (blank to configure later)" API_KEY ""
        LLM_PROVIDER="$PROVIDER"
    fi

    cat > "$ENV_FILE" <<EOF
LETHE_HOME=$LETHE_HOME
WORKSPACE_DIR=$LETHE_HOME/workspace
MEMORY_DIR=$LETHE_HOME/data/memory
CACHE_DIR=$LETHE_HOME/cache
LOGS_DIR=$LETHE_HOME/logs
CREDENTIALS_DIR=$LETHE_HOME/credentials

LETHE_MODE=telegram
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_ALLOWED_USER_IDS=$TELEGRAM_ALLOWED_USER_IDS

LLM_PROVIDER=$LLM_PROVIDER
LLM_MODEL=$LLM_MODEL
LLM_MODEL_AUX=$LLM_MODEL_AUX
LLM_API_BASE=$LLM_API_BASE
$KEY_ENV=$API_KEY

HEARTBEAT_ENABLED=true
HEARTBEAT_INTERVAL=3600

LETHE_SEMANTIC_SEARCH_ENABLED=true
LETHE_EMBEDDING_PROVIDER=fastembed
LETHE_EMBEDDING_MODEL=Snowflake/snowflake-arctic-embed-m-v2.0
EOF

    chmod 600 "$ENV_FILE"
    success "Wrote $ENV_FILE"
}

build_binary() {
    info "Building Lethe with Cargo..."
    cargo build --release --manifest-path "$INSTALL_DIR/Cargo.toml"
    mkdir -p "$BIN_DIR"
    cp "$INSTALL_DIR/target/release/lethe" "$BIN_DIR/lethe"
    chmod +x "$BIN_DIR/lethe"
    success "Installed $BIN_DIR/lethe"
}

main() {
    print_header
    write_env_file

    if [ "${LETHE_INSTALL_FROM_SOURCE:-0}" != "1" ] && install_release_binary; then
        :
    else
        warn "Falling back to source build."
        ensure_cargo
        ensure_protoc
        checkout_repo
        build_binary
    fi

    echo ""
    success "Lethe installed."
    echo "Run:"
    echo "  $BIN_DIR/lethe check"
    echo "  $BIN_DIR/lethe telegram run"
    echo ""
    echo "Config:"
    echo "  $ENV_FILE"
}

main "$@"
