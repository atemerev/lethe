FROM python:3.12-slim

RUN pip install --no-cache-dir \
    aiogram>=3.24.0 \
    docker>=7.0.0 \
    httpx>=0.28.0

WORKDIR /app
COPY gateway/ ./gateway/

CMD ["python", "-m", "gateway.main"]
