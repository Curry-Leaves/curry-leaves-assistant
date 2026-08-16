"""The single WebSocket control plane: `/ws`.

One socket per client carries every live stream — the app event feed today, chat and
audio in later commits. Replaces the per-surface SSE/WS endpoints. Auth is the first
frame (no `?token=` in the URL); after that the client subscribes to channels and the
hub (core/ws_hub) fans frames back.

Frame protocol (JSON text frames):
  client → {type:"hello", token}                    → {type:"welcome", serverTime}
        → {type:"sub", channel:"events", since?}     → replay (or {type:"replay.reset"}) then live
        → {type:"sub", channel:"chat", runId, since?}→ (re)attach to a run; replay then live
        → {type:"unsub", channel:"events"|"chat", runId?}
        → {type:"req", id, method, params}           → {type:"res", id, ok, ...}
        → {type:"audio.start", streamId, kind}       → {type:"audio.ready", streamId, slot}
        → <binary: [1-byte slot][float32 PCM]>       (live audio; addressed by slot)
        → {type:"audio.end", streamId}               → final transcript append
        → {type:"tts.speak", streamId, text, voice?, lang?} → tts.start, PCM frames, tts.end
        → {type:"tts.stop", streamId}                (cancel in-flight synthesis/playback)
        → {type:"pong"}                              (reply to our ping; keeps the socket warm)
  server → {type:"event", event}                     (an app event)
        → {type:"tts.start", streamId, slot, sampleRate}   (playback begins)
        → <binary: [1-byte slot][float32 PCM]>       (synthesized speech; addressed by slot)
        → {type:"tts.end", streamId}                 (playback complete / cancelled)
        → {type:"chat", runId, seq, ev}              (one chat frame)
        → {type:"chat.gone", runId}                  (subscribed to a run that no longer exists)
        → {type:"transcript", streamId, append, isFinal?}  (live transcript delta)
        → {type:"ping", t}                           (every PING_SEC; client should pong)

Chat is fully WS: `chat.start` returns {sessionId, runId} and auto-subscribes the caller;
`chat.stop|steer|respond|cancelPending` are req/res methods. See agents/chat_runs.py.
Live audio: one binary stream per `audio.start`, tagged by a slot byte, transcribed
incrementally back over `transcript` frames. See domain/transcribe.LiveTranscriber.
"""
from __future__ import annotations

import asyncio
import json
import struct
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from curry_leaves_assistant.agents import chat_runs
from curry_leaves_assistant.core import auth, events
from curry_leaves_assistant.core.store import now_iso
from curry_leaves_assistant.core.ws_hub import Connection, hub
from curry_leaves_assistant.domain import transcribe

router = APIRouter(tags=["ws"])

PING_SEC = 20.0

