"""Recording storage: one dir per recording under ~/.curry-leaves/recordings/<id>/.

Audio is streamed in as webm chunks during capture, then finalized. Metadata is a
plain meta.json. Finalizing and transcribing each emit an event.
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from curry_leaves_assistant.core import doc_text

from curry_leaves_assistant.core import events

from curry_leaves_assistant.core import settings as app_settings

from curry_leaves_assistant.core.paths import RECORDINGS_DIR, rec_dir, rec_meta_path, rec_audio_path, rec_transcript_path, rec_outputs_dir
from curry_leaves_assistant.core.store import read_json, write_json, now_iso


def _meta(rec_id: str) -> dict | None:
    return read_json(rec_meta_path(rec_id), None)


def create_draft(name: str | None = None, template_id: str | None = None, language: str | None = None) -> dict:
    rec_id = uuid.uuid4().hex
    rec_dir(rec_id).mkdir(parents=True, exist_ok=True)
    rec_audio_path(rec_id).write_bytes(b"")  # start empty for chunked appends
    from curry_leaves_assistant.stores import templates_store

    tid = template_id or templates_store.default_template_id()
    tids = [tid] if tid else []
    meta = {
        "id": rec_id,
        "name": name or "Untitled recording",
        "status": "recording",
        "createdAt": now_iso(),
        "savedAt": None,
        "duration": None,
        "tags": [],
        "notes": "",                 # user's freeform notes for this recording
        "links": [],                 # [{url, title?}]
        "attachments": [],           # [{name, mdPath, size, chars}]
        "attendees": [],             # people in the meeting (names); editable while recording
        "organizer": None,           # whose meeting it is — one of `attendees`, or None
        "saveToKnowledge": True,     # file this meeting into the knowledge hub
        # The meeting template(s) driving the copilot's outputs. `templateId` is the primary
        # (owns the summary + kept for back-compat); `templateIds` is the full set (a recording
        # can use several). Both are switchable live and editable after the fact.
        "templateId": tid,
        "templateIds": tids,
        # Which agents process this recording, derived from the template(s) + always-agents.
        # Stamped so a later template edit doesn't retroactively change past recordings.
        "agentIds": templates_store.resolved_agent_ids(template_ids=tids),
        # Per-recording transcription language override; falsy -> falls back live to
        # Settings → Recording language (re-resolved on every re-transcription).
        "language": language,
        "audioPath": str(rec_audio_path(rec_id)),
        "transcriptPath": None,
        "transcript": None,
        "summary": None,
        "actionItems": [],
    }
    write_json(rec_meta_path(rec_id), meta)
    events.emit("recording.created", payload=meta, entity_id=rec_id, label=meta["name"])
    return meta


def append_chunk(rec_id: str, chunk: bytes) -> None:
    """No-op if the recording was already deleted (e.g. cancelled mid-flight) — a
    trailing chunk upload racing the delete shouldn't surface as a server error."""
    path = rec_audio_path(rec_id)
    if not path.parent.exists():
        return
    with path.open("ab") as f:
        f.write(chunk)


def finalize(rec_id: str, *, name: str | None = None, duration: float | None = None) -> dict | None:
    meta = _meta(rec_id)
    if meta is None:
        return None
    if name:
        meta["name"] = name
    meta["duration"] = duration
    meta["status"] = "saved"
    meta["savedAt"] = now_iso()
    write_json(rec_meta_path(rec_id), meta)
    events.emit("recording.finalized", payload=meta, entity_id=rec_id, label=meta["name"])
    return meta


