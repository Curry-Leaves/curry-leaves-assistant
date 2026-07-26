"""The `remember` tool — an agent's PRIVATE memory of how IT does its job.

The companion to `update_profile` (the shared user profile). Routing the model follows:
  • about the USER (name, prefs any assistant should know)   → update_profile  (shared)
  • about how YOU do YOUR job (a filing convention you use,   → remember        (private)
    a quirk you learned about a recurring task)
  • browsable CONTENT (a meeting, a person, a note)          → kb_write

The owning agent is discovered from the run context (trace_ctx.current_agent_id) — never
passed in — so a note always lands in the memory of whichever agent called the tool, and is
read back only for that agent.

Two ways in: agent_engine._with_user_profile injects the agent's notes into its prompt at build
time (capped, unfiltered — the agent can't know the run's input yet), and `recall` searches them
by MEANING mid-run, which is how an agent reaches a note the prompt block left out.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from curry_leaves.core.tools import ToolResult

from curry_leaves_assistant.core import trace_ctx
from curry_leaves_assistant.stores import agent_memory_store, episode_store


def _err(msg: str) -> ToolResult:
    return ToolResult(content=msg, is_error=True)


class RememberTool:
    """Record, correct, review, recall, or drop a PRIVATE note about how THIS agent does its job.
    Action-dispatched: set | list | recall | forget. Only this agent ever reads these notes."""
    name = "remember"
    description = (
        "Save a PRIVATE note about how YOU do YOUR job — action: set | list | recall | forget. "
        "Only YOU read these; they never reach other agents.\n"
        "• set: record a convention or lesson SPECIFIC to your work — e.g. a filing rule you "
        "follow, a formatting quirk the user wants from YOUR output, how a recurring task "
        "should go. Give a short stable `subject` — reusing it CORRECTS the note instead of "
        "adding a duplicate.\n"
        "• list: your current notes (check before adding so you correct rather than duplicate).\n"
        "• recall: search your notes by MEANING, not just wording — ask it what you'd ask a "
        "colleague ('how do I file these?') and it finds the note even if you worded it "
        "differently. Use when the task at hand might have a convention you've saved.\n"
        "• forget: drop a note by id.\n"
        "Use `update_profile` INSTEAD for anything true about the USER that EVERY assistant "
        "should know (their name, general preferences) — that's shared. Use `kb_write` for "
        "browsable content. A HIGH bar here too: only durable, repeated job-specific things."
    )
    risk = "write"

    class Args(BaseModel):
        action: Literal["set", "list", "recall", "forget"] = Field(description="set | list | recall | forget")
        text: str | None = Field(default=None, description="set: the note, a short self-contained sentence about how you work.")
        type: Literal["convention", "fact", "preference"] = Field(default="convention", description="set: 'convention' (a rule you follow), 'fact', or 'preference'.")
        subject: str | None = Field(default=None, description="set: short stable handle — reuse to correct an existing note.")
        source: Literal["told", "inferred"] = Field(default="inferred", description="set: 'told' if the user stated it, else 'inferred'.")
        query: str | None = Field(default=None, description="recall: what you're looking for, in your own words.")
        limit: int = Field(default=5, description="recall: max notes to return.")
        id: str | None = Field(default=None, description="forget: the note id (from list).")

    schema = Args
    timeout = None

    async def run(self, args: "RememberTool.Args", ctx, signal) -> ToolResult:
        agent_id = trace_ctx.current_agent_id()
        if not agent_id:
            return _err("remember can only be used inside an agent run (no current agent).")

        if args.action == "set":
            if not args.text:
                return _err("set requires `text`.")
            note = agent_memory_store.upsert(
                agent_id, args.text, type=args.type, subject=args.subject, source=args.source,
                confidence=0.95 if args.source == "told" else 0.8)
            verb = "Saved" if note.get("_created") else "Updated"
            return ToolResult(content=f"{verb} your note [{note['type']}] {note['subject']!r}: {note['body']}")

        if args.action == "list":
            notes = agent_memory_store.list_all(agent_id)
            if not notes:
                return ToolResult(content="You have no private notes yet.")
            lines = [f"- ({n['type']}) [{n['id']}] {n['subject']}: {n['body']}" for n in notes]
            return ToolResult(content="Your private notes:\n" + "\n".join(lines))

        if args.action == "recall":
            if not args.query:
                return _err("recall requires `query`.")
            notes = agent_memory_store.recall(agent_id, args.query, limit=args.limit)
            if not notes:
                return ToolResult(content="No notes of yours match that.")
            # Recalling a note is a use of it — bump so the UI can show what's actually earning
            # its place. Best-effort; never fails a read.
            agent_memory_store.touch(agent_id, [n["id"] for n in notes])
            lines = [f"- ({n['type']}) [{n['id']}] {n['subject']}: {n['body']}" for n in notes]
            return ToolResult(content="Your notes matching that:\n" + "\n".join(lines))

        # forget
        if not args.id:
            return _err("forget requires `id`.")
        ok = agent_memory_store.forget(agent_id, args.id)
        return ToolResult(content=f"Forgot {args.id}." if ok else f"No such note: {args.id}")


class RememberEventTool:
    """Record a notable EVENT — a dated thing that happened, worth referring back to later.

    Distinct from `remember` (a timeless convention about how you work) and `update_profile` (a
    fact about the user): an event is episodic — "on the 12th, we decided X in the budget chat".
    Used mainly by the nightly Memory Keeper distilling conversations, but any agent may record
    one. Events are searchable by meaning and cluster into consolidated lessons over time."""
    name = "remember_event"
    description = (
        "Record a notable EVENT that happened — a dated, episodic memory worth referring back to "
        "(a decision made, a milestone, something that went notably well or badly). NOT a timeless "
        "fact about the user (use `update_profile`) or a convention about how you work (use "
        "`remember`). Give a short `title` and a `body` that states what happened and why it "
        "matters, and set `about` to what the event concerns so it files beside that thing's "
        "other notes. HIGH bar: skip routine activity — only what a person would want surfaced "
        "weeks later."
    )
    risk = "write"

    class Args(BaseModel):
        title: str = Field(description="A short headline for the event.")
        body: str = Field(description="What happened and why it's worth remembering.")
        occurred: str | None = Field(default=None, description="ISO date/time it happened (defaults to now).")
        tags: list[str] | None = Field(default=None, description="Optional tags to group related events.")
        about: str | None = Field(
            default=None,
            description=("What the event is ABOUT, as a memory folder — 'apps/cbm', "
                         "'topics/hiring', 'people/priya'. The event is filed there and linked to "
                         "it. Omit only for events about how you yourself work."))

    schema = Args
    timeout = None

    async def run(self, args: "RememberEventTool.Args", ctx, signal) -> ToolResult:
        agent_id = trace_ctx.current_agent_id() or "assistant"
        rec = episode_store.remember_event(agent_id, title=args.title, body=args.body,
                                           occurred=args.occurred, tags=args.tags,
                                           about=args.about)
        return ToolResult(content=f"Remembered event: {rec['title']} ({rec['path']})")


MEMORY_TOOLS = [RememberTool(), RememberEventTool()]
