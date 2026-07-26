"""The Work Kernel — one front door for every agent run.

Every agent execution in the app is a `WorkItem` submitted through `submit()`. What differs
between a chat turn, a triggered background job, a dashboard tile, and a workflow step is
*data on the WorkItem* (mode, lane, band, autonomy), not separate dispatch code.

Two execution paths behind the one door:
  • mode="background"  → a durable job file → LaneScheduler → workers (this module owns the
                          job shape + dedupe + the completion-future registry; scheduler.py
                          picks, workers.py runs).
  • mode="interactive"/"ephemeral" → registered here for identity/observability, but executed
                          on the caller's own live-streaming path (chat). Interactive runs must
                          never wait behind background work, so they are not queued.

Durability, lanes, dead-letter, loop-guard, recovery live in scheduler.py / workers.py; this
module is the vocabulary (WorkItem, ids, bands) + the submit entry point + `on_complete`
(the join primitive workflows and `await_results` build on).
"""
from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

from curry_leaves_assistant.core import events
from curry_leaves_assistant.core.paths import QUEUE_DIR, agent_runs_dir
from curry_leaves_assistant.core.store import now_iso, read_json, write_json

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")

# Priority bands — lower runs first. Interactive-origin work (a user clicked, or a chat
# handed off a task) jumps ahead of event-triggered work, which jumps ahead of scheduled /
# maintenance batches. FIFO within a band.
BAND_INTERACTIVE = 0
BAND_EVENT = 1
BAND_BACKGROUND = 2


@dataclass
class WorkItem:
    """One unit of agent work. For background items this is persisted verbatim as the job
    JSON (plus runtime status fields the worker adds)."""
    kind: str = "agent"                     # "agent" | "tile"
    agent_id: Optional[str] = None          # who runs (None for tile — carried in payload)
    trigger: dict = field(default_factory=dict)   # the event/payload that composes the input
    mode: str = "background"                # "background" | "interactive" | "ephemeral"
    lane: str = "general"                   # sequential/parallel channel (see scheduler)
    band: int = BAND_EVENT                  # priority band
    autonomy: str = "auto"                  # "auto" (approve-all) | "ask" (SuspendHost)
    dedupe_key: Optional[str] = None        # stable id → idempotent enqueue; None → random
    correlation_id: Optional[str] = None    # rides into agent.run.* events (workflow joins)

    def job_id(self) -> str:
        """Deterministic job id: the same (dedupe_key, agent) enqueues at most one job, even
        across restarts. Falls back to a random id when no dedupe_key is given."""
        key = self.dedupe_key or uuid.uuid4().hex
        target = self.agent_id or self.kind
        return _SAFE.sub("_", f"{key}__{target}")[:180]


# ─── job file paths (the durable queue) ───────────────────────────────────────
def queue_file(job_id: str):
    return QUEUE_DIR / f"{job_id}.json"


def running_file(job_id: str):
    return QUEUE_DIR / f"{job_id}.json.running"


def dead_file(job_id: str):
    return QUEUE_DIR / "dead" / f"{job_id}.json"


def run_record(agent_id: str, job_id: str):
    return agent_runs_dir(agent_id) / f"{job_id}.json"


# ─── completion registry (the join primitive) ─────────────────────────────────
# jobId → Future resolved with the finished job dict. Workers resolve on terminal status;
# `on_complete` lets a caller (await_results, a workflow orchestrator) wait for children.
_completion: dict[str, asyncio.Future] = {}
_loop: Optional[asyncio.AbstractEventLoop] = None

# Terminal states a job can be resolved from (mirrors job_status precedence).
_TERMINAL_STATES = {"done", "stopped", "failed", "dead"}


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def on_complete(job_id: str) -> asyncio.Future:
    """A Future that resolves with the job's final record when it reaches a terminal state.
    If the job already finished (run record / dead-letter on disk), resolves immediately —
    otherwise a fast child that completes BEFORE its parent calls on_complete would hand back
    a fresh, never-resolved future and the parent would block until timeout."""
    loop = _loop or asyncio.get_event_loop()
    fut = _completion.get(job_id)
    if fut is None:
        fut = loop.create_future()
        _completion[job_id] = fut
    if not fut.done():
        # Terminal-first check: resolve now if the job already reached a terminal state.
        status = job_status(job_id)
        if status and status.get("state") in _TERMINAL_STATES:
            fut.set_result({"id": job_id, "status": status.get("state"),
                            "output": status.get("output"), "error": status.get("error"),
                            "agentId": status.get("agentId")})
            _completion.pop(job_id, None)
    return fut


