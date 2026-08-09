"""Codex ships no OAuth client id — sign-in requires the user to supply their own.

The bundled Codex CLI client id was removed deliberately, so the thing worth guarding is that it
(or any other default) doesn't creep back: a hardcoded fallback would silently restore a client
id we don't own. The rest covers the contract the UI leans on — `configured` telling the
Providers screen to warn *before* the user clicks Connect, and start_login failing as a normal
error payload rather than an exception, since that's the shape the card renders.
"""
from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from curry_leaves_assistant.providers import codex


@pytest.fixture(autouse=True)
def _no_client_id(monkeypatch):
    monkeypatch.delenv(codex.CLIENT_ID_ENV, raising=False)


def test_no_hardcoded_client_id_in_source():
    """No OpenAI-style client id literal anywhere in the module — including the one we removed."""
    src = inspect.getsource(codex)
    assert "app_EMoamEEZ73f0CkXaXp7hrann" not in src
    assert not re.search(r"[\"']app_[A-Za-z0-9]{16,}[\"']", src)


def test_client_id_requires_env(monkeypatch):
    with pytest.raises(RuntimeError):
        codex._client_id()
    monkeypatch.setenv(codex.CLIENT_ID_ENV, "app_mine")
    assert codex._client_id() == "app_mine"


def test_blank_env_is_not_a_client_id(monkeypatch):
    """A set-but-empty var must fail like an unset one, not send an empty client_id."""
    monkeypatch.setenv(codex.CLIENT_ID_ENV, "   ")
    assert codex.has_client_id() is False
    with pytest.raises(RuntimeError):
        codex._client_id()


def test_has_client_id_tracks_env(monkeypatch):
    assert codex.has_client_id() is False
    monkeypatch.setenv(codex.CLIENT_ID_ENV, "app_mine")
    assert codex.has_client_id() is True


def test_start_login_returns_error_without_client_id():
    """The UI renders `error` from this payload, so it must not raise — and must not bind the
    loopback port before it knows a sign-in can even proceed."""
    res = asyncio.run(codex.start_login())
    assert res["status"] == "error"
    assert codex.CLIENT_ID_ENV in res["error"]
    assert not codex._pending
