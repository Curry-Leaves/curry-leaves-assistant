"""Trigger sources → WorkItems.

Two ways work gets born here:
  • event triggers — an emitted event matched against agents' `triggers` → one WorkItem per
    matching agent (scoped for recording.* fan-out). Synchronous, via events.on_event.
  • scheduled work — agents on a `schedule`, plus the nightly knowledge Gardener, expressed
    as ScheduleSources the unified scheduler (orchestration.schedule) ticks. This module no
    longer runs its own poll loop; it just registers the sources.

Everything here still just constructs a WorkItem and calls work.submit() (or, for the
Gardener, runs a maintenance pass); the kernel owns dispatch, lanes, durability, dead-letter,
and the loop guard.
"""
from __future__ import annotations

import asyncio
import os

from curry_leaves_assistant.core import events
from curry_leaves_assistant.core.store import now_iso
from curry_leaves_assistant.orchestration import schedule, work
from curry_leaves_assistant.orchestration.schedule import ScheduledJob
from curry_leaves_assistant.orchestration.work import (
    BAND_BACKGROUND, BAND_EVENT, WorkItem,
)
from curry_leaves_assistant.stores import agent_store

GARDEN_HOUR = int(os.environ.get("CURRY_LEAVES_GARDEN_HOUR", "3"))


def start() -> None:
    """Register the event trigger handler and the schedule sources (agents + Gardener). The
    unified scheduler (orchestration.schedule) owns the tick loop; there is no loop here."""
    events.on_event(_handle_event)
    schedule.register(_AgentScheduleSource())
    schedule.register(_GardenerSource())
    print("[triggers] started", flush=True)


# ─── event → work ─────────────────────────────────────────────────────────────
def _handle_event(event: dict) -> None:
    """Sync; called from emit() in any context. Submit one WorkItem per matching agent."""
    if event["type"].startswith(events._NON_TRIGGER_PREFIXES):
        return  # agent.run.* / agent.job.* / chat.run.* never re-trigger agents
    candidates = agent_store.agents_for_trigger(event["type"])
    if event["type"].startswith("recording."):
        candidates = [a for a in candidates if _agent_bound(a, event)]
    for agent in candidates:
        work.submit(WorkItem(
            kind="agent",
            agent_id=agent["id"],
            trigger=event,
            mode="background",
            lane=agent.get("lane") or "general",
            band=BAND_EVENT,
            autonomy=agent.get("autonomy") or "auto",
            dedupe_key=event.get("id"),   # (event, agent) → at most one job
        ))


def _agent_bound(agent: dict, event: dict) -> bool:
    """Scope recording.* fan-out to the recording's own agentIds (or always-agents / legacy
    unbound recordings)."""
    if agent.get("always"):
        return True
    agent_ids = (event.get("payload") or {}).get("agentIds")
    return agent_ids is None or agent["id"] in agent_ids


# ─── scheduled agents ─────────────────────────────────────────────────────────
def _submit_scheduled_agent(agent: dict) -> None:
    """Fire a scheduled agent run as a background WorkItem. Kernel idempotency (job_id derived
    from the trigger id) still guards a double-fire within the same window."""
    ev_id = f"sched.{agent['id']}.{now_iso()}"
    work.submit(WorkItem(
        kind="agent",
        agent_id=agent["id"],
        trigger={"id": ev_id, "type": "schedule", "occurredAt": now_iso(), "payload": {}},
        mode="background",
        lane=agent.get("lane") or "general",
        band=BAND_BACKGROUND,   # scheduled batches yield to interactive + event work
        autonomy=agent.get("autonomy") or "auto",
        dedupe_key=ev_id,
    ))


class _AgentScheduleSource:
    """Every enabled agent carrying a real ScheduleSpec becomes one scheduled job."""
    name = "agents"

    def jobs(self) -> list[ScheduledJob]:
        out: list[ScheduledJob] = []
        for agent in agent_store.list_agents():
            if not agent.get("enabled"):
                continue
            spec = agent.get("schedule") or {"kind": "none"}
            if spec.get("kind", "none") == "none":
                continue
            # bind the agent into the fire closure by default-arg so the loop var isn't captured
            out.append(ScheduledJob(
                key=f"agent:{agent['id']}",
                spec=spec,
                fire=(lambda a=agent: _submit_scheduled_agent(a)),
            ))
        return out


# ─── nightly knowledge Gardener ───────────────────────────────────────────────
# Mechanical passes are pure code; LLM compaction is a separate agent triggered by
# maintenance.completed. Runs daily at GARDEN_HOUR (local). The unified scheduler's persisted
# next-run state provides the once-a-day guarantee across restarts, so the old
# gardener-last-run.txt date stamp is gone.
def _run_gardener() -> None:
    async def _go() -> None:
        try:
            from curry_leaves_assistant.domain import knowledge_gardener
            report = await asyncio.to_thread(knowledge_gardener.run)
            print(f"[gardener] ran: {report.get('repairs', 0)} repairs, "
                  f"{len(report.get('compaction', []))} to compact", flush=True)
            # Skill lifecycle sweep: promote trial skills that measured well, retire ones that
            # measured badly. Mechanical (no LLM) — closes the learning loop's feedback edge.
            from curry_leaves_assistant.stores import skill_meta
            sweep = await asyncio.to_thread(skill_meta.lifecycle_sweep)
            if sweep.get("promoted") or sweep.get("retired"):
                print(f"[skills] lifecycle: promoted {sweep['promoted']}, retired {sweep['retired']}", flush=True)
        except Exception as exc:
            print(f"[gardener] failed: {exc}", flush=True)
    # fire() is sync (called from the tick); the Gardener does blocking work, so hand it to
    # the loop as a task rather than blocking the scheduler tick.
    asyncio.get_event_loop().create_task(_go())


class _GardenerSource:
    name = "gardener"

    def jobs(self) -> list[ScheduledJob]:
        return [ScheduledJob(
            key="gardener",
            spec={"kind": "cron", "expr": f"0 {GARDEN_HOUR} * * *"},
            fire=_run_gardener,
        )]
