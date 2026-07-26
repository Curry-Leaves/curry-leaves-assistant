"""Episodic memory — TWO cleanly separated things that used to be one.

1. **Run stats** (`.index/stats.db`, this module's SQLite mirror). The mechanical, LLM-free
   summary of every run: task-shape, outcome, step count, tool errors. `record()` writes one
   row per run from `workers._finalize`. This is pure telemetry for the learning loop —
   `baseline_steps` (a median), `count_task_shape`, the failure/inefficiency detectors — NOT
   memory. It writes NO note: a "ran task, 1 step, done" per run buried memory in noise, and the
   full run already lives in `runs/` + `traces/`.

2. **Curated events** (`type: episodic` notes in the one bundle). Notable things worth referring
   back to, distilled from conversations by the nightly Memory Keeper via `remember_event()`.
   These are prose a human or a future agent finds useful, searchable/linkable/consolidatable
   like any note. `recall_events()` retrieves them by meaning.

The split is the whole point: numbers stay exact and cheap in SQL; memory stays curated and
sparse. `recall()`/`recent()` read the stats mirror (the learning loop); `recall_events()` reads
the notes (agents asking "what happened / what did I learn").

`stats.db` is the SOURCE OF TRUTH for run stats — including the `reviewed` bit, which isn't
derivable from anything else. Don't treat it as throwaway: deleting it drops every run's stats,
not just a cache.
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
import threading
from typing import Any

from curry_leaves_assistant.core import embeddings, memory_ref
from curry_leaves_assistant.core.paths import EPISODIC_DIR, MEMORY_DIR, safe_agent_seg
from curry_leaves_assistant.core.store import now_iso

_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG.sub("-", (text or "event").lower()).strip("-")[:48] or "event"

_DB_PATH = MEMORY_DIR / ".index" / "stats.db"
_local = threading.local()
_write_lock = threading.RLock()


# ─── derive an episode from a run record + its trace (mechanical, no LLM) ────────
def summarize(run: dict, spans: list[dict]) -> dict:
    """The mechanical summary of one run: what shape of task, how it went, how much work."""
    trig = run.get("trigger") or {}
    agent_id = run.get("agentId") or trig.get("agentId") or "unknown"
    tool_calls: dict[str, int] = {}
    tool_errors = 0
    steps = 0
    for s in spans:
        kind = s.get("kind")
        if kind == "llm_turn":
            steps += 1
        elif kind == "tool_call":
            name = (s.get("attributes") or {}).get("tool") or s.get("name") or "tool"
            tool_calls[name] = tool_calls.get(name, 0) + 1
            if (s.get("attributes") or {}).get("error"):
                tool_errors += 1
    steps = steps or run.get("steps") or 0
    outcome = "failed" if (run.get("error") or run.get("status") == "failed") else "done"
    shape = _task_shape(trig)
    return {
        "agentId": agent_id, "jobId": run.get("id") or "unknown",
        "traceId": run.get("traceId"), "taskShape": shape, "outcome": outcome,
        "steps": steps, "toolCalls": tool_calls, "toolCallTotal": sum(tool_calls.values()),
        "toolErrors": tool_errors, "maxToolRepeat": max(tool_calls.values(), default=0),
        "error": (run.get("error") or "")[:400] or None,
        "title": (trig.get("payload") or {}).get("title") or trig.get("type") or shape,
        "finishedAt": run.get("finishedAt") or now_iso(), "reviewed": False,
    }


_SKILL_RE = re.compile(r"skill://([A-Za-z0-9_\-]+)")


def loaded_skills(spans: list[dict]) -> list[str]:
    """Which skills a run actually LOADED (an honest signal — a teaser in a prompt isn't a use)."""
    found: set[str] = set()
    for s in spans:
        if s.get("kind") != "tool_call":
            continue
        found.update(_SKILL_RE.findall(json.dumps(s.get("attributes") or {})))
    return sorted(found)


def _task_shape(trig: dict) -> str:
    t = trig.get("type") or "adhoc"
    payload = trig.get("payload") or {}
    refine = payload.get("templateId") or payload.get("section") or payload.get("kind")
    return f"{t}:{refine}" if refine else t


def _bundle() -> Any:
    return memory_ref.get()


def record(episode: dict) -> dict:
    """Persist one run's STATS ROW — steps/outcome/shape for the learning loop. Idempotent on
    (agentId, jobId).

    Deliberately writes NO memory note. A mechanical "ran task, 1 step, done" is telemetry, not
    something worth remembering, and one per run buried memory in noise. The numbers the loop
    reads (baseline_steps, count_task_shape, the failure/inefficiency signals) all come from this
    row; the full transcript lives in runs/ + traces/. What DOES become an episodic memory note
    is the curated event the Memory Keeper distils from a conversation — see `remember_event`."""
    with _write_lock:
        _stat_upsert(episode)
    return episode


# Cosine floor for "this event is the SAME event, just worded differently". Measured on
# title+body pairs: genuine rephrases land 0.80–0.82 ("Deployed v2 to prod" vs "v2 went live in
# prod"), while distinct events — even same-SHAPED decisions ("ClickHouse over BigQuery" vs
# "Postgres over MySQL") — sit at 0.22. 0.75 splits them with wide margin in both directions.
_EVENT_DUP_SIM = 0.75


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # embeddings are L2-normalized -> dot == cosine


def _duplicate_event(agent_id: str, title: str, body: str) -> dict | None:
    """An existing curated event that is really the SAME as (title, body), by MEANING — or None.

    Closes the gap the old title-slug key left open: a rephrased event ("Chose X" vs "Decided on
    X") got a different slug and became a twin. We ask the bundle's vector recall for the few
    closest episodic notes (it already runs the search — no full scan here), then confirm the best
    one clears the floor. With no embedder we fall back to the exact-slug identity the id encodes,
    so dedup never silently vanishes."""
    b = _bundle()
    slug_id = f"ev_{agent_id}_{_slug(title)}"

    if not embeddings.vector_ready():
        note = b.read(f"{EPISODIC_DIR}/{safe_agent_seg(agent_id)}/{slug_id}.md")
        return note or None

    # Vector tier: the bundle finds the nearest episodic notes; we score only those few.
    try:
        hits = b.recall(f"{title} | {body}", type="episodic", limit=5)
    except Exception:
        return None
    query = f"{title} | {body}"
    best_sim, best = 0.0, None
    try:
        qvec = embeddings.embed([query])[0]
    except Exception:
        return None
    for h in hits:
        note = b.read(h["path"])
        fm = (note or {}).get("frontmatter") or {}
        if not note or fm.get("agent") != agent_id:   # dedup within this agent's own events
            continue
        text = f"{fm.get('title') or ''} | {(note.get('body') or '')}"
        try:
            sim = _cosine(qvec, embeddings.embed([text])[0])
        except Exception:
            continue
        if sim > best_sim:
            best_sim, best = sim, note
    return best if best_sim >= _EVENT_DUP_SIM else None


def remember_event(agent_id: str, *, title: str, body: str, occurred: str | None = None,
                   tags: list[str] | None = None, source: dict | None = None,
                   about: str | None = None) -> dict:
    """Write a CURATED episodic memory — a notable thing that happened, worth referring back to.

    This is the only path that creates a `type: episodic` note now. The Memory Keeper calls it
    after reading a conversation; the note is prose a human (or a future agent) would find useful,
    not a run summary. It joins the one bundle, so it's searchable, linkable, and clusters into
    consolidated lessons.

    Filed under what it's ABOUT, like every other memory — `about='apps/cbm'` puts a release event
    beside CBM's other notes and links it there. Who *recorded* it is provenance (`agent:` +
    `source:`), not location: filing by author scattered one project's history across whichever
    assistant happened to notice each part. Without `about` it falls back to the agent's own
    folder, which is right only for events genuinely about that assistant.

    Deduped by MEANING: if this event is really one already recorded (just worded differently),
    the existing note is UPDATED in place rather than a near-duplicate being added — the same
    upsert-in-place discipline the profile uses, but semantic instead of subject-string."""
    from cl_memory.util import dump_note
    from curry_leaves_assistant.stores import memory_router as router
    b = _bundle()
    with _write_lock:
        dup = _duplicate_event(agent_id, title, body)
        rec_id = (dup["frontmatter"] or {}).get("id") if dup else f"ev_{agent_id}_{_slug(title)}"
        area = (about or "").strip("/")
        fm = {
            "type": "episodic", "id": rec_id, "agent": agent_id,
            "title": title, "description": body[:200],
            # keep the ORIGINAL occurred on an update — the event happened once, this is a reword
            "occurred": (dup["frontmatter"].get("occurred") if dup else None)
                        or occurred or now_iso(),
            "tags": ["event", *(tags or [])],
            # Honest provenance: tag it with whoever recorded it. Callers that know more (e.g. a
            # future wiring passing which chat it came from) can override via `source`.
            "source": source or {"type": "agent", "agent": agent_id},
        }
        if area:
            fm["about"] = area
        text = body
        if area:
            rel = f"{area}/events/{rec_id}.md"
            anchor = router.anchor_for(area, exclude=rel)
            if not anchor:
                leaf = area.split("/")[-1]
                kind = {"people": "person", "apps": "app", "agents": "agent",
                        "topics": "topic"}.get(area.split("/")[0], "note")
                parent = router.ensure_parent(area, title=leaf.replace("-", " ").title(), type=kind)
                anchor = {"path": parent, "title": leaf.replace("-", " ").title()}
            text = f"{body}\n\nAbout: [{anchor['title']}](/{anchor['path']})\n"
        else:
            rel = f"{EPISODIC_DIR}/{safe_agent_seg(agent_id)}/{rec_id}.md"
        # An event that moved parents shouldn't leave a stale copy behind.
        if dup and dup.get("path") and dup["path"] != rel:
            b.delete(dup["path"], "refiled under what it's about")
        b.write_raw(rel, dump_note(fm, text))
    return {"id": rec_id, "title": title, "path": rel, "updated": dup is not None}


def mark_reviewed(agent_id: str, job_id: str) -> None:
    """Flip a run's reviewed flag in the stats mirror — the Skill Learner calls this after
    reflecting, so the same signal isn't re-processed."""
    with _write_lock:
        conn = _connect()
        conn.execute("UPDATE episodes SET reviewed=1 WHERE agentId=? AND jobId=?",
                     (agent_id, job_id))
        conn.commit()


# ─── query ──────────────────────────────────────────────────────────────────────
def recent(agent_id: str | None = None, *, task_shape: str | None = None,
           limit: int = 50, unreviewed_only: bool = False) -> list[dict]:
    where, args = [], []
    if agent_id:
        where.append("agentId=?"); args.append(agent_id)
    if task_shape:
        where.append("taskShape=?"); args.append(task_shape)
    if unreviewed_only:
        where.append("reviewed=0")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = _connect().execute(
        f"SELECT body FROM episodes {clause} ORDER BY finishedAt DESC LIMIT ?",
        (*args, limit)).fetchall()
    return [json.loads(r["body"]) for r in rows]


def recall(agent_id: str, *, query: str | None = None, task_shape: str | None = None,
           since: str | None = None, until: str | None = None, outcome: str | None = None,
           limit: int = 10) -> list[dict]:
    """This agent's past RUNS (stats), newest first — for the learning loop and the run-history
    view. Exact filters (shape / outcome / date window) are ANDed; `query` is a substring match
    on the run title/shape. For agent-facing "what did I learn / what happened" use
    ``recall_events`` — those are the curated notes, matched by meaning."""
    where, args = ["agentId=?"], [agent_id]
    if task_shape:
        where.append("taskShape=?"); args.append(task_shape)
    if outcome:
        where.append("outcome=?"); args.append(outcome)
    if since:
        where.append("finishedAt>=?"); args.append(since)
    if until:
        where.append("finishedAt<=?"); args.append(until)
    rows = _connect().execute(
        f"SELECT body FROM episodes WHERE {' AND '.join(where)} "
        "ORDER BY finishedAt DESC LIMIT ?",
        (*args, max(1, min(limit, 100)))).fetchall()
    eps = [json.loads(r["body"]) for r in rows]
    if not query:
        return eps
    q = query.lower()
    return [e for e in eps if q in (e.get("title") or "").lower()
            or q in (e.get("taskShape") or "").lower()][:limit]


def recall_events(query: str, *, limit: int = 8) -> list[dict]:
    """The curated episodic MEMORIES about `query`, matched by MEANING — the notable things the
    Memory Keeper distilled from conversations. Distinct from `recall` (raw run stats). Not
    agent-scoped by design: an event one assistant filed can be useful to another, and each note
    already carries its own `agent` for provenance."""
    if not (query or "").strip():
        return []
    b = _bundle()
    try:
        hits = b.recall(query, type="episodic", limit=limit)
    except Exception:
        return []
    out: list[dict] = []
    for h in hits:
        note = b.read(h["path"])
        if not note:
            continue
        fm = note.get("frontmatter") or {}
        out.append({"id": fm.get("id"), "title": fm.get("title"),
                    "occurred": fm.get("occurred"), "agent": fm.get("agent"),
                    "body": (note.get("body") or "").strip()})
    return out


def baseline_steps(agent_id: str, task_shape: str) -> float | None:
    """Median step count for this agent+task-shape — the 'usual' an inefficiency detector
    compares against. None if too few samples to be meaningful."""
    rows = _connect().execute(
        "SELECT steps FROM episodes WHERE agentId=? AND taskShape=? AND outcome='done'",
        (agent_id, task_shape)).fetchall()
    vals = [r["steps"] for r in rows if r["steps"]]
    return statistics.median(vals) if len(vals) >= 4 else None


def count_task_shape(agent_id: str, task_shape: str, *, since: str | None = None) -> int:
    sql = "SELECT COUNT(*) c FROM episodes WHERE agentId=? AND taskShape=?"
    args: list[Any] = [agent_id, task_shape]
    if since:
        sql += " AND finishedAt>=?"
        args.append(since)
    return int(_connect().execute(sql, args).fetchone()["c"])


# ─── the derived stats mirror ───────────────────────────────────────────────────
def _connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS episodes (
            agentId    TEXT NOT NULL,
            jobId      TEXT NOT NULL,
            taskShape  TEXT,
            outcome    TEXT,
            steps      INTEGER,
            reviewed   INTEGER DEFAULT 0,
            finishedAt TEXT,
            body       TEXT NOT NULL,
            PRIMARY KEY (agentId, jobId)
        );
        CREATE INDEX IF NOT EXISTS ep_shape ON episodes(agentId, taskShape);
        CREATE INDEX IF NOT EXISTS ep_time ON episodes(finishedAt);
        """
    )
    conn.commit()
    _local.conn = conn
    # Run stats live ONLY here — this DB is their source of truth (see module docstring).
    return conn


def _stat_upsert(ep: dict) -> None:
    conn = _connect()
    conn.execute("DELETE FROM episodes WHERE agentId=? AND jobId=?",
                 (ep["agentId"], ep.get("jobId")))
    conn.execute(
        "INSERT INTO episodes(agentId,jobId,taskShape,outcome,steps,reviewed,finishedAt,body) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (ep["agentId"], ep.get("jobId"), ep.get("taskShape"), ep.get("outcome"),
         ep.get("steps"), 1 if ep.get("reviewed") else 0, ep.get("finishedAt"),
         json.dumps(ep, ensure_ascii=False)),
    )
    conn.commit()


def close_thread_conn() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _local.conn = None
