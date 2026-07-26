"""Recordings: capture lifecycle, audio, attachments, per-agent outputs."""
from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from curry_leaves_assistant.core import events, trace_ctx
from curry_leaves_assistant.core.store import now_iso
from curry_leaves_assistant.domain import recordings, transcribe
from curry_leaves_assistant.orchestration import work
from curry_leaves_assistant.orchestration.work import BAND_INTERACTIVE, WorkItem
from curry_leaves_assistant.stores import agent_store

router = APIRouter(tags=["recordings"])


class CreateRec(BaseModel):
    name: str | None = None
    templateId: str | None = None  # meeting template to bind; falls back to the app default
    language: str | None = None    # transcription language override; falls back to Settings → Recording


class FinalizeRec(BaseModel):
    name: str | None = None
    duration: float | None = None


@router.post("/recordings")
def create_recording(body: CreateRec):
    return recordings.create_draft(body.name, template_id=body.templateId, language=body.language)


@router.post("/recordings/{rec_id}/chunk")
async def append_chunk(rec_id: str, request: Request):
    recordings.append_chunk(rec_id, await request.body())
    return {"ok": True}


@router.post("/recordings/{rec_id}/finalize")
async def finalize_recording(rec_id: str, body: FinalizeRec):
    async def _bg(trace_id: str, span_id: str):
        # Continue the recording's trace into the background transcription so the whole chain
        # (finalized → transcribed → summarizer → … → knowledge) is ONE trace.
        with trace_ctx.activate(trace_id, span_id):
            await asyncio.to_thread(transcribe.transcribe_recording, rec_id)

    with trace_ctx.span("action", "recording.finalized",
                        attributes={"type": "recording", "recordingId": rec_id}) as root:
        meta = recordings.finalize(rec_id, name=body.name, duration=body.duration)
        if meta is None:
            return Response(status_code=404)
        asyncio.create_task(_bg(root.trace_id, root.span_id))
    return meta


@router.post("/recordings/{rec_id}/recover")
async def recover_recording(rec_id: str):
    """Save an interrupted (crash-orphaned) draft: finalize it and run transcription, the same
    pipeline a normal Stop takes. The Discard choice is just DELETE /recordings/{id}."""
    async def _bg(trace_id: str, span_id: str):
        with trace_ctx.activate(trace_id, span_id):
            await asyncio.to_thread(transcribe.transcribe_recording, rec_id)

    with trace_ctx.span("action", "recording.recovered",
                        attributes={"type": "recording", "recordingId": rec_id}) as root:
        meta = recordings.recover(rec_id)
        if meta is None:
            return Response(status_code=404)
        asyncio.create_task(_bg(root.trace_id, root.span_id))
    return meta


@router.get("/recordings/interrupted")
def list_interrupted_recordings():
    """Drafts that crashed/were orphaned mid-capture and need a Save/Discard decision. The
    Recordings tab shows these as a banner. Kept as its own route so the main list stays clean."""
    return recordings.list_interrupted()


@router.get("/recordings")
def list_recordings():
    return recordings.list_recordings()


@router.get("/recordings/{rec_id}")
def get_recording(rec_id: str):
    return recordings.get(rec_id) or Response(status_code=404)


@router.get("/recordings/{rec_id}/audio")
def get_audio(rec_id: str):
    p = recordings.current_audio_path(rec_id)
    if not p.exists():
        return Response(status_code=404)
    mime = "audio/wav" if p.suffix == ".wav" else "audio/webm"
    # The file mutates in place (capture appends, trim replaces) while its URL may
    # stay identical — cached partials + range requests against a changed file → 416.
    return FileResponse(p, media_type=mime, headers={"Cache-Control": "no-store"})


@router.post("/recordings/{rec_id}/save-audio")
async def save_audio(rec_id: str, request: Request, duration: float | None = None):
    """Replace audio with an edited mono WAV (trim / add-recording) and re-transcribe."""
    meta = recordings.save_audio(rec_id, await request.body(), duration)
    if meta is None:
        return Response(status_code=404)
    asyncio.create_task(asyncio.to_thread(transcribe.transcribe_recording, rec_id))
    return meta


