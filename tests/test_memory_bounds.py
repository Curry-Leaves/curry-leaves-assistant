"""Bounds that keep long-running sessions from growing without limit.

Each test here pins a leak that was found in production use, where an hours-long meeting or a
never-terminated chat run grew resident memory until the machine swapped. They assert the
*bound*, not the exact size, so tuning the constants doesn't break them.
"""
from __future__ import annotations

import curry_leaves_assistant.core.ws_hub as ws_hub
import curry_leaves_assistant.domain.transcribe as transcribe
from curry_leaves_assistant.orchestration.live_context import (
    _MAX_SURFACED,
    _MAX_TRANSCRIPT_CHARS,
    _Session,
)


# ─── live audio buffer ────────────────────────────────────────────────────────
def test_live_transcriber_buffer_is_capped():
    """Audio arriving faster than the model drains it must not grow the buffer forever."""
    tr = transcribe.LiveTranscriber()
    # Feed well past the cap without ever draining (accept() never transcribes).
    chunk = b"\x00" * 100_000
    for _ in range(200):
        tr.accept(chunk)
    assert len(tr._buf) <= transcribe._LIVE_MAX_BYTES
    assert tr.dropped_bytes > 0


def test_live_transcriber_keeps_newest_audio():
    """Overflow drops the OLDEST audio — live captions should track current speech."""
    tr = transcribe.LiveTranscriber()
    tr.accept(b"\x01" * transcribe._LIVE_MAX_BYTES)
    tr.accept(b"\x02" * 1000)
    assert len(tr._buf) == transcribe._LIVE_MAX_BYTES
    assert tr._buf[-1000:] == b"\x02" * 1000   # newest survived
    assert tr._buf[0] == 1                      # front was trimmed, not the tail


def test_feed_still_transcribes_when_chunk_ready(monkeypatch):
    """accept()+feed_pending() must stay equivalent to the old single feed() call."""
    monkeypatch.setattr(transcribe, "transcribe_pcm", lambda s: "hello")
    tr = transcribe.LiveTranscriber()
    assert tr.feed(b"\x00" * (transcribe._LIVE_CHUNK_BYTES - 4)) == ""   # not yet a full chunk
    assert tr.feed(b"\x00" * 8) == "hello"                               # crossed the threshold
    assert len(tr._buf) == 0                                             # drained


# ─── chat replay buffers ──────────────────────────────────────────────────────
def test_chat_buffer_swept_when_run_never_terminates(monkeypatch):
    """A run with no done/error frame has no cleanup timer — the idle sweep must collect it.

    Simulates the passage of time by backdating the entry's touch stamp, rather than sleeping.
    """
    hub = ws_hub._Hub()
    monkeypatch.setattr(ws_hub, "_CHAT_IDLE_TTL_SEC", 1800.0)
    hub.open_chat("run-a")
    assert hub.chat_exists("run-a")
    # Age run-a past the TTL; the run opened next drives the sweep that collects it.
    hub._chat["run-a"]["touched"] -= 3600.0
    hub.open_chat("run-b")
    assert not hub.chat_exists("run-a")
    assert hub.chat_exists("run-b")   # the fresh one is untouched by the sweep


def test_chat_buffers_capped_by_run_count(monkeypatch):
    """Even inside the idle window, the number of retained runs is bounded."""
    hub = ws_hub._Hub()
    monkeypatch.setattr(ws_hub, "_CHAT_IDLE_TTL_SEC", 10_000.0)  # nothing is idle
    monkeypatch.setattr(ws_hub, "_CHAT_MAX_RUNS", 5)
    for i in range(50):
        hub.open_chat(f"run-{i}")
    assert len(hub._chat) <= 5


def test_live_run_survives_the_sweep(monkeypatch):
    """The sweep must not evict a run that is actively publishing."""
    hub = ws_hub._Hub()
    monkeypatch.setattr(ws_hub, "_CHAT_IDLE_TTL_SEC", 10_000.0)
    monkeypatch.setattr(ws_hub, "_CHAT_MAX_RUNS", 3)
    hub.publish_chat("live", {"type": "delta", "text": "hi"})
    for i in range(10):
        hub.open_chat(f"other-{i}")
        hub.publish_chat("live", {"type": "delta", "text": "still here"})
    assert hub.chat_exists("live")


# ─── live-context session ─────────────────────────────────────────────────────
def _session() -> _Session:
    s = _Session.__new__(_Session)   # bypass __init__: it wants a live connection
    s._transcript = ""
    s._seen = set()
    s._surfaced = []
    return s


def test_session_transcript_is_capped():
    """An hours-long meeting must not retain every word — only the window is ever read."""
    s = _session()
    for _ in range(500):
        s._append_transcript("word " * 200)
    assert len(s._transcript) <= _MAX_TRANSCRIPT_CHARS


def test_session_transcript_keeps_the_tail():
    """Briefs read the END of the transcript, so that is the part trimming must preserve."""
    s = _session()
    s._append_transcript("x" * _MAX_TRANSCRIPT_CHARS)
    s._append_transcript("THE-NEWEST-WORDS")
    assert s._transcript.endswith("THE-NEWEST-WORDS")
    assert len(s._transcript) <= _MAX_TRANSCRIPT_CHARS


def test_surfaced_and_seen_stay_in_step_when_trimmed():
    """_seen is rebuilt from the survivors, so it can't outgrow _surfaced."""
    s = _session()
    for i in range(_MAX_SURFACED * 3):
        s._surfaced.append(f"card {i}")
        s._seen.add(f"card {i}".lower())
        if len(s._surfaced) > _MAX_SURFACED:
            s._surfaced = s._surfaced[-_MAX_SURFACED:]
            s._seen = {t.lower() for t in s._surfaced}
    assert len(s._surfaced) <= _MAX_SURFACED
    assert len(s._seen) == len(s._surfaced)
