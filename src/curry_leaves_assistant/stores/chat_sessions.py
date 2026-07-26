"""Chat sessions, file-backed by a curry-leaves-compatible FileSessionStore.

Sessions are stored as <home>/sessions/<id>/{meta.json, transcript.jsonl}
(home is pointed at ~/.curry-leaves/curry-leaves in app.py). We add a sidecar curry-leaves.json
per session for the bits SessionMeta doesn't carry (which agent / model the chat uses),
and reconstruct UI-shaped messages (bubbles + tool cards) from the transcript.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
from pathlib import Path

from curry_leaves_assistant.stores.session_store import FileSessionStore
from curry_leaves.core.messages import assistant_text

from curry_leaves_assistant.core import doc_text


store = FileSessionStore()  # <curry-leaves home>/sessions


# ─── sidecar (agent / model per session) ──────────────────────────────────────
def _sidecar(sid: str):
    return store.root / sid / "curry-leaves.json"


def set_sidecar(sid: str, agent_id: str | None, model: str | None) -> None:
    try:
        existing = get_sidecar(sid)
        existing["agentId"] = agent_id
        existing["model"] = model or ""
        _sidecar(sid).write_text(json.dumps(existing))
    except Exception:
        pass


def get_sidecar(sid: str) -> dict:
    try:
        return json.loads(_sidecar(sid).read_text())
    except Exception:
        return {}


# ─── nightly memory-learner support ───────────────────────────────────────────
# The Memory Keeper agent sweeps chats it hasn't learned from yet, distills durable facts +
# events, then marks each done so tomorrow's sweep skips it. The "learned" flag lives in the
# session sidecar (same place as agentId/model), keyed by the message count at learn time —
# so a conversation that CONTINUES after being learned becomes eligible again, but an unchanged
# one never is.
# How far back the nightly sweep looks. Sessions are newest-first, so this only matters if MORE
# than this many chats gained new content since the last sweep — vanishingly unlikely for a daily
# run, but if it ever happens we log it rather than silently dropping the tail.
_SCAN_WINDOW = 500


def unlearned_sessions(limit: int = 50) -> list[dict]:
    """Direct chats with new user/assistant content the learner hasn't processed yet.

    "Direct" = a real conversation (has an assistant agent + at least one user turn), not the
    hundreds of machine-to-machine runs. Newest first. Each carries `newMessages` so the agent
    can skip trivially short ones."""
    out: list[dict] = []
    scanned = 0
    for m in store.list_recent(_SCAN_WINDOW):
        scanned += 1
        sc = get_sidecar(m.id)
        if not sc.get("agentId"):
            continue  # background/system session, no conversational owner
        if (sc.get("surface") or "chat") != "chat":
            continue  # spoken one-off, not a conversation worth learning a person from
        learned_at = int(sc.get("learnedAtCount") or 0)
        if m.message_count <= learned_at:
            continue  # nothing new since we last learned from it
        # Must contain at least one real user turn — otherwise there's no person to learn about.
        # Cheap check (stops at the first user record) before we commit to a full parse.
        if not _has_user_turn(m.id):
            continue
        out.append({"id": m.id, "title": m.title or "New chat", "agentId": sc.get("agentId"),
                    "messageCount": m.message_count, "newMessages": m.message_count - learned_at,
                    "updatedAt": m.updated_at})
        if len(out) >= limit:
            break
    if scanned >= _SCAN_WINDOW and len(out) >= limit:
        print(f"[memory-keeper] scan window ({_SCAN_WINDOW}) saturated — older unlearned chats "
              f"may be deferred to a later sweep", flush=True)
    return out


def _has_user_turn(sid: str) -> bool:
    """True if the transcript has at least one user record — stops at the first, no full parse."""
    try:
        for line in (store.root / sid / "transcript.jsonl").read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                if json.loads(line).get("kind") == "user":
                    return True
            except ValueError:
                continue
    except OSError:
        pass
    return False


# In-band control markers the UI embeds in message text (attachment/artifact metadata), framed
# by the RS control char (\x1e). They're plumbing, not speech. Strip the whole `<<<…>>>` block,
# then any stray RS chars left behind, then collapse the blank lines that framing leaves.
_CONTROL_MARKER = re.compile(r"<<<curry-leaves:.*?>>>", re.DOTALL)


def _strip_control(text: str) -> str:
    # Drop the marker block, then the separators that framed it — both the RS control char
    # (\x1e) and its visible-symbol form (␞, which the UI actually stores).
    text = _CONTROL_MARKER.sub("", text or "").replace("\x1e", "").replace("␞", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _clean_turns(sid: str) -> list[dict]:
    """A conversation as plain user/assistant turns — the signal, with the plumbing stripped.

    Drops tool_start/tool_end/approval/thinking (HOW the assistant worked, not what was SAID —
    a third of the transcript) and the UI's in-band control markers. What's left is the actual
    exchange the learner should reason over."""
    turns: list[dict] = []
    try:
        for line in (store.root / sid / "transcript.jsonl").read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("kind") not in ("user", "assistant"):
                continue
            text = _strip_control(r.get("text") or "")
            if text:
                turns.append({"role": r["kind"], "text": text})
    except (OSError, ValueError):
        pass
    return turns


def clean_transcript(sid: str) -> str:
    """The conversation as readable text for the learner's prompt (user/assistant only)."""
    return "\n\n".join(f"{t['role'].upper()}: {t['text']}" for t in _clean_turns(sid))


