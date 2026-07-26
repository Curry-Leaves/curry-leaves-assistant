"""Live in-meeting context engine.

Taps the live transcript as it streams and surfaces cards that help the user drive the meeting
— open loops, reminders, questions to ask, answers to what they're unsure about, and guidance.

Each pass runs the seeded ``meeting-live`` agent through the Work Kernel (a normal WorkItem on
the interactive band): the agent reads the transcript window, gathers relevant todos/reminders
and knowledge notes with its own tools (reading a note's body to actually answer a question),
and returns a small JSON array of cards. We parse that and push it to the client as a
``live.context`` frame over the same WebSocket the transcript rides.

One ``_Session`` per active recording stream. Passes are gated so this stays cheap: a cooldown
between passes, a per-session cap, and the recording's template must opt in (``live.watch``
non-empty). A meeting that yields nothing worth surfacing costs a cheap agent run and no cards.
"""
from __future__ import annotations

import asyncio
import json

# Pass gating.
_COOLDOWN_S = 20.0          # min seconds between passes per session (signals bypass this)
_MIN_NEW_CHARS = 120        # transcript growth before a passive (transcript-driven) pass
_FIRST_PASS_CHARS = 40      # fire the FIRST pass early so the copilot engages fast
_MAX_PASSES = 100           # hard per-session budget backstop
_MAX_CARDS_PER_PASS = 2
_WINDOW_CHARS = 1600        # how much recent transcript the agent sees


def _parse_cards(output: str, allowed_kinds: list[str] | None = None) -> list[dict]:
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
    return out[:_MAX_CARDS_PER_PASS]


def _brief(window: str, watch: list[str], attendees: list[str], first: bool,
           template_ctx: str = "", kinds: list[str] | None = None) -> str:
    """The task brief handed to the meeting-live agent for a pass. The agent runs in ONE
    continuing session per recording, so later passes are follow-up turns — it already
    remembers the cards it surfaced earlier and must not repeat them.

    The FIRST pass carries the template context (what kind of meeting this is, what it's for,
    how it's run); later passes don't repeat it — same session, the agent still has it."""
    who = ", ".join(attendees) if attendees else "(unknown)"
    cares = ", ".join(watch) if watch else "open loops, questions, and useful guidance"
    allowed = ", ".join(kinds) if kinds else ""
    if first:
        opening = (
            "You are helping DRIVE a live meeting right now. Surface a few genuinely useful "
            "cards, then stop.\n\n"
        )
        if template_ctx:
            opening += (
                "=== THIS MEETING ===\n" + template_ctx + "\n\n"
                "Tailor everything you surface to the meeting type above — what counts as useful "
                "in a 1:1 is not what counts in a client call or an interview.\n\n"
            )
        opening += f"This meeting cares about: {cares}.\nAttendees present: {who}.\n\n"
    else:
        opening = (
            "The meeting continues. Here is the latest transcript. Add ONLY new, still-useful "
            "cards — do NOT repeat anything you already surfaced earlier in this conversation or "
            "reword it. If nothing new genuinely helps, return [].\n\n"
        )
    tail = (
        "Use your tools to gather relevant todos, reminders, and knowledge notes — and when a "
        "question is being asked, read the relevant note's body so you can ANSWER it. Then return "
        "your JSON array of at most 2 cards (or [] if nothing genuinely helps)."
    )
    if allowed:
        tail += (f"\n\nThis meeting only wants these card kinds: {allowed}. "
                 "Cards of any other kind are discarded, so don't spend a card on one.")
    return (
        opening
        + "Recent transcript window (most recent last):\n"
        + f"{window[-_WINDOW_CHARS:] or '(nothing spoken yet)'}\n\n"
        + tail
    )


