"""Registry for LLM-generated deliverables — presentations, reports, one-page sites,
diagrams, anything an agent produces that outlives the chat turn.

Same shape as recordings: one directory per artifact, no central index file — the
registry IS the directory scan. `meta.json` holds the record; `entry` names the file
to serve/download (self-contained HTML is the convention, enforced by the tool layer
and the presentation skill, not by this store).

Sharing is a capability URL: a random `shareToken` minted at creation is the only
auth for the public `/a/<id>/<token>/...` routes (see api/artifacts.py + core/auth.py).
Regenerating the token invalidates every link handed out before that point.
"""
from __future__ import annotations

import secrets
import shutil
import uuid
from pathlib import Path

from curry_leaves_assistant.core import doc_text, image_meta
from curry_leaves_assistant.core.paths import ARTIFACTS_DIR, artifact_dir, artifact_file_path, artifact_meta_path
from curry_leaves_assistant.core.store import now_iso, read_json, write_json

KINDS = {"presentation", "report", "page", "diagram", "other"}

# Bookkeeping files that live in an artifact dir but are not part of the deliverable:
# the registry record, and the legacy markdown sidecar from the retired deck builder.
# Shared so `list_assets` and the download's file-vs-zip decision cannot drift apart.
INTERNAL_FILES = {"meta.json", "source.md"}

# Where uploaded reference images go. Namespaced so a user's assets are never confused
# with a generated sibling file, and so `<img src="assets/logo.png">` reads clearly.
ASSETS_SUBDIR = "assets"


def _new_id() -> str:
    return "a-" + uuid.uuid4().hex[:8]


def _new_token() -> str:
    return secrets.token_hex(16)


def create(title: str, entry_content: str, *, kind: str = "other", entry: str = "index.html",
           description: str | None = None, agent_id: str | None = None,
           source: dict | None = None) -> dict:
    """Create a new artifact. `entry_content` is the full text of the entry file."""
    artifact_id = _new_id()
    d = artifact_dir(artifact_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / entry).write_text(entry_content, encoding="utf-8")
    meta = {
        "id": artifact_id,
        "title": title.strip() if title else "Untitled",
        "kind": kind if kind in KINDS else "other",
        "description": description or "",
        "entry": entry,
        "agentId": agent_id,
        "source": source,
        "shareToken": _new_token(),
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
    }
    write_json(artifact_meta_path(artifact_id), meta)
    return meta


def update(artifact_id: str, entry_content: str, *, title: str | None = None,
           kind: str | None = None, description: str | None = None,
           entry: str | None = None) -> dict | None:
    """Replace an existing artifact's entry file in place — the id and share link
    stay stable, so a saved link/bookmark keeps working after the content changes."""
    meta = read_meta(artifact_id)
    if meta is None:
        return None
    d = artifact_dir(artifact_id)
    new_entry = entry or meta["entry"]
    if new_entry != meta["entry"]:
        old_path = d / meta["entry"]
        if old_path.exists():
            old_path.unlink()
    (d / new_entry).write_text(entry_content, encoding="utf-8")
    meta["entry"] = new_entry
    if title:
        meta["title"] = title.strip()
    if kind and kind in KINDS:
        meta["kind"] = kind
    if description is not None:
        meta["description"] = description
    meta["updatedAt"] = now_iso()
    write_json(artifact_meta_path(artifact_id), meta)
    return meta


def read_meta(artifact_id: str) -> dict | None:
    p = artifact_meta_path(artifact_id)
    if not p.exists():
        return None
    return read_json(p, None)


def entry_path(artifact_id: str) -> Path | None:
    meta = read_meta(artifact_id)
    if meta is None:
        return None
    p = artifact_file_path(artifact_id, meta["entry"])
    return p if p.exists() else None


def asset_path(artifact_id: str, rel: str) -> Path | None:
    """A non-entry file inside the artifact dir (escape-checked)."""
    meta = read_meta(artifact_id)
    if meta is None:
        return None
    try:
        p = artifact_file_path(artifact_id, rel)
    except ValueError:
        return None
    return p if p.is_file() else None


