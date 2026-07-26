"""The Knowledge Hub — the browsable face of the one memory bundle.

There is no separate knowledge store any more. One bundle (``domain/memory.bundle``) holds
everything, so a fact can link to the meeting that produced it and consolidation/``trace()``
work at all. What differs is the VIEW:

  • this module = the hub: browsable notes under ``apps/ topics/ people/ meetings/ notes/`` —
    which is where facts and preferences live too, filed under whatever they're about;
  • ``memory/`` = the machinery the user doesn't browse: per-agent private notes and the
    consolidated lessons folded out of them.

Sharing storage must not mean sharing the view. Every hub-facing read here therefore filters the
``memory/`` subtree out (see ``_is_hub``) — otherwise an assistant's private working note would show
up as a page in the note tree, in ⌘K's Knowledge results, on the graph, and as an "orphan" in
Health, all while already living in its own tab.

This module keeps the app's long-standing surface: every caller still does
``from curry_leaves_assistant.domain import knowledge`` then ``knowledge.<fn>(...)``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from curry_leaves_assistant.domain import memory
from curry_leaves_assistant.domain.memory import AREAS, VECTOR_SEARCH, bundle

_b = bundle


# ─── read ─────────────────────────────────────────────────────────────────────
def read_note(ref: str) -> dict | None:
    return _b.read(ref)


def read_raw(ref: str) -> dict | None:
    return _b.read_raw(ref)


def read_file(rel: str) -> str | None:
    return _b.read_file(rel)


# The bundle holds the hub's browsable notes AND the memory scopes (facts, per-agent notes,
# episodes, consolidated lessons) under `memory/`. They share one bundle so links can cross
# between them — but the hub VIEW must not show them: a profile fact like "user name" is not a
# page you browse, and it already has its own home in the Facts tab. Everything hub-facing
# therefore filters the memory subtree out.
_MEMORY_PREFIX = "memory/"


def _is_hub(rel: str) -> bool:
    """True for a browsable knowledge note (i.e. not one of the memory scopes)."""
    return not (rel == "memory" or rel.startswith(_MEMORY_PREFIX))


def list_notes(subdir: str | None = None, type: str | None = None) -> list[dict]:
    """The hub's browsable notes. Memory-scope notes are excluded — see _is_hub."""
    return [n for n in _b.notes(subdir, type) if _is_hub(n.get("path") or "")]


def list_dirs() -> list[str]:
    """The hub's folders, minus the memory subtree (so the tree shows no `memory/` branch)."""
    return [d for d in _b.dirs() if _is_hub(d)]


def established_tags(limit: int = 20) -> list[str]:
    return _b.established_tags(limit)


def resolve(name: str) -> dict | None:
    return _b.resolve(name)


def list_conflicts() -> list[dict]:
    return _b.conflicts()


# ─── search / graph / links / health ──────────────────────────────────────────
def search(query: str, limit: int = 8, *, mode: str | None = None) -> list[dict]:
    """Ranked search over the hub's browsable notes. `mode` = keyword | vector | hybrid;
    defaults to the best available. Memory-scope hits are filtered out — searching the Knowledge
    Hub (or ⌘K's "Knowledge" group) shouldn't return a profile fact; `memory.search` is the
    cross-memory surface for that. Over-fetches so the cap still holds after filtering."""
    hits = memory.search(query, limit=limit * 3, mode=mode)
    return [h for h in hits if _is_hub(h.get("path") or "")][:limit]


def graph() -> dict:
    """The hub's link graph — memory notes and their edges are left out so the view stays a map
    of browsable knowledge."""
    g = _b.graph()
    nodes = [n for n in g.get("nodes", []) if _is_hub(n.get("path") or "")]
    keep = {n["path"] for n in nodes}
    links = [e for e in g.get("links", [])
             if e.get("source") in keep and e.get("target") in keep]
    return {"nodes": nodes, "links": links}


def note_links(ref: str) -> dict:
    return _b.links(ref)


def health() -> dict:
    """The hub's health sweep, scoped to browsable notes.

    Memory-scope notes are excluded on purpose: a profile fact has no links and no tags by
    design, so every one would land in `orphans` / `missing_tags` and drown the real knowledge
    issues in noise the user can't act on."""
    h = _b.health()
    out = dict(h)
    for key in ("orphans", "missing_type", "missing_description", "missing_tags"):
        out[key] = [p for p in h.get(key, []) if _is_hub(p)]
    out["broken_links"] = [b for b in h.get("broken_links", []) if _is_hub(b.get("from") or "")]
    counts = dict(h.get("counts") or {})
    counts["notes"] = len(list_notes())
    out["counts"] = counts
    return out


