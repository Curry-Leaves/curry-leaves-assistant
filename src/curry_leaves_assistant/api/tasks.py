"""The common pool, todos, and reminders."""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from curry_leaves_assistant.orchestration import dispatch
from curry_leaves_assistant.stores import data, pool_store

router = APIRouter(tags=["tasks"])


class PoolItem(BaseModel):
    title: str = ""          # optional — the describe box sends only `description`
    description: str = ""
    tags: list[str] = []
    priority: str = "P2"
    autonomy: str = "auto"   # 'auto' (approve-all) | 'ask' (pause for approval) — poster's choice


@router.get("/pool")
def pool_list():
    return pool_store.list_items()


@router.post("/pool")
def pool_create(body: PoolItem):
    # A human drop from CMD+K / the Assistants page: source='user' (the default) so the Lead
    # picks it up for triage. The describe box may send only a description — derive a title
    # from its first line so the pool card and the "done" notification have a label.
    title = body.title.strip() or _first_line(body.description)
    return pool_store.create(title, body.description, body.tags, body.priority,
                             source="user", autonomy=body.autonomy)


def _first_line(text: str, limit: int = 80) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else "Untitled task"
    return line[:limit].strip() or "Untitled task"


@router.post("/pool/{item_id}/assign")
async def pool_assign(item_id: str, request: Request):
    body = await request.json()
    item = dispatch.assign_pool_item(item_id, body.get("agentId", ""))
    return item if item is not None else Response(status_code=404)


@router.post("/pool/{item_id}/done")
async def pool_done(item_id: str, request: Request):
    body = await request.json()
    return pool_store.complete(item_id, result=body.get("result", ""), by=body.get("by")) or Response(status_code=404)


@router.delete("/pool/{item_id}")
def pool_delete(item_id: str):
    return {"ok": pool_store.delete(item_id)}


# ─── Todos / reminders ────────────────────────────────────────────────────────
class CreateTodo(BaseModel):
    text: str
    priority: str | None = None
    dueDate: str | None = None


@router.get("/todos")
def get_todos():
    return data.list_todos()


@router.post("/todos")
def post_todo(body: CreateTodo):
    return data.create_todo(body.text, priority=body.priority, due_date=body.dueDate)


@router.patch("/todos/{todo_id}")
async def patch_todo(todo_id: str, request: Request):
    body = await request.json()
    return data.update_todo(todo_id, body) or Response(status_code=404)


@router.delete("/todos/{todo_id}")
def delete_todo(todo_id: str):
    return {"ok": data.delete_todo(todo_id)}


class CreateReminder(BaseModel):
    title: str
    dueAt: str
    notes: str | None = None


@router.get("/reminders")
def get_reminders():
    return data.list_reminders()


@router.post("/reminders")
def post_reminder(body: CreateReminder):
    return data.create_reminder(body.title, due_at=body.dueAt, notes=body.notes)


@router.patch("/reminders/{reminder_id}")
async def patch_reminder(reminder_id: str, request: Request):
    body = await request.json()
    return data.update_reminder(reminder_id, body) or Response(status_code=404)


@router.delete("/reminders/{reminder_id}")
def delete_reminder(reminder_id: str):
    return {"ok": data.delete_reminder(reminder_id)}
