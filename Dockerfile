# Lethe - Autonomous Executive Assistant
# Container image for "safety first" installation

FROM python:3.12-slim

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY config/ config/

# Install dependencies
RUN uv sync --frozen

# Create directories for persistent data
RUN mkdir -p /app/workspace /app/data

# Volumes for persistence
VOLUME /app/workspace
VOLUME /app/data
VOLUME /app/config

# Run
CMD ["uv", "run", "lethe"]