def mark_learned(sid: str) -> None:
    """Record that the learner has processed this session up to its current length, so the
    nightly sweep skips it until it grows."""
    try:
        m = store.load_meta(sid) if store.exists(sid) else None
        sc = get_sidecar(sid)
        sc["learnedAtCount"] = m.message_count if m else sc.get("learnedAtCount", 0)
        _sidecar(sid).write_text(json.dumps(sc))
    except Exception:
        pass


# ─── session-scoped tool approvals ("Allow for this chat") ────────────────────
# Distinct from settings.py's global_approvals (a tool trusted app-wide): these live in
# the session sidecar so a "session" grant survives across turns within THIS chat, but
# a fresh chat starts asking again — matching PermissionEngine's own "session" scope.
def get_tasks(sid: str) -> list[dict]:
    """The session's run-scoped agent task list, read from <session_dir>/tasks.json
    (rewritten by the task_create/task_update tools — see agent_engine._task_tools_for).
    Normalized for the chat UI's Tasks panel. Empty list if none / unreadable."""
    if not sid:
        return []
    try:
        data = json.loads((store.root / sid / "tasks.json").read_text())
    except (OSError, ValueError):
        return []
    return [{"id": t.get("id"), "subject": t.get("subject"), "activeForm": t.get("active_form"),
             "status": t.get("status")} for t in data.get("tasks", [])]


def session_approvals(sid: str) -> list[str]:
    return list(get_sidecar(sid).get("permissions") or [])


def add_session_approval(sid: str, tool: str) -> None:
    try:
        existing = get_sidecar(sid)
        grants = set(existing.get("permissions") or [])
        grants.add(tool)
        existing["permissions"] = sorted(grants)
        _sidecar(sid).write_text(json.dumps(existing))
    except Exception:
        pass


# ─── lifecycle ────────────────────────────────────────────────────────────────
def ensure(session_id: str | None, agent_id: str, model: str | None, first_message: str,
           *, surface: str = "chat") -> str:
    """Return an existing session id or create a new one, recording the agent/model.

    `surface` marks where the turn came from. Anything but "chat" is a real session (it needs
    a transcript so gated tools have somewhere to prompt) that is nonetheless hidden from the
    chat list and the learner — see list_sessions / unlearned_sessions."""
    if session_id and store.exists(session_id):
        sid = session_id
    else:
        meta = store.create(title=(first_message or "New chat")[:48], model=model or "")
        sid = meta.id
    set_sidecar(sid, agent_id, model)
    if surface and surface != "chat":
        try:
            sc = get_sidecar(sid)
            sc["surface"] = surface
            _sidecar(sid).write_text(json.dumps(sc))
        except Exception:
            pass
    return sid


