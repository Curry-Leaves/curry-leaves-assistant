"""Artifact registry (management, requires the app PIN) plus the public capability
routes recipients use to view/download a shared artifact with no login at all — the
per-artifact `shareToken` in the URL IS the auth (see core/auth.py's `/a/` bypass and
stores/artifact_store.check_token).
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from curry_leaves_assistant.core import doc_text, events, image_meta
from curry_leaves_assistant.stores import artifact_store

router = APIRouter(tags=["artifacts"])

# Served artifact HTML gets a CSP that blocks it from phoning home: artifacts inline their
# own CSS/JS, and this keeps a prompt-injected or malicious deck from calling out from a
# viewer's browser. `img-src` stays permissive so a deck can show its own uploaded
# reference assets (served same-origin from this artifact's own dir) and data: URIs.
_ARTIFACT_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "img-src * data: blob:; "
    "font-src data:; "
    "media-src data: blob:;"
)

# Assets are served as their own top-level document when opened directly, so they get a
# CSP of their own. It matters most for SVG, which is an XML document that can carry
# script: `sandbox` neutralises it, and nosniff stops a mislabelled file being re-typed
# into something executable. (Embedded via <img> an SVG can't run script anyway.)
_ASSET_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox;",
    "X-Content-Type-Options": "nosniff",
}

# An upload cap that comfortably holds a print-resolution photo while bounding what a
# single request can buffer in memory.
MAX_ASSET_BYTES = 10 * 1024 * 1024


# ─── Management (PIN-gated, like every other /... route) ──────────────────────
@router.get("/artifacts")
def list_artifacts():
    return artifact_store.list_artifacts()


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str):
    meta = artifact_store.read_meta(artifact_id)
    return meta or Response(status_code=404)


@router.delete("/artifacts/{artifact_id}")
def delete_artifact(artifact_id: str):
    ok = artifact_store.delete(artifact_id)
    if ok:
        events.emit("artifact.deleted", entity_id=artifact_id)
    return {"ok": ok}


@router.post("/artifacts/{artifact_id}/regenerate-token")
def regenerate_token(artifact_id: str):
    meta = artifact_store.regenerate_token(artifact_id)
    return meta or Response(status_code=404)


# ─── Content editing (PIN-gated) — the UI's equivalent of read/save_artifact ──
# Legacy decks built via the retired markdown builder still carry a source.md sidecar;
# for those, a manual edit re-renders through the presenter so the deck stays consistent.
# New decks (and everything else) are hand-written HTML — the entry file is edited directly.
class _ContentBody(BaseModel):
    content: str


@router.get("/artifacts/{artifact_id}/content")
def get_artifact_content(artifact_id: str):
    meta = artifact_store.read_meta(artifact_id)
    if meta is None:
        return Response(status_code=404)
    source = artifact_store.asset_path(artifact_id, "source.md")
    if source is not None:
        return {"format": "deck-markdown", "content": source.read_text(encoding="utf-8")}
    p = artifact_store.entry_path(artifact_id)
    if p is None:
        return Response(status_code=404)
    try:
        return {"format": "entry", "content": p.read_text(encoding="utf-8")}
    except UnicodeDecodeError:
        return JSONResponse({"detail": "entry file is binary — not editable"}, status_code=415)


@router.put("/artifacts/{artifact_id}/content")
def update_artifact_content(artifact_id: str, body: _ContentBody):
    meta = artifact_store.read_meta(artifact_id)
    if meta is None:
        return Response(status_code=404)
    if artifact_store.asset_path(artifact_id, "source.md") is not None:
        from curry_leaves_presenter import Deck, PresenterError

        try:
            deck = Deck.from_string(body.content)
            errors = [d for d in deck.validate() if d.severity == "error"]
            if errors:
                msgs = [f"[{d.code}] slide {d.slide_index}: {d.message}" for d in errors]
                return JSONResponse({"errors": msgs}, status_code=422)
            html = deck.render_html()
        except PresenterError as e:
            return JSONResponse({"errors": [str(e)]}, status_code=422)
        meta = artifact_store.update(artifact_id, html)
        artifact_store.write_asset(artifact_id, "source.md", body.content)
    else:
        meta = artifact_store.update(artifact_id, body.content)
    if meta is None:
        return Response(status_code=404)
    events.emit("artifact.saved", payload=meta, entity_id=artifact_id, label=meta["title"])
    return meta


# ─── Reference assets (PIN-gated) ─────────────────────────────────────────────
# Images the user supplies for an artifact to use — a logo, a photo, a screenshot. The
# agent reads them back through `artifacts_read(action='assets')` and refers to them by
# relative path in the HTML it writes.
@router.get("/artifacts/{artifact_id}/assets")
def list_artifact_assets(artifact_id: str):
    if artifact_store.read_meta(artifact_id) is None:
        return Response(status_code=404)
    return artifact_store.list_assets(artifact_id)


@router.post("/artifacts/{artifact_id}/assets")
async def upload_artifact_asset(artifact_id: str, request: Request, filename: str):
    """Raw body + `?filename=`, matching how chat attachments and KB ingest upload."""
    if artifact_store.read_meta(artifact_id) is None:
        return Response(status_code=404)

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_ASSET_BYTES:
        return JSONResponse({"error": "file too large"}, status_code=413)
    raw = await request.body()
    if not raw:
        return JSONResponse({"error": "empty body"}, status_code=400)
    # Re-check against the real length: a Content-Length header can lie.
    if len(raw) > MAX_ASSET_BYTES:
        return JSONResponse({"error": "file too large"}, status_code=413)

    name = doc_text.safe_name(filename)
    ext = Path(name).suffix.lower()
    if ext not in image_meta.UPLOAD_IMAGE_TYPES:
        return JSONResponse(
            {"error": f"unsupported type {ext or '(none)'}; allowed: "
                      + ", ".join(sorted(image_meta.UPLOAD_IMAGE_TYPES))},
            status_code=415,
        )
    # Trust the bytes, not the extension — a script renamed .png must not be stored.
    sniffed = image_meta.sniff(raw)
    if sniffed is None or sniffed != image_meta.UPLOAD_IMAGE_TYPES[ext]:
        return JSONResponse(
            {"error": f"file content is not a valid {ext} image"}, status_code=415
        )

    from curry_leaves_assistant.core.paths import artifact_dir

    dest_dir = artifact_dir(artifact_id) / artifact_store.ASSETS_SUBDIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = doc_text.unique(dest_dir, name).name
    rel = f"{artifact_store.ASSETS_SUBDIR}/{final}"
    if not artifact_store.write_asset_bytes(artifact_id, rel, raw):
        return JSONResponse({"error": "could not write asset"}, status_code=400)

    artifact_store.touch(artifact_id)
    events.emit("artifact.saved", entity_id=artifact_id, label=f"asset {final}")
    item = {"path": rel, "name": final, "size": len(raw),
            "kind": doc_text.artifact_kind(ext)}
    dims = image_meta.dimensions(dest_dir / final)
    if dims:
        item["width"], item["height"] = dims
    return item


@router.delete("/artifacts/{artifact_id}/assets/{path:path}")
def delete_artifact_asset(artifact_id: str, path: str):
    if artifact_store.read_meta(artifact_id) is None:
        return Response(status_code=404)
    if artifact_store.asset_path(artifact_id, path) is None:
        return Response(status_code=404)
    artifact_store.delete_asset(artifact_id, path)
    artifact_store.touch(artifact_id)
    events.emit("artifact.saved", entity_id=artifact_id, label=f"removed {path}")
    return {"ok": True}


# ─── Public capability routes — no PIN, token in the path is the auth ─────────
@router.get("/a/{artifact_id}/{token}/")
def view_artifact(artifact_id: str, token: str):
    meta = artifact_store.check_token(artifact_id, token)
    if meta is None:
        return Response(status_code=404)
    p = artifact_store.entry_path(artifact_id)
    if p is None:
        return Response(status_code=404)
    if p.suffix in (".html", ".htm"):
        return HTMLResponse(p.read_text(encoding="utf-8"), headers={"Content-Security-Policy": _ARTIFACT_CSP})
    mime, _ = mimetypes.guess_type(str(p))
    return FileResponse(p, media_type=mime or "text/plain")


@router.get("/a/{artifact_id}/{token}/download")
def download_artifact(artifact_id: str, token: str):
    meta = artifact_store.check_token(artifact_id, token)
    if meta is None:
        return Response(status_code=404)
    from curry_leaves_assistant.core.paths import artifact_dir

    d = artifact_dir(artifact_id)
    entry_p = d / meta["entry"]
    # meta.json and source.md are internal bookkeeping (registry record / deck markdown
    # for read_artifact), not user assets — they alone shouldn't turn a download into a zip.
    # Recursive: uploaded reference images live in assets/, and an entry file that depends
    # on them is no longer self-contained, so it has to download as a zip or the images
    # 404 for whoever opens it.
    other_files = [f for f in d.rglob("*")
                   if f.is_file() and f.name not in artifact_store.INTERNAL_FILES and f != entry_p]
    safe_title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in meta["title"]).strip() or artifact_id
    if not other_files:
        return FileResponse(entry_p, media_type="application/octet-stream",
                             filename=f"{safe_title}{entry_p.suffix}")
    zpath = artifact_store.zip_artifact(artifact_id)
    if zpath is None:
        return Response(status_code=404)
    return FileResponse(zpath, media_type="application/zip", filename=f"{safe_title}.zip")


@router.get("/a/{artifact_id}/{token}/{path:path}")
def view_artifact_asset(artifact_id: str, token: str, path: str):
    """A non-entry asset alongside the entry file (images, extra pages, etc.)."""
    meta = artifact_store.check_token(artifact_id, token)
    if meta is None:
        return Response(status_code=404)
    # The share link grants viewing the deliverable, not the bookkeeping beside it:
    # meta.json holds the shareToken itself, so serving it would hand a recipient the
    # capability to keep after the link is revoked.
    if Path(path).name in artifact_store.INTERNAL_FILES:
        return Response(status_code=404)
    p = artifact_store.asset_path(artifact_id, path)
    if p is None:
        return Response(status_code=404)
    mime, _ = mimetypes.guess_type(str(p))
    return FileResponse(p, media_type=mime or "application/octet-stream",
                        headers=_ASSET_HEADERS)
