"""Session persistence — the app's adapter over the curry-leaves kernel session format.

Two access surfaces onto the same on-disk sessions:
  • FileSessionStore — the create / list_recent / load / append / truncate / exists API that
    chat_sessions.py drives the chat UI through.
  • open_runner_store / RunnerSessionStore — the write path the agent runner attaches as
    ``config.store`` (see agent_engine.py) so a run's turns land in the session transcript.

The on-disk format is the CURRY-LEAVES native transcript.jsonl (event records produced by the
runner's SessionStore). We reconstruct pydantic message objects from those records for the
get_messages() / load() callers, so the two surfaces stay consistent.

A separate app-meta.json sidecar holds the UI fields (title, message_count, updated_at, …)
that curry-leaves's own meta.json doesn't carry, avoiding conflicts with the runner's store.
(Sessions written before the Curry Leaves rename used buddy-meta.json; it is migrated in
place the first time a session is touched.)

Usage in build_runner (agent_engine.py):
    from curry_leaves_assistant.stores.session_store import open_runner_store
    config.store = open_runner_store(session_id, model=..., provider=...)
    # caller must await config.store.close() after the run

Usage in chat_sessions.py:
    store = FileSessionStore()
    meta = store.create(title=..., model=...)
    store.append(sid, msg)
    msgs = store.load(sid)
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from curry_leaves.core.messages import (
    AssistantMessage,
    AudioBlock,
    FileBlock,
    ImageBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)
from curry_leaves.session.store import (
    SessionStore as CLSessionStore,
    SessionMeta as CLSessionMeta,
    _json_default,
)
from curry_leaves.util.paths import sessions_dir


def _now_ts() -> float:
    return time.time()


def _new_session_id() -> str:
    return f"{int(_now_ts())}-{uuid.uuid4().hex[:6]}"


# ─── compat meta (sidecar: app-meta.json) ─────────────────────────────────────

class SessionMeta(BaseModel):
    id: str
    title: str = ""
    model: str = ""
    provider: str = ""
    cwd: str = ""
    parent_id: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    message_count: int = 0


# ─── message reconstruction from event records ────────────────────────────────

def _records_from_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            pass
    return records


def _content_block(raw: Any):
    """Turn a raw JSON content block back into a pydantic block.
    Returns None for block types that carry no displayable content (signature)."""
    if isinstance(raw, str):
        return TextBlock(type="text", text=raw)
    if not isinstance(raw, dict):
        return TextBlock(type="text", text=str(raw))
    t = raw.get("type", "")
    if t == "signature":
        return None
    if t == "thinking":
        return ThinkingBlock(thinking=raw.get("thinking", ""))
    if t == "image":
        return ImageBlock.model_validate(raw)
    if t == "file":
        return FileBlock.model_validate(raw)
    if t == "audio":
        return AudioBlock.model_validate(raw)
    if t == "tool_call" or ("name" in raw and "arguments" in raw):
        return ToolCallBlock(
            type="tool_call",
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            arguments=raw.get("arguments", {}),
        )
    return TextBlock(type="text", text=raw.get("text", ""))


def records_to_messages(records: list[dict]) -> list:
    """Convert curry-leaves event records to pydantic message objects for the UI."""
    msgs = []
    for r in records:
        kind = r.get("kind")
        if kind == "user":
            if "content" in r:  # multimodal turn — reconstruct its blocks (images/files/audio)
                content = [b for b in (_content_block(raw) for raw in r["content"]) if b is not None]
            else:
                content = [TextBlock(type="text", text=r.get("text", ""))]
            msgs.append(UserMessage(role="user", content=content))
        elif kind == "assistant":
            content_raw = r.get("content") or []
            content = [b for b in (_content_block(raw) for raw in content_raw) if b is not None]
            msgs.append(AssistantMessage(role="assistant", content=content))
        elif kind == "tool_end":
            result_raw = r.get("result") or []
            content = [_content_block(b) for b in (result_raw if isinstance(result_raw, list) else [result_raw])]
            msgs.append(ToolResultMessage(
                role="tool_result",
                tool_call_id=r.get("id", ""),
                tool_name=r.get("name", ""),
                content=content,
                is_error=bool(r.get("is_error")),
            ))
    return msgs


# ─── compatibility store ───────────────────────────────────────────────────────

_META_NAME = "app-meta.json"
_LEGACY_META_NAME = "buddy-meta.json"  # pre-rename sidecar; migrated in place on first touch


class FileSessionStore:
    """Legacy-API session manager backed by curry-leaves paths.

    Stores under <CURRY_LEAVES_HOME>/sessions/<id>/
    - transcript.jsonl  — curry-leaves event records (written by runner's CLFileSessionStore)
    - app-meta.json     — UI metadata (title, message_count, …) written by this class
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else Path(sessions_dir())

    def _dir(self, sid: str) -> Path:
        return self.root / sid

    def _meta_path(self, sid: str) -> Path:
        p = self._dir(sid) / _META_NAME
        if not p.exists():
            legacy = self._dir(sid) / _LEGACY_META_NAME
            if legacy.exists():
                try:
                    legacy.rename(p)
                except OSError:
                    return legacy
        return p

    def _transcript_path(self, sid: str) -> Path:
        return self._dir(sid) / "transcript.jsonl"

    def create(self, *, model: str = "", provider: str = "", cwd: str = "",
               title: str = "", parent_id: str | None = None, now: float | None = None) -> SessionMeta:
        ts = now if now is not None else _now_ts()
        sid = _new_session_id()
        self._dir(sid).mkdir(parents=True, exist_ok=True)
        # Create empty transcript so the dir is recognizable.
        tp = self._transcript_path(sid)
        if not tp.exists():
            tp.touch()
        meta = SessionMeta(id=sid, model=model, provider=provider, cwd=cwd, title=title,
                           parent_id=parent_id, created_at=ts, updated_at=ts)
        self._write_meta(meta)
        return meta

    def exists(self, sid: str) -> bool:
        return self._meta_path(sid).is_file()

    def load_meta(self, sid: str) -> SessionMeta:
        return SessionMeta.model_validate_json(
            self._meta_path(sid).read_text(encoding="utf-8")
        )

    def load(self, sid: str) -> list:
        """Return pydantic message objects reconstructed from the event record transcript."""
        records = _records_from_jsonl(self._transcript_path(sid))
        return records_to_messages(records)

    def load_records(self, sid: str) -> list[dict]:
        """The RAW transcript records, before they're narrowed to message objects.

        `load()` builds pydantic messages, which model only what the LLM needs to see —
        role and content. Per-step bookkeeping the transcript also carries (`usage`, `ts`)
        has no home on those objects and is dropped. Replaying a session's token counts and
        timing needs the records themselves, so callers that want them read this instead."""
        return _records_from_jsonl(self._transcript_path(sid))

    def append(self, sid: str, message, *, now: float | None = None) -> None:
        """Append a single message to the transcript (used for manual writes, e.g. compaction)."""
        record: dict[str, Any] = {}
        role = getattr(message, "role", "")
        if role == "user":
            text = "".join(getattr(b, "text", "") for b in (getattr(message, "content", []) or []))
            record = {"kind": "user", "text": text}
        elif role == "assistant":
            content = [b.model_dump(mode="json") for b in (getattr(message, "content", []) or [])]
            record = {"kind": "assistant", "content": content}
        elif role == "tool_result":
            content = [b.model_dump(mode="json") for b in (getattr(message, "content", []) or [])]
            record = {
                "kind": "tool_end",
                "id": getattr(message, "tool_call_id", ""),
                "name": getattr(message, "tool_name", ""),
                "result": content,
                "is_error": bool(getattr(message, "is_error", False)),
            }
        if not record:
            return
        import datetime as _dt
        ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
        with open(self._transcript_path(sid), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts, **record}) + "\n")
        try:
            meta = self.load_meta(sid)
            meta.message_count += 1
            meta.updated_at = now if now is not None else _now_ts()
            if not meta.title:
                meta.title = _title_from_message(message)
            self._write_meta(meta)
        except Exception:
            pass

    def truncate(self, sid: str, count: int, *, now: float | None = None) -> None:
        """Keep only the first `count` *message-level* records in the transcript."""
        records = _records_from_jsonl(self._transcript_path(sid))
        # Count only user/assistant/tool_end records (not metadata records).
        kept_records, msg_count = [], 0
        for r in records:
            if r.get("kind") in ("user", "assistant", "tool_end"):
                if msg_count >= count:
                    break
                msg_count += 1
            kept_records.append(r)
        with open(self._transcript_path(sid), "w", encoding="utf-8") as f:
            for r in kept_records:
                f.write(json.dumps(r) + "\n")
        try:
            meta = self.load_meta(sid)
            meta.message_count = msg_count
            meta.updated_at = now if now is not None else _now_ts()
            self._write_meta(meta)
        except Exception:
            pass

    def list_recent(self, limit: int = 20) -> list[SessionMeta]:
        if not self.root.is_dir():
            return []
        metas: list[SessionMeta] = []
        for d in self.root.iterdir():
            mp = self._meta_path(d.name)
            if mp.is_file():
                try:
                    metas.append(SessionMeta.model_validate_json(mp.read_text(encoding="utf-8")))
                except Exception:
                    continue
        metas.sort(key=lambda m: m.updated_at, reverse=True)
        return metas[:limit]

    def _write_meta(self, meta: SessionMeta) -> None:
        self._meta_path(meta.id).write_text(meta.model_dump_json(indent=2), encoding="utf-8")


