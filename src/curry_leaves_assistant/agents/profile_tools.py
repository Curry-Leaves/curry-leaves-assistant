"""The `update_profile` tool — how an agent records durable facts about the USER.

This is SHARED semantic memory: every agent reads the profile (it's injected into every
agent's system prompt by agent_engine._with_user_profile), so a fact one agent learns is
known to all. That's the point — "the user prefers bullets" should reach whichever agent is
drafting next, not just the one that heard it.

Routing (which drawer a durable thing goes in):
  • about the USER (name, role, how they like work done)      → update_profile  (this tool)
  • browsable CONTENT (a meeting, a person, a project note)   → kb_write
  • how to DO a task (a procedure/workflow)                   → a skill (SKILL.md)

A high save bar and subject-based dedupe (in profile_store.upsert) keep the profile clean.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from curry_leaves.core.tools import ToolResult

from curry_leaves_assistant.stores import profile_store


def _err(msg: str) -> ToolResult:
    return ToolResult(content=msg, is_error=True)


class ProfileReadTool:
    """Read side of the shared USER PROFILE: list or search the durable facts/preferences every
    agent reads. Read-only, so it never prompts for permission. Recording/correcting a fact
    (set/forget) lives in the separate `update_profile` write tool."""
    name = "profile_read"
    description = (
        "Read the shared USER PROFILE — action: list | recall.\n"
        "• list: every durable fact/preference about the user.\n"
        "• recall: search the profile by MEANING, not just wording — ask what you'd ask a "
        "colleague ('how does she like reports laid out?') and it finds the fact even if it was "
        "worded differently. The prompt block you already have is capped, so use this when a "
        "task hinges on a preference that might not be in it.\n"
        "To record or correct a fact, use `update_profile`."
    )
    risk = "read"

    class Args(BaseModel):
        action: Literal["list", "recall"] = Field(default="list", description="list | recall")
        query: str | None = Field(default=None, description="recall: what you're looking for, in your own words.")
        limit: int = Field(default=6, description="recall: max facts to return.")

    schema = Args
    timeout = None

    async def run(self, args: "ProfileReadTool.Args", ctx, signal) -> ToolResult:
        if args.action == "recall":
            if not args.query:
                return _err("recall requires `query`.")
            facts = profile_store.recall(args.query, limit=args.limit)
            if not facts:
                return ToolResult(content="Nothing in the user profile matches that.")
            # Recalling a fact is a use of it — bump so the UI shows what's earning its place.
            profile_store.touch([f["id"] for f in facts])
            lines = [f"- ({f['type']}) [{f['id']}] {f['subject']}: {f['body']}" for f in facts]
            return ToolResult(content="User profile matching that:\n" + "\n".join(lines))
        facts = profile_store.list_all()
        if not facts:
            return ToolResult(content="The user profile is empty.")
        lines = [f"- ({f['type']}) [{f['id']}] {f['subject']}: {f['body']}" for f in facts]
        return ToolResult(content="User profile:\n" + "\n".join(lines))


class UpdateProfileTool:
    """Record, correct, or remove a durable fact/preference about the USER — the shared
    profile every assistant reads. Action-dispatched: set | forget. To review the profile,
    use the read-only `profile_read` tool."""
    name = "update_profile"
    description = (
        "Record or correct a DURABLE fact or preference — action: set | forget.\n"
        "• set: save a fact (type='fact', e.g. a name, role, timezone) or a preference "
        "(type='preference', e.g. 'terse email intros', 'bullets over paragraphs'). Give a "
        "short stable `subject` ('user name', 'release cadence') — reusing the same subject "
        "CORRECTS the existing fact instead of adding a duplicate. Set source='told' when the "
        "user stated it directly, 'inferred' when you deduced it.\n"
        "• WHERE IT LANDS — decide what the fact is ABOUT:\n"
        "  – About the USER themselves (their name, how they like ALL answers, a standing "
        "personal preference) → omit `about`. It joins the shared profile every assistant "
        "reads, so keep this small and universal.\n"
        "  – About a SUBJECT (an app, a project, a topic — 'CBM releases ship Thursdays', "
        "'stock reports as a grid') → pass `about` with that subject's folder, e.g. "
        "`about='apps/cbm'` or `about='topics/stock-reports'`. The fact is then filed WITH that "
        "subject's knowledge, where someone reading about it would look — not in the global "
        "profile. Prefer this whenever the fact only matters for one app/project/topic.\n"
        "• forget: remove a fact by its id (from `profile_read`).\n"
        "Do NOT use this for browsable content (a meeting, a person, a full project note → "
        "kb_write) or for how YOU do a task (that's a skill). A HIGH bar: only durable, "
        "repeated, or explicitly-stated things — never a one-off or a shaky guess."
    )
    risk = "write"

    class Args(BaseModel):
        action: Literal["set", "forget"] = Field(description="set | forget")
        # set
        text: str | None = Field(default=None, description="set: the fact/preference, as a short self-contained sentence about the user.")
        type: Literal["fact", "preference"] = Field(default="fact", description="set: 'fact' (something true) or 'preference' (how they like things done).")
        subject: str | None = Field(default=None, description="set: short stable handle for the fact (e.g. 'user name', 'release cadence') — reuse it to correct an existing fact.")
        source: Literal["told", "inferred"] = Field(default="inferred", description="set: 'told' if the user stated it directly, else 'inferred'.")
        about: str | None = Field(default=None, description="set: the SUBJECT this fact is about, as its folder (e.g. 'apps/cbm', 'topics/stock-reports'). Files the fact with that subject's knowledge. Omit only when the fact is about the USER themselves.")
        # forget
        id: str | None = Field(default=None, description="forget: the fact id (from `profile_read`).")

    schema = Args
    timeout = None

    async def run(self, args: "UpdateProfileTool.Args", ctx, signal) -> ToolResult:
        if args.action == "set":
            if not args.text:
                return _err("set requires `text`.")
            fact = profile_store.upsert(
                args.text, type=args.type, subject=args.subject, source=args.source,
                confidence=0.95 if args.source == "told" else 0.8, about=args.about)
            verb = "Saved" if fact.get("_created") else "Updated"
            where = f" under {fact['about']}" if fact.get("about") else " in the user profile"
            return ToolResult(
                content=f"{verb}{where} [{fact['type']}] {fact['subject']!r}: {fact['body']}")

        # forget
        if not args.id:
            return _err("forget requires `id`.")
        ok = profile_store.forget(args.id)
        return ToolResult(content=f"Forgot {args.id}." if ok else f"No such profile fact: {args.id}")


PROFILE_TOOLS = [ProfileReadTool(), UpdateProfileTool()]
