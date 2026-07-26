"""Centralized eventing.

One ``emit()`` does three things:
  1. appends the event to ~/.curry-leaves/events/log.ndjson (durable),
  2. fans out to live SSE subscribers (drives the Agents activity feed),
  3. runs registered trigger handlers (the agent pool enqueues matching work).

Decoupled by design: this module imports nothing app-specific. The pool calls
``on_event()`` to register itself. Events carry a full ``payload`` so a handler
can complete the work standalone.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any, Callable, Optional

from curry_leaves_assistant.core.paths import EVENTS_LOG
from curry_leaves_assistant.core.store import append_ndjson, now_iso, tail_lines
from curry_leaves_assistant.core.ws_hub import hub

# The event log is append-only; cap it so it can't grow without bound. recent_events
# and the activity feed only ever look at the newest entries, so older lines are dead
# weight. Overridable for tests / heavy-history setups.
_EVENTS_KEEP = int(os.environ.get("CURRY_LEAVES_EVENTS_KEEP", "5000"))


def recent_events(limit: int = 50) -> list[dict]:
    """Read the last `limit` events from the durable log, newest first.

    Tails the file (bounded by `limit`) instead of loading the whole log, so cost
    doesn't grow with total history."""
    out = []
    for line in tail_lines(EVENTS_LOG, limit):
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    out.reverse()
    return out


# How far back a reconnecting client can ask us to replay. Cursor older than this
# window → we can't guarantee no gap, so the client is told to reset (refetch + go live).
_REPLAY_WINDOW = 500


def events_since(event_id: str, cap: int = _REPLAY_WINDOW) -> tuple[list[dict], bool]:
    """Events emitted after `event_id`, oldest first, for reconnect replay.

    Returns ``(events, found)``: if `event_id` is within the recent window, `found` is
    True and `events` are the ones after it; if it's unknown/too old, `found` is False
    (the caller should tell the client to reset — refetch state, then go live)."""
    recent = list(reversed(recent_events(cap)))  # oldest → newest
    for i, ev in enumerate(recent):
        if ev.get("id") == event_id:
            return recent[i + 1:], True
    return [], False

# Canonical event types, in editor-display order. Keep complete: agents can trigger on
# any of these (except agent.run.*, which the pool never re-triggers on — see pool.py).
EVENT_TYPES = [
    "recording.created",
    "recording.finalized",
    "recording.transcribed",
    "recording.summarized",
    "recording.output.saved",
    "recording.outputs.completed",
    "recording.updated",
    "recording.deleted",
    "todo.created",
    "todo.completed",
    "todo.updated",
    "todo.deleted",
    "reminder.created",
    "reminder.updated",
    "reminder.deleted",
    "reminder.due",
    "pool.item.created",
    "pool.item.assigned",
    "pool.item.done",
    "knowledge.ingest.requested",
    "knowledge.ingested",
    "knowledge.conflict.detected",
    "knowledge.maintenance.completed",
    # Learning signals — a mechanical detector spotted something worth reflecting on (a
    # failure that later recovered, an inefficient run, a user correction, a repeated task).
    # The Skill Learner triggers on this and turns the evidence into procedural/semantic memory.
    "learn.signal",
    "agent.run.started",
    "agent.run.completed",
    "agent.run.failed",
    "agent.run.needs_input",  # a background run suspended awaiting a human approval/answer
    "agent.run.input_received",  # …and the answer arrived — the run resumes (clears the waiting state)
    "agent.job.dead",       # a poison job was quarantined (dead-lettered) after repeated crashes
    "agent.job.refused",    # an enqueue was refused to break a runaway causal loop
    "tile.run.started",
    "tile.run.completed",
    "tile.run.failed",
    "tile.alert.raised",
    # UI-only signal so a second client viewing the same chat session can mirror the run
    # live. Never a trigger (see trigger_types) — it exists purely for cross-client discovery.
    "chat.run.started",
    # Meeting-template edits — UI-only, so an open Capture/Settings screen live-refreshes its
    # template list. Never triggers agents (see _NON_TRIGGER_PREFIXES).
    "template.created",
    "template.updated",
    "template.deleted",
    # AI readiness — UI-only signal so open clients live-update the "no provider / no
    # default model" warning banner. Never triggers agents (see _NON_TRIGGER_PREFIXES).
    "system.ai.status",
]

# Event prefixes that never fire agent-trigger matching (avoid agent→agent loops, and keep
# purely-UI / pool-internal signals from spawning work).
_NON_TRIGGER_PREFIXES = ("agent.run.", "agent.job.", "chat.run.", "template.", "system.")


def trigger_types() -> list[str]:
    """Event types an agent can be triggered on — everything except the UI/lifecycle
    signals in _NON_TRIGGER_PREFIXES (which would otherwise cause agent→agent loops)."""
    return [t for t in EVENT_TYPES if not t.startswith(_NON_TRIGGER_PREFIXES)]

_trigger_handlers: list[Callable[[dict], None]] = []
_loop: Optional[asyncio.AbstractEventLoop] = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called once at startup; retained for symmetry with ws_hub.set_loop (the hub owns
    cross-thread event delivery now)."""
    global _loop
    _loop = loop


def on_event(handler: Callable[[dict], None]) -> None:
    """Register a trigger handler (e.g. the agent pool's enqueue function)."""
    _trigger_handlers.append(handler)


def emit(event_type: str, payload: dict[str, Any] | None = None,
         entity_id: str | None = None, label: str | None = None) -> dict:
    """Build, log, fan out, and dispatch an event. Safe to call from any context.

    When emitted inside a trace (an agent run, tool call, or a propagated root), the event is
    stamped with `traceId` + `causedBy` and recorded as a span; the event's own `spanId` is set
    BEFORE trigger handlers run, so a triggered job carries this span as the parent of the run
    it spawns (deferred nesting). Outside any trace, the event is plain (untraced)."""
    from curry_leaves_assistant.core import trace_ctx

    parent = trace_ctx.current()
    event = {
        "id": uuid.uuid4().hex,
        "type": event_type,
        "occurredAt": now_iso(),
        "entityId": entity_id,
        "label": label,
        "payload": payload or {},
    }
    if parent:
        event["traceId"] = parent["traceId"]
        event["causedBy"] = parent["spanId"]

    append_ndjson(EVENTS_LOG, event, max_lines=_EVENTS_KEEP)

    # Fan out to the WebSocket hub's `events` channel (thread-safe; the hub bounces to
    # its own loop). This is the live feed to the UI.
    try:
        hub.publish_event(event)
    except Exception:
        pass

    def _dispatch():
        for handler in list(_trigger_handlers):  # fast: should just enqueue a job file
            try:
                handler(event)
            except Exception as exc:  # never let a handler break emit()
                print(f"[events] trigger handler error: {exc}", flush=True)

    if parent:
        # Record the event as a span, and run trigger handlers INSIDE it so any enqueued job
        # carries this event-span as its parent.
        with trace_ctx.span("event", event_type,
                            attributes={"type": event_type, "entityId": entity_id,
                                        "label": label, "payload": payload or {},
                                        "causedBy": parent["spanId"]}) as h:
            event["spanId"] = h.span_id
            _dispatch()
    else:
        _dispatch()

    return event