@router.delete("/recordings/{rec_id}/audio")
def delete_audio(rec_id: str):
    return recordings.delete_audio(rec_id) or Response(status_code=404)


@router.delete("/recordings/{rec_id}")
def delete_recording(rec_id: str):
    return {"ok": recordings.delete(rec_id)}


@router.post("/recordings/{rec_id}/resubmit")
def resubmit_recording(rec_id: str):
    """Re-emit recording.transcribed → triggers the summarizer (and any agent on it)."""
    meta = recordings.resubmit(rec_id)
    return meta or Response(status_code=409)  # 409 = no transcript to reprocess


_REC_PATCH_FIELDS = {"name", "notes", "links", "tags", "saveToKnowledge", "language",
                     "attendees", "templateId", "templateIds"}


@router.patch("/recordings/{rec_id}")
async def patch_recording(rec_id: str, request: Request):
    """Update a recording's user-editable context (name, notes, links, tags, knowledge flag, language)."""
    body = await request.json()
    patch = {k: v for k, v in body.items() if k in _REC_PATCH_FIELDS}
    meta = recordings.update(rec_id, patch)
    if meta is None:
        return Response(status_code=404)
    # Learn vocabulary from freshly typed notes/attendees. Doing it here (and not only at
    # transcribe time) means a term typed mid-meeting is already known when a *later*
    # recording is transcribed, and that notes edited after the fact still teach us.
    if "notes" in patch or "attendees" in patch:
        try:
            from curry_leaves_assistant.stores import vocabulary_store
            text = "\n".join([
                str(patch.get("notes") or ""),
                *[a for a in (patch.get("attendees") or []) if isinstance(a, str)],
            ])
            if text.strip():
                vocabulary_store.learn(text, source=rec_id)
        except Exception:
            pass  # never fail a note save over vocabulary
    events.emit("recording.updated", payload=meta, entity_id=rec_id, label=meta["name"])
    return meta


@router.post("/recordings/{rec_id}/attach")
async def attach_recording_doc(rec_id: str, request: Request, filename: str = "file"):
    """Attach a document to a recording (raw octet-stream body); rendered to markdown."""
    att = recordings.attach_file(rec_id, filename, await request.body())
    return att or Response(status_code=404)


@router.delete("/recordings/{rec_id}/attachments/{name}")
def delete_recording_doc(rec_id: str, name: str):
    return recordings.remove_attachment(rec_id, name) or Response(status_code=404)


# ─── Recording outputs (per-agent artifacts) ───────────────────────────────────
@router.get("/recordings/{rec_id}/outputs")
def list_recording_outputs(rec_id: str):
    if recordings.get(rec_id) is None:
        return Response(status_code=404)
    return recordings.list_outputs(rec_id)


@router.delete("/recordings/{rec_id}/outputs/{key}")
def delete_recording_output(rec_id: str, key: str):
    """Delete one output by its file key — <agentId> or <agentId>.<section>."""
    return {"ok": recordings.delete_output(rec_id, key)}


@router.get("/recordings/attendees/suggest")
def suggest_attendees():
    """Distinct attendee names seen across past recordings plus people already in the
    knowledge base — the pick-from-history source for the recording attendee chips."""
    return {"names": recordings.attendee_suggestions()}


@router.post("/recordings/{rec_id}/rerun/{agent_id}")
def rerun_recording_agent(rec_id: str, agent_id: str):
    """Enqueue a fresh job for just this (recording, agent) pair — re-emits a
    recording.transcribed-shaped trigger with a NEW event id so the pool's
    deterministic (event, agent) job id doesn't collide with the original run."""
    meta = recordings.get(rec_id)
    if meta is None:
        return Response(status_code=404)
    agent = agent_store.read_agent(agent_id)
    if agent is None:
        return Response(status_code=404)
    trigger = {"id": uuid.uuid4().hex, "type": "recording.transcribed",
              "occurredAt": now_iso(), "payload": meta}
    job_id = work.submit(WorkItem(
        kind="agent", agent_id=agent_id, trigger=trigger, mode="background",
        lane=agent.get("lane") or "general", band=BAND_INTERACTIVE,   # user-clicked → jumps the queue
        autonomy=agent.get("autonomy") or "auto", dedupe_key=trigger["id"]))
    return {"jobId": job_id}