def save_transcript(rec_id: str, text: str, segments: list[dict] | None = None) -> dict | None:
    meta = _meta(rec_id)
    if meta is None:
        return None
    rec_transcript_path(rec_id).write_text(text)
    meta["transcriptPath"] = str(rec_transcript_path(rec_id))
    meta["transcript"] = text
    # Normalize into an immutable, indexed transcript (turns) — the provenance anchor
    # for the knowledge base. Deterministic meeting_id makes re-processing idempotent.
    if segments is not None:
        try:
            from curry_leaves_assistant.stores import transcripts

            meta["meetingId"] = transcripts.store(
                rec_id, segments, title=meta.get("name"), date=(meta.get("savedAt") or meta.get("createdAt")))
            meta["turnCount"] = len(segments)
        except Exception as exc:  # normalization is best-effort; never block transcription
            print(f"[transcripts] normalize failed for {rec_id}: {exc}", flush=True)
    write_json(rec_meta_path(rec_id), meta)
    # Carry the transcript text + meetingId in the payload so a triggered agent works standalone.
    events.emit("recording.transcribed", payload=meta, entity_id=rec_id, label=meta["name"])
    return meta


def resubmit(rec_id: str) -> dict | None:
    """Re-emit recording.transcribed so the agent pool reprocesses it (re-summarize,
    re-extract action items, etc.). Carries the full transcript in the payload."""
    meta = _meta(rec_id)
    if meta is None or not meta.get("transcript"):
        return None
    events.emit("recording.transcribed", payload=meta, entity_id=rec_id, label=meta["name"])
    return meta


def set_summary(rec_id: str, summary: str) -> dict | None:
    """Persist a summary onto a recording (used by the summarizer agent).

    Compat wrapper over the generic outputs store: writes outputs/meeting-summarizer.md
    like any other agent output, and mirrors the text onto meta.summary so existing
    consumers (CaughtCard, status chips, recording.summarized subscribers) keep working.
    """
    meta = _meta(rec_id)
    if meta is None:
        return None
    _write_output_file(rec_id, "meeting-summarizer", "Summary", summary, emit_event=False)
    meta["summary"] = summary
    write_json(rec_meta_path(rec_id), meta)
    events.emit("recording.summarized", payload=meta, entity_id=rec_id, label=meta["name"])
    return meta


# ─── agent outputs (markdown files under outputs/) ─────────────────────────────
# One file per (agent, section). The file STEM is the output "key" that tabs are keyed on:
# "<agentId>" for a single unsectioned output, or "<agentId>.<section>" for a template
# section. The Meeting Copilot produces several sections, each its own file; other agents
# keep one file (no section) exactly as before.
def _slug(s: str) -> str:
    safe = "".join(c for c in s if c.isalnum() or c in "_-").strip("_-")
    return safe


def _output_path(rec_id: str, agent_id: str, section: str | None = None):
    a = _slug(agent_id)
    if not a:
        raise ValueError(f"invalid agent id for output: {agent_id!r}")
    if section:
        s = _slug(section)
        if not s:
            raise ValueError(f"invalid section for output: {section!r}")
        return rec_outputs_dir(rec_id) / f"{a}.{s}.md"
    return rec_outputs_dir(rec_id) / f"{a}.md"


def _write_output_file(rec_id: str, agent_id: str, title: str, content: str,
                       job_id: str | None = None, section: str | None = None,
                       emit_event: bool = True) -> dict:
    import yaml
    fm = {"agent": agent_id, "title": title or agent_id, "updatedAt": now_iso()}
    if section:
        fm["section"] = section
    if job_id:
        fm["jobId"] = job_id
    front = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    p = _output_path(rec_id, agent_id, section)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{front}\n---\n\n{content.strip()}\n", encoding="utf-8")
    out = {"agentId": agent_id, "key": p.stem, **fm}
    if emit_event:
        meta = _meta(rec_id)
        events.emit("recording.output.saved",
                    payload={"id": rec_id, "agentId": agent_id, "key": p.stem, "title": fm["title"]},
                    entity_id=rec_id, label=(meta or {}).get("name"))
    return out


def save_output(rec_id: str, agent_id: str, title: str, content: str,
                job_id: str | None = None, section: str | None = None) -> dict | None:
    """Save/replace one of an agent's artifacts for a recording. Without a section, the
    agent owns a single outputs/<agentId>.md (the classic behavior). With a section, the
    agent owns one file per section (outputs/<agentId>.<section>.md) — this is how the
    Meeting Copilot emits a template's multiple sections. Emits recording.output.saved.
    A `summary` section is routed to save_summary so the meta.summary mirror stays live."""
    if _meta(rec_id) is None:
        return None
    if section == "summary":
        set_summary(rec_id, content)
        return {"agentId": agent_id, "key": "meeting-summarizer", "title": title or "Summary"}
    return _write_output_file(rec_id, agent_id, title, content, job_id=job_id, section=section)


