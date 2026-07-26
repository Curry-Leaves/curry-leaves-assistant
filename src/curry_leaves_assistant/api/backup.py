"""Backup & restore of the ~/.curry-leaves data directory.

Export streams a zip of the whole data dir minus what shouldn't travel:
``models/`` (hundreds of MB of re-downloadable Whisper weights), ``queue/``
(in-flight kernel jobs), any top-level ``*.zip`` (so old backups never nest
inside new ones), and filesystem junk.

Restore swaps the data dir wholesale: the upload is extracted to a staging dir
(zip-slip checked), the current dir is renamed aside as a safety copy, staging
takes its place, and ``models/`` + ``queue/`` are carried over from the old dir
so restores never force a model re-download. The safety copy is kept on disk —
restore is destructive only after the user deletes it themselves. In-memory
state (kernel, caches) still reflects the old files, so the UI advises a
backend restart after a restore.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Response, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from curry_leaves_assistant.core.paths import DATA_DIR

router = APIRouter(tags=["backup"])

# Top-level entries that never go into a backup (and are preserved across a restore).
EXCLUDE_TOP = {"models", "queue"}
JUNK_NAMES = {".DS_Store", "__pycache__"}
JUNK_SUFFIXES = (".tmp", ".part")


def _included_files() -> list[Path]:
    """Every file that belongs in a backup, as paths under DATA_DIR."""
    out: list[Path] = []
    for root, dirs, files in os.walk(DATA_DIR):
        rel_root = Path(root).relative_to(DATA_DIR)
        at_top = not rel_root.parts
        dirs[:] = [d for d in dirs if d not in JUNK_NAMES
                   and not (at_top and (d in EXCLUDE_TOP or d.startswith(".curry-leaves-")))]
        for f in files:
            if f in JUNK_NAMES or f.endswith(JUNK_SUFFIXES):
                continue
            if at_top and f.endswith(".zip"):  # stray archives / old backups
                continue
            out.append(Path(root) / f)
    return out


@router.get("/backup/info")
def backup_info():
    """Per-category size breakdown of what a backup would contain."""
    cats: dict[str, dict[str, int]] = {}
    total = 0
    for p in _included_files():
        rel = p.relative_to(DATA_DIR)
        cat = rel.parts[0] if len(rel.parts) > 1 else "(top-level files)"
        try:
            size = p.stat().st_size
        except OSError:
            continue
        c = cats.setdefault(cat, {"bytes": 0, "files": 0})
        c["bytes"] += size
        c["files"] += 1
        total += size
    return {
        "dataDir": str(DATA_DIR),
        "totalBytes": total,
        "categories": [{"name": k, **v} for k, v in sorted(cats.items(), key=lambda kv: -kv[1]["bytes"])],
        "excluded": sorted(EXCLUDE_TOP),
    }


@router.get("/backup/export")
def backup_export():
    """Zip the data dir into a temp file and stream it as a download."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    tmp = tempfile.NamedTemporaryFile(prefix="cl-backup-", suffix=".zip", delete=False)
    tmp.close()
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in _included_files():
                zf.write(p, arcname=str(p.relative_to(DATA_DIR)))
    except Exception:
        os.unlink(tmp.name)
        raise
    return FileResponse(
        tmp.name,
        media_type="application/zip",
        filename=f"curry-leaves-backup-{stamp}.zip",
        background=BackgroundTask(os.unlink, tmp.name),  # delete after the response is sent
    )


# Markers a real curry-leaves backup must contain at least one of — rejects
# arbitrary zips before anything is touched.
_BACKUP_MARKERS = ("settings.json", "agents/", "knowledge/", "recordings/")


@router.post("/backup/restore")
async def backup_restore(file: UploadFile):
    parent = DATA_DIR.parent
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    staging = parent / f".curry-leaves-restore-{stamp}"
    safety = parent / f".curry-leaves-pre-restore-{stamp}"

    # Stream the upload to a temp zip (backups can be tens of MB — don't buffer in RAM).
    tmp = tempfile.NamedTemporaryFile(prefix="cl-restore-", suffix=".zip", delete=False)
    try:
        while chunk := await file.read(1 << 20):
            tmp.write(chunk)
        tmp.close()

        with zipfile.ZipFile(tmp.name) as zf:
            names = zf.namelist()
            if not any(n == m or n.startswith(m) for n in names for m in _BACKUP_MARKERS):
                return Response(content="Not a Curry Leaves backup (no recognizable contents).", status_code=422)
            # Zip-slip guard: every entry must land inside the staging dir.
            staging.mkdir(parents=True, exist_ok=False)
            for n in names:
                dest = staging / n
                if not str(dest.resolve()).startswith(str(staging.resolve())):
                    shutil.rmtree(staging, ignore_errors=True)
                    return Response(content=f"Backup contains an unsafe path: {n!r}", status_code=422)
            zf.extractall(staging)
    except zipfile.BadZipFile:
        shutil.rmtree(staging, ignore_errors=True)
        return Response(content="That file isn't a valid zip archive.", status_code=422)
    finally:
        os.unlink(tmp.name)

    # Swap: current dir → safety copy, staging → live. Same parent dir = same
    # filesystem, so both renames are atomic.
    os.rename(DATA_DIR, safety)
    os.rename(staging, DATA_DIR)

    # Carry over what backups deliberately exclude, so a restore never costs a
    # model re-download or drops queued work.
    for name in EXCLUDE_TOP:
        src, dst = safety / name, DATA_DIR / name
        if src.exists() and not dst.exists():
            shutil.move(str(src), str(dst))

    return {
        "ok": True,
        "safetyCopy": str(safety),
        "note": "Restored. The previous data is kept at the safety copy path — "
                "restart the backend so every feature reads the restored files.",
    }
