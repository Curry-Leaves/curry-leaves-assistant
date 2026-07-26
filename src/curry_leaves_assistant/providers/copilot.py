"""GitHub Copilot connect — non-blocking device flow (ported from curry-leaves).

smart-loop's ``CopilotAuth.login()`` is a blocking CLI loop, so we split it into
``start_device_flow()`` / ``poll_device_flow()`` the UI can drive. The long-lived
GitHub OAuth token is stored at ~/.curry-leaves/copilot_oauth.json (mirroring smart-loop's
cache shape, so its CopilotAuth/CopilotProvider can read it too).
"""
from __future__ import annotations

import os

import httpx

from curry_leaves_assistant.core import settings as app_settings

from curry_leaves_assistant.providers.copilot_provider import (
    CLIENT_ID, DEVICE_CODE_URL, ACCESS_TOKEN_URL,
    _EDITOR_HEADERS, CopilotAuth, CopilotProvider,
)

MODELS_URL = "https://api.githubcopilot.com/models"


# ─── token persistence (settings.json only — no separate file) ────────────────
def load_github_token() -> str | None:
    cfg = app_settings.read_settings()["ai"]["providers"].get("copilot", {})
    return cfg.get("githubToken") or os.environ.get("GITHUB_COPILOT_TOKEN") or None


def save_github_token(token: str) -> None:
    app_settings.patch_ai({"providers": {"copilot": {"githubToken": token}}})


def clear_github_token() -> None:
    app_settings.patch_ai({"providers": {"copilot": {"githubToken": ""}}})


def is_connected() -> bool:
    return bool(load_github_token())


# ─── device flow ──────────────────────────────────────────────────────────────
async def start_device_flow() -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(DEVICE_CODE_URL, headers={"Accept": "application/json"},
                         data={"client_id": CLIENT_ID, "scope": "read:user"})
        r.raise_for_status()
        d = r.json()
    return {
        "device_code": d["device_code"], "user_code": d["user_code"],
        "verification_uri": d["verification_uri"],
        "interval": d.get("interval", 5), "expires_in": d.get("expires_in", 900),
    }


async def poll_device_flow(device_code: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(ACCESS_TOKEN_URL, headers={"Accept": "application/json"}, data={
            "client_id": CLIENT_ID, "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
        body = r.json()
        token = body.get("access_token")
        if not token:
            err = body.get("error")
            if err == "authorization_pending":
                return {"status": "pending"}
            if err == "slow_down":
                return {"status": "slow_down", "interval": body.get("interval", 10)}
            return {"status": "error", "error": err or "unknown"}

    # Savior's approach: DON'T verify via copilot_internal/v2/token at connect time. That
    # endpoint is gated to GitHub's first-party clients, so a custom OAuth app 404s there even
    # though the raw GitHub token works against the Copilot chat API directly. We just store the
    # GitHub token; the chat/models requests use it (or lazily exchange) at request time.
    save_github_token(token)
    return {"status": "connected", "models": await list_models()}


# ─── model catalog ────────────────────────────────────────────────────────────
async def list_models() -> list[dict]:
    token = load_github_token()
    if not token:
        return []
    try:
        bearer = await CopilotAuth(github_token=token).bearer()
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(MODELS_URL, headers={"Authorization": f"Bearer {bearer}", **_EDITOR_HEADERS})
            r.raise_for_status()
            data = r.json()
    except Exception:
        return []
    raw = data.get("data") if isinstance(data, dict) else data
    out, seen = [], set()
    for m in raw or []:
        mid = m.get("id") if isinstance(m, dict) else str(m)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append({"id": mid})
    out.sort(key=lambda x: x["id"])
    return out


def build_provider() -> CopilotProvider:
    return CopilotProvider(auth=CopilotAuth(github_token=load_github_token()))
