"""The WebSocket hub: one place that owns every live connection and fans messages out.

A single ``/ws`` endpoint (api/ws.py) accepts connections and registers each as a
``Connection`` here. Producers — the event bus, chat runs, the live transcriber — call
``publish_*`` to push frames; the hub routes them to the connections subscribed to that
channel and each connection's own writer task drains its queue to the socket.

Design notes:
- **Thread-safe.** ``emit()`` and pool workers run off the event loop, so every enqueue
  goes through ``loop.call_soon_threadsafe`` (mirrors core/events.py). ``set_loop()`` is
  called once at startup.
- **Bounded queues → backpressure.** Each connection has a capped send queue; a client
  that stops reading overflows and is dropped (its socket closed) rather than leaking
  memory. Replay-on-reconnect makes a drop safe — the client resubscribes with a cursor.
- **Chat replay buffers.** Frames for a run are ring-buffered by ``runId`` with a
  monotonic ``seq`` so a client that (re)subscribes with ``since`` gets the frames it
  missed, then live. Buffers are dropped a short while after a run ends.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional

# A slow/stuck client shouldn't grow memory without bound; past this many undrained
# frames we close the socket and let it reconnect + replay. Generous — a healthy client
# never approaches it.
_MAX_QUEUED = int(os.environ.get("CURRY_LEAVES_WS_QUEUE_MAX", "2000"))
# How many frames to retain per run for reconnect replay, and how long after a run ends
# to keep its buffer around.
_CHAT_BUFFER_MAX = int(os.environ.get("CURRY_LEAVES_WS_CHAT_BUFFER", "1000"))
_CHAT_BUFFER_TTL_SEC = 60.0
# A run's buffer is normally dropped by the timer armed on its terminal (done/error) frame.
# But a run that is cancelled, crashes, or is registered by open_chat and never streams has no
# terminal frame — so nothing ever fires and its entry (up to _CHAT_BUFFER_MAX frames) stays
# resident for the life of the process. These two bound that: any buffer untouched for
# _CHAT_IDLE_TTL_SEC is swept, and the total number of retained runs is capped.
_CHAT_IDLE_TTL_SEC = float(os.environ.get("CURRY_LEAVES_WS_CHAT_IDLE_TTL", "1800"))
_CHAT_MAX_RUNS = int(os.environ.get("CURRY_LEAVES_WS_CHAT_MAX_RUNS", "200"))

_loop: Optional[asyncio.AbstractEventLoop] = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called once at startup so publish_* can push from any thread."""
    global _loop
    _loop = loop


class Connection:
    """One live socket. The endpoint owns the read side; the hub fills ``queue`` and the
    endpoint's writer task drains it. ``channels`` is the set this socket subscribes to
    (``"events"`` and/or ``"chat:<runId>"``)."""

    __slots__ = ("id", "queue", "channels", "closed")

    def __init__(self, conn_id: str) -> None:
        self.id = conn_id
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_MAX_QUEUED)
        self.channels: set[str] = set()
        self.closed = False

    def _try_put(self, frame: dict) -> None:
        """Enqueue on the loop thread. On overflow, mark closed so the writer tears down
        the socket — a client that fell this far behind must reconnect and replay."""
        if self.closed:
            return
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.closed = True
            # Wake the writer so it notices `closed` and exits.
            try:
                self.queue.get_nowait()
                self.queue.put_nowait({"type": "overflow"})
            except Exception:
                pass


