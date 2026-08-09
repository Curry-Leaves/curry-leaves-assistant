"""AI provider connections: status, model catalogs, and OAuth flows (Copilot, Codex)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from curry_leaves_assistant.agents import agent_engine, readiness
from curry_leaves_assistant.core import settings as app_settings
from curry_leaves_assistant.providers import codex, copilot, ollama

router = APIRouter(tags=["providers"])


@router.get("/providers/status")
async def providers_status():
    """Live connection status for OAuth/local providers (Copilot, Codex, Ollama).

    ``connected`` stays purely about the credential/server here — unlike Ollama, where being
    switched off IS the disconnected state (a local server has no credential to clear). The
    separate ``enabled`` flag carries the user's on/off choice for all three."""
    connected = copilot.is_connected()
    return {
        "copilot": {"connected": connected, "enabled": app_settings.provider_enabled("copilot"),
                    "models": await copilot.list_models() if connected else []},
        # ``configured`` = a client id is present. Codex ships no default one, so a false here
        # means Connect cannot work until the user supplies one; the UI says so up front.
        "codex": {"connected": codex.is_connected(), "enabled": app_settings.provider_enabled("codex"),
                  "configured": codex.has_client_id(), "clientIdEnv": codex.CLIENT_ID_ENV,
                  "models": await codex.list_models()},
        "ollama": await ollama.status(),
    }


@router.get("/providers/ai-status")
async def providers_ai_status():
    """Whether a run can actually start now — any provider usable AND a default model
    resolves. The frontend fetches this on mount to seed its warning banner; live updates
    then arrive via the ``system.ai.status`` event (emitted at startup + on settings change)."""
    return readiness.ai_status()


def _spec_dto(spec) -> dict:
    """A provider spec as the frontend needs it to render a card (no secrets).

    ``connected`` and ``enabled`` are computed server-side on purpose: connectedness used to be
    re-derived in three places in the UI (settings cards, setup wizard, agent form), which is
    exactly how they drifted. One answer, from the same helpers a real run consults."""
    api_key = app_settings.provider_cfg(spec.id)[0]
    return {
        "id": spec.id, "name": spec.name, "wire": spec.wire, "keyed": spec.keyed,
        "custom": spec.custom, "tiers": spec.tiers, "hint": spec.hint,
        "keyPlaceholder": spec.key_placeholder, "defaultModel": spec.default_model,
        "baseUrl": spec.base_url,
        "connected": readiness._provider_connected(spec.id, api_key),
        "enabled": app_settings.provider_enabled(spec.id),
    }


@router.get("/providers/catalog")
async def providers_catalog(usable: bool = False):
    """The set of provider cards to render: every built-in provider from the registry, plus
    any user-defined custom (OpenAI-compatible) providers saved in settings. The frontend
    builds its UI entirely from this — no hardcoded provider list.

    ``usable=true`` narrows it to providers that are connected AND enabled — what the agent
    and chat model pickers offer. Settings deliberately asks for the full list instead, since
    a disabled provider has to stay visible to be switched back on."""
    from curry_leaves_assistant.providers import registry
    builtins = [_spec_dto(s) for s in registry.builtin_specs()]
    saved = app_settings.read_settings()["ai"]["providers"]
    customs = [
        _spec_dto(registry.custom_spec(pid, cfg))
        for pid, cfg in saved.items()
        if isinstance(cfg, dict) and cfg.get("custom") and not registry.is_builtin(pid)
    ]
    out = builtins + customs
    if usable:
        out = [p for p in out if p["connected"] and p["enabled"]]
    return {"providers": out}


def _filter_ids(ids: set[str], prefixes: tuple) -> list[str]:
    """Keep model ids matching one of the spec's prefixes (or all, if it declares none),
    newest-ish first (reverse-sorted so e.g. gpt-4o > gpt-4)."""
    if prefixes:
        ids = {i for i in ids if any(i.startswith(p) for p in prefixes)}
    return sorted(ids, reverse=True)


class AuthError(Exception):
    """The provider rejected the API key (HTTP 401/403). Distinct from a network/endpoint
    error so a caller can refuse to save a dead key instead of falling back to a curated list."""


async def _live_models(spec, api_key: str) -> list[dict] | None:
    """Fetch a provider's live model catalog. Anthropic uses its native /v1/models; every
    OpenAI-compatible provider (including custom endpoints) hits ``{base_url}/models`` with a
    Bearer key.

    Returns None on a *non-auth* error (network blip, endpoint quirk, empty list) so callers can
    fall back to the curated/free-text path. Raises ``AuthError`` on a 401/403 so a connect flow
    can reject an invalid key rather than silently saving a dead connection."""
    import httpx
    if spec.wire == "anthropic":
        url = "https://api.anthropic.com/v1/models"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    else:  # OpenAI-wire: standard /v1/models listing (data[].id).
        base = (spec.base_url or "https://api.openai.com/v1").rstrip("/")
        url = f"{base}/models"
        headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url, headers=headers)
        if r.status_code in (401, 403):
            raise AuthError(_provider_auth_message(r))
        r.raise_for_status()
        data = r.json().get("data") or []
        ids = {m["id"] for m in data if isinstance(m, dict) and m.get("id")}
        out = _filter_ids(ids, spec.model_prefixes)
        return [{"id": i} for i in out] if out else None
    except AuthError:
        raise
    except Exception:
        return None


