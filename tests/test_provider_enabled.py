"""The per-provider enable flag, and the gates that must respect it.

The flag's whole point is that *connected* and *enabled* are independent: a provider can hold a
valid key and still be switched off. Three separate places have to agree about that — the
readiness snapshot the UI renders, the run-time gate in build_runner, and the model catalog the
pickers read — and they historically drifted, so each gets a test.
"""
from __future__ import annotations

import asyncio

import pytest

from curry_leaves_assistant.core import paths
from curry_leaves_assistant.core import settings as app_settings


@pytest.fixture(autouse=True)
def _clean_settings():
    """settings.json lives in the shared temp dir, so wipe it between tests — a leftover
    `enabled: false` would otherwise silently poison every later case."""
    if paths.SETTINGS_PATH.exists():
        paths.SETTINGS_PATH.unlink()
    yield
    if paths.SETTINGS_PATH.exists():
        paths.SETTINGS_PATH.unlink()


def _connect(provider: str = "anthropic", **extra) -> None:
    app_settings.patch_ai({
        "active": provider,
        "providers": {provider: {"apiKey": "sk-test", "model": "claude-sonnet-4-6", **extra}},
    })


def test_absent_flag_means_enabled():
    """Existing installs (and providers with no saved cfg at all) predate the flag and must not
    go dark on upgrade."""
    assert app_settings.provider_enabled("groq") is True   # no cfg saved whatsoever
    _connect()
    assert app_settings.provider_enabled("anthropic") is True


def test_disable_survives_a_round_trip_and_keeps_the_key():
    _connect()
    app_settings.patch_ai({"providers": {"anthropic": {"enabled": False}}})
    assert app_settings.provider_enabled("anthropic") is False
    # Disabling must not tear down the credential — that's what Disconnect is for.
    assert app_settings.provider_cfg("anthropic")[0] == "sk-test"
    app_settings.patch_ai({"providers": {"anthropic": {"enabled": True}}})
    assert app_settings.provider_enabled("anthropic") is True


def test_readiness_reports_provider_disabled_not_no_provider():
    """A connected-but-off provider gets its own reason: telling the user nothing is connected
    about a provider they can see is connected reads as a bug, and the fix is a different one."""
    from curry_leaves_assistant.agents import readiness
    _connect()
    assert readiness.ai_status()["ready"] is True

    app_settings.patch_ai({"providers": {"anthropic": {"enabled": False}}})
    status = readiness.ai_status()
    assert status["ready"] is False
    assert status["reason"] == "provider_disabled"
    assert "anthropic" in status["detail"]


def test_build_runner_refuses_a_disabled_default_provider():
    from curry_leaves_assistant.agents import agent_engine
    _connect()
    app_settings.patch_ai({"providers": {"anthropic": {"enabled": False}}})

    with pytest.raises(RuntimeError, match="turned off"):
        asyncio.run(agent_engine.build_runner({"id": "a", "name": "A"}))


def test_build_runner_refuses_a_disabled_pinned_provider():
    """A pinned provider must fail loudly rather than fall back to the app default: the agent's
    author chose it deliberately, and running somewhere else changes behaviour AND cost."""
    from curry_leaves_assistant.agents import agent_engine
    # A *different* provider is the working default, so a silent fallback would have succeeded.
    _connect("openai")
    app_settings.patch_ai({"providers": {"anthropic": {"apiKey": "sk-test", "enabled": False}}})

    with pytest.raises(RuntimeError, match="pinned"):
        asyncio.run(agent_engine.build_runner({"id": "a", "name": "A", "provider": "anthropic"}))


def test_ollama_is_enabled_delegates_to_the_shared_flag():
    """Ollama's Disconnect predates the general flag and writes the same key; the two readers
    must not be able to disagree."""
    from curry_leaves_assistant.providers import ollama
    app_settings.patch_ai({"providers": {"ollama": {"enabled": False}}})
    assert ollama.is_enabled() is False
    assert app_settings.provider_enabled("ollama") is False
    app_settings.patch_ai({"providers": {"ollama": {"enabled": True}}})
    assert ollama.is_enabled() is True
