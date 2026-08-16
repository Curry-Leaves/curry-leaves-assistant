"""Local transcription with two selectable backends:

  • mlx-whisper    — Apple-Silicon accelerated (mlx-community Whisper repos)
  • faster-whisper — CTranslate2 CPU/int8 (Systran repos; cross-platform)

Models download into ~/.curry-leaves/models/<backend>-<name>/. The active backend +
model + language come from settings (Recording section). ``transcribe_*`` are
blocking — call via asyncio.to_thread so the event loop stays responsive.
"""
from __future__ import annotations

import os
import threading
import traceback
from typing import Any

from curry_leaves_assistant.domain import recordings

from curry_leaves_assistant.core import settings as app_settings

from curry_leaves_assistant.core.paths import MODELS_DIR

# Selectable MLX Whisper models (name → mlx-community HF repo + approx size).
MLX_MODEL_REGISTRY = {
    "tiny":            {"repo": "mlx-community/whisper-tiny-mlx",        "sizeLabel": "~75 MB"},
    "base":            {"repo": "mlx-community/whisper-base-mlx",        "sizeLabel": "~145 MB"},
    "small":           {"repo": "mlx-community/whisper-small-mlx",       "sizeLabel": "~480 MB"},
    "medium":          {"repo": "mlx-community/whisper-medium-mlx",      "sizeLabel": "~1.5 GB"},
    "large-v3":        {"repo": "mlx-community/whisper-large-v3-mlx",    "sizeLabel": "~3.0 GB"},
    "distil-large-v3": {"repo": "mlx-community/distil-whisper-large-v3", "sizeLabel": "~1.5 GB"},
}

# Selectable faster-whisper models (name → Systran CTranslate2 repo + approx size).
FW_MODEL_REGISTRY = {
    "tiny":            {"repo": "Systran/faster-whisper-tiny",             "sizeLabel": "~75 MB"},
    "base":            {"repo": "Systran/faster-whisper-base",             "sizeLabel": "~145 MB"},
    "small":           {"repo": "Systran/faster-whisper-small",            "sizeLabel": "~970 MB"},
    "medium":          {"repo": "Systran/faster-whisper-medium",           "sizeLabel": "~1.5 GB"},
    "large-v3":        {"repo": "Systran/faster-whisper-large-v3",         "sizeLabel": "~3.1 GB"},
    "distil-large-v3": {"repo": "Systran/faster-distil-whisper-large-v3",  "sizeLabel": "~1.5 GB"},
}

REGISTRIES = {"mlx-whisper": MLX_MODEL_REGISTRY, "faster-whisper": FW_MODEL_REGISTRY}


def _mlx_available() -> bool:
    try:
        import mlx_whisper  # noqa: F401
        return True
    except ImportError:
        return False


# mlx-whisper only builds on Apple Silicon; everywhere else (Linux containers,
# Intel Macs) fall back to faster-whisper so a fresh install has a working default
# instead of crashing on first transcription.
DEFAULT_BACKEND = "mlx-whisper" if _mlx_available() else "faster-whisper"

# Back-compat alias — earlier code/comments referenced transcribe.MODEL_REGISTRY.
MODEL_REGISTRY = MLX_MODEL_REGISTRY


def _registry(backend: str) -> dict:
    return REGISTRIES.get(backend, MLX_MODEL_REGISTRY)


def model_dir(name: str, backend: str = DEFAULT_BACKEND):
    return MODELS_DIR / f"{backend}-{name}"


def is_downloaded(name: str, backend: str = DEFAULT_BACKEND) -> bool:
    d = model_dir(name, backend)
    if not d.is_dir():
        return False
    files = {f.name for f in d.iterdir()}
    if "config.json" not in files:
        return False
    if backend == "faster-whisper":
        return "model.bin" in files
    return "weights.npz" in files or any(f.endswith(".safetensors") for f in files)


def active_backend() -> str:
    b = app_settings.recording_cfg().get("backend") or os.environ.get("CURRY_LEAVES_BACKEND", DEFAULT_BACKEND)
    return b if b in REGISTRIES else DEFAULT_BACKEND


