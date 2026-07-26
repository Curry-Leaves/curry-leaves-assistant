"""Pool item todoId back-reference + the todo write-back on completion."""
from __future__ import annotations

from curry_leaves_assistant.stores import data, pool_store


def test_create_carries_todo_id():
    item = pool_store.create("Research apps", "brief", todo_id="todo-1")
    assert item["todoId"] == "todo-1"
    # Default (no todo) stays None.
    assert pool_store.create("Some task")["todoId"] is None


def test_complete_writes_back_to_todo():
    todo = data.create_todo("Research note-taking apps")
    item = pool_store.create("Research apps", "brief", todo_id=todo["id"])
    data.attach_assistant_result(todo["id"], status="working", pool_item_id=item["id"])

    pool_store.complete(item["id"], result="Top 3: A, B, C", by="researcher",
                        session_id="run_job42")

    stored = next(x for x in data.list_todos() if x["id"] == todo["id"])
    assert stored["assistantStatus"] == "review"
    assert stored["assistantResult"] == "Top 3: A, B, C"
    assert stored["assistantSessionId"] == "run_job42"
    assert stored["assistantPoolItemId"] == item["id"]
    assert stored["done"] is False  # review, NOT auto-done


def test_complete_without_todo_id_leaves_todos_untouched():
    todo = data.create_todo("A todo the pool item is NOT linked to")
    item = pool_store.create("Unrelated task")  # no todo_id
    pool_store.complete(item["id"], result="done", by="someone", session_id="run_x")

    stored = next(x for x in data.list_todos() if x["id"] == todo["id"])
    assert stored["assistantStatus"] is None
    assert stored["assistantResult"] is None
