"""Global search — one endpoint that fans out across every silo (knowledge, recordings,
todos, reminders, artifacts, assistants) so the ⌘K palette can find anything from one box.
The chat composer's @-mention menu shares it, scoped to one silo via `types=` and falling
back to that silo's recents when nothing has been typed yet.

Knowledge rides its BM25 FTS index (real full-text over note bodies); the other silos are
small per-user collections, so a case-insensitive substring match over their titles/text
(scored by match position + recency) is plenty and needs no extra index. Results are typed
so the frontend can route each hit to the right screen.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from curry_leaves_assistant.core import paths
from curry_leaves_assistant.domain import knowledge, recordings
from curry_leaves_assistant.stores import agent_store, artifact_store, data

router = APIRouter(tags=["search"])

# Per-type caps so no single silo floods the palette; knowledge gets the most since it's
# the only true full-text corpus.
_CAPS = {"knowledge": 8, "recording": 5, "todo": 4, "reminder": 4, "artifact": 4, "agent": 4}


def _score(hay: str, q: str) -> int:
    """Cheap relevance for substring silos: title hit beats body hit, earlier beats later,
    exact/prefix beats mid-word. Returns 0 for no match so callers can drop it."""
    h = hay.lower()
    i = h.find(q)
    if i < 0:
        return 0
    s = 100
    if h == q:
        s += 100
    elif h.startswith(q):
        s += 60
    elif i == 0 or not h[i - 1].isalnum():
        s += 30  # word-boundary hit
    return s - min(i, 40)  # earlier match ranks higher


def _clip(text: str, q: str, width: int = 90) -> str:
    """A snippet window centered on the match, so the user sees why it matched."""
    if not text:
        return ""
    low = text.lower()
    i = low.find(q)
    if i < 0:
        return text[:width].strip()
    start = max(0, i - width // 3)
    end = min(len(text), start + width)
    frag = text[start:end].strip()
    return ("…" if start > 0 else "") + frag + ("…" if end < len(text) else "")


def _recents(kinds: set[str], limit: int) -> list[dict]:
    """Newest items of one silo, for an empty query. The chat composer's @-menu opens on a
    bare `@` with nothing typed yet — "show me my recent recordings" is the whole point, so
    it can't use the scored path (there's nothing to score against)."""
    out: list[dict] = []
    if "recording" in kinds:
        for r in recordings.list_recordings()[:limit]:  # already newest-first
            out.append({
                "type": "recording", "id": r.get("id"),
                "title": r.get("name") or "Untitled recording",
                "subtitle": "recording", "snippet": "",
                "date": r.get("createdAt"), "score": 0,
            })
    if "todo" in kinds:
        for t in sorted(data.list_todos(), key=lambda t: t.get("createdAt") or "", reverse=True):
            if t.get("done"):
                continue  # a done todo is rarely what you mean to talk about
            out.append({
                "type": "todo", "id": t.get("id"), "title": t.get("text") or "todo",
                "subtitle": "due " + t["dueDate"] if t.get("dueDate") else "todo",
                "snippet": "", "date": t.get("createdAt"), "score": 0,
            })
            if len(out) >= limit:
                break
    if "reminder" in kinds:
        for rm in sorted(data.list_reminders(), key=lambda r: r.get("dueAt") or "", reverse=True)[:limit]:
            out.append({
                "type": "reminder", "id": rm.get("id"), "title": rm.get("title") or "reminder",
                "subtitle": "reminder", "snippet": "", "date": rm.get("dueAt"), "score": 0,
            })
    if "knowledge" in kinds:
        # list_notes() carries no timestamps and comes back in filesystem order, so "recent"
        # has to come from the file's own mtime — otherwise this list is arbitrary.
        notes = knowledge.list_notes()
        stamped = []
        for n in notes:
            try:
                mtime = (paths.KNOWLEDGE_DIR / n["path"]).stat().st_mtime
            except OSError:
                mtime = 0.0
            stamped.append((mtime, n))
        stamped.sort(key=lambda t: t[0], reverse=True)
        for mtime, n in stamped[:limit]:
            out.append({
                "type": "knowledge", "id": n.get("path"),
                "title": n.get("title") or n.get("path") or "note",
                "subtitle": n.get("type") or "note", "snippet": "",
                "date": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat() if mtime else None,
                "score": 0,
            })
    return out[:limit]


@router.get("/search")
def global_search(q: str, limit: int = 24, types: str | None = None):
    """Aggregate hits across silos for one query. Returns a flat list of typed results,
    each: {type, id, title, subtitle, snippet, date}. `id` is what the frontend's open-
    handler for that type needs (note path, recording id, etc.).

    `types`: optional comma-separated allowlist ("recording,todo") — filtered-out silos are
    never queried. Used by the chat composer's @-menu to scope to one kind; the ⌘K palette
    passes nothing and searches everything, as before.

    With `types` naming a single silo, an EMPTY query returns that silo's most recent items
    instead of nothing — an @-menu opens before the user has typed anything."""
    query = (q or "").strip()
    kinds = {t.strip() for t in types.split(",") if t.strip()} if types else set(_CAPS)
    if not query:
        # Recents only make sense scoped: an unscoped empty query is the ⌘K idle state.
        return {"results": _recents(kinds, limit) if types and len(kinds) == 1 else []}
    if len(query) < 2:
        return {"results": []}
    ql = query.lower()
    out: list[dict] = []

    # ── Knowledge: real FTS ───────────────────────────────────────────────────
    try:
        for h in knowledge.search(query, limit=_CAPS["knowledge"]) if "knowledge" in kinds else []:
            out.append({
                "type": "knowledge",
                "id": h.get("path"),
                "title": h.get("title") or h.get("path") or "note",
                "subtitle": h.get("type") or "note",
                "snippet": h.get("snippet") or h.get("description") or "",
                "date": None,
                "score": 1000 + int(h.get("score") or 0),  # FTS hits float above substring hits
            })
    except Exception as exc:  # never let one silo's failure blank the whole palette
        print(f"[search] knowledge failed: {exc}", flush=True)

    # ── Recordings: title/notes substring ─────────────────────────────────────
    try:
        scored = []
        for r in recordings.list_recordings() if "recording" in kinds else []:
            hay = f"{r.get('name', '')} {r.get('notes', '') or ''} {' '.join(r.get('tags') or [])}"
            sc = _score(hay, ql)
            if sc:
                scored.append((sc, r))
        scored.sort(key=lambda t: t[0], reverse=True)
        for sc, r in scored[: _CAPS["recording"]]:
            out.append({
                "type": "recording", "id": r.get("id"),
                "title": r.get("name") or "Untitled recording",
                "subtitle": "recording",
                "snippet": _clip(r.get("notes") or "", ql),
                "date": r.get("createdAt"), "score": sc,
            })
    except Exception as exc:
        print(f"[search] recordings failed: {exc}", flush=True)

    # ── Todos ─────────────────────────────────────────────────────────────────
    try:
        scored = [(_score(t.get("text", ""), ql), t) for t in (data.list_todos() if "todo" in kinds else [])]
        scored = [(s, t) for s, t in scored if s]
        scored.sort(key=lambda t: t[0], reverse=True)
        for sc, t in scored[: _CAPS["todo"]]:
            out.append({
                "type": "todo", "id": t.get("id"),
                "title": t.get("text") or "todo",
                "subtitle": "done" if t.get("done") else ("due " + t["dueDate"] if t.get("dueDate") else "todo"),
                "snippet": "", "date": t.get("createdAt"), "score": sc,
            })
    except Exception as exc:
        print(f"[search] todos failed: {exc}", flush=True)

    # ── Reminders ─────────────────────────────────────────────────────────────
    try:
        scored = []
        for rm in data.list_reminders() if "reminder" in kinds else []:
            hay = f"{rm.get('title', '')} {rm.get('notes', '') or ''}"
            sc = _score(hay, ql)
            if sc:
                scored.append((sc, rm))
        scored.sort(key=lambda t: t[0], reverse=True)
        for sc, rm in scored[: _CAPS["reminder"]]:
            out.append({
                "type": "reminder", "id": rm.get("id"),
                "title": rm.get("title") or "reminder",
                "subtitle": "reminder", "snippet": _clip(rm.get("notes") or "", ql),
                "date": rm.get("dueAt"), "score": sc,
            })
    except Exception as exc:
        print(f"[search] reminders failed: {exc}", flush=True)

    # ── Artifacts ─────────────────────────────────────────────────────────────
    try:
        scored = []
        for a in artifact_store.list_artifacts() if "artifact" in kinds else []:
            hay = f"{a.get('title', '')} {a.get('description', '') or ''}"
            sc = _score(hay, ql)
            if sc:
                scored.append((sc, a))
        scored.sort(key=lambda t: t[0], reverse=True)
        for sc, a in scored[: _CAPS["artifact"]]:
            out.append({
                "type": "artifact", "id": a.get("id"),
                "title": a.get("title") or "artifact",
                "subtitle": a.get("kind") or "artifact",
                "snippet": _clip(a.get("description") or "", ql),
                "date": a.get("createdAt"), "score": sc,
            })
    except Exception as exc:
        print(f"[search] artifacts failed: {exc}", flush=True)

    # ── Assistants ────────────────────────────────────────────────────────────
    try:
        scored = []
        for ag in agent_store.list_agents() if "agent" in kinds else []:
            hay = f"{ag.get('name', '')} {ag.get('description', '') or ''}"
            sc = _score(hay, ql)
            if sc:
                scored.append((sc, ag))
        scored.sort(key=lambda t: t[0], reverse=True)
        for sc, ag in scored[: _CAPS["agent"]]:
            out.append({
                "type": "agent", "id": ag.get("id"),
                "title": ag.get("name") or "assistant",
                "subtitle": "assistant",
                "snippet": _clip(ag.get("description") or "", ql),
                "date": None, "score": sc,
            })
    except Exception as exc:
        print(f"[search] agents failed: {exc}", flush=True)

    out.sort(key=lambda r: r.get("score", 0), reverse=True)
    return {"results": out[:limit]}
