"""Simple PIN lock for the local backend.

Curry Leaves runs as a single-user desktop app against a localhost FastAPI server, so
"auth" here is a numeric passcode that gates the API — not multi-user identity.

- The PIN is stored hashed (PBKDF2-HMAC-SHA256 + per-install salt) in
  ``~/.curry-leaves/auth.json``; the plain PIN never touches disk.
- On first login (no PIN configured yet) the submitted PIN *becomes* the PIN.
- A successful login mints a random bearer token held in memory only, so tokens
  die with the process (every app launch starts a fresh backend → re-login).

The ``AuthMiddleware`` rejects any HTTP request lacking a valid token, except the
public endpoints below. Websockets bypass HTTP middleware, so they check the
token themselves via a ``?token=`` query param (see ``token_ok``).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from curry_leaves_assistant.core.paths import DATA_DIR
from curry_leaves_assistant.core.store import read_json_safe, write_json

AUTH_PATH = DATA_DIR / "auth.json"

# Endpoints reachable without a token (login flow + health probe). Everything
# else requires a valid bearer token.
PUBLIC_PATHS = {"/health", "/auth/status", "/auth/login", "/auth/logout"}

# Active bearer tokens (in-memory only — wiped on restart).
_TOKENS: set[str] = set()

_ITERATIONS = 120_000


def _hash(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), _ITERATIONS).hex()


def is_configured() -> bool:
    return bool(read_json_safe(AUTH_PATH, {}).get("hash"))


def set_pin(pin: str) -> None:
    salt = secrets.token_hex(16)
    write_json(AUTH_PATH, {"salt": salt, "hash": _hash(pin, salt)})
    try:
        AUTH_PATH.chmod(0o600)
    except Exception:
        pass


def seed_pin_from_env() -> None:
    """Pre-seed the PIN from ``CURRY_LEAVES_PIN`` when none is configured.

    For headless/Docker deployments: without this, a network-exposed backend sits in
    setup mode (whole API open) until someone reaches the UI and completes the wizard.
    A no-op once a PIN exists, so it never overrides one the user chose.
    """
    pin = (os.environ.get("CURRY_LEAVES_PIN") or "").strip()
    if pin and pin.isdigit() and 4 <= len(pin) <= 12 and not is_configured():
        set_pin(pin)


def verify(pin: str) -> bool:
    rec = read_json_safe(AUTH_PATH, {})
    if not rec.get("hash") or not rec.get("salt"):
        return False
    return hmac.compare_digest(_hash(pin, rec["salt"]), rec["hash"])


def issue_token() -> str:
    tok = secrets.token_hex(32)
    _TOKENS.add(tok)
    return tok


def token_ok(tok: str) -> bool:
    return bool(tok) and tok in _TOKENS


def revoke(tok: str) -> None:
    _TOKENS.discard(tok)


def token_from_request(request) -> str:
    """Pull the bearer token from the Authorization header or a ?token= param.

    EventSource (SSE) and WebSocket can't set custom headers, so they pass the
    token as a query param instead.
    """
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return request.query_params.get("token", "")


def _is_static_frontend_request(request) -> bool:
    """True for GETs that don't match any API route — i.e. they'll fall through to
    the built-frontend static mount (see app.py), which must load before login."""
    if request.method != "GET":
        return False
    from starlette.routing import Match
    for route in request.app.routes:
        if getattr(route, "name", None) == "frontend":
            continue
        if route.matches(request.scope)[0] != Match.NONE:
            return False
    return True


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        # CORS preflight + the login/health endpoints are always open, as is any
        # request that resolves to the static frontend (HTML/JS/CSS) rather than
        # an API route — the SPA needs to load before the user can log in.
        # Artifact share links (/a/<id>/<token>/...) are ALSO public: they carry their
        # own per-artifact capability token (checked in api/artifacts.py), by design
        # reachable without the app's PIN so a recipient can open/download with just
        # the link — see stores/artifact_store.check_token.
        if request.method == "OPTIONS" or path in PUBLIC_PATHS or path.startswith("/a/") \
                or _is_static_frontend_request(request):
            return await call_next(request)
        # Setup mode: before any PIN exists, the whole API is open. The first-run wizard
        # writes real state (identity, provider OAuth, model downloads) *before* it creates
        # the PIN that would mint a token, so it has nothing to authenticate with. This is
        # not a downgrade: while unconfigured, anyone who can reach the port can already
        # claim the install outright by POSTing their own PIN to /auth/login. Set
        # CURRY_LEAVES_PIN to pre-seed the PIN at boot and skip this window entirely.
        if not is_configured():
            return await call_next(request)
        if not token_ok(token_from_request(request)):
            return PlainTextResponse("Unauthorized", status_code=401,
                                     headers={"Access-Control-Allow-Origin": "*"})
        return await call_next(request)
