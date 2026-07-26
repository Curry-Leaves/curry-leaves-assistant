"""Meeting templates: list/get/CRUD plus describe-only authoring.

The user never writes template markdown. `POST /templates/generate` turns a plain-language
description into a full template (an LLM structured pass, tool-less, like the dashboard tile
generator); `POST /templates/{id}/revise` edits an existing one from a plain instruction —
and, when given a recordingId, re-runs the Meeting Copilot so a "you missed X" correction
takes effect on that recording immediately.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from curry_leaves_assistant.agents import agent_engine
from curry_leaves_assistant.core.store import now_iso
from curry_leaves_assistant.domain import recordings
from curry_leaves_assistant.orchestration import work
from curry_leaves_assistant.orchestration.work import BAND_INTERACTIVE, WorkItem
from curry_leaves_assistant.stores import agent_store, templates_store

router = APIRouter(tags=["templates"])


# ─── structured authoring schema ───────────────────────────────────────────────
class SectionDraft(BaseModel):
    id: str = Field(description="Slug id, e.g. 'follow-up-email'. Use 'summary' for the summary section.")
    title: str = Field(description="Human title shown on the tab, e.g. 'Follow-up email'.")


class TemplateDraft(BaseModel):
    name: str = Field(description="Short template name, e.g. 'Client Call'.")
    description: str = Field(description="One line describing what this template produces.")
    sections: list[SectionDraft] = Field(description="Ordered output sections; put 'summary' first when useful.")
    watch: list[str] = Field(
        description="In-meeting live cues to surface; any of: open-loops, commitments, "
                    "unanswered-questions, decisions, conflicts. Empty for a quick note.")
    body: str = Field(description="Markdown instructions telling the copilot how to write each section.")


_AUTHOR_INSTRUCTIONS = (
    "You author meeting-template files for a voice assistant. A template defines what the "
    "post-meeting copilot produces for one kind of meeting. Keep sections few and meaningful; "
    "use 'summary' as the first section id when a summary fits. Section ids are lowercase slugs. "
    "The body is plain-language guidance the copilot follows to write each section — be specific "
    "about what counts as an action item and what to skip."
)

_WATCH = set(templates_store.LIVE_WATCH_KINDS)


def _draft_to_template(draft: TemplateDraft) -> dict:
    return {
        "name": draft.name,
        "description": draft.description,
        "sections": [{"id": s.id, "title": s.title} for s in draft.sections],
        # Every template extracts todos + action items by default — not the model's call. It
        # stays overridable later via the advanced editor / raw save.
        "extract": {"todos": True, "reminders": True},
        "live": {"watch": [w for w in draft.watch if w in _WATCH]},
        "body": draft.body,
    }


async def _author(prompt: str) -> TemplateDraft | None:
    spec = {"id": "template-author", "model": None, "tools": [],
            "instructions": _AUTHOR_INSTRUCTIONS, "description": ""}
    draft, _raw = await agent_engine.run_agent_structured(spec, prompt, TemplateDraft)
    return draft


# ─── reads ─────────────────────────────────────────────────────────────────────
@router.get("/templates")
def list_templates():
    return templates_store.list_templates()


@router.get("/templates/{template_id}")
def get_template(template_id: str):
    return templates_store.get_template(template_id) or Response(status_code=404)


# ─── describe-to-create ─────────────────────────────────────────────────────────
class GenerateBody(BaseModel):
    description: str


@router.post("/templates/generate")
async def generate_template(body: GenerateBody):
    """Author a new template from a plain-language description and save it."""
    desc = (body.description or "").strip()
    if not desc:
        return Response(content="Describe the meeting type you want.", status_code=400)
    prompt = (
        "Create a meeting template from this description:\n\n"
        f'"{desc}"\n\n'
        "Return the template's name, one-line description, ordered sections, which live cues to "
        "watch, and the body instructions. Every meeting captures agreed action items as todos "
        "and reminders — always include, in the body, guidance to capture them (only genuinely "
        "agreed items, never inferred)."
    )
    try:
        draft = await _author(prompt)
    except Exception as e:
        return Response(content=f"Template generation failed: {e}", status_code=502)
    if draft is None:
        return Response(content="The AI provider returned no usable output — check the active provider/model in Settings.",
                        status_code=502)
    return templates_store.save_template(_draft_to_template(draft))


# ─── conversational revise (also the fix-after-miss hook) ───────────────────────
class ReviseBody(BaseModel):
    instruction: str
    recordingId: str | None = None  # when set, re-run the copilot on this recording after revising


@router.post("/templates/{template_id}/revise")
async def revise_template(template_id: str, body: ReviseBody):
    """Revise a template from a plain instruction (e.g. 'also capture budget discussions').
    With a recordingId, seed the pass with what the copilot actually produced for that
    recording, then re-run it so the correction lands immediately."""
    current = templates_store.get_template(template_id)
    if current is None:
        return Response(status_code=404)
    instruction = (body.instruction or "").strip()
    if not instruction:
        return Response(content="Say what to change.", status_code=400)

    produced = ""
    if body.recordingId:
        outs = recordings.list_outputs(body.recordingId)
        if outs:
            produced = "\n\nFor reference, here is what the current template produced for a recent " \
                       "recording (revise so a gap like the one the user names is covered next time):\n" \
                       + "\n".join(f"- {o['title']}: {(o.get('content') or '')[:400]}" for o in outs)

    prompt = (
        "Revise this meeting template per the user's instruction. Return the COMPLETE revised "
        "template (all fields), not just the change.\n\n"
        f"Current name: {current['name']}\n"
        f"Current description: {current['description']}\n"
        f"Current sections: {[s['id'] for s in current['sections']]}\n"
        f"Current body:\n{current['body']}\n\n"
        f'User instruction: "{instruction}"{produced}'
    )
    try:
        draft = await _author(prompt)
    except Exception as e:
        return Response(content=f"Template revision failed: {e}", status_code=502)
    if draft is None:
        return Response(content="The AI provider returned no usable output — check the active provider/model in Settings.",
                        status_code=502)

    revised = _draft_to_template(draft)
    revised["id"] = template_id  # keep the same file/id
    saved = templates_store.save_template(revised)

    if body.recordingId:
        _rerun_copilot(body.recordingId)
    return saved


def _rerun_copilot(rec_id: str) -> None:
    """Re-enqueue the Meeting Copilot on a recording (fresh event id, interactive band)."""
    meta = recordings.get(rec_id)
    agent = agent_store.read_agent("meeting-copilot")
    if meta is None or agent is None:
        return
    trigger = {"id": uuid.uuid4().hex, "type": "recording.transcribed",
               "occurredAt": now_iso(), "payload": meta}
    work.submit(WorkItem(
        kind="agent", agent_id="meeting-copilot", trigger=trigger, mode="background",
        lane=agent.get("lane") or "general", band=BAND_INTERACTIVE,
        autonomy=agent.get("autonomy") or "auto", dedupe_key=trigger["id"]))


# ─── raw save (advanced editor) + delete + default ──────────────────────────────
class SaveBody(BaseModel):
    name: str | None = None
    description: str | None = None
    sections: list[dict] | None = None
    extract: dict | None = None
    live: dict | None = None
    agents: list[str] | None = None
    body: str | None = None


@router.put("/templates/{template_id}")
def save_template(template_id: str, body: SaveBody):
    """Raw create/replace (the Advanced editor). Merges over the existing template."""
    current = templates_store.get_template(template_id) or {"id": template_id}
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    merged = {**current, **patch, "id": template_id}
    return templates_store.save_template(merged)


@router.delete("/templates/{template_id}")
def delete_template(template_id: str):
    return {"ok": templates_store.delete_template(template_id)}


class SetDefault(BaseModel):
    templateId: str | None = None


@router.post("/templates/default")
def set_default(body: SetDefault):
    try:
        return templates_store.set_default(body.templateId)
    except ValueError:
        return Response(status_code=400)
