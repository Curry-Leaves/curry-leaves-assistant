"""Live Copilot gating: the app setting, the per-recording override, and the clamps.

The copilot spends real tokens on every pass, so "off means off" is the property worth pinning
down — including the awkward case of a pass that was already scheduled when the user switched it
off, and a settings file hand-edited to values that would defeat the gating entirely.
"""
from __future__ import annotations

import pytest

from curry_leaves_assistant.core import paths
from curry_leaves_assistant.core import settings as app_settings
from curry_leaves_assistant.orchestration import live_context


@pytest.fixture(autouse=True)
def _clean_settings():
    if paths.SETTINGS_PATH.exists():
        paths.SETTINGS_PATH.unlink()
    yield
    if paths.SETTINGS_PATH.exists():
        paths.SETTINGS_PATH.unlink()


def _session() -> live_context._Session:
    return live_context._Session(None, "stream-1", "rec-1", lambda *a: None)


def test_defaults_to_off():
    """Opt-in, not opt-out — every pass is a billable agent run."""
    assert app_settings.live_cfg()["enabled"] is False
    assert _session().enabled() is False


def test_per_recording_override_works_in_both_directions():
    sess = _session()                      # app setting is off
    sess.set_enabled(True)
    assert sess.enabled() is True          # on for THIS recording only

    app_settings.patch_live({"enabled": True})
    sess2 = _session()
    assert sess2.enabled() is True
    sess2.set_enabled(False)
    assert sess2.enabled() is False        # off for THIS recording only


def test_clearing_the_override_returns_to_the_app_setting():
    sess = _session()
    sess.set_enabled(True)
    sess.set_enabled(None)
    assert sess.enabled() is False


def test_settings_are_read_per_pass_not_snapshotted():
    """A session outlives a settings change: the copilot must pick up new tuning (and a new
    on/off state) on a recording already in progress."""
    sess = _session()
    assert sess._cfg()["cooldownSeconds"] == 20
    app_settings.patch_live({"enabled": True, "cooldownSeconds": 45})
    assert sess._cfg()["cooldownSeconds"] == 45
    assert sess.enabled() is True


def test_manual_refresh_is_a_no_op_while_disabled():
    """refresh() deliberately bypasses the template gate (the user asked directly) but must NOT
    bypass the enable gate — a copilot the user switched off must not run."""
    sess = _session()                      # app setting off, no override
    scheduled: list[bool] = []
    sess._schedule = lambda loop, *, force: scheduled.append(force)  # type: ignore[method-assign]
    sess.refresh(None)                     # type: ignore[arg-type]
    assert scheduled == []

    sess.set_enabled(True)
    sess.refresh(None)                     # type: ignore[arg-type]
    assert scheduled == [True]


def test_engaged_requires_both_the_setting_and_a_watching_template():
    """Both gates, not either: a template that opts in shouldn't start the copilot while the
    app setting is off."""
    sess = _session()
    sess._watch_kinds = lambda tids=None: ["open-loops"]  # type: ignore[method-assign]
    assert sess.engaged() is False         # template watches, but the setting is off
    sess.set_enabled(True)
    assert sess.engaged() is True

    sess._watch_kinds = lambda tids=None: []  # type: ignore[method-assign]
    assert sess.engaged() is False         # enabled, but no template opts in


def test_numeric_settings_are_clamped_to_sane_floors():
    """A 0 from a hand-edited settings.json would otherwise run the agent on every transcript
    chunk with no cooldown."""
    live = app_settings.patch_live({
        "minNewChars": 0, "cooldownSeconds": 0, "maxPasses": 0, "maxCardsPerPass": 0,
    })["live"]
    assert live["minNewChars"] == 20
    assert live["cooldownSeconds"] == 5
    assert live["maxPasses"] == 1
    assert live["maxCardsPerPass"] == 1


def test_non_numeric_values_are_ignored_rather_than_corrupting_the_block():
    app_settings.patch_live({"cooldownSeconds": "junk", "maxPasses": None})
    live = app_settings.live_cfg()
    assert live["cooldownSeconds"] == 20   # untouched default
    assert live["maxPasses"] == 100


# ── context cost ──────────────────────────────────────────────────────────────
# Passes used to share one sessionId, which agent_engine rehydrates in full on every run, so
# input grew quadratically with meeting length (~140k tokens on pass 100 of one meeting). These
# pin the two properties that fixed it.

def test_each_pass_gets_its_own_session_so_nothing_is_rehydrated():
    """A stable sessionId is what made cost quadratic — every pass replayed the whole prior
    conversation. Distinct ids per pass keep each pass self-contained."""
    sess = _session()
    sess._passes = 1
    first = f"live_{sess.rec_id}_{sess._passes}"
    sess._passes = 7
    later = f"live_{sess.rec_id}_{sess._passes}"
    assert first != later

    # And the id actually used is the per-pass one, not the per-recording one.
    import inspect
    src = inspect.getsource(live_context._Session._run_agent)
    assert '"sessionId": f"live_{self.rec_id}_{self._passes}"' in src


