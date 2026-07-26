"""Agent dashboard: named boards of tiles, each tile bound to one agent.

A tile is a binding, not the agent itself — placement on the grid, a plain-language
`focus` + `rules` brief appended to the agent's own instructions for that run, an
output shape, a refresh mode, and the cached last output. Boards are file-per-record
like everything else: ``dashboard/<boardId>.json`` + a small ``dashboard/index.json``
listing id/name/order for the switcher.
"""
from __future__ import annotations

import uuid

from curry_leaves_assistant.core.paths import DASHBOARD_INDEX_PATH, board_path
from curry_leaves_assistant.core.store import read_json, write_json, now_iso

OUTPUT_FORMATS = ("summary", "list", "metric", "table", "markdown", "diff")
# Formats whose runs carry a pydantic output_type (agents/tile_shapes.py) so the reply
# comes back as validated JSON. Everything except markdown, which stays free-text —
# kept as a literal here (not imported from tile_shapes) so this store stays free of
# agents-layer imports; tile_shapes asserts the two stay in sync.
STRUCTURED_FORMATS = ("summary", "list", "metric", "table", "diff")
TILE_STYLES = ("card", "flat", "outlined", "accent-bar", "glass", "glow", "gradient-edge")


def _index() -> list[dict]:
    return read_json(DASHBOARD_INDEX_PATH, [])


def _write_index(entries: list[dict]) -> None:
    write_json(DASHBOARD_INDEX_PATH, entries)


def list_boards() -> list[dict]:
    return sorted(_index(), key=lambda b: b.get("order", 0))


def get_board(board_id: str) -> dict | None:
    return read_json(board_path(board_id), None)


def create_board(name: str) -> dict:
    idx = _index()
    board = {
        "id": uuid.uuid4().hex,
        "name": name.strip() or "Board",
        "tiles": [],
        "order": len(idx),
        "updatedAt": now_iso(),
    }
    write_json(board_path(board["id"]), board)
    idx.append({"id": board["id"], "name": board["name"], "order": board["order"]})
    _write_index(idx)
    return board


def update_board(board_id: str, **patch) -> dict | None:
    board = get_board(board_id)
    if board is None:
        return None
    if "name" in patch:
        board["name"] = (patch["name"] or "").strip() or board["name"]
    if "order" in patch:
        board["order"] = patch["order"]
    board["updatedAt"] = now_iso()
    write_json(board_path(board_id), board)
    idx = _index()
    for e in idx:
        if e["id"] == board_id:
            e["name"] = board["name"]
            e["order"] = board["order"]
    _write_index(idx)
    return board


def delete_board(board_id: str) -> bool:
    p = board_path(board_id)
    existed = p.exists()
    p.unlink(missing_ok=True)
    idx = [e for e in _index() if e["id"] != board_id]
    _write_index(idx)
    return existed


def ensure_default_board() -> dict:
    """Seed one board on first run, so the dashboard is never empty-state at the API level."""
    idx = _index()
    if idx:
        return get_board(idx[0]["id"]) or create_board("Board")
    return create_board("Dashboard")


# ─── Tiles ──────────────────────────────────────────────────────────────────────
def _default_layout(existing: list[dict]) -> dict:
    """Stack new tiles below the lowest occupied row so they never overlap on add."""
    bottom = max((t["layout"]["y"] + t["layout"]["h"] for t in existing), default=0)
    return {"x": 0, "y": bottom, "w": 4, "h": 2}


def add_tile(board_id: str, agent_id: str, title: str | None = None) -> dict | None:
    board = get_board(board_id)
    if board is None:
        return None
    tile = {
        "id": uuid.uuid4().hex,
        "agentId": agent_id,
        "title": title or agent_id,
        "layout": _default_layout(board["tiles"]),
        "config": {
            "focus": "",
            "rules": "",
            "outputFormat": "summary",
            "emptyMessage": "",
            "markdownTemplate": "",
            "style": "card",
            "alert": "",
            "refresh": {"mode": "manual"},
        },
        "state": {
            "lastOutput": None,
            "lastRunAt": None,
            "lastRunStatus": "idle",
            "lastJobId": None,
        },
    }
    board["tiles"].append(tile)
    board["updatedAt"] = now_iso()
    write_json(board_path(board_id), board)
    return tile


def _find_tile(board: dict, tile_id: str) -> dict | None:
    return next((t for t in board["tiles"] if t["id"] == tile_id), None)


