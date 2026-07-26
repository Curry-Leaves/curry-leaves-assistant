"""Runs one dashboard tile: build the task text from its brief, run the bound agent
via the same path an ordinary agent run takes, shape the result, and persist it back
onto the tile. Mirrors pool.py's run step but scoped to a single (board, tile) pair
instead of the durable job queue — a tile refresh is short-lived and idempotent by
nature (re-running just overwrites the cached output).

Also owns tile triggering: schedule-mode tiles register as a ScheduleSource with the unified
scheduler (orchestration.schedule) and fire on their ScheduleSpec; event-mode tiles fire via
an events.on_event handler when their subscribed event type occurs. This module no longer runs
its own poll loop.
"""
from __future__ import annotations

import asyncio
import uuid

from curry_leaves_assistant.agents import tile_shapes

from curry_leaves_assistant.stores import agent_store

from curry_leaves_assistant.stores import dashboard_store

from curry_leaves_assistant.core import events

from curry_leaves_assistant.core.store import now_iso


async def run_tile(board_id: str, tile_id: str, job_id: str | None = None) -> dict | None:
    """Run one tile as a STREAMING agent run (chat parity: the runs page can watch it live and
    steer/stop it). `job_id` is the Work Kernel job id so the runs page attaches to the right
    channel; a direct/legacy call mints one. Streams via launch_stream_run; the tile's output is
    parsed + shaped in on_terminal (structured → to_wire()/shape_output fallback). Awaits the run
    to completion so the caller (the worker) holds its lane slot for the tile's lifetime."""
    board = dashboard_store.get_board(board_id)
    if board is None:
        return None
    tile = next((t for t in board["tiles"] if t["id"] == tile_id), None)
    if tile is None:
        return None
    agent = agent_store.read_agent(tile["agentId"])
    if agent is None:
        dashboard_store.set_tile_state(board_id, tile_id, lastRunStatus="error", lastRunAt=now_iso())
        return None

    job_id = job_id or uuid.uuid4().hex
    dashboard_store.set_tile_state(board_id, tile_id, lastRunStatus="running", lastJobId=job_id)
    events.emit("tile.run.started", payload={"boardId": board_id, "tileId": tile_id, "agentId": agent["id"]},
                entity_id=tile_id, label=tile["title"])

    task_text = dashboard_store.build_task_text(tile)
    extra_tools = None
    alert = (tile["config"].get("alert") or "").strip()
    if alert:
        # A fresh notify_user tool instance, scoped to this tile via closure — only this run's
        # Agent gets it, so concurrent tile runs can't race on a shared registry entry.
        from curry_leaves_assistant.agents.agent_tools import NotifyUserTool
        extra_tools = [NotifyUserTool(board_id, tile_id, tile["title"])]
    fmt = tile["config"].get("outputFormat", "summary")
    output_type = tile_shapes.OUTPUT_TYPES.get(fmt)
    from curry_leaves_assistant.stores import trace_store
    trace_id, span_id = f"tr_{job_id}", f"spj_{job_id}"
    trace_store.ensure_root(trace_id, span_id, name=agent["name"], kind="session",
                            attributes={"agentId": agent["id"], "jobId": job_id, "tileId": tile_id})

    result_box: dict = {"shaped": None}
    done = asyncio.get_event_loop().create_future()

    def _finalize(status: str, output_text: str, error: str | None) -> None:
        from curry_leaves_assistant.core.store import write_json
        from curry_leaves_assistant.core.paths import agent_runs_dir
        if error:
            dashboard_store.set_tile_state(board_id, tile_id, lastRunAt=now_iso(), lastRunStatus="error")
            agent_store.update_meta(agent["id"], lastRunAt=now_iso(), lastRunStatus="error")
            events.emit("tile.run.failed",
                        payload={"boardId": board_id, "tileId": tile_id, "agentId": agent["id"], "error": error},
                        entity_id=tile_id, label=tile["title"])
            rec_status = "failed"
        else:
            # Parse the final streamed text. Structured → validate to the shape, else fall back
            # to shape_output (the same graceful degrade the run-to-completion path used).
            if output_type is not None:
                parsed = None
                try:
                    parsed = output_type.model_validate_json(output_text)
                except Exception:
                    parsed = None
                shaped = parsed.to_wire() if parsed is not None else dashboard_store.shape_output(output_text, fmt)
            else:
                shaped = dashboard_store.shape_output(output_text, fmt)
            result_box["shaped"] = shaped
            dashboard_store.set_tile_state(board_id, tile_id, lastOutput=shaped,
                                           lastRunAt=now_iso(), lastRunStatus="success")
            agent_store.update_meta(agent["id"], lastRunAt=now_iso(), lastRunStatus="success")
            events.emit("tile.run.completed",
                        payload={"boardId": board_id, "tileId": tile_id, "agentId": agent["id"], "output": shaped},
                        entity_id=tile_id, label=tile["title"])
            rec_status = "done"
        # Run record → the tile run shows in the bound agent's history (Agents tab).
        rec = {"id": job_id, "agentId": agent["id"], "kind": "tile", "status": rec_status,
               "traceId": trace_id, "finishedAt": now_iso(), "output": output_text, "error": error,
               "trigger": {"type": "tile.refresh", "payload": {"boardId": board_id, "tileId": tile_id}}}
        try:
            write_json(agent_runs_dir(agent["id"]) / f"{job_id}.json", rec)
        except Exception:
            pass
        if not done.done():
            done.set_result(True)

    from curry_leaves_assistant.agents import chat_runs
    chat_runs.launch_stream_run(
        job_id, agent, task_text, session_id=f"run_{job_id}", autonomous=True,
        surface="dashboard", extra_tools=extra_tools, output_type=output_type,
        activate_trace=(trace_id, span_id), on_terminal=_finalize)

    await done
    return result_box["shaped"]


