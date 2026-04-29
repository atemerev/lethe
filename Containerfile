FROM registry.fedoraproject.org/fedora-minimal:43

RUN microdnf install -y --setopt=install_weak_deps=0 python3.12 git \
    && microdnf clean all \
    && useradd -m -d /home/lethe -s /bin/bash lethe

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /opt/lethe
ENV UV_CACHE_DIR=/tmp/uv-cache
ENV HOME=/home/lethe

COPY pyproject.toml uv.lock ./
COPY src/ src/
RUN uv sync --frozen \
    && find .venv -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null \
    && rm -rf /tmp/uv-cache \
    && chown -R lethe:lethe /opt/lethe

USER lethe

ENTRYPOINT ["uv", "run", "lethe"]
