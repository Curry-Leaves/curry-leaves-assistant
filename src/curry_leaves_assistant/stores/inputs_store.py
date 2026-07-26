"""Organized store for raw INPUTS fed to the knowledge base (docs, pastes).

Every input lands in its own directory so an agent can read it CHUNK BY CHUNK on
demand instead of us stuffing the whole thing into a prompt — the agent controls
its own context window.

    .inputs/{docId}/
        meta.json          # {docId, title, kind, filename, chars, chunkCount, status, created}
        document.md        # the full rendered markdown (source of truth for this input)
        source.<ext>       # the original uploaded file, when it came from one
        chunks/000.md …    # deterministic, bounded slices for paged reading

``docId = d-{sha1(markdown)}`` is deterministic → re-feeding the same content is a
no-op that just re-points at the existing directory.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from curry_leaves_assistant.core.paths import KNOWLEDGE_DIR
from curry_leaves_assistant.core.store import now_iso

INPUTS_DIR = KNOWLEDGE_DIR / ".inputs"
CHUNK_CHARS = 40000  # ~10k tokens per chunk — the agent pages through these in one run


def doc_id_for(markdown: str) -> str:
    return "d-" + hashlib.sha1((markdown or "").strip().encode("utf-8")).hexdigest()[:8]


def _dir(doc_id: str) -> Path:
    return INPUTS_DIR / doc_id


# ─── chunking ────────────────────────────────────────────────────────────────
def _split(md: str, budget: int = CHUNK_CHARS) -> list[str]:
    """Split markdown into bounded chunks on heading / paragraph boundaries."""
    md = (md or "").strip()
    if len(md) <= budget:
        return [md] if md else []
    # Break before headings, then pack blocks up to the budget; hard-slice giants.
    blocks: list[str] = []
    for part in re.split(r"\n(?=#{1,6}\s)", md):
        if len(part) <= budget:
            blocks.append(part)
            continue
        cur = ""
        for para in part.split("\n\n"):
            if len(cur) + len(para) + 2 > budget and cur:
                blocks.append(cur); cur = ""
            if len(para) > budget:
                for i in range(0, len(para), budget):
                    blocks.append(para[i:i + budget])
            else:
                cur = (cur + "\n\n" + para) if cur else para
        if cur:
            blocks.append(cur)
    chunks: list[str] = []
    cur = ""
    for b in blocks:
        if len(cur) + len(b) + 2 > budget and cur:
            chunks.append(cur.strip()); cur = ""
        cur = (cur + "\n\n" + b) if cur else b
    if cur.strip():
        chunks.append(cur.strip())
    return chunks or [md]


def _head(text: str, n: int = 80) -> str:
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:n]
    return text[:n].strip()


# ─── write ───────────────────────────────────────────────────────────────────
def save(title: str | None, markdown: str, *, kind: str,
         filename: str | None = None, raw: bytes | None = None, source_ext: str = "") -> dict:
    """Persist an input into its own directory + chunk files. Returns its meta.
    Idempotent: identical markdown → same docId → overwrites the same directory."""
    markdown = (markdown or "").strip()
    if not markdown:
        raise ValueError("empty document")
    doc_id = doc_id_for(markdown)
    d = _dir(doc_id)
    existed = d.exists()
    (d / "chunks").mkdir(parents=True, exist_ok=True)
    (d / "document.md").write_text(markdown)
    if raw is not None and source_ext:
        (d / f"source{source_ext}").write_bytes(raw)
    chunks = _split(markdown)
    # Clear any stale chunk files, then write the current ones.
    for old in (d / "chunks").glob("*.md"):
        old.unlink()
    for i, ch in enumerate(chunks):
        (d / "chunks" / f"{i:03}.md").write_text(ch)
    meta = {
        "docId": doc_id, "title": title, "kind": kind, "filename": filename,
        "chars": len(markdown), "chunkCount": len(chunks),
        "status": "pending", "created": now_iso(), "duplicate": existed,
    }
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


def doc_id_for_bytes(raw: bytes) -> str:
    """Deterministic id from the raw file bytes (known before conversion)."""
    return "d-" + hashlib.sha1(raw or b"").hexdigest()[:8]


def stage_source(title: str | None, *, kind: str, filename: str | None,
                 raw: bytes, source_ext: str) -> dict:
    """Phase 1: persist the raw upload and a `converting` meta, before the (slow) render.
    Returns the meta immediately so the HTTP upload can return without waiting on OCR."""
    doc_id = doc_id_for_bytes(raw)
    d = _dir(doc_id)
    existed = (d / "document.md").exists()
    (d / "chunks").mkdir(parents=True, exist_ok=True)
    (d / f"source{source_ext}").write_bytes(raw)
    meta = {
        "docId": doc_id, "title": title, "kind": kind, "filename": filename,
        "chars": 0, "chunkCount": 0, "status": "converting",
        "created": now_iso(), "duplicate": existed,
    }
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


def source_path(doc_id: str) -> Path | None:
    d = _dir(doc_id)
    for p in sorted(d.glob("source.*")):
        return p
    return None


def finalize(doc_id: str, markdown: str) -> dict:
    """Phase 2: after conversion, write document.md + chunk files and mark it queued."""
    d = _dir(doc_id)
    markdown = (markdown or "").strip()
    (d / "document.md").write_text(markdown)
    for old in (d / "chunks").glob("*.md"):
        old.unlink()
    chunks = _split(markdown)
    for i, ch in enumerate(chunks):
        (d / "chunks" / f"{i:03}.md").write_text(ch)
    meta = get(doc_id) or {"docId": doc_id}
    meta.update(chars=len(markdown), chunkCount=len(chunks), status="queued")
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


def set_status(doc_id: str, status: str) -> None:
    m = get(doc_id)
    if m:
        m["status"] = status
        (_dir(doc_id) / "meta.json").write_text(json.dumps(m, ensure_ascii=False, indent=1))


# ─── read ────────────────────────────────────────────────────────────────────
def get(doc_id: str) -> dict | None:
    p = _dir(doc_id) / "meta.json"
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def list_inputs(status: str | None = None) -> list[dict]:
    out = []
    if INPUTS_DIR.exists():
        for d in INPUTS_DIR.iterdir():
            m = get(d.name)
            if m and (status is None or m.get("status") == status):
                out.append(m)
    out.sort(key=lambda m: m.get("created") or "", reverse=True)
    return out


def chunk(doc_id: str, index: int) -> dict | None:
    """One chunk of an input, with paging metadata (index, total, has_more)."""
    m = get(doc_id)
    if not m:
        return None
    n = m.get("chunkCount") or 0
    if index < 0 or index >= n:
        return None
    text = (_dir(doc_id) / "chunks" / f"{index:03}.md").read_text()
    return {"docId": doc_id, "title": m.get("title"), "index": index,
            "total": n, "has_more": index < n - 1, "text": text}


def outline(doc_id: str) -> dict | None:
    """A map of the input's chunks (index · size · first heading) for navigation."""
    m = get(doc_id)
    if not m:
        return None
    items = []
    for i in range(m.get("chunkCount") or 0):
        t = (_dir(doc_id) / "chunks" / f"{i:03}.md").read_text()
        items.append({"index": i, "chars": len(t), "head": _head(t)})
    return {"docId": doc_id, "title": m.get("title"), "chunkCount": len(items), "chunks": items}
