"""Shared doc→markdown rendering for uploaded files (chat attachments & recording docs).

Text-like files are decoded directly; audio/video is transcribed via local Whisper;
richer formats (pdf/docx/xlsx/pptx/html/images/…) go through markitdown, with a decode
fallback and finally a placeholder. Used by both chat_sessions and recordings so an
attached document is rendered identically wherever it's uploaded.
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path

# Decoded straight to text.
_TEXT_EXT = {
    ".md", ".markdown", ".txt", ".text", ".log", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".rtf",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".scss", ".sh", ".bash", ".zsh",
    ".sql", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs",
    ".rb", ".php", ".swift", ".r", ".lua", ".pl", ".tex", ".env", ".gitignore",
}

# Audio/video — transcribed to text via local Whisper (ffmpeg decodes the container).
_AUDIO_EXT = {
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".webm",
    ".wma", ".aiff", ".aif", ".mp4", ".mov", ".m4v", ".mkv",
}

# ─── native-artifact classification (send to the LLM raw, not via markdown) ────
# Images and PDFs go to multimodal-capable models as native content blocks instead
# of a lossy markdown round-trip (OCR/extraction). The set below is what a provider
# can actually ingest as bytes; everything else keeps the to_markdown() text path.
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# Audio the multimodal wire formats accept as raw bytes (OpenAI input_audio). Narrower
# than _AUDIO_EXT (which is "anything Whisper can transcribe"): only these encode 1:1
# to an AudioBlock; other containers still fall back to a Whisper transcript.
_NATIVE_AUDIO = {".wav": "wav", ".mp3": "mp3"}


def artifact_kind(ext: str) -> str:
    """Classify a file extension into how it should reach the LLM:

    "image" | "pdf" | "audio" → a native multimodal block (bytes sent directly);
    "text"                    → rendered to markdown and inlined as text.

    Callers pair this with the run's provider (see chat_sessions.build_user_message)
    to decide native-vs-fallback, since audio is OpenAI-only and PDF/image support
    varies by model."""
    ext = ext.lower()
    if ext in _IMAGE_EXT:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in _NATIVE_AUDIO:
        return "audio"
    return "text"


def audio_format(ext: str) -> str | None:
    """The AudioBlock format ("wav"/"mp3") for a native-audio extension, else None."""
    return _NATIVE_AUDIO.get(ext.lower())


def media_type(path: Path) -> str:
    """Best-effort MIME type for an attachment (used for ImageBlock/FileBlock)."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "application/pdf"
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def safe_name(name: str) -> str:
    base = Path(name or "file").name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"
    return base[:120]


def unique(d: Path, name: str) -> Path:
    p = d / name
    if not p.exists():
        return p
    stem, suffix, i = p.stem, p.suffix, 2
    while True:
        cand = d / f"{stem}-{i}{suffix}"
        if not cand.exists():
            return cand
        i += 1


# ─── PDF → markdown via docling with forced full-page OCR ──────────────────────
# Many PDFs (Manning-style books, scans) have an obfuscated or absent text layer, so a
# plain text extract comes out as Caesar-shifted junk. Docling renders each page and OCRs
# it, giving clean markdown regardless. The converter loads ML models once, so cache it.
_docling_conv = None


def _pdf_to_markdown(path: Path) -> str:
    """Convert a PDF to markdown with docling (forced OCR). Blocking + slow (~seconds per
    page) — callers run it off the request path."""
    global _docling_conv
    if _docling_conv is None:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        opts = PdfPipelineOptions()
        opts.do_ocr = True
        opts.do_table_structure = True
        try:
            opts.ocr_options.force_full_page_ocr = True  # ignore the broken text layer
        except Exception:
            pass
        _docling_conv = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    return (_docling_conv.convert(str(path)).document.export_to_markdown() or "").strip()


def to_markdown(path: Path, raw: bytes) -> str:
    """Best-effort render of an uploaded file to markdown text."""
    ext = path.suffix.lower()
    if ext in _TEXT_EXT:
        return raw.decode("utf-8", errors="replace")
    if ext == ".pdf":
        try:
            md = _pdf_to_markdown(path)
            if md:
                return md
        except Exception as exc:
            print(f"[doc_text] docling failed for {path.name}: {exc}; falling back to markitdown", flush=True)
        # fall through to markitdown as a last resort
    if ext in _AUDIO_EXT:
        try:
            from curry_leaves_assistant.domain import transcribe

            text = (transcribe.transcribe_file(str(path)) or "").strip()
            body = text or "*(no speech detected)*"
            return f"# Transcript of {path.name}\n\n{body}"
        except Exception:
            return f"*(Could not transcribe `{path.name}`.)*"
    try:
        from markitdown import MarkItDown
        text = (MarkItDown().convert(str(path)).text_content or "").strip()
        if text:
            return text
        return f"*(No extractable text in `{path.name}` — it may be a scanned/image-only {ext.lstrip('.') or 'file'}.)*"
    except Exception:
        pass
    try:
        text = raw.decode("utf-8")
        if "\x00" not in text:
            return text
    except Exception:
        pass
    return f"*(Could not extract text from `{path.name}`.)*"
