"""The unified scheduler — one clock for every timed trigger in the app.

Replaces three separate 20s polling loops (agents on a schedule, dashboard tiles, due
reminders) with a single tick-driven kernel modeled.

  • One `ScheduleSpec` tagged union — `none` | `at` | `every` | `cron` — describes *when*.
  • Each timed subsystem registers a `ScheduleSource`: it enumerates its schedulable jobs
    and knows how to `fire` one. Sources decide *what happens* (submit a WorkItem, emit an
    event); the kernel owns *when it happens*.
  • Due-ness is a timestamp compare: every job carries a computed `next_run_at_ms`; the tick
    fires those with `next_run_at_ms <= now` and recomputes the next one forward.
  • The loop sleeps until the soonest job (floored 1s, capped 60s), not a fixed 20s spin, so
    idle periods are cheap and clock jumps / new jobs are still picked up within a minute.
  • `next_run_at_ms` is persisted (schedule-state.json), so a job whose window passed while
    the process was down fires once on the next boot (catch-up), then advances forward.

The kernel never runs a job itself — `fire()` calls the same `work.submit()` / `events.emit()`
the subsystems always called, so durability, lanes, dead-letter, tracing, and the loop guard
stay entirely in the Work Kernel below.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from croniter import croniter

from curry_leaves_assistant.core.paths import DATA_DIR
from curry_leaves_assistant.core.schedule_spec import Spec, cron_from_frequency  # re-exported
from curry_leaves_assistant.core.store import read_json, write_json

__all__ = ["compute_next_run", "cron_from_frequency", "is_recurring", "register",
           "start", "stop", "ScheduledJob", "Spec"]

# ─── time helpers ─────────────────────────────────────────────────────────────
# All scheduling math is in epoch-milliseconds (int). Wall-clock local
# time is only used to interpret cron expressions (which are inherently local-time).


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


# ─── the schedule spec ────────────────────────────────────────────────────────
# A tagged union. `kind` selects the shape:
#   none  — never fires (a job with no schedule).
#   at    — one-shot; fires once at `at_ms`, then retires (next_run → None).
#   every — recurring fixed interval `every_ms`, phase-anchored at `anchor_ms` (default: the
#           first computation time) so runs land on a stable cadence, not drifting from boot.
#   cron  — recurring 5-field cron `expr` interpreted in local time.
# The Spec shape + spec_none()/cron_from_frequency() live in core.schedule_spec (dependency-
# free, importable from every layer); this module adds the timing math on top.


def compute_next_run(spec: Spec, now_ms: int, *, after_ms: int | None = None) -> int | None:
    """The next fire time strictly in the future, or None if the spec never fires again.

    `after_ms` (default now) is the instant we compute forward from — passing the previous
    `next_run` here would let a fixed interval land exactly on cadence, but we deliberately
    compute forward from *now* so a job that missed windows during downtime collapses to a
    single catch-up run rather than replaying every missed tick."""
    base = now_ms if after_ms is None else after_ms
    kind = (spec or {}).get("kind", "none")

    if kind == "none":
        return None

    if kind == "at":
        at_ms = spec.get("at_ms")
        if at_ms is None:
            return None
        # Future one-shot → fire then. Already past → still due (catch-up); the fire path
        # retires it, so returning it once is correct.
        return int(at_ms)

    if kind == "every":
        every = int(spec.get("every_ms") or 0)
        if every <= 0:
            return None
        anchor = int(spec.get("anchor_ms", base))
        if base < anchor:
            return anchor  # first fire lands on the anchor when we're still before it
        # Smallest anchor + n·every strictly after `base` (n≥1, so we always advance — a job
        # armed exactly at its own next_run computes the *following* occurrence, not itself).
        n = ((base - anchor) // every) + 1
        return anchor + n * every

    if kind == "cron":
        expr = spec.get("expr")
        if not expr:
            return None
        try:
            # croniter works in local wall-clock; our cron expressions are authored as local.
            base_local = datetime.fromtimestamp(base / 1000).astimezone()
            itr = croniter(expr, base_local)
            nxt = itr.get_next(datetime)
            return int(nxt.timestamp() * 1000)
        except Exception:
            return None

    return None


def is_recurring(spec: Spec) -> bool:
    return (spec or {}).get("kind") in ("every", "cron")


# ─── the source interface ─────────────────────────────────────────────────────
@dataclass
class ScheduledJob:
    """One schedulable thing a source knows about, as seen by the kernel each tick."""
    key: str            # stable, globally-unique id (source namespaces its own — see below)
    spec: Spec          # when it fires
    fire: Callable[[], object]  # what happens when it's due (submit()/emit()); return ignored


class ScheduleSource(Protocol):
    name: str
    def jobs(self) -> list[ScheduledJob]:
        """Enumerate this source's currently-schedulable jobs. Called every tick, so it must
        reflect live config (an agent whose schedule changed, a deleted tile drops out)."""
        ...


_sources: list[ScheduleSource] = []


def register(source: ScheduleSource) -> None:
    """Add a source. Idempotent by identity so a re-registered module (dev reload) doesn't
    double-fire."""
    if source not in _sources:
        _sources.append(source)


# ─── persisted next-run state ─────────────────────────────────────────────────
# key → {"next": int|None}. Persisting `next` is what makes catch-up correct: on boot we can
# tell "this window already passed, fire it once" from "not due yet". Kept small (one row per
# live schedule) and rewritten atomically via write_json.
_STATE_PATH = DATA_DIR / "schedule-state.json"
_state: dict[str, dict] = {}


def _load_state() -> None:
    global _state
    _state = read_json(_STATE_PATH, {}) or {}


def _save_state() -> None:
    write_json(_STATE_PATH, _state)


def _next_of(key: str) -> int | None:
    row = _state.get(key)
    return row.get("next") if row else None


def _set_next(key: str, next_ms: int | None) -> None:
    _state[key] = {"next": next_ms}


# ─── the tick loop ────────────────────────────────────────────────────────────
_MIN_SLEEP_S = 1.0     # floor — never busy-spin
_MAX_SLEEP_S = 60.0    # cap — re-check within a minute so clock jumps / new jobs are caught

_task: asyncio.Task | None = None


def _tick(now_ms: int) -> None:
    """One pass: fire every due job, advance its next-run, prune state for jobs that vanished."""
    live_keys: set[str] = set()
    dirty = False

    for src in _sources:
        try:
            jobs = src.jobs()
        except Exception as exc:  # a broken source must not kill the whole scheduler
            print(f"[schedule] source {getattr(src, 'name', src)!r} jobs() failed: {exc}", flush=True)
            continue

        for job in jobs:
            live_keys.add(job.key)
            nxt = _next_of(job.key)

            if nxt is None and job.key not in _state:
                # First time we've seen this job — arm it forward without firing. (A brand-new
                # schedule shouldn't retro-fire; only an armed-then-missed window catches up.)
                nxt = compute_next_run(job.spec, now_ms)
                _set_next(job.key, nxt)
                dirty = True
                continue

            if nxt is None:
                # Retired (a one-shot that already fired). Leave the tombstone so it can't
                # re-arm; pruned only when the source stops listing it.
                continue

            if nxt <= now_ms:
                try:
                    job.fire()
                except Exception as exc:
                    print(f"[schedule] fire {job.key!r} failed: {exc}", flush=True)
                # Advance. Recurring → next future occurrence; one-shot → retire (None).
                _set_next(job.key, compute_next_run(job.spec, now_ms) if is_recurring(job.spec) else None)
                dirty = True

    # Prune tombstones/rows for jobs no source lists any more (deleted agent, tile, reminder).
    for stale in [k for k in _state if k not in live_keys]:
        del _state[stale]
        dirty = True

    if dirty:
        _save_state()


def _sleep_for(now_ms: int) -> float:
    """Seconds until the soonest armed job, clamped to [_MIN_SLEEP_S, _MAX_SLEEP_S]."""
    upcoming = [row["next"] for row in _state.values() if row.get("next") is not None]
    if not upcoming:
        return _MAX_SLEEP_S
    delta_s = (min(upcoming) - now_ms) / 1000.0
    return max(_MIN_SLEEP_S, min(_MAX_SLEEP_S, delta_s))


async def _loop() -> None:
    while True:
        now = _now_ms()
        try:
            _tick(now)
        except Exception as exc:
            print(f"[schedule] tick failed: {exc}", flush=True)
        await asyncio.sleep(_sleep_for(_now_ms()))


def start() -> None:
    """Load persisted next-run state and launch the single tick loop. Sources should be
    registered (by their modules' own start()) before or right after this — jobs() is polled
    every tick, so registration order doesn't matter for correctness."""
    global _task
    _load_state()
    _task = asyncio.create_task(_loop())  # retained → not GC'd; cancellable
    print(f"[schedule] started ({len(_sources)} source(s))", flush=True)


async def stop() -> None:
    """Cancel the tick loop on shutdown, flushing state so next-run survives the restart."""
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    _save_state()
