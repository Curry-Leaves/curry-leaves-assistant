#!/usr/bin/env python3
"""Curry Leaves backend entrypoint.

Owns the ~/.curry-leaves file layout and exposes recordings, agents, todos/reminders,
chat, and a live event stream over HTTP+SSE. Boots the agent pool + scheduler.
Signals readiness to the Electron shell with `CURRY_LEAVES_LISTENING <host>:<port>`.

The HTTP surface lives in curry_leaves_assistant/api/ (one router per domain); this
module only wires env → app → middleware → routers and starts uvicorn.
"""
from __future__ import annotations

import asyncio
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path


def _load_env() -> None:
    """Load KEY=VALUE pairs from a local .env (next to app.py) if present, so
    ANTHROPIC_API_KEY etc. are available in dev without exporting them."""
    env = Path(__file__).parent / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

# Point curry-leaves's internal storage (sessions, skills) at the same ~/.curry-leaves
# dir this app already uses for agents/, knowledge/, etc. — one flat layout, not a
# nested "data/" subtree. Must be set before importing curry_leaves.
os.environ.setdefault(
    "CURRY_LEAVES_HOME",
    os.environ.get("CURRY_LEAVES_DIR", str(Path.home() / ".curry-leaves")),
)

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from curry_leaves.catalog import load_catalog

import curry_leaves_assistant.api as api
from curry_leaves_assistant.agents import readiness
import curry_leaves_assistant.orchestration as orchestration
from curry_leaves_assistant.core import auth, events, paths, service, ws_hub
from curry_leaves_assistant.domain import knowledge, recordings
from curry_leaves_assistant.stores import agent_store, dashboard_store, skills_store, templates_store

HOST = os.environ.get("CURRY_LEAVES_HOST", "127.0.0.1")
PORT = int(os.environ.get("CURRY_LEAVES_PORT") or 0)
# Fixed default (matches Docker). A random port would change the browser origin on every
# restart in web mode, wiping origin-scoped state: the auth token, the saved mic
# device id, and the microphone permission grant.
DEFAULT_PORT = 5177


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    paths.ensure_dirs()
    try:
        await load_catalog()  # model context windows + live models.dev pricing, for usage_store
    except Exception as exc:
        print(f"[catalog] load_catalog failed: {exc}", flush=True)
    recordings.reap_stale_drafts()
    # Hand the LLM consolidation hook down to the memory bundle: folding episodes into durable
    # notes needs a model, and the provider machinery lives above domain/. Composition root's job.
    from curry_leaves_assistant.agents import consolidation
    from curry_leaves_assistant.domain import memory as memory_mod
    memory_mod.set_summarizer(consolidation.summarize)
    knowledge.seed_bundle()
    skills_store.seed_default_skills()
    agent_store.seed_default_agents()
    templates_store.seed_default_templates()
    dashboard_store.ensure_default_board()
    loop = asyncio.get_running_loop()
    events.set_loop(loop)
    ws_hub.set_loop(loop)  # so hub.publish_* can push from worker threads
    orchestration.start()  # work kernel + schedule sources + the one tick loop, in order
    try:
        readiness.emit_ai_status()  # seed the "no AI provider / no default model" banner for connecting clients
    except Exception as exc:
        print(f"[readiness] initial emit failed: {exc}", flush=True)

    # Warm Kokoro off the boot path — but ONLY if the user already set voice up (weights on
    # disk). We never download TTS at boot: a fresh install that never enables spoken replies
    # shouldn't pull ~300 MB of voice weights it won't use. The download is an explicit action
    # in setup/settings (POST /wakeword/tts/download); this only pays the ~7s pipeline-build
    # cost up front for installs that have already opted in, so their first speak() is instant.
    async def _warm_tts() -> None:
        from curry_leaves_assistant.domain import tts
        if not tts.is_downloaded():
            return  # voice not set up yet — stay cold until the user enables it
        try:
            await asyncio.to_thread(tts.warm)
        except Exception as exc:
            print(f"[tts] warm failed (will load lazily): {exc}", flush=True)

    # The knowledge base's embedding model (~90 MB on first run) IS warmed eagerly — semantic
    # search is on by default for everyone, so this is a first-boot cost we do want to pay. The
    # KB reads the weights at construction to decide whether the vector tier comes up, so this
    # fetch lands for the NEXT boot — until then search is keyword-only, the old behavior.
    async def _warm_embeddings() -> None:
        try:
            await asyncio.to_thread(knowledge.warm_embeddings)
        except Exception as exc:
            print(f"[embeddings] warm failed (search stays keyword-only): {exc}", flush=True)

    # Hold the references: a bare create_task() is only weakly held by the loop and can be
    # garbage-collected mid-download.
    warm_task = asyncio.create_task(_warm_tts())
    embed_task = asyncio.create_task(_warm_embeddings())

    # Record the pid so `curry-leaves-assistant stop` can find us. Written here rather than
    # in main() so it only exists once the app is actually up, and is cleared below on exit.
    service.write_pid()

    print(f"CURRY_LEAVES_LISTENING {HOST}:{PORT}", flush=True)  # readiness signal for Electron
    try:
        yield
    finally:
        warm_task.cancel()  # a download still in flight shouldn't hold up shutdown
        embed_task.cancel()
        await orchestration.stop()  # scheduler tick down, then drain the worker fleet
        service.clear_pid()


