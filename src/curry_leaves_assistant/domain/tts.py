"""Local text-to-speech via Kokoro-82M (hexgrad/kokoro, Apache-2.0).

The mirror image of ``domain/transcribe.py``: where transcribe turns PCM into text,
this turns text into 24 kHz mono float32 PCM. One warm ``KPipeline`` handle is kept and
dropped on ``reset_model()``. All entry points are BLOCKING — call via
``asyncio.to_thread`` so the event loop stays responsive (``api/ws.py`` does).

Weights download into ~/.curry-leaves/models/kokoro/ (repo ``hexgrad/Kokoro-82M``),
same shape as the whisper backends and covered by the same backup exclusion. Kokoro's
phonemizer needs the system ``espeak-ng`` binary (apt: ``espeak-ng``; macOS:
``brew install espeak-ng``); without it synthesis raises and we surface the error.
"""
from __future__ import annotations

import contextlib
import os
import re
import threading
import warnings
from typing import Iterable, Iterator


@contextlib.contextmanager
def _quiet_kokoro_load():
    """Silence Kokoro's two known-benign load-time warnings — an LSTM built with
    ``dropout`` at ``num_layers=1`` and torch's ``weight_norm`` deprecation. Both come from
    inside the vocoder's own construction (nothing we can change) and print on every boot's warm.
    Scoped to just the model build, so genuine warnings elsewhere still surface."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=r".*dropout option adds dropout.*")
        warnings.filterwarnings("ignore", message=r".*weight_norm.*is deprecated.*")
        yield

import numpy as np

from curry_leaves_assistant.core import settings as app_settings
from curry_leaves_assistant.core.paths import MODELS_DIR

# Kokoro emits audio at a fixed 24 kHz mono. The browser AudioContext resamples on
# playback, so we ship this rate on the frame and let Web Audio handle it.
SAMPLE_RATE = 24_000

KOKORO_REPO = "hexgrad/Kokoro-82M"

# lang_code → default voice. Kokoro packs voices per language; 'a' = American English.
# See https://github.com/hexgrad/kokoro for the full voice list.
DEFAULT_LANG = "a"
DEFAULT_VOICE = "af_heart"


def model_dir():
    return MODELS_DIR / "kokoro"


def list_voices() -> list[str]:
    """Voice ids present on disk (Kokoro ships one .pt per voice under voices/).

    Read from the filesystem rather than a hardcoded table so the picker can't drift from
    what the installed model actually offers. Empty until the weights are downloaded.
    """
    d = model_dir() / "voices"
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.pt"))


def is_downloaded() -> bool:
    d = model_dir()
    if not d.is_dir():
        return False
    files = {f.name for f in d.rglob("*")}
    # The repo ships the model weights + a config; a voices/ dir holds the voice packs.
    return any(f.endswith((".pth", ".safetensors")) for f in files) and "config.json" in files


def download_model() -> bool:
    """Fetch the Kokoro weights + voices into ~/.curry-leaves/models/kokoro/."""
    from huggingface_hub import snapshot_download
    snapshot_download(repo_id=KOKORO_REPO, local_dir=str(model_dir()))
    return is_downloaded()


def warm() -> None:
    """Fetch the weights (if absent) and build the pipeline, so the first `speak()` doesn't
    pay for it. Cold, that cost is ~7s of dead air *after* the user has already spoken —
    long enough to read as "voice chat is broken". Blocking and best-effort: call from a
    thread off the boot path (app.py does), and let any failure fall through to the lazy
    path in synth_pcm, which surfaces a real error to the client."""
    if not available():
        return
    if not is_downloaded():
        download_model()
    with _lock:
        _get_pipeline(active_lang())


def active_voice() -> str:
    # The wakeword block is the settable one — patch_recording whitelists four keys and
    # silently drops ttsVoice, so that path can only ever be set from the environment.
    return (app_settings.wakeword_cfg().get("voice")
            or app_settings.recording_cfg().get("ttsVoice")
            or os.environ.get("CURRY_LEAVES_TTS_VOICE") or DEFAULT_VOICE)


def active_lang() -> str:
    return (app_settings.recording_cfg().get("ttsLang")
            or os.environ.get("CURRY_LEAVES_TTS_LANG") or DEFAULT_LANG)


# ─── one warm KPipeline, dropped on reset_model() ─────────────────────────────
_pipeline = None
_pipeline_key: str | None = None
_lock = threading.Lock()


def reset_model() -> None:
    """Drop the warm pipeline so the next synthesis reloads (e.g. after a voice change)."""
    global _pipeline, _pipeline_key
    with _lock:
        _pipeline = None
        _pipeline_key = None


def _local_files() -> tuple[str, str] | None:
    """The downloaded (config.json, weights) pair, or None if we haven't fetched them."""
    if not is_downloaded():
        return None
    d = model_dir()
    config = d / "config.json"
    weights = next((p for p in sorted(d.rglob("*")) if p.suffix in (".pth", ".safetensors")), None)
    if not config.is_file() or weights is None:
        return None
    return str(config), str(weights)


def _build_model():
    """A KModel bound to our local weights when we have them, else None to let Kokoro fetch.

    ``repo_id`` must stay the HF id (Kokoro feeds it to ``hf_hub_download`` and looks it up in
    ``KModel.MODEL_NAMES``) — a local path there raises HFValidationError. Local files are
    passed *separately* as ``config``/``model``, which is what keeps this offline once warm."""
    local = _local_files()
    if local is None:
        return None
    from kokoro import KModel
    config, weights = local
    with _quiet_kokoro_load():
        return KModel(repo_id=KOKORO_REPO, config=config, model=weights)


