"""Assign a waiting pool item to an agent and enqueue the durable job.

The single place that turns "give pool item X to agent Y" into a real WorkItem on the Work
Kernel. `pool_store` deliberately can't reach up into orchestration (layer boundary), so the
enqueue lives here, above both the store and the kernel. Two callers use it:
  • api/tasks.py — a human clicking "assign" on a pool card.
  • agents/assign_tools.py — the Lead routing a posted task to the best-fit agent.

Keeping it here (not in the API layer) is what lets the Lead's tool assign work exactly the
way the UI does, with one implementation and no drift.
"""
from __future__ import annotations

from curry_leaves_assistant.orchestration import work
from curry_leaves_assistant.orchestration.work import BAND_INTERACTIVE, WorkItem
from curry_leaves_assistant.stores import agent_store, pool_store


def assign_pool_item(item_id: str, agent_id: str) -> dict | None:
    """Mark `item_id` assigned to `agent_id` and enqueue the agent's job (interactive band —
    a triaged task jumps ahead of routine event work). Returns the updated pool item, or None
    if the item doesn't exist. Assumes the agent exists (callers validate first)."""
    item = pool_store.assign(item_id, agent_id)
    if item is None:
        return None
    # pool_store.assign returns the job it wants run under a private `_job` key (the store
    # can't enqueue itself). Submit it, then strip the internal field before returning.
    job = item.pop("_job", None)
    if job:
        agent = agent_store.read_agent(job["agentId"]) or {}
        # Autonomy precedence: the ASK's own choice (set by the poster in the composer) wins;
        # an agent that is itself 'ask'-only can still force a pause. So 'ask' from either side
        # means ask; only when both allow auto does the run go headless.
        item_autonomy = item.get("autonomy") or "auto"
        agent_autonomy = agent.get("autonomy") or "auto"
        autonomy = "ask" if "ask" in (item_autonomy, agent_autonomy) else "auto"
        work.submit(WorkItem(
            kind="agent", agent_id=job["agentId"], trigger=job["trigger"], mode="background",
            lane=agent.get("lane") or "general", band=BAND_INTERACTIVE,
            autonomy=autonomy,
            dedupe_key=job["trigger"].get("id") or f"pool.{item_id}"))
    return item