def write_asset(artifact_id: str, rel: str, content: str) -> bool:
    """Write a non-entry file inside the artifact dir (escape-checked). Used to keep
    an editable source (e.g. a deck's markdown) next to the rendered entry file.
    False if the artifact doesn't exist or the path escapes."""
    if read_meta(artifact_id) is None:
        return False
    try:
        p = artifact_file_path(artifact_id, rel)
    except ValueError:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return True


def write_asset_bytes(artifact_id: str, rel: str, data: bytes) -> bool:
    """Binary sibling of `write_asset` — user-uploaded reference images an artifact's
    HTML then refers to by relative path (`<img src="assets/logo.png">`). Kept as its own
    function rather than a `str | bytes` union so the encoding decision stays explicit at
    each call site."""
    if read_meta(artifact_id) is None:
        return False
    try:
        p = artifact_file_path(artifact_id, rel)
    except ValueError:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return True


def list_assets(artifact_id: str) -> list[dict]:
    """Every non-internal file in the artifact dir, recursively.

    Paths come back POSIX-style and artifact-relative, i.e. exactly what belongs in the
    HTML's `src` — a Windows backslash path would not resolve in the browser.
    """
    meta = read_meta(artifact_id)
    if meta is None:
        return []
    d = artifact_dir(artifact_id)
    if not d.is_dir():
        return []
    entry = meta.get("entry") or "index.html"
    out: list[dict] = []
    for p in d.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(d).as_posix()
        if rel in INTERNAL_FILES or rel == entry:
            continue
        stat = p.stat()
        item = {
            "path": rel,
            "name": p.name,
            "size": stat.st_size,
            "kind": doc_text.artifact_kind(p.suffix),
        }
        dims = image_meta.dimensions(p)
        if dims:
            item["width"], item["height"] = dims
        out.append(item)
    out.sort(key=lambda a: a["path"])
    return out


def touch(artifact_id: str) -> dict | None:
    """Bump `updatedAt` without changing content — for side-file changes (an asset added
    or removed) that the registry should still reflect as a modification."""
    meta = read_meta(artifact_id)
    if meta is None:
        return None
    meta["updatedAt"] = now_iso()
    write_json(artifact_meta_path(artifact_id), meta)
    return meta


def delete_asset(artifact_id: str, rel: str) -> None:
    """Remove a non-entry file if present (e.g. a stale source.md once the entry is
    no longer generated from it)."""
    p = asset_path(artifact_id, rel)
    if p is not None:
        p.unlink()


def list_artifacts() -> list[dict]:
    out = []
    if ARTIFACTS_DIR.exists():
        for d in ARTIFACTS_DIR.iterdir():
            if not d.is_dir():
                continue
            meta = read_json(d / "meta.json", None)
            if meta:
                out.append(meta)
    out.sort(key=lambda m: m.get("updatedAt") or "", reverse=True)
    return out


def regenerate_token(artifact_id: str) -> dict | None:
    meta = read_meta(artifact_id)
    if meta is None:
        return None
    meta["shareToken"] = _new_token()
    meta["updatedAt"] = now_iso()
    write_json(artifact_meta_path(artifact_id), meta)
    return meta


def check_token(artifact_id: str, token: str) -> dict | None:
    """Returns the meta iff `token` matches (constant-time), else None."""
    import hmac

    meta = read_meta(artifact_id)
    if meta is None or not token:
        return None
    if not hmac.compare_digest(meta.get("shareToken", ""), token):
        return None
    return meta


def delete(artifact_id: str) -> bool:
    d = artifact_dir(artifact_id)
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True


def zip_artifact(artifact_id: str) -> Path | None:
    """Zip the whole artifact dir into a temp file for download when it has more than
    just the entry file. Caller is responsible for cleaning up the returned path."""
    meta = read_meta(artifact_id)
    if meta is None:
        return None
    d = artifact_dir(artifact_id)
    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(prefix="cl-artifact-"))
    archive_base = tmp_dir / artifact_id
    archive_path = shutil.make_archive(str(archive_base), "zip", root_dir=str(d))
    return Path(archive_path)
