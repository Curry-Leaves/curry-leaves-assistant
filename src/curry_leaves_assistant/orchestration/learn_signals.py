"""Learning-signal detectors — turn a just-finished episode into a `learn.signal` event.

These are MECHANICAL (pure code, no LLM): cheap rules over the episode store that fire only
when there's genuine evidence worth an agent's attention. Each emits `learn.signal` with the
evidence in the payload, so the Skill Learner — which triggers on `learn.signal` — can
reflect on ONE concrete thing (a specific failed trace, a specific inefficient run) instead of
mining 200 run records blind. That "signal at the moment, evidence in hand" is the whole point
of the redesign: the old nightly-batch learner never had a pointer to what mattered.

Precision over recall. A false signal wastes an LLM reflection and risks a junk skill, so the
thresholds are deliberately conservative and only high-confidence detectors are live by
default (see LIVE_KINDS). Fuzzier ones can be enabled once observed clean on real data.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from curry_leaves_assistant.core import events
from curry_leaves_assistant.core.store import now_iso
from curry_leaves_assistant.stores import episode_store

# Which signal kinds actually emit. Start with the two highest-precision detectors; the fuzzier
# inefficiency/repetition ones are computed + logged but gated off until tuned on real data.
LIVE_KINDS = {"failure_recovered", "inefficiency"}

# Tuning knobs (conservative on purpose).
_RECOVER_WINDOW_HOURS = 24     # a recovery must follow the failure within this window
_INEFFICIENCY_FACTOR = 2.0     # steps must exceed this multiple of the agent+shape median
_REPEAT_TOOL_MIN = 5           # ≥ this many identical tool calls in one run = spinning


def inspect(episode: dict) -> None:
    """Run every detector on a freshly-recorded episode; emit a learn.signal for each LIVE hit.
    Called from workers._finalize (already wrapped in a fail-soft guard there)."""
    for signal in _detect(episode):
        if signal["kind"] not in LIVE_KINDS:
            continue
        events.emit(
            "learn.signal",
            payload=signal,
            entity_id=episode.get("agentId"),
            label=f"learn: {signal['kind']}",
        )


def _detect(ep: dict) -> list[dict]:
    """All detector hits for this episode (regardless of LIVE gating — callers filter)."""
    out: list[dict] = []
    agent_id = ep.get("agentId")
    shape = ep.get("taskShape")

    # ── inefficiency: this run took far more steps than the usual for its kind, OR it
    #    hammered one tool many times over (the 6x-kb_read pattern). Only meaningful on a
    #    SUCCESS — a failure's step count is noise. ────────────────────────────────────────
    if ep.get("outcome") == "done":
        reasons = []
        median = episode_store.baseline_steps(agent_id, shape)
        if median and ep.get("steps", 0) > _INEFFICIENCY_FACTOR * median:
            reasons.append(f"took {ep['steps']} steps vs a usual ~{median:.0f} for {shape}")
        if ep.get("maxToolRepeat", 0) >= _REPEAT_TOOL_MIN:
            top = max(ep.get("toolCalls", {}).items(), key=lambda kv: kv[1], default=("?", 0))
            reasons.append(f"called {top[0]} {top[1]}x in one run")
        if reasons:
            out.append(_signal("inefficiency", ep,
                               summary="; ".join(reasons),
                               hint="Find the redundant work in the trace and capture the leaner "
                                    "approach as a skill for this agent."))

    # ── failure recovered: this run FAILED, but an earlier or later run of the same
    #    agent+shape SUCCEEDED nearby — so there's a known-good path this failure missed.
    #    The recovery's trace shows what worked; the lesson is how to avoid the failure. ────
    if ep.get("outcome") == "failed":
        recovery = _find_recovery(ep)
        if recovery:
            out.append(_signal("failure_recovered", ep,
                               summary=f"failed ({(ep.get('error') or '')[:120]}) but a nearby "
                                       f"{shape} run succeeded",
                               hint="Compare the failed trace with the successful one; capture "
                                    "what to do differently as a correction skill.",
                               extra={"recoveryJobId": recovery.get("jobId"),
                                      "recoveryTraceId": recovery.get("traceId")}))

    # ── repetition: this exact task-shape has come up many times this week with no covering
    #    skill — an automation/standardization candidate. (Fuzzy; gated off by default.) ────
    week_ago = _iso_days_ago(7)
    if episode_store.count_task_shape(agent_id, shape, since=week_ago) >= 6:
        out.append(_signal("repetition", ep,
                           summary=f"{shape} has run many times this week",
                           hint="If there's a consistent multi-step approach, capture it as a "
                                "reusable skill so it's standardized."))
    return out


def _find_recovery(failed: dict) -> dict | None:
    """A successful run of the same agent+shape within the recovery window of this failure."""
    agent_id, shape = failed.get("agentId"), failed.get("taskShape")
    try:
        t0 = datetime.fromisoformat(failed["finishedAt"])
    except Exception:
        return None
    for ep in episode_store.recent(agent_id, task_shape=shape, limit=20):
        if ep.get("outcome") != "done" or ep.get("jobId") == failed.get("jobId"):
            continue
        try:
            t1 = datetime.fromisoformat(ep["finishedAt"])
        except Exception:
            continue
        if abs((t1 - t0).total_seconds()) <= _RECOVER_WINDOW_HOURS * 3600:
            return ep
    return None


def _signal(kind: str, ep: dict, *, summary: str, hint: str, extra: dict | None = None) -> dict:
    """Assemble one signal payload — enough evidence for the Skill Learner to act without re-mining history:
    the agent, the task-shape, the trace to read, a plain-language summary, and a next-step hint."""
    sig = {
        "kind": kind,
        "agentId": ep.get("agentId"),
        "jobId": ep.get("jobId"),
        "traceId": ep.get("traceId"),
        "taskShape": ep.get("taskShape"),
        "summary": summary,
        "hint": hint,
        "detectedAt": now_iso(),
    }
    if extra:
        sig.update(extra)
    return sig


def _iso_days_ago(days: int) -> str:
    # now_iso() is timezone-aware ISO; derive the window bound the same way for a clean compare.
    try:
        return (datetime.fromisoformat(now_iso()) - timedelta(days=days)).isoformat()
    except Exception:
        return now_iso()