def update_tile(board_id: str, tile_id: str, **patch) -> dict | None:
    board = get_board(board_id)
    if board is None:
        return None
    tile = _find_tile(board, tile_id)
    if tile is None:
        return None
    if "title" in patch:
        tile["title"] = patch["title"]
    if "layout" in patch:
        tile["layout"].update(patch["layout"])
    if "config" in patch:
        cfg = patch["config"] or {}
        if "focus" in cfg:
            tile["config"]["focus"] = cfg["focus"]
        if "rules" in cfg:
            tile["config"]["rules"] = cfg["rules"]
        if "outputFormat" in cfg and cfg["outputFormat"] in OUTPUT_FORMATS:
            tile["config"]["outputFormat"] = cfg["outputFormat"]
        if "emptyMessage" in cfg:
            tile["config"]["emptyMessage"] = cfg["emptyMessage"]
        if "markdownTemplate" in cfg:
            tile["config"]["markdownTemplate"] = cfg["markdownTemplate"]
        if "style" in cfg and cfg["style"] in TILE_STYLES:
            tile["config"]["style"] = cfg["style"]
        if "alert" in cfg:
            tile["config"]["alert"] = cfg["alert"]
        if "refresh" in cfg:
            tile["config"]["refresh"] = cfg["refresh"]
    board["updatedAt"] = now_iso()
    write_json(board_path(board_id), board)
    return tile


def update_layout(board_id: str, layouts: list[dict]) -> dict | None:
    """Bulk layout patch after a drag/resize: [{id, x, y, w, h}, ...]."""
    board = get_board(board_id)
    if board is None:
        return None
    by_id = {l["id"]: l for l in layouts}
    for tile in board["tiles"]:
        l = by_id.get(tile["id"])
        if l:
            tile["layout"] = {"x": l["x"], "y": l["y"], "w": l["w"], "h": l["h"]}
    board["updatedAt"] = now_iso()
    write_json(board_path(board_id), board)
    return board


def delete_tile(board_id: str, tile_id: str) -> bool:
    board = get_board(board_id)
    if board is None:
        return False
    before = len(board["tiles"])
    board["tiles"] = [t for t in board["tiles"] if t["id"] != tile_id]
    if len(board["tiles"]) == before:
        return False
    board["updatedAt"] = now_iso()
    write_json(board_path(board_id), board)
    return True


def set_tile_state(board_id: str, tile_id: str, **state) -> dict | None:
    board = get_board(board_id)
    if board is None:
        return None
    tile = _find_tile(board, tile_id)
    if tile is None:
        return None
    tile["state"].update(state)
    write_json(board_path(board_id), board)
    return tile


def all_tiles() -> list[tuple[str, dict]]:
    """Every (boardId, tile) across every board — used by the tile scheduler."""
    out = []
    for entry in _index():
        board = get_board(entry["id"])
        if board:
            out.extend((board["id"], t) for t in board["tiles"])
    return out


def build_task_text(tile: dict) -> str:
    """Compose the run's task text from the tile's brief — same shape as a chat
    message; the agent uses its own tools to satisfy it."""
    focus = (tile["config"].get("focus") or "").strip()
    rules = (tile["config"].get("rules") or "").strip()
    fmt = tile["config"].get("outputFormat", "summary")
    alert = (tile["config"].get("alert") or "").strip()
    parts = []
    if focus:
        parts.append(focus)
    else:
        parts.append("Give a status update relevant to your role.")
    if rules:
        parts.append(f"Constraints: {rules}")
    # Appended last regardless of format, so it reads as a final directive on top of
    # whatever shape the reply takes — the notify_user tool is only in this run's tool
    # list when an alert is configured (see dashboard_runner.run_tile), so naming it
    # here is safe even though it's not one of the agent's normally-declared tools.
    alert_instruction = ""
    if alert:
        alert_instruction = (
            f'\n\nAlert condition: "{alert}". After you finish, if what you found this '
            "run actually meets that condition, call the notify_user tool with a short "
            "message explaining why. If the condition is not met, do not call it."
        )
    md_template = (tile["config"].get("markdownTemplate") or "").strip()
    if fmt == "markdown" and md_template:
        # A concrete skeleton beats a vague "write markdown" instruction — the
        # agent fills in placeholders instead of inventing structure each run,
        # so repeated runs of the same tile come back shaped the same way. This
        # replaces (not appends to) the generic empty-reply instruction below,
        # since "keep every heading, write None under empty ones" is the correct
        # empty-handling rule here — a bare "None" would blow away the skeleton.
        parts.append(
            "Follow this exact markdown structure — same headings, same section "
            "order. Replace the placeholder text under each heading with real "
            "content; if a section has nothing to report, keep the heading and "
            "write \"None\" under it. Do not add, remove, or rename sections. If "
            "EVERY section has nothing to report, still output the full "
            "structure with \"None\" under each heading — don't collapse it to "
            "a single word.\n\n"
            f"{md_template}"
        )
        return "\n\n".join(parts) + alert_instruction
    if fmt in STRUCTURED_FORMATS:
        # The run carries an output_type (see tile_shapes.OUTPUT_TYPES) — the kernel
        # injects the JSON schema and demands the final reply match it, so no prose
        # shape hint is needed; only the empty-flag semantics are worth spelling out.
        parts.append(
            "If you find nothing relevant to report this run, set \"empty\": true in "
            "your reply and leave the content fields minimal — don't pad it out."
        )
        return "\n\n".join(parts) + alert_instruction
    parts.append(f"Shape your reply as a {_FORMAT_HINT.get(fmt, 'concise summary')}.")
    parts.append("If you find nothing relevant, reply with just \"None\" — don't pad it out.")
    return "\n\n".join(parts) + alert_instruction


