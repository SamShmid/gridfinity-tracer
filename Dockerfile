# Base images are pinned by tag AND digest (multi-arch index digests, valid for linux/amd64 and
# linux/arm64). Dependabot (.github/dependabot.yml, "docker") proposes bumps for both.
# ---- frontend build -------------------------------------------------------
FROM node:22.23.3-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- backend --------------------------------------------------------------
FROM python:3.14.7-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d AS app
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
COPY --from=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 /uv /usr/local/bin/uv
WORKDIR /app
# requirements.lock is generated from pyproject.toml with --generate-hashes (see README, "Dependencies");
# --require-hashes makes uv refuse any wheel that does not match, so rebuilds are reproducible.
COPY backend/pyproject.toml backend/requirements.lock ./
RUN uv pip install --system --no-cache --require-hashes -r requirements.lock
COPY backend/app ./app
COPY backend/scripts/download_models.py ./scripts/download_models.py
COPY --from=web /backend/app/static ./app/static
# Model weights are fetched on first start into the /models volume (see entrypoint).
COPY docker/entrypoint.sh /entrypoint.sh
# Only the volumes belong to the app user; /app stays root-owned (read-only for the process).
RUN chmod +x /entrypoint.sh \
    && mkdir -p /models /data \
    && chown tracer:tracer /models /data
EXPOSE 8000
VOLUME ["/models", "/data"]
# The single healthcheck definition (docker-compose.yml inherits it). Long start period: the first
# boot downloads ~330 MB of weights before uvicorn comes up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD python -c "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4); sys.exit(0 if r.status == 200 else 1)"
# The entrypoint starts as root only to chown the volumes, then re-execs itself as `tracer`.
ENTRYPOINT ["/entrypoint.sh"]
