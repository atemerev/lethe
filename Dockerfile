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
RUN npm install -g agent-browser && agent-browser install --with-deps

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

# Install dependencies (CPU-only PyTorch to save ~2GB)
ENV UV_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN uv sync --frozen --index-strategy unsafe-best-match

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
CMD ["uv", "run", "lethe"]