def list_sessions() -> list[dict]:
    """Conversations for the chat list. Non-chat surfaces (voice) keep real sessions but are
    hidden here — a spoken one-off isn't something the user goes back and reads."""
    out = []
    for m in store.list_recent(100):
        sc = get_sidecar(m.id)
        if (sc.get("surface") or "chat") != "chat":
            continue
        out.append({
            "id": m.id,
            "title": m.title or "New chat",
            "agentId": sc.get("agentId"),
            "model": sc.get("model") or m.model or "",
            "messageCount": m.message_count,
            "updatedAt": m.updated_at,
        })
    return out


def fork(source_id: str, *, upto_turn: int | None = None) -> str:
    """Branch a brand-new session off `source_id`'s transcript, up through the `upto_turn`-th
    user turn (0-indexed; None keeps the whole conversation). The fork is a fully independent
    session from here — continuing it never touches the source's transcript.

    Reuses curry-leaves's own fork_session (session/replay.py) for the transcript copy +
    turn-boundary math, since both this store and the kernel resolve the same on-disk
    <CURRY_LEAVES_HOME>/sessions root (app.py points CURRY_LEAVES_HOME at the same dir this
    store's `root` derives from). The app's own sidecars (app-meta.json, curry-leaves.json)
    are what list_sessions/ensure actually read, so this creates the session the normal way
    first — store.create() writes an empty transcript.jsonl — and fork_session then reuses
    that same directory, appending the copied (possibly truncated) transcript into it."""
    from curry_leaves.session import SessionMeta as CLSessionMeta, fork_session
    from curry_leaves_assistant.stores.session_store import _records_from_jsonl

    sc = get_sidecar(source_id)
    src_meta = store.load_meta(source_id) if store.exists(source_id) else None
    src_title = (src_meta.title if src_meta else "") or "New chat"
    title = src_title if upto_turn is None else f"{src_title} (fork)"
    model = (src_meta.model if src_meta else "") or sc.get("model") or ""
    new_meta = store.create(title=title[:48], model=model, parent_id=source_id)
    fork_session(source_id, new_meta.id, CLSessionMeta(model=model, provider="", cwd=""), upto_turn=upto_turn)
    records = _records_from_jsonl(store._transcript_path(new_meta.id))
    new_meta.message_count = sum(1 for r in records if r.get("kind") in ("user", "assistant", "tool_end"))
    store._write_meta(new_meta)
    set_sidecar(new_meta.id, sc.get("agentId"), model)
    return new_meta.id


def delete(sid: str) -> bool:
    d = store.root / sid
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False


def replace_with_summary(sid: str, summary: str) -> None:
    """Compaction: drop the transcript and keep a single summary message."""
    store.truncate(sid, 0)
    store.append(sid, assistant_text(summary))