def _parse_output_file(p) -> dict | None:
    from curry_leaves_assistant.stores.agent_store import parse_frontmatter
    try:
        fm, body = parse_frontmatter(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    agent_id = fm.get("agent") or p.stem
    return {
        "agentId": agent_id,
        "key": p.stem,               # tab identity — unique per (agent, section)
        "section": fm.get("section"),
        "title": fm.get("title") or agent_id,
        "updatedAt": fm.get("updatedAt"),
        "jobId": fm.get("jobId"),
        "content": body.strip(),
    }


def list_outputs(rec_id: str) -> list[dict]:
    """All agent artifacts for a recording (parsed frontmatter + content)."""
    d = rec_outputs_dir(rec_id)
    if not d.is_dir():
        return []
    out = [o for p in sorted(d.glob("*.md")) if (o := _parse_output_file(p))]
    out.sort(key=lambda o: o.get("updatedAt") or "")
    return out


def output_keys(rec_id: str) -> list[str]:
    """The output-file keys (stems) present — one per (agent, section). Cheap dir listing."""
    d = rec_outputs_dir(rec_id)
    return sorted(p.stem for p in d.glob("*.md")) if d.is_dir() else []


def delete_output(rec_id: str, key: str) -> bool:
    """Delete one output by its file key (the stem: <agentId> or <agentId>.<section>)."""
    # Keys are dotted slugs; reject anything that could escape the outputs dir.
    if not key or "/" in key or "\\" in key or not all(c.isalnum() or c in "._-" for c in key):
        return False
    p = rec_outputs_dir(rec_id) / f"{key}.md"
    existed = p.exists()
    p.unlink(missing_ok=True)
    if existed:
        meta = _meta(rec_id)
        if meta is not None and key == "meeting-summarizer" and meta.get("summary"):
            meta["summary"] = None  # keep the legacy summary mirror consistent
            write_json(rec_meta_path(rec_id), meta)
        events.emit("recording.updated", payload=meta or {"id": rec_id}, entity_id=rec_id,
                    label=(meta or {}).get("name"))
    return existed


def update(rec_id: str, patch: dict) -> dict | None:
    meta = _meta(rec_id)
    if meta is None:
        return None
    meta.update(patch)
    # Normalize the two free-text fields the UI now writes directly. Done here rather than in the
    # route because agents/agent_tools.py calls update() too, and a blank name or a tag list with
    # a stray "" would otherwise reach disk and show up as an unnamed row / an empty tag group.
    if "name" in patch:
        name = str(patch.get("name") or "").strip()
        meta["name"] = name or "Untitled recording"  # the sentinel the auto-titler looks for
    if "tags" in patch:
        # Order is meaningful — the Recordings rail groups a recording under tags[0] — so dedupe
        # in place instead of sorting. Case-insensitive, keeping the spelling the user typed.
        seen: set[str] = set()
        tags: list[str] = []
        for raw in (patch.get("tags") or []):
            tag = str(raw).strip()
            key = tag.lower()
            if tag and key not in seen:
                seen.add(key)
                tags.append(tag)
        meta["tags"] = tags
    # The organizer is one of the attendees, so the two fields have to stay consistent however
    # they're patched: dropping someone from the attendee list clears the organizer rather than
    # leaving a name pointing at nobody. Matched case-insensitively, then snapped to the
    # attendee-list spelling so the UI can compare with ===.
    if "organizer" in patch or "attendees" in patch:
        attendees = [a for a in (meta.get("attendees") or []) if isinstance(a, str)]
        organizer = meta.get("organizer")
        want = str(organizer or "").strip().lower()
        meta["organizer"] = next((a for a in attendees if a.strip().lower() == want), None) if want else None
    # Changing the template selection (add/remove one live or after the fact) re-derives which
    # agents process the recording and keeps templateId/templateIds in sync. Either field may
    # be sent; the array is authoritative, with templateId as its first element.
    if "templateId" in patch or "templateIds" in patch:
        from curry_leaves_assistant.stores import templates_store
        if "templateIds" in patch:
            tids = [t for t in (patch.get("templateIds") or []) if isinstance(t, str)]
        else:
            tid = patch.get("templateId")
            tids = [tid] if tid else []
        meta["templateIds"] = tids
        meta["templateId"] = tids[0] if tids else None
        meta["agentIds"] = templates_store.resolved_agent_ids(template_ids=tids)
    write_json(rec_meta_path(rec_id), meta)
    return meta


def get(rec_id: str) -> dict | None:
    meta = _meta(rec_id)
    if meta is not None:
        # Computed at read time (never persisted): which agents have artifacts.
        meta["outputs"] = output_keys(rec_id)
    return meta


# ─── attachments (documents the user adds to a recording) ─────────────────────
def _files_dir(rec_id: str) -> Path:
    d = rec_dir(rec_id) / "files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def attach_file(rec_id: str, filename: str, raw: bytes) -> dict | None:
    """Save a document onto a recording, render it to markdown (so agents can read it),
    and record its metadata. Mirrors chat_sessions.attach_file."""
    meta = _meta(rec_id)
    if meta is None:
        return None
    d = _files_dir(rec_id)
    orig = doc_text.unique(d, doc_text.safe_name(filename))
    orig.write_bytes(raw)
    md = doc_text.to_markdown(orig, raw)
    md_path = orig.with_suffix(orig.suffix + ".md")
    md_path.write_text(md, encoding="utf-8")
    att = {"name": orig.name, "mdPath": md_path.name, "size": len(raw), "chars": len(md)}
    meta.setdefault("attachments", []).append(att)
    write_json(rec_meta_path(rec_id), meta)
    events.emit("recording.updated", payload=meta, entity_id=rec_id, label=meta["name"])
    return att


def remove_attachment(rec_id: str, name: str) -> dict | None:
    meta = _meta(rec_id)
    if meta is None:
        return None
    kept = [a for a in (meta.get("attachments") or []) if a.get("name") != name]
    meta["attachments"] = kept
    d = _files_dir(rec_id)
    # Sanitize before joining into a path — the same normalization attach_file applied
    # when it saved the file, so a crafted name can't point unlink() outside this dir.
    safe = doc_text.safe_name(name)
    for p in (d / safe, d / f"{safe}.md"):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    write_json(rec_meta_path(rec_id), meta)
    events.emit("recording.updated", payload=meta, entity_id=rec_id, label=meta["name"])
    return meta


def _attachment_md(rec_id: str, att: dict) -> str:
    p = _files_dir(rec_id) / (att.get("mdPath") or "")
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def agent_context(rec_id: str, include_outputs: bool = False) -> str:
    """Everything an agent should see about a recording, as one labeled text block:
    name, tags, the user's own notes, links, attached-document text, and the transcript.
    The single source of truth so the summarizer, read_recording, and knowledge ingest
    all work from the same context.

    include_outputs appends each agent-produced artifact (outputs/*.md) as its own
    labeled section. Keep it OFF for agents triggered on recording.transcribed — they
    run in parallel, so seeing a sibling's (or their own previous) output would make
    their input timing-dependent. Turn it ON for on-demand reads (chat's read_recording)
    and post-completion stages (the Knowledge Keeper)."""
    meta = _meta(rec_id)
    if meta is None:
        return ""
    parts = [f"Recording id: {rec_id}", f"Name: {meta.get('name')}"]
    ident = app_settings.identity_cfg()
    if ident.get("name"):
        who = f"Operator (the person who recorded this, not necessarily the only speaker): {ident['name']}"
        if ident.get("work"):
            who += f" — {ident['work']}"
        parts.append(who)
    if meta.get("duration") is not None:
        parts.append(f"Duration (s): {meta.get('duration')}")
    if meta.get("tags"):
        parts.append("Tags: " + ", ".join(meta["tags"]))
    attendees = [a for a in (meta.get("attendees") or []) if isinstance(a, str) and a.strip()]
    if attendees:
        parts.append("Attendees (attribute action items and owners to these people when named): "
                     + ", ".join(attendees))
    if (meta.get("organizer") or "").strip():
        parts.append(f"Organizer (whose meeting this is; owns follow-ups nobody else claimed): {meta['organizer']}")
    if (meta.get("notes") or "").strip():
        parts.append(f"\nUser notes:\n{meta['notes'].strip()}")
    links = meta.get("links") or []
    if links:
        parts.append("\nLinks:\n" + "\n".join(
            f"- {l.get('url')}" + (f" — {l.get('title')}" if l.get("title") else "") for l in links))
    for att in (meta.get("attachments") or []):
        body = _attachment_md(rec_id, att).strip()
        if body:
            parts.append(f"\nAttached document — {att.get('name')}:\n{body}")
    parts.append(f"\nTranscript:\n{meta.get('transcript') or '(empty)'}")
    if include_outputs:
        for o in list_outputs(rec_id):
            if o.get("content"):
                when = f", updated {o['updatedAt']}" if o.get("updatedAt") else ""
                parts.append(f"\nAgent output — {o['title']} (agent: {o['agentId']}{when}):\n{o['content']}")
    return "\n".join(parts)


def provenance_context(rec_id: str) -> str:
    """Extra context for the Knowledge Filer: the meeting id (for the note's `source:`
    frontmatter), an indexed turn listing (so it can cite turn ranges), and — for
    idempotency — any notes a prior ingest of this same meeting already produced."""
    meta = _meta(rec_id)
    if meta is None:
        return ""
    meeting_id = meta.get("meetingId")
    if not meeting_id:
        return ""
    from curry_leaves_assistant.domain import knowledge

    from curry_leaves_assistant.stores import transcripts

    parts = [f"\nMeeting id (for source: frontmatter): {meeting_id}"]
    prior = knowledge.ingested_notes(meeting_id)
    if prior:
        parts.append("This meeting was ALREADY filed into these notes — EDIT them with any "
                     "genuinely new facts instead of creating duplicates:\n"
                     + "\n".join(f"- {p}" for p in prior))
    doc = transcripts.load(meeting_id)
    turns = (doc or {}).get("turns") or []
    if turns:
        lines = "\n".join(f"[{t['i']}] {t['text']}" for t in turns)
        parts.append("Indexed transcript turns (set turn_range in source: to the [i] range a "
                     f"fact came from):\n{lines}")
    return "\n".join(parts)


def current_audio_path(rec_id: str):
    """The audio file currently in use (edited WAV if present, else the original webm)."""
    from pathlib import Path
    meta = _meta(rec_id)
    if meta and meta.get("audioPath"):
        p = Path(meta["audioPath"])
        if p.exists():
            return p
    return rec_audio_path(rec_id)


def save_audio(rec_id: str, wav_bytes: bytes, duration: float | None) -> dict | None:
    """Replace the recording's audio with an edited mono WAV (trim / add-recording)."""
    meta = _meta(rec_id)
    if meta is None:
        return None
    wav = rec_dir(rec_id) / "audio.wav"
    wav.write_bytes(wav_bytes)
    meta["audioPath"] = str(wav)
    meta["duration"] = duration
    write_json(rec_meta_path(rec_id), meta)
    events.emit("recording.updated", payload=meta, entity_id=rec_id, label=meta["name"])
    return meta


def delete_audio(rec_id: str) -> dict | None:
    """Remove the audio file(s) but keep the recording (transcript/summary/todos)."""
    meta = _meta(rec_id)
    if meta is None:
        return None
    for p in rec_dir(rec_id).glob("audio*.*"):
        try:
            p.unlink()
        except Exception:
            pass
    meta["audioPath"] = None
    meta["duration"] = None
    write_json(rec_meta_path(rec_id), meta)
    events.emit("recording.updated", payload=meta, entity_id=rec_id, label=meta["name"])
    return meta


# A draft is worth offering to recover only if it captured a real amount of audio; below this
# it's a false start (opened the recorder, spoke nothing, quit) and we bin it silently.
_RECOVER_MIN_SECONDS = 120.0

# A still-"recording" draft counts as orphaned (vs. actively capturing) only once its audio
# file has gone quiet for this long. A live capture appends chunks every few seconds, so a
# file untouched past this window means the writer is gone. Guards list_interrupted() from
# ever offering to "recover" the recording the user is currently making.
_ORPHAN_QUIET_SECONDS = 30.0


def reap_stale_drafts() -> int:
    """Mark recordings left stuck in status "recording" by a crash or a hard quit mid-capture.
    Called once at process startup: since this process just started, nothing can genuinely
    still be recording, so any such draft is orphaned.

    We do NOT decide the draft's fate here — a draft that captured real audio is flagged
    ``status="interrupted"`` (with the recovered duration stamped so the list shows how much),
    and the user chooses Save or Discard from the recordings list. Empty / trivially-short
    false starts are deleted outright, since there is nothing to offer. Returns how many
    orphaned drafts were touched."""
    if not RECORDINGS_DIR.exists():
        return 0
    from curry_leaves_assistant.domain import transcribe
    n = 0
    for d in RECORDINGS_DIR.iterdir():
        meta = _meta(d.name)
        if meta is None or meta.get("status") != "recording":
            continue
        audio = rec_audio_path(d.name)
        seconds = transcribe.probe_duration(str(audio)) if audio.exists() and audio.stat().st_size > 0 else None
        if seconds is not None and seconds >= _RECOVER_MIN_SECONDS:
            meta["status"] = "interrupted"
            meta["duration"] = seconds
            write_json(rec_meta_path(d.name), meta)
        else:
            delete(d.name)
        n += 1
    return n


def recover(rec_id: str) -> dict | None:
    """The user chose Save on an interrupted/orphaned draft: finalize it so it enters the
    normal transcribe → summarize pipeline. The caller kicks off transcription.

    Accepts a draft that is either already ``interrupted`` (flagged by the boot reaper) OR
    still ``recording`` but orphaned mid-session and surfaced by ``list_interrupted`` — in the
    latter case its duration was never written, so we probe it here. A finalized recording is a
    no-op returning None, so a double Save can't re-finalize."""
    meta = _meta(rec_id)
    if meta is None or meta.get("status") not in ("interrupted", "recording"):
        return None
    duration = meta.get("duration")
    if meta.get("status") == "recording":
        # Never finalize a draft that's still actively capturing (audio written recently) —
        # that's the live recording, which the Capture screen owns.
        import time
        audio = rec_audio_path(rec_id)
        if audio.exists() and (time.time() - audio.stat().st_mtime) < _ORPHAN_QUIET_SECONDS:
            return None
        if duration is None:
            from curry_leaves_assistant.domain import transcribe
            duration = transcribe.probe_duration(str(audio))
    return finalize(rec_id, duration=duration)


def list_interrupted() -> list[dict]:
    """Drafts that need a Save/Discard decision — surfaced as a banner in the Recordings tab.

    This covers two cases so recovery doesn't depend on a restart:
      • ``status == "interrupted"`` — already flagged by the boot reaper.
      • ``status == "recording"`` with real captured audio — orphaned DURING this session
        (a tab closed, a crash without a reboot). We probe its duration on the fly and only
        surface ones past the recover threshold; a shorter/empty one is a live-or-trivial draft
        we leave alone (the Capture screen owns a genuinely-live one, and it has no meaningful
        audio to offer anyway).

    Read-only: it does not mutate status. `recover()` / `delete()` act when the user chooses,
    and `recover()` accepts a still-"recording" draft too (see below)."""
    if not RECORDINGS_DIR.exists():
        return []
    from curry_leaves_assistant.domain import transcribe
    out: list[dict] = []
    for d in RECORDINGS_DIR.iterdir():
        meta = _meta(d.name)
        if meta is None:
            continue
        status = meta.get("status")
        if status == "interrupted":
            out.append(meta)
        elif status == "recording":
            audio = rec_audio_path(d.name)
            if not (audio.exists() and audio.stat().st_size > 0):
                continue
            # Skip a draft whose audio is still being written — that's a live capture, not an
            # orphan. Only a file quiet past the window is genuinely abandoned.
            import time
            if (time.time() - audio.stat().st_mtime) < _ORPHAN_QUIET_SECONDS:
                continue
            seconds = transcribe.probe_duration(str(audio))
            if seconds is not None and seconds >= _RECOVER_MIN_SECONDS:
                meta["duration"] = seconds  # so the banner can show how much was captured
                out.append(meta)
    out.sort(key=lambda m: m.get("createdAt") or "", reverse=True)
    return out


def attendee_suggestions() -> list[str]:
    """Distinct attendee names across past recordings, merged with people/ notes in the
    knowledge base. Case-insensitive dedupe, keeping the first-seen spelling; sorted."""
    seen: dict[str, str] = {}
    if RECORDINGS_DIR.exists():
        for d in RECORDINGS_DIR.iterdir():
            m = _meta(d.name)
            for name in ((m or {}).get("attendees") or []):
                key = (name or "").strip().lower()
                if key and key not in seen:
                    seen[key] = name.strip()
    try:
        from curry_leaves_assistant.domain import knowledge
        for note in knowledge.list_notes(subdir="people"):
            person = (note.get("title") or "").strip()
            key = person.lower()
            if key and key not in seen:
                seen[key] = person
    except Exception:
        pass  # people listing is best-effort — never block the suggestions
    return sorted(seen.values(), key=str.lower)


def tag_suggestions() -> list[dict]:
    """Distinct recording tags with their use counts, most-used first then alphabetical.

    Derived on read by scanning every meta.json rather than kept in a registry file — same
    call as `attendee_suggestions()` above and `cl_memory`'s `established_tags()`: a tag only
    exists because a recording carries it, so a separate store could only ever drift. Unlike
    `established_tags()` there's no >1-use floor, because the picker has to offer a tag back
    the moment it's first typed, and the left panel groups by tags a single recording has.

    Tags differing only in case are one tag. The spelling shown is the one used most often,
    ties broken lexicographically — NOT the first one scanned, because iterdir() order over
    randomly-named recording dirs is arbitrary and would make the displayed casing (and the
    group heading built from it) flip between calls.
    """
    counts: dict[str, int] = {}
    spellings: dict[str, dict[str, int]] = {}
    if RECORDINGS_DIR.exists():
        for d in RECORDINGS_DIR.iterdir():
            # RECORDINGS_DIR also holds vocabulary.json — _meta() returns None for a non-dir.
            m = _meta(d.name)
            for tag in ((m or {}).get("tags") or []):
                label = str(tag or "").strip()
                key = label.lower()
                if not key:
                    continue
                counts[key] = counts.get(key, 0) + 1
                seen = spellings.setdefault(key, {})
                seen[label] = seen.get(label, 0) + 1
    return [
        {"tag": min(spellings[k], key=lambda s: (-spellings[k][s], s)), "count": counts[k]}
        for k in sorted(counts, key=lambda k: (-counts[k], k))
    ]


def list_recordings() -> list[dict]:
    out = []
    if RECORDINGS_DIR.exists():
        for d in RECORDINGS_DIR.iterdir():
            m = _meta(d.name)
            # Only finalized recordings belong in the main list. A live "recording" belongs to
            # the Capture screen; an "interrupted"/orphaned draft is surfaced separately by
            # list_interrupted() as a Save/Discard banner, not as a normal row.
            if m and m.get("status") == "saved":
                m["outputs"] = output_keys(d.name)  # names only — no content reads
                out.append(m)
    out.sort(key=lambda m: m.get("createdAt") or "", reverse=True)
    return out


def delete(rec_id: str) -> bool:
    d = rec_dir(rec_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        events.emit("recording.deleted", entity_id=rec_id)
        return True
    return False