class _Session:
    """One live recording's context engine. Feed it transcript growth, attendee adds, and typed
    notes; it runs the meeting-live agent per pass and fans ``live.context`` card frames back."""

    def __init__(self, conn, stream_id: str, recording_id: str, send) -> None:
        self.conn = conn
        self.stream_id = stream_id
        self.rec_id = recording_id
        self._send = send                      # send(conn, frame_dict)
        self._transcript = ""
        self._since_pass = 0
        self._last_pass_mono = -1e9
        self._passes = 0
        self._seen: set[str] = set()            # lowercased card texts — a cheap exact-dup backstop
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

    def engaged(self) -> bool:
        return bool(self._watch_kinds())

    # ── inputs (called from the ws read loop; cheap, schedule the heavy work) ──
    def feed_transcript(self, text: str, loop: asyncio.AbstractEventLoop) -> None:
        if not text or not self.engaged():
            return
        self._transcript += (" " + text)
        self._since_pass += len(text)
        # Fire the first pass early (short threshold) so the copilot engages quickly; later
        # passes wait for more new speech so it isn't chatty.
        threshold = _FIRST_PASS_CHARS if self._passes == 0 else _MIN_NEW_CHARS
        if self._since_pass >= threshold:
            self._schedule(loop, force=False)

    def feed_signal(self, hint: str, loop: asyncio.AbstractEventLoop) -> None:
        """An attendee added or a note typed — a high-signal cue; run immediately (bypasses the
        growth threshold and the cooldown)."""
        if not hint or not self.engaged():
            return
        self._transcript += (" " + hint)
        self._schedule(loop, force=True)

    def refresh(self, loop: asyncio.AbstractEventLoop) -> None:
        """The user asked for fresh cards → force a pass now, bypassing the cooldown/threshold.
        Engages the engine even if the template opted out of passive watching, since the user
        explicitly asked."""
        self._since_pass = 0
        self._schedule(loop, force=True)

    def _schedule(self, loop: asyncio.AbstractEventLoop, *, force: bool) -> None:
        loop.create_task(self._maybe_pass(force=force))

    async def _maybe_pass(self, *, force: bool) -> None:
        # A transcript-driven pass in cooldown waits it out FIRST (before taking the lock), so
        # the context you just added still produces a pass — but a forced signal (note/attendee)
        # can run immediately meanwhile. Only one deferred pass waits at a time.
        if not force:
            if self._deferring:
                return  # a deferred pass is already pending — it'll cover this new context
            wait = _COOLDOWN_S - (asyncio.get_event_loop().time() - self._last_pass_mono)
            if wait > 0:
                self._deferring = True
                try:
                    await asyncio.sleep(wait)
                finally:
                    self._deferring = False
        if self._lock.locked():
            return
        async with self._lock:
            now = asyncio.get_event_loop().time()
            if not force and (now - self._last_pass_mono) < _COOLDOWN_S:
                return  # a signal jumped in during our wait and reset the cooldown — yield to it
            if self._passes >= _MAX_PASSES:
                return
            self._last_pass_mono = now
            self._since_pass = 0
            self._passes += 1
            cards = await self._run_agent()
            fresh = [c for c in cards if c["text"].lower() not in self._seen][:_MAX_CARDS_PER_PASS]
            if not fresh:
                return
            for c in fresh:
                self._seen.add(c["text"].lower())
            self._send(self.conn, {"type": "live.context", "streamId": self.stream_id,
                                   "recordingId": self.rec_id, "cards": fresh})

    async def _run_agent(self) -> list[dict]:
        """Run the meeting-live agent through the Work Kernel and parse its card output. Every
        pass for this recording continues ONE session, so the agent keeps its history and won't
        repeat cards it already surfaced."""
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
                       template_ctx=templates_store.live_context(template_ids=tids), kinds=kinds)
        # Fresh event id per pass → a distinct job; stable sessionId → one continuing conversation.
        trigger = {"id": f"live_{self.rec_id}_{self._passes}", "type": "task",
                   "sessionId": f"live_{self.rec_id}", "occurredAt": now_iso(),
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
        return _parse_cards(result.get("output") or "", kinds)
