"""Bridges mcp_store's saved configs to curry_leaves.mcp's connectable server objects.

Two use sites:
  - app.py's /mcp/servers/{name}/test route: connect once, list remote tools, close.
  - agent_engine._build_agent: connect each MCP server an agent's tools reference,
    pick the agent's chosen tools off it, and close all of them when the run ends.
"""
from __future__ import annotations

from curry_leaves.mcp.server import McpServerHttp, McpServerStdio

MCP_TOOL_PREFIX = "mcp__"


def build_server(cfg: dict):
    """Construct (but do not connect) an McpServerStdio/McpServerHttp from a stored config."""
    transport = cfg.get("transport", "stdio")
    risk = cfg.get("risk") or "exec"
    if transport == "http":
        return McpServerHttp(
            name=cfg["name"],
            url=cfg["url"],
            headers=cfg.get("headers") or {},
            transport=cfg.get("httpTransport", "http"),
            risk=risk,
        )
    return McpServerStdio(
        name=cfg["name"],
        command=cfg["command"],
        args=cfg.get("args") or [],
        env=cfg.get("env") or {},
        cwd=cfg.get("cwd") or None,
        risk=risk,
    )


async def test_connection(cfg: dict) -> dict:
    """Connect to a server, list its remote tools, then close. Returns
    {"ok": True, "tools": [{"name", "description"}]} or {"ok": False, "error": str}."""
    server = build_server(cfg)
    try:
        await server.connect()
        tools = await server.list_tools()
        prefix = f"{MCP_TOOL_PREFIX}{cfg['name']}__"
        out = []
        for t in tools:
            bare = t.name[len(prefix):] if t.name.startswith(prefix) else t.name
            out.append({"name": bare, "description": getattr(t, "description", "") or ""})
        return {"ok": True, "tools": out}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        await server.close()


def qualified_name(server_name: str, tool_name: str) -> str:
    return f"{MCP_TOOL_PREFIX}{server_name}__{tool_name}"


def parse_qualified_name(qualified: str) -> tuple[str, str] | None:
    """`mcp__<server>__<tool>` → (server, tool), or None if not an MCP tool name."""
    if not qualified.startswith(MCP_TOOL_PREFIX):
        return None
    rest = qualified[len(MCP_TOOL_PREFIX):]
    server, sep, tool = rest.partition("__")
    return (server, tool) if sep else None


async def connect_servers_for_tools(tool_names: list[str]):
    """Connect every MCP server referenced by `tool_names` (agent's declared tools),
    pick only the requested tools off each, and return (tool_instances, servers) so
    the caller can close `servers` when the run ends. Servers that fail to connect, or
    picked tools no longer present remotely, are skipped rather than failing the run."""
    from curry_leaves_assistant.stores import mcp_store

    from curry_leaves.mcp.pick import mcp_tools as pick_tools

    by_server: dict[str, list[str]] = {}
    for qn in tool_names:
        parsed = parse_qualified_name(qn)
        if parsed:
            server, tool = parsed
            by_server.setdefault(server, []).append(tool)

    tools: list = []
    servers: list = []
    for server_name, wanted in by_server.items():
        cfg = mcp_store.get(server_name)
        if not cfg or not cfg.get("enabled", True):
            continue
        server = build_server(cfg)
        try:
            await server.connect()
        except Exception:
            continue
        servers.append(server)
        try:
            tools.extend(await pick_tools(server, *wanted))
        except ValueError:
            available = {t.name for t in await server.list_tools()}
            prefix = f"{MCP_TOOL_PREFIX}{server_name}__"
            bare_available = {n[len(prefix):] if n.startswith(prefix) else n for n in available}
            usable = [w for w in wanted if w in bare_available]
            if usable:
                tools.extend(await pick_tools(server, *usable))
    return tools, servers


async def close_servers(servers: list) -> None:
    for server in servers:
        try:
            await server.close()
        except Exception:
            pass
