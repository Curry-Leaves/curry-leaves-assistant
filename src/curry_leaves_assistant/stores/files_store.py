"""Sandboxed read-only view of a few of the user's own folders, for chat `@file` references.

This is the ONLY place in the app that reaches outside `~/.curry-leaves/` (core/paths.py), so
the containment check here is load-bearing rather than incidental:

  * Roots are a fixed allowlist — Desktop / Documents / Downloads / the data dir. Not
    configurable, because a "let me add a root" setting is how a sandbox stops being one.
  * `resolve()` runs BEFORE the containment test, so a symlink pointing out of a root is
    caught. A string-prefix check on the un-resolved path would happily follow it.
  * Local deployments only. In Docker/web the backend is a server, not the user's laptop —
    these paths would be the *container's* filesystem, and browsing them is an information
    leak dressed up as a feature. See `enabled()`.
"""
from __future__ import annotations

import os
from pathlib import Path

from curry_leaves_assistant.core import paths

# Skipped when listing: noise the user never means to reference, and dot-dirs that mostly
# hold credentials and caches. Hidden files stay hidden — `@file` is for documents.
_SKIP = {"node_modules", "__pycache__", ".git", ".venv", "venv", ".DS_Store"}
# Read cap. Well under any model's context, and a guard against handing back a 2GB binary
# because the picker happened to land on one.
MAX_READ_BYTES = 256_000


def roots() -> list[dict]:
    """The browsable roots that actually exist on this machine."""
    home = Path.home()
    out = []
    for label, p in (("Desktop", home / "Desktop"), ("Documents", home / "Documents"),
                     ("Downloads", home / "Downloads"), ("Curry Leaves", paths.DATA_DIR)):
        try:
            if p.is_dir():
                out.append({"label": label, "path": str(p.resolve())})
        except OSError:
            continue
    return out


def enabled() -> bool:
    """Is `@file` available in this deployment?

    Only when the backend is bound to loopback — i.e. it IS the user's machine. A Docker or
    hosted deployment binds 0.0.0.0 to be reachable, and there these roots belong to the
    server. `CURRY_LEAVES_FILE_BROWSE=0` force-disables it regardless."""
    if os.environ.get("CURRY_LEAVES_FILE_BROWSE", "").strip() == "0":
        return False
    host = os.environ.get("CURRY_LEAVES_HOST", "127.0.0.1").strip()
    return host in ("127.0.0.1", "localhost", "::1")


def _contained(p: Path) -> Path | None:
    """Resolve `p` and return it only if it lands inside a root. Resolution happens first so
    symlinks and `..` segments are evaluated before the test, not after."""
    try:
        rp = p.resolve()
    except OSError:
        return None
    for r in roots():
        root = Path(r["path"])
        if rp == root or root in rp.parents:
            return rp
    return None


def browse(path: str | None) -> dict:
    """List one directory. An empty `path` returns the roots themselves, so the picker opens
    on "where do you want to look?" rather than an arbitrary folder.

    Raises PermissionError for anything outside the sandbox, FileNotFoundError for a missing
    or non-directory target — the router maps those to 403/404."""
    if not enabled():
        raise PermissionError("file browsing is disabled in this deployment")
    want = (path or "").strip()
    if not want:
        return {"path": "", "parent": None,
                "entries": [{"name": r["label"], "path": r["path"], "isDir": True, "size": None}
                            for r in roots()]}
    target = _contained(Path(want))
    if target is None:
        raise PermissionError("path is outside the browsable folders")
    if not target.is_dir():
        raise FileNotFoundError("not a directory")

    entries: list[dict] = []
    for child in target.iterdir():
        if child.name in _SKIP or child.name.startswith("."):
            continue
        try:
            # Anything resolving outside the sandbox is omitted rather than shown-and-refused:
            # a symlink out is blocked on access anyway, so listing it only offers the user a
            # row that 403s when clicked.
            if _contained(child) is None:
                continue
            is_dir = child.is_dir()
            entries.append({"name": child.name, "path": str(child),
                            "isDir": is_dir, "size": None if is_dir else child.stat().st_size})
        except OSError:
            continue  # unreadable entry — skip rather than fail the whole listing
    # Folders first, then files; each alphabetical, case-insensitive.
    entries.sort(key=lambda e: (not e["isDir"], str(e["name"]).lower()))
    # `parent` stays None at a root so the picker can't walk up out of the sandbox.
    parent = str(target.parent) if _contained(target.parent) and target != Path(target.anchor) else None
    return {"path": str(target), "parent": parent, "entries": entries}


def read_text(path: str) -> tuple[str, str]:
    """Return (display name, text) for a sandboxed file. Text only and size-capped: the
    picker can point at anything, including a binary the model can't use."""
    if not enabled():
        raise PermissionError("file reading is disabled in this deployment")
    p = _contained(Path(path))
    if p is None:
        raise PermissionError("path is outside the readable folders")
    if not p.is_file():
        raise FileNotFoundError("not a file")
    if p.stat().st_size > MAX_READ_BYTES:
        raise ValueError(f"file is larger than {MAX_READ_BYTES // 1000}kB")
    try:
        return p.name, p.read_text(errors="strict")
    except UnicodeDecodeError:
        raise ValueError("file is not text") from None


def exists(path: str) -> bool:
    """Cheap liveness check for a staged @file handle (chat_runs._ref_exists)."""
    p = _contained(Path(path))
    return p is not None and p.is_file()
