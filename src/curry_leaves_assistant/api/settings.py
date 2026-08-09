"""Settings and transcription model management."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from curry_leaves_assistant.agents import readiness
from curry_leaves_assistant.core import settings as app_settings
from curry_leaves_assistant.domain import transcribe
from curry_leaves_assistant.stores import profile_store, vocabulary_store

router = APIRouter(tags=["settings"])


@router.get("/settings")
def get_settings():
    return app_settings.read_settings()


@router.patch("/settings/ai")
async def patch_settings_ai(request: Request):
    """Configure providers / select the active one: {active?, providers?}."""
    updated = app_settings.patch_ai(await request.json())
    # Provider/key/model just changed — re-broadcast readiness so every open client
    # updates (or clears) the "no AI provider / no default model" banner live.
    readiness.emit_ai_status()
    return updated


@router.patch("/settings/live")
async def patch_settings_live(request: Request):
    """Configure the in-meeting Live Copilot: {enabled?, minNewChars?, cooldownSeconds?,
    maxPasses?, maxCardsPerPass?}. Takes effect on the next pass of any recording already
    running — live_context re-reads this per pass rather than snapshotting it at attach."""
    return app_settings.patch_live(await request.json())


@router.patch("/settings/appearance")
async def patch_settings_appearance(request: Request):
    """Persist appearance prefs: {theme?}."""
    return app_settings.patch_appearance(await request.json())


@router.patch("/settings/identity")
async def patch_settings_identity(request: Request):
    """Persist the operator's identity: {name?, work?, behavior?, workingHours?, usage?}.

    Also mirrors it into the knowledge base as user-stated facts, so what the user typed
    here is recallable (and traceable) rather than living only in settings.json.
    """
    saved = app_settings.patch_identity(await request.json())
    # Off the request path: writing notes touches the KB index, and saving Settings should
    # never block on it. Fails soft inside sync_identity.
    await asyncio.to_thread(profile_store.sync_identity, saved["identity"])
    return saved


# ─── Transcription models ─────────────────────────────────────────────────────
LANGUAGES = [
    {"code": "auto", "label": "Auto-detect"}, {"code": "en", "label": "English"},
    {"code": "es", "label": "Spanish"}, {"code": "fr", "label": "French"},
    {"code": "de", "label": "German"}, {"code": "it", "label": "Italian"},
    {"code": "pt", "label": "Portuguese"}, {"code": "hi", "label": "Hindi"},
    {"code": "zh", "label": "Chinese"}, {"code": "ja", "label": "Japanese"},
    {"code": "ar", "label": "Arabic"}, {"code": "ru", "label": "Russian"},
    {"code": "ta", "label": "Tamil"},
]


@router.get("/models")
def list_models_route():
    return {
        "models": transcribe.list_models(),
        "language": app_settings.recording_cfg().get("language", "en"),
        "languages": LANGUAGES,
        "vocabulary": app_settings.recording_cfg().get("vocabulary", ""),
        # Terms mined from the user's own meeting notes — shown separately from the
        # hand-typed list above, since these were learned rather than chosen.
        "learnedVocabulary": vocabulary_store.list_terms(),
        "learnedMinCount": vocabulary_store.MIN_COUNT,
    }


@router.post("/vocabulary/block")
async def block_learned_term(request: Request):
    """Stop a learned term biasing transcription (or restore one). Blocks rather than
    deletes — a delete would be undone by the next note mentioning the term."""
    body = await request.json()
    term = (body.get("term") or "").strip()
    if not term:
        return {"ok": False}
    if body.get("blocked") is False:
        vocabulary_store.unblock(term)
    else:
        vocabulary_store.block(term)
    return {"ok": True, "learnedVocabulary": vocabulary_store.list_terms()}


@router.post("/vocabulary/clear")
async def clear_learned_vocabulary():
    """Forget every learned term. The hand-typed Settings vocabulary is untouched."""
    vocabulary_store.clear()
    return {"ok": True, "learnedVocabulary": vocabulary_store.list_terms()}


@router.post("/models/select")
async def select_model(request: Request):
    body = await request.json()
    patch = {}
    if body.get("backend"):
        patch["backend"] = body["backend"]
    if body.get("model"):
        patch["model"] = body["model"]
    if patch:
        app_settings.patch_recording(patch)
        transcribe.reset_model()  # reload on next transcription
    if "language" in body:
        app_settings.patch_recording({"language": body["language"]})
    if "vocabulary" in body:
        app_settings.patch_recording({"vocabulary": body["vocabulary"]})
    return list_models_route()


@router.post("/models/{name}/download")
async def download_model_route(name: str, request: Request):
    body = await request.json() if await request.body() else {}
    backend = body.get("backend") or transcribe.DEFAULT_BACKEND
    ok = await asyncio.to_thread(transcribe.download_model, name, backend)
    return {"ok": ok, "models": transcribe.list_models()}
