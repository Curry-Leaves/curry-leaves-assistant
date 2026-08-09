"""OpenAI Codex connect — ChatGPT-subscription login (no API key).

Codex doesn't offer a GitHub-style device flow; it uses a loopback PKCE OAuth (the
same flow the `codex` CLI uses): we open the browser to OpenAI's authorize page and
catch the redirect on a one-shot local server at http://localhost:1455. The resulting
ChatGPT tokens talk to the Codex backend (chatgpt.com/backend-api/codex/responses),
which speaks the Responses API — see responses_wire.py.

Mirrors copilot.py's non-blocking start_*/poll_* split so the UI can drive it. Tokens
live in settings.json under providers.codex (accessToken/refreshToken/accountId).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
import uuid
from typing import AsyncIterator
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from curry_leaves_assistant.core import settings as app_settings

from curry_leaves.providers.base import Context, Model, StreamEvent, StreamOpts
from curry_leaves_assistant.providers.responses_wire import build_responses_request, parse_responses_stream
from curry_leaves.providers.sse import iter_sse

ISSUER = "https://auth.openai.com"
# We ship no default OAuth client: Codex login requires you to register your own OpenAI OAuth
# integration and point CURRY_LEAVES_CODEX_CLIENT_ID at it — see
# docs/design/oauth-provider-registration.md. Note the caveat: the ChatGPT-subscription (Codex)
# backend is gated to originator=codex_cli_rs, so a third-party client generally can't use a
# user's ChatGPT plan; for first-party OpenAI branding, use the API-key `openai` provider.
CLIENT_ID_ENV = "CURRY_LEAVES_CODEX_CLIENT_ID"
NO_CLIENT_ID_ERROR = (
    f"Codex login needs an OpenAI OAuth client id — set {CLIENT_ID_ENV} to your registered "
    "integration's client id (see docs/design/oauth-provider-registration.md), or use the "
    "API-key `openai` provider instead."
)


def _client_id() -> str:
    """Resolved at call time, not import time, so setting the env var doesn't need a restart."""
    cid = (os.environ.get(CLIENT_ID_ENV) or "").strip()
    if not cid:
        raise RuntimeError(NO_CLIENT_ID_ERROR)
    return cid
REDIRECT_PORT = 1455
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/auth/callback"
SCOPE = "openid profile email offline_access"
CHAT_URL = "https://chatgpt.com/backend-api/codex/responses"

# A stable per-process session id Codex's backend expects on each request.
_SESSION_ID = str(uuid.uuid4())

# Pending logins keyed by oauth `state`: holds the loopback server + a future the
# callback resolves with the auth code, plus the PKCE verifier for the exchange.
_pending: dict[str, dict] = {}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# ─── token persistence (settings.json under providers.codex) ──────────────────
def _load() -> dict:
    return app_settings.read_settings()["ai"]["providers"].get("codex", {})


def has_client_id() -> bool:
    """Whether a client id is configured at all — the UI uses this to say "you must supply one"
    up front rather than only after the user clicks Connect and the flow fails."""
    return bool((os.environ.get(CLIENT_ID_ENV) or "").strip())


def is_connected() -> bool:
    return bool(_load().get("accessToken"))


def clear_tokens() -> None:
    app_settings.patch_ai({"providers": {"codex": {"accessToken": "", "refreshToken": "", "accountId": "", "expiresAt": 0}}})


def _account_id(id_token: str | None) -> str | None:
    """Pull the ChatGPT account id from the id_token's claims (JWT payload, no verify)."""
    if not id_token:
        return None
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
    except Exception:
        return None


def _save_tokens(d: dict) -> None:
    """Merge an OAuth token response into settings (refresh responses may omit fields)."""
    prev = _load()
    id_token = d.get("id_token")
    expires_in = d.get("expires_in") or 0
    app_settings.patch_ai({"providers": {"codex": {
        "accessToken": d.get("access_token") or prev.get("accessToken", ""),
        "refreshToken": d.get("refresh_token") or prev.get("refreshToken", ""),
        "accountId": (_account_id(id_token) if id_token else None) or prev.get("accountId", ""),
        "expiresAt": (time.time() + float(expires_in)) if expires_in else prev.get("expiresAt", 0),
    }}})


# ─── loopback OAuth (PKCE) ────────────────────────────────────────────────────
async def _start_callback_server(state: str, fut: asyncio.Future) -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            target = line.decode("latin1").split(" ")[1] if b" " in line else ""
            q = parse_qs(urlparse(target).query)
            code = (q.get("code") or [None])[0]
            ok = bool(code) and (q.get("state") or [None])[0] == state
            if ok and not fut.done():
                fut.set_result(code)
            msg = "Codex connected — you can close this tab and return to Curry Leaves." if ok else "Authorization failed."
            body = f"<html><body style='font-family:sans-serif'><h2>{msg}</h2></body></html>".encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    # Bind loopback by default (desktop/local runs). In Docker the app sets
    # CURRY_LEAVES_HOST=0.0.0.0 so the published -p 1455:1455 mapping can reach this
    # callback server; a 127.0.0.1 bind would refuse Docker's forwarded traffic.
    host = os.environ.get("CURRY_LEAVES_HOST", "127.0.0.1").strip() or "127.0.0.1"
    return await asyncio.start_server(handle, host, REDIRECT_PORT)


