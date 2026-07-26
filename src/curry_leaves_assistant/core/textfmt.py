"""Shared text/display formatting for SSE payloads, traces and agent logs."""
from __future__ import annotations

import json


def sse(obj) -> str:
    """Encode an object as one Server-Sent-Events `data:` frame."""
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def args_text(args, indent: int | None = 2) -> str:
    """Render tool-call arguments for display (compact when indent=None)."""
    if args is None:
        return ""
    if isinstance(args, (dict, list)):
        try:
            return json.dumps(args, indent=indent, ensure_ascii=False, default=str)
        except Exception:
            return str(args)
    return str(args)


def result_text(res) -> str:
    """Extract display text from a smart-loop ToolResultMessage (or anything)."""
    content = getattr(res, "content", None)
    if isinstance(content, list):
        parts = [getattr(b, "text", None) for b in content]
        return "\n".join(p for p in parts if p).strip()
    return "" if res is None else str(res)


def trunc(s, cap: int = 16000) -> str:
    s = s if isinstance(s, str) else str(s)
    return s if len(s) <= cap else s[:cap] + "…(truncated)"
