FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data && chmod 777 /data

# Config comes from a mounted config.yaml (e.g. a Render "Secret File" at
# /app/config.yaml, or a path pointed to by CONFIG_PATH). Secrets in it are
# overridden by environment variables at runtime — see config_env.py.
ENV CONFIG_PATH=/app/config.yaml

VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=5 \
    CMD curl -f http://localhost:${PORT:-8080}/healthz || exit 1

CMD ["python", "scanner.py"]