def history(ref: str) -> list[dict]:
    return _b.history(ref)


def provenance(rel: str) -> dict:
    return _b.provenance(rel)


def trace(query: str) -> dict:
    """Start at the best match and walk the links outward — connect the dots across the whole
    bundle (a fact -> the meeting that produced it -> the people in it)."""
    return _b.trace(query)


# ─── write path ─────────────────────────────────────────────────────────────────
def write_file(rel: str, content: str) -> dict:
    return _b.write(rel, content)


def kb_edit(rel: str, edits: list[dict]) -> dict:
    return _b.edit(rel, edits)


def write_raw(rel_path: str, text: str) -> dict:
    return _b.write_raw(rel_path, text)


def system_write(rel: str, content: str, *, source: dict | None = None) -> dict:
    return _b.system_write(rel, content, source=source)


def delete_note(ref: str, reason: str | None = None) -> bool:
    return _b.delete(ref, reason)


# ─── folders + move/rename ──────────────────────────────────────────────────────
def create_dir(rel: str) -> dict:
    return _b.create_dir(rel)


def move_note(rel: str, dest_dir: str, new_name: str | None = None) -> dict:
    return _b.move(rel, dest_dir, new_name)


def move_dir(rel: str, dest_parent: str, new_name: str | None = None) -> dict:
    return _b.move_dir(rel, dest_parent, new_name)


def archive_dir(rel: str, reason: str | None = None) -> dict:
    return _b.archive_dir(rel, reason)


# ─── index maintenance ──────────────────────────────────────────────────────────
def reconcile_index() -> dict:
    return _b.reconcile()


def reindex() -> dict:
    return _b.reindex()


def regenerate_index(dir_rel: str = "") -> None:
    _b.regenerate_index(dir_rel)


# ─── idempotency ledger (meeting -> notes) ──────────────────────────────────────
def note_ingest(meeting_id: str, rel: str) -> None:
    _b.note_ingest(meeting_id, rel)


def already_ingested(meeting_id: str) -> bool:
    return _b.already_ingested(meeting_id)


def ingested_notes(meeting_id: str) -> list[str]:
    return _b.ingested_notes(meeting_id)


# ─── bundle skeleton ─────────────────────────────────────────────────────────────
def seed_bundle() -> None:
    """Create the bundle + reconcile the index. Idempotent; called on boot."""
    memory.seed()


def warm_embeddings() -> None:
    memory.warm_embeddings()


def embeddings_status() -> dict:
    """Semantic-search readiness: libraries present, not opted out, weights on disk. Drives the
    setup/settings UI that offers the ~90 MB download."""
    return memory.embeddings_status()


def download_embeddings() -> dict:
    """Fetch + warm the embedding model on explicit request (setup or settings). Never at boot."""
    return memory.download_embeddings()


# ─── internals a few app modules still reach for ───────────────────────────────
def knowledge_path(rel: str) -> Path:
    return _b._ctx.path(rel)


def _rel(path: Path) -> str:
    return _b._ctx.rel(path)


def _all_note_files() -> list[Path]:
    return _b._ctx.all_note_files()


def parse_note(text: str) -> tuple[dict, str]:
    from cl_memory.util import parse_note as _parse

    return _parse(text)


def _outbound_targets(rel: str, body: str) -> set[str]:
    from cl_memory.links import outbound_targets

    return outbound_targets(rel, body)


def _emit(kind: str, payload: dict) -> None:
    """Emit an already-namespaced app event (e.g. ``knowledge.maintenance.completed``)."""
    try:
        from curry_leaves_assistant.core import events

        events.emit(kind, payload=payload)
    except Exception:
        pass


__all__: list[Any] = [
    "AREAS", "VECTOR_SEARCH", "bundle", "read_note", "read_raw", "read_file", "list_notes",
    "list_dirs", "established_tags", "resolve", "list_conflicts", "search", "graph",
    "note_links", "health", "history", "provenance", "trace", "write_file", "kb_edit",
    "write_raw", "system_write", "delete_note", "create_dir", "move_note", "move_dir",
    "archive_dir", "reconcile_index", "reindex", "regenerate_index", "note_ingest",
    "already_ingested", "ingested_notes", "seed_bundle", "warm_embeddings",
    "embeddings_status", "download_embeddings", "knowledge_path",
    "parse_note",
]
