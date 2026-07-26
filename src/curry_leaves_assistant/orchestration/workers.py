"""Workers — claim a job from the LaneScheduler, run it, record the result.

Ported from the old pool's `_run_job` with the same durability contract:
  atomic claim (rename pending→running) · run · write the run record FIRST, then clear the
  claimed file · dead-letter after MAX_ATTEMPTS crashes · recovery on boot.

Dispatch is by `kind`: "agent" → agent_engine.run_agent (with a per-run host + permission
chosen by the job's `autonomy`); "tile" → dashboard_runner.run_tile. On any terminal state
the job's completion Future is resolved (the join primitive workflows await).
"""
from __future__ import annotations

import asyncio
import os

from curry_leaves_assistant.core import events
from curry_leaves_assistant.core.paths import QUEUE_DIR
from curry_leaves_assistant.core.store import now_iso, read_json, write_json
from curry_leaves_assistant.orchestration import work
from curry_leaves_assistant.orchestration.scheduler import MAX_CONCURRENCY, scheduler
from curry_leaves_assistant.stores import agent_store, pool_store, trace_store

MAX_ATTEMPTS = int(os.environ.get("CURRY_LEAVES_MAX_ATTEMPTS", "3"))
RUNS_KEEP = int(os.environ.get("CURRY_LEAVES_RUNS_KEEP", "200"))
_N_WORKERS = MAX_CONCURRENCY


# ─── episodic memory + learning signals (post-run, mechanical) ────────────────
def _record_episode(agent_id: str, job_id: str, job: dict, trace_id: str) -> None:
    """Derive a mechanical run summary from the finished run + its trace, write its STATS ROW
    (steps/outcome/shape — no memory note), then let the learning-signal detectors inspect it
    (they may emit learn.* events that wake the Skill Learner). What becomes durable MEMORY is
    not this — it's what the nightly Memory Keeper distils from conversations. Fails soft: a
    hiccup here must never affect the run's outcome."""
    try:
        from curry_leaves_assistant.stores import episode_store, trace_store
        from curry_leaves_assistant.orchestration import learn_signals

        spans = trace_store.get_trace(trace_id) or []
        episode = episode_store.summarize(job, spans)
        episode_store.record(episode)
        # Close the measurement loop: credit/debit any learned skill this run actually LOADED
        # with the run's outcome, so the nightly sweep can promote the ones that help and retire
        # the ones that don't. (No-op for seeded skills, which carry no metrics.)
        loaded = episode_store.loaded_skills(spans)
        if loaded:
            from curry_leaves_assistant.stores import skill_meta
            skill_meta.record_run(loaded, success=episode.get("outcome") == "done")
        learn_signals.inspect(episode)
    except Exception as exc:   # pragma: no cover - defensive
        print(f"[episode] skipped for {agent_id}/{job_id}: {exc}", flush=True)


# ─── run-record retention (mirrors trace_store.prune) ─────────────────────────
def _prune_runs(agent_id: str, keep: int = RUNS_KEEP) -> None:
    from curry_leaves_assistant.core.paths import agent_runs_dir
    d = agent_runs_dir(agent_id)
    if not d.is_dir():
        return
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[keep:]:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


# ─── lifecycle ────────────────────────────────────────────────────────────────
# Strong refs to the worker fleet. Without this, asyncio holds only a weak ref to each
# create_task result and a worker could be GC'd; it also gives shutdown something to cancel.
_workers: set[asyncio.Task] = set()


def start() -> None:
    """Wire submit→scheduler, recover interrupted jobs, spawn the worker fleet."""
    work.register_enqueue(_on_submit)
    _recover_pending()
    for _ in range(_N_WORKERS):
        t = asyncio.create_task(_worker())
        _workers.add(t)
        t.add_done_callback(_workers.discard)
    print("[workers] started", flush=True)


async def stop() -> None:
    """Cancel the worker fleet and wait for them to unwind. In-flight jobs left as *.running
    residue are recovered on the next boot (the durability contract is unchanged)."""
    tasks = list(_workers)
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _on_submit(job_id: str) -> None:
    """submit() calls this (on the loop) for a freshly-written background job → hand its
    (lane, band) to the scheduler."""
    job = read_json(work.queue_file(job_id), None)
    if job:
        scheduler.add(job_id, job.get("lane", "general"), int(job.get("band", 1)))


