"""Health, event history, traces, and the token-usage ledger.

The LIVE event feed is served over the shared WebSocket (api/ws.py) now; only the
persisted history endpoint (used for initial hydration) lives here.
"""
from __future__ import annotations

from fastapi import APIRouter, Response

from curry_leaves_assistant.core import events
from curry_leaves_assistant.stores import trace_store, usage_store

router = APIRouter(tags=["system"])


@router.get("/health")
def health():
    return {"ok": True}


@router.get("/capabilities")
def capabilities():
    """Optional runtime features the frontend gates UI on. `tts` is true only when the
    Kokoro package imports (it's a heavy dep + needs the espeak-ng binary) — the chat
    Voice button hides itself when it's false. `wakeWord` is true once the ONNX chain
    is on disk; the Ask AI listener stays dark until then. `fileBrowse` is true only on a
    local (loopback) deployment — in Docker/web the browsable folders would be the
    server's, so the composer's @file option hides itself."""
    from curry_leaves_assistant.domain import tts, wakeword
    from curry_leaves_assistant.stores import files_store
    return {"tts": tts.available(), "wakeWord": wakeword.available(),
            "fileBrowse": files_store.enabled()}


# ─── Event history (live feed is on the WebSocket) ────────────────────────────
@router.get("/events/recent")
def events_recent(limit: int = 50):
    return events.recent_events(limit)


# ─── Traces (causal spans: events · agent runs · LLM turns · tools) ────────────
@router.get("/traces")
def traces_list(limit: int = 50):
    return trace_store.list_traces(limit)


@router.get("/traces/{trace_id}")
def trace_get(trace_id: str):
    spans = trace_store.get_trace(trace_id)
    return {"traceId": trace_id, "spans": spans} if spans else Response(status_code=404)


@router.delete("/traces/{trace_id}")
def trace_delete(trace_id: str):
    return {"ok": trace_store.delete(trace_id)}


@router.delete("/traces")
def traces_clear():
    return {"cleared": trace_store.clear()}


# ─── Token usage (durable ledger; never pruned) ───────────────────────────────
@router.get("/usage")
def usage_summary(days: int | None = None):
    return usage_store.summary(days)
