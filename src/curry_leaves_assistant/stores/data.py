"""Todos and reminders — flat JSON lists under ~/.curry-leaves, each mutation emits an event.

Agent-created todos therefore feed the event loop just like user-created ones.
"""
from __future__ import annotations

import uuid

from curry_leaves_assistant.core import events

from curry_leaves_assistant.core.paths import TODOS_PATH, REMINDERS_PATH
from curry_leaves_assistant.core.store import read_json, write_json, now_iso


def _norm(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def _same_source(a: dict | None, b: dict | None) -> bool:
    return bool(a and b and a.get("id") and a.get("id") == b.get("id"))


# ─── Todos ────────────────────────────────────────────────────────────────────
def list_todos() -> list[dict]:
    return read_json(TODOS_PATH, [])


def create_todo(text: str, *, priority: str | None = None, due_date: str | None = None,
                source: dict | None = None) -> dict:
    todos = list_todos()
    # Idempotent: re-running an agent on the same recording must not duplicate items.
    if source:
        for t in todos:
            if _same_source(t.get("source"), source) and _norm(t.get("text")) == _norm(text):
                return t
    todo = {
        "id": uuid.uuid4().hex,
        "text": text,
        "done": False,
        "priority": priority,
        "dueDate": due_date,
        "source": source,            # e.g. {"type": "recording", "id": "..."}
        "createdAt": now_iso(),
        # Proactive-assistant lifecycle: the Todo Triage agent may post an actionable todo to
        # the pool for the team to attempt. None → untouched; 'working' → a run is in flight;
        # 'review' → done, result + conversation attached below for the user to inspect.
        "assistantStatus": None,     # None | 'working' | 'review'
        "assistantResult": None,     # summary of what the team did
        "assistantSessionId": None,  # run_<jobId> — open the conversation to continue it
        "assistantPoolItemId": None, # the pool item this todo was posted as
    }
    todos.append(todo)
    write_json(TODOS_PATH, todos)
    events.emit("todo.created", payload=todo, entity_id=todo["id"], label=text)
    return todo


_TODO_FIELDS = ("text", "priority", "dueDate", "done")


def update_todo(todo_id: str, patch: dict) -> dict | None:
    todos = list_todos()
    for t in todos:
        if t["id"] == todo_id:
            if "done" in patch:
                t["completedAt"] = now_iso() if patch["done"] else None
            for k in _TODO_FIELDS:
                if k in patch:
                    t[k] = patch[k]
            write_json(TODOS_PATH, todos)
            evt = "todo.completed" if patch.get("done") else "todo.updated"
            events.emit(evt, payload=t, entity_id=todo_id, label=t["text"])
            return t
    return None


def attach_assistant_result(todo_id: str, *, status: str, result: str | None = None,
                            session_id: str | None = None, pool_item_id: str | None = None) -> dict | None:
    """Write proactive-assistant progress back onto a todo and emit `todo.updated`.

    Only the provided fields are touched (a 'working' transition sets just status + pool item;
    the completing 'review' transition adds the result summary + session). Kept OUT of
    `update_todo` / `_TODO_FIELDS` on purpose: these fields must not be settable via the generic
    PATCH /todos endpoint. Emits `todo.updated` (never `todo.created`), so it can't re-wake the
    Todo Triage agent — no loop."""
    todos = list_todos()
    for t in todos:
        if t["id"] == todo_id:
            t["assistantStatus"] = status
            if result is not None:
                t["assistantResult"] = result
            if session_id is not None:
                t["assistantSessionId"] = session_id
            if pool_item_id is not None:
                t["assistantPoolItemId"] = pool_item_id
            write_json(TODOS_PATH, todos)
            events.emit("todo.updated", payload=t, entity_id=todo_id, label=t["text"])
            return t
    return None


def delete_todo(todo_id: str) -> bool:
    todos = list_todos()
    kept = [t for t in todos if t["id"] != todo_id]
    if len(kept) == len(todos):
        return False
    write_json(TODOS_PATH, kept)
    events.emit("todo.deleted", entity_id=todo_id)
    return True


# ─── Reminders ────────────────────────────────────────────────────────────────
def list_reminders() -> list[dict]:
    return read_json(REMINDERS_PATH, [])


def create_reminder(title: str, *, due_at: str, notes: str | None = None,
                    source: dict | None = None) -> dict:
    reminders = list_reminders()
    if source:
        for r in reminders:
            if _same_source(r.get("source"), source) and _norm(r.get("title")) == _norm(title):
                return r
    reminder = {
        "id": uuid.uuid4().hex,
        "title": title,
        "notes": notes,
        "dueAt": due_at,
        "done": False,
        "alertedAt": None,
        "source": source,
        "createdAt": now_iso(),
    }
    reminders.append(reminder)
    write_json(REMINDERS_PATH, reminders)
    events.emit("reminder.created", payload=reminder, entity_id=reminder["id"], label=title)
    return reminder


_REMINDER_FIELDS = ("title", "notes", "dueAt", "done")


def update_reminder(reminder_id: str, patch: dict) -> dict | None:
    reminders = list_reminders()
    for r in reminders:
        if r["id"] == reminder_id:
            if "done" in patch:
                r["completedAt"] = now_iso() if patch["done"] else None
            for k in _REMINDER_FIELDS:
                if k in patch:
                    r[k] = patch[k]
            # Pushing the due time out (or reopening a done reminder) should re-arm
            # the due-alert so the scheduler fires again instead of staying silent.
            if "dueAt" in patch or ("done" in patch and not patch["done"]):
                r["alertedAt"] = None
            write_json(REMINDERS_PATH, reminders)
            events.emit("reminder.updated", payload=r, entity_id=reminder_id, label=r["title"])
            return r
    return None


def mark_reminder_alerted(reminder_id: str) -> dict | None:
    """Stamp a reminder as alerted so the due-reminder scheduler doesn't re-fire it."""
    reminders = list_reminders()
    for r in reminders:
        if r["id"] == reminder_id:
            r["alertedAt"] = now_iso()
            write_json(REMINDERS_PATH, reminders)
            return r
    return None


def delete_reminder(reminder_id: str) -> bool:
    reminders = list_reminders()
    kept = [r for r in reminders if r["id"] != reminder_id]
    if len(kept) == len(reminders):
        return False
    write_json(REMINDERS_PATH, kept)
    events.emit("reminder.deleted", entity_id=reminder_id)
    return True
