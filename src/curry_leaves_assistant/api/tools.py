"""Agent tool configuration and external MCP servers."""
from __future__ import annotations

from fastapi import APIRouter, Request, Response

from curry_leaves_assistant.agents import agent_tools
from curry_leaves_assistant.providers import mcp_client
from curry_leaves_assistant.stores import mcp_store, tools_store

router = APIRouter(tags=["tools"])


@router.get("/tools")
def list_tools():
    """The full built-in tool catalog (definitions from agent_tools) merged with each
    tool's saved config (from tools_store). Lives here, not in the store, so the store
    layer never has to reach up into agents/ — see the layered-architecture contract."""
    cfg = tools_store.read_all()
    out = []
    for name, tool in agent_tools.ALL_TOOLS.items():
        c = cfg.get(name, {})
        try:
            schema = tool.Args.model_json_schema()
        except Exception:
            schema = {}
        out.append({
            "name": name,
            "description": getattr(tool, "description", ""),
            "risk": getattr(tool, "risk", "read"),
            "schema": schema,
            "enabled": c.get("enabled", True),
            "config": c.get("config", {}),
        })
    out.sort(key=lambda t: t["name"])
    return out


@router.patch("/tools/{name}")
async def patch_tool(name: str, request: Request):
    return tools_store.set_config(name, await request.json())


# ─── MCP servers ────────────────────────────────────────────────────────────────
@router.get("/mcp/servers")
def list_mcp_servers():
    return mcp_store.list_servers()


@router.post("/mcp/servers/test")
async def test_mcp_server(request: Request):
    """Connect to a not-yet-saved server config and list its remote tools, without
    persisting anything — the "Test connection" step before a user picks tools to save."""
    cfg = await request.json()
    if not cfg.get("name"):
        return Response(content="A server name is required.", status_code=400)
    return await mcp_client.test_connection(cfg)


@router.post("/mcp/servers")
async def save_mcp_server(request: Request):
    """Save a server's connection config (post-test) plus the tools the user picked."""
    body = await request.json()
    if not body.get("name"):
        return Response(content="A server name is required.", status_code=400)
    return mcp_store.save_server(body)


@router.patch("/mcp/servers/{name}")
async def patch_mcp_server(name: str, request: Request):
    body = await request.json()
    try:
        if "pickedTools" in body:
            mcp_store.set_picked_tools(name, body["pickedTools"])
        if "enabled" in body:
            mcp_store.set_enabled(name, bool(body["enabled"]))
    except KeyError:
        return Response(status_code=404)
    return mcp_store.get(name)


@router.delete("/mcp/servers/{name}")
def delete_mcp_server(name: str):
    return {"ok": mcp_store.delete_server(name)}