def resolve_completion(job: dict) -> None:
    """Called by workers when a job reaches a terminal status — wakes any awaiters, then drops
    the future from the registry so it can't accumulate for the process lifetime."""
    job_id = job.get("id") or ""
    fut = _completion.get(job_id)
    if fut is not None and not fut.done():
        fut.set_result(job)
    # Pop unconditionally: the awaiter already holds its own reference to the future, so
    # removing it here just stops the registry growing without bound. An on_complete that
    # arrives AFTER this re-resolves immediately via the terminal-first check above.
    _completion.pop(job_id, None)


# The scheduler registers itself here so submit() can hand it a newly-written background job
# from any thread. Kept as a callback to avoid a hard import cycle (work ← scheduler).
_enqueue_cb = None


def register_enqueue(cb) -> None:
    global _enqueue_cb
    _enqueue_cb = cb


def submit(item: WorkItem) -> str:
    """The one front door. Returns the job id (stable, even when deduped or refused).

    Background: writes a durable job file and hands it to the scheduler (idempotent — a job
    already queued/running/done is a no-op). Interactive/ephemeral: returns an id for
    identity; the caller executes it on its own path (this keeps chat off the background
    queue by design)."""
    job_id = item.job_id()

    if item.mode != "background":
        # Identity only — the caller (chat) runs it live. No queue, no file.
        return job_id

    # Idempotent: already queued, in-flight, or completed → no-op.
    if (queue_file(job_id).exists() or running_file(job_id).exists()
            or (item.agent_id and run_record(item.agent_id, job_id).exists())):
        return job_id

    # Loop guard: refuse work whose causal chain is already too deep / re-entrant.
    from curry_leaves_assistant.orchestration import guard
    refusal = guard.loop_refusal(item.agent_id, item.trigger)
    if refusal:
        events.emit("agent.job.refused",
                    payload={"agentId": item.agent_id, "reason": refusal,
                             "traceId": item.trigger.get("traceId")},
                    entity_id=item.agent_id, label=f"job refused: {refusal}")
        return job_id

    job = _to_job(item, job_id)
    write_json(queue_file(job_id), job)
    if _enqueue_cb is not None and _loop is not None:
        _loop.call_soon_threadsafe(_enqueue_cb, job_id)
    elif _enqueue_cb is not None:
        _enqueue_cb(job_id)
    broadcast_kernel()   # a new job appeared in the queue → refresh the live Work panel
    return job_id


def _to_job(item: WorkItem, job_id: str) -> dict:
    """The persisted job record. `trigger` carries the event/payload; the rest is policy the
    worker reads to build the host, choose the lane, and thread correlation."""
    return {
        "id": job_id,
        "kind": item.kind,
        "agentId": item.agent_id,
        "status": "pending",
        "attempts": 0,
        "lane": item.lane,
        "band": item.band,
        "autonomy": item.autonomy,
        "correlationId": item.correlation_id,
        "trigger": item.trigger,
        "createdAt": now_iso(),
    }


def queued_jobs(agent_id: str | None = None) -> list[dict]:
    """Jobs currently pending or in-flight (from the durable queue), newest first. Optionally
    filtered to one agent. Powers 'what's queued right now'."""
    out = []
    for f in list(QUEUE_DIR.glob("*.json")) + list(QUEUE_DIR.glob("*.json.running")):
        job = read_json(f, None)
        if not job:
            continue
        if agent_id and job.get("agentId") != agent_id:
            continue
        job["state"] = "running" if f.name.endswith(".running") else "queued"
        out.append(job)
    out.sort(key=lambda j: j.get("startedAt") or j.get("createdAt") or "", reverse=True)
    return out