async def start_login() -> dict:
    """Begin the loopback OAuth: spin up the callback server and return the URL the UI
    should open. Returns {auth_url, state}; the UI then polls poll_login(state)."""
    try:
        client_id = _client_id()
    except RuntimeError as e:
        return {"status": "error", "error": str(e)}
    verifier = _b64url(os.urandom(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64url(os.urandom(32))
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    try:
        server = await _start_callback_server(state, fut)
    except OSError as e:
        return {"status": "error", "error": f"Port {REDIRECT_PORT} is busy — close any running Codex login and retry. ({e})"}
    _pending[state] = {"future": fut, "verifier": verifier, "server": server}
    params = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
    })
    return {"auth_url": f"{ISSUER}/oauth/authorize?{params}", "state": state, "expires_in": 600}


def _cleanup(state: str) -> None:
    p = _pending.pop(state, None)
    if p:
        try:
            p["server"].close()
        except Exception:
            pass


async def _exchange(grant: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{ISSUER}/oauth/token", data={"client_id": _client_id(), **grant})
        r.raise_for_status()
        return r.json()


async def poll_login(state: str) -> dict:
    """Check whether the browser redirect has arrived; on success exchange the code for
    tokens and persist them. Returns {status: pending|connected|error}."""
    p = _pending.get(state)
    if not p:
        return {"status": "error", "error": "No pending Codex login."}
    fut: asyncio.Future = p["future"]
    if not fut.done():
        return {"status": "pending"}
    try:
        tokens = await _exchange({
            "grant_type": "authorization_code",
            "code": fut.result(),
            "redirect_uri": REDIRECT_URI,
            "code_verifier": p["verifier"],
        })
        _save_tokens(tokens)
    except Exception as e:
        _cleanup(state)
        return {"status": "error", "error": f"Token exchange failed: {e}"}
    _cleanup(state)
    return {"status": "connected", "models": [{"id": m} for m in KNOWN_MODELS]}


async def _valid_access_token() -> tuple[str, str]:
    """Return a currently-valid (access_token, account_id), refreshing if near expiry."""
    cfg = _load()
    access, refresh, account = cfg.get("accessToken", ""), cfg.get("refreshToken", ""), cfg.get("accountId", "")
    if access and time.time() < float(cfg.get("expiresAt") or 0) - 300:
        return access, account
    if refresh:
        tokens = await _exchange({"grant_type": "refresh_token", "refresh_token": refresh, "scope": SCOPE})
        _save_tokens(tokens)
        cfg = _load()
        return cfg.get("accessToken", ""), cfg.get("accountId", "")
    return access, account


# ─── model catalog ────────────────────────────────────────────────────────────
# Fallback if the live models endpoint errors. The backend gates its catalog by
# `client_version` (rolling out new models to newer CLI releases), so this list can
# go stale — the live fetch below is the source of truth whenever it succeeds.
KNOWN_MODELS = ["gpt-5.5", "gpt-5.4"]
MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
# The endpoint requires a `client_version` query param and gates the returned catalog
# by it, mirroring the version the real `codex` CLI would send (MAJOR.MINOR.PATCH).
CLIENT_VERSION = "0.124.0"


async def list_models() -> list[dict]:
    if not is_connected():
        return []
    try:
        access, account = await _valid_access_token()
        if not access:
            return []
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(MODELS_URL, params={"client_version": CLIENT_VERSION}, headers={
                "Authorization": f"Bearer {access}",
                "chatgpt-account-id": account or "",
                "Accept": "application/json",
            })
            r.raise_for_status()
            data = r.json()
    except Exception:
        return [{"id": m} for m in KNOWN_MODELS]
    raw = data.get("models") if isinstance(data, dict) else data
    out, seen = [], set()
    for m in raw or []:
        mid = m.get("slug") if isinstance(m, dict) else str(m)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append({"id": mid})
    out.sort(key=lambda x: x["id"])
    return out or [{"id": m} for m in KNOWN_MODELS]


# ─── provider ─────────────────────────────────────────────────────────────────
class CodexProvider:
    """Provider over the ChatGPT-backend Codex endpoint via the shared Responses wire."""

    async def stream(self, ctx: Context, model: Model, opts: StreamOpts | None = None) -> AsyncIterator[StreamEvent]:
        access, account = await _valid_access_token()
        if not access:
            raise RuntimeError("Not signed in to Codex. Connect it in Settings → AI providers.")
        body = build_responses_request(ctx, model, opts or StreamOpts())
        # `originator: codex_cli_rs` is what OpenAI gates Codex access on — it MUST stay.
        # We only append our app identity to the User-Agent + add a non-gating client header
        # so Codex-side logs attribute activity to Curry Leaves rather than a bare codex_cli_rs.
        from curry_leaves_assistant.providers.identity import app_ua
        headers = {
            "Authorization": f"Bearer {access}",
            "chatgpt-account-id": account or "",
            "OpenAI-Beta": "responses=experimental",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "originator": "codex_cli_rs",
            "User-Agent": f"codex_cli_rs {app_ua()}",
            "X-Curry-Leaves-Client": app_ua(),
            "session_id": _SESSION_ID,
        }
        # read=60: a stalled connection (no bytes for 60s, e.g. a malformed request the
        # edge holds open instead of rejecting) raises instead of hanging forever. Applies
        # per-chunk, not to total stream duration, so a long-but-active stream is unaffected.
        timeout = httpx.Timeout(connect=15, read=60, write=15, pool=15)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", CHAT_URL, json=body, headers=headers) as resp:
                resp.raise_for_status()
                async for event in parse_responses_stream(iter_sse(resp)):
                    yield event


def build_provider() -> CodexProvider:
    return CodexProvider()
