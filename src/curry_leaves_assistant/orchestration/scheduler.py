"""LaneScheduler — the queue discipline: sequential channels, parallel channels, priority.

Replaces the pool's single FIFO + global semaphore. Jobs are routed to a named **lane**;
each lane has a **width** (max concurrent jobs in it): width 1 = strictly sequential (e.g.
"kb" — kb-filer runs never overlap), width N = capped parallel (e.g. "tiles" — no stampede),
"general" = unbounded within the global cap (independent readers run freely).

Across lanes, jobs run by **priority band** (lower first), FIFO within a band. A worker asks
`next_runnable()` for the best job it's allowed to start right now: lowest band → oldest →
whose lane has spare width and the global cap has room.

**Capacity is explicit** so a run that suspends (waiting on a human approval or on child
jobs) can *release* its slot — freeing the worker and the lane for other work — then
*reacquire* when it resumes. That's what makes suspend/await not starve the pool.
"""
from __future__ import annotations

import asyncio
import os

MAX_CONCURRENCY = int(os.environ.get("CURRY_LEAVES_MAX_CONCURRENCY", "4"))

# Named lane widths. Anything not listed → "general" (unbounded within the global cap).
# kb: sequential (serializes KB writers). tiles: capped parallel (no 08:00 stampede).
# maintenance: sequential (the gardener / sweeps).
_LANE_WIDTH: dict[str, int] = {
    "kb": 1,
    "tiles": int(os.environ.get("CURRY_LEAVES_TILES_WIDTH", "2")),
    "maintenance": 1,
}


def lane_width(lane: str) -> int:
    return _LANE_WIDTH.get(lane, MAX_CONCURRENCY)  # "general" et al: only the global cap bounds


class LaneScheduler:
    def __init__(self) -> None:
        # Pending jobs waiting to start: (band, seq, jobId, lane). A heap-ish list kept sorted
        # by (band, seq) so the oldest highest-priority runnable job is easy to find.
        self._pending: list[tuple[int, int, str, str]] = []
        self._seq = 0
        self._inflight_global = 0
        self._inflight_lane: dict[str, int] = {}
        self._cond = asyncio.Condition()   # workers wait here for a runnable job / freed capacity
        # Strong refs to in-flight _notify() tasks. asyncio keeps only a WEAK ref to a bare
        # create_task/ensure_future result, so without this the wakeup task can be GC'd before
        # it runs → a job added while every worker is parked never wakes anyone.
        self._notify_tasks: set[asyncio.Task] = set()

    # ─── enqueue ──────────────────────────────────────────────────────────────
    def add(self, job_id: str, lane: str, band: int) -> None:
        """Register a pending job and wake a worker to reconsider. Always invoked on the loop
        thread (submit() bounces here via call_soon_threadsafe), so notifying the condition's
        waiters via call_soon is safe and lock-free from the caller's view."""
        self._seq += 1
        self._pending.append((band, self._seq, job_id, lane))
        self._wake()

    def _wake(self) -> None:
        """Wake condition waiters. Scheduling a coroutine that acquires the lock avoids
        needing to hold it in the synchronous add() path. The task is retained in
        `_notify_tasks` (and discarded when done) so it can't be GC'd before it fires."""
        task = asyncio.ensure_future(self._notify())
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    # ─── worker side: get the next job this worker may start ──────────────────
    async def next_runnable(self) -> tuple[str, str]:
        """Block until a pending job can start (its lane has spare width AND the global cap
        has room), then claim its capacity and return `(jobId, lane)`. The caller MUST later
        release the SAME lane via `finish(lane)` (or release/reacquire around a suspension) —
        the lane is returned here, authoritatively, so the worker never has to re-derive it
        (re-deriving it from the job file leaks capacity when the read/claim fails)."""
        async with self._cond:
            while True:
                pick = self._pick_locked()
                if pick is not None:
                    band, seq, job_id, lane = pick
                    self._pending.remove(pick)
                    self._acquire(lane)
                    return job_id, lane
                await self._cond.wait()

    def _pick_locked(self):
        """The first pending job (by band, then seq) whose lane has width and the global cap
        has room. Returns the pending tuple or None."""
        if self._inflight_global >= MAX_CONCURRENCY:
            return None
        for entry in sorted(self._pending):  # (band, seq, …) → lowest band, oldest first
            _band, _seq, _job, lane = entry
            if self._inflight_lane.get(lane, 0) < lane_width(lane):
                return entry
        return None

    # ─── capacity accounting ──────────────────────────────────────────────────
    def _acquire(self, lane: str) -> None:
        self._inflight_global += 1
        self._inflight_lane[lane] = self._inflight_lane.get(lane, 0) + 1

    def _release(self, lane: str) -> None:
        self._inflight_global = max(0, self._inflight_global - 1)
        self._inflight_lane[lane] = max(0, self._inflight_lane.get(lane, 0) - 1)

    async def finish(self, lane: str) -> None:
        """A job fully finished — free its capacity and wake waiters."""
        async with self._cond:
            self._release(lane)
            self._cond.notify_all()

    async def release_for_wait(self, lane: str) -> None:
        """A running job is about to suspend (await a human/children) — give its slot back so
        other work runs while it waits. It will reacquire before continuing."""
        async with self._cond:
            self._release(lane)
            self._cond.notify_all()

    async def reacquire_after_wait(self, lane: str) -> None:
        """A suspended job resumed — take a slot back before it continues executing. Blocks if
        the lane/global is momentarily full (keeps the concurrency invariant honest)."""
        async with self._cond:
            while (self._inflight_global >= MAX_CONCURRENCY
                   or self._inflight_lane.get(lane, 0) >= lane_width(lane)):
                await self._cond.wait()
            self._acquire(lane)

    def stats(self) -> dict:
        return {"pending": len(self._pending), "inflightGlobal": self._inflight_global,
                "inflightLane": dict(self._inflight_lane)}


scheduler = LaneScheduler()