_FORMAT_HINT = {
    "summary": "concise prose summary (2-5 sentences)",
    "list": "short bulleted list",
    "metric": "single headline number with a brief label (e.g. '14 unread'), nothing else",
    "table": "markdown table",
    "markdown": "markdown document",
    "diff": (
        "unified diff: one line per changed item, prefixed with \"+\" for "
        "additions, \"-\" for removals, and \" \" (a leading space) for an "
        "unchanged item worth showing as context. No diff headers (---/+++/@@)."
    ),
}


# Phrases an agent commonly uses to report "nothing to show" in prose — checked
# against the WHOLE text (not a substring) after light normalization, so a summary
# that merely mentions "no blockers" as one clause among several isn't misflagged.
_EMPTY_PHRASES = (
    "no data", "no results", "nothing to report", "nothing found", "none found",
    "no items", "no updates", "all clear", "n/a", "none", "nothing",
)


def _is_empty_prose(text: str) -> bool:
    norm = text.strip().strip(".!").lower()
    return norm in _EMPTY_PHRASES


def shape_output(text: str, fmt: str) -> dict:
    """Best-effort coercion of the agent's free-form text into the tile's declared
    shape. Never fails — falls back to raw markdown so a tile never goes blank.
    Every result carries `empty`: true when the agent ran but found nothing, so the
    tile can show its configured empty-state message instead of a blank body."""
    text = (text or "").strip()
    if not text:
        return {"format": "markdown", "value": "", "empty": True}
    if fmt == "list":
        lines = [ln.lstrip("-*• ").strip() for ln in text.splitlines() if ln.strip()]
        items = [ln for ln in lines if ln]
        if items:
            # A single-line "list" that's really just a no-results sentence.
            if len(items) == 1 and _is_empty_prose(items[0]):
                return {"format": "list", "value": [], "empty": True}
            return {"format": "list", "value": items, "empty": False}
    elif fmt == "metric":
        import re
        m = re.search(r"[-+]?\d[\d,]*\.?\d*%?", text)
        if m:
            value = m.group(0)
            label = (text[:m.start()] + text[m.end():]).strip(" -:.\n")
            empty = value.strip("-+.,") in ("", "0")
            return {"format": "metric", "value": {"value": value, "label": label or None}, "empty": empty}
    elif fmt == "table":
        rows = [ln for ln in text.splitlines() if ln.strip().startswith("|")]
        if len(rows) >= 2:
            def cells(row: str) -> list[str]:
                return [c.strip() for c in row.strip().strip("|").split("|")]
            header = cells(rows[0])
            body = [cells(r) for r in rows[2:]] if len(rows) > 2 else []
            return {"format": "table", "value": {"header": header, "rows": body}, "empty": len(body) == 0}
    elif fmt == "diff":
        lines = []
        for ln in text.splitlines():
            if not ln.strip() or ln.startswith(("---", "+++", "@@")):
                continue
            if ln.startswith("+"):
                lines.append({"kind": "add", "text": ln[1:].strip()})
            elif ln.startswith("-"):
                lines.append({"kind": "remove", "text": ln[1:].strip()})
            else:
                lines.append({"kind": "context", "text": ln.lstrip(" ").strip()})
        if lines:
            # A single context-only line ("No changes") reporting the null result.
            if len(lines) == 1 and lines[0]["kind"] == "context" and _is_empty_prose(lines[0]["text"]):
                return {"format": "diff", "value": [], "empty": True}
            return {"format": "diff", "value": lines, "empty": False}
    return {"format": fmt if fmt in ("summary", "markdown") else "markdown", "value": text,
            "empty": _is_empty_prose(text)}
