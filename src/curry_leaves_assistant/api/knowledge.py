"""Knowledge base (the OKF bundle): notes, graph, health, the Gardener, and ingest."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from curry_leaves_assistant.domain import knowledge, knowledge_gardener, knowledge_ingest
from curry_leaves_assistant.orchestration import work as work_mod
from curry_leaves_assistant.stores import agent_store, inputs_store

router = APIRouter(tags=["knowledge"])


class KnowledgeSave(BaseModel):
    path: str
    text: str


@router.get("/knowledge")
def list_knowledge():
    """All notes (metadata only) + every folder, for the knowledge hub tree. `dirs` lets
    the tree render folders that hold no notes yet (user-created, empty)."""
    return {"notes": knowledge.list_notes(), "dirs": knowledge.list_dirs()}


class DirCreate(BaseModel):
    path: str


class NoteMove(BaseModel):
    path: str
    dest_dir: str
    new_name: str | None = None


class DirMove(BaseModel):
    path: str
    dest_parent: str
    new_name: str | None = None


@router.post("/knowledge/dir")
def create_knowledge_dir(body: DirCreate):
    """Create an empty folder (the tree's 'New folder')."""
    try:
        return knowledge.create_dir(body.path)
    except ValueError as exc:
        return Response(content=str(exc), status_code=400)


@router.post("/knowledge/note/move")
def move_knowledge_note(body: NoteMove):
    """Move/rename a note, rewriting links across the bundle to keep it connected."""
    try:
        return knowledge.move_note(body.path, body.dest_dir, body.new_name)
    except FileNotFoundError as exc:
        return Response(content=str(exc), status_code=404)
    except ValueError as exc:
        return Response(content=str(exc), status_code=409)


@router.post("/knowledge/dir/move")
def move_knowledge_dir(body: DirMove):
    """Rename/move a folder — every note under it moves as one link-preserving batch."""
    try:
        return knowledge.move_dir(body.path, body.dest_parent, body.new_name)
    except FileNotFoundError as exc:
        return Response(content=str(exc), status_code=404)
    except ValueError as exc:
        return Response(content=str(exc), status_code=409)


@router.delete("/knowledge/dir")
def delete_knowledge_dir(path: str, reason: str | None = None):
    """Soft-archive a whole folder (subtree → _archive/, restorable)."""
    try:
        return knowledge.archive_dir(path, reason)
    except (ValueError, FileNotFoundError) as exc:
        return Response(content=str(exc), status_code=400)


@router.get("/knowledge/search")
def search_knowledge(q: str, limit: int = 12, mode: str | None = None):
    """Ranked search. `mode` = keyword | vector | hybrid; omit it for the best available
    (hybrid when the local embedding model is ready, keyword otherwise). `vectorSearch` reports
    which one is live so the UI can label/offer the modes honestly."""
    return {"hits": knowledge.search(q, limit=limit, mode=mode),
            "vectorSearch": knowledge.VECTOR_SEARCH}


@router.get("/knowledge/graph")
def knowledge_graph():
    """Nodes + link edges for the graph dashboard."""
    return knowledge.graph()


@router.get("/knowledge/note")
def get_knowledge_note(path: str):
    """Full markdown text of a note (for the editor)."""
    note = knowledge.read_raw(path)
    return note or Response(status_code=404)


@router.get("/knowledge/note/links")
def knowledge_note_links(path: str):
    """A note's outbound links + inbound backlinks (for the note page)."""
    return knowledge.note_links(path)


@router.get("/knowledge/note/history")
def knowledge_note_history(path: str):
    """Every recorded version of a note (newest first, each with its snapshot)."""
    return {"history": knowledge.history(path)}


@router.get("/knowledge/note/provenance")
def knowledge_note_provenance(path: str):
    """A note's provenance entries, each resolved to its transcript span (Feature 1)."""
    return knowledge.provenance(path)


@router.get("/knowledge/conflicts")
def knowledge_conflicts():
    """Notes flagged `status: conflicted` — the human-resolution queue (Feature 2)."""
    return {"conflicts": knowledge.list_conflicts()}


@router.get("/knowledge/health")
def knowledge_health():
    """Read-only KB health sweep — broken links, orphans, missing frontmatter, tag
    drift. Computed fresh on every call; writes nothing (contrast with the Gardener,
    which is a maintenance pass that writes a report note)."""
    return knowledge.health()


@router.get("/knowledge/embeddings")
def get_embeddings_status():
    """Semantic-search model status: {available, enabled, downloaded, ready}. The weights are
    NOT fetched at boot — this tells the setup/settings UI whether to offer the download."""
    return knowledge.embeddings_status()


@router.post("/knowledge/embeddings/download")
async def download_embeddings_route():
    """Fetch the ~90 MB embedding model and warm it, so semantic search comes up. Triggered
    explicitly from the setup wizard's Knowledge step or Settings — never in the background."""
    try:
        status = await asyncio.to_thread(knowledge.download_embeddings)
    except Exception as e:  # noqa: BLE001 - surface the reason to the UI
        return {"ok": False, "error": str(e), **knowledge.embeddings_status()}
    return {"ok": status.get("ready", False), **status}


@router.get("/knowledge/gardener")
def knowledge_gardener_report():
    """The latest Gardener maintenance report (or null if it hasn't run)."""
    note = knowledge.read_raw("notes/gardener-report.md")
    return {"report": note}


@router.post("/knowledge/gardener")
def knowledge_gardener_run():
    """Force a Gardener maintenance run now (mechanical passes; Feature 4)."""
    return knowledge_gardener.run()


@router.post("/knowledge/reindex")
def knowledge_reindex():
    """Rebuild the search/link index from the markdown files (recovery / after external
    edits). The index is derived + disposable — this reconstructs it from the truth."""
    return knowledge.reindex()


@router.put("/knowledge/note")
def save_knowledge_note(body: KnowledgeSave):
    """Save hand-edited markdown. 400 if frontmatter lacks a `type`."""
    try:
        return knowledge.write_raw(body.path, body.text)
    except ValueError as exc:
        return Response(content=str(exc), status_code=400)


@router.delete("/knowledge/note")
def delete_knowledge_note(path: str):
    return {"ok": knowledge.delete_note(path)}


class KnowledgeIngest(BaseModel):
    text: str
    title: str | None = None


@router.post("/knowledge/ingest")
def ingest_knowledge_text(body: KnowledgeIngest):
    """Feed pasted text into the knowledge base (routes to the Keeper, async)."""
    try:
        return knowledge_ingest.submit_text(body.text, body.title)
    except ValueError as exc:
        return Response(content=str(exc), status_code=400)


@router.post("/knowledge/ingest/file")
async def ingest_knowledge_file(request: Request, filename: str = "document"):
    """Feed an uploaded file. Returns immediately after staging; conversion to markdown
    (docling OCR for PDFs — can take minutes) runs in the background, then it's filed."""
    try:
        meta = knowledge_ingest.submit_file(filename, await request.body())
    except ValueError as exc:
        return Response(content=str(exc), status_code=400)
    # Convert off the request path (blocking OCR) → then dispatch to the doc-filer.
    asyncio.create_task(asyncio.to_thread(knowledge_ingest.convert, meta["docId"]))
    return meta


_INGEST_STATE = {"pending": "queued", "queued": "queued", "running": "running",
                 "done": "filed", "failed": "failed"}


def _ingest_entry(job: dict) -> dict:
    """Normalize a keeper job/run record into a compact ingest-status row."""
    trig = job.get("trigger") or {}
    p = trig.get("payload") or {}
    ttype = trig.get("type")
    if ttype == "knowledge.ingest.requested":
        kind, title = "document", (p.get("title") or "Untitled document")
    elif ttype in ("recording.summarized", "recording.transcribed"):
        kind, title = "meeting", (p.get("name") or trig.get("label") or "Recording")
    else:
        kind, title = "other", (trig.get("label") or ttype or "Task")
    state = job.get("state") or _INGEST_STATE.get(job.get("status") or "", "queued")
    output = (job.get("output") or "").strip()
    # A run that finished but produced nothing did not actually file anything — surface it
    # honestly instead of a false "Filed" (e.g. the doc was too large / the model failed).
    if state == "filed" and not output:
        state = "empty"
    return {
        "jobId": job.get("id"), "kind": kind, "title": title, "state": state,
        "at": job.get("finishedAt") or job.get("startedAt") or job.get("createdAt"),
        "error": job.get("error") if state == "failed" else None,
        "summary": output[:200] if state == "filed" else None,
    }


# The Ingest Activity view tracks the Knowledge Filer's runs — both document feeds
# (knowledge.ingest.requested) and meeting filing (recording.outputs.completed), since
# kb-filer handles both triggers.
_INGEST_AGENTS = ("kb-filer",)


@router.get("/knowledge/ingest/status")
def knowledge_ingest_status(limit: int = 25):
    """What's being filed right now + recently — documents (doc-filer) and meetings
    (keeper): queued · running · filed · failed. Powers the ingest activity view."""
    active = [_ingest_entry(j) for a in _INGEST_AGENTS for j in work_mod.queued_jobs(a)]
    # Inputs still converting (docling OCR) have no job yet — surface them as 'converting';
    # failed conversions show once under recent.
    conv_failed = []
    for m in inputs_store.list_inputs():
        row = {"jobId": "input-" + m["docId"], "kind": "document",
               "title": m.get("title") or "document", "at": m.get("created"),
               "error": None, "summary": None}
        if m.get("status") == "converting":
            active.append({**row, "state": "converting"})
        elif m.get("status") == "failed":
            conv_failed.append({**row, "state": "failed", "error": "Could not extract text from this file."})
    active.sort(key=lambda e: e.get("at") or "", reverse=True)
    active_ids = {e["jobId"] for e in active}
    recent = []
    for a in _INGEST_AGENTS:
        for r in agent_store.recent_runs(a, limit):
            if r.get("id") not in active_ids:
                recent.append(_ingest_entry(r))
    recent.extend(conv_failed)
    recent.sort(key=lambda e: e.get("at") or "", reverse=True)
    return {"active": active, "recent": recent[:limit]}
