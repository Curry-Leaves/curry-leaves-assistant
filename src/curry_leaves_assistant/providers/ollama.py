"""Ollama provider — local models, OpenAI-compatible, no API key.

Ollama serves an OpenAI chat-completions endpoint at ``http://localhost:11434/v1``,
so we reuse curry-leaves's shared OpenAI wire (build_openai_request / parse_openai_stream)
and just point at the local server. No auth, no cloud — everything stays on the machine.

Mirrors copilot.py / codex.py as a Curry Leaves-local provider module: ``build_provider()``
returns the curry-leaves-compatible client, and ``status()`` lets the UI show whether the
local server is up and which models are installed.
"""
from __future__ import annotations

import os

import httpx

from curry_leaves_assistant.core import settings as app_settings

from curry_leaves.providers.openai import OllamaProvider

DEFAULT_HOST = "http://localhost:11434"


def _host() -> str:
    """The Ollama server root (no trailing /v1). Settings win over env over default."""
    cfg = app_settings.read_settings()["ai"]["providers"].get("ollama", {})
    raw = (cfg.get("baseUrl") or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).strip()
    base = raw.rstrip("/")
    if base.endswith("/v1"):  # accept either the host root or a full .../v1 base
        base = base[:-3]
    return base or DEFAULT_HOST


def build_provider(base_url: str | None = None) -> OllamaProvider:
    host = (base_url or _host()).rstrip("/")
    # OllamaProvider expects a full base URL including /v1
    if not host.endswith("/v1"):
        host = host + "/v1"
    return OllamaProvider(base_url=host)


async def list_models() -> list[dict]:
    """Installed models from the local server (/api/tags), or [] if it's unreachable."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(_host() + "/api/tags")
            resp.raise_for_status()
            data = resp.json()
        return [{"id": m["name"]} for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def is_enabled() -> bool:
    """Whether the user has kept Ollama enabled. A local server can't be "disconnected"
    the way a keyed provider can — it's simply up or down — so the Disconnect button flips
    this flag instead. Default True: an existing/up server keeps working untouched."""
    cfg = app_settings.read_settings()["ai"]["providers"].get("ollama", {})
    return cfg.get("enabled", True) is not False


async def status() -> dict:
    """Live status for the UI: is the local server reachable, and what's installed.

    Reports ``connected: False`` when the user has disabled Ollama, even if the server is
    up — so Disconnect actually returns the card to its unconnected (Connect) state."""
    if not is_enabled():
        return {"connected": False, "models": [], "host": _host(), "enabled": False}
    models = await list_models()
    return {"connected": bool(models), "models": models, "host": _host(), "enabled": True}
