"""Orchestration tool — how an agent hands work to the background pool and composes it.

The `orchestrate` tool turns "the LLM decided to run other agents" into durable, laned,
governed WorkItems, action-dispatched:
  • assign  — fire-and-forget: hand a task to a background agent, don't wait (the chat →
              pool front door; the user keeps chatting while it runs).
  • spawn   — start a background agent run and get a handle back (for workflows).
  • status  — poll a job's state/output (non-blocking).
  • await   — wait for spawned runs to finish and collect outputs. The awaiting agent
              SUSPENDS (its pool slot is released) until the children complete — this is
              what lets a "workflow skill" fan out and join durably.

Together with a workflow SKILL.md (a prose sequence the orchestrator agent follows), these
actions are the entire "workflow engine": there is no engine, just an agent using this tool.

Layer note: this agent-facing tool submits into orchestration (work.submit /
work.on_complete) via lazy imports — the same allowed upward edge dashboard_tools uses for
run_tile (see the import-linter contract).
"""
from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, Field
from curry_leaves.core.tools import ToolResult


def _err(msg: str) -> ToolResult:
    return ToolResult(content=msg, is_error=True)


class OrchestrateTool:
    """One tool for background orchestration, action-dispatched: assign | spawn | status |
    await. `assign` is fire-and-forget side work; `spawn` + `await` compose a durable
    workflow (spawn several, then await to join); `status` polls without blocking. Every
    spawned/assigned agent starts with NO memory of this chat — its brief must be fully
    self-contained (subject, ids, facts), never a reference like 'it'/'that'."""
    name = "orchestrate"
    description = (
        "Run background agents — action: assign | spawn | status | await.\n"
        "• assign: hand a task to a background agent, fire-and-forget. Returns a job id "
        "immediately; runs in the durable pool (retried on crash, in the activity feed) while "
        "you continue. For side work the user needn't wait for.\n"
        "• spawn: start a run and get a job id back WITHOUT waiting — the building block of a "
        "workflow. Spawn several, then `await` them to run in parallel and join.\n"
        "• status: POLL one job's state (queued/running/done/failed/dead) and, once finished, "
        "its output/error. Returns instantly — use for 'is it done?'.\n"
        "• await: BLOCK (durably — yields this agent's pool slot) until the given jobs reach a "
        "terminal state, then return each one's status + output.\n"
        "CRITICAL: an assigned/spawned agent has NO memory of this conversation — write a "
        "COMPLETE, self-contained brief (subject, names/ids/dates, constraints, what a good "
        "result looks like), never 'it'/'that'. If you can't yet, gather context or ask first."
    )
    risk = "exec"  # assign/spawn exec; status/await are lighter but gated together

    class Args(BaseModel):
        action: Literal["assign", "spawn", "status", "await"] = Field(
            description="assign | spawn | status | await")
        # assign / spawn
        agentId: str | None = Field(default=None, description="assign/spawn: the background agent to run (e.g. 'kb-filer').")
        title: str | None = Field(default=None, description="assign: short title shown in the activity feed.")
        description: str | None = Field(default=None, description=(
            "assign/spawn: the COMPLETE, self-contained task brief — the agent has no other "
            "context, so spell out the subject, goal, and every fact from the chat it needs."))
        # status
        jobId: str | None = Field(default=None, description="status: the job id returned by assign/spawn.")
        # await
        jobIds: list[str] | None = Field(default=None, description="await: job ids returned by spawn.")
        timeoutSec: int = Field(default=900, description="await: max seconds to wait before giving up.")

    schema = Args
    timeout = None

    async def run(self, args: "OrchestrateTool.Args", ctx, signal) -> ToolResult:
        if args.action == "assign":
            if not args.agentId or not args.title:
                return _err("assign requires `agentId` and `title`.")
            job_id = _submit(args.agentId, args.title, args.description or "", band_interactive=True)
            if job_id is None:
                return _err(f"No such agent: {args.agentId}")
            return ToolResult(content=f"Handed '{args.title}' to {args.agentId} (job {job_id[:12]}). "
                                      "It's running in the background; the user can watch it in the Agents tab.")

        if args.action == "spawn":
            if not args.agentId or not args.description:
                return _err("spawn requires `agentId` and `description` (the self-contained input).")
            job_id = _submit(args.agentId, args.description[:60], args.description, band_interactive=False)
            if job_id is None:
                return _err(f"No such agent: {args.agentId}")
            return ToolResult(content=f"Spawned {args.agentId} → jobId: {job_id}. "
                                      "Call orchestrate(action='await', jobIds=[...]) to get its output when done.")

        if args.action == "status":
            if not args.jobId:
                return _err("status requires `jobId`.")
            from curry_leaves_assistant.orchestration import work
            st = work.job_status(args.jobId)
            if st is None:
                return _err(f"No job found for id {args.jobId!r}. It may never have been submitted, "
                            "or its record has been pruned from history.")
            state = st.get("state")
            if state in ("done",):
                out = (st.get("output") or "").strip()
                return ToolResult(content=f"Job {args.jobId[:12]} is DONE.\n\n{out or '(no output)'}")
            if state in ("failed", "dead"):
                err = (st.get("error") or "").strip()
                return ToolResult(content=f"Job {args.jobId[:12]} {state.upper()}: {err or 'no detail'}")
            # queued / running — not finished yet.
            return ToolResult(content=f"Job {args.jobId[:12]} is {str(state).upper()} — not finished yet. "
                                      "Check again shortly, or use action='await' to wait for it.")

        # await
        if not args.jobIds:
            return _err("await requires `jobIds`.")
        from curry_leaves_assistant.orchestration import work
        from curry_leaves_assistant.orchestration.scheduler import scheduler

        futs = {jid: work.on_complete(jid) for jid in args.jobIds}
        # Release this agent's pool slot while it waits on children, so awaiting doesn't
        # starve the pool (mirrors SuspendHost). Reacquire before continuing.
        lane = _current_lane()
        await scheduler.release_for_wait(lane)
        try:
            done = await asyncio.wait_for(
                asyncio.gather(*futs.values(), return_exceptions=True), timeout=args.timeoutSec)
        except asyncio.TimeoutError:
            done = []
        finally:
            await scheduler.reacquire_after_wait(lane)

        lines = ["Results:"]
        results = {}
        for job in done:
            if isinstance(job, dict):
                results[job.get("id")] = job
        for jid in args.jobIds:
            job = results.get(jid)
            if not job:
                lines.append(f"- {jid[:12]}: (timed out / no result)")
                continue
            status = job.get("status")
            out = (job.get("output") or job.get("error") or "").strip()
            lines.append(f"- {jid[:12]} [{status}]: {out[:500]}")
        return ToolResult(content="\n".join(lines))


