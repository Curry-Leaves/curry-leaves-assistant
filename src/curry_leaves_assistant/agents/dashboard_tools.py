"""Dashboard tool for agents: inspect boards and pin/modify a recurring ask on one as
a tile. This is how chat ties into the dashboard — when a user asks for data on a
schedule ("every morning show me…"), the chat agent creates the tile itself via the
`dashboard` tool's add_tile action instead of telling the user to configure one by hand.
The dashboard-tiles skill documents when and how to reach for these.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field
from curry_leaves.core.tools import ToolResult

from curry_leaves_assistant.core import events
from curry_leaves_assistant.stores import agent_store, dashboard_store

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _err(msg: str) -> ToolResult:
    return ToolResult(content=msg, is_error=True)


def _build_refresh(mode, frequency, time, day_of_week, event_type) -> dict | str:
    """Validate refresh args into a refresh config dict, or return an error string. The
    ergonomic frequency/time/day_of_week inputs are translated into the unified ScheduleSpec
    (a cron expression) that the scheduler consumes. Shared by add_tile and update_tile
    (callers only reach here with a non-None mode)."""
    if mode == "manual":
        return {"mode": "manual"}
    if mode == "schedule":
        if not frequency or not time:
            return "refresh_mode='schedule' needs frequency and time"
        if not _TIME_RE.match(time):
            return f"time must be 24h HH:MM, got {time!r}"
        if frequency == "weekly" and day_of_week is None:
            return "frequency='weekly' needs day_of_week (0=Sunday .. 6=Saturday)"
        from curry_leaves_assistant.core.schedule_spec import cron_from_frequency
        spec = cron_from_frequency(frequency, time, day_of_week)
        if spec is None:
            return f"invalid schedule: frequency={frequency!r} time={time!r} day_of_week={day_of_week!r}"
        return {"mode": "schedule", "schedule": spec}
    valid = events.trigger_types()
    if event_type not in valid:
        return f"event_type must be one of {valid}, got {event_type!r}"
    return {"mode": "event", "eventType": event_type}


def _list_boards() -> ToolResult:
    """Read-only: boards and their recurring tiles. Shared by the read-only `dashboard_read`
    tool and (historically) the write tool's list action, which now lives in dashboard_read."""
    out = []
    for entry in dashboard_store.list_boards():
        board = dashboard_store.get_board(entry["id"]) or {}
        out.append({
            "board": board.get("name", entry.get("name", "")),
            "boardId": board.get("id", entry.get("id", "")),
            "tiles": [{
                "tileId": t.get("id", ""),
                "title": t.get("title", ""),
                "agentId": t.get("agentId", ""),
                "outputFormat": (t.get("config") or {}).get("outputFormat", ""),
                "focus": (t.get("config") or {}).get("focus", ""),
                "refresh": (t.get("config") or {}).get("refresh") or {"mode": "manual"},
            } for t in board.get("tiles", [])],
        })
    if not out:
        return ToolResult(content="No boards yet — add_tile will create one.")
    # boardId/tileId are what update_tile addresses — always list first to get them,
    # so you MODIFY the existing tile instead of adding a duplicate.
    return ToolResult(content=json.dumps(out, ensure_ascii=False, indent=1))


class DashboardReadTool:
    """Read side of the dashboard, action-dispatched: list boards and their recurring tiles.
    Read-only, so it never prompts for permission. Pinning/modifying tiles (add_tile,
    update_tile) lives in the separate `dashboard` write tool."""
    name = "dashboard_read"
    description = (
        "Read the user's DASHBOARD — action: list_boards. `list_boards` returns boards and "
        "the recurring tiles on each (tileId, title, bound agent, output format, refresh) — "
        "NOT artifacts (use `artifacts_read`) or KB notes (use `kb_read`). List first to get "
        "boardId/tileId, then add or modify tiles with the `dashboard` tool."
    )
    risk = "read"

    class Args(BaseModel):
        action: Literal["list_boards"] = Field(default="list_boards", description="list_boards")

    schema = Args
    timeout = None

    async def run(self, args: "DashboardReadTool.Args", ctx, signal) -> ToolResult:
        return _list_boards()