def _recover_pending() -> None:
    """Crash recovery. Unstarted jobs (*.json) → re-add. Jobs interrupted mid-run
    (*.json.running) → count the interruption; retry until MAX_ATTEMPTS, then dead-letter."""
    for qf in QUEUE_DIR.glob("*.json"):
        job = read_json(qf, None)
        if job:
            scheduler.add(job["id"], job.get("lane", "general"), int(job.get("band", 1)))
    for rf in QUEUE_DIR.glob("*.json.running"):
        job = read_json(rf, None)
        if not job:
            rf.unlink(missing_ok=True)
            continue
        agent_id = job.get("agentId")
        if agent_id and work.run_record(agent_id, job["id"]).exists():
            rf.unlink(missing_ok=True)            # already completed → drop residue
            continue
        job["attempts"] = int(job.get("attempts", 0)) + 1
        if job["attempts"] >= MAX_ATTEMPTS:
            _dead_letter(job, rf, reason="crashed the worker repeatedly")
        else:
            write_json(rf, job)
            os.replace(rf, work.queue_file(job["id"]))
            scheduler.add(job["id"], job.get("lane", "general"), int(job.get("band", 1)))


def _dead_letter(job: dict, src, reason: str) -> None:
    """Quarantine a poison job to dead/ + emit a visible event. Best-effort — never raises."""
    job.update(status="dead", deadAt=now_iso(), deadReason=reason)
    dead = work.dead_file(job["id"])
    try:
        dead.parent.mkdir(parents=True, exist_ok=True)
        write_json(dead, job)
        try:
            src.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception as exc:
        print(f"[workers] dead-letter failed for {job.get('id')}: {exc}", flush=True)
    events.emit("agent.job.dead",
                payload={"jobId": job.get("id"), "agentId": job.get("agentId"),
                         "attempts": job.get("attempts"), "reason": reason},
                entity_id=job.get("agentId"), label=f"job dead: {reason}")
    work.resolve_completion(job)
    work.broadcast_kernel()   # job quarantined → refresh the Work panel (now in `dead`)


# ─── worker loop ────────────────────────────────────────────────────────────
async def _worker() -> None:
    while True:
        job_id, lane = await scheduler.next_runnable()   # blocks until lane+global capacity
        try:
            await _run_job(job_id)
        finally:
            # Release the SAME lane the scheduler acquired. Never re-derive it from the job
            # file — a failed claim/read would mislabel the lane and permanently leak the real
            # lane's capacity (starving that lane until restart).
            await scheduler.finish(lane)           # free capacity, wake waiters


