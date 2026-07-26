"""Curry Leaves's agent tools — thin curry-leaves tools over the data layer.

Each follows curry-leaves's Tool protocol: a ``name``, ``description``, a pydantic
``Args`` model (→ JSON schema for free), and an async ``run``. They call the same
data functions the UI does, so agent-created items emit events like any other.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field
from curry_leaves.core.tools import ToolResult


def _err(msg: str) -> ToolResult:
    return ToolResult(content=msg, is_error=True)

from curry_leaves_assistant.stores import data

from curry_leaves_assistant.core import events

from curry_leaves_assistant.domain import recordings



def _rec_source(rec_id: str | None) -> dict | None:
    """A self-describing backlink to the parent recording (id + current name)."""
    if not rec_id:
        return None
    rec = recordings.get(rec_id)
    return {"type": "recording", "id": rec_id, "label": (rec or {}).get("name")}


class TodosReadTool:
    """Read side of todos, action-dispatched: list them. Read-only, so it never prompts for
    permission. Creating, updating, and completing/deleting todos live in the separate write
    `todos` tool."""
    name = "todos_read"
    description = (
        "Read the user's TODOS (plain tasks to track and check off) — action: list. "
        "`list` returns the user's todos (active only by default; pass include_done to also "
        "show completed ones). To create, update, or delete a todo use the `todos` tool. This "
        "does NOT cover reminders — list them separately with `reminders`."
    )
    risk = "read"

    class Args(BaseModel):
        action: Literal["list"] = Field(default="list", description="list")
        include_done: bool = Field(default=False, description="list: include completed todos.")

    schema = Args
    timeout = None

    async def run(self, args: "TodosReadTool.Args", ctx, signal) -> ToolResult:
        todos = [t for t in data.list_todos() if args.include_done or not t["done"]]
        return ToolResult(content=json.dumps(todos, indent=2) if todos else "No todos.")


class TodosTool:
    """Write side of the todo lifecycle, action-dispatched. Todos are plain tasks to
    track/check off; a due_date here is informational only (no notification) — for a
    timed ping use the `reminders` tool instead. To list todos use the read-only
    `todos_read` tool."""
    name = "todos"
    description = (
        "Create, update, or delete the user's TODOS (plain tasks to track and check off) — "
        "action: create | update | delete. A todo's due_date is informational only and "
        "fires no notification; when the user wants to be actively pinged at a time, use "
        "the `reminders` tool instead. To list todos, use `todos_read`. Does NOT cover "
        "reminders — manage them separately."
    )
    risk = "write"

    class Args(BaseModel):
        action: Literal["create", "update", "delete"] = Field(
            description="create | update | delete")
        # create / update
        text: str | None = Field(default=None, description="create: the task (required). update: new task text.")
        priority: str | None = Field(default=None, description="create/update: high | medium | low")
        due_date: str | None = Field(default=None, description="create/update: ISO 8601 due date (informational only).")
        done: bool | None = Field(default=None, description="update: mark complete (true) or active (false).")
        source_recording_id: str | None = Field(
            default=None, description="create: recording id this came from, if any.")
        # update / delete
        todo_id: str | None = Field(default=None, description="update/delete: the id of the todo (list first to get it).")

    schema = Args
    timeout = None

    async def run(self, args: "TodosTool.Args", ctx, signal) -> ToolResult:
        if args.action == "create":
            if not args.text:
                return _err("create requires `text`.")
            todo = data.create_todo(args.text, priority=args.priority, due_date=args.due_date,
                                    source=_rec_source(args.source_recording_id))
            return ToolResult(content=f"Created todo {todo['id']}: {todo['text']}")
        if args.action == "update":
            if not args.todo_id:
                return _err("update requires `todo_id`.")
            patch = {}
            if args.text is not None: patch["text"] = args.text
            if args.priority is not None: patch["priority"] = args.priority
            if args.due_date is not None: patch["dueDate"] = args.due_date
            if args.done is not None: patch["done"] = args.done
            if not patch:
                return _err("Nothing to update — pass at least one of text/priority/due_date/done.")
            todo = data.update_todo(args.todo_id, patch)
            if todo is None:
                return _err(f"No todo {args.todo_id}")
            return ToolResult(content=f"Updated todo {todo['id']}: {todo['text']} (done={todo['done']})")
        # delete
        if not args.todo_id:
            return _err("delete requires `todo_id`.")
        ok = data.delete_todo(args.todo_id)
        return ToolResult(content=f"Deleted todo {args.todo_id}" if ok else f"No todo {args.todo_id}", is_error=not ok)


class RemindersReadTool:
    """Read side of reminders, action-dispatched: list them. Read-only, so it never prompts
    for permission. Creating/updating/deleting reminders lives in the separate `reminders`
    write tool."""
    name = "reminders_read"
    description = (
        "Read the user's REMINDERS (time-bound notifications that ping the user) — "
        "action: list. Returns reminders (active by default; pass include_done for all). "
        "To create/update/delete a reminder use the `reminders` tool. Does NOT cover todos "
        "— list them separately with `todos`."
    )
    risk = "read"

    class Args(BaseModel):
        action: Literal["list"] = Field(default="list", description="list")
        include_done: bool = Field(default=False, description="list: include completed reminders.")

    schema = Args
    timeout = None

    async def run(self, args: "RemindersReadTool.Args", ctx, signal) -> ToolResult:
        rems = [r for r in data.list_reminders() if args.include_done or not r.get("done")]
        return ToolResult(content=json.dumps(rems, indent=2) if rems else "No reminders.")


class RemindersTool:
    """Write side of the reminder lifecycle, action-dispatched. Reminders actively notify
    the user at a time; for a plain task with no ping use the `todos` tool. To find/list
    reminders use the read-only `reminders_read` tool."""
    name = "reminders"
    description = (
        "Manage the user's REMINDERS (time-bound notifications that ping the user) — "
        "action: create | update | delete. Use for 'remind me...' / 'don't let me "
        "forget...'. To list reminders, use `reminders_read` instead. For a plain task to "
        "track without a notification, use `todos`. Does NOT cover todos."
    )
    risk = "write"

    class Args(BaseModel):
        action: Literal["create", "update", "delete"] = Field(
            description="create | update | delete")
        # create / update
        title: str | None = Field(default=None, description="create: what to be reminded about (required). update: new title.")
        due_at: str | None = Field(default=None, description="create: ISO 8601 due datetime (required). update: new due datetime.")
        notes: str | None = Field(default=None, description="create/update: optional extra detail.")
        done: bool | None = Field(default=None, description="update: mark complete (true) or active (false).")
        source_recording_id: str | None = Field(default=None, description="create: recording id this came from, if any.")
        # update / delete
        reminder_id: str | None = Field(default=None, description="update/delete: the id of the reminder (list first to get it).")

    schema = Args
    timeout = None

    async def run(self, args: "RemindersTool.Args", ctx, signal) -> ToolResult:
        if args.action == "create":
            if not args.title or not args.due_at:
                return _err("create requires `title` and `due_at`.")
            r = data.create_reminder(args.title, due_at=args.due_at, notes=args.notes,
                                     source=_rec_source(args.source_recording_id))
            return ToolResult(content=f"Created reminder {r['id']} due {r['dueAt']}: {r['title']}")
        if args.action == "update":
            if not args.reminder_id:
                return _err("update requires `reminder_id`.")
            patch = {}
            if args.title is not None: patch["title"] = args.title
            if args.notes is not None: patch["notes"] = args.notes
            if args.due_at is not None: patch["dueAt"] = args.due_at
            if args.done is not None: patch["done"] = args.done
            if not patch:
                return _err("Nothing to update — pass at least one of title/notes/due_at/done.")
            r = data.update_reminder(args.reminder_id, patch)
            if r is None:
                return _err(f"No reminder {args.reminder_id}")
            return ToolResult(content=f"Updated reminder {r['id']}: {r['title']} due {r['dueAt']} (done={r['done']})")
        # delete
        if not args.reminder_id:
            return _err("delete requires `reminder_id`.")
        ok = data.delete_reminder(args.reminder_id)
        return ToolResult(content=f"Deleted reminder {args.reminder_id}" if ok else f"No reminder {args.reminder_id}", is_error=not ok)


_PLACEHOLDER_TAGS = ("Meeting", "Brain dump", "Idea", "Task", "Research")


def _is_placeholder_name(name: str) -> bool:
    """A recording still carries its auto-generated name (safe to overwrite with a title)."""
    name = (name or "").strip()
    if not name or name == "Untitled recording":
        return True
    return any(name.startswith(f"{tag} · ") for tag in _PLACEHOLDER_TAGS)


class RecordingsReadTool:
    """Read side of recordings, action-dispatched: find them or read one. Read-only, so it
    never prompts for permission. Titling (set_title) and writing an agent's per-recording
    output live in the separate `recordings` / `recording_output` write tools."""
    name = "recordings_read"
    description = (
        "Read the user's RECORDINGS/meetings — action: list | read. "
        "`list` returns recordings (newest first) with id/name/status/date; `read` returns "
        "one recording's full context (transcript, notes, links, documents, agent outputs) "
        "by its recording id. To (re)title a recording use the `recordings` tool. This is NOT "
        "for artifacts (use `artifacts_read`) or dashboard boards (use `dashboard_read`)."
    )
    risk = "read"

    class Args(BaseModel):
        action: Literal["list", "read"] = Field(description="list | read")
        # list
        limit: int = Field(default=20, description="list: max recordings to return (newest first).")
        query: str | None = Field(default=None, description="list: optional case-insensitive filter on name/transcript/tags.")
        # read
        recording_id: str | None = Field(default=None, description="read: the recording id.")

    schema = Args
    timeout = None

    async def run(self, args: "RecordingsReadTool.Args", ctx, signal) -> ToolResult:
        if args.action == "list":
            recs = recordings.list_recordings()
            if args.query:
                q = args.query.lower()
                recs = [r for r in recs
                        if q in f"{r.get('name','')} {r.get('transcript') or ''} {' '.join(r.get('tags') or [])}".lower()]
            view = [{
                "id": r["id"], "name": r["name"],
                "status": "processed" if r.get("outputs") else "transcribed" if r.get("transcript") else r.get("status"),
                "duration": r.get("duration"), "createdAt": r.get("createdAt"),
                "outputs": r.get("outputs") or [],
            } for r in recs[: max(1, args.limit)]]
            return ToolResult(content=json.dumps(view, indent=2) if view else "No recordings.")
        # read
        if not args.recording_id:
            return _err("read requires `recording_id`.")
        if recordings.get(args.recording_id) is None:
            return _err(f"No recording {args.recording_id}")
        return ToolResult(content=recordings.agent_context(args.recording_id, include_outputs=True))


class RecordingsTool:
    """Write side of recordings: (re)title a recording whose name is still the auto-generated
    placeholder. To find or read recordings use the read-only `recordings_read` tool; to write
    an agent's per-recording output/summary use `recording_output` (different risk + shape)."""
    name = "recordings"
    description = (
        "Title the user's RECORDINGS/meetings — action: set_title. `set_title` proposes a "
        "short title, applied ONLY if the recording still has its auto-generated placeholder "
        "name. To find or read recordings, use `recordings_read` instead."
    )
    risk = "write"

    class Args(BaseModel):
        action: Literal["set_title"] = Field(default="set_title", description="set_title")
        recording_id: str | None = Field(default=None, description="set_title: the recording id.")
        title: str | None = Field(default=None, description="set_title: short title (3-8 words, no trailing punctuation).")

    schema = Args
    timeout = None

    async def run(self, args: "RecordingsTool.Args", ctx, signal) -> ToolResult:
        if not args.recording_id or not args.title:
            return _err("set_title requires `recording_id` and `title`.")
        rec = recordings.get(args.recording_id)
        if rec is None:
            return _err(f"No recording {args.recording_id}")
        if not _is_placeholder_name(rec.get("name", "")):
            return ToolResult(content=f"Skipped — recording {args.recording_id} already has a custom title ('{rec.get('name')}').")
        title = args.title.strip()
        if not title:
            return _err("title must not be empty")
        meta = recordings.update(args.recording_id, {"name": title})
        if meta is None:
            return _err(f"No recording {args.recording_id}")
        events.emit("recording.updated", payload=meta, entity_id=args.recording_id, label=title)
        return ToolResult(content=f"Titled recording {args.recording_id}: '{title}'")


