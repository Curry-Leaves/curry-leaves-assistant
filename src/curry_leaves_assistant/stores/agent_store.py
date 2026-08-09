"""Agents as files: a portable ``<id>.md`` (frontmatter + instructions) plus an
operational ``<id>.meta.json`` (triggers, schedule, run history).

The markdown is what you'd share; the meta is what the runtime owns. An in-memory
agent dict is just the two merged.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from curry_leaves_assistant.core.paths import AGENTS_DIR, agent_md_path, agent_meta_path, agent_runs_dir
from curry_leaves_assistant.core.store import read_json, write_json, now_iso


def recent_runs(agent_id: str, limit: int = 25) -> list[dict]:
    """The agent's most-recent run records (newest first), read from runs/<agentId>/.
    The pool writes one <jobId>.json per run; callers that just want to display run
    history use this instead of globbing the runs dir themselves."""
    d = agent_runs_dir(agent_id)
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        r = read_json(f, None)
        if r:
            out.append(r)
    return out


def _normalize_schedule(spec: dict | None) -> dict:
    """Return a ScheduleSpec the scheduler understands (has a `kind`). Older/UI-authored metas
    stored the human {frequency, time, dayOfWeek} vocabulary WITHOUT a kind — the scheduler's
    _AgentScheduleSource skips anything whose kind is 'none'/absent, so those agents silently
    never fired (this is why the Skill Learner never ran once). Translate that legacy shape
    into a cron spec at read time via the one canonical translator, so a drifted meta heals
    without a migration pass. A spec that already has a kind is returned untouched."""
    if not spec:
        return {"kind": "none"}
    if spec.get("kind"):
        return spec
    freq = spec.get("frequency")
    if freq:
        from curry_leaves_assistant.core.schedule_spec import cron_from_frequency
        cron = cron_from_frequency(freq, spec.get("time") or "09:00", spec.get("dayOfWeek"))
        if cron:
            return cron
    return {"kind": "none"}


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a ``---``-delimited YAML frontmatter block from the markdown body.

    A malformed frontmatter block (e.g. an unquoted colon-space in a value) yields an
    empty dict rather than raising: this parser sits on the read path of every
    frontmatter store, and one bad learned-skill file must never crash the caller
    (notably _scoped_skills, which runs on every agent build). The body is still
    returned so the file is usable; the writer is where correct YAML is enforced."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            raw = text[3:end].strip()
            body = text[end + 4:].lstrip("\n")
            try:
                meta = yaml.safe_load(raw) or {}
            except yaml.YAMLError:
                meta = {}
            if not isinstance(meta, dict):  # a scalar/list front block isn't valid frontmatter
                meta = {}
            return meta, body
    return {}, text


def render_frontmatter(fm: dict, body: str) -> str:
    """Serialize a frontmatter dict + markdown body into the ``---``-delimited file format
    that parse_frontmatter reads back. The single writer for every frontmatter-file store
    (agents, templates, skills, profile/agent memory) — each builds its own ``fm`` dict, this
    handles the YAML dump + framing so that shape lives in one place."""
    front = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{front}\n---\n\n{(body or '').strip()}\n"


def _dump_md(agent: dict) -> str:
    fm = {}
    for k in ("name", "description", "provider", "model", "tools", "permissions", "lane", "autonomy"):
        if agent.get(k):
            fm[k] = agent[k]
    ms = agent.get("max_steps", agent.get("maxSteps"))  # accept either casing on round-trip
    if ms is not None:
        fm["max_steps"] = ms
    deferred = agent.get("deferred_tools", agent.get("deferredTools"))  # accept either casing on round-trip
    if deferred:
        fm["deferred_tools"] = deferred
    return render_frontmatter(fm, agent.get("instructions", ""))


def read_agent(agent_id: str) -> dict | None:
    md_path = agent_md_path(agent_id)
    if not md_path.exists():
        return None
    fm, body = parse_frontmatter(md_path.read_text())
    meta = read_json(agent_meta_path(agent_id), {})
    return {
        "id": agent_id,
        "name": fm.get("name", agent_id),
        "description": fm.get("description", ""),
        "provider": fm.get("provider"),  # None -> use the app-wide default/active provider
        "model": fm.get("model"),
        "tools": fm.get("tools", []),
        # Opt-in engine feature (agent_engine._build_agent): tools listed here are
        # registered but NOT advertised until the model calls search_tools and finds
        # them by keyword — keeps a big roster's schema tokens off every turn's prompt
        # for tools that aren't needed most turns. Omit entirely for always-on (today's
        # default for every existing agent — no agent is auto-split).
        "deferredTools": fm.get("deferred_tools", []),
        "permissions": fm.get("permissions", {}),
        "maxSteps": fm.get("max_steps", 20),
        # Work Kernel policy. `lane`: the scheduler channel (e.g. "kb" serializes KB writers,
        # "general" runs parallel). `autonomy`: "auto" (approve granted tools headlessly) or
        # "ask" (a background run suspends + notifies a human on an approval/ask).
        "lane": fm.get("lane"),
        "autonomy": fm.get("autonomy"),
        "instructions": body,
        # operational (meta)
        "enabled": meta.get("enabled", True),
        "internal": meta.get("internal", False),  # UI-only helper agents (hidden from chat/dashboard)
        # Runs on every recording regardless of its agentIds binding — for infrastructure
        # agents (e.g. the Knowledge Keeper) that shouldn't be a per-recording choice.
        "always": meta.get("always", False),
        "surfaces": meta.get("surfaces", []),
        "triggers": meta.get("triggers", []),
        "subagents": meta.get("subagents", []),
        "skills": meta.get("skills", []),
        "schedule": _normalize_schedule(meta.get("schedule")),
        "lastRunAt": meta.get("lastRunAt"),
        "lastRunStatus": meta.get("lastRunStatus"),
        "createdAt": meta.get("createdAt"),
        "updatedAt": meta.get("updatedAt"),
    }


def list_agents() -> list[dict]:
    ids = sorted(p.stem for p in AGENTS_DIR.glob("*.md"))
    return [a for a in (read_agent(i) for i in ids) if a]


def write_agent(agent: dict) -> dict | None:
    agent_id = agent["id"]
    agent_md_path(agent_id).parent.mkdir(parents=True, exist_ok=True)
    agent_md_path(agent_id).write_text(_dump_md(agent))
    existing = read_json(agent_meta_path(agent_id), {})
    meta = {
        "id": agent_id,
        "enabled": agent.get("enabled", True),
        "internal": agent.get("internal", existing.get("internal", False)),
        "always": agent.get("always", existing.get("always", False)),
        "surfaces": agent.get("surfaces", []),
        "triggers": agent.get("triggers", []),
        "subagents": agent.get("subagents", []),
        "skills": agent.get("skills", []),
        "schedule": agent.get("schedule", {"kind": "none"}),
        "lastRunAt": agent.get("lastRunAt", existing.get("lastRunAt")),
        "lastRunStatus": agent.get("lastRunStatus", existing.get("lastRunStatus")),
        "createdAt": existing.get("createdAt") or now_iso(),
        "updatedAt": now_iso(),
    }
    write_json(agent_meta_path(agent_id), meta)
    return read_agent(agent_id)


def update_meta(agent_id: str, **patch) -> None:
    meta = read_json(agent_meta_path(agent_id), {})
    meta.update(patch)
    meta["updatedAt"] = now_iso()
    write_json(agent_meta_path(agent_id), meta)


def delete_agent(agent_id: str) -> bool:
    md = agent_md_path(agent_id)
    existed = md.exists()
    md.unlink(missing_ok=True)
    agent_meta_path(agent_id).unlink(missing_ok=True)
    return existed


def agents_for_trigger(event_type: str) -> list[dict]:
    return [a for a in list_agents()
            if a["enabled"] and event_type in (a.get("triggers") or [])]


# ─── Default agents (seeded on first run from the bundled seeds/agents/*.md) ───
# Each seed file uses the same frontmatter+body format as a live agent file, with the
# meta-side fields (surfaces/triggers/schedule/subagents/internal/always) inline;
# write_agent() splits them out. The filename stem is the agent id.
SEED_AGENTS_DIR = Path(__file__).resolve().parents[1] / "seeds" / "agents"


def seed_default_agents() -> None:
    """Seed any bundled default agent that isn't on disk yet. seeds/agents/ is the
    source of truth for fresh installs; existing agents are never touched (no
    migrations) so user edits are preserved. To adopt a changed default, delete that
    agent and let it re-seed."""
    existing = {p.stem for p in AGENTS_DIR.glob("*.md")}
    added = 0
    for src in sorted(SEED_AGENTS_DIR.glob("*.md")):
        if src.stem not in existing:
            fm, body = parse_frontmatter(src.read_text(encoding="utf-8"))
            write_agent({"id": src.stem, **fm, "instructions": body})
            added += 1
    if added:
        print(f"[agents] seeded {added} default agent(s)", flush=True)