# ─── file attachments (per-session, converted to markdown) ────────────────────
# Each session keeps its uploads under <store.root>/<sid>/files/. The original is
# saved verbatim; a markdown rendering sits alongside it as "<name>.md", which is
# what the agent actually reads (inlined into the user turn — see augment_input).
# The doc→markdown renderer is shared with recordings (see doc_text.py).
def files_dir(sid: str) -> Path:
    d = store.root / sid / "files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def attach_file(sid: str, filename: str, raw: bytes) -> dict:
    """Save an upload under the session and return its metadata.

    The original bytes are always saved verbatim. For types we send to the LLM as
    native content blocks (images, PDFs, wav/mp3) we DON'T pre-render markdown — the
    raw file is what the model sees, and a text rendering is only produced lazily if a
    fallback is ever needed (see build_user_message / _attachment_markdown). Text-like
    and unsupported types still render to markdown eagerly, as before.

    The returned ``mdPath`` is the id used by later turns; it is the ".md" sidecar for
    text attachments and the original filename for native ones. ``chars`` is only
    present when we rendered markdown up front."""
    d = files_dir(sid)
    orig = doc_text.unique(d, doc_text.safe_name(filename))
    orig.write_bytes(raw)
    kind = doc_text.artifact_kind(orig.suffix)
    if kind == "text":
        md = doc_text.to_markdown(orig, raw)
        md_path = orig.with_suffix(orig.suffix + ".md")
        md_path.write_text(md, encoding="utf-8")
        return {"name": orig.name, "size": len(raw), "mdPath": md_path.name, "chars": len(md)}
    # Native artifact: the id points at the original file; no eager markdown render.
    return {"name": orig.name, "size": len(raw), "mdPath": orig.name, "kind": kind}


def _resolve_attachment(sid: str, mp: str) -> Path | None:
    """Map an attachment id (basename from attach_file) to its file, refusing any
    path that escapes the session's files dir. Returns the resolved Path or None."""
    d = files_dir(sid)
    p = (d / Path(mp).name).resolve()
    if d.resolve() not in p.parents or not p.is_file():
        return None
    return p


def read_attachment(sid: str, md_path: str) -> tuple[str, str] | None:
    """Return (display name, markdown) for a stored attachment, or None if missing.

    md_path is the basename returned by attach_file; we refuse anything that tries
    to escape the session's files dir. Accepts either a ".md" sidecar (text
    attachments) or a native original (rendered to markdown on the fly for fallback)."""
    p = _resolve_attachment(sid, md_path)
    if p is None:
        return None
    if p.name.endswith(".md"):
        return p.name[:-3], p.read_text(encoding="utf-8", errors="replace")
    # Native artifact used on the text-fallback path: render it now.
    return p.name, doc_text.to_markdown(p, p.read_bytes())


# ─── attachment inlining (persisted in the user turn, parsed back for the UI) ──
_ATT_OPEN = "␞<<<curry-leaves:attachment {meta}>>>␞"
_ATT_RE = re.compile(r"␞<<<curry-leaves:attachment (\{.*?\})>>>␞\n(.*?)\n␞<<<curry-leaves:/attachment>>>␞",
                     re.DOTALL)
# @-mention preamble (chat_runs._references_block). Same in-band-marker trick as attachments:
# the agent sees the handles, the UI strips them back out into chips on reload.
_REF_OPEN = "␞<<<curry-leaves:references {meta}>>>␞"
_REF_RE = re.compile(r"␞<<<curry-leaves:references (\{.*?\})>>>␞\n(.*?)\n␞<<<curry-leaves:/references>>>␞",
                     re.DOTALL)


def augment_input(sid: str, message: str, md_paths: list[str]) -> tuple[str, list[dict]]:
    """Inline each attachment's markdown into the user turn so the agent sees it.

    Returns (text to send/persist, attachment metadata for the UI bubble). The
    inlined blocks are wrapped in sentinels so get_messages can strip them on reload."""
    blocks, metas = [], []
    for mp in md_paths or []:
        got = read_attachment(sid, mp)
        if got is None:
            continue
        name, md = got
        metas.append({"name": name})
        head = _ATT_OPEN.format(meta=json.dumps({"name": name}))
        blocks.append(f"{head}\n{md}\n␞<<<curry-leaves:/attachment>>>␞")
    if not blocks:
        return message, []
    body = "\n\n".join(blocks)
    text = f"{message}\n\n{body}" if message.strip() else body
    return text, metas


