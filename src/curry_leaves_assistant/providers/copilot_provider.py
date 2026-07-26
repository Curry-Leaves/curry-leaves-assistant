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

# GitHub's first-party Copilot OAuth client. Override with your own registered GitHub OAuth
# App's id (env CURRY_LEAVES_GITHUB_CLIENT_ID) to brand the consent screen — see
# docs/design/oauth-provider-registration.md for the steps AND the caveat (the Copilot API is gated
# to GitHub's own clients, so your own app may authenticate the user but fail the Copilot
# token exchange). Defaults to GitHub's id, which keeps subscription access working.
CLIENT_ID = os.environ.get("CURRY_LEAVES_GITHUB_CLIENT_ID") or "Ov23li1znm6QS9ZzkZ6H"
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
CHAT_URL = "https://api.githubcopilot.com/chat/completions"

# Copilot gates API access on the editor identity (Copilot-Integration-Id / Editor-Version /
# Editor-Plugin-Version) — those MUST stay as GitHub expects. We only append our own app
# identity to the User-Agent + add a non-gating X-Curry-Leaves-Client header, so Copilot's
# dashboards/audit logs attribute activity to Curry Leaves rather than a bare editor UA.
from curry_leaves_assistant.providers.identity import app_ua

_EDITOR_HEADERS = {
    "Copilot-Integration-Id": "vscode-chat",
    "Editor-Version": "vscode/1.95.0",
    "Editor-Plugin-Version": "copilot-chat/0.22.0",
    "User-Agent": f"GitHubCopilotChat/0.22.0 {app_ua()}",
    "X-Curry-Leaves-Client": app_ua(),
}


class CopilotAuth:
    def __init__(self, github_token: str | None = None):
        self._github_token = github_token
        self._copilot_token: str | None = None
        self._expires_at: float = 0.0

    async def bearer(self) -> str:
        """The bearer token to send to api.githubcopilot.com.

        Savior's approach: try to exchange the GitHub token for a short-lived Copilot token at
        copilot_internal/v2/token (works for GitHub's first-party client), but if that endpoint
        rejects us (a custom OAuth app 404s there), fall back to sending the raw GitHub token
        directly — the Copilot chat API accepts it. Either way we get a usable bearer."""
        if self._copilot_token and time.time() < self._expires_at - 300:
            return self._copilot_token
        if not self._github_token:
            raise RuntimeError("Not logged in to Copilot.")
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(
                    COPILOT_TOKEN_URL,
                    headers={"Authorization": f"token {self._github_token}", "Accept": "application/json", **_EDITOR_HEADERS},
                )
                r.raise_for_status()
                d = r.json()
            self._copilot_token = d["token"]
            self._expires_at = float(d.get("expires_at", time.time() + 1500))
            return self._copilot_token
        except Exception:
            # Exchange unavailable (e.g. custom client id) — use the raw GitHub token directly.
            # Don't cache it under _copilot_token; re-derive each time so a later working
            # exchange can take over. Short expiry buffer is irrelevant here.
            return self._github_token

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
        body = build_openai_request(ctx, model, opts if opts is not None else StreamOpts())
        headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json", **_EDITOR_HEADERS}
        client = self._client or httpx.AsyncClient(timeout=None)
        try:
            async with client.stream("POST", CHAT_URL, json=body, headers=headers) as resp:
                resp.raise_for_status()
                async for event in parse_openai_stream(iter_sse(resp, done_sentinel="[DONE]")):
                    yield event
        finally:
            if self._client is None:
                await client.aclose()
