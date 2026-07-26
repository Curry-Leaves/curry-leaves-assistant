"""Orchestration layer: agent pool, schedulers, dashboard runner.

Runs agents in response to events/schedules. Sits above domain/ + stores/ + agents/;
nothing below this layer may import it (enforced by import-linter).

``start()`` / ``stop()`` are the single entry/exit the app lifespan uses to bring the whole
orchestration substrate up and down in the right order — the composition root shouldn't have to
know which sub-modules register what, or that the scheduler must stop before the workers drain.
"""
from __future__ import annotations


def start() -> None:
    """Bring up the orchestration substrate. Order matters: the Work Kernel (workers + event
    triggers + agent/Gardener schedule sources) first, then the modules that register their own
    schedule sources (dashboard tiles, reminders), then the one tick loop that drives them all —
    started last so every source is registered before the first poll."""
    from curry_leaves_assistant.orchestration import (
        boot as work_kernel,
        dashboard_runner,
        reminder_scheduler,
        schedule,
    )

    work_kernel.start()          # workers + event triggers + agent/Gardener schedule sources
    dashboard_runner.start()     # registers the tile schedule source + event handler
    reminder_scheduler.start()   # registers the reminder schedule source
    schedule.start()             # the one tick loop that drives every registered schedule source


async def stop() -> None:
    """Tear the substrate down cleanly. Stop the scheduler tick FIRST, then drain the worker
    fleet, so the loop closes without 'Task was destroyed but it is pending' noise or orphaned
    polls. In-flight jobs are left as durable *.running residue and recovered on the next boot."""
    from curry_leaves_assistant.orchestration import boot as work_kernel, schedule

    await schedule.stop()
    await work_kernel.stop()
