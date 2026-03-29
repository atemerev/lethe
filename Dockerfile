# Lethe Container - Safe Mode
# Access restricted to /workspace only

FROM python:3.12-slim

# Install system dependencies + common tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    nodejs \
    npm \
    sudo \
    gosu \
    # Common tools the agent might need
    imagemagick \
    qpdf \
    poppler-utils \
    ffmpeg \
    jq \
    wget \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install agent-browser for browser automation (with Playwright deps)
# On ARM64 (e.g., AWS Graviton), Chrome for Testing isn't available;
# install system chromium instead and skip agent-browser's bundled Chrome.
RUN npm install -g agent-browser && \
    if agent-browser install --with-deps 2>/dev/null; then \
        echo "agent-browser installed with bundled Chrome"; \
    else \
        apt-get update && apt-get install -y --no-install-recommends chromium && \
        rm -rf /var/lib/apt/lists/* && \
        echo "Installed system chromium for ARM64"; \
    fi
ENV CHROME_PATH=/usr/bin/chromium

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/ && \
    mv /root/.local/bin/uvx /usr/local/bin/ 2>/dev/null || true
ENV PATH="/usr/local/bin:$PATH"

# Create workspace directory
RUN mkdir -p /workspace /app

WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY config/ ./config/

# Install runtime dependencies only (skip dev group in container image)
# Torch CPU index is already configured explicitly in pyproject via [tool.uv.sources].
RUN uv sync --frozen --no-dev

# Copy entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Environment
ENV WORKSPACE_DIR=/workspace
ENV MEMORY_DIR=/workspace/data/memory
ENV LETHE_CONFIG_DIR=/app/config
ENV XDG_CACHE_HOME=/workspace/.cache
ENV UV_CACHE_DIR=/workspace/.cache/uv

# Entrypoint handles user creation based on HOST_UID
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uv", "run", "--no-sync", "lethe"]
