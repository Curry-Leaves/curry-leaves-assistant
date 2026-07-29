"""Copilot provider — copied from smart-loop and adapted to curry-leaves imports.

Provides CopilotAuth, CopilotProvider, and the URL/header constants copilot.py needs.
"""

from __future__ import annotations

import json
import os
import time
from typing import AsyncIterator

import httpx

from curry_leaves.providers.base import Context, Model, StreamEvent
from curry_leaves.providers.openai import build_openai_request, parse_openai_stream
from curry_leaves.providers.sse import iter_sse

# Curry Leaves' own registered GitHub OAuth App. This is the same approach opencode takes — its
# own OAuth app, which per GitHub's official OpenCode partnership it is "required to use". This app
# is granted the GA model catalog; the preview / Business / Enterprise models require GitHub's
# copilot_internal/v2/token exchange together with additional request headers, which this app does
# not send by default.
#
# A user can override the client id via settings ``ai.providers.copilot.clientId`` or env
# ``CURRY_LEAVES_GITHUB_CLIENT_ID`` — their choice, on their account, not our default. Resolution
# order: settings → env → our own app. See docs/design/oauth-provider-registration.md.
DEFAULT_CLIENT_ID = "Ov23li1znm6QS9ZzkZ6H"
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"


def _override_client_id() -> str:
    """A user-supplied client-id override (settings → env), or "" if none. Resolved fresh each
    call so a settings change takes effect on the next connect without a restart."""
    from curry_leaves_assistant.core import settings as app_settings
    cfg = app_settings.read_settings()["ai"]["providers"].get("copilot", {})
    return (cfg.get("clientId") or "").strip() or os.environ.get("CURRY_LEAVES_GITHUB_CLIENT_ID") or ""


def client_id() -> str:
    """The GitHub OAuth client id to authenticate with. Our own registered app by default; a
    user's explicit override wins. See the DEFAULT_CLIENT_ID note for the trade-off."""
    return _override_client_id() or DEFAULT_CLIENT_ID


def _override_headers() -> dict:
    """User-supplied custom request headers from settings (``ai.providers.copilot.headers``), an
    ``{name: value}`` map. Empty by default. A user can add any headers here — e.g.
    ``{"Copilot-Integration-Id": "vscode-chat", "Editor-Version": "vscode/1.95.0"}`` — which GitHub
    may use to return a different model set."""
    from curry_leaves_assistant.core import settings as app_settings
    cfg = app_settings.read_settings()["ai"]["providers"].get("copilot", {})
    h = cfg.get("headers")
    return {str(k): str(v) for k, v in h.items()} if isinstance(h, dict) else {}


def is_overridden() -> bool:
    """True when the user has set a custom client id and/or custom request headers. This is the
    single switch that turns on the token-exchange path: the default app uses the raw token, while a
    user override opts into the exchange, which GitHub only honors alongside the custom headers the
    user supplied (without them it returns "400 missing Editor-Version")."""
    return bool(_override_client_id() or _override_headers())


from curry_leaves_assistant.providers.identity import app_ua

# Base request headers — Curry Leaves' own User-Agent plus standard API headers. Any additional
# headers come only from user settings (see _override_headers). The raw GitHub token with just
# these is authorized for the GA model catalog, which is what opencode ships.
_BASE_HEADERS = {
    "User-Agent": app_ua(),
    "X-GitHub-Api-Version": "2025-05-01",
    "X-Curry-Leaves-Client": app_ua(),
}


def request_headers() -> dict:
    """Headers to send on Copilot API calls: the base headers, plus any user-supplied custom
    headers layered on top. X-Curry-Leaves-Client is kept last so activity is attributed to Curry
    Leaves in GitHub's audit logs."""
    out = dict(_BASE_HEADERS)
    out.update(_override_headers())
    out["X-Curry-Leaves-Client"] = app_ua()
    return out


API_BASE = "https://api.githubcopilot.com"


class CopilotAuth:
    def __init__(self, github_token: str | None = None, use_exchange: bool | None = None):
        self._github_token = github_token
        # Whether to run the token-exchange path (exchange + custom headers). Defaults to whether the
        # user set any override (client id / headers); callers may pin it explicitly (e.g. tests).
        self._use_exchange = is_overridden() if use_exchange is None else use_exchange
        self._copilot_token: str | None = None
        self._expires_at: float = 0.0
        # The account-specific API host from the exchange envelope's `endpoints.api`. Business /
        # Enterprise SKUs are routed to an SKU-isolated host (e.g. api.business.githubcopilot.com);
        # individual subscribers get api.individual.githubcopilot.com. Falls back to the public host.
        self._api_base: str = API_BASE

    def headers(self) -> dict:
        """The request headers to send — the base identity plus any user-supplied custom headers.
        (Delegates to module-level request_headers; kept as a method so call sites read naturally.)"""
        return request_headers()

    async def bearer(self) -> str:
        """The bearer token for the Copilot API.

        Default path: send the **raw GitHub token**. GitHub authorizes it for the GA model catalog
        and it needs no extra headers — this is what opencode ships.

        Exchange path (only when the user set a client-id or header override): exchange the GitHub
        token at copilot_internal/v2/token for a short-lived **session token** and use it. That token
        can reach a fuller catalog, but GitHub only accepts it alongside the custom headers the user
        supplied, which is why the exchange lives on the same switch as those headers. If the
        exchange fails we fall back to the raw token so chat still works."""
        if not self._github_token:
            raise RuntimeError("Not logged in to Copilot.")
        if not self._use_exchange:
            # Default: raw token, no exchange, base headers only.
            self._api_base = API_BASE
            return self._github_token
        if self._copilot_token and time.time() < self._expires_at - 300:
            return self._copilot_token
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(
                    COPILOT_TOKEN_URL,
                    headers={"Authorization": f"token {self._github_token}", "Accept": "application/json", **request_headers()},
                )
                r.raise_for_status()
                d = r.json()
            self._copilot_token = d["token"]
            self._expires_at = float(d.get("expires_at", time.time() + 1500))
            self._api_base = ((d.get("endpoints") or {}).get("api") or API_BASE).rstrip("/")
            return self._copilot_token
        except Exception:
            # Exchange failed — fall back to the raw token so chat still works. Don't cache it under
            # _copilot_token; re-derive so a later working exchange wins.
            self._api_base = API_BASE
            return self._github_token

    async def api_base(self) -> str:
        """The Copilot API host to call — the account's `endpoints.api` once the exchange has run,
        else the public default. Ensures the exchange has happened first so routing is correct."""
        await self.bearer()
        return self._api_base

    @property
    def logged_in(self) -> bool:
        return self._github_token is not None


class CopilotProvider:
    def __init__(self, auth: CopilotAuth | None = None, client: httpx.AsyncClient | None = None):
        self._auth = auth or CopilotAuth()
        self._client = client

    async def stream(self, ctx: Context, model: Model, opts=None) -> AsyncIterator[StreamEvent]:
        from curry_leaves.providers.base import StreamOpts
        bearer = await self._auth.bearer()
        chat_url = f"{await self._auth.api_base()}/chat/completions"
        body = build_openai_request(ctx, model, opts if opts is not None else StreamOpts())
        headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json", **self._auth.headers()}
        client = self._client or httpx.AsyncClient(timeout=None)
        try:
            async with client.stream("POST", chat_url, json=body, headers=headers) as resp:
                resp.raise_for_status()
                async for event in parse_openai_stream(iter_sse(resp, done_sentinel="[DONE]")):
                    yield event
        finally:
            if self._client is None:
                await client.aclose()
