"""Tiny atomic, crash-durable JSON helpers. No database — just files under ~/.curry-leaves.

Every whole-file write goes tmp-file → fsync(file) → os.replace → fsync(dir). The two
fsyncs are what make it survive a power loss / kill: without fsync'ing the temp file the
rename can land pointing at data still in the page cache (→ a zero-length/truncated file),
and without fsync'ing the directory the rename itself may not be durable.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CorruptFileError(Exception):
    """A file exists but its contents couldn't be parsed — distinct from 'file absent'.
    Raised by read_json(strict=True) so a truncated/garbled file surfaces instead of
    silently reverting a caller to its default (which looks identical to a fresh install)."""


def read_json(path: Path, default: Any, *, strict: bool = False) -> Any:
    """Read + parse JSON. Missing file → `default`. A present-but-unparseable file →
    `default` too by default (back-compat), but with `strict=True` raises CorruptFileError
    so callers who care about integrity (settings, auth) can tell corruption from absence."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return default
    except Exception:
        return default
    try:
        return json.loads(text)
    except Exception as exc:
        if strict:
            raise CorruptFileError(f"{path} is present but unparseable: {exc}") from exc
        return default


def read_json_safe(path: Path, default: Any) -> Any:
    """Like read_json, but corruption is NOT silent: a present-but-unparseable file is
    quarantined (renamed to <name>.corrupt-<epoch>) and logged loudly, then `default` is
    returned so the app still starts. Use for integrity-critical files (settings, auth)
    where silently reverting to defaults would hide data loss and then overwrite the
    evidence on the next save."""
    try:
        return read_json(path, default, strict=True)
    except CorruptFileError as exc:
        try:
            # A monotonic-ish suffix without Date.now (kept import-light): use the file's
            # own mtime, falling back to a counter isn't needed — collisions just overwrite
            # an equally-corrupt quarantine copy.
            stamp = int(path.stat().st_mtime)
        except Exception:
            stamp = 0
        quarantine = path.with_name(f"{path.name}.corrupt-{stamp}")
        try:
            os.replace(path, quarantine)
            print(f"[store] CORRUPT FILE quarantined: {path} → {quarantine.name} ({exc})", flush=True)
        except Exception as move_exc:
            print(f"[store] CORRUPT FILE {path} (could not quarantine: {move_exc}): {exc}", flush=True)
        return default


def _fsync_dir(dir_path: Path) -> None:
    """Best-effort fsync of a directory so a just-completed rename is durable. Not all
    platforms/filesystems support this (notably Windows) — failure is non-fatal."""
    try:
        dfd = os.open(str(dir_path), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception:
        pass


def write_text_atomic(path: Path, text: str) -> None:
    """Atomic + durable write of arbitrary text (markdown, indexes, …): tmp file (fsync'd)
    → os.replace → fsync the parent dir. The durable primitive behind write_json and the
    knowledge base's file writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())  # data is on disk BEFORE the rename makes it visible
        os.replace(tmp, path)
        _fsync_dir(path.parent)  # the rename itself is now durable
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_json(path: Path, data: Any) -> None:
    """Atomic + durable write of JSON: see write_text_atomic."""
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False))


def append_ndjson(path: Path, obj: Any, *, max_lines: int | None = None) -> None:
    """Append one JSON line. If `max_lines` is set and the file exceeds ~2x that,
    rotate it down to the newest `max_lines` lines so an append-only log can't grow
    without bound. Rotation is amortized (only every ~max_lines appends) and durable.

    The per-line append is intentionally NOT fsync'd: this backs the events feed — a
    capped, best-effort activity log — and an fsync on every emit() would be a real cost
    for negligible gain (a lost tail line on a hard crash is acceptable there). The durable
    stores (settings, todos, run records, …) go through write_json, which IS fsync'd."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    if max_lines is not None:
        _maybe_rotate_ndjson(path, max_lines)


def _maybe_rotate_ndjson(path: Path, max_lines: int) -> None:
    try:
        # Count newlines cheaply (streamed in blocks, no full decode). Rotate only once
        # the file exceeds 2x the cap, so this trims ~max_lines at a time rather than on
        # every append — the count is the only per-append cost, and it's O(bytes) I/O.
        with path.open("rb") as f:
            n = sum(block.count(b"\n") for block in iter(lambda: f.read(1 << 20), b""))
        if n <= max_lines * 2:
            return
        lines = path.read_text().splitlines()
        kept = "\n".join(lines[-max_lines:]) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(kept)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            _fsync_dir(path.parent)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except Exception:
        pass  # rotation is best-effort; never break an append


def tail_lines(path: Path, limit: int) -> list[str]:
    """Return the last `limit` lines of a text file without loading the whole file.

    Reads fixed-size blocks backward from EOF until enough newlines are seen, so cost
    scales with `limit`, not with total file size (the events log is append-only)."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            block = 65536
            data = b""
            newlines = 0
            pos = end
            # +1: the trailing newline shouldn't count toward the lines we need.
            while pos > 0 and newlines <= limit:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
                newlines = data.count(b"\n")
    except FileNotFoundError:
        return []
    except Exception:
        return []
    return data.decode("utf-8", "replace").splitlines()[-limit:]
