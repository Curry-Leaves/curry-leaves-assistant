"""Tools for the Skill Learner agent: `history` (browse recent agent runs and chat
conversations) and `skills` (read/write skill files). The agent keeps its OWN review index
inside its skill (via skills read/write actions) — these tools just supply the raw material
and the mechanical read/write; the agent decides what's new and what's worth remembering.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field
from curry_leaves.core.tools import ToolResult

from curry_leaves_assistant.stores import skills_store

from curry_leaves_assistant.stores import trace_store

from curry_leaves_assistant.core.paths import RUNS_DIR
from curry_leaves_assistant.core.store import read_json


def _err(msg: str) -> ToolResult:
    return ToolResult(content=msg, is_error=True)


class HistoryTool:
    """Read-only review surface over what the agents and chat have done — the raw material
    the Skill Learner reviews. Action-dispatched:
      • recent_runs  — recent agent run records (job/agent/status/trace).
      • trace        — the full span timeline for one trace id.
      • chat_sessions — recent chat conversations (metadata).
      • read_chat    — one conversation's full message history."""
    name = "history"
    description = (
        "Browse run + conversation HISTORY — action: recent_runs | trace | chat_sessions | "
        "read_chat | episodes.\n"
        "• recent_runs: recent agent runs (newest first) — jobId, agentId, status, "
        "triggerType, traceId, error. Find which runs happened since your last review.\n"
        "• trace: the full span timeline for a trace id — every llm_turn, tool_call (args, "
        "result, error), approval, ask, in order. See exactly what an agent did and how it "
        "recovered.\n"
        "• chat_sessions: recent conversations (session id, title, agent, model, message "
        "count, updated-at).\n"
        "• read_chat: one conversation's full message history by session id.\n"
        "• episodes: mechanical per-run summaries (taskShape, outcome, steps, tool histogram, "
        "errors, reviewed flag) — the compact 'what happened' index. Filter by agent_id and "
        "unreviewed_only to find learnable runs fast without reading every trace."
    )
    risk = "read"

    class Args(BaseModel):
        action: Literal["recent_runs", "trace", "chat_sessions", "read_chat", "episodes"] = Field(
            description="recent_runs | trace | chat_sessions | read_chat | episodes")
        unreviewed_only: bool = Field(default=False, description="episodes: only episodes not yet marked reviewed.")
        # recent_runs
        agent_id: str | None = Field(default=None, description="recent_runs: filter to one agent id.")
        status: str | None = Field(default=None, description="recent_runs: filter to 'done' or 'failed'.")
        limit: int = Field(default=50, description="recent_runs/chat_sessions: max to return (newest first).")
        # trace
        trace_id: str | None = Field(default=None, description="trace: the trace id (from recent_runs or a run record).")
        # read_chat
        session_id: str | None = Field(default=None, description="read_chat: the chat session id.")

    schema = Args
    timeout = None

    async def run(self, args: "HistoryTool.Args", ctx, signal) -> ToolResult:
        if args.action == "recent_runs":
            out = []
            if RUNS_DIR.exists():
                for agent_dir in RUNS_DIR.iterdir():
                    if not agent_dir.is_dir():
                        continue
                    if args.agent_id and agent_dir.name != args.agent_id:
                        continue
                    for f in agent_dir.glob("*.json"):
                        run = read_json(f, None)
                        if not run:
                            continue
                        if args.status and run.get("status") != args.status:
                            continue
                        out.append({
                            "jobId": run.get("id"), "agentId": run.get("agentId"),
                            "status": run.get("status"), "traceId": run.get("traceId"),
                            "triggerType": (run.get("trigger") or {}).get("type"),
                            "error": run.get("error"), "finishedAt": run.get("finishedAt"),
                        })
            out.sort(key=lambda r: r.get("finishedAt") or "", reverse=True)
            out = out[: max(1, args.limit)]
            return ToolResult(content=json.dumps(out, indent=2) if out else "No matching runs.")

        if args.action == "trace":
            if not args.trace_id:
                return _err("trace requires `trace_id`.")
            spans = trace_store.get_trace(args.trace_id)
            if not spans:
                return _err(f"No trace {args.trace_id}")
            view = [{
                "kind": s.get("kind"), "name": s.get("name"), "status": s.get("status"),
                "attributes": s.get("attributes"),
            } for s in spans]
            return ToolResult(content=json.dumps(view, indent=2))

        if args.action == "chat_sessions":
            from curry_leaves_assistant.stores import chat_sessions
            sessions = chat_sessions.list_sessions()[: max(1, args.limit)]
            return ToolResult(content=json.dumps(sessions, indent=2) if sessions else "No chat sessions.")

        if args.action == "episodes":
            from curry_leaves_assistant.stores import episode_store
            eps = episode_store.recent(args.agent_id, limit=max(1, args.limit),
                                       unreviewed_only=args.unreviewed_only)
            view = [{
                "agentId": e.get("agentId"), "jobId": e.get("jobId"), "traceId": e.get("traceId"),
                "taskShape": e.get("taskShape"), "outcome": e.get("outcome"),
                "steps": e.get("steps"), "toolCalls": e.get("toolCalls"),
                "toolErrors": e.get("toolErrors"), "maxToolRepeat": e.get("maxToolRepeat"),
                "error": e.get("error"), "reviewed": e.get("reviewed"),
                "finishedAt": e.get("finishedAt"),
            } for e in eps]
            return ToolResult(content=json.dumps(view, indent=2) if view else "No episodes.")

        # read_chat
        if not args.session_id:
            return _err("read_chat requires `session_id`.")
        from curry_leaves_assistant.stores import chat_sessions
        msgs = chat_sessions.get_messages(args.session_id)
        if not msgs:
            return _err(f"No messages for session {args.session_id}")
        return ToolResult(content=json.dumps(msgs, indent=2))