def _title_from_message(msg) -> str:
    for b in getattr(msg, "content", []) or []:
        if hasattr(b, "text") and b.text:
            return b.text[:48].replace("\n", " ")
    return ""


# ─── runner store (writes into the app session dir) ───────────────────────────

class RunnerSessionStore(CLSessionStore):
    """SessionStore that appends event records to the app session's transcript.jsonl.

    Unlike CLFileSessionStore (which resolves paths via curry_leaves.util.paths and may
    use a stale home dir), this writes directly to the FileSessionStore's own session dir
    so both the compat store and the runner see the same transcript file.
    """

    def __init__(self, session_dir: Path, session_id: str, model: str, provider: str) -> None:
        super().__init__(session_id, CLSessionMeta(model=model, provider=provider, cwd=""))
        self._transcript_path = session_dir / "transcript.jsonl"
        self._stream = None
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            self._stream = open(self._transcript_path, "a", encoding="utf-8")
        except OSError:
            pass

    def persist_meta(self, meta: dict) -> None:
        pass  # the app-meta.json sidecar is managed by FileSessionStore; runner meta goes to CLFileSessionStore only

    async def close(self) -> None:
        await super().close()
        # Refresh the sidecar meta message_count from the transcript so list_recent stays accurate.
        try:
            compat = FileSessionStore(self._transcript_path.parent.parent)
            sid = self._transcript_path.parent.name
            if compat.exists(sid):
                m = compat.load_meta(sid)
                records = _records_from_jsonl(self._transcript_path)
                m.message_count = sum(1 for r in records if r.get("kind") in ("user", "assistant", "tool_end"))
                import time as _time
                m.updated_at = _time.time()
                compat._write_meta(m)
        except Exception:
            pass

    def persist_record(self, record: dict) -> None:
        if self._stream is None:
            return
        try:
            import json as _json
            self._stream.write(_json.dumps(record, default=_json_default) + "\n")
            self._stream.flush()
        except OSError:
            pass

    async def flush(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.close()


def open_runner_store(session_dir: Path, session_id: str, *, model: str = "",
                      provider: str = "") -> RunnerSessionStore:
    """Create a runner SessionStore that writes into the given app session directory.

    Caller must call `await store.close()` after the run finishes.
    """
    return RunnerSessionStore(session_dir, session_id, model=model, provider=provider)
