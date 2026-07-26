"""`recall_events` — look back over what has HAPPENED, from memory.

Distinct from two neighbours:
  • the `history` tool (`episodes` action) surfaces raw RUN STATS (steps/outcome) for the
    learning loop — telemetry, not memory;
  • this tool surfaces the CURATED events the nightly Memory Keeper distilled from conversations
    ("on the 12th we decided X") — prose worth referring back to, matched by meaning.
"""
from __future__ import annotations

from typing import Literal

from curry_leaves.core.tools import ToolResult
from pydantic import BaseModel, Field

from curry_leaves_assistant.stores import episode_store


class RecallEventsTool:
    """Recall notable EVENTS from memory — dated things that happened, distilled from past
    conversations. Read-only, matched by meaning."""
    name = "recall_events"
    description = (
        "Recall notable EVENTS from memory — action: recall. Each is a dated, curated record of "
        "something that happened (a decision, a milestone, something that went notably well or "
        "badly), distilled from past conversations. Ask in your own words — `query` matches by "
        "MEANING, so 'the budget decision' finds an event titled differently. Use it for 'what "
        "happened with X / when did we last decide Y'. For raw run telemetry (how many steps a "
        "task took) use the `history` tool instead."
    )
    risk = "read"

    class Args(BaseModel):
        action: Literal["recall"] = Field(default="recall", description="recall")
        query: str = Field(description="What you're looking for, in your own words.")
        limit: int = Field(default=8, description="Max events to return, best match first.")

    schema = Args
    timeout = None

    async def run(self, args: "RecallEventsTool.Args", ctx, signal) -> ToolResult:
        events = episode_store.recall_events(args.query, limit=args.limit)
        if not events:
            return ToolResult(content="No matching events in memory.")
        lines = []
        for e in events:
            when = (e.get("occurred") or "")[:10]
            lines.append(f"- [{when}] {e.get('title')}: {e.get('body')}")
        return ToolResult(content="\n".join(lines))


EPISODE_TOOLS = [RecallEventsTool()]