async def _run_job(job_id: str) -> None:
    """Claim, run, record. Capacity is freed by the worker using the scheduler-supplied lane."""
    running = work.running_file(job_id)
    # Atomic claim: rename pending → running. Winner owns it; anyone else gets an error.
    try:
        os.replace(work.queue_file(job_id), running)
    except (FileNotFoundError, OSError):
        return  # already claimed/completed/gone
    job = read_json(running, None)
    if job is None:
        running.unlink(missing_ok=True)
        return

    if job.get("kind") == "tile":
        await _run_tile_job(job, running)
        return

    # ─── agent job ─────────────────────────────────────────────────────────
    agent = agent_store.read_agent(job["agentId"])
    if agent is None:
        running.unlink(missing_ok=True)
        return
    if work.run_record(agent["id"], job_id).exists():
        running.unlink(missing_ok=True)   # already finished (duplicate enqueue)
        return

    job["status"] = "running"
    job["startedAt"] = now_iso()
    write_json(running, job)
    work.broadcast_kernel()   # job moved queued → running: refresh the live Work panel
    _corr = job.get("correlationId")
    events.emit("agent.run.started",
                payload={"jobId": job_id, "agentId": agent["id"], "correlationId": _corr},
                entity_id=agent["id"], label=agent["name"])

    trig = job.get("trigger") or {}
    user_input = _build_input(agent, trig)
    trace_id = trig.get("traceId") or f"tr_{job_id}"
    span_id = trig.get("spanId") or f"spj_{job_id}"
    if not trig.get("traceId"):
        trace_store.ensure_root(trace_id, span_id, name=agent["name"], kind="session",
                                attributes={"agentId": agent["id"], "jobId": job_id})
    job["traceId"] = trace_id

    # Stream the run through the shared engine (chat parity: live frames + steer/stop + live
    # approvals), keyed by job_id so the runs page attaches to `chat:<job_id>`. The worker
    # holds its lane slot until the run finishes — `done` is set from on_terminal.
    done = asyncio.get_event_loop().create_future()

    # A background run gets a stable session (run_<jobId>) so its transcript persists and the
    # runs page can keep chatting after it finishes. A trigger may pin its own sessionId so a
    # series of jobs CONTINUES one conversation. Computed here so _finalize can hand it to
    # pool_store.complete → the todo write-back links back to this exact conversation.
    session_id = trig.get("sessionId") or f"run_{job_id}"

    def _finalize(status: str, output: str, error: str | None) -> None:
        job.update(status=("done" if status in ("done", "stopped") else "failed"),
                   finishedAt=now_iso(), output=output)
        if error:
            job["error"] = error
        agent_store.update_meta(agent["id"], lastRunAt=now_iso(),
                                lastRunStatus="error" if error else "success")
        if error:
            events.emit("agent.run.failed",
                        payload={"jobId": job_id, "agentId": agent["id"], "error": error, "correlationId": _corr},
                        entity_id=agent["id"], label=agent["name"])
        else:
            events.emit("agent.run.completed",
                        payload={"jobId": job_id, "agentId": agent["id"], "output": output, "correlationId": _corr},
                        entity_id=agent["id"], label=agent["name"])
        pool_item = (trig.get("payload") or {}).get("poolItemId")
        if pool_item and not error:
            pool_store.complete(pool_item, result=output, by=agent["id"], session_id=session_id)
        write_json(work.run_record(agent["id"], job_id), job)   # source of truth
        _prune_runs(agent["id"])
        _record_episode(agent["id"], job_id, job, trace_id)     # episodic memory + learning signals
        work.resolve_completion(job)
        # Remove the .running file BEFORE broadcasting: kernel_snapshot() reads the queue dir
        # from disk, so if the file still exists the broadcast reports this finished job as
        # still running — and since no later broadcast fires, the client's Work panel / office
        # stays stuck showing it as running (person never returns to the pool). Unlinking first
        # makes this snapshot authoritative. The lane slot is still held until `await done`.
        running.unlink(missing_ok=True)
        work.broadcast_kernel()   # job left the queue (done/failed): refresh the Work panel
        if not done.done():
            done.set_result(True)

    from curry_leaves_assistant.agents import chat_runs
    # session_id (run_<jobId>) was computed above _finalize so the todo write-back can link to
    # this conversation; the runs page keeps chatting on it after the run finishes.
    # The pool item this run concerns, when there is one: an assigned run carries
    # poolItemId; the Lead's triage run was woken by pool.item.created, whose payload IS
    # the item. Persisted with any ask the run makes, so the desk pins the question to
    # the right ask row.
    trig_payload = trig.get("payload") or {}
    pool_item_id = trig_payload.get("poolItemId") or (
        trig_payload.get("id") if trig.get("type") == "pool.item.created" else None)
    chat_runs.launch_stream_run(
        job_id, agent, user_input, model=None, session_id=session_id,
        autonomous=(job.get("autonomy") or "auto") == "auto",
        thinking_effort=None, surface="pool",
        activate_trace=(trace_id, span_id), on_terminal=_finalize,
        lane=job.get("lane") or "general", pool_item_id=pool_item_id)

    await done   # hold the lane slot until the streaming run reaches a terminal state
    running.unlink(missing_ok=True)
    if trig.get("type") == "recording.transcribed":
        _maybe_complete_outputs(trig)


# Host/permission are now built inside chat_runs.launch_stream_run (one streaming engine for
# chat + background). autonomy: "auto" → auto-approve; "ask" → a live approval card streams to
# the runs page, answered via run.respond (no separate SuspendHost path for streaming runs).


