"""Image attachments for knowledge notes — stored as real files inside the bundle.

An image pasted or dropped into a note is written to ``memory/assets/`` and referenced from
the markdown as a normal relative link (``![alt](assets/2026-08-08-<slug>.png)``). That choice
follows the repo's "data is plain files" rule and buys three things a base64 data-URI would
lose: the note stays small and greppable, the bundle keeps working when opened in another
markdown tool (Obsidian resolves the same relative path), and backup/restore already covers
the file because it zips the bundle wholesale.

Two properties are load-bearing for safety, since the bytes and the *name* both come from the
browser:

  • **Containment.** The stored name is rebuilt from scratch (date + slug + extension chosen by
    us from the sniffed type), never taken from the upload. A caller cannot steer the write with
    ``../`` or an absolute path because the untrusted string never reaches the path join intact.
  • **Type honesty.** The extension is derived from the magic bytes, not the client's declared
    content-type or filename. A ``.png`` that is really HTML would otherwise be served back from
    our own origin and run as script (stored XSS); serving it as the type it actually *is*, and
    refusing anything that is not a known image, closes that.
"""
from __future__ import annotations

import re
from datetime import datetime

from curry_leaves_assistant.core.paths import KNOWLEDGE_DIR

# Recognized image types, keyed by the extension we store. Sniffed from magic bytes rather
# than trusted from the client — see the module docstring. SVG is deliberately absent: it is
# an XML document that can carry <script>, so accepting it would reintroduce the stored-XSS
# hole this module exists to avoid. (Notes that want vector art already have ```svg fences,
# which the renderer sanitizes through DOMPurify.)
_SNIFFERS: tuple[tuple[str, str, object], ...] = (
    ("png",  "image/png",  lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
    ("jpg",  "image/jpeg", lambda b: b.startswith(b"\xff\xd8\xff")),
    ("gif",  "image/gif",  lambda b: b.startswith((b"GIF87a", b"GIF89a"))),
    ("webp", "image/webp", lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP"),
)

ASSETS_SUBDIR = "assets"

# A paste of a large screenshot is normal; a 25 MB one is a mistake or an attack on disk.
MAX_BYTES = 20 * 1024 * 1024


def sniff(data: bytes) -> tuple[str, str] | None:
    """Return ``(extension, mime)`` for recognized image bytes, else None."""
    for ext, mime, matches in _SNIFFERS:
        if matches(data):  # type: ignore[operator]
            return ext, mime
    return None


def content_type(rel: str) -> str:
    """The MIME type to serve a stored asset as, keyed off the extension WE assigned.

    Falls back to a non-renderable type rather than guessing: an unknown extension inside
    the assets dir should download, never execute in the page.
    """
    ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
    for e, mime, _ in _SNIFFERS:
        if e == ext:
            return mime
    return "application/octet-stream"


def _slug(name: str) -> str:
    """A short, filesystem-safe stem from the user's filename — for humans browsing the dir.

    Purely cosmetic: everything that makes the path *safe* comes from rebuilding it here, so a
    hostile name degrades to "image" instead of escaping.
    """
    stem = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    stem = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    return (stem[:48] or "image")


def save(data: bytes, filename: str = "image") -> dict:
    """Store image bytes in the bundle's assets dir; return the note-relative reference.

    The returned ``path`` is what goes in the markdown (``assets/<name>.png``) — bundle-relative
    so the link resolves both in our renderer and in any other markdown tool pointed at the vault.
    """
    if not data:
        raise ValueError("empty upload")
    if len(data) > MAX_BYTES:
        raise ValueError(f"image too large ({len(data) // 1024 // 1024} MB; limit is {MAX_BYTES // 1024 // 1024} MB)")
    kind = sniff(data)
    if kind is None:
        raise ValueError("unsupported image type — PNG, JPEG, GIF and WebP are accepted")
    ext, mime = kind

    d = KNOWLEDGE_DIR / ASSETS_SUBDIR
    d.mkdir(parents=True, exist_ok=True)

    # Date prefix keeps the directory chronologically browsable; the numeric suffix only
    # appears on a same-day name collision, so the common case stays a clean readable name.
    base = f"{datetime.now().strftime('%Y-%m-%d')}-{_slug(filename)}"
    name = f"{base}.{ext}"
    n = 2
    while (d / name).exists():
        name = f"{base}-{n}.{ext}"
        n += 1

    # Not routed through core/store.write_json (that helper is JSON-only), but the same
    # durability reasoning applies: write to a temp name in the SAME directory, then
    # os.replace, so a crash mid-write can't leave a truncated half-image that the note
    # already links to.
    target = d / name
    tmp = d / f".{name}.tmp"
    try:
        tmp.write_bytes(data)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)

    return {"path": f"{ASSETS_SUBDIR}/{name}", "mime": mime, "bytes": len(data)}
