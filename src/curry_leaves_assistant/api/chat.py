"""Chat HTTP surface: file-backed sessions, attachments, and compaction.

The live chat *stream* (start a turn, deltas, approvals, steer/stop) runs over the shared
WebSocket now — see agents/chat_runs.py (run lifecycle) and api/ws.py (the chat.* methods).
This module keeps only the request/response endpoints: session CRUD, fork, file attach,
and compaction.
"""
from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from curry_leaves_assistant.agents import agent_engine
from curry_leaves_assistant.stores import agent_store, chat_sessions

router = APIRouter(tags=["chat"])


# ─── Chat sessions (file-backed via curry-leaves) ────────────────────────────
@router.get("/sessions")
def list_sessions():
    return chat_sessions.list_sessions()


@router.get("/sessions/live-runs")
def live_runs():
    """{sessionId: runId} for chat runs streaming right now.

    A run outlives the client that started it (nothing cancels it on disconnect), so the UI
    can't infer "still working" from its own state alone: switching sessions, reloading the
    page, or opening a second tab all leave a live run with no local handle. This is the
    reverse lookup that lets the client re-attach to that run's frame stream and mark the
    session busy in the list. Declared BEFORE /sessions/{sid}/… so "live-runs" isn't
    captured as a session id."""
    from curry_leaves_assistant.agents import chat_runs
    return chat_runs.live_session_runs()


@router.get("/sessions/{sid}/messages")
def session_messages(sid: str):
    return chat_sessions.get_messages(sid)


@router.get("/sessions/{sid}/tasks")
def session_tasks(sid: str):
    """The session's current agent task list, so reopening a chat restores the Tasks panel."""
    return chat_sessions.get_tasks(sid)


@router.delete("/sessions/{sid}")
def delete_session(sid: str):
    return {"ok": chat_sessions.delete(sid)}


class ForkBody(BaseModel):
    uptoTurn: int | None = None  # 0-indexed user turn to fork after; None forks the whole chat


@router.post("/sessions/{sid}/fork")
def fork_session_route(sid: str, body: ForkBody):
    """Branch a new session off `sid`'s transcript up through the given user turn — see
    chat_sessions.fork. get_messages(sid) indexes user turns in display order, so a UI
    passes the same index it renders next to a "Fork from here" action."""
    if not chat_sessions.store.exists(sid):
        return Response(status_code=404)
    new_id = chat_sessions.fork(sid, upto_turn=body.uptoTurn)
    return {"sessionId": new_id}


@router.post("/sessions/attach")
async def attach_session_file(request: Request, agentId: str, filename: str,
                              sessionId: str | None = None, model: str | None = None):
    """Upload a file (raw octet-stream body) into a chat session, rendering it to
    markdown. Creates the session if one doesn't exist yet, so attaching a file is
    enough to start a chat. Returns the (possibly new) session id and file metadata."""
    raw = await request.body()
    sid = chat_sessions.ensure(sessionId, agentId, model, filename)
    file = await asyncio.to_thread(chat_sessions.attach_file, sid, filename, raw)
    return {"sessionId": sid, "file": file}



class CompactBody(BaseModel):
    sessionId: str
    agentId: str