app = FastAPI(lifespan=lifespan)
# Headless/Docker installs can set CURRY_LEAVES_PIN so the backend boots already
# configured, rather than sitting in the open "setup mode" window until someone
# completes the first-run wizard. No-op when a PIN already exists.
auth.seed_pin_from_env()
# Order matters: Starlette applies the LAST-added middleware outermost, so adding
# CORS after the auth gate keeps CORS on the outside — every 401 still carries the
# CORS headers the renderer needs to read the response.
app.add_middleware(auth.AuthMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

for router in api.ALL_ROUTERS:
    app.include_router(router)


# ─── Built frontend (pip install / Docker / web-mode) ──────────────────────────
# The React app lives in its own repo (curry-leaves-assistant-web) and ships as a
# static bundle. When that bundle is present we serve it from this same
# process/port, so the whole app is single-origin. Mounted last so it never shadows
# an API route above; index.html is the SPA fallback for client routing. If no
# built dir is found (e.g. a bare backend checkout with no UI fetched yet) the mount
# is skipped and the API still runs — a separate web dev server can point at it.
#
# Two places are checked:
#   1. curry_leaves_assistant/webui/ — bundled INSIDE the package (pip installs;
#      see pyproject.toml [tool.hatch.build]). scripts/build_webui.sh unpacks the
#      published web bundle here before a package build.
#   2. <repo root>/dist — a loose dist/ copy, for local testing without a full
#      package build.
_BUNDLED_STATIC_DIR = Path(__file__).resolve().parent / "webui"
_REPO_STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "dist"
_STATIC_DIR = _BUNDLED_STATIC_DIR if _BUNDLED_STATIC_DIR.is_dir() else _REPO_STATIC_DIR
if _STATIC_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles

    class _SPAStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope):
            response = await super().get_response(path, scope)
            if response.status_code == 404:
                response = await super().get_response("index.html", scope)
            return response

    app.mount("/", _SPAStaticFiles(directory=str(_STATIC_DIR), html=True), name="frontend")


def serve() -> None:
    """Boot the HTTP server. The `start` command, and what `python app.py` does."""
    global PORT
    if not PORT:
        if _port_available(DEFAULT_PORT):
            PORT = DEFAULT_PORT
        else:
            PORT = _free_port()
            print(f"[app] port {DEFAULT_PORT} is busy — using {PORT}; browser origin-scoped "
                  "state (login, mic grant) from previous runs won't carry over", flush=True)
    from curry_leaves_assistant.core import server_info

    server_info.set_base_url(HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


def main(argv: list[str] | None = None) -> None:
    """Back-compat entrypoint. The console script now points at cli:main, but this name
    is load-bearing: the Electron shell spawns `python -m curry_leaves_assistant.app`,
    and older installs' scripts still reference `app:main`. Delegates so both get the
    same subcommands."""
    from curry_leaves_assistant.cli import main as cli_main

    cli_main(argv)


if __name__ == "__main__":
    main()
