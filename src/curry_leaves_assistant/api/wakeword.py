"""Wake-word config + model delivery.

Detection runs client-side, so this router's job is to hand the renderer its fused
ONNX model and persist which one is active. The weights are served from
~/.curry-leaves/models/wakeword/ rather than bundled into the web build: they are ~3 MB
for a default-off feature, and bundling would hardcode the model set into the wheel —
the opposite of the drop-in-your-own-model seam domain/wakeword.py is built around.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse

from curry_leaves_assistant.core import settings as app_settings
from curry_leaves_assistant.domain import wakeword

router = APIRouter(prefix="/wakeword", tags=["wakeword"])


@router.get("")
def get_wakeword():
    from curry_leaves_assistant.domain import tts
    cfg = app_settings.wakeword_cfg()
    return {
        **cfg,
        "available": wakeword.available(),
        "heads": wakeword.list_heads(),
        "activeHead": wakeword.active_head(),
        # Enumerated from the installed model rather than a hardcoded table, so the
        # picker can't offer a voice this install doesn't have.
        "voices": tts.list_voices(),
        "defaultVoice": tts.DEFAULT_VOICE,
        "ttsAvailable": tts.available(),
        # Whether the Kokoro weights are on disk. Spoken replies stay unavailable until
        # this is true; the setup/settings UI offers a download to flip it (never at boot).
        "ttsDownloaded": tts.is_downloaded(),
    }


@router.patch("")
async def patch_wakeword_route(request: Request):
    body = await request.json()
    patch = {}
    for k in ("enabled", "active", "threshold", "speak", "continuous", "autoDismiss",
              "dismissAfterMs", "voice", "silenceMs", "workApproval"):
        if k in body:
            patch[k] = body[k]
    if patch:
        app_settings.patch_wakeword(patch)
    return get_wakeword()


@router.post("/download")
async def download_route(request: Request):
    """Fetch the shared stages + one head. Threaded — it's a few MB over the network."""
    body = await request.json() if await request.body() else {}
    head = body.get("head") or None
    try:
        await asyncio.to_thread(wakeword.download, head)
    except Exception as e:  # noqa: BLE001 - surface the reason to the settings UI
        return {"ok": False, "error": str(e), **get_wakeword()}
    return get_wakeword()


@router.post("/tts/download")
async def tts_download_route():
    """Fetch the Kokoro weights so spoken replies work. Triggered explicitly from setup or
    settings — never at boot — so an install that never enables voice never pulls this.
    Threaded: the weights are ~300 MB, and warming builds the pipeline while we're at it so
    the first speak() after this is instant."""
    from curry_leaves_assistant.domain import tts
    try:
        await asyncio.to_thread(tts.warm)  # downloads if absent, then builds the pipeline
    except Exception as e:  # noqa: BLE001 - surface the reason to the settings UI
        return {"ok": False, "error": str(e), **get_wakeword()}
    return get_wakeword()


@router.get("/models/{name}")
def get_model(name: str):
    """Serve one .onnx to the renderer.

    Deliberately NOT in auth.PUBLIC_PATHS — that would let an unauthenticated caller
    read files out of the models dir. The renderer fetches these with its auth header
    and hands the ArrayBuffer to the worker, so onnxruntime never fetches a URL itself
    and no bearer token has to be smuggled into a query string.
    """
    p = wakeword.resolve_path(name)
    if p is None:
        return Response(status_code=404)
    # Weights for a given filename never change, so let the browser keep them rather
    # than refetch megabytes on every app launch.
    return FileResponse(
        p,
        media_type="application/octet-stream",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
