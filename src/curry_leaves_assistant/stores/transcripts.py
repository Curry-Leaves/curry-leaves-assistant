"""Normalized, immutable transcripts — the provenance anchor for the knowledge base.

Each finalized recording is normalized into an indexed list of turns and stored as
``.transcripts/m-{sha1(text)[:8]}.json`` under the knowledge bundle. The id is
**deterministic**: re-transcribing identical speech yields the same ``meeting_id``,
so re-processing a recording is a no-op (idempotent ingest, spec invariant #3).

A turn is ``{i, speaker, t0, t1, text}``; ``turn_range: [a, b]`` in a note's
provenance points at ``turns[a..b]`` here. Whisper gives us no diarization, so
``speaker`` is ``None`` today — the shape is ready for it later.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from curry_leaves_assistant.core.paths import KNOWLEDGE_DIR
from curry_leaves_assistant.core.store import now_iso

TRANSCRIPTS_DIR = KNOWLEDGE_DIR / ".transcripts"


def meeting_id_for(text: str) -> str:
    """Deterministic meeting id from the transcript's normalized text."""
    h = hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:8]
    return f"m-{h}"


def path_for(meeting_id: str) -> Path:
    return TRANSCRIPTS_DIR / f"{meeting_id}.json"


def turns_from_segments(segments: list[dict]) -> list[dict]:
    """Index kept Whisper segments into turns (indices are the provenance anchor)."""
    turns = []
    for i, s in enumerate(segments):
        turns.append({
            "i": i,
            "speaker": s.get("speaker"),
            "t0": round(float(s.get("t0", 0.0) or 0.0), 2),
            "t1": round(float(s.get("t1", 0.0) or 0.0), 2),
            "text": (s.get("text") or "").strip(),
        })
    return turns


def store(rec_id: str, segments: list[dict], *, title: str | None = None,
          date: str | None = None) -> str:
    """Normalize + persist a transcript; return its deterministic ``meeting_id``.

    Idempotent: identical speech → identical id → overwrites the same file with the
    same bytes. Never mutated after the fact (immutable per invariant #1)."""
    turns = turns_from_segments(segments)
    full = " ".join(t["text"] for t in turns).strip()
    meeting_id = meeting_id_for(full or (title or rec_id))
    doc = {
        "meeting_id": meeting_id,
        "rec_id": rec_id,
        "title": title,
        "date": (date or now_iso())[:10],
        "created": now_iso(),
        "turns": turns,
    }
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path_for(meeting_id).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    tmp.replace(path_for(meeting_id))
    return meeting_id


def load(meeting_id: str) -> dict | None:
    p = path_for(meeting_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def span(meeting_id: str, turn_range: tuple[int, int] | list[int] | None) -> dict | None:
    """Resolve a provenance ref to its actual transcript span (text, speaker, times).

    This is the last hop of the provenance chain (note → meeting_id + turn_range →
    the words that were said). Returns None if the transcript or range is missing."""
    doc = load(meeting_id)
    if not doc:
        return None
    turns = doc.get("turns") or []
    if not turns:
        return {"meeting_id": meeting_id, "rec_id": doc.get("rec_id"),
                "title": doc.get("title"), "text": "", "speaker": None, "t0": None, "t1": None}
    if turn_range and len(turn_range) == 2:
        a, b = int(turn_range[0]), int(turn_range[1])
    else:
        a, b = 0, len(turns) - 1
    a = max(0, min(a, len(turns) - 1))
    b = max(a, min(b, len(turns) - 1))
    picked = turns[a:b + 1]
    speakers = [t.get("speaker") for t in picked if t.get("speaker")]
    return {
        "meeting_id": meeting_id,
        "rec_id": doc.get("rec_id"),
        "title": doc.get("title"),
        "turn_range": [a, b],
        "t0": picked[0].get("t0"),
        "t1": picked[-1].get("t1"),
        "speaker": speakers[0] if speakers else None,
        "text": " ".join(t.get("text") or "" for t in picked).strip(),
    }