# ─── native multimodal turns (send artifacts to the LLM directly, no md round-trip) ──
# Which native block kinds each provider can actually ingest. A (provider, kind) not
# listed here falls back to the markdown text path so attachments keep working on every
# model (see the fallback in build_user_message). Mirrors the providers' own wire builders
# in curry_leaves: Anthropic takes image+document but rejects audio; OpenAI takes all three
# (Chat Completions image_url / file / input_audio); text-only providers take none.
_NATIVE_SUPPORT: dict[str, set[str]] = {
    "anthropic": {"image", "pdf"},
    "openai": {"image", "pdf", "audio"},
    "copilot": {"image"},   # OpenAI-compatible gateway; images are broadly available, file/audio are not
    "codex": {"image", "pdf"},   # ChatGPT Responses API: input_image + input_file (see responses_wire)
    "google": {"image", "pdf"},
    "ollama": set(),        # local models: assume text-only unless proven otherwise
}


def _import_message_types():
    from curry_leaves.core.messages import (  # local import: kernel is a heavy dep
        AudioBlock, FileBlock, ImageBlock, TextBlock, UserMessage,
    )
    return UserMessage, TextBlock, ImageBlock, FileBlock, AudioBlock


# Native blocks carry no display name of their own (except FileBlock.filename), so we
# stamp one in a tiny provider-neutral sentinel appended to the turn's text. get_messages
# reads it back to render the attachment chips on reload — same idea as _ATT_RE, but the
# body lives in the block, not the text.
_NATIVE_META_RE = re.compile(r"␞<<<curry-leaves:artifact (\{.*?\})>>>␞")


def build_user_message(sid: str, message: str, md_paths: list[str], provider: str):
    """Build a curry_leaves UserMessage for this turn, sending each attachment to the
    model in its best form: images/PDFs/audio as native content blocks when the run's
    provider accepts them, everything else (and unsupported combos) inlined as markdown.

    Returns (UserMessage, ui_metas). Falls back to a plain text turn if the kernel's
    multimodal types aren't importable, so a stale kernel can't break chat."""
    try:
        UserMessage, TextBlock, ImageBlock, FileBlock, AudioBlock = _import_message_types()
    except Exception:
        text, metas = augment_input(sid, message, md_paths)
        return None, metas  # signal: caller uses text path

    supported = _NATIVE_SUPPORT.get(provider, set())
    blocks: list = []
    metas: list[dict] = []
    md_fallbacks: list[str] = []      # ids that must go through the markdown text path
    native_names: list[str] = []      # chip names for artifacts sent natively

    for mp in md_paths or []:
        p = _resolve_attachment(sid, mp)
        if p is None:
            continue
        kind = doc_text.artifact_kind(p.suffix)
        if kind == "text" or kind not in supported:
            md_fallbacks.append(mp)
            continue
        source = base64.b64encode(p.read_bytes()).decode()
        if kind == "image":
            blocks.append(ImageBlock(source=source, media_type=doc_text.media_type(p)))
        elif kind == "pdf":
            blocks.append(FileBlock(source=source, media_type="application/pdf", filename=p.name))
        elif kind == "audio":
            fmt = doc_text.audio_format(p.suffix) or "wav"
            blocks.append(AudioBlock(source=source, format=fmt))
        native_names.append(p.name)
        metas.append({"name": p.name})

    # Markdown fallbacks (text files + provider-unsupported natives) inline into the text.
    text, fb_metas = augment_input(sid, message, md_fallbacks)
    metas.extend(fb_metas)
    # Stamp native chip names so reload can rebuild them (native blocks have no md sentinel).
    if native_names:
        tag = "␞<<<curry-leaves:artifact {meta}>>>␞".format(
            meta=json.dumps({"names": native_names}))
        text = f"{text}\n\n{tag}" if text.strip() else tag

    content: list = []
    if text.strip():
        content.append(TextBlock(text=text))
    content.extend(blocks)
    if not content:
        content.append(TextBlock(text=message))
    return UserMessage(content=content), metas


