"""Live in-meeting context engine.

Taps the live transcript as it streams and surfaces cards that help the user drive the meeting
— open loops, reminders, questions to ask, answers to what they're unsure about, and guidance.

Each pass runs the seeded ``meeting-live`` agent through the Work Kernel (a normal WorkItem on
the interactive band): the agent reads the transcript window, gathers relevant todos/reminders
and knowledge notes with its own tools (reading a note's body to actually answer a question),
and returns a small JSON array of cards. We parse that and push it to the client as a
``live.context`` frame over the same WebSocket the transcript rides.

**Every pass is self-contained.** Passes used to share one sessionId so the agent would remember
what it had surfaced — but a shared session is rehydrated in full on each run, so input grew
quadratically with meeting length (~140k tokens on pass 100 of a single meeting). Now each pass
gets its own session and the brief carries the already-surfaced card texts forward explicitly
(``_MAX_ALREADY``), which is the only part of that history that was load-bearing. Per-pass input
is flat; see ``_Session._run_agent``.

One ``_Session`` per active recording stream. Passes are gated so this stays cheap: a cooldown
between passes, a per-session cap, and the recording's template must opt in (``live.watch``
non-empty). A meeting that yields nothing worth surfacing costs a cheap agent run and no cards.

Three gates decide whether a session runs at all, and ALL must pass:
  1. ``settings.live.enabled`` — the app-level switch (default off), overridable per recording
     from the Capture screen via the ``enabled`` argument on ``live.attach``.
  2. the recording's template opting in (``live.watch`` non-empty).
  3. the pass budget/cooldown below.
The tuning constants are defaults only — the real values come from ``settings.live`` and are
read per pass, so changing them in Settings takes effect on a recording already in progress.
"""
from __future__ import annotations

import asyncio
import json

# Pass gating. Defaults only — settings.live overrides all but _FIRST_PASS_CHARS/_WINDOW_CHARS,
# which are engine feel rather than cost knobs and stay fixed.
_COOLDOWN_S = 20.0          # min seconds between passes per session (signals bypass this)
_MIN_NEW_CHARS = 120        # transcript growth before a passive (transcript-driven) pass
_FIRST_PASS_CHARS = 40      # fire the FIRST pass early so the copilot engages fast
_MAX_PASSES = 100           # hard per-session budget backstop
_MAX_CARDS_PER_PASS = 2
_WINDOW_CHARS = 1600        # how much recent transcript the agent sees
# How many already-surfaced card texts to echo back as the "don't repeat these" list. Bounded so
# a long meeting can't grow the brief without limit — the whole point of dropping the session
# replay. Newest-last: a card from 40 minutes ago is unlikely to be re-surfaced now, whereas the
# recent ones are exactly what the agent would otherwise restate.
_MAX_ALREADY = 12
# How much transcript a session retains. Only the last _WINDOW_CHARS is ever read into a brief,
# so anything beyond a small multiple of that is dead weight held for the whole meeting — an
# hour of speech is several hundred KB per active recording. Keep enough slack that the window
# is always full, and drop the rest.
_MAX_TRANSCRIPT_CHARS = _WINDOW_CHARS * 4
# Cap on the dedup/anti-repetition history. Only the newest _MAX_ALREADY are ever sent, and
# _seen exists purely as an exact-duplicate backstop, so neither needs to be unbounded.
_MAX_SURFACED = 200


def _parse_cards(output: str, allowed_kinds: list[str] | None = None,
                 max_cards: int = _MAX_CARDS_PER_PASS) -> list[dict]:
    """Parse the agent's final message (a JSON array of card objects). Tolerates a stray code
    fence or surrounding prose by extracting the first [...] block. Returns [] on anything odd.

    ``allowed_kinds`` (from the template's live.watch) drops cards the template didn't ask for,
    so watch is load-bearing rather than advisory. Passing None disables the filter."""
    if not output:
        return []
    text = output.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:] if "\n" in text else text
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        arr = json.loads(text[start:end + 1])
    except Exception:
        return []
    out = []
    for c in arr if isinstance(arr, list) else []:
        if isinstance(c, dict) and c.get("kind") and c.get("text"):
            kind = str(c["kind"])
            if allowed_kinds is not None and kind not in allowed_kinds:
                continue
            out.append({"kind": kind, "text": str(c["text"]),
                        "source": c.get("source"), "refId": c.get("refId")})
    return out[:max_cards]