# ─── helpers (lazy imports keep module load cheap + the layer edge explicit) ──
def _submit(agent_id: str, title: str, description: str, *, band_interactive: bool):
    from curry_leaves_assistant.orchestration import work
    from curry_leaves_assistant.orchestration.work import BAND_BACKGROUND, BAND_INTERACTIVE, WorkItem
    from curry_leaves_assistant.stores import agent_store, pool_store

    agent = agent_store.read_agent(agent_id)
    if agent is None:
        return None
    # Record it as a common-pool item too, so it appears in the Tasks view + closes out.
    # source='orchestrate' so the Lead's pool.item.created trigger skips it — this item is
    # already targeted at `agent_id` below, not awaiting triage.
    item = pool_store.create(title, description, source="orchestrate")
    trigger = {"id": item["id"], "type": "task", "occurredAt": _now(),
               "payload": {"poolItemId": item["id"], "input": description, "title": title}}
    # Propagate the current trace so the loop guard bounds runaway spawn chains.
    _stamp_trace(trigger)
    return work.submit(WorkItem(
        kind="agent", agent_id=agent_id, trigger=trigger, mode="background",
        lane=agent.get("lane") or "general",
        band=BAND_INTERACTIVE if band_interactive else BAND_BACKGROUND,
        autonomy=agent.get("autonomy") or "auto",
        dedupe_key=item["id"]))


def _stamp_trace(trigger: dict) -> None:
    from curry_leaves_assistant.core import trace_ctx
    cur = trace_ctx.current()
    if cur:
        trigger["traceId"] = cur["traceId"]
        trigger["spanId"] = cur["spanId"]


def _current_lane() -> str:
    """The lane the awaiting run occupies. Orchestrators default to 'general'; the exact lane
    only matters for capacity accounting, and general is the safe reacquire target."""
    return "general"


def _now() -> str:
    from curry_leaves_assistant.core.store import now_iso
    return now_iso()


ORCHESTRATION_TOOLS = [OrchestrateTool()]
