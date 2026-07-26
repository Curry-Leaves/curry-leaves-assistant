"""Interactive chat runs — transport-free.

Owns the live-run registry and the run lifecycle (start / stop / steer / respond /
cancel-pending) independent of how a client talks to us. The WebSocket layer (api/ws.py)
calls these; frames stream back over the hub's ``chat:<runId>`` channel.

A run is a detached asyncio Task (``drive()``) plus a drainer task that forwards the
run's outbox to the hub. Because delivery is decoupled from any single request, a client
can disconnect and reattach mid-run (the hub buffers frames per run for replay) — the run
keeps going server-side and is only killed by an explicit ``stop_run``.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Literal

from pydantic import BaseModel

from curry_leaves_assistant.agents import agent_engine
from curry_leaves_assistant.agents.agent_engine import stream_agent
from curry_leaves_assistant.core import events
from curry_leaves_assistant.core import settings as app_settings
from curry_leaves_assistant.core import trace_ctx
from curry_leaves_assistant.core.textfmt import args_text as _args_text, result_text as _result_text
from curry_leaves_assistant.core.ws_hub import hub
from curry_leaves_assistant.providers.model_limits import context_limit
from curry_leaves_assistant.stores import agent_store, chat_sessions, trace_store


# ─── Request models (shared by api/ws.py's method dispatch) ───────────────────
class Reference(BaseModel):
    """One thing the user pointed at with @ in the composer — a HANDLE, not content. The
    turn's prompt gets `type · id · title` and the name of the tool that opens it; the agent
    spends a tool call on the body only if the question needs it. That keeps a two-hour
    transcript and a three-word todo the same ~20-token cost at the prompt."""
    type: Literal["recording", "todo", "reminder", "note", "file"]
    id: str      # recording id | todo id | reminder id | note path | absolute file path
    title: str   # what the user saw when they picked it


class ChatBody(BaseModel):
    agentId: str
    message: str
    sessionId: str | None = None
    model: str | None = None
    history: list[dict] = []     # ephemeral context (used by AI text edits)
    ephemeral: bool = False      # don't persist a session (one-off AI edits)
    attachments: list[str] = []  # mdPath ids of files attached to this turn
    references: list[Reference] = []  # @-mentions: handles to the user's own stuff
    thinkingEffort: str | None = "medium"  # "minimal" | "low" | "medium" | "high" | None (off)
    autonomous: bool = False     # skip approval prompts for this turn (auto-approve write/network tools)
    elide: bool = False          # reclaim context by stubbing stale tool results (see curry_leaves.elision)
    # Where this turn came from. "voice" gets a real session (so gated tools have a host to
    # prompt) but is tagged so it stays out of the chat list and the Memory Keeper's sweep —
    # a spoken one-off isn't a conversation the user wants to scroll back through.
    surface: str = "chat"


class ChatStop(BaseModel):
    runId: str


class ChatSteer(BaseModel):
    runId: str
    message: str
    mode: str = "steer"     # "steer" (fold in now) or "follow_up" (queue for after this turn)
    queueId: str            # client-generated id echoed back in the picked_up event for matching
    references: list[Reference] = []  # @-mentions typed mid-run — dropped silently without this


class ChatCancelPending(BaseModel):
    runId: str
    queueId: str


class ChatRespond(BaseModel):
    runId: str
    requestId: str
    approved: bool | None = None   # answer to an `approve` prompt
    answer: str | None = None      # answer to an `ask` prompt
    tool: str | None = None        # tool name (for scope != "once")
    scope: str = "once"            # "once" | "session" ("Allow for this chat") | "always"


# Live interactive chat runs: runId -> (host, permission, runner_box, task_box, pending_queue).
# runner_box/task_box are one-item lists filled in once the run's curry-leaves Runner /
# asyncio Task exist (both come into being slightly after the run starts), so stop/steer —
# which race the run itself — always see the latest value.
_CHAT_RUNS: dict = {}

# Live *chat* runs by session: sessionId -> runId. _CHAT_RUNS is keyed by runId and carries
# no session identity, but the UI needs the reverse lookup: on opening (or reloading) a
# session it must discover whether a run is still streaming for it and re-attach, and the
# session list needs it to show which chats are busy. Only surface="chat" runs with a session
# land here (background/tile runs and ephemeral turns have no session row to attach to).
# Entries are removed when the run reaches a terminal frame — see `_forget_session_run`.
_SESSION_RUNS: dict[str, str] = {}


def _forget_session_run(run_id: str) -> None:
    """Drop the session -> run mapping for a finished run (id-guarded: a newer run for the
    same session must not be evicted by a late terminal frame from an older one)."""
    for sid, rid in list(_SESSION_RUNS.items()):
        if rid == run_id:
            _SESSION_RUNS.pop(sid, None)


def live_session_runs() -> dict[str, str]:
    """Snapshot of {sessionId: runId} for chat runs still streaming right now."""
    return dict(_SESSION_RUNS)


def _msg_text(msg) -> str:
    blocks = getattr(msg, "content", None) or []
    return "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", None) == "text")


def _humanize_provider_error(msg: str) -> str:
    """Turn a raw kernel/provider error into a short, actionable line for the chat bubble.

    The kernel wraps HTTP failures as e.g.
      ``provider error: HttpError: Anthropic 401: {"type":"error","error":{"message":"invalid x-api-key"}}``
    We keep the useful bit (status + the provider's own message) and, for the common auth case,
    point the user at where to fix it. Anything we don't recognize passes through unchanged so we
    never hide detail."""
    import json as _json
    import re as _re

    text = msg.strip()
    # Pull the provider's human message out of the embedded JSON body, if present.
    inner = text
    brace = text.find("{")
    if brace != -1:
        try:
            body = _json.loads(text[brace:])
            inner = (body.get("error") or {}).get("message") or body.get("message") or inner
        except Exception:
            pass
    status = None
    m = _re.search(r"\b(\d{3})\b", text[:brace] if brace != -1 else text)
    if m:
        status = m.group(1)
    if status in {"401", "403"} or "api-key" in inner.lower() or "api key" in inner.lower() \
            or "authentication" in inner.lower() or "unauthorized" in inner.lower():
        return (f"Authentication failed ({inner}). Check the API key for this provider in "
                "Settings → AI providers.")
    if status == "404" or "model" in inner.lower() and "not" in inner.lower():
        return f"The provider rejected the request ({inner}). Check the selected model in Settings → AI providers."
    if status == "429" or "rate limit" in inner.lower() or "quota" in inner.lower():
        return f"Rate limited or out of quota ({inner}). Try again shortly or check your plan."
    # Fall back to the cleaned inner message (or the whole thing if we found nothing better).
    return inner if inner != text else text


def _chat_events(ev, usage: dict, pending_queue: dict | None = None, session_id: str | None = None) -> list[dict]:
    """Translate one curry-leaves event into zero or more chat frames.

    `session_id`, when given, turns task-list tool calls (task_create/update/...) into
    `tasks` snapshot events for the composer's Tasks panel instead of normal tool cards
    — the panel IS their rendering. Without a session (ephemeral runs, in-memory task
    store) there's no tasks.json to snapshot, so they fall through as plain tool cards.

    `pending_queue` (text -> queueId), when given, is how a `picked_up` event gets its
    queueId: steer registers the text it queued there, and the FIRST message_start for a
    steering/follow_up UserMessage whose text matches pops it back out. Matching by text
    (not a kernel-level message id) is a deliberate app-layer choice — adding an id to
    UserMessage itself would touch session-store/provider serialization across the kernel
    for a concern that's purely about correlating this stream to its caller."""
    # curry-leaves events have a .type Literal field (snake_case), not class-name matching.
    t = getattr(ev, "type", None)
    if t == "run_meta":
        usage["model"] = getattr(ev, "model", "") or ""
        return []
    if t == "message_start":
        msg = getattr(ev, "message", None)
        origin = getattr(msg, "origin", None)
        if origin in ("steering", "follow_up"):
            text = _msg_text(msg)
            queue_id = pending_queue.pop(text, None) if pending_queue else None
            return [{"type": "picked_up", "origin": origin, "text": text, "queueId": queue_id}]
        return []
    if t == "message_update" and getattr(ev, "delta", None) is not None:
        d = ev.delta
        if getattr(d, "kind", None) == "text":
            return [{"type": "token", "text": d.value}]
        if getattr(d, "kind", None) == "thinking":
            return [{"type": "thinking", "text": d.value}]
    elif t == "tool_start":
        name = getattr(ev, "tool_name", "")
        if name in ("ask", "approve"):  # interactive tools — shown via PendingCard, not ToolCallCard
            return []
        if name in agent_engine._TASK_TOOL_NAMES and session_id:  # shown via the Tasks panel
            return []
        return [{"type": "tool_start", "id": getattr(ev, "tool_call_id", ""),
                 "name": name, "input": _args_text(getattr(ev, "args", None))}]
    elif t == "tool_end":
        name = getattr(ev, "tool_name", "")
        if name in ("ask", "approve"):
            return []
        if name in agent_engine._TASK_TOOL_NAMES and session_id:
            # The store has already persisted this mutation — push the fresh snapshot.
            return [{"type": "tasks", "tasks": chat_sessions.get_tasks(session_id)}]
        res = getattr(ev, "result", None)
        turn = usage.get("turn") or {}
        return [{"type": "tool_end", "id": getattr(ev, "tool_call_id", ""),
                 "name": name, "output": _result_text(res),
                 "isError": bool(getattr(res, "is_error", False)),
                 "tokensIn": turn.get("input", 0), "tokensOut": turn.get("output", 0)}]
    elif t == "message_end":
        u = getattr(getattr(ev, "message", None), "usage", None)
        if u:
            turn_in, turn_out = getattr(u, "input", 0) or 0, getattr(u, "output", 0) or 0
            usage["input"] += turn_in
            usage["output"] += turn_out
            # Stashed so the tool calls this turn's message provokes (tool_start/tool_end,
            # emitted strictly between this message_end and the next message_start) can be
            # tagged with the token cost of the LLM call that decided to make them.
            usage["turn"] = {"input": turn_in, "output": turn_out}
    elif t == "elision":
        return [{"type": "elision", "resultsElided": getattr(ev, "results_elided", 0),
                 "tokensReclaimed": getattr(ev, "tokens_reclaimed", 0)}]
    elif t == "error":
        # The kernel reports a provider/tool failure as an ErrorEvent(message=…) INSIDE the
        # stream (e.g. a 401 invalid-api-key) — the Runner records it on the assistant message
        # and ends the turn normally rather than raising, so drive()'s except never fires. Without
        # forwarding it, chat shows a blank bubble with no hint of why. Surface the reason as the
        # same {type:"error"} frame the UI already renders (⚠ …). We don't stop the run here —
        # the kernel still emits agent_end/done to close the turn cleanly.
        msg = getattr(ev, "message", "") or "The AI provider returned an error."
        return [{"type": "error", "error": _humanize_provider_error(msg),
                 "fatal": bool(getattr(ev, "fatal", False))}]
    elif t == "agent_end":
        # input/output are cumulative across the run's LLM calls (cost); the LAST call's
        # input+output is the context the model actually saw — that's what the UI meter shows.
        # `limit` is the model's real context window so the meter isn't pinned to 128k.
        turn = usage.get("turn") or {}
        model = usage.get("model", "")
        # Wall time for the whole turn — model calls AND tool/subagent time, which on a
        # delegating run is most of it. 0 when the caller didn't stamp a start (older
        # callers), which the UI reads as "unknown" and omits.
        t0 = usage.get("t0")
        elapsed_ms = int((time.monotonic() - t0) * 1000) if t0 else 0
        return [{"type": "usage", "input": usage["input"], "output": usage["output"],
                 "context": turn.get("input", 0) + turn.get("output", 0),
                 "limit": context_limit(model), "model": model, "elapsedMs": elapsed_ms},
                {"type": "done"}]
    # NOTE: subagent_activity is deliberately NOT handled here. It reaches the UI as `sub`
    # frames via SSEChatHost._subagent_items, which tags each one with the delegating
    # tool_call_id so it renders nested inside that tool's card. Unwrapping it here too
    # (as we used to) flattened a subagent's calls into the parent's own tool list, so the
    # same work showed up twice — once inline, once in the subagent panel.
    return []


# Which read tool opens each reference kind. Naming the tool inline in the prompt removes the
# inference step between "there is a recording" and "I can open it" — without it a model will
# happily answer from the title alone and sound right while never reading the transcript.
_REF_TOOL = {
    "recording": 'recordings_read(action="read", recording_id="{id}")',
    "note": 'kb_read(path="{id}")',
    "todo": "todos_read(action=\"list\") — then find id {id}",
    "reminder": "reminders_read(action=\"list\") — then find id {id}",
    "file": 'file_read(path="{id}")',
}
# The tool a reference kind needs ADVERTISED. Only these two are deferred/absent on the seed
# agent; todos_read/reminders_read/kb_read are always-on already.
REF_PROMOTES = {"recording": "recordings_read", "file": "file_read"}


def _ref_exists(ref: Reference) -> bool:
    """Is this handle still live? The user may have picked a recording in the menu and deleted
    it before sending. A stale handle must degrade to a visible note rather than a phantom the
    agent burns a failed tool call on mid-run."""
    from curry_leaves_assistant.domain import knowledge, recordings
    from curry_leaves_assistant.stores import data
    try:
        if ref.type == "recording":
            return recordings.get(ref.id) is not None
        if ref.type == "note":
            # By PATH (what the picker hands back), not by name — knowledge.resolve() is a
            # name/alias lookup and returns None for a perfectly live path.
            return knowledge.read_note(ref.id) is not None
        if ref.type == "todo":
            return any(t.get("id") == ref.id for t in data.list_todos())
        if ref.type == "reminder":
            return any(r.get("id") == ref.id for r in data.list_reminders())
        if ref.type == "file":
            from curry_leaves_assistant.stores import files_store
            return files_store.exists(ref.id)
    except Exception:
        return True  # a store hiccup shouldn't strip a probably-fine reference
    return True


def _references_block(refs: list[Reference]) -> str:
    """The `@`-mentions as a prompt preamble: what the user pointed at and how to open it.

    Wrapped in the same in-band sentinels attachments use, so get_messages strips it back out
    into chips on reload instead of showing the user machinery they didn't type."""
    if not refs:
        return ""
    lines = []
    for r in refs:
        if not _ref_exists(r):
            lines.append(f'{r.type} "{r.title}" — NO LONGER EXISTS (deleted since it was referenced)')
            continue
        how = _REF_TOOL.get(r.type, "").format(id=r.id)
        lines.append(f'{r.type} {r.id} "{r.title}"' + (f" — read with {how}" if how else ""))
    head = chat_sessions._REF_OPEN.format(
        meta=json.dumps({"refs": [{"type": r.type, "id": r.id, "title": r.title} for r in refs]}))
    body = ("The user pointed at these with @ in their message. Read one only if answering "
            "needs its contents.\n" + "\n".join(lines))
    return f"{head}\n{body}\n␞<<<curry-leaves:/references>>>␞\n\n"


async def start_run(body: ChatBody) -> dict:
    """Start a chat run. Returns ``{"sessionId", "runId"}`` (sessionId None for ephemeral).
    Frames then stream over the hub's ``chat:<runId>`` channel; the caller should already
    be subscribed (api/ws.py subscribes the requester before calling this). Raises
    ``LookupError`` if the agent doesn't exist."""
    agent = agent_store.read_agent(body.agentId)
    if agent is None:
        raise LookupError(f"agent not found: {body.agentId}")

    # Ephemeral runs (e.g. the System Prompt AI editor) don't create a session.
    sid = None if body.ephemeral else chat_sessions.ensure(
        body.sessionId, body.agentId, body.model, body.message, surface=body.surface)
    # `user_input` is either a plain string or a curry_leaves UserMessage carrying native
    # multimodal blocks (images/PDFs/audio sent to the model directly, no markdown round-trip).
    # @-references prepend the TEXT, so they must be folded in before build_user_message wraps
    # it into a UserMessage (whose first TextBlock is this string).
    user_input = _references_block(body.references) + body.message
    if sid and body.attachments:
        provider = agent_engine._effective_provider(agent)
        msg, _ = chat_sessions.build_user_message(sid, user_input, body.attachments, provider)
        if msg is not None:
            user_input = msg
        else:  # kernel too old for multimodal types — fall back to markdown inlining
            user_input, _ = chat_sessions.augment_input(sid, user_input, body.attachments)
    if body.ephemeral and body.history:
        convo = "\n".join(f"{'User' if t.get('role') == 'user' else 'Assistant'}: {t.get('content', '')}"
                          for t in body.history)
        user_input = f"Conversation so far:\n{convo}\n\nUser: {body.message}"

    run_id = uuid.uuid4().hex
    # Register the run's buffer NOW (before any frame / the discovery event) so a mirror
    # subscribing in the gap after chat.run.started isn't bounced with chat.gone.
    hub.open_chat(run_id)

    def publish(item: dict) -> None:
        hub.publish_chat(run_id, item)

    # ─── ephemeral: un-gated, no session, no interactive host ─────────────────
    if body.ephemeral:
        async def drive_simple():
            usage = {"input": 0, "output": 0, "model": "", "t0": time.monotonic()}
            try:
                async for ev in stream_agent(agent, user_input, body.model, sid, traced=False):
                    for item in _chat_events(ev, usage):
                        publish(item)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                publish({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                _CHAT_RUNS.pop(run_id, None)
        task_box: list = []
        _CHAT_RUNS[run_id] = (None, None, [], task_box, {})
        task_box.append(asyncio.create_task(drive_simple()))
        return {"sessionId": None, "runId": run_id}

    # ─── real chat: the shared streaming-run core ─────────────────────────────
    # Group every turn of this chat session under one trace, parented to a session root span.
    session_trace, session_span = f"tr_{sid}", f"sps_{sid}"
    trace_store.ensure_root(session_trace, session_span, name=(body.message or "Chat")[:48],
                            kind="session", attributes={"sessionId": sid})

    # Register BEFORE launching: the run can reach a terminal frame (and try to clear this)
    # on the very next tick, and a fast failure must not leave a stale "busy" session behind.
    if sid:
        _SESSION_RUNS[sid] = run_id
    launch_stream_run(run_id, agent, user_input, model=body.model, session_id=sid,
                      autonomous=body.autonomous, thinking_effort=body.thinkingEffort,
                      elide=body.elide, surface=body.surface,
                      activate_trace=(session_trace, session_span),
                      promote={REF_PROMOTES[r.type] for r in body.references if r.type in REF_PROMOTES})

    # Discovery signal for OTHER clients (multi-device / a second tab on the same session).
    events.emit("chat.run.started",
                payload={"sessionId": sid, "runId": run_id, "agentId": body.agentId,
                         "message": body.message},
                entity_id=sid, label=(body.message or "Chat")[:48])
    return {"sessionId": sid, "runId": run_id}


def launch_stream_run(run_id: str, agent: dict, user_input, *, model: str | None = None,
                      session_id: str | None = None, autonomous: bool = False,
                      thinking_effort: str | None = None, elide: bool = False,
                      surface: str = "chat", extra_tools: list | None = None,
                      output_type: type | None = None, activate_trace: tuple | None = None,
                      on_terminal=None, promote: set[str] | None = None,
                      lane: str | None = None, pool_item_id: str | None = None) -> None:
    """The one streaming-run engine — used by chat AND background runs (workers, tiles).

    Builds an interactive host + permission engine, registers the run under `run_id` in
    _CHAT_RUNS (so stop/steer/respond/cancel_pending work by id), and spawns the drive +
    drainer tasks that stream frames to the hub's `chat:<run_id>` channel. Returns immediately;
    the run continues in the background.

    `on_terminal(status, output_text, error)` fires when the run ends — chat passes None; a
    background caller (workers/tiles) uses it to write the run record, resolve completion, etc.
    `activate_trace=(traceId, spanId)` roots the run under an existing session/job span."""
    from curry_leaves_assistant.agents.chat_host import SSEChatHost
    from curry_leaves.permission import PermissionEngine, PermissionOptions
    from curry_leaves.thinking import Effort

    # Background runs (surface != "chat") announce ask/approve to the global event bus so the
    # office walks the agent to Your desk and a "waiting on you" notification fires. Live chat
    # keeps its ask card on-screen, so it doesn't need the app-wide alert.
    # `lane`/`pool_item_id` (background callers only) let an unbounded ask release its
    # scheduler slot and pin the persisted question to the pool ask it concerns.
    host = SSEChatHost(job_id=run_id, agent=agent, announce=(surface != "chat"),
                       lane=lane, pool_item_id=pool_item_id)
    permission = PermissionEngine(PermissionOptions(
        global_approvals=app_settings.global_approvals(),
        on_global_approve=app_settings.add_global_approval,
        auto_approve=(lambda _tool, _risk, _args: True) if autonomous else None,
    ))
    if session_id:
        permission._session_approvals |= set(chat_sessions.session_approvals(session_id))

    runner_box: list = []
    task_box: list = []
    pending_queue: dict = {}
    _CHAT_RUNS[run_id] = (host, permission, runner_box, task_box, pending_queue)
    hub.open_chat(run_id)
    effort = Effort(thinking_effort) if thinking_effort else None

    def publish(item: dict) -> None:
        hub.publish_chat(run_id, item)

    async def drainer():
        while True:
            item = await host.outbox.get()
            if item.get("type") == "__end__":
                break
            publish(item)

    async def drive():
        # `t0` stamps the wall clock at run start so the closing `usage` frame can report
        # how long the turn took. Measured here, server-side, so it covers the whole run
        # (model calls + tool time) rather than whatever the browser happened to observe.
        usage = {"input": 0, "output": 0, "model": "", "t0": time.monotonic()}
        text_parts: list[str] = []   # accumulate the assistant's text → the run's final output
        status, error = "done", None
        try:
            def _run():
                return stream_agent(agent, user_input, model, session_id, host=host, permission=permission,
                                    thinking_effort=effort, autonomous=autonomous, elide=elide,
                                    surface=surface, on_runner=lambda r: runner_box.append(r),
                                    extra_tools=extra_tools, output_type=output_type, promote=promote)
            if activate_trace:
                with trace_ctx.activate(activate_trace[0], activate_trace[1], kind="session"):
                    async for ev in _run():
                        for item in _chat_events(ev, usage, pending_queue, session_id=session_id):
                            if item.get("type") == "token":
                                text_parts.append(item.get("text", ""))
                            host.put(item)
            else:
                async for ev in _run():
                    for item in _chat_events(ev, usage, pending_queue, session_id=session_id):
                        if item.get("type") == "token":
                            text_parts.append(item.get("text", ""))
                        host.put(item)
        except asyncio.CancelledError:
            status = "stopped"
            host.put({"type": "done", "stopped": True})
        except Exception as exc:
            import traceback; traceback.print_exc()
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            host.put({"type": "error", "error": error})
        finally:
            if session_id:
                for tool in permission.session_grants:
                    chat_sessions.add_session_approval(session_id, tool)
            host.put({"type": "__end__"})
            _CHAT_RUNS.pop(run_id, None)
            _forget_session_run(run_id)
            if on_terminal is not None:
                try:
                    on_terminal(status, "".join(text_parts), error)
                except Exception as exc:  # never let finalization break the run teardown
                    print(f"[chat_runs] on_terminal failed for {run_id}: {exc}", flush=True)

    asyncio.create_task(drainer())
    task_box.append(asyncio.create_task(drive()))


def stop_run(run_id: str) -> bool:
    """Hard-stop a live run by cancelling its drive Task. Returns False if unknown."""
    entry = _CHAT_RUNS.get(run_id)
    if entry is None:
        return False
    _host, _permission, _runner_box, task_box, _pending_queue = entry
    if task_box and not task_box[0].done():
        task_box[0].cancel()
    return True


def steer_run(body: ChatSteer) -> str:
    """Queue a message against a still-running turn. Returns "ok", "gone" (no such run),
    or "early" (Runner not captured yet — too early to steer)."""
    entry = _CHAT_RUNS.get(body.runId)
    if entry is None:
        return "gone"
    _host, _permission, runner_box, _task_box, pending_queue = entry
    if not runner_box:
        return "early"
    runner = runner_box[0]
    # The runner gets the @-reference handles folded in; pending_queue stays keyed by what the
    # user actually typed, since that's what the UI matches its pending chip against.
    text = _references_block(body.references) + body.message
    pending_queue[body.message] = body.queueId
    if body.mode == "follow_up":
        runner.follow_up(text, id=body.queueId)
    else:
        runner.steer(text, id=body.queueId)
    return "ok"


def cancel_pending(body: ChatCancelPending) -> tuple[str, bool]:
    """Pull a queued steer/follow_up item back out. Returns (status, ok) where status is
    "gone" (no run), "early" (no runner yet), or "ok"; ok is whether it was actually
    cancelled (False if already picked up)."""
    entry = _CHAT_RUNS.get(body.runId)
    if entry is None:
        return "gone", False
    _host, _permission, runner_box, _task_box, pending_queue = entry
    if not runner_box:
        return "early", False
    ok = runner_box[0].cancel_pending(body.queueId)
    if ok:
        stale = [t for t, qid in pending_queue.items() if qid == body.queueId]
        for t in stale:
            pending_queue.pop(t, None)
    return "ok", ok


def respond(body: ChatRespond) -> tuple[str, bool]:
    """Answer a pending approve/ask prompt. Returns (status, ok): status "gone" if no run,
    else "ok"; ok is whether the pending request was resolved.

    For an approve prompt, PermissionEngine.authorize expects a SCOPE STRING back —
    "always"/"session"/"once"/"deny" — not a bool (a bool there raises a pydantic error
    that silently kills the run right after approval)."""
    entry = _CHAT_RUNS.get(body.runId)
    if entry is None:
        return "gone", False
    host, _permission, _runner_box, _task_box, _pending_queue = entry
    if body.answer is not None:
        ok = host.resolve(body.requestId, body.answer)
    else:
        approved = bool(body.approved)
        scope = "deny" if not approved else (body.scope if body.scope in ("session", "always") else "once")
        ok = host.resolve(body.requestId, scope)
    return "ok", ok
