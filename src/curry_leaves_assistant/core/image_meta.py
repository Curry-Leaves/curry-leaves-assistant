"""Image identification from header bytes — no decoding, no image library.

Two jobs, both about *not trusting a filename*:

  • `sniff` — what a file actually is, from its magic bytes. An upload named `.png` that
    is really a script must not be stored as an image.
  • `dimensions` — width/height for the asset manifest, read from the header alone.

Deliberately not Pillow. It is only a transitive dependency here (via markitdown/kokoro),
so relying on it would make an unpromised import load-bearing; and fully decoding
user-supplied images is a much larger attack surface than reading a fixed-size header.
Everything degrades to None rather than raising — a missing dimension is cosmetic.
"""
from __future__ import annotations

import struct
from pathlib import Path

# ext -> the media type we store it as, for the formats we accept as uploads.
UPLOAD_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

# Formats a model can actually be shown. SVG is storable and renders fine in a browser,
# but no provider accepts image/svg+xml, so it is never attached to a tool result.
MODEL_VIEWABLE = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def sniff(data: bytes) -> str | None:
    """The real media type from magic bytes, or None if it isn't an image we accept."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if _looks_like_svg(data):
        return "image/svg+xml"
    return None


def _looks_like_svg(data: bytes) -> bool:
    # SVG is text with no magic number; look for an <svg tag near the start, past any
    # XML declaration, BOM or leading comment.
    head = data[:1024].lstrip().lower()
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:].lstrip()
    return b"<svg" in head


def dimensions(path: Path) -> tuple[int, int] | None:
    """(width, height) parsed from the file header, or None if unavailable."""
    try:
        with path.open("rb") as fh:
            head = fh.read(32)
            if not head:
                return None
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                # IHDR is the first chunk; width/height are big-endian at offset 16.
                w, h = struct.unpack(">II", head[16:24])
                return int(w), int(h)
            if head[:6] in (b"GIF87a", b"GIF89a"):
                w, h = struct.unpack("<HH", head[6:10])
                return int(w), int(h)
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                return _webp_dimensions(fh, head)
            if head[:3] == b"\xff\xd8\xff":
                return _jpeg_dimensions(fh)
    except (OSError, struct.error, ValueError):
        return None
    return None


def _webp_dimensions(fh, head: bytes) -> tuple[int, int] | None:
    fh.seek(12)
    chunk = fh.read(18)
    if len(chunk) < 14:
        return None
    fourcc = chunk[:4]
    if fourcc == b"VP8X":
        # 24-bit little-endian, stored as (value - 1).
        w = int.from_bytes(chunk[12:15], "little") + 1
        h = int.from_bytes(chunk[15:18], "little") + 1
        return w, h
    if fourcc == b"VP8 ":
        w, h = struct.unpack("<HH", chunk[14:18])
        return w & 0x3FFF, h & 0x3FFF
    if fourcc == b"VP8L":
        bits = int.from_bytes(chunk[9:13], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def _jpeg_dimensions(fh) -> tuple[int, int] | None:
    # Walk the marker segments to the start-of-frame, which carries the real size.
    fh.seek(2)
    while True:
        b = fh.read(1)
        if not b:
            return None
        if b != b"\xff":
            continue
        marker = fh.read(1)
        while marker == b"\xff":  # fill bytes
            marker = fh.read(1)
        if not marker:
            return None
        m = marker[0]
        if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
            continue
        size_bytes = fh.read(2)
        if len(size_bytes) < 2:
            return None
        seg_len = struct.unpack(">H", size_bytes)[0]
        # SOF0-SOF15, excluding the non-frame markers DHT/JPG/DAC.
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            body = fh.read(5)
            if len(body) < 5:
                return None
            h, w = struct.unpack(">HH", body[1:5])
            return int(w), int(h)
        fh.seek(seg_len - 2, 1)