class SkillsReadTool:
    """Read side of the agents' SKILL library, action-dispatched:
      • list — every skill's name + description (dedupe check before creating).
      • read — a file inside a skill's dir (SKILL.md or a bundled reference).
    Read-only, so it never prompts for permission. Writing/creating skill files lives in the
    separate `skills` write tool."""
    name = "skills_read"
    description = (
        "Read the agent SKILL library — action: list | read.\n"
        "• list: every skill's name + description — check whether one already covers a topic "
        "before creating a duplicate.\n"
        "• read: a file inside a skill's directory (its SKILL.md before editing, or a bundled "
        "reference/template), skill name and path as SEPARATE args. (Equivalent to kb_read "
        "read with a 'skill://<skill>/<path>' path — different calling convention.)\n"
        "To create or overwrite a skill file, use the `skills` tool instead."
    )
    risk = "read"

    class Args(BaseModel):
        action: Literal["list", "read"] = Field(description="list | read")
        # read: address a file inside a skill
        skill: str | None = Field(default=None, description="read: the skill's directory name.")
        path: str = Field(default="SKILL.md", description="read: path relative to the skill dir (e.g. 'SKILL.md', 'references/notes.md').")

    schema = Args
    timeout = None

    async def run(self, args: "SkillsReadTool.Args", ctx, signal) -> ToolResult:
        if args.action == "list":
            skills = skills_store.list_skills()
            return ToolResult(content=json.dumps(skills, indent=2) if skills else "No skills yet.")
        # read
        if not args.skill:
            return _err("read requires `skill`.")
        try:
            content = skills_store.read_file(args.skill, args.path)
        except (FileNotFoundError, ValueError) as exc:
            return _err(f"Not found: {exc}")
        return ToolResult(content=content)


class SkillsTool:
    """Write side of the agents' SKILL library, action-dispatched:
      • write  — create/overwrite a file in an EXISTING skill's dir.
      • create — a brand-new skill (fails if the name exists → use write).
    To list skills or read a skill file, use the read-only `skills_read` tool."""
    name = "skills"
    description = (
        "Write to the agent SKILL library — action: write | create.\n"
        "• write: create or fully overwrite a file inside an EXISTING skill's directory "
        "(SKILL.md, or references/scripts/assets alongside it). Use create first if the skill "
        "doesn't exist yet.\n"
        "• create: a brand-new skill (a directory with a SKILL.md: name + description "
        "frontmatter, then the body). Fails if the name already exists — use write to update.\n"
        "To list skills or read a skill file, use `skills_read` instead."
    )
    risk = "write"

    class Args(BaseModel):
        action: Literal["write", "create"] = Field(description="write | create")
        # write: address a file inside a skill
        skill: str | None = Field(default=None, description="write: the skill's directory name.")
        path: str = Field(default="SKILL.md", description="write: path relative to the skill dir (e.g. 'SKILL.md', 'references/notes.md').")
        # write
        content: str | None = Field(default=None, description="write: full file content.")
        # create
        name: str | None = Field(default=None, description="create: skill directory name, short and kebab-case.")
        description: str | None = Field(default=None, description="create: one sentence — what the skill is for and when to use it.")
        body: str | None = Field(default=None, description="create: the procedure as markdown (after the frontmatter).")

    schema = Args
    timeout = None

    async def run(self, args: "SkillsTool.Args", ctx, signal) -> ToolResult:
        if args.action == "write":
            if not args.skill or args.content is None:
                return _err("write requires `skill` and `content`.")
            try:
                skills_store.write_file(args.skill, args.path, args.content)
            except ValueError as exc:
                return _err(f"Rejected: {exc}")
            return ToolResult(content=f"Wrote {args.skill}/{args.path}")
        # create
        if not args.name or not args.description or args.body is None:
            return _err("create requires `name`, `description`, and `body`.")
        try:
            skills_store.create_skill(args.name, args.description, args.body)
        except FileExistsError:
            return _err(f"Skill '{args.name}' already exists — use action='write' to update it.")
        return ToolResult(content=f"Created skill {args.name}")


SKILL_TOOLS = [HistoryTool(), SkillsReadTool(), SkillsTool()]