class _Hub:
    def __init__(self) -> None:
        self._conns: dict[str, Connection] = {}
        # runId -> (list[(seq, frame)], last_seq, ended_at|None)
        self._chat: dict[str, dict[str, Any]] = {}

    # ─── connection lifecycle (called on the loop thread by the endpoint) ─────
    def add(self, conn: Connection) -> None:
        self._conns[conn.id] = conn

    def remove(self, conn: Connection) -> None:
        conn.closed = True
        self._conns.pop(conn.id, None)

    def subscribe(self, conn: Connection, channel: str) -> None:
        conn.channels.add(channel)

    def unsubscribe(self, conn: Connection, channel: str) -> None:
        conn.channels.discard(channel)

    def send_to(self, conn: Connection, frame: dict) -> None:
        """Direct send to one connection (used for replay + handshake), loop thread."""
        conn._try_put(frame)

    # ─── publishing (safe from any thread) ────────────────────────────────────
    def publish_event(self, event: dict) -> None:
        """Fan an app event out to every connection subscribed to the ``events`` channel."""
        self._publish("events", {"type": "event", "event": event})

    def publish_chat(self, run_id: str, ev: dict) -> None:
        """Buffer a chat frame under ``run_id`` (for replay) and fan it out live to
        subscribers of ``chat:<run_id>``."""
        def _do() -> None:
            self._sweep_chat(incoming=0 if run_id in self._chat else 1)
            rec = self._chat.get(run_id)
            if rec is None:
                rec = {"frames": [], "seq": 0, "ended_at": None, "touched": time.monotonic()}
                self._chat[run_id] = rec
            rec["touched"] = time.monotonic()
            rec["seq"] += 1
            seq = rec["seq"]
            rec["frames"].append((seq, ev))
            if len(rec["frames"]) > _CHAT_BUFFER_MAX:
                rec["frames"] = rec["frames"][-_CHAT_BUFFER_MAX:]
            frame = {"type": "chat", "runId": run_id, "seq": seq, "ev": ev}
            for conn in list(self._conns.values()):
                if f"chat:{run_id}" in conn.channels:
                    conn._try_put(frame)
            # A terminal frame ends the run: schedule the buffer for cleanup so a late
            # reconnect can still replay it briefly, then it's dropped.
            if ev.get("type") in ("done", "error"):
                rec["ended_at"] = True
                if _loop is not None:
                    _loop.call_later(_CHAT_BUFFER_TTL_SEC, self._chat.pop, run_id, None)

        self._on_loop(_do)

    def publish_stream(self, frame: dict) -> None:
        """Fan a transcript/audio frame out to every connection (streams are addressed by
        their own ``streamId`` inside the frame; only the originating client will match)."""
        self._publish("__stream__", frame, all_conns=True)

    def publish_kernel(self, snapshot: dict) -> None:
        """Fan a Work Kernel snapshot out to subscribers of the ``kernel`` channel. Stateless
        (no replay buffer): each frame is a full snapshot, so a late subscriber just gets the
        next one — the endpoint also sends one immediately on subscribe."""
        self._publish("kernel", {"type": "kernel", "snapshot": snapshot})

    def open_chat(self, run_id: str) -> None:
        """Register a run's buffer the moment it starts, BEFORE any frame is published.
        Without this, chat_exists() would be False until the first frame, so a mirror that
        subscribes in the gap (right after chat.run.started) gets bounced with chat.gone."""
        def _do() -> None:
            self._sweep_chat(incoming=0 if run_id in self._chat else 1)
            # Insert AFTER the sweep: the run being opened is the newest and must survive it,
            # even if the sweep just evicted others to make room.
            if run_id not in self._chat:
                self._chat[run_id] = {"frames": [], "seq": 0, "ended_at": None,
                                      "touched": time.monotonic()}
        self._on_loop(_do)

    def _sweep_chat(self, *, incoming: int = 0) -> None:
        """Drop chat buffers the terminal-frame timer will never collect.

        The happy path is still the ``call_later`` armed on done/error. This is the backstop for
        runs that end without one — cancelled, crashed, or opened by ``open_chat`` and never
        streamed — which otherwise pin their frames for the life of the process. Runs on
        publish/open (loop thread, no timer of its own): idle buffers go first, then the oldest
        if we are still over the run cap.

        ``incoming`` is how many entries the caller is about to add, so the cap holds *after*
        the insert rather than one over it.
        """
        now = time.monotonic()
        if _CHAT_IDLE_TTL_SEC > 0:
            for rid, rec in list(self._chat.items()):
                if now - float(rec.get("touched") or 0.0) >= _CHAT_IDLE_TTL_SEC:
                    self._chat.pop(rid, None)
        budget = max(0, _CHAT_MAX_RUNS - incoming)
        if len(self._chat) > budget:
            # Still over budget: evict oldest-touched first. A live run is touched by every
            # frame it publishes, so the ones shed here are the least recently active.
            for rid, _ in sorted(self._chat.items(),
                                 key=lambda kv: float(kv[1].get("touched") or 0.0)
                                 )[:len(self._chat) - budget]:
                self._chat.pop(rid, None)

    # ─── chat replay (loop thread) ────────────────────────────────────────────
    def chat_exists(self, run_id: str) -> bool:
        return run_id in self._chat

    def replay_chat(self, conn: Connection, run_id: str, since: int) -> None:
        """Send buffered frames with seq > since to one connection, in order."""
        rec = self._chat.get(run_id)
        if not rec:
            return
        for seq, ev in rec["frames"]:
            if seq > since:
                conn._try_put({"type": "chat", "runId": run_id, "seq": seq, "ev": ev})

    # ─── internals ────────────────────────────────────────────────────────────
    def _publish(self, channel: str, frame: dict, *, all_conns: bool = False) -> None:
        def _do() -> None:
            for conn in list(self._conns.values()):
                if all_conns or channel in conn.channels:
                    conn._try_put(frame)
        self._on_loop(_do)

    def _on_loop(self, fn) -> None:
        """Run ``fn`` on the event loop thread. If we're already on it, run inline."""
        if _loop is None:
            fn()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is _loop:
            fn()
        else:
            _loop.call_soon_threadsafe(fn)


hub = _Hub()
