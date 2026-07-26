"""The Gardener — now a thin shim over ``cl_memory``'s gardener.

The mechanical maintenance passes (edge integrity + retarget suggestions, GRAPH.md, compaction
and repair worklists, sweeps, report, single-run guard, worklist-change fingerprint) all live
in the ``cl_memory`` framework now, driven by the one configured bundle in ``domain.knowledge``.
This module preserves the app's entry point: ``run()``.

The bundle's gardener emits ``maintenance.progress`` / ``maintenance.completed``, which the app's
``on_event`` hook (in ``domain.knowledge``) re-namespaces to ``knowledge.maintenance.*`` — so the
UI stream and the Knowledge Maintainer trigger fire exactly as before.
"""
from __future__ import annotations

from typing import Any

from curry_leaves_assistant.domain import knowledge

# Kept for any legacy reference; the framework owns these values internally now.
REPORT_PATH = "notes/gardener-report.md"


def run() -> dict[str, Any]:
    """Run the Gardener (mechanical passes only; no LLM). Delegates to the framework's
    single-run-guarded gardener on the app's configured bundle."""
    return knowledge.bundle.garden()