async def _run_tile_job(job: dict, running) -> None:
    """Run a dashboard tile as a pool job. run_tile streams the tile's agent (job['id'] is the
    run id the runs page attaches to), writes its own tile state + run record + tile.run.*
    events, and awaits completion — so the worker's lane slot is held for the tile's lifetime."""
    from curry_leaves_assistant.orchestration import dashboard_runner
    p = job.get("trigger", {}).get("payload") or {}
    board_id, tile_id = p.get("boardId") or "", p.get("tileId") or ""
    job["status"] = "running"
    job["startedAt"] = now_iso()
    write_json(running, job)
    work.broadcast_kernel()   # tile job queued → running
    try:
        await dashboard_runner.run_tile(board_id, tile_id, job_id=job["id"])
        job.update(status="done", finishedAt=now_iso())
    except Exception as exc:  # run_tile shouldn't raise, but never let a worker die
        job.update(status="failed", finishedAt=now_iso(), error=f"{type(exc).__name__}: {exc}")
    running.unlink(missing_ok=True)
    work.resolve_completion(job)
    work.broadcast_kernel()   # tile job left the queue


# ─── recording fan-in barrier (unchanged from the old pool) ───────────────────
def _agent_bound(agent: dict, event: dict) -> bool:
    if agent.get("always"):
        return True
    agent_ids = (event.get("payload") or {}).get("agentIds")
    return agent_ids is None or agent["id"] in agent_ids


def _maybe_complete_outputs(trig: dict) -> None:
    """Once every agent bound to a recording has a run record for THIS trigger, emit
    recording.outputs.completed so the Knowledge Filer files the full artifact set —
    UNLESS the user turned off "save to knowledge hub" for this recording."""
    p = trig.get("payload") or {}
    rec_id = p.get("id")
    if not rec_id:
        return
    # Honor the per-recording toggle. It's read from the LIVE meta (not the trigger payload,
    # which snapshots the recording at transcription time and would miss a later flip). Only a
    # value that is explicitly False opts out — absent/None defaults to filing, matching the
    # recording's own `saveToKnowledge: True` default.
    from curry_leaves_assistant.domain import recordings
    meta = recordings.get(rec_id) or {}
    if meta.get("saveToKnowledge") is False:
        return  # user opted this recording out of the knowledge hub — don't wake the filer
    bound = [a for a in agent_store.agents_for_trigger("recording.transcribed")
             if _agent_bound(a, trig)]
    if not bound:
        return
    ev_id = trig.get("id") or ""
    for agent in bound:
        from curry_leaves_assistant.orchestration.work import _SAFE
        job_id = _SAFE.sub("_", f"{ev_id}__{agent['id']}")[:180]
        if not work.run_record(agent["id"], job_id).exists():
            return  # still waiting on at least one bound agent
    events.emit("recording.outputs.completed", payload=p, entity_id=rec_id, label=p.get("name"))