class FileReadTool:
    """Read a file the user pointed at with `@file` in chat. Read-only, so it never prompts.

    Deliberately narrow: it is the only tool that reaches outside `~/.curry-leaves/`, and
    every path goes through files_store's sandbox (fixed roots, resolve-then-contain, text
    only, size-capped). It cannot list or discover — the user picks the file in the composer
    and the agent opens exactly that one."""
    name = "file_read"
    description = (
        "Read a local FILE the user referenced with @file in their message. Give the exact "
        "`path` from the reference. Only files under the user's Desktop, Documents, Downloads "
        "or Curry Leaves folder can be read, and only text files. This cannot browse or "
        "search the filesystem — use it solely to open a path the user already pointed at."
    )
    risk = "read"

    class Args(BaseModel):
        path: str = Field(description="Absolute path, exactly as given in the @file reference.")

    schema = Args
    timeout = None

    async def run(self, args: "FileReadTool.Args", ctx, signal) -> ToolResult:
        from curry_leaves_assistant.stores import files_store
        try:
            name, text = files_store.read_text(args.path)
        except PermissionError as exc:
            return _err(f"Not allowed: {exc}")
        except FileNotFoundError:
            return _err(f"No such file: {args.path}")
        except (ValueError, OSError) as exc:
            return _err(f"Can't read that file: {exc}")
        return ToolResult(content=f"# {name}\n\n{text}")


