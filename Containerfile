FROM rust:1.88-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        build-essential \
        pkg-config \
        protobuf-compiler \
        libprotobuf-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/lethe

COPY Cargo.toml Cargo.lock ./
COPY config/ config/
COPY src/ src/

RUN cargo build --release

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        diffutils \
        ffmpeg \
        file \
        findutils \
        git \
        procps \
        unzip \
        which \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -d /home/lethe -s /bin/bash lethe

WORKDIR /opt/lethe

COPY --from=builder /opt/lethe/target/release/lethe /usr/local/bin/lethe
COPY config/ config/

ENV HOME=/home/lethe
USER lethe

ENTRYPOINT ["lethe"]