# ─── input composition (moved verbatim from the old pool) ─────────────────────
def _build_input(agent: dict, trigger: dict) -> str:
    from curry_leaves_assistant.domain import recordings
    t = trigger.get("type")
    p = trigger.get("payload") or {}
    if t in ("recording.transcribed", "recording.summarized", "recording.outputs.completed"):
        include_outputs = t != "recording.transcribed"
        context = recordings.agent_context(p.get("id"), include_outputs=include_outputs) or (
            f"Recording id: {p.get('id')}\nTranscript:\n{p.get('transcript') or '(empty)'}")
        if "kb-filer" in (agent.get("id") or ""):
            context += "\n" + recordings.provenance_context(p.get("id"))
        # The Meeting Copilot's whole job is defined by the recording's template — inject it so
        # it produces exactly the sections the template promises (see templates_store).
        if agent.get("id") == "meeting-copilot":
            from curry_leaves_assistant.stores import templates_store
            tids = p.get("templateIds") or ([p["templateId"]] if p.get("templateId") else None)
            context += "\n\n=== MEETING TEMPLATE ===\n" + templates_store.agent_context(template_ids=tids)
        return ("A recording is ready. Do your job for it. The user's own notes, links, and "
                "attached documents (if any) are authoritative context.\n\n" + context)
    if t == "knowledge.ingest.requested":
        dup = ("This document was fed before — EDIT existing notes with any genuinely new facts "
               "instead of duplicating.\n" if p.get("duplicate") else "")
        doc_id = p.get("docId")
        n = p.get("chunkCount") or 1
        return (
            "A document has been converted to markdown and placed in the knowledge base inputs. "
            "It is YOURS to file end to end — read through it with your tools and capture every "
            "durable fact. It is a DOCUMENT, not a meeting transcript, so do NOT set a meeting "
            "`source:` on the notes you file.\n\n"
            f"Input id: {doc_id}\nTitle: {p.get('title') or '(untitled)'}\nSize: {n} chunk(s).\n{dup}\n"
            "How to work:\n"
            f"1. input_outline('{doc_id}') → see the document's shape.\n"
            f"2. Go through EVERY chunk in order (i = 0 … {n - 1}): read_input('{doc_id}', i), pull the "
            "durable facts, and file them — search_kb / read the target note first so you "
            "MERGE into existing notes instead of duplicating, then write_file / kb_edit, linking every "
            "note. File as you go; move to the next chunk. Do NOT stop until you've read the last chunk.\n"
            "3. Finish with a one-line summary of everything you filed.")
    if t == "todo.created":
        # The Todo Triage agent decides whether the team can meaningfully do this todo, and if
        # so posts it to the pool (which wakes the Lead). Pure personal reminders → do nothing.
        due = p.get("dueDate")
        prio = p.get("priority")
        return (
            "A new todo was just added. Decide whether the team can meaningfully DO it with their "
            "tools (research, notes, filing, dashboards, etc.). If it's a pure personal reminder "
            "the team can't act on (e.g. 'call mom', 'buy milk'), do nothing and stop. If it's "
            "actionable, call `triage_post` exactly once with this todo's id, then stop — don't do "
            "the task yourself and don't create any todos.\n\n"
            f"Todo id: {p.get('id')}\n"
            f"Text: {p.get('text')}\n"
            + (f"Priority: {prio}\n" if prio else "")
            + (f"Due: {due}\n" if due else "")
        )
    if t == "pool.assigned":
        tags = ", ".join(p.get("tags") or [])
        return (
            "You've been assigned a task from the common pool. Complete it and use your tools as needed.\n\n"
            f"Title: {p.get('title')}\n"
            f"Details: {p.get('description') or '(none)'}\n"
            + (f"Tags: {tags}\n" if tags else "")
        )
    if t == "learn.signal":
        # A detector spotted something learnable and handed the evidence over. The Skill Learner reflects on
        # THIS one thing (read its trace) rather than mining all of history.
        extra = ""
        if p.get("recoveryTraceId"):
            extra = (f"\nA nearby SUCCESSFUL run of the same task is trace "
                     f"{p['recoveryTraceId']} (job {p.get('recoveryJobId')}) — compare the two.")
        return (
            "A learning signal fired — reflect on this ONE piece of evidence and decide what, if "
            "anything, is worth remembering.\n\n"
            f"Signal: {p.get('kind')}\n"
            f"Agent involved: {p.get('agentId')}\n"
            f"Task shape: {p.get('taskShape')}\n"
            f"What happened: {p.get('summary')}\n"
            f"Suggested angle: {p.get('hint')}\n"
            f"Evidence trace: {p.get('traceId')} (job {p.get('jobId')}).{extra}\n\n"
            "Read the trace, then follow your procedure: capture a procedural lesson as a SKILL "
            "(scoped to the agent it applies to), a durable user/agent fact via update_profile / "
            "remember, or nothing if it isn't a real, repeatable lesson. Then mark this episode "
            "reviewed.")
    if t == "schedule":
        return "This is a scheduled autonomous run. Perform your routine and use your tools as needed."
    if t == "task":
        # A direct task handed to an agent (assign_task / spawn_agent): the payload IS the brief.
        return p.get("input") or p.get("description") or "Perform the assigned task."
    return f"An event '{t}' occurred. Context:\n{trigger}"
