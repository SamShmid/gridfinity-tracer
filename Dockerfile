# ---- frontend build -------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- backend --------------------------------------------------------------
FROM python:3.12-slim AS app
ENV PYTHONUNBUFFERED=1 \
    GT_MODELS_DIR=/models \
    GT_DATA_DIR=/data \
    GT_STATIC_DIR=/app/app/static \
    U2NET_HOME=/models/u2net
# OpenCascade / opencv-headless need a few shared libs even without a display.
# setpriv (util-linux) lets the entrypoint drop from root to the app user after fixing volume ownership.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglu1-mesa libxrender1 libxext6 libsm6 libgomp1 libfontconfig1 util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 tracer && useradd --uid 1000 --gid tracer --create-home --shell /usr/sbin/nologin tracer
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
# requirements.lock is generated from pyproject.toml (see README, "Dependencies") so builds are reproducible.
COPY backend/pyproject.toml backend/requirements.lock ./
RUN uv pip install --system --no-cache -r requirements.lock
COPY backend/app ./app
COPY backend/scripts ./scripts
COPY --from=web /backend/app/static ./app/static
# Model weights are fetched on first start into the /models volume (see entrypoint).
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && mkdir -p /models /data \
    && chown -R tracer:tracer /models /data /app
EXPOSE 8000
VOLUME ["/models", "/data"]
# Long start period: the first boot downloads ~330 MB of weights before uvicorn comes up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD python -c "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4); sys.exit(0 if r.status == 200 else 1)"
# The entrypoint starts as root only to chown the volumes, then re-execs itself as `tracer`.
ENTRYPOINT ["/entrypoint.sh"]