# ─── kernel live view (the dashboard's Work panel) ────────────────────────────
# A trimmed job row for the wire — the operator view needs identity, placement
# (lane/band), state, and a human label, not the full trigger payload.
def _job_row(job: dict, state: str) -> dict:
    trig = job.get("trigger") or {}
    return {
        "id": job.get("id"),
        "kind": job.get("kind", "agent"),
        "agentId": job.get("agentId"),
        "lane": job.get("lane", "general"),
        "band": int(job.get("band", BAND_EVENT)),
        "state": state,
        "attempts": int(job.get("attempts", 0)),
        "autonomy": job.get("autonomy", "auto"),
        "correlationId": job.get("correlationId"),
        "traceId": job.get("traceId") or trig.get("traceId"),
        "triggerType": trig.get("type") or (trig.get("payload") or {}).get("type"),
        "createdAt": job.get("createdAt"),
        "startedAt": job.get("startedAt"),
        "deadReason": job.get("deadReason"),
    }


def kernel_snapshot() -> dict:
    """A full picture of the Work Kernel right now: every queued/running job and the most
    recent dead-lettered ones. Sent on subscribe and re-broadcast on every state change so
    the dashboard's Work panel is always a live mirror of the queue (no per-job deltas to
    keep in sync — one authoritative snapshot)."""
    active = queued_jobs()
    rows = [_job_row(j, j["state"]) for j in active]
    dead = []
    dead_dir = QUEUE_DIR / "dead"
    if dead_dir.exists():
        for f in dead_dir.glob("*.json"):
            job = read_json(f, None)
            if job:
                dead.append(_job_row(job, "dead"))
    dead.sort(key=lambda j: j.get("createdAt") or "", reverse=True)
    running = [r for r in rows if r["state"] == "running"]
    queued = [r for r in rows if r["state"] == "queued"]
    return {
        "running": running,
        "queued": queued,
        "dead": dead[:20],
        "counts": {"running": len(running), "queued": len(queued), "dead": len(dead)},
        "at": now_iso(),
    }


def job_status(job_id: str) -> dict | None:
    """Look a job up by id alone, across every state it could be in, and return a uniform
    status dict. A caller (an agent that fired `assign_task`, the API) holds only the job id —
    this finds it wherever it lives:
      • finished  → the run record  RUNS_DIR/<agentId>/<jobId>.json   (has output/error)
      • dead      → queue/dead/<jobId>.json                            (has deadReason)
      • running   → queue/<jobId>.json.running
      • queued    → queue/<jobId>.json
    Returns None only if the id is unknown (never submitted, or pruned from the runs history).
    Precedence is terminal-first: a run record is the source of truth even if a stale queue
    file lingers."""
    from curry_leaves_assistant.core.paths import RUNS_DIR
    # Finished — the authoritative record. Job ids are unique, so the first match wins.
    for f in RUNS_DIR.glob(f"*/{job_id}.json"):
        rec = read_json(f, None)
        if rec:
            return {"jobId": job_id, "state": rec.get("status", "done"),
                    "agentId": rec.get("agentId"), "output": rec.get("output"),
                    "error": rec.get("error"), "finishedAt": rec.get("finishedAt"),
                    "traceId": rec.get("traceId")}
    # Dead-lettered.
    dead = read_json(dead_file(job_id), None)
    if dead:
        return {"jobId": job_id, "state": "dead", "agentId": dead.get("agentId"),
                "output": None, "error": dead.get("deadReason"),
                "attempts": dead.get("attempts"), "finishedAt": dead.get("deadAt")}
    # In-flight or queued.
    if running_file(job_id).exists():
        job = read_json(running_file(job_id), {}) or {}
        return {"jobId": job_id, "state": "running", "agentId": job.get("agentId"),
                "output": None, "error": None, "startedAt": job.get("startedAt")}
    if queue_file(job_id).exists():
        job = read_json(queue_file(job_id), {}) or {}
        return {"jobId": job_id, "state": "queued", "agentId": job.get("agentId"),
                "output": None, "error": None, "createdAt": job.get("createdAt")}
    return None


def broadcast_kernel() -> None:
    """Publish the current kernel snapshot to the `kernel` WS channel. Called (thread-safe,
    the hub hops to the loop) at every lifecycle transition — submit, claim→running,
    terminal, dead-letter — so subscribers see the queue change in real time. Best-effort:
    never let a telemetry push break job processing."""
    try:
        from curry_leaves_assistant.core.ws_hub import hub
        hub.publish_kernel(kernel_snapshot())
    except Exception:
        pass
