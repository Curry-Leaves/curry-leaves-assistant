"""The one memory bundle — everything the app remembers, in one place.

``~/.curry-leaves/memory/`` is a single ``cl_memory`` Bundle holding the whole knowledge base AND
every kind of memory, partitioned by the ``type:`` frontmatter field rather than by directory:

    type: preference     how the user likes things done (injected into every agent's prompt)
    type: fact           something true, about whatever it's filed under (pulled in on relevance)
    type: private        how ONE agent does its job (carries an `agent:` field)
    type: episodic       a dated record of one agent run (carries an `occurred` event time)
    type: consolidated   a durable summary the consolidation pass folded out of episodes
    type: topic|person|meeting|note|…   browsable knowledge-hub content

This mirrors what the framework itself says: *"There is no separate hub — the whole knowledge base
lives in this one bundle of markdown notes."* Scopes stay apart via ``notes(type=…)`` /
``recall(type=…)`` / ``timeline(type=…)``, not via separate bundles.

One bundle rather than four is what makes the framework's best features possible at all:
  • **consolidation** clusters episodes by a shared tag AND a shared *link* — across separate
    bundles a meeting note and the run that filed it can't link, so it would never find a
    candidate;
  • **trace()** walks links to connect a fact back to the meeting that produced it;
  • recall answers one query across facts, runs and content;
  • one index + one embedder handle instead of four.

Everything app-specific is wired here (this is the composition point for memory): the vector
tier, the event bridge, the meeting provenance resolver, and the LLM summarizer consolidation
needs. Callers don't use this module directly — ``domain/knowledge.py`` and the ``stores/``
memory modules are the typed views over it.
"""
from __future__ import annotations

from typing import Any

from cl_memory import Bundle, FtsIndex, memory_conventions

from curry_leaves_assistant.core import embeddings, memory_ref
from curry_leaves_assistant.core.paths import (
    CONSOLIDATED_DIR,
    EPISODIC_DIR,
    MEMORY_DIR,
    PRIVATE_DIR,
    SEMANTIC_DIR,
)

# The knowledge-hub areas. Kept as a folder taxonomy because the KB is *browsable* — the tree UI,
# the gardener's sweeps and the filer's rules all lean on it. Memory notes (semantic/private/
# episodic/consolidated) live under `memory/` and are found by `type:`, never by folder.
AREAS = ("apps", "topics", "people", "meetings", "notes", "memory")

_NON_NOTE_RELS = frozenset({"notes/gardener-report.md"})


def _on_event(kind: str, payload: dict[str, Any]) -> None:
    """Bridge cl_memory's neutral event names onto this app's ``knowledge.*`` bus. The bus may be
    absent in CLI/test contexts, so failures are swallowed."""
    try:
        from curry_leaves_assistant.core import events

        events.emit(f"knowledge.{kind}", payload=payload)
    except Exception:
        pass


def _meeting_provenance(src: dict[str, Any], note: dict[str, Any]) -> dict[str, Any]:
    """Expand a meeting ``source:`` into its transcript span — the app-specific chain
    (source -> meeting_id + turns -> the words said), for the UI."""
    from curry_leaves_assistant.stores import transcripts

    turn_range = src.get("turn_range")
    meeting_id = src.get("id")
    return {
        "meeting_id": meeting_id,
        "section": src.get("section"),
        "turn_range": list(turn_range) if turn_range else None,
        "speaker": src.get("speaker"),
        "span": transcripts.span(meeting_id, turn_range) if meeting_id else None,
    }


# The consolidation summarizer, injected at boot. Folding a cluster of episodes into a durable
# note needs an LLM, and the provider/model machinery lives ABOVE this layer (agents/,
# providers/) — so app.py, the composition root, hands it down via set_summarizer(). Same
# bring-your-own-hook shape cl_memory uses on us: without one, consolidation is a no-op that
# still reports candidates.
_summarizer: Any = None


def set_summarizer(fn: Any) -> None:
    """Wire the LLM consolidation hook (called from app.py at boot)."""
    global _summarizer
    _summarizer = fn


def _summarize(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    if _summarizer is None:
        raise RuntimeError("no consolidation summarizer wired (no AI provider?)")
    return _summarizer(episodes)


_vec = embeddings.vector_index()

bundle = Bundle(
    MEMORY_DIR,
    index=FtsIndex(vector=_vec),
    conventions=memory_conventions(
        areas=AREAS,
        id_prefix="kn_",
        reserved_files=("index.md", "log.md", "GRAPH.md", "CONVENTIONS.md"),
        non_note_rels=_NON_NOTE_RELS,
    ),
    on_event=_on_event,
    provenance_resolvers={"meeting": _meeting_provenance},
    summarize=_summarize,
)

# Hand the composed bundle down to core so the stores/ memory views can reach it without
# importing domain (which would point the dependency upward). See core/memory_ref.py.
memory_ref.set_bundle(bundle)

# True when recall can match on meaning (the local embedder is ready). Callers that offer a mode
# switch should degrade to keyword when this is False.
VECTOR_SEARCH = _vec is not None

def seed() -> None:
    """Create the bundle skeleton and reconcile the index against the files. Idempotent."""
    bundle.seed()


def warm_embeddings() -> None:
    """Load the embedding model so the vector tier comes up on the next boot — but only if the
    weights are ALREADY on disk. This never downloads: semantic search is opt-in (a setup step,
    or Settings), so a fresh install that skipped it shouldn't pull ~90 MB at boot. Blocking and
    best-effort — call from a thread off the boot path."""
    if embeddings.enabled() and embeddings.is_downloaded():
        embeddings.warm()


def embeddings_status() -> dict:
    """Whether semantic search can run: the libraries import (`available`), the user hasn't opted
    out (`enabled`), and the weights are on disk (`downloaded`). `ready` is all three — the same
    gate `vector_ready()` uses. Drives the setup/settings UI that offers the download."""
    return {
        "available": embeddings.available(),
        "enabled": embeddings.enabled(),
        "downloaded": embeddings.is_downloaded(),
        "ready": embeddings.vector_ready(),
    }


def download_embeddings() -> dict:
    """Fetch the embedding weights (~90 MB) and warm the model. Triggered explicitly from setup
    or settings — never at boot — so an install that never wants semantic search never pulls it.
    Returns the fresh status."""
    if embeddings.available():
        embeddings.warm()  # downloads if absent, then loads the model
    return embeddings_status()


def search(query: str, limit: int = 8, *, mode: str | None = None,
           type: str | None = None) -> list[dict[str, Any]]:
    """Ranked search across the bundle. `mode` = keyword | vector | hybrid; defaults to the best
    available. `type` restricts to one kind of note (e.g. only semantic facts)."""
    if mode is None:
        mode = "hybrid" if VECTOR_SEARCH else "keyword"
    elif mode in ("vector", "hybrid") and not VECTOR_SEARCH:
        mode = "keyword"
    if type is None:
        return bundle.search(query, limit=limit, mode=mode)
    return bundle.recall(query, type=type, mode=mode, limit=limit)


__all__ = ["bundle", "seed", "search", "warm_embeddings", "embeddings_status",
           "download_embeddings", "VECTOR_SEARCH", "AREAS",
           "SEMANTIC_DIR", "PRIVATE_DIR", "EPISODIC_DIR", "CONSOLIDATED_DIR"]