def _brief(window: str, watch: list[str], attendees: list[str], first: bool,
           template_ctx: str = "", kinds: list[str] | None = None,
           max_cards: int = _MAX_CARDS_PER_PASS, already: list[str] | None = None) -> str:
    """The task brief handed to the meeting-live agent for a pass.

    Every pass is a SELF-CONTAINED turn: the brief carries the meeting context, the recent
    transcript window, and ``already`` — the cards surfaced so far — so the agent can avoid
    repeating itself without being handed the whole prior conversation. See _run_agent for why
    that matters (replaying history made cost grow quadratically with meeting length).

    Because passes no longer share a conversation, the template context goes on EVERY brief
    rather than just the first — there's no session memory left holding it. It's a couple of
    hundred tokens against the several thousand the replay used to cost."""
    who = ", ".join(attendees) if attendees else "(unknown)"
    cares = ", ".join(watch) if watch else "open loops, questions, and useful guidance"
    allowed = ", ".join(kinds) if kinds else ""
    # The brief is DATA, not instructions: the role, the procedure, and the output contract all
    # live in the system prompt, which is cached. Anything restated here is billed at full price
    # on every tool step of every pass, so this carries only what changes per pass.
    opening = "" if first else "Meeting already under way; add only NEW cards.\n"
    if template_ctx:
        opening += "\n=== THIS MEETING ===\n" + template_ctx + "\nTailor cards to this meeting type.\n"
    opening += f"\nCares about: {cares}.\nAttendees: {who}.\n"
    if already:
        # The anti-repetition guarantee, carried explicitly instead of via replayed history.
        # Newest last, capped — an old card is far less likely to be re-surfaced than a recent
        # one, so the tail is what actually earns its tokens.
        shown = already[-_MAX_ALREADY:]
        opening += (
            "\nAlready surfaced — do NOT repeat or reword:\n"
            + "\n".join(f"- {t}" for t in shown)
            + "\n"
        )
    # The card cap and the allowed-kind filter are the only per-pass constraints — both are
    # config-driven, so they can't live in the cached system prompt. Everything else the tail
    # used to repeat (use your tools, read the note body, return JSON) is already there.
    tail = f"Return at most {max_cards} card{'' if max_cards == 1 else 's'}."
    if allowed:
        tail += f" Only these kinds count; others are discarded: {allowed}."
    return (
        opening
        + "\nTranscript (most recent last):\n"
        + f"{window[-_WINDOW_CHARS:] or '(nothing spoken yet)'}\n\n"
        + tail
    )


