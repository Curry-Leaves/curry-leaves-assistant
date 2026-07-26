"""Point CURRY_LEAVES_DIR at a throwaway dir BEFORE the store modules import their paths.

core.paths reads CURRY_LEAVES_DIR at import time, and the stores bind path constants by value
(e.g. `from ...paths import TODOS_PATH`). Setting the env var here, at collection time, means
every test writes into an isolated temp tree instead of the developer's real ~/.curry-leaves.
"""
from __future__ import annotations

import os
import tempfile

import pytest

# Set once, before curry_leaves_assistant.core.paths is first imported anywhere.
_TMP = tempfile.mkdtemp(prefix="curry-leaves-test-")
os.environ["CURRY_LEAVES_DIR"] = _TMP


@pytest.fixture(autouse=True)
def _clean_data_dir():
    """Each test starts from an empty data dir so todos/pool items don't leak between tests."""
    from curry_leaves_assistant.core import paths
    for f in (paths.TODOS_PATH, paths.REMINDERS_PATH):
        if f.exists():
            f.unlink()
    if paths.POOL_DIR.exists():
        for p in paths.POOL_DIR.glob("*.json"):
            p.unlink()
    paths.ensure_dirs()
    yield


@pytest.fixture
def events_sink(monkeypatch):
    """Capture emitted events as (type, payload) tuples so tests can assert on them without
    driving the real event loop / trigger machinery."""
    seen: list[tuple[str, dict | None]] = []

    def _fake_emit(event_type, payload=None, entity_id=None, label=None, **kw):
        seen.append((event_type, payload))
        return {"type": event_type, "payload": payload}

    # Patch emit on every module that imported it by value.
    from curry_leaves_assistant.stores import data as data_mod
    from curry_leaves_assistant.stores import pool_store as pool_mod
    monkeypatch.setattr(data_mod.events, "emit", _fake_emit)
    monkeypatch.setattr(pool_mod.events, "emit", _fake_emit)
    return seen