def active_model_name() -> str:
    name = app_settings.recording_cfg().get("model") or os.environ.get("CURRY_LEAVES_WHISPER_MODEL", "small")
    return name if name in _registry(active_backend()) else "small"


def active_language() -> str | None:
    lang = app_settings.recording_cfg().get("language") or os.environ.get("CURRY_LEAVES_LANG", "en")
    return None if lang in ("", "auto") else lang  # None → Whisper auto-detects


_UNSET: Any = object()  # sentinel: "no per-recording override given" (distinct from "auto-detect")


def _resolve_language(override: Any) -> str | None:
    """override is _UNSET (use the global Settings default) or a per-recording
    value ("", "auto", a code, or None) which always wins when given."""
    if override is _UNSET:
        return active_language()
    return None if override in ("", "auto", None) else override


def active_vocabulary() -> str:
    """Names/jargon the user saved in Settings → Recording, biasing Whisper's
    word recognition via initial_prompt (mlx) / initial_prompt+hotwords (faster-whisper)."""
    return (app_settings.recording_cfg().get("vocabulary") or "").strip()


def list_models() -> list[dict]:
    backend, active = active_backend(), active_model_name()
    out: list[dict] = []
    for b, reg in REGISTRIES.items():
        for n, m in reg.items():
            out.append({
                "name": n,
                "backend": b,
                "sizeLabel": m["sizeLabel"],
                "downloaded": is_downloaded(n, b),
                "active": n == active and b == backend,
            })
    return out


def download_model(name: str, backend: str = DEFAULT_BACKEND) -> bool:
    """Download a Whisper model into ~/.curry-leaves/models/<backend>-<name>/."""
    reg = _registry(backend)
    if name not in reg:
        return False
    from huggingface_hub import snapshot_download
    snapshot_download(repo_id=reg[name]["repo"], local_dir=str(model_dir(name, backend)))
    return is_downloaded(name, backend)


# ─── warm model handles, dropped on reset_model() ─────────────────────────────
_fw_model = None
_fw_model_key: str | None = None
_fw_lock = threading.Lock()

# Only ONE transcription decodes at a time, whichever backend is active. Both backends cache
# the loaded model (faster-whisper via _fw_model below, mlx via its own ModelHolder), so the
# weights were never the whole story — but each concurrent decode still allocates its own
# activations, mel spectrogram, and KV cache on top, and on Apple Silicon that lands in unified
# memory the OS cannot swap. Several recordings finalizing together (or a boot recovering stale
# drafts) was enough to exhaust the machine. Whisper saturates the accelerator anyway, so
# serializing costs little throughput. Every transcription path funnels through _segments(), so
# this one gate covers finalize, recover, audio-replace, the clip endpoint, and live streams.
_decode_gate = threading.Semaphore(int(os.environ.get("CURRY_LEAVES_TRANSCRIBE_CONCURRENCY", "1")))


def reset_model() -> None:
    """Drop any warm backend handle so the next transcription reloads.

    Covers both backends. mlx keeps its cached model in its own module-level ModelHolder rather
    than here, so switching model or backend in Settings used to leave the previous mlx weights
    resident for the life of the process — clearing it is what actually frees them."""
    global _fw_model, _fw_model_key
    with _fw_lock:
        _fw_model = None
        _fw_model_key = None
    try:
        from mlx_whisper.transcribe import ModelHolder  # type: ignore[import-untyped]
        ModelHolder.model = None
        ModelHolder.model_path = None
    except Exception:
        pass  # mlx not installed (non-Apple-Silicon), or its internals moved — nothing to drop


def _mlx_model_path() -> str:
    """Local dir if downloaded, else the HF repo (mlx_whisper fetches on demand)."""
    name = active_model_name()
    return str(model_dir(name, "mlx-whisper")) if is_downloaded(name, "mlx-whisper") \
        else MLX_MODEL_REGISTRY[name]["repo"]


