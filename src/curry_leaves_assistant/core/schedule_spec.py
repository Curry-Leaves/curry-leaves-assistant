"""ScheduleSpec — the shared, dependency-free vocabulary for "when does this fire".

A tagged union (dict with a `kind`): `none` | `at` | `every` | `cron`. The scheduler
(orchestration.schedule) computes fire times from it; agents, tiles, and reminders all carry
one. This module holds only the *shape* and the pure translation from the human
frequency/time/day vocabulary — no timing, no croniter, no I/O — so it can live in `core` and
be imported from any layer (the natural-language authoring paths in `agents`/`api` need the
translator, and the layer rules forbid them importing `orchestration`).
"""
from __future__ import annotations

Spec = dict  # {"kind": "none"|"at"|"every"|"cron", ...}


def spec_none() -> Spec:
    return {"kind": "none"}


def cron_from_frequency(frequency: str, time: str, day_of_week: int | None = None) -> Spec | None:
    """Translate the human `frequency`/`time`/`day_of_week` vocabulary (used by the
    natural-language tile/agent authoring paths) into a `cron` ScheduleSpec — the ONE place
    that day-of-week convention lives, so agents and tiles can't drift apart. `day_of_week`
    uses cron's own convention (0=Sunday .. 6=Saturday). Returns None on invalid input."""
    try:
        hh, mm = time.split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    if frequency == "daily":
        return {"kind": "cron", "expr": f"{m} {h} * * *"}
    if frequency == "weekdays":
        return {"kind": "cron", "expr": f"{m} {h} * * 1-5"}
    if frequency == "weekly":
        if day_of_week is None or not (0 <= day_of_week <= 6):
            return None
        return {"kind": "cron", "expr": f"{m} {h} * * {day_of_week}"}
    return None