# ─── submit a tile to the Work Kernel (scheduler, events, and manual refresh) ─
def submit_tile(board_id: str, tile_id: str, dedupe_key: str, band: int) -> str:
    """Hand a tile to the Work Kernel as a background job in the 'tiles' lane (capped width →
    no stampede), gaining dead-letter, recovery, tracing, and a spot in the agent's run list.
    Returns the job id. Every refresh path — schedule, event, and the manual button — goes
    through here, so a tile refresh is always a real WorkItem."""
    from curry_leaves_assistant.orchestration import work
    from curry_leaves_assistant.orchestration.work import WorkItem
    return work.submit(WorkItem(
        kind="tile",
        trigger={"type": "tile.refresh", "payload": {"boardId": board_id, "tileId": tile_id}},
        mode="background", lane="tiles", band=band, autonomy="auto",
        dedupe_key=dedupe_key,
    ))


# Back-compat internal alias for the scheduler/event callers below.
_submit_tile = submit_tile


def _handle_event(event: dict) -> None:
    """Sync trigger handler: submit any tile whose refresh mode subscribes to this event."""
    if event["type"].startswith("tile.run.") or event["type"].startswith("agent."):
        return
    for board_id, tile in dashboard_store.all_tiles():
        refresh = tile["config"].get("refresh") or {}
        if refresh.get("mode") == "event" and refresh.get("eventType") == event["type"]:
            _submit_tile(board_id, tile["id"], dedupe_key=f"tile.{tile['id']}.{event.get('id')}", band=1)


# ─── schedule-mode tiles as a ScheduleSource ──────────────────────────────────
from curry_leaves_assistant.orchestration import schedule
from curry_leaves_assistant.orchestration.schedule import ScheduledJob


class _TileScheduleSource:
    """Every schedule-mode tile becomes one scheduled job, firing at BAND_BACKGROUND (2) into
    the capped 'tiles' lane. The per-minute dedupe key is gone — the unified scheduler's
    next-run state is the once-per-window guard now — but kernel idempotency on the job id
    still guards a same-instant double submit."""
    name = "tiles"

    def jobs(self) -> list[ScheduledJob]:
        out: list[ScheduledJob] = []
        for board_id, tile in dashboard_store.all_tiles():
            refresh = tile["config"].get("refresh") or {}
            if refresh.get("mode") != "schedule":
                continue
            spec = refresh.get("schedule") or {"kind": "none"}
            if spec.get("kind", "none") == "none":
                continue
            out.append(ScheduledJob(
                key=f"tile:{board_id}:{tile['id']}",
                spec=spec,
                fire=(lambda b=board_id, t=tile: _submit_tile(
                    b, t["id"], dedupe_key=f"tile.{t['id']}.{now_iso()}", band=2)),
            ))
        return out


def start() -> None:
    """Register the tile event handler and the schedule source. The unified scheduler
    (orchestration.schedule) owns the tick loop; due tiles are submitted to the Work Kernel."""
    events.on_event(_handle_event)
    schedule.register(_TileScheduleSource())
