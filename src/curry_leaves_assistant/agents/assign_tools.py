"""The `assign` tool — how the Lead routes a posted task to the right teammate.

The Lead is triggered by `pool.item.created`; this tool is its hands. Action-dispatched:
  • pool  — the waiting user-posted items awaiting triage (skips agent-delegated items).
  • team  — the roster the Lead routes to: each agent's name, role, and current load.
  • assign — hand ONE waiting item to ONE agent → a durable WorkItem on the Work Kernel
             (the agent runs as its own job, visible in the office, survives restart).

`assign` reuses the exact path the "assign" button in the UI uses (orchestration.dispatch),
so the Lead assigns work identically to a human, with no separate dispatch code.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from curry_leaves.core.tools import ToolResult

from curry_leaves_assistant.stores import agent_store, pool_store


def _err(msg: str) -> ToolResult:
    return ToolResult(content=msg, is_error=True)


# Engine-plumbing helpers the Lead must never hand user work to — these are invoked by a
# specific feature (the rich editors, the KB writers, the skill reflector, the live copilot),
# not chosen as teammates. NOTE: this is deliberately NOT keyed off the `internal` flag: every
# seeded agent carries `internal: true` for the "Default" UI grouping, but most of them
# (assistant, meeting-copilot, dashboard-watcher, kb-filer, title-generator) are still valid
# routing targets. Keep this list in sync when adding a new plumbing-only helper.
_NON_ROUTABLE_IDS = frozenset({
    "lead",            # can't route to itself
    "kb-maintainer", "note-editor", "text-editor", "skill-learner", "meeting-live",
    "voice",           # answers out loud in the moment; can't run background work
})


def _routable_agents() -> list[dict]:
    """Agents the Lead may hand work to: enabled, not a plumbing-only helper, and not the Lead
    itself. See `_NON_ROUTABLE_IDS` — routing is gated on that explicit set, not `internal`,
    because all seeds are `internal` (UI grouping) yet most are real teammates."""
    out = []
    for a in agent_store.list_agents():
        if not a.get("enabled", True) or a.get("id") in _NON_ROUTABLE_IDS:
            continue
        out.append(a)
    return out


def _load_by_agent() -> dict[str, int]:
    """How many jobs each agent has queued or in-flight right now — so the Lead can favor an
    idle teammate over a busy one. Best-effort; an empty map just means 'load unknown'."""
    try:
        from curry_leaves_assistant.orchestration import work
        counts: dict[str, int] = {}
        for job in work.queued_jobs():
            aid = job.get("agentId")
            if aid:
                counts[aid] = counts.get(aid, 0) + 1
        return counts
    except Exception:
        return {}


class AssignTool:
    """Route posted tasks to teammates — action: pool | team | assign. The Lead uses `pool` to
    see what's waiting, `team` to see who can do it, and `assign` to hand one task to one agent
    as a durable background job. It does NOT do the work itself — it routes and reports."""
    name = "assign"
    description = (
        "Route a posted task to the right teammate — action: pool | team | assign.\n"
        "• pool: list the waiting user-posted tasks awaiting assignment (id + what's needed). "
        "Start here to see what to route.\n"
        "• team: the roster you can assign to — each agent's id, name, role, tools, and current "
        "load (queued/running jobs). Match the task to an agent whose role AND tools fit — a "
        "task that creates or changes something needs an agent with the matching write tool; "
        "prefer a less-loaded one when several fit.\n"
        "• assign: hand ONE pool item to ONE agent (poolItemId + agentId). This enqueues a "
        "durable job for that agent (it runs on its own, visible in the office, and closes the "
        "pool item when done). Assign to exactly one agent per task.\n"
        "You are a router: understand the ask, pick the best teammate, assign. Do not attempt "
        "the task yourself. If genuinely no one fits, say so and leave the item unassigned."
    )
    risk = "exec"  # assign enqueues real work; pool/team are read-only but gated together

    class Args(BaseModel):
        action: Literal["pool", "team", "assign"] = Field(description="pool | team | assign")
        poolItemId: str | None = Field(default=None, description="assign: the waiting pool item's id (from action='pool').")
        agentId: str | None = Field(default=None, description="assign: the agent to hand it to (from action='team').")

    schema = Args
    timeout = None

    async def run(self, args: "AssignTool.Args", ctx, signal) -> ToolResult:
        if args.action == "pool":
            items = [it for it in pool_store.list_items()
                     if it.get("status") == "waiting" and it.get("source", "user") == "user"]
            if not items:
                return ToolResult(content="No user-posted tasks are waiting. Nothing to route.")
            lines = ["Waiting tasks:"]
            for it in items:
                desc = (it.get("description") or "").strip().replace("\n", " ")
                lines.append(f"- [{it['id']}] {it.get('title', '(untitled)')}"
                             + (f" — {desc[:200]}" if desc else ""))
            return ToolResult(content="\n".join(lines))

        if args.action == "team":
            load = _load_by_agent()
            agents = _routable_agents()
            if not agents:
                return _err("No routable teammates exist. The task can't be assigned.")
            lines = ["Team:"]
            for a in agents:
                n = load.get(a["id"], 0)
                busy = f" — {n} job(s) in flight" if n else " — idle"
                role = (a.get("description") or "").strip().replace("\n", " ")
                # Tools are part of the listing so the Lead routes on capability, not role
                # wording alone — e.g. "create a dashboard tile" must go to an agent that HAS
                # the `dashboard` write tool, not the read-only tile runner whose description
                # merely mentions dashboards.
                tools = ", ".join((a.get("tools") or []) + (a.get("deferredTools") or []))
                lines.append(f"- {a['id']} ({a.get('name', a['id'])}){busy}"
                              + (f": {role[:160]}" if role else "")
                              + f"\n  tools: {tools or '(none)'}")
            return ToolResult(content="\n".join(lines))

        # assign
        if not args.poolItemId or not args.agentId:
            return _err("assign requires `poolItemId` (from action='pool') and `agentId` (from action='team').")
        item = pool_store.get(args.poolItemId)
        if item is None:
            return _err(f"No such pool item: {args.poolItemId}. Call action='pool' for valid ids.")
        if item.get("status") != "waiting":
            return _err(f"Pool item {args.poolItemId} is already {item.get('status')} — not assignable.")
        if agent_store.read_agent(args.agentId) is None:
            return _err(f"No such agent: {args.agentId}. Call action='team' for valid ids.")
        # Reuse the exact UI assign path so the Lead dispatches identically to a human click.
        from curry_leaves_assistant.orchestration import dispatch
        updated = dispatch.assign_pool_item(args.poolItemId, args.agentId)
        if updated is None:
            return _err(f"Failed to assign {args.poolItemId}.")
        return ToolResult(content=f"Assigned '{updated.get('title', args.poolItemId)}' to {args.agentId}. "
                                  "It's now running as a background job and will close the task when done.")


ASSIGN_TOOLS = [AssignTool()]