def _split_attachments(content: str) -> tuple[str, list[dict], list[dict]]:
    """Inverse of augment_input/build_user_message/_references_block: pull attachment chips
    and @-reference chips out of a user turn's text, leaving what the user actually typed.
    Returns (clean text, attachment metas, reference metas)."""
    metas = []
    for m in _ATT_RE.finditer(content):
        try:
            metas.append(json.loads(m.group(1)))
        except Exception:
            metas.append({"name": "attachment"})
    for m in _NATIVE_META_RE.finditer(content):
        try:
            for name in json.loads(m.group(1)).get("names", []):
                metas.append({"name": name})
        except Exception:
            metas.append({"name": "attachment"})
    refs: list[dict] = []
    for m in _REF_RE.finditer(content):
        try:
            refs.extend(json.loads(m.group(1)).get("refs", []))
        except Exception:
            pass
    clean = _REF_RE.sub("", _NATIVE_META_RE.sub("", _ATT_RE.sub("", content))).strip()
    return clean, metas, refs


# ─── transcript → UI messages ─────────────────────────────────────────────────
def _text(msg) -> str:
    return "".join(
        getattr(b, "text", "") or ""
        for b in getattr(msg, "content", [])
        if getattr(b, "type", "") not in ("thinking", "signature")
    )


def _thinking(msg) -> str:
    return "".join(
        getattr(b, "thinking", "") or ""
        for b in getattr(msg, "content", [])
        if getattr(b, "type", "") == "thinking"
    )


def _is_tool_call(b) -> bool:
    return hasattr(b, "arguments") and hasattr(b, "name") and hasattr(b, "id")


