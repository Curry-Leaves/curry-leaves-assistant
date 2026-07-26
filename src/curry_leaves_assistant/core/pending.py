"""Durable records of a suspended run's unanswered ask/approve request.

One JSON file per parked job — queue/<jobId>.pending.json — written the moment a
background run asks the user something and removed when the answer arrives. They are
what GET /pending-inputs serves, so the UI can list every outstanding question (even
ones older than the activity feed or a page reload) and answer whenever. Both suspend
paths write here: SuspendHost (non-streaming waits) and SSEChatHost (streaming
background runs — workers, tiles).

`poolItemId` links the question back to the pool ask it is about, when known: an
assigned run's item, or — for the Lead's triage clarifications — the item whose
`pool.item.created` trigger woke the run. The desk uses it to pin the question onto
the right ask row.
"""
from __future__ import annotations

from pathlib import Path

from curry_leaves_assistant.core.paths import QUEUE_DIR
from curry_leaves_assistant.core.store import now_iso, write_json


def pending_path(job_id: str) -> Path:
    return QUEUE_DIR / f"{job_id}.pending.json"


def persist(job_id: str, request_id: str, agent: dict, frame: dict,
            pool_item_id: str | None = None) -> None:
    """Best-effort write of the pending record (never raises into the run)."""
    try:
        rec = {"jobId": job_id, "requestId": request_id,
               "agentId": agent.get("id"), "agentName": agent.get("name"),
               "frame": frame, "at": now_iso()}
        if pool_item_id:
            rec["poolItemId"] = pool_item_id
        write_json(pending_path(job_id), rec)
    except Exception:
        pass


def clear(job_id: str) -> None:
    try:
        pending_path(job_id).unlink(missing_ok=True)
    except Exception:
        pass
