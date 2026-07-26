"""TracingHost — captures curry-leaves's internal run via its Host seam.

curry-leaves emits every engine event to `host.emit(...)` (assistant turns, tool start/end,
thinking, usage) in both `run()` and `stream()`, and routes approvals/questions through
`host.request(...)`. We attach a TracingHost (wrapping any real host) to every runner; it maps
those into leaf spans (`llm_turn`, `tool_call`, `approval`, `ask`) under the run's `agent_run`
span, and forwards everything to the inner host so chat's SSE/approvals keep working.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from curry_leaves_assistant.stores import trace_store

from curry_leaves_assistant.stores import usage_store

from curry_leaves.core.host import DefaultHost, AskUser, ApproveTool
from curry_leaves_assistant.core.textfmt import result_text as _result_text, trunc as _trunc
from curry_leaves_assistant.core.textfmt import args_text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sid() -> str:
    return "sp_" + uuid.uuid4().hex[:12]


def _args_text(args) -> str:
    return args_text(args, indent=None)  # spans store compact JSON


class TracingHost:
    """Wraps an inner Host; records spans for each engine event under `parent_span_id`."""

    def __init__(self, trace_id: str, parent_span_id: str, model: str | None = None, inner=None,
                 agent_id: str | None = None, surface: str | None = None):
        self.trace_id = trace_id
        self.parent = parent_span_id
        self.model = model
        self.agent_id = agent_id
        self.surface = surface
        self.inner = inner or DefaultHost()
        self.tokens_in = 0
        self.tokens_out = 0
        self._msg: dict | None = None         # current assistant message accumulator
        self._tools: dict[str, tuple] = {}    # tool_call_id -> (name, args, t0, started_iso)

    def _write(self, kind: str, name: str, started: str, t0: float, attrs: dict,
               status: str = "ok", error: str | None = None) -> None:
        rec = {
            "traceId": self.trace_id, "spanId": _sid(), "parentSpanId": self.parent,
            "kind": kind, "name": name, "status": status, "startedAt": started, "endedAt": _now(),
            "durationMs": int((time.monotonic() - t0) * 1000), "attributes": attrs,
        }
        if error:
            rec["error"] = error
        trace_store.write_span(rec)

    def subscribe(self, fn) -> callable:
        """Forward subscribe to the inner host so the session store gets events."""
        subscribe_fn = getattr(self.inner, "subscribe", None)
        if callable(subscribe_fn):
            return subscribe_fn(fn)
        return lambda: None

    # ── curry-leaves NOTIFY channel ──────────────────────────────────────────
    def emit(self, event) -> None:
        try:
            self._handle(event)
        except Exception:
            pass
        try:
            self.inner.emit(event)
        except Exception:
            pass

    def flush(self) -> None:
        """Write spans for any tools that started but never reported an end (interrupted /
        cancelled), so they still appear in the trace."""
        for _tid, (name, args, t0, started) in list(self._tools.items()):
            self._write("tool_call", name, started, t0,
                        {"name": name, "args": _trunc(args), "result": "(no result — interrupted)",
                         "isError": True}, status="error")
        self._tools.clear()

    def _handle(self, ev) -> None:
        t = getattr(ev, "type", None)
        if t == "subagent_activity":
            inner = getattr(ev, "event", None)
            if inner is not None:
                self._handle(inner)  # capture a sub-agent's tools/turns too
            return
        if t == "message_start":
            self._msg = {"t0": time.monotonic(), "started": _now(), "text": "", "thinking": ""}
        elif t == "message_update":
            d = getattr(ev, "delta", None)
            if d is not None and self._msg is not None:
                v = getattr(d, "value", "") or ""
                if getattr(d, "kind", None) == "thinking":
                    self._msg["thinking"] += v
                elif getattr(d, "kind", None) == "text":
                    self._msg["text"] += v
        elif t == "thinking" and self._msg is not None:
            self._msg["thinking"] += (getattr(ev, "text", "") or getattr(ev, "value", "") or "")
        elif t == "message_end":
            u = getattr(getattr(ev, "message", None), "usage", None)
            ti = (getattr(u, "input", 0) or 0) if u else 0
            to = (getattr(u, "output", 0) or 0) if u else 0
            cr = (getattr(u, "cache_read", 0) or 0) if u else 0
            cw = (getattr(u, "cache_write", 0) or 0) if u else 0
            self.tokens_in += ti
            self.tokens_out += to
            usage_store.record(ti, to, model=self.model, agent_id=self.agent_id, surface=self.surface,
                                cache_read=cr, cache_write=cw)
            m = self._msg or {"t0": time.monotonic(), "started": _now(), "text": "", "thinking": ""}
            if m["text"].strip() or m["thinking"].strip():
                self._write("llm_turn", "assistant turn", m["started"], m["t0"], {
                    "model": self.model, "text": _trunc(m["text"]), "thinking": _trunc(m["thinking"]),
                    "tokensIn": ti, "tokensOut": to,
                })
            self._msg = None
        elif t == "tool_start":
            tid = getattr(ev, "tool_call_id", "") or _sid()
            self._tools[tid] = (getattr(ev, "tool_name", ""), _args_text(getattr(ev, "args", None)),
                                time.monotonic(), _now())
        elif t == "tool_end":
            tid = getattr(ev, "tool_call_id", "")
            name, args, t0, started = self._tools.pop(
                tid, (getattr(ev, "tool_name", ""), "", time.monotonic(), _now()))
            res = getattr(ev, "result", None)
            err = bool(getattr(res, "is_error", False))
            self._write("tool_call", name, started, t0, {
                "name": name, "args": _trunc(args), "result": _trunc(_result_text(res)), "isError": err,
            }, status="error" if err else "ok")

    # ── curry-leaves REQUEST channel (approvals / asks) ──────────────────────
    async def request(self, req):
        t0, started = time.monotonic(), _now()
        if isinstance(req, ApproveTool):
            answer = await self.inner.request(req)
            self._write("approval", f"approve {req.tool}", started, t0, {
                "tool": req.tool, "risk": getattr(req, "risk", ""),
                "args": _trunc(_args_text(req.args)), "approved": bool(answer),
            }, status="ok" if answer else "error")
            return answer
        if isinstance(req, AskUser):
            answer = await self.inner.request(req)
            self._write("ask", "ask user", started, t0, {
                "question": req.question, "options": list(getattr(req, "options", []) or []),
                "answer": str(answer),
            })
            return answer
        return await self.inner.request(req)


class UsageHost:
    """Minimal host for un-traced runs (e.g. ephemeral System-Prompt edits): records token
    usage on every MessageEnd so no model call is ever missed from the ledger. Forwards to an
    inner host; requests pass straight through."""

    def __init__(self, model: str | None = None, agent_id: str | None = None,
                 surface: str | None = None, inner=None):
        self.model = model
        self.agent_id = agent_id
        self.surface = surface
        self.inner = inner or DefaultHost()

    def subscribe(self, fn) -> callable:
        """Forward subscribe to the inner host so the session store gets events."""
        subscribe_fn = getattr(self.inner, "subscribe", None)
        if callable(subscribe_fn):
            return subscribe_fn(fn)
        return lambda: None

    def emit(self, event) -> None:
        try:
            t = getattr(event, "type", None)
            if t == "subagent_activity":
                inner = getattr(event, "event", None)
                if inner is not None:
                    self.emit(inner)
            elif t == "message_end":
                u = getattr(getattr(event, "message", None), "usage", None)
                if u:
                    usage_store.record(getattr(u, "input", 0) or 0, getattr(u, "output", 0) or 0,
                                       model=self.model, agent_id=self.agent_id, surface=self.surface,
                                       cache_read=getattr(u, "cache_read", 0) or 0,
                                       cache_write=getattr(u, "cache_write", 0) or 0)
        except Exception:
            pass
        try:
            self.inner.emit(event)
        except Exception:
            pass

    async def request(self, req):
        return await self.inner.request(req)
