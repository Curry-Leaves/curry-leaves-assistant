"""The `triage_post` tool — how the Todo Triage agent hands an actionable todo to the team.

The Todo Triage agent is triggered by `todo.created`. When it judges a new todo to be something
the team can meaningfully do with their tools, it calls `triage_post` once. That:
  • posts the todo to the common pool as a user-sourced item (which emits `pool.item.created`,
    waking the Lead to route it — exactly like a human dropping a task), carrying a `todoId`
    back-reference so the finished run's result + conversation land back on the todo, and
  • moves the todo to 'working' so the UI shows a run is in flight.

It deliberately does NOT enqueue a job itself — routing is the Lead's job. Pure personal
reminders the team can't act on should be left alone (the agent simply doesn't call this).
"""
from __future__ import annotations

from pydantic import BaseModel, Field
from curry_leaves.core.tools import ToolResult

from curry_leaves_assistant.stores import data, pool_store


class TriagePostTool:
    """Post an actionable todo to the common pool for the team, and mark the todo 'working'.
    Call this exactly once, only for todos the team can actually DO with their tools. Leave
    pure personal reminders (call mom, buy milk) untouched — just don't call this for them."""
    name = "triage_post"
    description = (
        "Post an ACTIONABLE todo to the common pool so the team can attempt it. Use only when the "
        "todo is something the team can meaningfully do with their tools (research, notes, filing, "
        "dashboards, etc.) — NOT for pure personal reminders (call mom, buy milk), which you should "
        "leave alone by not calling this.\n"
        "Give `todoId` (from the todo you were woken for), a short `title`, and a `description` that "
        "restates what needs doing as a clear task brief. This posts the task (waking the Lead to "
        "route it to the best-fit teammate) and marks the todo 'working'. Call it exactly once, then "
        "stop — do NOT do the task yourself."
    )
    risk = "exec"  # posts real work to the pool that the team will act on

    class Args(BaseModel):
        todoId: str = Field(description="The id of the todo you were woken for (from the run brief).")
        title: str = Field(description="A short title for the pool task.")
        description: str = Field(default="", description="A clear brief restating what needs doing.")
        tags: list[str] = Field(default_factory=list, description="Optional tags to aid routing.")
        priority: str = Field(default="P2", description="P1 | P2 | P3 (default P2).")

    schema = Args
    timeout = None

    async def run(self, args: "TriagePostTool.Args", ctx, signal) -> ToolResult:
        todo = next((t for t in data.list_todos() if t["id"] == args.todoId), None)
        if todo is None:
            return ToolResult(content=f"No such todo: {args.todoId}.", is_error=True)
        item = pool_store.create(
            args.title or todo.get("text") or "Untitled task",
            args.description, args.tags, args.priority,
            source="user", autonomy="auto", todo_id=args.todoId)
        data.attach_assistant_result(args.todoId, status="working", pool_item_id=item["id"])
        return ToolResult(content=(
            f"Posted '{item.get('title')}' to the pool (todo {args.todoId} → working). "
            "The Lead will route it to a teammate. You're done — stop now."))


TRIAGE_TOOLS = [TriagePostTool()]