def _fw_model_path() -> str:
    name = active_model_name()
    return str(model_dir(name, "faster-whisper")) if is_downloaded(name, "faster-whisper") \
        else FW_MODEL_REGISTRY[name]["repo"]


def _filter_segments(segments) -> list[dict]:
    """Drop no-speech / low-confidence segments (Whisper hallucinates on silence) and
    keep the surviving ones as timed turns ``{t0, t1, text}`` — the provenance anchor."""
    out: list[dict] = []
    for s in segments:
        if (s.get("no_speech_prob", 0.0) or 0.0) > 0.6:
            continue
        if (s.get("avg_logprob", 0.0) or 0.0) < -1.0:
            continue
        t = (s.get("text") or "").strip()
        if t:
            out.append({"t0": float(s.get("start", 0.0) or 0.0),
                        "t1": float(s.get("end", 0.0) or 0.0), "text": t})
    return out


def _join(segs: list[dict]) -> str:
    """Flatten timed turns into one string, collapsing immediately-repeated words."""
    words = " ".join(s["text"] for s in segs).split()
    out: list[str] = []
    for w in words:
        if out and out[-1].lower() == w.lower():
            continue
        out.append(w)
    return " ".join(out).strip()


def _clean(segments) -> str:
    """Back-compat: filter + flatten to a single transcript string."""
    return _join(_filter_segments(segments))


# Whisper's decoder holds 448 tokens and caps the prompt at half that (224), truncating
# from the FRONT with no error. Overflow therefore silently drops terms — and drops the
# ones we put first. Budget below the cap so the hand-typed Settings vocabulary and the
# per-recording extras always survive; learned terms only spend what's left over.
_PROMPT_TOKEN_BUDGET = 200
_CHARS_PER_TOKEN = 3  # conservative: proper nouns fragment worse than prose


def _est_tokens(text: str) -> int:
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def _vocab(extra: str | None) -> str:
    """The prompt biasing Whisper's recognition, in priority order:
    Settings vocabulary → per-recording extras (attendees) → terms learned from notes.

    Order is load-bearing. What the user typed by hand and who is in *this* meeting
    outrank a general learned term, and because overflow truncates from the front, the
    learned tail is what gets cut — which is the right thing to lose.
    """
    from curry_leaves_assistant.stores import vocabulary_store

    parts = [p for p in (active_vocabulary(), (extra or "").strip()) if p]
    spent = sum(_est_tokens(p) + 1 for p in parts)
    # Skip learned terms already present above — an attendee named in this meeting is
    # very often also a learned term, and repeating it just burns budget another one needs.
    already = {w.lower().strip(".,;:") for p in parts for w in p.split()}
    learned = [t for t in vocabulary_store.top_terms(max(0, _PROMPT_TOKEN_BUDGET - spent))
               if t.lower() not in already]
    if learned:
        parts.append(" ".join(learned))
    return " ".join(parts).strip()


def _segments_mlx(audio, language: Any = _UNSET, vocabulary: str | None = None) -> list[dict]:
    import mlx_whisper  # lazy — heavy
    # No warm handle is kept here on purpose: mlx_whisper caches the decoded model itself, in
    # ModelHolder, keyed by path — so repeated calls already reuse one copy. (transcribe() takes
    # `path_or_hf_repo`, NOT a model object; anything else lands in **decode_options and is
    # passed down to the decoder, which breaks the call.) What we DO need is _decode_gate in
    # _segments: ModelHolder holds one model, but concurrent callers each build their own
    # activations and KV cache on top of it, and that is what exhausted memory.
    result = mlx_whisper.transcribe(
        audio, path_or_hf_repo=_mlx_model_path(),
        language=_resolve_language(language), condition_on_previous_text=False,
        initial_prompt=_vocab(vocabulary) or None,
    )
    return _filter_segments(result.get("segments", []))