# Strong refs to detached audio-flush tasks (see _AudioStream.finish_detached) so they aren't
# GC'd mid-flight. asyncio holds only a weak ref to a bare create_task result.
_detached_finishers: set[asyncio.Task] = set()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()

    # ─── handshake: first frame must authenticate ────────────────────────────
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
    except (asyncio.TimeoutError, Exception):
        await ws.close(code=1008)
        return
    if not (isinstance(hello, dict) and hello.get("type") == "hello"
            and auth.token_ok(hello.get("token") or "")):
        await ws.close(code=1008)  # policy violation
        return
    await ws.send_json({"type": "welcome", "serverTime": now_iso()})

    conn = Connection(uuid.uuid4().hex)
    hub.add(conn)

    async def writer() -> None:
        """Drain this connection's queue to the socket. Exits when the connection is
        marked closed (normal teardown or backpressure overflow)."""
        while not conn.closed:
            frame = await conn.queue.get()
            if conn.closed:
                break
            try:
                # A binary frame (TTS PCM) is carried through the same ordered queue as a
                # tagged dict {"__bytes__": ...} so it stays in sequence with its
                # surrounding tts.start/tts.end JSON frames. Everything else is JSON.
                raw = frame.get("__bytes__") if isinstance(frame, dict) else None
                if raw is not None:
                    await ws.send_bytes(raw)
                else:
                    await ws.send_json(frame)
            except Exception:
                # Peer went away before the read loop noticed — stop writing and let the read
                # loop's finally do the teardown. Swallowing here keeps a doomed send from
                # surfacing as an unretrieved-task-exception log.
                break

    async def pinger() -> None:
        while not conn.closed:
            await asyncio.sleep(PING_SEC)
            hub.send_to(conn, {"type": "ping", "t": now_iso()})

    writer_task = asyncio.create_task(writer())
    ping_task = asyncio.create_task(pinger())
    # Live audio streams on THIS connection: slot(int) -> _AudioStream. Slots are addressed
    # by the 1-byte prefix on each binary frame, so one socket can carry several streams.
    audio: dict[int, _AudioStream] = {}
    # Outbound TTS playback streams on THIS connection: slot(int) -> _TtsStream. The slot is
    # the 1-byte prefix stamped on each outbound PCM frame (symmetric to inbound audio).
    tts_streams: dict[int, _TtsStream] = {}

    try:
        while True:
            # Handle both text (JSON control/req frames) and binary (audio PCM) frames.
            raw = await ws.receive()
            if raw.get("type") == "websocket.disconnect":
                break
            if (data := raw.get("bytes")) is not None:
                await _handle_audio_frame(audio, data)
            elif (text := raw.get("text")) is not None:
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                await _handle(conn, audio, tts_streams, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        for st in audio.values():
            st.cancel()
        for tst in tts_streams.values():
            tst.cancel()
        hub.remove(conn)
        writer_task.cancel()
        ping_task.cancel()
        # Await the cancellations so the tasks are fully unwound before the handler returns —
        # otherwise a writer mid-send can raise into a "Task exception was never retrieved" log.
        await asyncio.gather(writer_task, ping_task, return_exceptions=True)
        try:
            await ws.close()
        except Exception:
            pass


async def _handle(conn: Connection, audio: dict, tts_streams: dict, msg: dict) -> None:
    """Route one inbound client TEXT frame. Unknown types are ignored (forward-compatible)."""
    t = msg.get("type")
    if t == "sub":
        channel = msg.get("channel")
        if channel == "events":
            _sub_events(conn, msg.get("since"))
        elif channel == "chat":
            _sub_chat(conn, msg.get("runId"), msg.get("since") or 0)
        elif channel == "kernel":
            _sub_kernel(conn)
    elif t == "unsub":
        channel = msg.get("channel")
        if channel == "chat":
            run_id = msg.get("runId")
            if run_id:
                hub.unsubscribe(conn, f"chat:{run_id}")
        elif channel:
            hub.unsubscribe(conn, channel)
    elif t == "req":
        await _handle_req(conn, msg)
    elif t == "audio.start":
        _audio_start(conn, audio, msg.get("streamId"), msg.get("recordingId"), msg.get("kind"))
    elif t == "audio.end":
        _audio_end(audio, msg.get("streamId"))   # detached flush — see _audio_end
    elif t == "tts.speak":
        # Synthesize `text` and stream it back as slot-tagged PCM frames (chat Voice button).
        _tts_speak(conn, tts_streams, msg.get("streamId"), msg.get("text") or "",
                   msg.get("voice"), msg.get("lang"))
    elif t == "tts.stop":
        # User interrupted / started talking → cancel the in-flight synthesis + playback.
        _tts_stop(tts_streams, msg.get("streamId"))
    elif t == "live.attach":
        # Attach the live-context engine to an open stream once the recordingId is known
        # (it can arrive after audio.start). Idempotent. An optional `enabled` bool is the
        # Capture toggle's per-recording override of the app-level live.enabled setting;
        # omitting it (or null) leaves this recording following the setting.
        _live_attach(conn, audio, msg.get("streamId"), msg.get("recordingId"),
                     msg.get("enabled"))
    elif t == "live.signal":
        # A high-signal cue from the UI (attendee added, note typed) → run the live engine now.
        _live_signal(audio, msg.get("streamId"), msg.get("hint") or "")
    elif t == "live.refresh":
        # The user asked for fresh copilot cards → force a pass now (bypasses the cooldown).
        _live_refresh(audio, msg.get("streamId"))
    # "pong" and anything else: no-op (the read itself proves the socket is alive).


# ─── req/res dispatch (chat verbs) ────────────────────────────────────────────
async def _handle_req(conn: Connection, msg: dict) -> None:
    """Handle a request frame: run the named method, reply with a matching res frame.
    A method raising sends ``ok:false`` with an error; unknown methods → not_found."""
    req_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    try:
        result = await _dispatch(conn, method, params)
        hub.send_to(conn, {"type": "res", "id": req_id, "ok": True, **result})
    except _ReqError as e:
        hub.send_to(conn, {"type": "res", "id": req_id, "ok": False,
                           "error": {"code": e.code, "message": e.message}})
    except Exception as e:  # noqa: BLE001 — never let a bad request kill the socket
        hub.send_to(conn, {"type": "res", "id": req_id, "ok": False,
                           "error": {"code": "internal", "message": f"{type(e).__name__}: {e}"}})


class _ReqError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message
        super().__init__(message)


async def _dispatch(conn: Connection, method: str | None, params: dict) -> dict:
    if method == "chat.start":
        body = chat_runs.ChatBody(**params)
        try:
            res = await chat_runs.start_run(body)
        except LookupError as e:
            raise _ReqError("not_found", str(e))
        # Auto-subscribe this connection to the run it just started, so its frames arrive
        # without a separate `sub` round-trip.
        hub.subscribe(conn, f"chat:{res['runId']}")
        return res
    if method == "chat.stop":
        ok = chat_runs.stop_run(chat_runs.ChatStop(**params).runId)
        if not ok:
            raise _ReqError("not_found", "run not found")
        return {}
    if method == "chat.steer":
        status = chat_runs.steer_run(chat_runs.ChatSteer(**params))
        if status == "gone":
            raise _ReqError("not_found", "run not found")
        if status == "early":
            raise _ReqError("too_early", "run not ready to steer")
        return {}
    if method == "chat.cancelPending":
        status, ok = chat_runs.cancel_pending(chat_runs.ChatCancelPending(**params))
        if status == "gone":
            raise _ReqError("not_found", "run not found")
        return {"cancelled": ok}
    if method == "chat.respond":
        status, ok = chat_runs.respond(chat_runs.ChatRespond(**params))
        if status == "gone":
            raise _ReqError("not_found", "run not found")
        return {"resolved": ok}
    # ─── run.* — control a BACKGROUND run (tile / trigger / workflow) by its jobId. A
    # streaming background run registers in _CHAT_RUNS under jobId, so the same chat_runs
    # functions drive it. These mirror chat.* but the id is a jobId (= the run's channel). ──
    if method == "run.stop":
        ok = chat_runs.stop_run(params.get("jobId") or params.get("runId"))
        if not ok:
            raise _ReqError("not_found", "run not found")
        return {}
    if method == "run.steer":
        jid = params.get("jobId") or params.get("runId")
        status = chat_runs.steer_run(chat_runs.ChatSteer(
            runId=jid, message=params.get("message", ""),
            mode=params.get("mode", "steer"), queueId=params.get("queueId", "")))
        if status == "gone":
            raise _ReqError("not_found", "run not found")
        if status == "early":
            raise _ReqError("too_early", "run not ready to steer")
        return {}
    if method == "run.cancelPending":
        jid = params.get("jobId") or params.get("runId")
        status, ok = chat_runs.cancel_pending(chat_runs.ChatCancelPending(runId=jid, queueId=params.get("queueId", "")))
        if status == "gone":
            raise _ReqError("not_found", "run not found")
        return {"cancelled": ok}
    if method == "run.respond":
        # Answer a pending ask/approve for a background run. A STREAMING run's SSEChatHost is in
        # _CHAT_RUNS → chat_runs.respond resolves it. A suspended (non-streaming) ask run uses
        # the SuspendHost → fall back to suspend_host.resolve.
        job_id = params.get("jobId") or params.get("runId")
        req_id = params.get("requestId")
        status, ok = chat_runs.respond(chat_runs.ChatRespond(
            runId=job_id, requestId=req_id, approved=params.get("approved"),
            answer=params.get("answer"), scope=params.get("scope", "once")))
        if status != "gone" and ok:
            return {"resolved": True}
        # Fall back to the SuspendHost path.
        from curry_leaves_assistant.orchestration import suspend_host
        if params.get("answer") is not None:
            answer = params["answer"]
        else:
            approved = bool(params.get("approved"))
            scope = params.get("scope")
            answer = "deny" if not approved else (scope if scope in ("session", "always") else "once")
        ok = suspend_host.resolve(job_id, req_id, answer)
        return {"resolved": ok}
    raise _ReqError("unknown_method", f"unknown method: {method}")


def _sub_chat(conn: Connection, run_id: str | None, since: int) -> None:
    """(Re)attach to a chat run: subscribe, then replay buffered frames after `since`.
    Subscribing before replaying means a frame landing mid-replay is delivered live (the
    client dedupes by seq). If the run is unknown (ended + buffer dropped), tell the client."""
    if not run_id:
        return
    if not hub.chat_exists(run_id):
        hub.send_to(conn, {"type": "chat.gone", "runId": run_id})
        return
    hub.subscribe(conn, f"chat:{run_id}")
    hub.replay_chat(conn, run_id, since)


def _sub_kernel(conn: Connection) -> None:
    """Subscribe to the Work Kernel live feed and send the current snapshot immediately, so a
    freshly-attached dashboard renders the queue without waiting for the next state change.
    The channel is stateless (each frame is a full snapshot) — no replay cursor needed."""
    from curry_leaves_assistant.orchestration import work
    hub.subscribe(conn, "kernel")
    hub.send_to(conn, {"type": "kernel", "snapshot": work.kernel_snapshot()})


def _sub_events(conn: Connection, since: str | None) -> None:
    """Subscribe to the app event feed. With `since`, replay missed events first (or tell
    the client to reset if the cursor is too old to guarantee no gap), then go live.

    Subscribing BEFORE replaying means an event landing mid-replay is delivered live
    rather than dropped — at worst the client sees it twice (events carry stable ids, so
    the client dedupes)."""
    hub.subscribe(conn, "events")
    if not since:
        return
    missed, found = events.events_since(since)
    if not found:
        hub.send_to(conn, {"type": "replay.reset"})
        return
    for ev in missed:
        hub.send_to(conn, {"type": "event", "event": ev})


# ─── live audio → transcript ──────────────────────────────────────────────────
class _AudioStream:
    """One live audio stream on a connection. Binary frames feed a LiveTranscriber; when a
    chunk transcribes, a ``transcript`` frame goes back to this connection. Transcription
    is offloaded to a thread and serialized (one chunk at a time) so frames stay ordered."""

    # Cap on chunk tasks queued behind the lock. Each inbound binary frame used to spawn one
    # unconditionally, so if transcription ran slower than the mic produced audio the set grew
    # for the whole meeting. One running + a couple waiting is all that can ever be useful:
    # the transcriber holds its own PCM buffer, so a frame whose task we skip is still fed by
    # whichever task drains next. This bounds tasks, not audio.
    _MAX_PENDING = 3

    def __init__(self, conn: Connection, stream_id: str, live=None) -> None:
        self.conn = conn
        self.stream_id = stream_id
        self._tr = transcribe.LiveTranscriber()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self.live = live  # optional live_context._Session (meeting streams only)

    def feed(self, pcm: bytes) -> None:
        # Always buffer the PCM (cheap, and the transcriber caps its own buffer); only spawn a
        # drain task if one isn't already backed up waiting for the model.
        self._tr.accept(pcm)
        if len(self._tasks) < self._MAX_PENDING:
            self._spawn(self._tr.feed_pending)

    async def end(self) -> None:
        # Wait for in-flight chunk work, then flush the tail as the final append.
        await self._await_tasks()
        try:
            text = await asyncio.to_thread(self._tr.flush)
        except Exception as exc:  # noqa: BLE001 — a failed flush still needs a terminal frame
            hub.send_to(self.conn, {"type": "transcript.error", "streamId": self.stream_id,
                                    "error": f"{type(exc).__name__}: {exc}"})
            return
        hub.send_to(self.conn, {"type": "transcript", "streamId": self.stream_id,
                                "append": text, "isFinal": True})

    def finish_detached(self) -> None:
        """Run end() (which flushes a full tail transcription — seconds of CPU) OFF the socket
        read loop, so ending one stream doesn't stall every other inbound frame. Retained in a
        module-level set until done so the task can't be GC'd mid-flush."""
        task = asyncio.create_task(self.end())
        _detached_finishers.add(task)
        task.add_done_callback(_detached_finishers.discard)

    def cancel(self) -> None:
        for t in self._tasks:
            t.cancel()

    def _spawn(self, fn) -> None:
        async def run():
            async with self._lock:  # serialize → transcript frames stay in order
                try:
                    text = await asyncio.to_thread(fn)
                except Exception as exc:  # noqa: BLE001
                    hub.send_to(self.conn, {"type": "transcript.error", "streamId": self.stream_id,
                                            "error": f"{type(exc).__name__}: {exc}"})
                    return
                if text:
                    hub.send_to(self.conn, {"type": "transcript", "streamId": self.stream_id, "append": text})
                    if self.live is not None:  # meeting stream → feed the live context engine
                        try:
                            self.live.feed_transcript(text, asyncio.get_event_loop())
                        except Exception:
                            pass  # the live engine is best-effort; never disturb transcription
        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _await_tasks(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


def _audio_start(conn: Connection, audio: dict, stream_id: str | None,
                 recording_id: str | None = None, kind: str | None = None) -> None:
    """Begin a live audio stream. Assigns a slot (the 1-byte prefix the client stamps on
    each binary frame) and replies with it. A meeting stream (kind != 'dictation') that
    carries a recordingId gets a live_context session so the in-meeting engine can run (subject
    to the live.enabled setting, re-checked per pass); dictation streams never do."""
    if not stream_id:
        return
    slot = next((i for i in range(256) if i not in audio), None)
    if slot is None:
        hub.send_to(conn, {"type": "transcript.error", "streamId": stream_id, "error": "too many audio streams"})
        return
    st = _AudioStream(conn, stream_id)
    audio[slot] = st
    # A meeting stream may carry its recordingId up front, or attach it later via live.attach
    # (the id can be assigned after the stream opens). Either path engages the live engine.
    if recording_id and kind != "dictation":
        _engage_live(st, conn, stream_id, recording_id)
    hub.send_to(conn, {"type": "audio.ready", "streamId": stream_id, "slot": slot})


def _engage_live(st: "_AudioStream", conn: Connection, stream_id: str, recording_id: str,
                 enabled: bool | None = None) -> None:
    """Create + attach a live-context session to a stream (idempotent).

    The session is attached whenever the template opts in (live.watch non-empty), even if the
    copilot is currently switched off — the OFF state lives on the session as an override the
    Capture toggle can flip mid-recording, so the engine must already be there to receive it.
    Every pass re-checks ``session.enabled()``, so an attached-but-disabled session costs
    nothing but the transcript it accumulates.

    ``enabled`` is the per-recording override from the Capture toggle; None = follow the app
    setting. On an already-attached session it updates the override in place rather than
    rebuilding, so the agent keeps its conversation history across a toggle.
    """
    if st.live is not None:
        if enabled is not None:
            st.live.set_enabled(enabled)
        return
    from curry_leaves_assistant.orchestration import live_context
    session = live_context._Session(conn, stream_id, recording_id, hub.send_to, enabled=enabled)
    if session._watch_kinds():
        st.live = session


def _live_attach(conn: Connection, audio: dict, stream_id: str | None, recording_id: str | None,
                 enabled: bool | None = None) -> None:
    """Attach the live engine to an already-open stream once the recordingId is known. Also the
    channel the Capture copilot toggle uses — re-sending attach with ``enabled`` set applies a
    per-recording override to the live session."""
    if not stream_id or not recording_id:
        return
    for st in audio.values():
        if st.stream_id == stream_id:
            _engage_live(st, conn, stream_id, recording_id, enabled)
            return


def _live_signal(audio: dict, stream_id: str | None, hint: str) -> None:
    """A UI cue (attendee added / note typed) for the stream with this id → run the live
    engine immediately on the hint text (bypasses the transcript-growth threshold)."""
    if not stream_id or not hint:
        return
    for st in audio.values():
        if st.stream_id == stream_id and st.live is not None:
            try:
                st.live.feed_signal(hint, asyncio.get_event_loop())
            except Exception:
                pass
            return


def _live_refresh(audio: dict, stream_id: str | None) -> None:
    """The user clicked refresh → force a copilot pass now (bypasses the cooldown/threshold)."""
    if not stream_id:
        return
    for st in audio.values():
        if st.stream_id == stream_id and st.live is not None:
            try:
                st.live.refresh(asyncio.get_event_loop())
            except Exception:
                pass
            return


def _audio_end(audio: dict, stream_id: str | None) -> None:
    """Flush + close the stream with this id (final transcript append). The flush runs a full
    tail transcription (seconds on CPU) — do it DETACHED, not inline on the socket read loop,
    or the whole connection stalls (no other audio, chat.stop, or req is read meanwhile). The
    slot is freed immediately; end() sends the final `transcript` frame when the flush lands."""
    for slot, st in list(audio.items()):
        if st.stream_id == stream_id:
            audio.pop(slot, None)
            st.finish_detached()
            return


async def _handle_audio_frame(audio: dict, data: bytes) -> None:
    """A binary frame: first byte is the stream slot, the rest is float32 PCM."""
    if not data:
        return
    st = audio.get(data[0])
    if st is not None:
        st.feed(data[1:])


# ─── text → speech (Kokoro) ───────────────────────────────────────────────────
class _TtsStream:
    """One text-to-speech playback stream on a connection. A `tts.speak` request spins one
    up: it synthesizes the text off the loop (Kokoro is blocking) and sends the audio back
    as a `tts.start` JSON frame, then binary PCM frames (each prefixed with a 1-byte slot so
    the client can bind them to this stream — symmetric to inbound audio), then `tts.end`.

    Long text is synthesized sentence-by-sentence so the first words start playing before the
    whole utterance is generated. Cancellable mid-flight (the user interrupts / starts talking)."""

    # float32 samples per binary frame (~85 ms at 24 kHz) — small enough for gapless playback
    # scheduling on the client, large enough to keep frame overhead low.
    CHUNK = 2048

    def __init__(self, conn: Connection, stream_id: str, slot: int, text: str,
                 voice: str | None, lang: str | None) -> None:
        self.conn = conn
        self.stream_id = stream_id
        self.slot = slot
        self._text = text
        self._voice = voice
        self._lang = lang
        self._cancelled = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        _detached_finishers.add(self._task)
        self._task.add_done_callback(_detached_finishers.discard)

    def cancel(self) -> None:
        self._cancelled = True
        if self._task is not None:
            self._task.cancel()

    async def _run(self) -> None:
        from curry_leaves_assistant.domain import tts
        header = struct.pack("<B", self.slot)
        hub.send_to(self.conn, {"type": "tts.start", "streamId": self.stream_id,
                                "slot": self.slot, "sampleRate": tts.SAMPLE_RATE})
        try:
            # Synthesize one sentence at a time on a worker thread; stream each result's PCM
            # out in CHUNK-sized binary frames as soon as it lands.
            sentences = await asyncio.to_thread(tts.split_sentences, self._text)
            if not sentences:
                sentences = [self._text.strip()] if self._text.strip() else []
            for sent in sentences:
                if self._cancelled:
                    break
                pcm = await asyncio.to_thread(tts.synth_pcm, sent, self._voice, self._lang)
                # float32 little-endian bytes; slice into slot-prefixed binary frames.
                buf = pcm.astype("<f4", copy=False).tobytes()
                step = self.CHUNK * 4
                for off in range(0, len(buf), step):
                    if self._cancelled:
                        break
                    hub.send_to(self.conn, {"__bytes__": header + buf[off:off + step]})
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — a failed synth still needs a terminal frame
            hub.send_to(self.conn, {"type": "tts.error", "streamId": self.stream_id,
                                    "error": f"{type(exc).__name__}: {exc}"})
        finally:
            hub.send_to(self.conn, {"type": "tts.end", "streamId": self.stream_id})


def _tts_speak(conn: Connection, tts_streams: dict, stream_id: str | None,
               text: str, voice: str | None, lang: str | None) -> None:
    """Begin a TTS playback stream for `text`. Assigns a slot (the 1-byte prefix stamped on
    each outbound PCM frame) and kicks off synthesis; frames flow back asynchronously."""
    if not stream_id or not (text or "").strip():
        return
    slot = next((i for i in range(256) if i not in tts_streams), None)
    if slot is None:
        hub.send_to(conn, {"type": "tts.error", "streamId": stream_id, "error": "too many tts streams"})
        return
    st = _TtsStream(conn, stream_id, slot, text, voice, lang)
    tts_streams[slot] = st

    def _drop(_task, slot=slot):
        tts_streams.pop(slot, None)
    st.start()
    if st._task is not None:
        st._task.add_done_callback(_drop)


def _tts_stop(tts_streams: dict, stream_id: str | None) -> None:
    """Cancel the TTS stream with this id (user interrupted / started talking)."""
    for slot, st in list(tts_streams.items()):
        if st.stream_id == stream_id:
            st.cancel()
            tts_streams.pop(slot, None)
            return
