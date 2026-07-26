"""The triage_post tool: post an actionable todo to the pool and mark it 'working'."""
from __future__ import annotations

import pytest

from curry_leaves_assistant.agents.triage_tools import TriagePostTool
from curry_leaves_assistant.stores import data, pool_store


async def _run(tool, **kw):
    return await tool.run(tool.Args(**kw), ctx=None, signal=None)


@pytest.mark.asyncio
async def test_triage_post_creates_user_pool_item_and_marks_working():
    todo = data.create_todo("Research the top 3 note-taking apps")
    tool = TriagePostTool()

    res = await _run(tool, todoId=todo["id"], title="Research note-taking apps",
                     description="Compare the top 3 and summarize.")
    assert not res.is_error

    items = pool_store.list_items()
    assert len(items) == 1
    item = items[0]
    assert item["source"] == "user"          # so the Lead picks it up for triage
    assert item["todoId"] == todo["id"]       # back-reference threaded through
    assert item["status"] == "waiting"        # NOT assigned — the Lead routes it

    stored = next(x for x in data.list_todos() if x["id"] == todo["id"])
    assert stored["assistantStatus"] == "working"
    assert stored["assistantPoolItemId"] == item["id"]


@pytest.mark.asyncio
async def test_triage_post_unknown_todo_errors_and_posts_nothing():
    tool = TriagePostTool()
    res = await _run(tool, todoId="nope", title="X")
    assert res.is_error
    assert pool_store.list_items() == []


@pytest.mark.asyncio
async def test_triage_post_does_not_enqueue_a_job():
    """triage_post only posts to the pool (waking the Lead); it must not enqueue a WorkItem."""
    from curry_leaves_assistant.core import paths
    todo = data.create_todo("Research something")
    tool = TriagePostTool()
    await _run(tool, todoId=todo["id"], title="Research")

    queued = list(paths.QUEUE_DIR.glob("*.json")) if paths.QUEUE_DIR.exists() else []
    assert queued == []  # no job — routing is the Lead's job, off the emitted pool.item.created
