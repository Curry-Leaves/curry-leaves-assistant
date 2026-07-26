"""Direct document intake for the knowledge base — the 'feed docs' entry point.

Paste text or upload a file → render to markdown → store it as an organized INPUT
(``inputs_store``) split into bounded chunks → fire ``knowledge.ingest.requested``.
The event carries only the input id + chunk count, NOT the text: the Knowledge Keeper
reads the input CHUNK BY CHUNK with the read_input tool, so a huge document never has
to fit in a single prompt.
"""
from __future__ import annotations

from pathlib import Path

from curry_leaves_assistant.core import doc_text

from curry_leaves_assistant.core import events

from curry_leaves_assistant.stores import inputs_store



def submit_text(text: str, title: str | None = None) -> dict:
    """Feed pasted text into the knowledge base."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty document")
    meta = inputs_store.save(title or None, text, kind="paste")
    return _dispatch(meta)


def submit_file(filename: str, raw: bytes) -> dict:
    """Phase 1 (fast): stage the uploaded file. Conversion to markdown (docling OCR for
    PDFs — can take minutes) happens later via convert(), off the request path."""
    if not raw:
        raise ValueError("empty file")
    inputs_store.INPUTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = doc_text.safe_name(filename or "document")
    title = Path(filename).stem.replace("-", " ").replace("_", " ").strip() or filename
    meta = inputs_store.stage_source(title, kind="file", filename=filename,
                                     raw=raw, source_ext=Path(safe).suffix)
    return {"ok": True, "docId": meta["docId"], "title": meta.get("title"),
            "chars": 0, "chunkCount": 0, "duplicate": meta.get("duplicate"),
            "status": "converting"}


def convert(doc_id: str) -> None:
    """Phase 2 (slow, background): render the staged file to markdown, chunk it, and
    dispatch it to the doc-filer. On failure the input is marked so the UI can show it."""
    src = inputs_store.source_path(doc_id)
    if src is None:
        inputs_store.set_status(doc_id, "failed")
        return
    try:
        md = (doc_text.to_markdown(src, src.read_bytes()) or "").strip()
    except Exception as exc:
        print(f"[ingest] conversion failed for {doc_id}: {exc}", flush=True)
        inputs_store.set_status(doc_id, "failed")
        return
    if not md or md.startswith("*(Could not") or md.startswith("*(No extractable"):
        inputs_store.set_status(doc_id, "failed")
        return
    meta = inputs_store.finalize(doc_id, md)
    _dispatch(meta)


def _dispatch(meta: dict) -> dict:
    """Fire ONE intake event for the whole document → the pool routes it to a single
    doc-filer job. We do NOT chunk-and-fan-out: the markdown lives in the input's dir and
    the agent reads through it with its tools and files everything itself."""
    payload = {"docId": meta["docId"], "title": meta.get("title"), "kind": meta.get("kind"),
               "chunkCount": meta.get("chunkCount"), "chars": meta.get("chars"),
               "duplicate": meta.get("duplicate"), "filename": meta.get("filename")}
    inputs_store.set_status(meta["docId"], "queued")
    events.emit("knowledge.ingest.requested", payload=payload,
                entity_id=meta["docId"], label=meta.get("title") or "document")
    return {"ok": True, "docId": meta["docId"], "title": meta.get("title"),
            "chars": meta.get("chars"), "chunkCount": meta.get("chunkCount"),
            "duplicate": meta.get("duplicate")}
