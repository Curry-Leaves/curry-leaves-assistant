"""Agent definitions: CRUD, AI-drafted configs, run history, and manual runs."""
from __future__ import annotations

import json
import re
import uuid

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from curry_leaves_assistant.agents import agent_engine, agent_tools
from curry_leaves_assistant.core import events, paths
from curry_leaves_assistant.core.store import now_iso, read_json
from curry_leaves_assistant.providers import mcp_client
from curry_leaves_assistant.orchestration import work
from curry_leaves_assistant.orchestration.work import BAND_INTERACTIVE, WorkItem
from curry_leaves_assistant.stores import agent_store, mcp_store

router = APIRouter(tags=["agents"])


@router.get("/pending-inputs")
def pending_inputs():
    """Unanswered questions/approvals from suspended runs, read from the durable
    queue/<jobId>.pending.json records — so they outlive the activity feed and a UI
    reload. Answerable = the run is parked in THIS process: a SuspendHost wait, or a
    streaming background run (still `running` in the kernel while its ask blocks).
    Residue from finished/dead jobs is pruned here; a `queued` job (recovery re-run
    not yet started) keeps its file quietly and re-asks when it gets there."""
    from curry_leaves_assistant.orchestration import suspend_host
    hosted = suspend_host.hosted_job_ids()
    out = []
    for f in sorted(paths.QUEUE_DIR.glob("*.pending.json")):
        rec = read_json(f, {}) or {}
        job_id = str(rec.get("jobId", ""))
        status = work.job_status(job_id) if job_id else None
        state = (status or {}).get("state")
        if job_id and (job_id in hosted or state == "running"):
            out.append(rec)
        elif state != "queued":
            f.unlink(missing_ok=True)
    return out


@router.get("/agent-options")
def agent_options():
    """Options for the agent editor: available tools, trigger event types, and skills."""
    from curry_leaves.skills import SkillRegistry
    tools = [{"name": n, "description": getattr(t, "description", "")} for n, t in agent_tools.ALL_TOOLS.items()]
    # Fold in each enabled MCP server's picked tools, namespaced mcp__<server>__<tool> —
    # same names agent_tools.resolve_tools/agent_engine._build_agent look for at run time.
    for server in mcp_store.list_servers():
        if not server.get("enabled", True):
            continue
        for tool_name in server.get("pickedTools", []):
            tools.append({
                "name": mcp_client.qualified_name(server["name"], tool_name),
                "description": f"[{server['name']}] {tool_name}",
            })
    # All discovered skills (including hidden ones) are bindable to an agent.
    skills = [{"name": s.name, "description": s.description} for s in SkillRegistry(discover=True).all()]
    return {"tools": tools, "triggers": events.trigger_types(), "skills": skills}


@router.get("/agents")
def list_agents():
    return agent_store.list_agents()


@router.get("/agents/{agent_id}")
def get_agent(agent_id: str):
    return agent_store.read_agent(agent_id) or Response(status_code=404)


@router.post("/agents")
async def save_agent(request: Request):
    return agent_store.write_agent(await request.json())


class GenerateAgentBody(BaseModel):
    description: str


@router.post("/agents/generate")
async def generate_agent(body: GenerateAgentBody):
    """Draft an agent config from a plain-English description (uses the active LLM)."""
    tool_names = list(agent_tools.ALL_TOOLS)
    trig = events.trigger_types()
    prompt = (
        f'Design a Curry Leaves assistant for this need: "{body.description}".\n'
        "Name it after its ROLE, not a person — a short Title Case label of what it does, "
        "like \"News Scout\", \"Knowledge Filer\", or \"Meeting Copilot\". Never a human first "
        "name, and no parentheses.\n"
        "Return ONLY a JSON object (no prose) with keys: name (the role label above), "
        "description (one line, LEADING with the role, describing what it does), "
        "instructions (a system prompt, 2-5 sentences), "
        f"tools (a subset of {tool_names}), triggers (a subset of {trig})."
    )
    spec = {"id": "generator", "model": None, "tools": [],
            "instructions": "You output only valid minified JSON. No markdown, no prose.", "description": ""}
    try:
        out = await agent_engine.run_agent(spec, prompt)
    except Exception as e:
        return Response(content=f"Agent generation failed: {e}", status_code=502)
    m = re.search(r"\{.*\}", out or "", re.S)
    if not m:
        return Response(content="The AI provider returned no usable output — check the active provider/model in Settings.",
                         status_code=502)
    try:
        cfg = json.loads(m.group(0))
    except Exception:
        cfg = {}
    return {
        "name": cfg.get("name") or "New agent",
        "description": cfg.get("description") or "",
        "instructions": cfg.get("instructions") or "",
        "tools": [t for t in (cfg.get("tools") or []) if t in tool_names],
        "triggers": [t for t in (cfg.get("triggers") or []) if t in trig],
    }


@router.patch("/agents/{agent_id}")
async def patch_agent(agent_id: str, request: Request):
    """Update operational meta only (enabled / triggers / schedule / surfaces)."""
    body = await request.json()
    allowed = {k: body[k] for k in ("enabled", "triggers", "schedule", "surfaces") if k in body}
    if not allowed:
        return Response(status_code=400)
    agent_store.update_meta(agent_id, **allowed)
    return agent_store.read_agent(agent_id) or Response(status_code=404)


@router.delete("/agents/{agent_id}")
def delete_agent_route(agent_id: str):
    return {"ok": agent_store.delete_agent(agent_id)}


@router.get("/agents/{agent_id}/runs")
def agent_runs(agent_id: str, limit: int = 20):
    """This agent's work, newest first: currently QUEUED / RUNNING WorkItems (from the Work
    Kernel's durable queue) on top, then completed run records. So the page shows the whole
    lifecycle — a job waiting in a lane, one in flight, and the history below."""
    runs = []
    # Live WorkItems for this agent (queued or in-flight) — not yet a run record.
    for job in work.queued_jobs(agent_id):
        runs.append({
            "id": job.get("id"),
            "agentId": agent_id,
            "status": "running" if job.get("state") == "running" else "queued",
            "trigger": job.get("trigger"),
            "traceId": job.get("traceId"),
            "startedAt": job.get("startedAt"),
            "createdAt": job.get("createdAt"),
            "lane": job.get("lane"),
        })
    # Completed run records (history).
    d = paths.agent_runs_dir(agent_id)
    if d.is_dir():
        for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            r = read_json(f, None)
            if r:
                runs.append(r)
    return runs


@router.post("/agents/{agent_id}/run")
def run_agent_now(agent_id: str):
    """Manually enqueue an autonomous run."""
    agent = agent_store.read_agent(agent_id)
    if agent is None:
        return Response(status_code=404)
    ev_id = uuid.uuid4().hex
    job_id = work.submit(WorkItem(
        kind="agent", agent_id=agent_id,
        trigger={"id": ev_id, "type": "schedule", "occurredAt": now_iso(), "payload": {}},
        mode="background", lane=agent.get("lane") or "general", band=BAND_INTERACTIVE,
        autonomy=agent.get("autonomy") or "auto", dedupe_key=ev_id))
    return {"jobId": job_id}
