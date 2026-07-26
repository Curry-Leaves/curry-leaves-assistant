"""Dictation of short audio clips (request/response).

Live/streaming transcription moved to the shared WebSocket — see api/ws.py's audio
handler and domain/transcribe.LiveTranscriber. This keeps only the one-shot clip endpoint.
"""
from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Request

from curry_leaves_assistant.domain import transcribe

router = APIRouter(tags=["transcription"])


@router.post("/transcribe")
async def transcribe_audio(request: Request):
    """Dictation: transcribe a short audio clip (raw webm bytes) → text."""
    import tempfile
    data = await request.body()
    fd, tmp = tempfile.mkstemp(suffix=".webm")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    try:
        text = await asyncio.to_thread(transcribe.transcribe_file, tmp)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    return {"text": text or ""}
