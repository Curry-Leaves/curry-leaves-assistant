"""Causal loop guard — stops an agent whose tool emits an event that re-triggers itself
(directly or through a chain) from fanning out without bound.

Uses the trigger's trace: every agent run in a causal chain shares one traceId (an emitted
event inherits the running trace), and each run roots a span carrying its `agentId`. So the
agent-run spans already in that trace ARE the chain — count them for depth, and reject
re-entry of the same agent.
"""
from __future__ import annotations

import os

# Cap on the causal chain length (event → job → event → job …) rooted at one trace.
MAX_CAUSAL_DEPTH = int(os.environ.get("CURRY_LEAVES_MAX_CAUSAL_DEPTH", "12"))


def loop_refusal(agent_id: str | None, trigger: dict) -> str | None:
    """Return a reason string if running `agent_id` for `trigger` would extend a runaway
    causal loop, else None."""
    trace_id = trigger.get("traceId")
    if not trace_id or not agent_id:
        return None  # untraced (user action / schedule) — no chain to bound
    try:
        from curry_leaves_assistant.stores import trace_store
        agents_in_chain = [
            (s.get("attributes") or {}).get("agentId")
            for s in trace_store.read_spans(trace_id)
            if (s.get("attributes") or {}).get("agentId")
        ]
    except Exception:
        return None  # never let the guard's own failure block legitimate work
    if len(agents_in_chain) >= MAX_CAUSAL_DEPTH:
        return f"causal depth {len(agents_in_chain)} ≥ {MAX_CAUSAL_DEPTH}"
    if agent_id in agents_in_chain:
        return f"agent {agent_id} already ran in this chain"
    return None
