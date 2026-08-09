"""Single source of truth for the ~/.curry-leaves file layout.

Honors the CURRY_LEAVES_DIR env var (set by the Electron shell) so the data dir is
relocatable. Everything is plain JSON/markdown — human-readable, hand-editable.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

DATA_DIR = Path(os.environ.get("CURRY_LEAVES_DIR", str(Path.home() / ".curry-leaves")))

# Top-level files
SETTINGS_PATH = DATA_DIR / "settings.json"
# Written by the running backend, read by `curry-leaves-assistant stop` and by the Electron
# shell (which SIGKILLs a stale pid on start). The name/location is a cross-repo contract:
# desktop's src/electron/paths.ts hardcodes ~/.curry-leaves/service.pid.
SERVICE_PID_PATH = DATA_DIR / "service.pid"
TODOS_PATH = DATA_DIR / "todos.json"
REMINDERS_PATH = DATA_DIR / "reminders.json"

# Sub-directories
AGENTS_DIR = DATA_DIR / "agents"
RECORDINGS_DIR = DATA_DIR / "recordings"
EVENTS_DIR = DATA_DIR / "events"
QUEUE_DIR = DATA_DIR / "queue"
RUNS_DIR = DATA_DIR / "runs"
MODELS_DIR = DATA_DIR / "models"
POOL_DIR = DATA_DIR / "pool"        # common pool of work items awaiting assignment
# The ONE bundle: the knowledge base AND every kind of memory (semantic facts, per-agent private
# notes, episodic run records, consolidated summaries), partitioned by the `type:` frontmatter
# field rather than by directory. See domain/memory.py.
MEMORY_DIR = DATA_DIR / "memory"
KNOWLEDGE_DIR = MEMORY_DIR   # back-compat alias: the KB is this bundle's hub-typed notes

# Where each memory kind's notes sit INSIDE that bundle (bundle-relative). Folders are for humans
# browsing the tree; the code tells scopes apart by the note's frontmatter type.
#   SEMANTIC_DIR     -> the shared user profile
#   PRIVATE_DIR      -> one agent's own notes (scoped by an `agent` field)
#   EPISODIC_DIR     -> one record per agent run
#   CONSOLIDATED_DIR -> summaries folded out of clusters of episodes
SEMANTIC_DIR = "memory/profile"
PRIVATE_DIR = "memory/agents"
EPISODIC_DIR = "memory/episodes"
CONSOLIDATED_DIR = "memory/consolidated"
TRACES_DIR = DATA_DIR / "traces"    # one <traceId>.jsonl of spans per trace + index.jsonl
USAGE_DIR = DATA_DIR / "usage"      # durable append-only token ledger (never pruned)
DASHBOARD_DIR = DATA_DIR / "dashboard"  # boards: one <boardId>.json each + index.json
ARTIFACTS_DIR = DATA_DIR / "artifacts"  # LLM-generated deliverables: one <id>/ dir each
# NOTE: there is deliberately no PROFILE_DIR / EPISODES_DIR. The user profile (semantic), each
# agent's private notes, and episodic run records are all notes inside MEMORY_DIR, told apart by
# their `type:` field — see domain/memory.py. Separate dirs would re-fragment the one bundle and
# break consolidation/trace, which need links to cross those scopes.

EVENTS_LOG = EVENTS_DIR / "log.ndjson"
KNOWLEDGE_LOG = KNOWLEDGE_DIR / "log.md"  # append-only change history for the bundle
TRACES_INDEX = TRACES_DIR / "index.jsonl"  # one summary row per trace, for fast listing


TEMPLATES_DIR = DATA_DIR / "templates"  # meeting templates: one <id>.md each (frontmatter + body)


def ensure_dirs() -> None:
    for d in (DATA_DIR, AGENTS_DIR, RECORDINGS_DIR, EVENTS_DIR,
              QUEUE_DIR, RUNS_DIR, MODELS_DIR, POOL_DIR, MEMORY_DIR, TRACES_DIR, USAGE_DIR,
              DASHBOARD_DIR, ARTIFACTS_DIR, TEMPLATES_DIR):
        d.mkdir(parents=True, exist_ok=True)


def knowledge_path(rel: str):
    """A bundle-relative path under KNOWLEDGE_DIR (no escaping the bundle).

    The escape check compares *resolved* paths (symlink-safe), but the returned
    path stays literally under KNOWLEDGE_DIR so ``relative_to`` round-trips.
    """
    p = KNOWLEDGE_DIR / rel.lstrip("/")
    if not str(p.resolve()).startswith(str(KNOWLEDGE_DIR.resolve())):
        raise ValueError(f"path escapes knowledge bundle: {rel!r}")
    return p


# ─── Agent path helpers ───────────────────────────────────────────────────────
def agent_md_path(agent_id: str) -> Path:
    return AGENTS_DIR / f"{agent_id}.md"


def agent_meta_path(agent_id: str) -> Path:
    return AGENTS_DIR / f"{agent_id}.meta.json"


def agent_runs_dir(agent_id: str) -> Path:
    return RUNS_DIR / agent_id


def safe_agent_seg(agent_id: str) -> str:
    """An agent id sanitized into a single path segment (it can't escape its folder)."""
    return agent_id.replace("/", "_").replace("..", "_")


# ─── Recording path helpers ───────────────────────────────────────────────────
def rec_dir(rec_id: str) -> Path:
    return RECORDINGS_DIR / rec_id


def rec_meta_path(rec_id: str) -> Path:
    return rec_dir(rec_id) / "meta.json"


def rec_audio_path(rec_id: str) -> Path:
    return rec_dir(rec_id) / "audio.webm"


def rec_transcript_path(rec_id: str) -> Path:
    return rec_dir(rec_id) / "transcript.md"


def rec_outputs_dir(rec_id: str) -> Path:
    """Agent-produced artifacts: one <agentId>.md (frontmatter + body) per agent."""
    return rec_dir(rec_id) / "outputs"


# Meeting templates: default pointer + one <id>.md per template.
TEMPLATES_CONFIG_PATH = TEMPLATES_DIR / "config.json"


def template_md_path(template_id: str) -> Path:
    return TEMPLATES_DIR / f"{template_id}.md"


# ─── Dashboard path helpers ────────────────────────────────────────────────────
DASHBOARD_INDEX_PATH = DASHBOARD_DIR / "index.json"


def board_path(board_id: str) -> Path:
    return DASHBOARD_DIR / f"{board_id}.json"


# ─── Artifact path helpers ─────────────────────────────────────────────────────
def artifact_dir(artifact_id: str) -> Path:
    return ARTIFACTS_DIR / artifact_id


def artifact_meta_path(artifact_id: str) -> Path:
    return artifact_dir(artifact_id) / "meta.json"


def artifact_file_path(artifact_id: str, rel: str) -> Path:
    """A file within one artifact's own directory (no escaping it).

    Containment is checked against the *resolved* parents rather than a string prefix:
    a prefix test passes for a sibling directory whose name merely starts with this
    artifact's id (`<id>-evil/x`). `resolve()` (not `absolute()`) so a symlink pointing
    out of the dir is caught too. Nested paths (`assets/img/logo.png`) stay legal.
    """
    if PurePosixPath(rel).is_absolute() or Path(rel).is_absolute():
        # Silently re-rooting '/etc/passwd' to a same-named file inside the artifact is
        # more surprising than refusing it.
        raise ValueError(f"artifact path must be relative: {rel!r}")
    d = artifact_dir(artifact_id)
    p = d / rel
    rp, dr = p.resolve(), d.resolve()
    if dr != rp and dr not in rp.parents:
        raise ValueError(f"path escapes artifact dir: {rel!r}")
    return p
