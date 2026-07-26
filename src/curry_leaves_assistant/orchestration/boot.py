"""Start the Work Kernel: set the loop, spawn workers, register trigger sources.

One call from the app lifespan (`orchestration.boot.start()`) wires the execution substrate:
workers (the fleet + crash recovery) and the trigger sources (event matching + the agent /
Gardener schedule sources). Dashboard tiles and reminders register their own schedule sources
from the app lifespan; the unified scheduler (orchestration.schedule) — started by the lifespan
too — ticks all of them.
"""
from __future__ import annotations

import asyncio

from curry_leaves_assistant.orchestration import triggers, work, workers


def start() -> None:
    work.set_loop(asyncio.get_running_loop())
    workers.start()      # scheduler wiring + recovery + worker fleet
    triggers.start()     # event trigger matching + agent/Gardener schedule sources
    print("[work-kernel] started", flush=True)


async def stop() -> None:
    """Tear the kernel down: drain the worker fleet. In-flight jobs are left as durable
    *.running residue and recovered on the next boot. The unified scheduler is stopped
    separately by the app lifespan."""
    await workers.stop()
    print("[work-kernel] stopped", flush=True)
