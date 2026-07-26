"""Todo assistant-lifecycle fields + the attach_assistant_result write-back helper."""
from __future__ import annotations

from curry_leaves_assistant.stores import data


def test_create_todo_inits_assistant_fields():
    t = data.create_todo("Research note-taking apps")
    assert t["assistantStatus"] is None
    assert t["assistantResult"] is None
    assert t["assistantSessionId"] is None
    assert t["assistantPoolItemId"] is None


def test_attach_assistant_result_working_then_review():
    t = data.create_todo("Research note-taking apps")
    tid = t["id"]

    working = data.attach_assistant_result(tid, status="working", pool_item_id="pool-1")
    assert working is not None
    assert working["assistantStatus"] == "working"
    assert working["assistantPoolItemId"] == "pool-1"
    assert working["assistantResult"] is None  # not set on the working transition

    review = data.attach_assistant_result(tid, status="review", result="Top 3: A, B, C",
                                          session_id="run_job1")
    assert review is not None
    assert review["assistantStatus"] == "review"
    assert review["assistantResult"] == "Top 3: A, B, C"
    assert review["assistantSessionId"] == "run_job1"
    assert review["assistantPoolItemId"] == "pool-1"  # preserved across the second call

    # It persists.
    stored = next(x for x in data.list_todos() if x["id"] == tid)
    assert stored["assistantStatus"] == "review"


def test_attach_assistant_result_emits_todo_updated_not_created(events_sink):
    t = data.create_todo("Research note-taking apps")
    events_sink.clear()  # drop the todo.created from creation
    data.attach_assistant_result(t["id"], status="working", pool_item_id="pool-1")
    types = [e[0] for e in events_sink]
    assert types == ["todo.updated"]
    assert "todo.created" not in types  # must never re-wake Todo Triage → no loop
    assert "todo.completed" not in types


def test_attach_assistant_result_unknown_todo_returns_none():
    assert data.attach_assistant_result("nope", status="working") is None
