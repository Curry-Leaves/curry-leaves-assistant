"""A one-slot holder for the app's memory bundle.

The bundle is *composed* in ``domain/memory.py`` — it needs the embedder, the event bridge and the
provenance resolver, all of which are domain concerns. The ``stores/`` memory views need that same
bundle but sit BELOW domain in the layering, so they must not import it.

So the dependency points downward at both ends: domain composes the bundle and registers it here
(``set_bundle``) at import; stores read it back (``get``). Deliberately dumb — no construction, no
config, just a slot, so this stays a legal ``core`` module.

Import order is handled by the one importer that matters: ``stores/__init__`` and the store
modules never touch the bundle at import time (only inside functions), and by the time any of them
runs, ``domain.memory`` has been imported — app.py imports it at boot, and every store's own
docstring points at it. ``get()`` raises a clear wiring error rather than guessing if that ever
stops being true.
"""
from __future__ import annotations

from typing import Any

_bundle: Any = None


def set_bundle(bundle: Any) -> None:
    """Register the composed bundle (domain/memory.py calls this at import)."""
    global _bundle
    _bundle = bundle


def get() -> Any:
    """The app's memory bundle."""
    if _bundle is None:  # pragma: no cover — a wiring bug, not a runtime state
        raise RuntimeError(
            "memory bundle not composed — import curry_leaves_assistant.domain.memory first "
            "(app.py does at boot)")
    return _bundle


__all__ = ["set_bundle", "get"]