def test_brief_size_is_flat_as_a_meeting_runs_long():
    """The regression that matters: brief size must plateau, not grow with pass count."""
    window = "word " * 9000                      # far longer than the transcript window
    # Fixed-width labels: this measures growth from the NUMBER of cards, not from longer ids.
    cards = [f"Card {i:03d}: someone committed to something" for i in range(200)]

    def size(n_cards: int) -> int:
        return len(live_context._brief(
            window, ["open-loops"], ["Alice"], first=False,
            template_ctx="A weekly 1:1.", kinds=["open-loop"], already=cards[:n_cards]))

    early, mid, late = size(5), size(50), size(200)
    assert mid == late, "brief must stop growing once the already-list hits its cap"
    assert late - early < 700, "brief must not scale with the number of cards surfaced"


def test_already_surfaced_list_is_capped_and_keeps_the_newest():
    """Newest-last: a card from 40 minutes ago is unlikely to be re-surfaced; the recent ones
    are what the agent would otherwise restate."""
    cards = [f"card-{i}" for i in range(50)]
    brief = live_context._brief("talk", [], [], first=False, already=cards)
    assert "card-49" in brief                    # newest kept
    assert "card-0" not in brief                 # oldest dropped
    listed = sum(1 for line in brief.splitlines() if line.startswith("- card-"))
    assert listed == live_context._MAX_ALREADY


def test_brief_carries_every_per_pass_input():
    """The brief was tightened to remove text the (cached) system prompt already carries. These
    are the things it must still carry, because none of them are knowable at prompt-build time."""
    brief = live_context._brief(
        "we should ship on Friday", ["open-loops", "commitments"], ["Alice", "Bob"],
        first=False, template_ctx="A weekly 1:1 with a direct report.",
        kinds=["open-loop", "answer"], max_cards=3, already=["Alice owes Bob the doc"])
    assert "A weekly 1:1" in brief                    # which meeting this is
    assert "open-loops, commitments" in brief         # what the template watches for
    assert "Alice, Bob" in brief                      # who is present
    assert "we should ship on Friday" in brief        # the transcript window
    assert "Alice owes Bob the doc" in brief          # anti-repetition list
    assert "at most 3 cards" in brief                 # the configured cap
    assert "open-loop, answer" in brief               # the allowed-kind filter


def test_memory_instinct_only_for_agents_holding_those_tools():
    """meeting-live holds three read tools, so the update_profile/remember instruction was pure
    waste in its prompt — and it rides in EVERY agent's prompt, so the gate matters broadly."""
    from curry_leaves_assistant.agents.agent_engine import _with_user_profile
    assert "Record something durable" not in _with_user_profile("B", tool_names=["todos_read"])
    both = _with_user_profile("B", tool_names=["remember", "update_profile"])
    assert "`update_profile`" in both and "`remember`" in both
    # Only the tool actually held is named — no "(only if you hold the tool)" hedge needed.
    only_remember = _with_user_profile("B", tool_names=["remember"])
    assert "`remember`" in only_remember and "`update_profile`" not in only_remember


def test_surfaced_cards_are_carried_into_the_next_brief():
    """The anti-repetition guarantee that replayed history used to provide."""
    sess = _session()
    sess._seen.add("alice owes bob the doc")
    sess._surfaced.append("Alice owes Bob the doc")
    brief = live_context._brief("talk", [], [], first=False, already=sess._surfaced)
    assert "do NOT repeat" in brief
    assert "Alice owes Bob the doc" in brief


def test_system_prompt_stays_above_the_prompt_cache_floor():
    """The kernel caches the system prompt + tool schemas by default (opts.cache defaults True,
    see curry_leaves.providers.anthropic), and a prefix under the model's minimum cacheable size
    silently doesn't cache — no error, just full price on every call, forever.

    The prefix is ~1400 tokens against a 1024-token floor on Sonnet 4.6. That is real but not
    generous headroom, so "shrink the prompt" is a change that can make the copilot MORE
    expensive if taken too far. This pins the seed body's contribution with a conservative
    chars/token estimate; if it trips, check the real prefix size before trimming further.
    """
    from curry_leaves_assistant.stores import agent_store
    agent_store.seed_default_agents()
    rec = agent_store.read_agent("meeting-live")
    assert rec is not None
    body = rec.get("instructions") or ""
    # ~4 chars/token is a deliberate underestimate of the seed's token count; the tool schemas
    # (~970 tokens, not counted here) sit on top of it.
    assert len(body) // 4 > 300, (
        f"meeting-live seed is down to ~{len(body)//4} tokens — with the tool schemas that may "
        "fall under the 1024-token cache floor, which would disable prompt caching entirely."
    )