def _load_fw():
    from faster_whisper import WhisperModel  # lazy — heavy
    global _fw_model, _fw_model_key
    path = _fw_model_path()
    with _fw_lock:
        if _fw_model is None or _fw_model_key != path:
            _fw_model = WhisperModel(path, device="cpu", compute_type="int8")
            _fw_model_key = path
        return _fw_model


def _segments_fw(audio, language: Any = _UNSET, vocabulary: str | None = None) -> list[dict]:
    model = _load_fw()
    vocab = _vocab(vocabulary)
    segments, _info = model.transcribe(
        audio, language=_resolve_language(language),
        condition_on_previous_text=False, vad_filter=True,
        initial_prompt=vocab or None, hotwords=vocab or None,
    )
    segs = [
        {"text": s.text, "start": s.start, "end": s.end,
         "no_speech_prob": s.no_speech_prob, "avg_logprob": s.avg_logprob}
        for s in segments
    ]
    return _filter_segments(segs)


def _segments(audio, language: Any = _UNSET, vocabulary: str | None = None) -> list[dict]:
    """Transcribe to timed turns ``[{t0, t1, text}]`` — the normalized-transcript unit.

    ``language`` overrides the global Settings default for this call only (e.g. a
    per-recording language pick); leave unset to use ``active_language()``. ``vocabulary``
    adds per-recording bias words (e.g. attendee names) on top of the Settings vocabulary.

    Serialized by ``_decode_gate`` — see its definition for why. This is the single chokepoint
    every transcription path reaches, so the bound holds no matter who calls.
    """
    with _decode_gate:
        if active_backend() == "faster-whisper":
            return _segments_fw(audio, language, vocabulary)
        return _segments_mlx(audio, language, vocabulary)


def _transcribe(audio) -> str:
    return _join(_segments(audio))


def transcribe_file(path: str) -> str:
    return _transcribe(path)


def probe_duration(path: str) -> float | None:
    """Seconds of audio in a media file, by decoding and counting samples. Returns None if
    the file can't be opened/decoded. Used to recover crashed drafts whose webm header never
    got a duration written — byte size doesn't tell you (webm is VBR), so we count for real.
    Costs a decode pass; only called on the handful of stale drafts at boot, never on the
    request path."""
    try:
        import av
        container = av.open(path)
        streams = container.streams.audio
        if not streams:
            container.close()
            return None
        stream = streams[0]
        samples = sum(getattr(frame, "samples", 0) for frame in container.decode(stream))
        rate = getattr(stream, "rate", None) or 16000
        container.close()
        return samples / float(rate)
    except Exception:
        return None


def transcribe_pcm(samples) -> str:
    """Transcribe a float32 mono 16kHz numpy buffer (live dictation/transcript)."""
    if samples is None or len(samples) < 1600:  # <0.1s of audio → nothing useful
        return ""
    return _transcribe(samples)


# Bytes per chunk (float32 * 16kHz * seconds) before we transcribe an incremental slice;
# bigger → better accuracy, more latency. Half a second is the floor worth transcribing.
_LIVE_CHUNK_BYTES = int(16000 * 4 * float(os.environ.get("CURRY_LEAVES_LIVE_CHUNK_SEC", "8")))
_LIVE_MIN_BYTES = int(16000 * 4 * 0.5)
# Hard ceiling on undrained live audio (default 60s ≈ 3.8 MB). Mic input arrives in real time
# but transcription is not guaranteed to keep up — it now queues behind _decode_gate, and a
# slow model can fall behind indefinitely. Without a cap the buffer grows for the whole meeting
# and the backlog can never be worked off. Past this we drop the OLDEST audio: losing the
# stalest seconds keeps live captions tracking what is being said now, which is what the
# feature is for. The finalized recording is transcribed separately from the full audio file
# on disk, so nothing dropped here is lost from the permanent transcript.
_LIVE_MAX_BYTES = int(16000 * 4 * float(os.environ.get("CURRY_LEAVES_LIVE_MAX_SEC", "60")))