def _provider_auth_message(resp) -> str:
    """Pull the provider's own auth-error message out of a 401/403 body, else a generic line."""
    try:
        body = resp.json()
        msg = (body.get("error") or {}).get("message") or body.get("message")
        if msg:
            return str(msg)
    except Exception:
        pass
    return "the API key was rejected"


def _catalog_response(name: str, models: list[dict]) -> dict:
    return {"active": name, "default_model": agent_engine.default_model_id(name), "models": models}


@router.get("/providers/models")
async def provider_models(provider: str | None = None):
    """Models for a provider. Copilot/Codex/Ollama use their live catalogs; every keyed
    provider (built-in or custom) pulls its live /models list and falls back to the registry's
    curated ids when the API is unreachable or lists nothing.

    With no ``provider`` query param, this resolves the *explicitly chosen* active provider
    (settings.ai.active) — NOT an env/auto-detected guess. When the user hasn't set a default,
    return an empty catalog so the chat model picker doesn't offer (say) Claude models for a
    provider that was never connected."""
    from curry_leaves_assistant.providers import registry
    if provider:
        name = provider
    else:
        name = app_settings.active_ai()[0] or ""
        if not name:
            return {"active": "", "default_model": "", "models": []}
    # A disabled provider offers nothing: agents can't run on it, so a picker must not be able
    # to select one of its models. Same empty-catalog shape as the no-active-provider case.
    if not app_settings.provider_enabled(name):
        return {"active": name, "default_model": "", "models": []}
    if name == "copilot":
        return _catalog_response(name, await copilot.list_models())
    if name == "codex":
        return _catalog_response(name, await codex.list_models())
    if name == "ollama":
        return _catalog_response(name, await ollama.list_models())
    cfg = app_settings.read_settings()["ai"]["providers"].get(name, {})
    spec = registry.spec_for(name, cfg)
    api_key = cfg.get("apiKey", "") if provider else app_settings.active_ai()[1]
    live = await _live_models(spec, api_key or "")
    if live:
        return _catalog_response(name, live)
    return _catalog_response(name, [{"id": m} for m in spec.curated])


@router.post("/providers/models/preview")
async def preview_models(request: Request):
    """Fetch a provider's live model catalog from a *candidate* key (and optional base URL for a
    custom endpoint) the user has typed but not yet saved — lets the model dropdown populate
    before Connect. Falls back to the registry's curated list when the key is empty/invalid or
    the API is unreachable."""
    from curry_leaves_assistant.providers import registry
    body = await request.json()
    name = body.get("provider") or ""
    api_key = (body.get("api_key") or "").strip()
    base_url = (body.get("base_url") or "").strip()
    # Build a spec from the built-in registry, or a custom one if a base URL was supplied.
    spec = registry.spec_for(name, {"baseUrl": base_url, "custom": bool(base_url)} if base_url else {})
    default_model = spec.default_model
    if spec.wire in ("anthropic", "openai") and api_key:
        # This is the connect-time validation gate. An invalid key raises AuthError → 400 so the
        # UI refuses to save a dead connection (previously the 401 was swallowed and we fell back
        # to the curated list, letting a bad key "connect" and then fail silently at chat time).
        try:
            live = await _live_models(spec, api_key)
        except AuthError as e:
            return JSONResponse(status_code=400, content={"detail": f"Invalid API key: {e}"})
        if live:
            return {"active": name, "default_model": default_model, "models": live}
    return {"active": name, "default_model": default_model, "models": [{"id": m} for m in spec.curated]}


@router.post("/providers/copilot/start")
async def copilot_start():
    return await copilot.start_device_flow()


@router.post("/providers/copilot/poll")
async def copilot_poll(request: Request):
    body = await request.json()
    result = await copilot.poll_device_flow(body.get("device_code", ""))
    if result.get("status") == "connected":
        # Make Copilot active and default to the first model if none chosen. Connecting also
        # re-enables it: a user who switched Copilot off and then deliberately reconnected
        # means to use it, and would otherwise land on a connected-but-off card.
        models = result.get("models") or []
        cfg: dict = {"enabled": True}
        if models:
            cfg["model"] = models[0]["id"]
        app_settings.patch_ai({"active": "copilot", "providers": {"copilot": cfg}})
        readiness.emit_ai_status()
    return result


@router.post("/providers/copilot/disconnect")
def copilot_disconnect():
    copilot.clear_github_token()
    if app_settings.read_settings()["ai"].get("active") == "copilot":
        app_settings.patch_ai({"active": ""})
    readiness.emit_ai_status()
    return {"ok": True}


@router.post("/providers/codex/start")
async def codex_start():
    return await codex.start_login()


@router.post("/providers/codex/poll")
async def codex_poll(request: Request):
    body = await request.json()
    result = await codex.poll_login(body.get("state", ""))
    if result.get("status") == "connected":
        # Make Codex active and default to the first model if none chosen. Connecting also
        # re-enables it — see the Copilot poll above for why.
        models = result.get("models") or []
        cfg: dict = {"enabled": True}
        if models:
            cfg["model"] = models[0]["id"]
        app_settings.patch_ai({"active": "codex", "providers": {"codex": cfg}})
        readiness.emit_ai_status()
    return result


@router.post("/providers/codex/disconnect")
def codex_disconnect():
    codex.clear_tokens()
    if app_settings.read_settings()["ai"].get("active") == "codex":
        app_settings.patch_ai({"active": ""})
    readiness.emit_ai_status()
    return {"ok": True}
