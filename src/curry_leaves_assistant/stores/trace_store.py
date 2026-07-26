"""Durable span store for traces — one ``<traceId>.jsonl`` per trace under ~/.curry-leaves/traces/.

A span is appended once when it opens (status=running) and again when it closes (final
attributes + endedAt); readers merge by spanId (last wins), so an in-flight trace is still
listable. Files over the retention cap are pruned oldest-first. Writes are serialized by a
lock since parallel branches of one trace can append concurrently.
"""
from __future__ import annotations

import json
import os
import threading

from curry_leaves_assistant.core.paths import TRACES_DIR

KEEP = int(os.environ.get("CURRY_LEAVES_TRACE_KEEP", "500"))  # max traces retained on disk

_lock = threading.Lock()


def _file(trace_id: str):
    return TRACES_DIR / f"{trace_id}.jsonl"


def write_span(span: dict) -> None:
    """Append one span record (an open or a close). Best-effort — never raises into a run."""
    try:
        TRACES_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(span, ensure_ascii=False, default=str)
        with _lock:
            with _file(span["traceId"]).open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as exc:  # tracing must never break the thing it traces
        print(f"[trace] write failed: {exc}", flush=True)


def ensure_root(trace_id: str, span_id: str, name: str, kind: str = "session",
                attributes: dict | None = None) -> None:
    """Write a synthetic root span once (idempotent) — e.g. a chat session that groups all of
    its turns. Closed instantly so the trace's `running` status reflects only its live turns."""
    if any(s.get("spanId") == span_id for s in read_spans(trace_id)):
        return
    now = _now()
    write_span({
        "traceId": trace_id, "spanId": span_id, "parentSpanId": None, "kind": kind, "name": name,
        "status": "ok", "startedAt": now, "endedAt": now, "durationMs": 0, "attributes": attributes or {},
    })


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def read_spans(trace_id: str) -> list[dict]:
    """All spans for a trace, in first-seen order, merging each span's open+close records."""
    p = _file(trace_id)
    if not p.exists():
        return []
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            s = json.loads(ln)
        except Exception:
            continue
        sid = s.get("spanId")
        if sid not in by_id:
            order.append(sid)
        by_id[sid] = {**by_id.get(sid, {}), **s}
    return [by_id[sid] for sid in order]


def get_trace(trace_id: str) -> list[dict]:
    return read_spans(trace_id)


def _summary(trace_id: str, spans: list[dict]) -> dict:
    root = next((s for s in spans if not s.get("parentSpanId")), spans[0])
    running = any(not s.get("endedAt") for s in spans)
    errored = any(s.get("status") == "error" for s in spans)
    attrs = root.get("attributes") or {}
    turns = [s for s in spans if s.get("kind") == "llm_turn"]
    ins = [(s.get("attributes") or {}).get("tokensIn", 0) or 0 for s in turns]
    # tok_in is CUMULATIVE across calls (prompt tokens billed on every round-trip) — a cost/
    # throughput number, not context size. On a tool-use trace each call re-sends the growing
    # prompt, so this is much larger than any single call's context. `peak_in` is the largest
    # single call = the real context high-water mark, so the UI can show both without
    # conflating "tokens processed" with "how full the window got".
    tok_in = sum(ins)
    peak_in = max(ins) if ins else 0
    tok_out = sum((s.get("attributes") or {}).get("tokensOut", 0) or 0 for s in turns)
    # Whole wall-clock span of the trace (root duration misses deferred/background work).
    starts = [s.get("startedAt") for s in spans if s.get("startedAt")]
    ends = [s.get("endedAt") for s in spans if s.get("endedAt")]
    wall = None
    if starts and ends:
        from datetime import datetime
        try:
            wall = int((datetime.fromisoformat(max(ends)) - datetime.fromisoformat(min(starts))).total_seconds() * 1000)
        except Exception:
            wall = None
    return {
        "traceId": trace_id,
        "rootName": root.get("name"),
        "rootKind": root.get("kind"),
        "rootType": attrs.get("type") or root.get("name"),
        "agentId": attrs.get("agentId"),
        "startedAt": root.get("startedAt"),
        "durationMs": wall if wall is not None else root.get("durationMs"),
        "tokensIn": tok_in,
        "tokensOut": tok_out,
        "peakContext": peak_in,
        "status": "running" if running else ("error" if errored else (root.get("status") or "ok")),
        "spanCount": len(spans),
    }


def list_traces(limit: int = 50) -> list[dict]:
    out = []
    if TRACES_DIR.exists():
        for p in TRACES_DIR.glob("*.jsonl"):
            spans = read_spans(p.stem)
            if spans:
                out.append(_summary(p.stem, spans))
    out.sort(key=lambda t: t.get("startedAt") or "", reverse=True)
    return out[:limit]


def prune(keep: int = KEEP) -> None:
    """Keep the most-recently-written `keep` traces; delete the rest."""
    files = [p for p in TRACES_DIR.glob("*.jsonl")] if TRACES_DIR.exists() else []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[keep:]:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def delete(trace_id: str) -> bool:
    p = _file(trace_id)
    existed = p.exists()
    p.unlink(missing_ok=True)
    return existed


def clear() -> int:
    n = 0
    if TRACES_DIR.exists():
        for p in TRACES_DIR.glob("*.jsonl"):
            p.unlink(missing_ok=True)
            n += 1
    return n
