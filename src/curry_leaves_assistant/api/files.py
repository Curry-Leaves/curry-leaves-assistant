"""Browse a few of the user's own folders, for the chat composer's `@file` picker.

Read-only and sandboxed — see stores/files_store.py for the roots and the containment rule.
Disabled outright unless the backend is bound to loopback (i.e. it is the user's own machine),
so a Docker/web deployment never exposes the server's filesystem.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from curry_leaves_assistant.stores import files_store

router = APIRouter(tags=["files"])


@router.get("/files/browse")
def browse(path: str | None = None):
    """One directory's contents. Empty `path` returns the browsable roots."""
    try:
        return files_store.browse(path)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