class NotifyUserTool:
    """Not in ALL_TOOLS / not agent-selectable — a fresh instance is built per dashboard
    tile run (see dashboard_runner.py) closing over that tile's identity, then injected
    into the run's tool list only when the tile has an alert condition configured. This
    keeps 'notify the user' scoped to tiles that actually asked for it, instead of being
    a standing capability every agent could reach for."""
    name = "notify_user"
    description = (
        "Send the user a desktop notification about this tile. Call this ONLY if the "
        "tile's alert condition is actually met by what you found this run — do not call "
        "it speculatively or on every run."
    )
    risk = "write"

    class Args(BaseModel):
        message: str = Field(description="Short, specific notification body (1-2 sentences): what triggered the alert.")

    schema = Args
    timeout = None

    def __init__(self, board_id: str, tile_id: str, tile_title: str):
        self._board_id = board_id
        self._tile_id = tile_id
        self._tile_title = tile_title

    async def run(self, args: "NotifyUserTool.Args", ctx, signal) -> ToolResult:
        message = args.message.strip()
        if not message:
            return ToolResult(content="message must not be empty", is_error=True)
        events.emit("tile.alert.raised",
                    payload={"boardId": self._board_id, "tileId": self._tile_id, "message": message},
                    entity_id=self._tile_id, label=self._tile_title)
        return ToolResult(content=f"Notified the user: {message}")


