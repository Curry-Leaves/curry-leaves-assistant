"""External MCP server configuration — one JSON file at
~/.curry-leaves/agents/tools/mcp_servers.json holding each user-added server's connection
details plus which of its remote tools are picked for use. Agents consult this (via
agent_tools.resolve_tools) the same way they consult tools_store for built-ins.
"""
from __future__ import annotations

from curry_leaves_assistant.core.paths import AGENTS_DIR
from curry_leaves_assistant.core.store import read_json, write_json

MCP_DIR = AGENTS_DIR / "tools"
MCP_CONFIG = MCP_DIR / "mcp_servers.json"


def read_all() -> dict:
    return read_json(MCP_CONFIG, {})


def write_all(data: dict) -> None:
    MCP_DIR.mkdir(parents=True, exist_ok=True)
    write_json(MCP_CONFIG, data)


def get(name: str) -> dict | None:
    cfg = read_all().get(name)
    return dict(cfg, name=name) if cfg is not None else None


def list_servers() -> list[dict]:
    data = read_all()
    return [dict(cfg, name=name) for name, cfg in sorted(data.items())]


def save_server(cfg: dict) -> dict:
    """Create or update a server's connection config. Expects at least `name` and
    `transport` ("stdio" | "http"); stdio needs `command`/`args`/`env`, http needs
    `url`/`headers`. Preserves `pickedTools`/`enabled` unless the caller overrides them."""
    name = cfg["name"]
    data = read_all()
    cur = data.get(name, {})
    cur.update({k: v for k, v in cfg.items() if k != "name"})
    cur.setdefault("enabled", True)
    cur.setdefault("pickedTools", [])
    cur.setdefault("risk", "exec")
    data[name] = cur
    write_all(data)
    return dict(cur, name=name)


def set_picked_tools(name: str, tool_names: list[str]) -> dict:
    data = read_all()
    cur = data.get(name)
    if cur is None:
        raise KeyError(name)
    cur["pickedTools"] = list(tool_names)
    data[name] = cur
    write_all(data)
    return dict(cur, name=name)


def set_enabled(name: str, enabled: bool) -> dict:
    data = read_all()
    cur = data.get(name)
    if cur is None:
        raise KeyError(name)
    cur["enabled"] = bool(enabled)
    data[name] = cur
    write_all(data)
    return dict(cur, name=name)


def delete_server(name: str) -> bool:
    data = read_all()
    if name not in data:
        return False
    del data[name]
    write_all(data)
    return True
