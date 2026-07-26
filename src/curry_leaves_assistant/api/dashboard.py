"""Dashboard tile boards: board/tile CRUD, layout, refresh, and AI-drafted tile config."""
from __future__ import annotations

import re
from typing import Literal, Optional

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from curry_leaves_assistant.agents import agent_engine
from curry_leaves_assistant.core import events
from curry_leaves_assistant.orchestration import dashboard_runner
from curry_leaves_assistant.stores import agent_store, dashboard_store

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/boards")
def list_boards():
    return dashboard_store.list_boards()


class CreateBoard(BaseModel):
    name: str = "Board"


@router.post("/dashboard/boards")
def create_board(body: CreateBoard):
    return dashboard_store.create_board(body.name)


@router.patch("/dashboard/boards/{board_id}")
async def patch_board(board_id: str, request: Request):
    body = await request.json()
    allowed = {k: body[k] for k in ("name", "order") if k in body}
    board = dashboard_store.update_board(board_id, **allowed) if allowed else dashboard_store.get_board(board_id)
    return board or Response(status_code=404)


@router.delete("/dashboard/boards/{board_id}")
def delete_board_route(board_id: str):
    return {"ok": dashboard_store.delete_board(board_id)}


@router.get("/dashboard/boards/{board_id}")
def get_board_route(board_id: str):
    return dashboard_store.get_board(board_id) or Response(status_code=404)


class AddTile(BaseModel):
    agentId: str
    title: str | None = None


@router.post("/dashboard/boards/{board_id}/tiles")
def add_tile(board_id: str, body: AddTile):
    if agent_store.read_agent(body.agentId) is None:
        return Response(status_code=400)
    tile = dashboard_store.add_tile(board_id, body.agentId, body.title)
    return tile or Response(status_code=404)


@router.patch("/dashboard/boards/{board_id}/tiles/{tile_id}")
async def patch_tile(board_id: str, tile_id: str, request: Request):
    body = await request.json()
    allowed = {k: body[k] for k in ("title", "layout", "config") if k in body}
    if not allowed:
        return Response(status_code=400)
    tile = dashboard_store.update_tile(board_id, tile_id, **allowed)
    return tile or Response(status_code=404)


@router.delete("/dashboard/boards/{board_id}/tiles/{tile_id}")
def delete_tile_route(board_id: str, tile_id: str):
    return {"ok": dashboard_store.delete_tile(board_id, tile_id)}


@router.patch("/dashboard/boards/{board_id}/layout")
async def patch_layout(board_id: str, request: Request):
    body = await request.json()
    layouts = body.get("layouts") or []
    board = dashboard_store.update_layout(board_id, layouts)
    return board or Response(status_code=404)


@router.post("/dashboard/boards/{board_id}/tiles/{tile_id}/refresh")
def refresh_tile(board_id: str, tile_id: str):
    """Manual refresh: submit an interactive-band tile WorkItem (fire-and-forget) and return
    its jobId. The tile updates itself when the run finishes — the client listens for
    tile.run.* over the WebSocket and refetches. Decoupled + scalable: no blocking request,
    and the refresh is a first-class WorkItem (shows on the agent page, respects the lane)."""
    import uuid
    job_id = dashboard_runner.submit_tile(
        board_id, tile_id, dedupe_key=f"tile.{tile_id}.manual.{uuid.uuid4().hex}",
        band=0)  # interactive band — a user clicked, so it jumps ahead of scheduled tiles
    return {"jobId": job_id}


class GenerateTileConfig(BaseModel):
    need: str  # plain-English: what the user wants this tile to show


class RefreshDraft(BaseModel):
    """AI-drafted refresh, only emitted when the need states a cadence or trigger."""

    mode: Literal["schedule", "event"]
    frequency: Optional[Literal["daily", "weekdays", "weekly"]] = Field(
        default=None, description="For mode=schedule. 'weekdays' = Monday-Friday only.")
    time: Optional[str] = Field(
        default=None, description="For mode=schedule: 24h HH:MM, e.g. '08:30'.")
    dayOfWeek: Optional[int] = Field(
        default=None, ge=0, le=6,
        description="For frequency=weekly: 0=Sunday .. 6=Saturday.")
    eventType: Optional[str] = Field(
        default=None, description="For mode=event: one of the listed event types, verbatim.")