class RecordingOutputTool:
    """Write side of a recording, action-dispatched:
      • summary — THE one summary field shown at the top of the recording's page.
      • output  — THIS agent's own named output slot on the recording (minutes/notes),
                  shown under the agent's name; a second call replaces this agent's prior
                  output and never touches another agent's.
    Neither is a standalone shareable deliverable — for those use the `artifacts` tool."""
    name = "recording_output"
    description = (
        "Attach agent-produced content to a RECORDING — action: summary | output. "
        "`summary` sets THE recording's single summary field (one per recording, shown at "
        "the top of its page). `output` saves THIS agent's own named output (minutes, notes) "
        "under the agent's name; each agent owns one output slot per recording and a repeat "
        "call replaces it. Neither is a standalone shareable deliverable — use `artifacts` "
        "for those."
    )
    risk = "write"

    class Args(BaseModel):
        action: Literal["summary", "output"] = Field(description="summary | output")
        recording_id: str = Field(description="The recording id to attach to.")
        content: str = Field(description="The text to save (markdown ok). For `summary`, the summary text.")
        title: str | None = Field(
            default=None, description="output: display title (defaults to the agent's name). Ignored for summary.")
        section: str | None = Field(
            default=None,
            description="output: template section id (e.g. 'decisions', 'follow-up-email') so each "
                        "section gets its own tab; omit for a single unnamed output. Ignored for summary.")

    schema = Args
    timeout = None

    async def run(self, args: "RecordingOutputTool.Args", ctx, signal) -> ToolResult:
        if args.action == "summary":
            rec = recordings.set_summary(args.recording_id, args.content)
            if rec is None:
                return _err(f"No recording {args.recording_id}")
            return ToolResult(content=f"Saved summary for recording {args.recording_id}")
        # output — scoped to the current agent
        from curry_leaves_assistant.core import trace_ctx

        agent_id = trace_ctx.current_agent_id()
        if not agent_id:
            return _err("output must be called from within an agent run")
        from curry_leaves_assistant.stores import agent_store

        rec = agent_store.read_agent(agent_id)
        title = args.title or (rec or {}).get("name") or agent_id
        out = recordings.save_output(args.recording_id, agent_id, title, args.content, section=args.section)
        if out is None:
            return _err(f"No recording {args.recording_id}")
        where = f"section '{args.section}' of " if args.section else ""
        return ToolResult(content=f"Saved '{title}' as {where}output for recording {args.recording_id}")