class LiveTranscriber:
    """Accumulates float32-PCM bytes for one live stream and transcribes a chunk whenever
    enough audio has arrived. Transport-free: ``feed`` / ``flush`` return the recognized
    text (or "") and the caller delivers it. One instance per live stream.

    The buffer is capped (``_LIVE_MAX_BYTES``); overflow discards the oldest audio."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.dropped_bytes = 0  # observability: how much live audio we shed under backpressure

    def accept(self, data: bytes) -> None:
        """Buffer PCM bytes without transcribing. Cheap and non-blocking — safe to call from
        the socket read loop on every inbound frame. Overflow drops the oldest audio."""
        self._buf.extend(data)
        if len(self._buf) > _LIVE_MAX_BYTES:
            # Keep the newest _LIVE_MAX_BYTES; the front is the stalest audio.
            excess = len(self._buf) - _LIVE_MAX_BYTES
            del self._buf[:excess]
            self.dropped_bytes += excess

    def feed_pending(self) -> str:
        """Transcribe the buffer if a full chunk has accumulated, else "". Pairs with
        ``accept``: the caller buffers on the loop thread and calls this from a worker.
        Runs the (blocking) model inline — call under ``asyncio.to_thread``."""
        if len(self._buf) >= _LIVE_CHUNK_BYTES:
            return self._drain()
        return ""

    def feed(self, data: bytes) -> str:
        """Append PCM bytes; transcribe + return text once a full chunk has accumulated,
        else "". Runs the (blocking) model inline — call under ``asyncio.to_thread``."""
        self.accept(data)
        return self.feed_pending()

    def flush(self) -> str:
        """Transcribe whatever remains (the final tail). Returns text or ""."""
        return self._drain()

    def _drain(self) -> str:
        import numpy as np
        if len(self._buf) < _LIVE_MIN_BYTES:
            self._buf.clear()
            return ""
        samples = np.frombuffer(bytes(self._buf), dtype=np.float32)
        self._buf.clear()
        return transcribe_pcm(samples)


def _learn_from_notes(rec_id: str, meta: dict) -> None:
    """Fold this recording's user-written text into the learned vocabulary.

    Notes and attendee names only — never the transcript, never agent output. The value
    of this signal is that a human typed it; machine text would just recycle Whisper's
    errors. Idempotent per recording, so this is safe alongside the same call on every
    note PATCH. Never raises: a vocabulary miss must not cost us the transcript.
    """
    try:
        from curry_leaves_assistant.stores import vocabulary_store
        sources = [meta.get("notes") or ""]
        sources += [a for a in (meta.get("attendees") or []) if isinstance(a, str)]
        text = "\n".join(s for s in sources if s.strip())
        if text.strip():
            vocabulary_store.learn(text, source=rec_id)
    except Exception:
        print(f"[transcribe] vocabulary learn failed:\n{traceback.format_exc()}", flush=True)


def transcribe_recording(rec_id: str) -> str | None:
    """Blocking. Transcribe a finalized recording, persist the flat transcript AND the
    normalized indexed transcript (turns) that anchors knowledge-base provenance."""
    audio = recordings.current_audio_path(rec_id)
    if not audio.exists() or audio.stat().st_size == 0:
        return None
    try:
        meta = recordings.get(rec_id) or {}
        language = meta.get("language") if meta.get("language") else _UNSET
        # Learn from what the user *typed* before transcribing, so terms pinned during
        # this meeting bias this meeting's own transcript. Notes are the right source:
        # they're the correct spelling of exactly the words Whisper mangles. (Mining the
        # transcript instead would feed Whisper's own mistakes back to it.)
        _learn_from_notes(rec_id, meta)
        # Bias recognition toward the meeting's attendee names (on top of Settings vocab).
        attendees = " ".join(a for a in (meta.get("attendees") or []) if isinstance(a, str))
        segs = _segments(str(audio), language=language, vocabulary=attendees or None)
        text = _join(segs)
        recordings.save_transcript(rec_id, text or "(no speech detected)", segments=segs)
        return text
    except Exception:
        print(f"[transcribe] failed for {rec_id}:\n{traceback.format_exc()}", flush=True)
        return None
