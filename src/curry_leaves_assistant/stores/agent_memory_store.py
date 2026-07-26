"""Per-agent PRIVATE memory — how a single agent does its job.

The companion to profile_store.py (the SHARED user profile). The split is by what a durable fact
is ABOUT:
  • about the USER (name, prefs) — every agent should know it   → profile_store  (shared)
  • about how THIS agent works (its filing convention, a caller → agent_memory_store (private)
    quirk it learned) — noise in any other agent's prompt

Both are notes in the app's single bundle (domain/memory.py); a private note carries
``type: private`` plus an ``agent:`` field naming its owner, and is only ever read back for that
agent. A tool discovers its owning agent via ``trace_ctx.current_agent_id()`` — never passed in.
"""
from __future__ import annotations

import threading

from curry_leaves_assistant.core.paths import PRIVATE_DIR, safe_agent_seg
from curry_leaves_assistant.stores._memory_scope import MemoryScope

# One scope object per agent (they're cheap views over the one bundle), created lazily + cached.
_scopes: dict[str, MemoryScope] = {}
_lock = threading.Lock()


def _scope(agent_id: str) -> MemoryScope:
    st = _scopes.get(agent_id)
    if st is not None:
        return st
    with _lock:
        if agent_id not in _scopes:
            seg = safe_agent_seg(agent_id)
            _scopes[agent_id] = MemoryScope(dir_rel=f"{PRIVATE_DIR}/{seg}", type="private",
                                            id_prefix="mem_", agent=agent_id)
        return _scopes[agent_id]


def upsert(agent_id: str, text: str, *, type: str = "convention", subject: str | None = None,
           source: str = "inferred", confidence: float = 0.8) -> dict:
    """Record/correct one private note for this agent. Subject-dedupe updates in place."""
    rec = _scope(agent_id).upsert(text, subject=subject, source=source, confidence=confidence)
    # `type` is the app's flavour (convention/fact/preference); the note's cl_memory type stays
    # `private` (that's what partitions the bundle), so surface the flavour back to callers.
    return {**rec, "type": type}


def recall(agent_id: str, query: str, *, limit: int = 5) -> list[dict]:
    """This agent's private notes matching a query — by MEANING when the embedder is ready."""
    return _scope(agent_id).recall(query, limit=limit)


def list_all(agent_id: str) -> list[dict]:
    """Every note this agent holds (newest-updated first) — for the UI panel."""
    return _scope(agent_id).list_all()


def touch(agent_id: str, mem_ids: list[str]) -> None:
    """Bump `uses` on notes this agent just recalled (best-effort)."""
    _scope(agent_id).touch(mem_ids)


def forget(agent_id: str, mem_id: str) -> bool:
    """Soft-delete a note (-> _archive/, restorable). False if it wasn't this agent's."""
    return _scope(agent_id).forget(mem_id)


def close_thread_conns() -> None:
    """No-op: the one bundle owns its connections (kept for callers/tests)."""
