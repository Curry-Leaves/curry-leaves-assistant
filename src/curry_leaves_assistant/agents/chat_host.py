"""SSEChatHost — bridges curry-leaves's Host seam to the chat SSE stream.

curry-leaves talks to a frontend through a Host: `emit(event)` (notify) and
`await request(req)` (interact — ask the user, approve a tool). Chat is delivered
over a one-way SSE stream, so we turn each interactive request into an outbound
SSE event and park the awaiting coroutine on a Future until the browser POSTs the
answer to `/chat/respond`, which resolves it. The agent then continues.

Both normal stream events and these requests flow through one `outbox` queue, so
the SSE generator can forward an approve/ask prompt even while the agent loop is
blocked waiting for the answer.
"""
from __future__ import annotations

import asyncio

from curry_leaves.core.host import AskUser, ApproveTool

from curry_leaves_assistant.core.textfmt import args_text as _arg_text, result_text as _res_text


def _key(call_id: str | None, depth: int) -> str:
    """The stable UI key for a tool call at `depth` (0 = the parent agent's own stream).

    Depth 0 keys are the raw tool_call_id, because that's what the parent's own
    tool_start/tool_end frames already use — a depth-1 step's `parent` has to match those
    ids for the UI to nest it under the right card. Deeper calls get a depth prefix so ids
    from two different subagents can't collide (each child numbers its calls from scratch).
    """
    if not call_id:
        return ""
    return call_id if depth <= 0 else f"s{depth}:{call_id}"


def _subagent_items(ev) -> list[dict]:
    """Translate a SubagentActivity into chat `sub` items, so the UI can show what
    delegated subagents are doing — nested under the tool call that delegated the work.

    Each item carries `parent` (the delegating call) and `agent` (which subagent produced
    it), so the frontend can hang these under the right card instead of listing them in a
    flat side panel. We surface tool calls and thinking — the useful signal — and skip raw
    reply text: the subagent's final answer already comes back as the parent's tool-result
    card, so streaming it here would duplicate it.

    `id` and `parent` share ONE scheme so the tree chains at any depth: a step's id is its
    parent's key + its own call id. A subagent's tool call can itself be a delegation, and
    the grandchild's `parent` is that call's raw id — which must equal the child step's
    `id`, or the grandchild can never be linked to it. (Raw ids alone won't do: they're
    only unique within one child's stream, so two subagents in the same turn collide.)"""
    from curry_leaves.core.events import SubagentActivity
    if not isinstance(ev, SubagentActivity):
        return []
    inner, depth = ev.event, ev.depth
    t = getattr(inner, "type", None)
    parent_raw = getattr(ev, "parent_tool_call_id", None)
    parent = _key(parent_raw, depth - 1)
    # This step's own key, in the SAME scheme — so if it delegates onward, its children's
    # `parent` (computed as _key(their raw parent, depth)) resolves back to exactly this.
    cid = _key(getattr(inner, "tool_call_id", ""), depth)
    base = {"type": "sub", "depth": depth, "parent": parent, "agent": ev.name}
    tool_name = getattr(inner, "tool_name", "")
    if t == "tool_start":
        if tool_name in ("ask", "approve"):
            return []
        return [{**base, "kind": "tool_start", "id": cid,
                 "name": tool_name, "input": _arg_text(getattr(inner, "args", None))}]
    if t == "tool_end":
        if tool_name in ("ask", "approve"):
            return []
        res = getattr(inner, "result", None)
        return [{**base, "kind": "tool_end", "id": cid,
                 "name": tool_name, "output": _res_text(res),
                 "isError": bool(getattr(res, "is_error", False))}]
    if t == "message_update":
        d = getattr(inner, "delta", None)
        if d is not None and getattr(d, "kind", None) == "thinking":
            return [{**base, "kind": "thinking", "text": getattr(d, "value", "")}]
        return []
    if t == "agent_end":
        return [{**base, "kind": "end"}]
    return []