class _Session:
    """One live recording's context engine. Feed it transcript growth, attendee adds, and typed
    notes; it runs the meeting-live agent per pass and fans ``live.context`` card frames back."""

    def __init__(self, conn, stream_id: str, recording_id: str, send,
                 enabled: bool | None = None) -> None:
        self.conn = conn
        self.stream_id = stream_id
        self.rec_id = recording_id
        self._send = send                      # send(conn, frame_dict)
        # Per-recording override of settings.live.enabled, sent by the Capture toggle. None =
        # follow the app setting. Held as a plain attribute so the toggle can flip it MID-
        # recording (see set_enabled) without tearing the session down and losing its history.
        self._enabled_override = enabled
        self._transcript = ""
        self._since_pass = 0
        self._last_pass_mono = -1e9
        self._passes = 0
        self._seen: set[str] = set()            # lowercased card texts — a cheap exact-dup backstop
        # The same texts in surfacing order (original casing). Feeds the "already surfaced,
        # don't repeat" list in each brief — the explicit replacement for the conversation
        # history passes used to share. A set can't do this: order is what lets us keep the
        # most recent (and most repeat-prone) cards when trimming to _MAX_ALREADY.
        self._surfaced: list[str] = []
        self._lock = asyncio.Lock()
        self._deferring = False                 # a transcript pass is waiting out the cooldown

    # ── template gate ──
    def _template_ids(self) -> list[str]:
        """The recording's selected template ids, re-read from disk every call so a template
        switched MID-recording takes effect on the next pass."""
        try:
            from curry_leaves_assistant.domain import recordings
            meta = recordings.get(self.rec_id) or {}
            return meta.get("templateIds") or ([meta["templateId"]] if meta.get("templateId") else [])
        except Exception:
            return []

    def _watch_kinds(self, tids: list[str] | None = None) -> list[str]:
        try:
            from curry_leaves_assistant.stores import templates_store
            watch: list[str] = []
            for tid in (self._template_ids() if tids is None else tids):
                for w in ((templates_store.get_template(tid) or {}).get("live") or {}).get("watch") or []:
                    if w not in watch:
                        watch.append(w)
            return watch
        except Exception:
            return []

    def _attendees(self) -> list[str]:
        try:
            from curry_leaves_assistant.domain import recordings
            return [a for a in ((recordings.get(self.rec_id) or {}).get("attendees") or []) if isinstance(a, str)]
        except Exception:
            return []

    # ── settings gate ──
    def _cfg(self) -> dict:
        """The live block, re-read per use so a Settings change lands on the next pass of a
        recording already running. Falls back to the module defaults if settings are unreadable —
        the engine should degrade, not die, on a corrupt settings file."""
        try:
            from curry_leaves_assistant.core import settings as app_settings
            return app_settings.live_cfg()
        except Exception:
            return {"enabled": False, "minNewChars": _MIN_NEW_CHARS,
                    "cooldownSeconds": _COOLDOWN_S, "maxPasses": _MAX_PASSES,
                    "maxCardsPerPass": _MAX_CARDS_PER_PASS}

    def enabled(self) -> bool:
        """Whether the copilot may run for THIS recording: the per-recording override if the
        user set one from Capture, else the app-level setting."""
        if self._enabled_override is not None:
            return self._enabled_override
        return bool(self._cfg().get("enabled"))

    def set_enabled(self, enabled: bool | None) -> None:
        """Apply a per-recording override mid-recording (the Capture toggle). Passing None
        returns this recording to following the app setting."""
        self._enabled_override = enabled

    def engaged(self) -> bool:
        return self.enabled() and bool(self._watch_kinds())

    # ── inputs (called from the ws read loop; cheap, schedule the heavy work) ──
    def _append_transcript(self, text: str) -> None:
        """Append to the rolling transcript, trimming to _MAX_TRANSCRIPT_CHARS. Briefs only ever
        read the tail, so the head is discardable — and holding it for a whole meeting is what
        made a long recording's session grow without bound."""
        self._transcript += (" " + text)
        if len(self._transcript) > _MAX_TRANSCRIPT_CHARS:
            self._transcript = self._transcript[-_MAX_TRANSCRIPT_CHARS:]

    def feed_transcript(self, text: str, loop: asyncio.AbstractEventLoop) -> None:
        if not text or not self.engaged():
            return
        self._append_transcript(text)
        self._since_pass += len(text)
        # Fire the first pass early (short threshold) so the copilot engages quickly; later
        # passes wait for more new speech so it isn't chatty.
        threshold = _FIRST_PASS_CHARS if self._passes == 0 else int(
            self._cfg().get("minNewChars") or _MIN_NEW_CHARS)
        if self._since_pass >= threshold:
            self._schedule(loop, force=False)

    def feed_signal(self, hint: str, loop: asyncio.AbstractEventLoop) -> None:
        """An attendee added or a note typed — a high-signal cue; run immediately (bypasses the
        growth threshold and the cooldown)."""
        if not hint or not self.engaged():
            return
        self._append_transcript(hint)
        self._schedule(loop, force=True)

    def refresh(self, loop: asyncio.AbstractEventLoop) -> None:
        """The user asked for fresh cards → force a pass now, bypassing the cooldown/threshold.
        Engages the engine even if the template opted out of passive watching, since the user
        explicitly asked. The enable gate still applies: a copilot the user switched off must
        not run, and the UI hides the refresh button in that state anyway."""
        if not self.enabled():
            return
        self._since_pass = 0
        self._schedule(loop, force=True)

    def _schedule(self, loop: asyncio.AbstractEventLoop, *, force: bool) -> None:
        loop.create_task(self._maybe_pass(force=force))

    async def _maybe_pass(self, *, force: bool) -> None:
        # A transcript-driven pass in cooldown waits it out FIRST (before taking the lock), so
        # the context you just added still produces a pass — but a forced signal (note/attendee)
        # can run immediately meanwhile. Only one deferred pass waits at a time.
        cfg = self._cfg()
        cooldown = float(cfg.get("cooldownSeconds") or _COOLDOWN_S)
        if not force:
            if self._deferring:
                return  # a deferred pass is already pending — it'll cover this new context
            wait = cooldown - (asyncio.get_event_loop().time() - self._last_pass_mono)
            if wait > 0:
                self._deferring = True
                try:
                    await asyncio.sleep(wait)
                finally:
                    self._deferring = False
        if self._lock.locked():
            return
        async with self._lock:
            # Re-check the gate here, not just at schedule time: the user can switch the copilot
            # off (globally or for this recording) while a deferred pass sits in its cooldown
            # sleep above, and that pass must not fire after the toggle went off.
            if not self.enabled():
                return
            now = asyncio.get_event_loop().time()
            if not force and (now - self._last_pass_mono) < cooldown:
                return  # a signal jumped in during our wait and reset the cooldown — yield to it
            if self._passes >= int(cfg.get("maxPasses") or _MAX_PASSES):
                return
            self._last_pass_mono = now
            self._since_pass = 0
            self._passes += 1
            cap = int(cfg.get("maxCardsPerPass") or _MAX_CARDS_PER_PASS)
            cards = await self._run_agent(cap)
            fresh = [c for c in cards if c["text"].lower() not in self._seen][:cap]
            if not fresh:
                return
            for c in fresh:
                self._seen.add(c["text"].lower())
                self._surfaced.append(c["text"])
            # Trim both to the newest _MAX_SURFACED. Briefs only carry _MAX_ALREADY, and a card
            # from far enough back is not one the agent is about to restate — so an unbounded
            # dedup history buys nothing. _seen is rebuilt from the survivors to stay in step.
            if len(self._surfaced) > _MAX_SURFACED:
                self._surfaced = self._surfaced[-_MAX_SURFACED:]
                self._seen = {t.lower() for t in self._surfaced}
            self._send(self.conn, {"type": "live.context", "streamId": self.stream_id,
                                   "recordingId": self.rec_id, "cards": fresh})

    async def _run_agent(self, max_cards: int = _MAX_CARDS_PER_PASS) -> list[dict]:
        """Run the meeting-live agent through the Work Kernel and parse its card output.

        Each pass runs in its OWN session. Passes used to share one stable sessionId so the
        agent would remember the cards it had already surfaced — but a shared session is
        rehydrated in full on every run (agent_engine replays the whole transcript: every prior
        brief, tool call, tool result, and reply). That made input cost grow quadratically with
        meeting length: by pass 100 a single pass was resending ~140k tokens, and an hour-long
        meeting billed several million input tokens for maybe a hundred short cards.

        The anti-repetition guarantee is preserved by carrying the surfaced card texts forward
        explicitly in the brief (bounded by _MAX_ALREADY) — the small part of that history that
        was actually load-bearing. Per-pass input is now flat instead of growing."""
        from curry_leaves_assistant.core.store import now_iso
        from curry_leaves_assistant.orchestration import work
        from curry_leaves_assistant.orchestration.work import BAND_INTERACTIVE, WorkItem
        from curry_leaves_assistant.stores import templates_store

        tids = self._template_ids()
        watch = self._watch_kinds(tids)
        # Kinds the template admits. An explicit refresh on a template that opted out of passive
        # watching (or has no template at all) still runs — accept anything rather than filter
        # every card away, since the user asked for these directly.
        kinds = templates_store.live_card_kinds(watch) or None
        brief = _brief(self._transcript, watch, self._attendees(), first=(self._passes == 1),
                       template_ctx=templates_store.live_context(template_ids=tids), kinds=kinds,
                       max_cards=max_cards, already=self._surfaced)
        # Fresh event id AND fresh sessionId per pass: each pass is self-contained, so nothing
        # is rehydrated and per-pass input stays flat over a long meeting. What the agent needs
        # to remember rides in the brief instead (see the docstring above).
        trigger = {"id": f"live_{self.rec_id}_{self._passes}", "type": "task",
                   "sessionId": f"live_{self.rec_id}_{self._passes}", "occurredAt": now_iso(),
                   "payload": {"input": brief}}
        job_id = work.submit(WorkItem(
            kind="agent", agent_id="meeting-live", trigger=trigger, mode="background",
            lane="live", band=BAND_INTERACTIVE, autonomy="auto", dedupe_key=trigger["id"]))
        try:
            result = await asyncio.wait_for(work.on_complete(job_id), timeout=45)
        except Exception as exc:
            print(f"[live-context] pass failed to complete: {type(exc).__name__}: {exc}", flush=True)
            return []
        if not result or result.get("error"):
            if result and result.get("error"):
                print(f"[live-context] agent run error: {result.get('error')}", flush=True)
            return []
        return _parse_cards(result.get("output") or "", kinds, max_cards)