def _parse_ts(raw) -> float | None:
    """A record's ISO-8601 `ts` → epoch seconds (None if absent/unparseable)."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(raw).timestamp()
    except Exception:
        return None


def _accumulate_usage(bubble: dict, rec: dict) -> None:
    """Fold one assistant step's usage record into the turn's bubble.

    A turn is many model calls (tool use → another call → …), and the live stream reports
    input/output SUMMED across them — the billed cost. Replay mirrors that by adding each
    step, so a reloaded turn shows the same totals it did live.

    `elapsedMs` is derived from record timestamps: the span from the turn's first step to
    its last. That's the closest recoverable analogue of the live wall clock — it excludes
    the leading gap before the first assistant record, which is why a replayed turn can
    read slightly shorter than it did live."""
    u = rec.get("usage")
    ts = _parse_ts(rec.get("ts"))
    if ts is not None:
        # Keep the turn's first and last stamps; the delta is its duration.
        if "_t0" not in bubble:
            bubble["_t0"] = ts
        bubble["_t1"] = ts
    if not isinstance(u, dict):
        return
    usage = bubble.setdefault("usage", {"input": 0, "output": 0})
    usage["input"] += int(u.get("input") or 0)
    usage["output"] += int(u.get("output") or 0)
    # The LAST step's own input+output is what the model actually held — same definition
    # the live `usage` frame uses for `context`, so the meter reads identically on reload.
    usage["context"] = int(u.get("input") or 0) + int(u.get("output") or 0)


def _finalize_usage(bubble: dict, model: str) -> None:
    """Stamp the model + elapsed onto a finished bubble and drop the scratch timestamps."""
    t0, t1 = bubble.pop("_t0", None), bubble.pop("_t1", None)
    usage = bubble.get("usage")
    if not isinstance(usage, dict):
        return
    if model:
        usage.setdefault("model", model)
    if t0 is not None and t1 is not None and t1 > t0:
        usage["elapsedMs"] = int((t1 - t0) * 1000)


def get_messages(sid: str) -> list[dict]:
    """Transcript → UI bubbles, ONE assistant bubble per turn. A turn spans many model
    steps (assistant message with tool calls, tool results, next assistant message, …)
    and the live SSE stream accumulates all of them into a single bubble — so replay
    merges consecutive assistant/tool_result messages the same way. Keeps reloaded
    history looking like it did on screen, with the whole turn's tool calls under one
    ToolGroup instead of one fragment per step.

    Each bubble also carries the turn's `usage` (tokens, model, elapsed) so reloaded
    history shows the same footer the live turn did. That data lives on the raw records —
    the message objects `store.load()` builds model only what the LLM sees — so we read
    both and pair them: `records_to_messages` emits exactly one message per user/assistant/
    tool_end record, in order, and skips every other kind, so filtering to those kinds
    realigns the two lists index-for-index."""
    ui: list[dict] = []
    by_id: dict[str, dict] = {}
    cur: dict | None = None  # assistant bubble accumulating the current turn's steps
    recs = [r for r in store.load_records(sid) if r.get("kind") in ("user", "assistant", "tool_end")]
    msgs = store.load(sid)
    for i, m in enumerate(msgs):
        rec = recs[i] if i < len(recs) else {}
        role = getattr(m, "role", "")
        if role == "user":
            content, atts, refs = _split_attachments(_text(m))
            msg: dict = {"role": "user", "content": content}
            if atts:
                msg["attachments"] = atts
            if refs:
                msg["references"] = refs
            ui.append(msg)
            cur = None
        elif role == "assistant":
            if cur is None:
                cur = {"role": "assistant", "content": ""}
            for b in getattr(m, "content", []):
                # `trail` records the turn's real order — thinking segments interleaved
                # with tool calls — so the UI's collapsed ToolGroup can replay it as a
                # timeline (thought → tools → thought → …) instead of one merged blob.
                if getattr(b, "type", "") == "thinking":
                    seg = getattr(b, "thinking", "") or ""
                    if seg:
                        trail = cur.setdefault("trail", [])
                        if trail and trail[-1]["kind"] == "thinking":
                            trail[-1]["text"] += f"\n\n{seg}"
                        else:
                            trail.append({"kind": "thinking", "text": seg})
                # ask/approve render as PendingCards live; task_* render as the Tasks panel
                # (restored via GET /sessions/{sid}/tasks) — neither replays as a tool card.
                elif _is_tool_call(b) and b.name not in (
                        "ask", "approve", "task_create", "task_update", "task_list", "task_get"):
                    tc = {"id": b.id, "name": b.name, "status": "done",
                          "input": json.dumps(b.arguments, indent=2, ensure_ascii=False, default=str)}
                    cur.setdefault("tools", []).append(tc)
                    cur.setdefault("trail", []).append({"kind": "tool", "id": b.id})
                    by_id[b.id] = tc
            text = _text(m)
            if text:
                cur["content"] = f"{cur['content']}\n\n{text}" if cur["content"] else text
            thinking = _thinking(m)
            if thinking:
                cur["thinking"] = f"{cur['thinking']}\n\n{thinking}" if cur.get("thinking") else thinking
            _accumulate_usage(cur, rec)
            # Append lazily, once the bubble first gains something visible; later steps
            # keep mutating the same dict in place.
            if (cur["content"] or cur.get("tools") or cur.get("thinking")) and (not ui or ui[-1] is not cur):
                ui.append(cur)
        elif role == "tool_result":
            tc = by_id.get(getattr(m, "tool_call_id", "") or "")
            if tc is not None:
                tc["output"] = _text(m)
                if getattr(m, "is_error", False):
                    tc["status"] = "error"
    # The transcript records tokens but not which model produced them. Resolve it the
    # same way list_sessions() does — sidecar first, then meta — and stamp it (with the
    # derived elapsed) once every bubble is complete. Either may be blank on older
    # sessions, in which case the footer just omits the model.
    model = ""
    try:
        model = get_sidecar(sid).get("model") or ""
    except Exception:
        pass
    if not model:
        try:
            model = store.load_meta(sid).model or ""
        except Exception:
            model = ""
    for b in ui:
        if b.get("role") == "assistant":
            _finalize_usage(b, model)
    return ui