def _get_pipeline(lang: str):
    """Return a warm KPipeline for ``lang``, building (and caching) it on first use.
    Caller must hold ``_lock``."""
    global _pipeline, _pipeline_key
    if _pipeline is not None and _pipeline_key == lang:
        return _pipeline
    from kokoro import KPipeline
    model = _build_model()
    with _quiet_kokoro_load():
        _pipeline = (KPipeline(lang_code=lang, repo_id=KOKORO_REPO, model=model) if model is not None
                     else KPipeline(lang_code=lang, repo_id=KOKORO_REPO))
    _pipeline_key = lang
    return _pipeline


def available() -> bool:
    """True if the kokoro package imports — the frontend gates the Voice button on this
    (surfaced via a health/capability check)."""
    try:
        import kokoro  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 — a broken install (e.g. missing torch) also means unavailable
        return False


# ─── markdown → speakable prose ───────────────────────────────────────────────
# Replies are markdown, but espeak-ng phonemizes punctuation literally: "**Bold**" comes out
# as "asterisk asterisk bold asterisk asterisk", `code` as "backtick", and a bare URL as
# "aitch tee tee pee colon slash slash…". Everything below is stripped to what a person would
# actually say. Order matters: links/images before emphasis (their brackets nest), fences
# before inline code.
_MD_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"```[\w-]*\n.*?```", re.S), " (code block) "),   # fenced code: don't read it aloud
    (re.compile(r"~~~[\w-]*\n.*?~~~", re.S), " (code block) "),
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),               # image → alt text
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),                # link → label, drop the URL
    (re.compile(r"^\s{0,3}#{1,6}\s+", re.M), ""),                 # heading marks
    (re.compile(r"^\s{0,3}>\s?", re.M), ""),                      # blockquote marks
    (re.compile(r"^\s{0,3}(?:[-*_]\s*){3,}$", re.M), ""),         # horizontal rules
    (re.compile(r"^(\s*)[-*+]\s+", re.M), r"\1"),                 # bullet marks (keep the text)
    (re.compile(r"\*\*\*(.+?)\*\*\*", re.S), r"\1"),              # bold+italic
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),                  # bold
    (re.compile(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", re.S), r"\1"),  # italic (not a*b)
    (re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", re.S), r"\1"),    # italic via underscore
    (re.compile(r"~~(.+?)~~", re.S), r"\1"),                      # strikethrough
    (re.compile(r"`([^`]+)`"), r"\1"),                            # inline code → bare text
    (re.compile(r"^\s*\|.*\|\s*$", re.M), " "),                   # table rows: unreadable aloud
    (re.compile(r"<[^>]+>"), ""),                                 # stray html tags
    (re.compile(r"https?://\S+"), " (link) "),                    # bare URLs
    (re.compile(r"[ \t]{2,}"), " "),
    (re.compile(r"\n{2,}"), "\n"),
]


def speakable(text: str) -> str:
    """Strip markdown so TTS reads prose, not punctuation. Idempotent; safe on plain text."""
    out = text or ""
    for pattern, repl in _MD_RULES:
        out = pattern.sub(repl, out)
    return out.strip()


def synth_pcm(text: str, voice: str | None = None, lang: str | None = None) -> np.ndarray:
    """Synthesize ``text`` to a single float32 mono PCM array at SAMPLE_RATE. Blocking.

    Markdown is stripped first — this is the one chokepoint every synthesis path goes
    through, so callers never have to remember to sanitize."""
    text = speakable(text)
    if not text:
        return np.zeros(0, dtype=np.float32)
    voice = voice or active_voice()
    lang = lang or active_lang()
    with _lock:
        pipeline = _get_pipeline(lang)
        chunks: list[np.ndarray] = []
        for _, _, audio in pipeline(text, voice=voice):
            chunks.append(np.asarray(audio, dtype=np.float32))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks)


# A sentence boundary: end punctuation (optionally followed by a closing quote/paren)
# then whitespace. Used to synthesize a streaming reply one utterance at a time so the
# first words start playing before the whole message is generated.
_SENT_SPLIT = re.compile(r"(?<=[.!?。！？])[\"')\]]*\s+")


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text or "")]
    return [p for p in parts if p]


def synth_sentence_pcm(sentence: str, voice: str | None = None,
                       lang: str | None = None) -> np.ndarray:
    """Alias for synth_pcm with sentence semantics — one utterance → one PCM block."""
    return synth_pcm(sentence, voice=voice, lang=lang)


def stream_sentences(text_iter: Iterable[str], voice: str | None = None,
                     lang: str | None = None) -> Iterator[np.ndarray]:
    """Consume a stream of *text deltas*, buffer to sentence boundaries, and yield one
    PCM block per completed sentence (plus any trailing tail on exhaustion). Lets a chat
    reply speak as it streams instead of waiting for the whole message. Blocking; drive
    the produced generator from a thread."""
    buf = ""
    for delta in text_iter:
        buf += delta or ""
        sents = split_sentences(buf)
        if len(sents) > 1:
            # All but the last are complete; the last may be a partial sentence still growing.
            *complete, buf = sents
            # Re-derive the unconsumed tail: split_sentences strips separators, so rebuild
            # from the remaining buffer to keep whitespace/partial punctuation intact.
            for s in complete:
                pcm = synth_pcm(s, voice=voice, lang=lang)
                if pcm.size:
                    yield pcm
    tail = buf.strip()
    if tail:
        pcm = synth_pcm(tail, voice=voice, lang=lang)
        if pcm.size:
            yield pcm