class TileConfigDraft(BaseModel):
    title: str = Field(description="Short tile heading (2-5 words).")
    focus: str = Field(description=(
        "Plain-language brief telling the agent what to watch/report on for this tile "
        "(1-3 sentences, written as an instruction to the agent, e.g. 'Summarize unread "
        "emails tagged urgent')."))
    rules: str = Field(description=(
        "Optional constraints layered on top of focus (length, filters, tone) — empty string if none."))
    outputFormat: str = Field(description="Whichever listed shape best fits what the user asked to see.")
    markdownTemplate: Optional[str] = Field(
        default=None, description=(
            "ONLY when outputFormat='markdown': a concrete markdown skeleton — the exact "
            "'## ' headings the agent fills in each run, chosen to best fit the user's ask, "
            "with a short parenthesized placeholder or '- ' under each. Null for every "
            "other format."))
    emptyMessage: str = Field(description="Short message shown when there is nothing to report, fitting the topic.")
    alert: Optional[str] = Field(
        default=None, description=(
            "ONLY when the user asked to be notified/alerted (e.g. 'ping me if more than 3 "
            "are urgent'): the alert condition in plain language, e.g. 'Notify me if there "
            "are more than 3 urgent unread emails'. Null when the need doesn't ask for a "
            "notification."))
    refresh: Optional[RefreshDraft] = Field(
        default=None, description=(
            "ONLY when the user's need explicitly states when to run — a cadence ('every "
            "morning at 8', 'weekly on Monday') becomes mode=schedule; a trigger ('whenever "
            "a recording is transcribed') becomes mode=event. Null when the need says "
            "nothing about timing — never invent one."))


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validated_refresh(draft: RefreshDraft | None) -> dict | None:
    """Map a drafted refresh onto the tile's refresh config, or None when it doesn't
    hold up (bad time, unknown event type) — a dropped draft just leaves the tile
    manual, which is what the user reviews in the form anyway."""
    if draft is None:
        return None
    if draft.mode == "schedule":
        if not draft.frequency or not draft.time or not _TIME_RE.match(draft.time):
            return None
        from curry_leaves_assistant.core.schedule_spec import cron_from_frequency
        spec = cron_from_frequency(draft.frequency, draft.time, draft.dayOfWeek)
        if spec is None:
            return None
        return {"mode": "schedule", "schedule": spec}
    if draft.mode == "event" and draft.eventType in events.trigger_types():
        return {"mode": "event", "eventType": draft.eventType}
    return None


@router.post("/dashboard/boards/{board_id}/tiles/{tile_id}/generate-config")
async def generate_tile_config(board_id: str, tile_id: str, body: GenerateTileConfig):
    """Draft a tile's Focus/Rules/output shape — and, when the need states a cadence
    or trigger, its refresh schedule — from a plain-English need (uses the active
    LLM) — the AI half of the dashboard's AI/Manual tile setup choice. Scoped to the
    tile's already-bound agent so the brief fits what that agent can actually do.
    Structured via output_type (the generator is tool-less, so providers with native
    JSON mode use it); the kernel validates and retries, no regex scraping here."""
    board = dashboard_store.get_board(board_id)
    tile = next((t for t in (board or {}).get("tiles", []) if t["id"] == tile_id), None) if board else None
    if tile is None:
        return Response(status_code=404)
    agent = agent_store.read_agent(tile["agentId"])
    if agent is None:
        return Response(status_code=404)
    prompt = (
        f'An agent named "{agent["name"]}" (purpose: {agent.get("description") or "general assistant"}) is '
        f'bound to a dashboard tile. The user wants this tile to show: "{body.need}".\n\n'
        "Draft the tile's setup.\n"
        f"Valid outputFormat values: {list(dashboard_store.OUTPUT_FORMATS)}.\n"
        f"Valid event types for refresh.eventType: {events.trigger_types()}.\n"
        "Include refresh ONLY if the user's need says when to run (a time/cadence or a "
        "trigger event); otherwise set it to null.\n"
        "If you pick outputFormat 'markdown', also provide markdownTemplate — the exact "
        "heading skeleton that best fits the ask; otherwise set it to null.\n"
        "Include alert ONLY if the user asked to be notified about a condition; otherwise "
        "set it to null."
    )
    spec = {"id": "generator", "model": None, "tools": [],
            "instructions": "You draft dashboard tile configurations.", "description": ""}
    try:
        draft, _raw = await agent_engine.run_agent_structured(spec, prompt, TileConfigDraft)
    except Exception as e:
        return Response(content=f"Tile config generation failed: {e}", status_code=502)
    if draft is None:
        return Response(content="The AI provider returned no usable output — check the active provider/model in Settings.",
                         status_code=502)
    fmt = draft.outputFormat if draft.outputFormat in dashboard_store.OUTPUT_FORMATS else "summary"
    return {
        "title": draft.title.strip() or tile["title"],
        "focus": draft.focus.strip(),
        "rules": draft.rules.strip(),
        "outputFormat": fmt,
        # Cleared (not preserved) for non-markdown formats, so a leftover skeleton from
        # a previous markdown setup can't silently ride along with the new shape.
        "markdownTemplate": (draft.markdownTemplate or "").strip() if fmt == "markdown" else "",
        "emptyMessage": draft.emptyMessage.strip(),
        "alert": (draft.alert or "").strip(),
        "refresh": _validated_refresh(draft.refresh),
    }
