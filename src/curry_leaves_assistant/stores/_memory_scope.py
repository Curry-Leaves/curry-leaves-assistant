"""One memory scope — a subject-deduped view over the app's single ``cl_memory`` bundle.

The engine behind ``agent_memory_store`` (per-agent private notes, ``type: private``), which keeps
its free-function API as a thin wrapper over this class.

There is no separate store any more: a scope is just *the notes of one type* (optionally owned by
one agent) inside ``domain/memory.bundle`` — the same bundle as the knowledge base and the
episodic records. That's what lets consolidation and ``trace()`` link a fact to the meeting that
produced it.

What this class owns is the *memory-scope* semantics cl_memory is deliberately generic about:
subject-based dedupe (upsert-in-place rather than append-a-duplicate) and the record view
(``id · type · subject · source · confidence · uses``).
"""
from __future__ import annotations

import re
import threading
import uuid
from typing import Any

from curry_leaves_assistant.core import memory_ref
from curry_leaves_assistant.core.store import now_iso

# A memory record's frontmatter, in emit order. `type` is cl_memory's required field; `agent`
# scopes a private note to its owner; the rest are ours and ride along as custom keys.
_FM_ORDER = ("type", "id", "agent", "subject", "source", "confidence", "uses",
             "createdAt", "updatedAt", "title", "description")


def _derive_subject(text: str) -> str:
    """Fallback subject when the caller didn't supply one: first few significant words."""
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    return " ".join(words[:4]).lower()[:60] or "misc"


class MemoryScope:
    """The notes of one ``type`` in the bundle, optionally owned by one ``agent``.

    ``dir_rel`` is where this scope's notes live (folders are for humans browsing the tree;
    ``type``/``agent`` are what the code filters on).
    """

    def __init__(self, *, dir_rel: str, type: str, id_prefix: str, agent: str | None = None) -> None:
        self.dir_rel = dir_rel.strip("/")
        self.type = type
        self.id_prefix = id_prefix
        self.agent = agent
        self._lock = threading.RLock()

    @property
    def _b(self) -> Any:
        """The app's one bundle, composed in domain/memory.py and handed down via core."""
        return memory_ref.get()

    def _path(self, rec_id: str) -> str:
        return f"{self.dir_rel}/{rec_id}.md"

    def _mine(self, fm: dict[str, Any]) -> bool:
        """Is this note in this scope? (right type, and — for private notes — right owner.)"""
        if fm.get("type") != self.type:
            return False
        return self.agent is None or fm.get("agent") == self.agent

    def _view(self, note: dict[str, Any]) -> dict[str, Any]:
        fm = note.get("frontmatter") or {}
        return {"id": fm.get("id"), "type": fm.get("type"), "subject": fm.get("subject"),
                "body": (note.get("body") or "").strip(), "source": fm.get("source"),
                "confidence": fm.get("confidence")}

    def _all(self) -> list[dict[str, Any]]:
        """Every note in this scope, as full note dicts."""
        b = self._b
        out: list[dict[str, Any]] = []
        for meta in b.notes(self.dir_rel, self.type):
            note = b.read(meta["path"])
            if note and self._mine(note.get("frontmatter") or {}):
                out.append(note)
        return out

    # ── public operations ───────────────────────────────────────────────────────
    def upsert(self, text: str, *, type: str | None = None, subject: str | None,
               source: str, confidence: float) -> dict:
        """Record or correct one record. Matches on ``subject`` (case-insensitive exact) and
        UPDATES in place — keeping id, uses and createdAt — instead of appending a near-duplicate,
        which is what keeps the scope from ballooning. ``_created`` flags a new record."""
        text = (text or "").strip()
        if not text:
            raise ValueError("empty text")
        subject = (subject or _derive_subject(text)).strip()
        kind = type or self.type
        with self._lock:
            existing = next((n for n in self._all()
                             if str((n["frontmatter"] or {}).get("subject") or "").lower()
                             == subject.lower()), None)
            now = now_iso()
            if existing:
                fm = dict(existing["frontmatter"])
                rec_id = fm.get("id") or existing["path"].rsplit("/", 1)[-1][:-3]
            else:
                rec_id = f"{self.id_prefix}{uuid.uuid4().hex[:12]}"
                fm = {"id": rec_id, "uses": 0, "createdAt": now}
            fm.update({"type": self.type, "subject": subject, "source": source,
                       "confidence": confidence, "updatedAt": now,
                       # `title`/`description` make the record legible in the memory UI and give
                       # the index something to weight above the body.
                       "title": subject, "description": text[:200]})
            if self.agent:
                fm["agent"] = self.agent
            fm.setdefault("uses", 0)
            fm.setdefault("createdAt", now)

            from cl_memory.util import dump_note
            self._b.write_raw(self._path(rec_id), dump_note(fm, text, order=list(_FM_ORDER)))
            return {"id": rec_id, "type": kind, "subject": subject, "source": source,
                    "confidence": confidence, "body": text, "uses": fm.get("uses", 0),
                    "createdAt": fm.get("createdAt"), "updatedAt": now,
                    "_created": existing is None}

    def recall(self, query: str, *, limit: int) -> list[dict]:
        """Ranked records matching a query — by MEANING when the embedder is ready, BM25
        otherwise. Scoped to this type (and owner)."""
        if not (query or "").strip():
            return []
        b = self._b
        try:
            # Over-fetch: recall ranks across the whole bundle before we filter to this scope.
            hits = b.recall(query, type=self.type, limit=max(limit * 4, 20))
        except Exception:
            return []
        out: list[dict] = []
        for h in hits:
            note = b.read(h["path"])
            if note and self._mine(note.get("frontmatter") or {}):
                out.append(self._view(note))
                if len(out) >= limit:
                    break
        return out

    def recent(self, limit: int) -> list[dict]:
        """Records newest-updated first, capped."""
        recs = sorted(self._all(),
                      key=lambda n: (n["frontmatter"] or {}).get("updatedAt") or "", reverse=True)
        return [self._view(n) for n in recs[:limit]]

    def list_all(self) -> list[dict]:
        """Every record with full metadata for the UI panel (newest-updated first)."""
        out = []
        for n in sorted(self._all(),
                        key=lambda n: (n["frontmatter"] or {}).get("updatedAt") or "",
                        reverse=True):
            fm = n["frontmatter"] or {}
            out.append({**self._view(n), "uses": fm.get("uses", 0),
                        "createdAt": fm.get("createdAt"), "updatedAt": fm.get("updatedAt")})
        return out

    def get(self, rec_id: str) -> dict | None:
        note = self._b.read(self._path(rec_id))
        if note is None or not self._mine(note.get("frontmatter") or {}):
            return None
        return note

    def forget(self, rec_id: str) -> bool:
        """Soft-delete a record (-> _archive/, restorable). False if it wasn't in this scope."""
        with self._lock:
            if self.get(rec_id) is None:
                return False
            return bool(self._b.delete(self._path(rec_id), "forgotten"))

    def touch(self, rec_ids: list[str]) -> None:
        """Bump `uses` on recalled records (best-effort; never breaks a read path)."""
        if not rec_ids:
            return
        with self._lock:
            for rec_id in rec_ids:
                try:
                    note = self.get(rec_id)
                    if note is None:
                        continue
                    uses = int((note["frontmatter"] or {}).get("uses") or 0)
                    self._b.upsert_meta(self._path(rec_id), {"uses": uses + 1})
                except Exception:
                    pass


__all__ = ["MemoryScope"]
