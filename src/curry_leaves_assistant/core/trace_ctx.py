"""Correlation context for tracing — a contextvar holding the current span.

`span(kind, name, ...)` opens a child of the current span (a new trace when there's no
parent), records open→close to the trace store, and restores the parent on exit. Contextvars
propagate across ``await``, ``asyncio.create_task`` (copied at creation), and
``asyncio.to_thread`` (copied), so anything running *inside* a span nests automatically.

`activate(traceId, spanId)` re-enters an existing trace in a fresh task/thread (deferred
nesting across the event→job→run boundary) without writing a span — the parent already exists.
"""
from __future__ import annotations

import contextlib
import contextvars
import time
import uuid
from datetime import datetime, timezone

from curry_leaves_assistant.stores import trace_store


# Current span reference: {"traceId", "spanId", "kind"} — minimal, just enough to parent children.
_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("trace_current", default=None)

# The agent id of the innermost running agent (set for the duration of run_agent /
# stream_agent). Lets a tool (e.g. save_output) know "who is calling me" without
# threading an extra param through Runner/Context — a tool never gets to claim it's
# a different agent's output.
_current_agent_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_agent_id", default=None)


def current_agent_id() -> str | None:
    return _current_agent_id.get()


@contextlib.contextmanager
def agent_scope(agent_id: str):
    token = _current_agent_id.set(agent_id)
    try:
        yield
    finally:
        _current_agent_id.reset(token)

_AGENT_KINDS = ("agent_run", "subagent_run", "tool_call")
_prune_every = 0


def current() -> dict | None:
    return _current.get()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:12]


class SpanHandle:
    """Returned by `span()`; lets the body add attributes / mark status before close."""
    __slots__ = ("trace_id", "span_id", "kind", "attributes", "status", "error")

    def __init__(self, trace_id: str, span_id: str, kind: str, attributes: dict):
        self.trace_id = trace_id
        self.span_id = span_id
        self.kind = kind
        self.attributes = attributes
        self.status = "ok"
        self.error: str | None = None

    def attr(self, **kw) -> None:
        self.attributes.update({k: v for k, v in kw.items() if v is not None})

    def set_error(self, exc) -> None:
        self.error = f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc)
        self.status = "error"


def is_agent_context() -> bool:
    """True when the current span is an agent run or a tool call (→ a new run nested here is a
    sub-agent, not a top-level run)."""
    cur = _current.get()
    return bool(cur and cur.get("kind") in _AGENT_KINDS)


@contextlib.contextmanager
def span(kind: str, name: str, *, attributes: dict | None = None, **attrs):
    parent = _current.get()
    trace_id = parent["traceId"] if parent else _id("tr_")
    span_id = _id("sp_")
    parent_span_id = parent["spanId"] if parent else None
    a = dict(attributes or {})
    a.update(attrs)
    started = _now()
    t0 = time.monotonic()

    trace_store.write_span({
        "traceId": trace_id, "spanId": span_id, "parentSpanId": parent_span_id,
        "kind": kind, "name": name, "status": "running", "startedAt": started, "attributes": a,
    })
    handle = SpanHandle(trace_id, span_id, kind, a)
    token = _current.set({"traceId": trace_id, "spanId": span_id, "kind": kind})
    try:
        yield handle
    except BaseException as exc:
        handle.set_error(exc)
        raise
    finally:
        _current.reset(token)
        rec = {
            "traceId": trace_id, "spanId": span_id, "parentSpanId": parent_span_id,
            "kind": kind, "name": name, "status": handle.status, "startedAt": started,
            "endedAt": _now(), "durationMs": int((time.monotonic() - t0) * 1000),
            "attributes": handle.attributes,
        }
        if handle.error:
            rec["error"] = handle.error
        trace_store.write_span(rec)
        _maybe_prune()


def open_span(kind: str, name: str, *, attributes: dict | None = None) -> tuple:
    """Open a span without a context manager — safe to use across async generator yields.

    Returns (handle, t0, parent_span_id) needed to close it. Does NOT set the ContextVar
    (that would need a reset in the same context). Use close_span() when done."""
    parent = _current.get()
    trace_id = parent["traceId"] if parent else _id("tr_")
    span_id = _id("sp_")
    parent_span_id = parent["spanId"] if parent else None
    a = dict(attributes or {})
    started = _now()
    t0 = time.monotonic()
    trace_store.write_span({
        "traceId": trace_id, "spanId": span_id, "parentSpanId": parent_span_id,
        "kind": kind, "name": name, "status": "running", "startedAt": started, "attributes": a,
    })
    return SpanHandle(trace_id, span_id, kind, a), t0, started, parent_span_id


def close_span(handle: SpanHandle, t0: float, started: str, parent_span_id: str | None) -> None:
    """Close a span opened with open_span()."""
    rec = {
        "traceId": handle.trace_id, "spanId": handle.span_id, "parentSpanId": parent_span_id,
        "kind": handle.kind, "name": handle.kind, "status": handle.status,
        "startedAt": started, "endedAt": _now(),
        "durationMs": int((time.monotonic() - t0) * 1000),
        "attributes": handle.attributes,
    }
    if handle.error:
        rec["error"] = handle.error
    trace_store.write_span(rec)
    _maybe_prune()


@contextlib.contextmanager
def activate(trace_id: str | None, span_id: str | None, kind: str = "event"):
    """Continue an existing trace under (trace_id, span_id) in a new task/thread.

    Uses set() without reset() because this is called inside asyncio.create_task(),
    which runs in a *copy* of the parent context. The token is bound to that copy;
    resetting it in a finally that may execute in the parent context raises ValueError.
    The ContextVar is task-local anyway, so no cleanup is needed — the copy is
    discarded when the task exits."""
    if not trace_id or not span_id:
        yield
        return
    _current.set({"traceId": trace_id, "spanId": span_id, "kind": kind})
    yield


def _maybe_prune() -> None:
    # Cheap amortized pruning: every ~200 closed spans, trim old trace files.
    global _prune_every
    _prune_every += 1
    if _prune_every % 200 == 0:
        try:
            trace_store.prune()
        except Exception:
            pass
