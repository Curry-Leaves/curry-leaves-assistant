"""Durable token-usage ledger — every model call, appended forever (never pruned).

Separate from the trace store (which caps/prunes), so usage totals can't be lost. One file
per month under ~/.curry-leaves/usage/<YYYY-MM>.jsonl. Recorded at the single chokepoint every
model turn passes through: a smart-loop `MessageEnd` (see trace_host.TracingHost / UsageHost).

Cost is priced off curry_leaves.catalog's live models.dev feed (loaded once at app startup —
see app.py's lifespan), so every provider models.dev tracks is covered without a hand-maintained
price table. A model missing from the catalog (never fetched, or genuinely unlisted) prices at $0.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

from curry_leaves.catalog import compute_cost
from curry_leaves.core.messages import Usage

from curry_leaves_assistant.core.paths import USAGE_DIR

_lock = threading.Lock()


def _cost(model: str | None, ti: int, to: int, cache_read: int = 0, cache_write: int = 0) -> float:
    if not model:
        return 0.0
    usage = Usage(input=ti, output=to, cache_read=cache_read, cache_write=cache_write)
    return compute_cost(usage, model).total


def record(tokens_in: int, tokens_out: int, *, model: str | None = None,
           agent_id: str | None = None, surface: str | None = None,
           cache_read: int = 0, cache_write: int = 0) -> None:
    """Append one model call's usage. Best-effort — never raises into a run."""
    if not (tokens_in or tokens_out or cache_read or cache_write):
        return
    try:
        USAGE_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        entry = {
            "ts": now.isoformat(), "model": model, "agentId": agent_id, "surface": surface,
            "tokensIn": int(tokens_in or 0), "tokensOut": int(tokens_out or 0),
            # Cache tokens are priced separately (cheaply) but were never persisted, so historical
            # token totals understated cache-heavy sessions. Store them so counts are complete and
            # auditable. Absent on pre-existing rows → default 0 on read.
            "cacheRead": int(cache_read or 0), "cacheWrite": int(cache_write or 0),
            "costUsd": round(_cost(model, tokens_in, tokens_out, cache_read, cache_write), 6),
        }
        with _lock:
            with (USAGE_DIR / f"{now.strftime('%Y-%m')}.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[usage] record failed: {exc}", flush=True)


def _local_day(ts: str) -> str:
    """Local calendar date for a stored UTC timestamp — days must match the user's clock."""
    try:
        return datetime.fromisoformat(ts).astimezone().date().isoformat()
    except Exception:
        return ts[:10]


def _iter(since_day: str | None):
    if not USAGE_DIR.exists():
        return
    for p in sorted(USAGE_DIR.glob("*.jsonl")):
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for ln in lines:
            try:
                e = json.loads(ln)
            except Exception:
                continue
            e["day"] = _local_day(e.get("ts", ""))
            if since_day and e["day"] < since_day:
                continue
            yield e


def _blank() -> dict:
    return {"calls": 0, "tokensIn": 0, "tokensOut": 0, "cacheRead": 0, "cacheWrite": 0, "costUsd": 0.0}


def summary(days: int | None = None) -> dict:
    """Aggregate all usage (optionally last `days`): totals + breakdowns by model/agent/surface/day.

    Days are the *local* calendar date of each call (timestamps are stored UTC) — bucketing by
    UTC date shoves evening usage into tomorrow's bar for anyone west of Greenwich.

    `byDay` is zero-filled across the whole requested range (today back `days` days), not just
    days that had a recorded call — otherwise a handful of active days scattered across a 30d/90d
    window renders as a couple of bars with no indication of the empty days between them.
    """
    today_local = datetime.now().astimezone().date()
    since = (today_local - timedelta(days=days - 1)).isoformat() if days else None
    total = _blank()
    by_model: dict[str, dict] = {}
    by_agent: dict[str, dict] = {}
    by_surface: dict[str, dict] = {}
    by_day: dict[str, dict] = {}

    def bump(group: dict, key, e) -> None:
        x = group.setdefault(key or "unknown", _blank())
        x["calls"] += 1
        x["tokensIn"] += e.get("tokensIn", 0)
        x["tokensOut"] += e.get("tokensOut", 0)
        x["cacheRead"] += e.get("cacheRead", 0)
        x["cacheWrite"] += e.get("cacheWrite", 0)
        x["costUsd"] += e.get("costUsd", 0.0)

    entries = list(_iter(since))
    if days:
        for i in range(days):
            day = (today_local - timedelta(days=days - 1 - i)).isoformat()
            by_day[day] = _blank()
    elif entries:
        first_day = min(e["day"] for e in entries)
        d = datetime.strptime(first_day, "%Y-%m-%d").date()
        while d <= today_local:
            by_day[d.isoformat()] = _blank()
            d += timedelta(days=1)

    for e in entries:
        bump({"_": total}, "_", e)  # accumulate the grand total via the same helper
        bump(by_model, e.get("model"), e)
        bump(by_agent, e.get("agentId"), e)
        bump(by_surface, e.get("surface"), e)
        bump(by_day, e["day"], e)

    def rows(group: dict) -> list[dict]:
        out = [{"key": k, **v, "costUsd": round(v["costUsd"], 4)} for k, v in group.items()]
        out.sort(key=lambda r: r["tokensIn"] + r["tokensOut"], reverse=True)
        return out

    total["costUsd"] = round(total["costUsd"], 4)
    return {
        "total": total,
        "byModel": rows(by_model),
        "byAgent": rows(by_agent),
        "bySurface": rows(by_surface),
        "byDay": sorted([{"key": k, **v, "costUsd": round(v["costUsd"], 4)} for k, v in by_day.items()],
                        key=lambda r: r["key"]),
    }
