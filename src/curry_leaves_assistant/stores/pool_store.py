"""Common pool — work items awaiting assignment to an agent.

Each item is a small JSON file under ~/.curry-leaves/pool/. A human (or an agent) posts
work; it sits as ``waiting`` until assigned to an agent, which enqueues a real job
in the agent pool. When that job finishes the item is marked ``done``.
"""
from __future__ import annotations

import uuid

from curry_leaves_assistant.core import events

from curry_leaves_assistant.core.paths import POOL_DIR
from curry_leaves_assistant.core.store import read_json, write_json, now_iso


def _path(item_id: str):
    return POOL_DIR / f"{item_id}.json"


def list_items() -> list[dict]:
    out = []
    if POOL_DIR.exists():
        for p in POOL_DIR.glob("*.json"):
            it = read_json(p, None)
            if it:
                out.append(it)
    # waiting first, then assigned, then done; newest within each group
    order = {"waiting": 0, "assigned": 1, "done": 2}
    out.sort(key=lambda i: i.get("createdAt") or "", reverse=True)
    out.sort(key=lambda i: order.get(i.get("status"), 9))
    return out


def get(item_id: str) -> dict | None:
    return read_json(_path(item_id), None)


def create(title: str, description: str = "", tags: list[str] | None = None, priority: str = "P2",
           source: str = "user", autonomy: str = "auto", todo_id: str | None = None) -> dict:
    """`source` marks WHO posted the item: 'user' = a human drop (via CMD+K / the Assistants
    page) that the Lead should triage and assign; 'orchestrate' = an item an agent created as a
    side effect of delegating (orchestrate/assign), already targeted at a specific agent, which
    the Lead MUST ignore or it would double-handle already-dispatched work.

    `autonomy` is how the assigned run should treat approvals: 'auto' (approve-all, runs
    headlessly — the default) or 'ask' (pause and ask the user before risky tool use). It's
    a per-ask preference the poster picks; dispatch threads it onto the WorkItem."""
    item = {
        "id": uuid.uuid4().hex,
        "title": title,
        "description": description,
        "tags": tags or [],
        "priority": priority if priority in ("P1", "P2", "P3") else "P2",
        "autonomy": autonomy if autonomy in ("auto", "ask") else "auto",
        "status": "waiting",      # waiting | assigned | done
        "source": source if source in ("user", "orchestrate") else "user",
        "assignedAgent": None,
        "result": None,
        "createdAt": now_iso(),
        # Back-reference to the todo this item was posted from, when it came from the Todo
        # Triage agent. On completion the result + conversation are written back onto it.
        "todoId": todo_id,
    }
    write_json(_path(item["id"]), item)
    events.emit("pool.item.created", payload=item, entity_id=item["id"], label=title)
    return item


def assign(item_id: str, agent_id: str) -> dict | None:
    """Mark a pool item assigned to an agent and return it with a `_job` field: the
    trigger the caller should hand to the pool (this store can't reach up into the
    orchestration layer, so enqueuing is the caller's job — see api/tasks.py)."""
    item = get(item_id)
    if item is None:
        return None
    item["status"] = "assigned"
    item["assignedAgent"] = agent_id
    write_json(_path(item_id), item)
    events.emit("pool.item.assigned", payload={"id": item_id, "agentId": agent_id, "title": item["title"]},
                entity_id=item_id, label=item["title"])
    # The job for the agent pool to run (closes itself out on completion). The caller
    # enqueues it; keeping the enqueue out of the store preserves the layer boundary.
    item["_job"] = {
        "agentId": agent_id,
        "trigger": {
            "type": "pool.assigned", "occurredAt": now_iso(),
            "payload": {"poolItemId": item_id, "title": item["title"],
                        "description": item["description"], "tags": item["tags"]},
        },
    }
    return item


def complete(item_id: str, result: str = "", by: str | None = None,
             session_id: str | None = None) -> dict | None:
    item = get(item_id)
    if item is None:
        return None
    item["status"] = "done"
    item["result"] = result
    if by:
        item["assignedAgent"] = by
    write_json(_path(item_id), item)
    events.emit("pool.item.done", payload={"id": item_id, "by": by, "title": item["title"]},
                entity_id=item_id, label=item["title"])
    # If this item was posted from a todo (Todo Triage → pool → team), write the result and the
    # run's conversation back onto that todo and move it to 'review'. `data` is a sibling store
    # (same layer — the import is allowed). Fail-soft: a todo hiccup must never break completion.
    if item.get("todoId"):
        try:
            from curry_leaves_assistant.stores import data
            data.attach_assistant_result(item["todoId"], status="review", result=result,
                                         session_id=session_id, pool_item_id=item_id)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[pool] todo write-back failed for {item_id}: {exc}", flush=True)
    return item


def delete(item_id: str) -> bool:
    p = _path(item_id)
    if p.exists():
        p.unlink()
        return True
    return False