# curry-leaves's built-in `ask` tool lets an agent question the user mid-run (HITL).
from curry_leaves.tools.ask import AskTool

# Web tools — reach the network (risk `network`, permission-gated):
#   web_fetch  — Curry Leaves's: readability extraction (trafilatura) → clean markdown, with
#                optional Playwright rendering for JS pages. See web_tools.py.
#   web_search — DuckDuckGo results: title + url + snippet (httpx; no API key)
#   browser    — headless Playwright browser (renders JS, click/fill/screenshot); needs
#                `playwright install`, else it returns a clear install message.
from curry_leaves_assistant.agents.web_tools import WebFetchTool
from curry_leaves.tools.web import WebSearchTool
from curry_leaves_assistant.agents.browser_tool import BrowserTool

# Knowledge-base tools (remember / search_knowledge / read_knowledge).
from curry_leaves_assistant.agents.knowledge_tools import KNOWLEDGE_TOOLS

# Procedural-memory tools (read run/trace history, read/write skill files).
from curry_leaves_assistant.agents.skill_tools import SKILL_TOOLS

# Artifact-store tools (save/list generated deliverables — presentations, reports, pages).
from curry_leaves_assistant.agents.artifact_tools import ARTIFACT_TOOLS

# Dashboard tools (list boards, pin a recurring ask onto a board as a tile).
from curry_leaves_assistant.agents.dashboard_tools import DASHBOARD_TOOLS

# Orchestration tools (hand work to the background pool; spawn + await agent runs = workflows).
from curry_leaves_assistant.agents.orchestration_tools import ORCHESTRATION_TOOLS

# Assignment tool (the Lead routes a posted pool task to the best-fit agent as a durable job).
from curry_leaves_assistant.agents.assign_tools import ASSIGN_TOOLS

# Triage tool (the Todo Triage agent posts an actionable new todo to the pool for the team).
from curry_leaves_assistant.agents.triage_tools import TRIAGE_TOOLS

# Semantic-memory tool (record durable facts/preferences about the user into the shared profile).
from curry_leaves_assistant.agents.profile_tools import PROFILE_TOOLS

# Learning-loop tool (Skill Learner: author governed learned skills, mark episodes reviewed).
from curry_leaves_assistant.agents.learn_tools import LEARN_TOOLS

# Per-agent private memory tool (how THIS agent does its job — read only by that agent).
from curry_leaves_assistant.agents.memory_tools import MEMORY_TOOLS

# Episodic-recall tool (an agent queries its OWN dated run history — "have I done this before?").
from curry_leaves_assistant.agents.episode_tools import EPISODE_TOOLS

# Chat-learner tool (the nightly Memory Keeper reads unlearned conversations to distil memory).
from curry_leaves_assistant.agents.learn_chats_tools import LEARN_CHATS_TOOLS

# Registry keyed by the tool name agents reference in their frontmatter `tools:` list.
ALL_TOOLS = {
    t.name: t for t in (
        TodosReadTool(), TodosTool(), RemindersReadTool(), RemindersTool(),
        RecordingsReadTool(), RecordingsTool(), RecordingOutputTool(),
        FileReadTool(),
        WebFetchTool(), WebSearchTool(), BrowserTool(),
        AskTool(),
        *KNOWLEDGE_TOOLS,
        *SKILL_TOOLS,
        *ARTIFACT_TOOLS,
        *DASHBOARD_TOOLS,
        *ORCHESTRATION_TOOLS,
        *ASSIGN_TOOLS,
        *TRIAGE_TOOLS,
        *PROFILE_TOOLS,
        *MEMORY_TOOLS,
        *EPISODE_TOOLS,
        *LEARN_TOOLS,
        *LEARN_CHATS_TOOLS,
    )
}


def resolve_tools(names: list[str]) -> list:
    """Map an agent's tool-name list to tool instances (unknown / disabled names ignored).
    Names starting with `mcp__` reference external MCP tools — those are resolved
    separately (agent_engine._build_agent connects the live server), so they're skipped
    here rather than looked up in ALL_TOOLS."""
    from curry_leaves_assistant.stores import tools_store  # lazy to avoid a circular import
    return [ALL_TOOLS[n] for n in names
            if n in ALL_TOOLS and not n.startswith("mcp__") and tools_store.is_enabled(n)]
