"""SuspendHost — lets a headless background run ask a human, without blocking the pool.

A background job runs with nobody attached, but an `ask`/`approve` needs a person. Instead of
freezing a worker for however long the human takes, SuspendHost:
  1. publishes the same ask/approve frame the chat UI already understands to the hub channel
     keyed by the job id (so the runs dashboard can render it live via subChat),
  2. emits `agent.run.needs_input` (activity feed / notification),
  3. RELEASES the run's scheduler capacity so other work runs while this one waits,
  4. parks on a Future until answered — the slot is already free, so waiting costs nothing.
     CURRY_LEAVES_APPROVAL_TIMEOUT (seconds) opts back into a hard cap → the request's
     default (deny) if nobody answers in time,
  5. on `resolve` (the dashboard answering via the `run.respond` WS method) → REACQUIRES
     capacity and returns the answer so the run continues.

The pending request is durable: queue/<jobId>.pending.json holds the full frame and is
served by GET /pending-inputs, so the UI can list every unanswered question — even ones
older than the activity feed — and answer whenever. A process restart re-runs the job
(the kernel's durability contract) and it re-asks when it reaches the same point.

Registered in a process-wide table by job id so `run.respond` can find the right host. This
is the same mechanism that later powers workflow `await_results` (a run suspending on child
completions instead of a human) — the primitive is "give the slot back while you wait."
"""
from __future__ import annotations

import asyncio
import os

from curry_leaves.core.host import AskUser, ApproveTool

from curry_leaves_assistant.core import events
from curry_leaves_assistant.core.pending import clear as clear_pending, persist as persist_pending
from curry_leaves_assistant.core.ws_hub import hub

# 0 (the default) waits until answered; a positive value is a hard cap → default-deny.
APPROVAL_TIMEOUT = float(os.environ.get("CURRY_LEAVES_APPROVAL_TIMEOUT", "0"))

# job_id → SuspendHost, so the WS `run.respond` method can resolve the right pending request.
_HOSTS: dict[str, "SuspendHost"] = {}


def resolve(job_id: str, request_id: str, answer) -> bool:
    """Answer a pending request for a suspended run (called from api/ws.py run.respond)."""
    host = _HOSTS.get(job_id)
    return host.resolve(request_id, answer) if host else False


def hosted_job_ids() -> set[str]:
    """Jobs parked in THIS process — the only ones whose pending request is answerable now."""
    return set(_HOSTS)


class SuspendHost:
    """Host for a background run that may need human input. `lane` + the scheduler let the
    run release/reacquire its slot around the wait so it never starves the pool."""

    def __init__(self, job_id: str, agent: dict, lane: str) -> None:
        self.job_id = job_id
        self.agent = agent
        self.lane = lane
        self._futures: dict[str, asyncio.Future] = {}
        self._seq = 0
        _HOSTS[job_id] = self  # register so run.respond can find us

    # Host protocol: emit is fire-and-forget notify. Background runs have no live stream to
    # forward incremental events to, so we drop them (tracing already records the run).
    def emit(self, event) -> None:  # noqa: D401
        return

    async def request(self, req):
        """Publish the ask/approve to the run's hub channel + notify, release capacity, and
        park until answered (or timeout → the request's default, i.e. deny)."""
        self._seq += 1
        rid = f"q{self._seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._futures[rid] = fut

        frame = self._frame(rid, req)
        if frame is None:
            return req.default
        # Persist a pending record (survives a UI reload; served by GET /pending-inputs).
        persist_pending(self.job_id, rid, self.agent, frame)
        # Same channel + frame shapes chat uses → the runs dashboard renders it via subChat.
        hub.publish_chat(self.job_id, frame)
        events.emit("agent.run.needs_input",
                    payload={"jobId": self.job_id, "agentId": self.agent.get("id"),
                             "requestId": rid, "kind": frame["type"]},
                    entity_id=self.agent.get("id"),
                    label=f"{self.agent.get('name', 'Agent')} needs input")

        from curry_leaves_assistant.orchestration.scheduler import scheduler
        await scheduler.release_for_wait(self.lane)   # free the slot while a human decides
        try:
            if APPROVAL_TIMEOUT > 0:
                return await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT)
            return await fut                          # no cap: park until answered
        except asyncio.TimeoutError:
            return req.default                        # capped and nobody answered → default (deny)
        finally:
            self._futures.pop(rid, None)
            await scheduler.reacquire_after_wait(self.lane)   # take a slot back to continue
            clear_pending(self.job_id)

    def resolve(self, request_id: str, answer) -> bool:
        fut = self._futures.get(request_id)
        if fut and not fut.done():
            fut.set_result(answer)
            return True
        return False

    def close(self) -> None:
        _HOSTS.pop(self.job_id, None)

    # ─── internals ────────────────────────────────────────────────────────────
    def _frame(self, rid: str, req) -> dict | None:
        if isinstance(req, AskUser):
            return {"type": "ask", "id": rid, "question": req.question, "options": list(req.options)}
        if isinstance(req, ApproveTool):
            return {"type": "approve", "id": rid, "tool": req.tool, "args": req.args,
                    "risk": req.risk, "reason": getattr(req, "reason", "")}
        return None

