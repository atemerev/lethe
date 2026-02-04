# Lethe Container - Safe Mode
# Access restricted to /workspace only

FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    nodejs \
    npm \
    sudo \
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

# Create non-root user with sudo access
RUN useradd -m -s /bin/bash lethe && \
    echo "lethe ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers && \
    chown -R lethe:lethe /app

USER lethe

# Environment
ENV WORKSPACE_DIR=/workspace
ENV MEMORY_DIR=/workspace/data/memory
ENV LETHE_CONFIG_DIR=/app/config

CMD ["uv", "run", "lethe"]
