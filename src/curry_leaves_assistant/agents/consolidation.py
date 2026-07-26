"""The consolidation summarizer — the one LLM step in the memory model.

cl_memory clusters related episodes mechanically (shared tag AND shared link, inside a time
window) and never bundles a model. This is the ``summarize`` hook it calls to fold one cluster
into a durable ``type: consolidated`` note; the framework then links the source episodes into it
as provenance and archives them.

It lives in ``agents/`` because it drives the kernel, and is injected DOWN into ``domain/memory``
by app.py at boot (``memory.set_summarizer``) — domain can't import agents. Without it,
consolidation is a no-op that still reports candidates, which is exactly what should happen when
no AI provider is configured.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pydantic

_INSTRUCTIONS = (
    "You consolidate a cluster of related episodic memories (dated records of past agent runs) "
    "into ONE durable note. Read the episodes and produce a single title, a one-sentence "
    "description capturing the lasting takeaway, and a body that states what was learned and "
    "cites the episodes. Be faithful — do not invent anything the episodes don't say."
)


class _Consolidation(pydantic.BaseModel):
    title: str
    description: str
    body: str


def _format(episodes: list[dict[str, Any]]) -> str:
    lines = ["Consolidate these related episodes into one durable memory:\n"]
    for i, e in enumerate(episodes, 1):
        fm = e.get("frontmatter", e)
        when = fm.get("occurred") or fm.get("timestamp") or ""
        lines.append(f"{i}. [{when}] {fm.get('title') or e.get('path')}: "
                     f"{fm.get('description') or ''}")
        body = (e.get("body") or "").strip()
        if body:
            lines.append(f"   {body[:500]}")
    return "\n".join(lines)


async def _run(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    from curry_leaves import Agent, Runner

    from curry_leaves_assistant.agents import agent_engine
    from curry_leaves_assistant.core import settings as app_settings

    # The app default the user set in Settings. No silent auto-detect: if nothing is configured,
    # raise — the framework treats that as "leave this cluster alone" and the Gardener still
    # reports the candidate.
    name, api_key, cfg_model = app_settings.active_ai()
    if not name:
        raise RuntimeError("no default AI provider configured — skipping consolidation")
    provider = agent_engine._make_provider(name, api_key)
    model = cfg_model or agent_engine.default_model_id(name)
    agent = Agent(model, provider=provider, instructions=_INSTRUCTIONS,
                  output_type=_Consolidation)
    result = await Runner(agent).run(_format(episodes))
    out = result.output
    if not isinstance(out, _Consolidation):  # no structured output — keep the prose
        return {"title": "Consolidated memory", "description": "", "body": result.output_text}
    return {"title": out.title, "description": out.description, "body": out.body}


def summarize(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold a cluster of episodes into ``{title, description, body}``.

    cl_memory's hook is sync and runs on the gardener's worker thread, so driving the async
    kernel with ``asyncio.run`` is safe — no loop is running there. Raising is fine: the
    framework treats a failed summary as "leave this cluster alone"."""
    return asyncio.run(_run(episodes))


__all__ = ["summarize"]
