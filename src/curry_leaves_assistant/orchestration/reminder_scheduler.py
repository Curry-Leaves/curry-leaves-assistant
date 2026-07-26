"""Reminders as a ScheduleSource: fire `reminder.due` once each reminder's time arrives.

Reminders are plain data (see stores/data.py) with no watcher of their own. This registers
them with the unified scheduler (orchestration.schedule): each un-fired reminder becomes a
one-shot `at` job derived from its `dueAt`. The scheduler's persisted next-run state is the
once-per-reminder guard; we still stamp `alertedAt` on fire because the store and UI read it
(a reopened / rescheduled reminder clears it, which naturally re-arms the job).
"""
from __future__ import annotations

from datetime import datetime, timezone

from curry_leaves_assistant.core import events
from curry_leaves_assistant.orchestration import schedule
from curry_leaves_assistant.orchestration.schedule import ScheduledJob
from curry_leaves_assistant.stores import data


def _due_ms(reminder: dict) -> int | None:
    """The reminder's dueAt as epoch-ms, or None if unset/unparseable."""
    due_at = reminder.get("dueAt")
    if not due_at:
        return None
    try:
        due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return int(due.timestamp() * 1000)


def _fire(reminder_id: str) -> None:
    """Emit reminder.due and mark it alerted. Re-reads the reminder so a title/notes edit
    between arming and firing is reflected in the event payload."""
    r = next((x for x in data.list_reminders() if x["id"] == reminder_id), None)
    if r is None or r.get("done") or r.get("alertedAt"):
        return
    data.mark_reminder_alerted(reminder_id)
    events.emit("reminder.due", payload=r, entity_id=r["id"], label=r["title"])


class _ReminderSource:
    name = "reminders"

    def jobs(self) -> list[ScheduledJob]:
        out: list[ScheduledJob] = []
        for r in data.list_reminders():
            if r.get("done") or r.get("alertedAt"):
                continue  # already handled → not schedulable
            at_ms = _due_ms(r)
            if at_ms is None:
                continue
            out.append(ScheduledJob(
                key=f"reminder:{r['id']}",
                spec={"kind": "at", "at_ms": at_ms},
                fire=(lambda rid=r["id"]: _fire(rid)),
            ))
        return out


def start() -> None:
    """Register the reminder source with the unified scheduler. Called once from the app
    lifespan; the scheduler owns the tick loop."""
    schedule.register(_ReminderSource())