class SSEChatHost:
    def __init__(self, *, job_id: str | None = None, agent: dict | None = None,
                 announce: bool = False, lane: str | None = None,
                 pool_item_id: str | None = None) -> None:
        self.outbox: asyncio.Queue = asyncio.Queue()
        self._futures: dict[str, asyncio.Future] = {}
        self._seq = 0
        self._subscribers: list = []
        # Background runs (workers/tiles) set announce=True so an ask/approve also fires a
        # global `agent.run.needs_input` event — the app-wide signal that drives the "waiting
        # on you" notification and walks the agent to Your desk in the office. Live chat leaves
        # this off: its ask card is already on-screen, so a global alert would be redundant.
        # For announced runs the request is also persisted (queue/<jobId>.pending.json →
        # GET /pending-inputs) so the question survives reloads and can be answered whenever;
        # `lane` lets the wait release its scheduler slot (an unanswered question must not
        # hold a worker), and `pool_item_id` pins the question to the pool ask it is about.
        self._job_id = job_id
        self._agent = agent or {}
        self._announce = announce and bool(job_id)
        self._lane = lane
        self._pool_item_id = pool_item_id

    def subscribe(self, fn) -> callable:
        """Allow the session store to observe all events via runner.subscribe()."""
        self._subscribers.append(fn)
        def off():
            try:
                self._subscribers.remove(fn)
            except ValueError:
                pass
        return off

    # Top-level chat events come from runner.stream(); the notify channel carries only
    # subagent activity (SubagentActivity), which we forward so delegation is visible.
    def emit(self, event) -> None:
        for fn in self._subscribers:
            try:
                fn(event)
            except Exception:
                pass
        for item in _subagent_items(event):
            self.put(item)

    def put(self, item: dict) -> None:
        self.outbox.put_nowait(item)

    async def request(self, req):
        """Surface an ask/approve prompt and block until the browser answers. Announced
        (background) runs persist the request durably and give their scheduler slot back
        while parked — the wait is unbounded, so it must cost neither capacity nor be
        forgettable across a reload."""
        self._seq += 1
        rid = f"q{self._seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._futures[rid] = fut
        if isinstance(req, AskUser):
            frame = {"type": "ask", "id": rid, "question": req.question, "options": list(req.options)}
        elif isinstance(req, ApproveTool):
            frame = {"type": "approve", "id": rid, "tool": req.tool, "args": req.args,
                     "risk": req.risk, "reason": getattr(req, "reason", "")}
        else:
            return req.default
        self.put(frame)
        if self._announce:
            from curry_leaves_assistant.core.pending import persist
            persist(self._job_id or "", rid, self._agent, frame, self._pool_item_id)
        self._announce_needs_input(rid, frame["type"])
        lane = self._lane if self._announce else None
        if lane:
            from curry_leaves_assistant.orchestration.scheduler import scheduler
            await scheduler.release_for_wait(lane)
        try:
            return await fut
        finally:
            self._futures.pop(rid, None)
            if lane:
                from curry_leaves_assistant.orchestration.scheduler import scheduler
                await scheduler.reacquire_after_wait(lane)
            if self._announce:
                from curry_leaves_assistant.core.pending import clear
                clear(self._job_id or "")

    def resolve(self, request_id: str, answer) -> bool:
        fut = self._futures.get(request_id)
        if fut and not fut.done():
            fut.set_result(answer)
            self._announce_input_received(request_id)
            return True
        return False

    # ─── background-run "waiting on you" signalling ───────────────────────────
    def _announce_needs_input(self, request_id: str, kind: str) -> None:
        if not self._announce:
            return
        from curry_leaves_assistant.core import events
        name = self._agent.get("name", "An assistant")
        events.emit("agent.run.needs_input",
                    payload={"jobId": self._job_id, "agentId": self._agent.get("id"),
                             "requestId": request_id, "kind": kind},
                    entity_id=self._agent.get("id"),
                    label=f"{name} needs input")

    def _announce_input_received(self, request_id: str) -> None:
        if not self._announce:
            return
        from curry_leaves_assistant.core import events
        events.emit("agent.run.input_received",
                    payload={"jobId": self._job_id, "agentId": self._agent.get("id"),
                             "requestId": request_id},
                    entity_id=self._agent.get("id"),
                    label=f"{self._agent.get('name', 'An assistant')} resumed")
