# Curry Leaves — single-container image: FastAPI backend + built React UI, one port.
#
# Build (from this repo — it's the build context; no sibling checkouts needed since every
# dependency, including the curry-leaves kernel, comes from PyPI):
#   docker build -t curry-leaves .
# Run:
#   docker run -p 5177:5177 -v curry-leaves-data:/data curry-leaves
# Then open http://localhost:5177
#
# Prefer `docker compose up` (see docker-compose.yml) — it wires the data volume and .env for you.

# ── Stage 1: fetch the prebuilt React frontend ─────────────────────────────────
# The frontend lives in its own repo (curry-leaves-assistant-web) and publishes a
# static dist/ to npm. We don't build it here — just unpack the pinned version.
# Override with `--build-arg WEB_VERSION=x.y.z`.
FROM node:20-slim AS frontend
ARG WEB_VERSION=1.0.3
WORKDIR /app
RUN npm pack "curry-leaves-assistant-web@${WEB_VERSION}" \
    && tar -xzf curry-leaves-assistant-web-*.tgz \
    && test -d package/dist \
    && mv package/dist ./dist

# ── Stage 2: Python backend + built frontend, installed as a real package ──────
FROM python:3.11-slim AS backend
WORKDIR /app

# build-essential: some backend deps (e.g. faster-whisper's ctranslate2, markitdown
# extras) pull in packages that need a compiler on some platforms.
# espeak-ng: Kokoro TTS (domain/tts.py) phonemizes text through the espeak-ng binary at
# runtime; without it the chat Voice button's synthesis raises.
# ffmpeg: faster-whisper (domain/transcribe.py) decodes the audio container through the
# ffmpeg binary before transcription; without it every transcription raises. espeak-ng:
# Kokoro TTS phonemizes through espeak-ng at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential curl espeak-ng ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# This backend + all its deps (the curry-leaves kernel, cl_memory, whisper, …) come from
# PyPI — nothing local to copy. The built frontend from stage 1 is dropped into the package's
# webui/ dir so `pip install .` bundles it and FastAPI serves it as an SPA.
COPY pyproject.toml LICENSE README.md ./
COPY src/curry_leaves_assistant ./src/curry_leaves_assistant
RUN rm -rf ./src/curry_leaves_assistant/webui ./src/curry_leaves_assistant/.env
COPY --from=frontend /app/dist ./src/curry_leaves_assistant/webui

# mlx-whisper is Apple-Silicon-only and simply won't resolve/build in this Linux
# image — pyproject.toml's platform marker on that dependency makes pip skip it
# here automatically; faster-whisper (also a dependency) is the backend used instead.
RUN pip install --no-cache-dir .

# Playwright ships only the Python package via pip; the actual Chromium binary is a
# separate download. The browser tool (agents/browser_tool.py, web_tools.py) launches
# headless chromium, so fetch it plus the OS shared libs the slim image lacks (nss,
# atk, …). Installs into /root/.cache/ms-playwright, which is where Playwright looks.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

ENV CURRY_LEAVES_HOST=0.0.0.0 \
    CURRY_LEAVES_PORT=5177 \
    CURRY_LEAVES_BACKEND=faster-whisper \
    CURRY_LEAVES_DIR=/data

VOLUME ["/data"]
EXPOSE 5177

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -f http://127.0.0.1:5177/health || exit 1

CMD ["curry-leaves-assistant"]