class DashboardTool:
    """One tool for the dashboard, action-dispatched: add_tile | update_tile. A tile pins a
    recurring ask (bound agent + focus brief + output shape + refresh) onto a board. Always
    list_boards (via `dashboard_read`) first to get boardId/tileId, so an edit MODIFIES the
    existing tile via update_tile instead of adding a duplicate."""
    name = "dashboard"
    description = (
        "Modify the user's DASHBOARD — action: add_tile | update_tile. To LIST boards/tiles "
        "use `dashboard_read` (list_boards lives there); list first to get boardId/tileId "
        "before an update, so you don't add a duplicate.\n"
        "• add_tile: pin a NEW recurring ask (bind an agent, focus brief, output shape, "
        "usually a refresh schedule). Use when the user wants data regularly ('every "
        "morning…', 'keep an eye on…') — not for one-off questions. First refresh runs "
        "immediately.\n"
        "• update_tile: MODIFY an existing tile in place (output format, focus, title, style, "
        "refresh, …) — use WHENEVER the user changes a tile they already have; pass boardId + "
        "tileId + only the fields you're changing. Re-runs the tile after the change."
    )
    risk = "write"

    class Args(BaseModel):
        action: Literal["add_tile", "update_tile"] = Field(
            description="add_tile | update_tile")
        # add_tile / update_tile addressing
        board: Optional[str] = Field(default=None, description=(
            "add_tile: board NAME to add to (omit for the default board; an unknown name "
            "creates that board)."))
        board_id: Optional[str] = Field(default=None, description="update_tile: boardId from list_boards.")
        tile_id: Optional[str] = Field(default=None, description="update_tile: tileId from list_boards.")
        agent_id: str = Field(default="dashboard-watcher", description=(
            "add_tile: agent to bind (default: the read-only dashboard-watcher). Override "
            "only with an id from the user's roster."))
        # tile config (add: title/focus required; update: all optional patch fields)
        title: Optional[str] = Field(default=None, description="Tile heading (2-5 words). add_tile: required. update_tile: omit to keep.")
        focus: Optional[str] = Field(default=None, description=(
            "Plain-language brief of what the tile watches/reports each run. add_tile: "
            "required. update_tile: omit to keep."))
        rules: Optional[str] = Field(default=None, description="Extra constraints on top of focus (length, filters, tone). update_tile: omit to keep.")
        output_format: Optional[Literal["summary", "list", "metric", "table", "markdown", "diff"]] = Field(
            default=None, description="Output shape ('table' is the grid). add_tile defaults to 'summary'. update_tile: omit to keep.")
        markdown_template: Optional[str] = Field(
            default=None, description="For output_format='markdown': the '## ' heading skeleton the agent fills each run.")
        empty_message: Optional[str] = Field(default=None, description="Shown when a run finds nothing to report.")
        style: Optional[Literal["card", "flat", "outlined", "accent-bar", "glass", "glow", "gradient-edge"]] = Field(
            default=None, description="update_tile: visual style (omit to keep).")
        alert: Optional[str] = Field(default=None, description="Only if the user asked to be notified: the alert condition in plain language.")
        # refresh
        refresh_mode: Optional[Literal["manual", "schedule", "event"]] = Field(
            default=None, description="When the tile re-runs. add_tile defaults to 'manual'; use 'schedule' when the user gave a cadence. update_tile: omit to leave unchanged.")
        frequency: Optional[Literal["daily", "weekdays", "weekly"]] = Field(
            default=None, description="For refresh_mode='schedule'. 'weekdays' = Mon-Fri.")
        time: Optional[str] = Field(default=None, description="For refresh_mode='schedule': 24h HH:MM, e.g. '08:00'.")
        day_of_week: Optional[int] = Field(default=None, ge=0, le=6,
                                           description="For frequency='weekly': 0=Sunday .. 6=Saturday.")
        event_type: Optional[str] = Field(default=None, description=(
            "For refresh_mode='event': the triggering event type (e.g. 'recording.transcribed')."))
        rerun: bool = Field(default=True, description="update_tile: re-run the tile after the change (default true).")

    schema = Args
    timeout = None

    async def run(self, args: "DashboardTool.Args", ctx, signal) -> ToolResult:
        if args.action == "add_tile":
            return await self._add_tile(args)
        return await self._update_tile(args)

    async def _add_tile(self, args: "DashboardTool.Args") -> ToolResult:
        if not args.title or not args.focus or not args.focus.strip():
            return _err("add_tile requires `title` and a non-empty `focus`.")
        if agent_store.read_agent(args.agent_id) is None:
            return _err(f"No agent {args.agent_id!r} — use an id from the user's roster.")
        refresh = _build_refresh(args.refresh_mode or "manual", args.frequency, args.time,
                                 args.day_of_week, args.event_type)
        if isinstance(refresh, str):
            return _err(refresh)

        wanted = (args.board or "").strip()
        if wanted:
            match = next((b for b in dashboard_store.list_boards()
                          if b["name"].strip().lower() == wanted.lower()), None)
            board = dashboard_store.get_board(match["id"]) if match else dashboard_store.create_board(wanted)
        else:
            board = dashboard_store.ensure_default_board()
        if board is None:
            return _err("Could not resolve a board.")

        output_format = args.output_format or "summary"
        tile = dashboard_store.add_tile(board["id"], args.agent_id, args.title.strip() or None)
        if tile is None:
            return _err("Could not add the tile.")
        dashboard_store.update_tile(board["id"], tile["id"], config={
            "focus": args.focus.strip(),
            "rules": (args.rules or "").strip(),
            "outputFormat": output_format,
            "markdownTemplate": (args.markdown_template or "").strip() if output_format == "markdown" else "",
            "emptyMessage": (args.empty_message or "").strip(),
            "alert": (args.alert or "").strip(),
            "refresh": refresh,
        })
        # Populate the tile right away so the user doesn't land on "Not run yet". Lazy
        # import: dashboard_runner → agent_engine → agent_tools → (this module) would
        # otherwise be a cycle at import time.
        from curry_leaves_assistant.orchestration import dashboard_runner
        asyncio.create_task(dashboard_runner.run_tile(board["id"], tile["id"]))

        rmode = args.refresh_mode or "manual"
        when = {"manual": "on manual refresh", "schedule": f"on schedule ({args.frequency} at {args.time})",
                "event": f"on event ({args.event_type})"}[rmode]
        return ToolResult(content=(
            f"Added tile '{tile['title']}' to board '{board['name']}' (agent {args.agent_id}, "
            f"{output_format}, refreshes {when}). Its first refresh is running now — "
            "tell the user to check the Dashboard tab and that they can tweak the tile via "
            "its ⋮ → Configure menu."))

    async def _update_tile(self, args: "DashboardTool.Args") -> ToolResult:
        if not args.board_id or not args.tile_id:
            return _err("update_tile requires `board_id` and `tile_id` (from list_boards).")
        board = dashboard_store.get_board(args.board_id)
        if board is None:
            return _err(f"No board {args.board_id!r} — use list_boards for valid ids.")
        tile = next((t for t in board.get("tiles", []) if t["id"] == args.tile_id), None)
        if tile is None:
            return _err(f"No tile {args.tile_id!r} on that board — use list_boards for valid ids.")

        # Partial config patch from only the fields the caller set — update_tile merges
        # into the existing config, so unspecified fields are untouched.
        cfg: dict = {}
        if args.focus is not None:
            cfg["focus"] = args.focus.strip()
        if args.rules is not None:
            cfg["rules"] = args.rules.strip()
        if args.output_format is not None:
            cfg["outputFormat"] = args.output_format
        if args.markdown_template is not None:
            cfg["markdownTemplate"] = args.markdown_template.strip()
        if args.empty_message is not None:
            cfg["emptyMessage"] = args.empty_message.strip()
        if args.style is not None:
            cfg["style"] = args.style
        if args.alert is not None:
            cfg["alert"] = args.alert.strip()
        if args.refresh_mode is not None:
            refresh = _build_refresh(args.refresh_mode, args.frequency, args.time,
                                     args.day_of_week, args.event_type)
            if isinstance(refresh, str):
                return _err(refresh)
            cfg["refresh"] = refresh

        patch: dict = {}
        if args.title is not None:
            patch["title"] = args.title.strip() or tile["title"]
        if cfg:
            patch["config"] = cfg
        if not patch:
            return _err("Nothing to update — pass at least one field to change.")

        updated = dashboard_store.update_tile(args.board_id, args.tile_id, **patch)
        if updated is None:
            return _err("Could not update the tile.")

        changed = ", ".join(sorted(cfg.keys()) + (["title"] if "title" in patch else [])) or "(nothing)"
        if args.rerun:
            from curry_leaves_assistant.orchestration import dashboard_runner
            asyncio.create_task(dashboard_runner.run_tile(args.board_id, args.tile_id))
        return ToolResult(content=(
            f"Updated tile '{updated['title']}' (changed: {changed})."
            + (" Re-running it now — tell the user to check the Dashboard tab." if args.rerun else "")))


DASHBOARD_TOOLS = [DashboardReadTool(), DashboardTool()]
