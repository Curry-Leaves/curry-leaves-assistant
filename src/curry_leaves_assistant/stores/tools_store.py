"""Tool configuration — one JSON file at ~/.curry-leaves/agents/tools/tools.json holding
per-tool settings (enabled flag + a free-form config object). Agents consult this:
a disabled tool is excluded from every agent's toolset.
"""
from __future__ import annotations

from curry_leaves_assistant.core.paths import AGENTS_DIR
from curry_leaves_assistant.core.store import read_json, write_json

TOOLS_DIR = AGENTS_DIR / "tools"
TOOLS_CONFIG = TOOLS_DIR / "tools.json"


def read_all() -> dict:
    return read_json(TOOLS_CONFIG, {})


def write_all(data: dict) -> None:
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    write_json(TOOLS_CONFIG, data)


def get(name: str) -> dict:
    return read_all().get(name, {})


def is_enabled(name: str) -> bool:
    return read_all().get(name, {}).get("enabled", True)


def set_config(name: str, patch: dict) -> dict:
    data = read_all()
    cur = data.get(name, {})
    if "enabled" in patch:
        cur["enabled"] = bool(patch["enabled"])
    if "config" in patch:
        cur["config"] = patch["config"]
    data[name] = cur
    write_all(data)
    return cur


# The tool *catalog* (definitions + config, needs agent_tools) is built in api/tools.py,
# not here — a store must not import the agents layer. This module owns only config.
