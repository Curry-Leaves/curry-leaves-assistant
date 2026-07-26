"""The `learn_chats` tool — the Memory Keeper's window onto conversations to learn from.

The nightly Memory Keeper distils durable memory out of your chats. This tool gives it exactly
what it needs and nothing more:
  • list  — the conversations it hasn't learned from yet (new since last time).
  • read  — one conversation as plain user/assistant text (tool calls, approvals and thinking
            stripped — that's HOW the assistant worked, not what was SAID).
  • done  — mark a conversation processed, so tomorrow's sweep skips it.

It writes what it learns through the ordinary memory tools (`update_profile` for facts about the
user, `remember_event` for things that happened), so everything it produces is curated, deduped
memory — never raw transcript.
"""
from __future__ import annotations

from typing import Literal

from curry_leaves.core.tools import ToolResult
from pydantic import BaseModel, Field

from curry_leaves_assistant.stores import chat_sessions


def _err(msg: str) -> ToolResult:
    return ToolResult(content=msg, is_error=True)


class LearnChatsTool:
    """Browse and read the chats waiting to be learned from, and mark each done.
    Action-dispatched: list | read | done."""
    name = "learn_chats"
    description = (
        "Find and read the CONVERSATIONS you haven't learned from yet — action: list | read | done.\n"
        "• list: conversations with new content since you last processed them (id · title · how "
        "many new messages). Newest first. Start here.\n"
        "• read: one conversation as plain text — the user and assistant turns, each labelled. "
        "Tool calls, tool results and approvals are stripped entirely (they're HOW the assistant "
        "worked, never a fact). Learn only from the `USER:` turns; the `ASSISTANT:` turns are "
        "context to understand them, not evidence.\n"
        "• done: mark a conversation processed once you've extracted everything durable from it, "
        "so it isn't shown again unless it grows.\n"
        "Learn from what you read by calling `update_profile` (a durable fact/preference about "
        "the USER) and `remember_event` (a notable thing that happened). A HIGH bar: skip small "
        "talk and one-offs; record only what's worth referring back to weeks later."
    )
    risk = "read"  # reading is safe; the writes happen through the gated memory tools

    class Args(BaseModel):
        action: Literal["list", "read", "done"] = Field(description="list | read | done")
        session_id: str | None = Field(default=None, description="read/done: the conversation id (from list).")
        limit: int = Field(default=25, description="list: max conversations to return.")

    schema = Args
    timeout = None

    async def run(self, args: "LearnChatsTool.Args", ctx, signal) -> ToolResult:
        if args.action == "list":
            sessions = chat_sessions.unlearned_sessions(limit=max(1, args.limit))
            if not sessions:
                return ToolResult(content="No conversations waiting to be learned from — you're caught up.")
            lines = [f"{len(sessions)} conversation(s) to learn from:"]
            lines += [f"- [{s['id']}] {s['title']} — {s['newMessages']} new message(s)"
                      for s in sessions]
            return ToolResult(content="\n".join(lines))

        if not args.session_id:
            return _err(f"{args.action} requires `session_id`.")

        if args.action == "read":
            text = chat_sessions.clean_transcript(args.session_id)
            if not text:
                return ToolResult(content="(this conversation has no user/assistant text to learn from)")
            return ToolResult(content=text)

        # done
        chat_sessions.mark_learned(args.session_id)
        return ToolResult(content=f"Marked {args.session_id} as learned.")


LEARN_CHATS_TOOLS = [LearnChatsTool()]
