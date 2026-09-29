#!/bin/sh
# Container entrypoint.
#
# 1. If we start as root, make sure the /models and /data volumes are writable by the app user
#    (bind mounts and old volumes may be root-owned), then re-exec as that user.
# 2. Fetch model weights if missing. A failed download (no internet) must NOT
#    kill the container: the app still serves the classical pipeline and reports models_ready=false
#    on /api/health. When the weights are already there we go fully offline (HF_HUB_OFFLINE=1) so
#    huggingface_hub never stalls on a network check.
# 3. Run uvicorn with exactly ONE worker. This is deliberate and must stay: the SAM embedding cache,
#    the preview cache, the rect metadata cache and the inference/CAD pools live in the process.
#    Concurrency comes from the pools inside app.main, not from extra uvicorn workers; a second worker
#    would recompute embeddings, fight over SQLite and double the memory.
set -u

APP_USER="${GT_USER:-tracer}"
MODELS_DIR="${GT_MODELS_DIR:-/models}"
DATA_DIR="${GT_DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ] && id "$APP_USER" >/dev/null 2>&1; then
  for d in "$MODELS_DIR" "$DATA_DIR"; do
    mkdir -p "$d"
    if [ "$(stat -c %U "$d")" != "$APP_USER" ]; then
      echo "[gridfinity-tracer] fixing ownership of $d for $APP_USER"
      chown -R "$APP_USER:$APP_USER" "$d" || echo "[gridfinity-tracer] warning: could not chown $d"
    fi
  done
  # setpriv keeps the environment, so point HOME (and the XDG dirs libraries like ezdxf use) at the
  # app user's home; otherwise they try to write under /root and crash.
  APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
  export HOME="${APP_HOME:-/home/$APP_USER}" XDG_CONFIG_HOME="${APP_HOME:-/home/$APP_USER}/.config" XDG_CACHE_HOME="${APP_HOME:-/home/$APP_USER}/.cache" MPLCONFIGDIR="${APP_HOME:-/home/$APP_USER}/.cache/mpl"
  mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" && chown -R "$APP_USER:$APP_USER" "$HOME" 2>/dev/null || true
  exec setpriv --reuid="$APP_USER" --regid="$APP_USER" --init-groups "$0" "$@"
fi

if [ -s "$MODELS_DIR/sam2.1-hiera-tiny/onnx/vision_encoder.onnx" ] && [ -n "$(find "$MODELS_DIR/u2net" -name '*.onnx' 2>/dev/null | head -1)" ]; then
  export HF_HUB_OFFLINE=1
  echo "[gridfinity-tracer] model weights present in $MODELS_DIR; running offline"
fi

if [ "${GT_SKIP_MODEL_DOWNLOAD:-0}" != "1" ]; then
  echo "[gridfinity-tracer] checking model weights in $MODELS_DIR ..."
  if ! python scripts/download_models.py; then
    echo "[gridfinity-tracer] WARNING: model download failed (offline?). Starting anyway; AI detection will be" \
         "unavailable until the weights exist (see /api/health models_ready). Retry with: docker compose restart"
  fi
fi

# --workers must stay 1 (see the header comment).
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --timeout-keep-alive 30