@router.post("/compact")
async def compact_conversation(body: CompactBody):
    """Summarize a session and replace its transcript with the compact recap."""
    msgs = chat_sessions.get_messages(body.sessionId)
    before = sum(len(m.get("content", "")) for m in msgs) // 4
    convo = "\n".join(f"{'User' if m['role'] == 'user' else 'Assistant'}: {m.get('content', '')}" for m in msgs)
    base = agent_store.read_agent(body.agentId) or {"id": body.agentId, "model": None}
    prompt = (
        "Summarize this conversation as a compact recap the assistant can use to continue it. "
        "Preserve key facts, decisions, names, and open questions. Use short bullet points.\n\n" + convo
    )
    agent = {**base, "tools": [], "instructions": "You faithfully and concisely summarize conversations."}
    summary = await agent_engine.run_agent(agent, prompt)
    chat_sessions.replace_with_summary(body.sessionId, summary)
    return {"summary": summary, "tokensSaved": max(0, before - len(summary) // 4)}


# ─── Conversation → learned skill (one click from the chat header) ─────────────
# Two steps, so the human stays in the loop: POST /sessions/{sid}/to-skill ANALYZES the
# conversation against the existing skill catalog and returns a verdict (create a new skill,
# improve an existing learned one, or skip — push back when there's nothing worth keeping);
# POST /sessions/{sid}/to-skill/apply persists whichever draft the user confirmed.
_SKILL_REVIEW_INSTRUCTIONS = (
    "You review ONE chat conversation and decide whether it is worth turning into a SKILL — a "
    "reusable procedure the same assistant can follow next time a similar request comes in, "
    "with fewer steps and fewer tokens. You are also given the existing skill catalog. "
    "Pick exactly ONE verdict:\n"
    "• skip — there is nothing durable to extract (chit-chat, a one-off question, no repeatable "
    "procedure, no correction worth keeping), OR an existing skill already covers this well "
    "(name it in `existing`). Explain plainly in `reason`. Do NOT invent a skill just to "
    "produce something — skipping is the right answer for most ordinary conversations.\n"
    "• update — an existing LEARNED skill (marked [learned]; its body is included below) "
    "handles the same task, but this conversation adds steps or corrections worth folding in. "
    "Set `existing` to its name and `body` to the COMPLETE revised body (not a diff); in "
    "`reason` say what changed and why the revision is better.\n"
    "• create — a repeatable procedure no existing skill covers. Fill name (short kebab-case, "
    "named after the TASK, not this conversation), description (one sentence — when to reach "
    "for this skill; future asks are matched against it), and body (short imperative markdown: "
    "the goal, the steps IN ORDER, which tools to call with which argument shapes, and any "
    "pitfalls. Replace values specific to this one conversation — dates, ids, filenames — with "
    "<placeholders>, but keep exact tool names and argument shapes. Fold in what the user "
    "corrected or clarified mid-conversation — that is the highest-value content. Dead ends "
    "only under 'Avoid', and only when knowing them prevents repeating the mistake).\n"
    "For create/update, fill `benefits`: 2–4 short bullets on what future runs concretely gain "
    "(steps skipped, round-trips avoided, mistakes prevented). Be faithful — only steps that "
    "actually happened and corrections actually given."
)


class _SkillVerdict(BaseModel):
    verdict: str  # create | update | skip
    name: str = ""
    description: str = ""
    body: str = ""
    existing: str = ""      # update: the skill to revise; skip: the skill that already covers it
    benefits: list[str] = []
    reason: str = ""


def _clip(text: str, n: int = 700) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + " …[truncated]"


def _skill_material(msgs: list[dict]) -> str:
    """The conversation as distillation material: user asks and assistant replies in full
    flow, tool calls with their (clipped) args and results — the steps a skill captures."""
    lines: list[str] = []
    for m in msgs:
        if m.get("role") == "user":
            lines.append(f"USER: {_clip(m.get('content', ''), 1500)}")
            continue
        for tc in m.get("tools") or []:
            lines.append(f"  TOOL {tc.get('name')}({_clip(tc.get('input', ''), 400)})"
                         f" -> [{tc.get('status')}] {_clip(tc.get('output', ''))}")
        if m.get("content"):
            lines.append(f"ASSISTANT: {_clip(m['content'], 1500)}")
    return "\n".join(lines)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60].rstrip("-") or "learned-skill"


def _skill_catalog(agent_id: str) -> str:
    """The existing skills as review context: a teaser line per skill, plus the full (clipped)
    bodies of live LEARNED skills this agent can see — those are the only valid update targets."""
    from curry_leaves_assistant.stores import skill_meta, skills_store
    from curry_leaves_assistant.stores.agent_store import parse_frontmatter
    lines, bodies = [], []
    for entry in skills_store.list_skills():
        meta = skill_meta.read_meta(entry["name"])
        learned = bool(meta.get("status"))
        tag = f" [learned, {meta.get('status')}]" if learned else ""
        lines.append(f"- {entry['name']}: {entry['description']}{tag}")
        scope = meta.get("appliesTo")
        visible = scope in (None, "all") or (isinstance(scope, list) and agent_id in scope)
        if learned and meta.get("status") != "retired" and visible and len(bodies) < 10:
            try:
                _, b = parse_frontmatter(skills_store.read_file(entry["name"], "SKILL.md"))
                bodies.append(f"### {entry['name']}\n{_clip(b, 3500)}")
            except (FileNotFoundError, ValueError):
                pass
    out = "EXISTING SKILLS:\n" + ("\n".join(lines) if lines else "(none)")
    if bodies:
        out += "\n\nLEARNED SKILL BODIES (the only valid `update` targets):\n\n" + "\n\n".join(bodies)
    return out


@router.post("/sessions/{sid}/to-skill")
async def session_to_skill(sid: str):
    """ANALYZE a conversation for skill-worthiness — no side effects. Returns the model's
    verdict (create | update | skip) with the drafted skill, the benefits it would bring, and
    the reasoning, so the UI can show the user exactly what would be saved (or why nothing
    should be) before anything is written."""
    from curry_leaves_assistant.stores import skill_meta

    if not chat_sessions.store.exists(sid):
        return Response(status_code=404)
    msgs = chat_sessions.get_messages(sid)
    if not any(m.get("role") == "user" for m in msgs):
        return Response(content="nothing to learn from an empty conversation", status_code=400)

    agent_id = chat_sessions.get_sidecar(sid).get("agentId") or ""
    base = agent_store.read_agent(agent_id) or {"id": agent_id or "skill-distiller", "model": None}
    agent = {**base, "tools": [], "instructions": _SKILL_REVIEW_INSTRUCTIONS}
    prompt = ("Review this conversation and decide: skip, update an existing learned skill, "
              "or create a new one.\n\nCONVERSATION:\n" + _skill_material(msgs)
              + "\n\n" + _skill_catalog(agent_id))
    v, _raw = await agent_engine.run_agent_structured(agent, prompt, _SkillVerdict)
    if v is None:
        return Response(content="the model could not produce a reviewable draft — try again",
                        status_code=502)

    if v.verdict not in ("create", "update", "skip"):
        v.verdict = "skip" if not v.body.strip() else "create"
    if v.verdict == "update" and not skill_meta.read_meta(v.existing).get("status"):
        # Only learned skills are updatable; a seeded/manual match becomes a new skill instead.
        v.verdict, v.name = "create", v.name or v.existing
    if v.verdict in ("create", "update") and not v.body.strip():
        v.verdict = "skip"
        v.reason = v.reason or "The conversation didn't yield a concrete procedure to save."
    if v.verdict == "create":
        v.name = _slug(v.name)
    return {"verdict": v.verdict, "name": v.name, "description": v.description, "body": v.body,
            "existing": v.existing, "benefits": v.benefits, "reason": v.reason}


class SkillApplyBody(BaseModel):
    verdict: str  # create | update — what the user confirmed from the analyze step
    name: str
    description: str = ""
    body: str


@router.post("/sessions/{sid}/to-skill/apply")
async def session_skill_apply(sid: str, body: SkillApplyBody):
    """Persist the confirmed draft. create → a new governed learned skill (trial, scoped to
    the session's agent, this trace as provenance). update → revise the learned skill's body,
    keeping its status/metrics and appending this trace to its provenance. Either way the
    existing loop takes over: teaser injection, per-run metrics, nightly promote/retire."""
    from curry_leaves_assistant.core.store import now_iso
    from curry_leaves_assistant.stores import skill_meta, skills_store

    if not chat_sessions.store.exists(sid):
        return Response(status_code=404)
    if not body.body.strip():
        return Response(content="an empty skill body cannot be saved", status_code=400)
    agent_id = chat_sessions.get_sidecar(sid).get("agentId") or ""

    if body.verdict == "update":
        meta = skill_meta.read_meta(body.name)
        if not meta.get("status"):
            return Response(content=f"'{body.name}' is not a learned skill — only learned "
                                    "skills can be updated this way", status_code=400)
        from curry_leaves_assistant.stores.agent_store import parse_frontmatter
        fm, _old = parse_frontmatter(skills_store.read_file(body.name, "SKILL.md"))
        if body.description:
            fm["description"] = body.description
        fm["learnedFrom"] = list(dict.fromkeys([*(fm.get("learnedFrom") or []), f"tr_{sid}"]))
        fm["learnedAt"] = now_iso()
        skills_store.write_file(body.name, "SKILL.md", skill_meta._render(fm, body.body))
        return {"name": body.name, "description": fm.get("description", ""),
                "status": fm.get("status"), "updated": True}

    name = _slug(body.name)
    for i in range(2, 50):  # never fail on a name collision — suffix instead
        try:
            skills_store.create_skill(name, body.description, body.body)
            break
        except FileExistsError:
            name = f"{_slug(body.name)}-{i}"
    else:
        return Response(content=f"too many skills named like {_slug(body.name)!r}", status_code=409)
    skill_meta.write_meta(name, {
        "status": "trial",
        "appliesTo": [agent_id] if agent_id else "all",
        "learnedFrom": [f"tr_{sid}"],
        "learnedAt": now_iso(),
        "metrics": {"loads": 0, "successes": 0, "failures": 0},
    })
    return {"name": name, "description": body.description,
            "appliesTo": [agent_id] if agent_id else "all", "status": "trial", "updated": False}
