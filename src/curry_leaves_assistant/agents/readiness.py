"""AI readiness: can a run actually start right now?

A single source of truth for "is any AI provider usable AND does a default model
resolve" — mirrors the gate in ``agent_engine.build_runner`` (the key guard) and
``_select_model_id`` (the model guard) so the UI's warning matches what a real run
would hit. Used two ways:

  - ``ai_status()`` — a snapshot the /providers/status-adjacent endpoint returns and
    the frontend fetches on mount.
  - ``emit_ai_status()`` — publishes that snapshot as a ``system.ai.status`` event so
    every connected client updates live (at startup and whenever provider settings
    change). The event is UI-only — see ``_NON_TRIGGER_PREFIXES`` in core/events.py.
"""
from __future__ import annotations

import os

from curry_leaves_assistant.agents import agent_engine
from curry_leaves_assistant.core import settings as app_settings

# Providers that authenticate with an API key (settings.json or env). The others
# (copilot/codex/ollama) prove usability by a live connection/local-server check. The
# per-provider key env vars now live on each ProviderSpec in providers/registry.py.


def _provider_connected(name: str, api_key: str) -> bool:
    """Whether ``name`` is usable right now (has a key, or a live connection). Keyed providers
    (any registry entry or custom endpoint) count as connected when they have a saved key or a
    matching env var; copilot/codex/ollama prove usability via a live connection/local probe."""
    from curry_leaves_assistant.providers import registry
    from curry_leaves_assistant.core import settings as _s
    cfg = _s.read_settings()["ai"]["providers"].get(name, {})
    spec = registry.spec_for(name, cfg)
    if spec.wire != "special":
        # Keyed provider: a saved key, or any of the spec's env vars, means connected.
        return bool(api_key) or any(os.environ.get(e) for e in spec.key_envs)
    # Special (OAuth/local) providers: ask them directly. Import lazily so a missing optional
    # dependency can't break readiness for the key-based providers.
    try:
        if name == "copilot":
            from curry_leaves_assistant.providers import copilot
            return copilot.is_connected()
        if name == "codex":
            from curry_leaves_assistant.providers import codex
            return codex.is_connected()
        if name == "ollama":
            from curry_leaves_assistant.providers import ollama
            import httpx
            if not ollama.is_enabled():  # user turned Ollama off — treat as not connected
                return False
            # A synchronous local probe (mirrors ollama.status(): reachable == has models).
            # Sync on purpose — ai_status() is called both from sync startup AND from inside
            # async request handlers, where asyncio.run() would raise "loop already running".
            try:
                r = httpx.get(ollama._host() + "/api/tags", timeout=3)
                r.raise_for_status()
                return bool(r.json().get("models"))
            except Exception:
                return False
    except Exception:
        return False
    return False


def ai_status() -> dict:
    """Snapshot of AI readiness: ``{ready, provider, model, reason}``.

    ``ready`` is True only when the user has explicitly connected a provider (chosen it as
    the active default in Settings) AND that provider is usable AND still switched on AND a
    default model resolves. ``reason`` is one of ``no_provider`` / ``provider_disabled`` /
    ``no_model``.

    We deliberately do NOT auto-detect a provider from env vars here: a stray ``ANTHROPIC_API_KEY``
    in the environment must not make the app *look* connected when the user never set anything up.
    Nothing is connected by default — the user connects a provider and picks it as default first.
    (``agent_engine`` still auto-detects at actual run time as a last-ditch fallback; this gate is
    purely about what the UI reports as ready/connected.)"""
    name = app_settings.active_ai()[0] or ""
    if not name:
        return {
            "ready": False, "provider": "", "model": "", "reason": "no_provider",
            "detail": ("No AI provider is connected. Connect a provider and set it as the "
                       "default in Settings → AI providers."),
        }
    api_key = app_settings.provider_cfg(name)[0]
    connected = _provider_connected(name, api_key)
    enabled = app_settings.provider_enabled(name)
    model = agent_engine.default_model_id(name)
    if not enabled:
        # Connected but switched off. Its own reason rather than folding into no_provider:
        # the fix is a toggle, not a credential, and saying "not connected" about a provider
        # the user can see is connected reads as a bug.
        return {
            "ready": False, "provider": name, "model": model, "reason": "provider_disabled",
            "detail": (f"The default AI provider '{name}' is turned off. Switch it back on in "
                       "Settings → AI providers, or make a different provider the default."),
        }
    if not connected:
        reason = "no_provider"
        detail = (f"No AI provider is connected. Add an API key or connect a provider "
                  f"in Settings → AI providers.")
    elif not model:
        reason = "no_model"
        detail = (f"No default model selected for '{name}'. Pick one in "
                  f"Settings → AI providers.")
    else:
        reason = ""
        detail = ""
    return {
        "ready": bool(connected and model),
        "provider": name,
        "model": model,
        "reason": reason,
        "detail": detail,
    }


def emit_ai_status() -> dict:
    """Compute readiness and publish it as a ``system.ai.status`` event (live to every
    client + durable, so a reconnecting client replays it). Returns the snapshot."""
    from curry_leaves_assistant.core import events

    status = ai_status()
    events.emit("system.ai.status", payload=status)
    return status
