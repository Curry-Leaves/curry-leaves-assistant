"""Auth (numeric PIN lock)."""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from curry_leaves_assistant.core import auth
from curry_leaves_assistant.core import settings as app_settings

router = APIRouter(tags=["auth"])


class LoginBody(BaseModel):
    pin: str


@router.get("/auth/status")
def auth_status():
    """Whether a PIN has been set yet (drives the login vs. first-run set-PIN UI).
    Also surfaces the identity name (non-sensitive) so the lock screen can greet
    the user by name before they've authenticated."""
    return {"configured": auth.is_configured(), "identityName": app_settings.identity_cfg().get("name", "")}


@router.post("/auth/login")
def auth_login(body: LoginBody):
    """Exchange a PIN for a bearer token. First login sets the PIN."""
    pin = (body.pin or "").strip()
    if not pin.isdigit() or not (4 <= len(pin) <= 12):
        return Response(content="PIN must be 4–12 digits", status_code=400)
    if not auth.is_configured():
        auth.set_pin(pin)  # first run: this PIN becomes the passcode
    elif not auth.verify(pin):
        return Response(content="Incorrect PIN", status_code=401)
    return {"token": auth.issue_token()}


@router.post("/auth/logout")
def auth_logout(request: Request):
    auth.revoke(auth.token_from_request(request))
    return {"ok": True}


@router.get("/auth/verify")
def auth_verify():
    """Gated by AuthMiddleware — reaching here means the caller's token is still valid.

    Lets the renderer confirm a token cached from a previous run (localStorage)
    survived the backend restart before it trusts it and mounts the authed app.
    """
    return {"ok": True}
