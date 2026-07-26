"""Meeting templates as files: one ``<id>.md`` (frontmatter + body) per template under
~/.curry-leaves/templates/, plus a small config.json holding the default pointer.

A template is user-facing INTENT ("what I want from this kind of meeting"), distinct from
a skill (the copilot's procedural memory). The frontmatter is the minimum the machine
reads — name, description, the ordered output `sections`, and the `live.watch` config for
the in-meeting engine. Everything template-specific about HOW to write those sections
lives in the markdown body, which the copilot follows and the authoring pass writes; the
user never edits it by hand.

Files-first, mirroring agent_store / skills_store: the markdown is the source of truth,
config.json only points at the default.
"""
from __future__ import annotations

import re
from pathlib import Path

from curry_leaves_assistant.core import events
from curry_leaves_assistant.core.paths import (
    TEMPLATES_CONFIG_PATH, TEMPLATES_DIR, template_md_path,
)
from curry_leaves_assistant.core.store import now_iso, read_json, write_json
from curry_leaves_assistant.stores.agent_store import parse_frontmatter, render_frontmatter

_SLUG = re.compile(r"[^a-z0-9-]+")

# Agents every recording runs regardless of template. The copilot produces the template's
# sections; title-generator names the recording. kb-filer is `always:true` so it isn't
# listed here — it runs on the outputs-completed barrier on its own.
BASE_AGENT_IDS = ["meeting-copilot", "title-generator"]

# Live *concerns* a template can opt into; the template's live.watch is filtered to these.
# These are user-facing intents ("what this meeting cares about"), NOT card types — the
# meeting-live agent emits a finer-grained card `kind`. LIVE_CONCERN_CARD_KINDS maps one to
# the other so a template's watch list actually gates which cards may surface.
LIVE_WATCH_KINDS = ["open-loops", "commitments", "unanswered-questions", "decisions", "conflicts"]

# concern → the card kinds it admits (see seeds/agents/meeting-live.md for the kind list).
# `suggestion` / `talking-point` are general-purpose guidance: admitted by any engaged
# template, so a meeting never loses proactive help just by watching one narrow concern.
LIVE_CONCERN_CARD_KINDS: dict[str, list[str]] = {
    "open-loops": ["open-loop", "reminder", "close-proposal"],
    "commitments": ["open-loop", "close-proposal", "reminder"],
    "unanswered-questions": ["answer", "clarify", "ask-this"],
    "decisions": ["decided-before", "ask-this", "clarify"],
    "conflicts": ["decided-before", "clarify", "ask-this"],
}
LIVE_ALWAYS_CARD_KINDS = ["suggestion", "talking-point"]


def live_card_kinds(watch: list[str] | None) -> list[str]:
    """The card kinds admissible for a watch list. Empty watch → empty (engine disengaged).
    Unknown concerns are ignored; the always-on guidance kinds ride along with any concern."""
    if not watch:
        return []
    out: list[str] = []
    for w in watch:
        for k in LIVE_CONCERN_CARD_KINDS.get(w, []):
            if k not in out:
                out.append(k)
    for k in LIVE_ALWAYS_CARD_KINDS:
        if k not in out:
            out.append(k)
    return out


def _slugify(name: str) -> str:
    return _SLUG.sub("-", (name or "template").strip().lower()).strip("-") or "template"


def _config() -> dict:
    return read_json(TEMPLATES_CONFIG_PATH, {}) or {}


def _norm_sections(raw) -> list[dict]:
    """Coerce a sections list into [{id, title}], slugifying ids and dropping dupes/blanks."""
    out: list[dict] = []
    seen: set[str] = set()
    for s in raw or []:
        if isinstance(s, str):
            sid, title = _slugify(s), s.strip()
        elif isinstance(s, dict):
            title = (s.get("title") or s.get("id") or "").strip()
            sid = _slugify(s.get("id") or title)
        else:
            continue
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append({"id": sid, "title": title or sid})
    return out


def _parse(path: Path) -> dict | None:
    try:
        fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    live = fm.get("live") or {}
    watch = [w for w in (live.get("watch") or []) if w in LIVE_WATCH_KINDS]
    extract = fm.get("extract") or {}
    return {
        "id": path.stem,
        "name": fm.get("name") or path.stem,
        "description": fm.get("description") or "",
        "sections": _norm_sections(fm.get("sections")),
        "extract": {
            "todos": bool(extract.get("todos", True)),
            "reminders": bool(extract.get("reminders", True)),
        },
        "live": {"watch": watch},
        "agents": [a for a in (fm.get("agents") or []) if isinstance(a, str)],
        "body": body.strip(),
        "createdAt": fm.get("createdAt"),
        "updatedAt": fm.get("updatedAt"),
    }


def _meta_only(t: dict) -> dict:
    return {k: v for k, v in t.items() if k != "body"}


# ─── reads ─────────────────────────────────────────────────────────────────────
def get_template(template_id: str | None) -> dict | None:
    if not template_id:
        return None
    p = template_md_path(template_id)
    return _parse(p) if p.exists() else None


def list_templates() -> dict:
    templates = []
    if TEMPLATES_DIR.is_dir():
        for p in sorted(TEMPLATES_DIR.glob("*.md")):
            t = _parse(p)
            if t:
                templates.append(_meta_only(t))
    return {"defaultTemplateId": _config().get("defaultTemplateId"), "templates": templates}


def default_template_id() -> str | None:
    tid = _config().get("defaultTemplateId")
    if tid and template_md_path(tid).exists():
        return tid
    # Fall back to any template on disk so a recording always resolves to something.
    if TEMPLATES_DIR.is_dir():
        first = next(iter(sorted(TEMPLATES_DIR.glob("*.md"))), None)
        if first:
            return first.stem
    return None


def _ids(template_ids: list[str] | None, template_id: str | None) -> list[str]:
    """Normalize the (possibly multi) template selection into an ordered, deduped id list.
    Accepts the new `templateIds` array and/or the legacy single `templateId`."""
    out: list[str] = []
    for tid in list(template_ids or []) + ([template_id] if template_id else []):
        if tid and tid not in out:
            out.append(tid)
    return out


def resolved_agent_ids(template_id: str | None = None, template_ids: list[str] | None = None) -> list[str]:
    """The full agent set for a recording bound to one OR MORE templates: the base recording
    agents plus every selected template's advanced `agents:`. Deduped, order-preserving."""
    ids = list(BASE_AGENT_IDS)
    for tid in _ids(template_ids, template_id):
        for a in (get_template(tid) or {}).get("agents") or []:
            if a not in ids:
                ids.append(a)
    return ids


def _section_key(template_id: str, section_id: str, is_primary: bool) -> str:
    """The save_output `section` value for a section. The primary template keeps bare section
    ids (so `summary` still routes to the summary/meta mirror); additional templates namespace
    their non-summary sections as `<templateId>__<section>` to avoid collisions."""
    if is_primary:
        return section_id
    return f"{template_id}__{section_id}"


def agent_context(template_id: str | None = None, template_ids: list[str] | None = None) -> str:
    """Render the selected template(s) as one labeled context block for the copilot: for each
    template, its identity, the sections it must produce, its extraction rule, and its body.
    The FIRST template is primary (owns the `summary` section); the rest namespace their
    non-summary sections so nothing collides."""
    ids = _ids(template_ids, template_id) or _ids(None, default_template_id())
    templates = [t for t in (get_template(i) for i in ids) if t]
    if not templates:
        return "No template resolved; write a concise summary and capture agreed action items."

    blocks: list[str] = []
    if len(templates) > 1:
        blocks.append("This recording uses MULTIPLE templates. Produce every section of EACH, "
                      "in the order given. Use the exact `section` value shown for each.")
    for ti, t in enumerate(templates):
        is_primary = ti == 0
        lines = [f"--- Template: {t['name']} ---", f"Purpose: {t['description']}", "", "Produce these sections:"]
        for si, s in enumerate(t["sections"]):
            key = _section_key(t["id"], s["id"], is_primary)
            how = "via save_summary" if (is_primary and s["id"] == "summary") \
                else f"via save_output with section='{key}'"
            lines.append(f"{si + 1}. {s['title']} ({how})")
        ex = t["extract"]
        lines.append(
            "Action items for this template: "
            + ("extract todos" if ex["todos"] else "do NOT create todos")
            + "; "
            + ("extract time-bound reminders." if ex["reminders"] else "do NOT create reminders.")
        )
        if t["body"]:
            lines.append("\nInstructions:\n" + t["body"])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def live_context(template_id: str | None = None, template_ids: list[str] | None = None) -> str:
    """Render the selected template(s) for the LIVE copilot — what kind of meeting this is and
    how to read the room. Distinct from ``agent_context``: the live agent produces cards, not
    sections, so the sections/save_output scaffolding is omitted. The body IS included (it is
    where a template's voice and priorities live) but framed as background, since it was written
    to instruct the post-meeting write-up rather than the in-meeting nudges.
    Returns "" when nothing resolves, so the caller can fall back to its generic brief."""
    ids = _ids(template_ids, template_id)
    templates = [t for t in (get_template(i) for i in ids) if t]
    if not templates:
        return ""
    blocks: list[str] = []
    for t in templates:
        lines = [f"--- Meeting type: {t['name']} ---"]
        if t["description"]:
            lines.append(f"Purpose: {t['description']}")
        if t["sections"]:
            titles = ", ".join(s["title"] for s in t["sections"])
            lines.append(
                f"Afterwards this meeting gets written up as: {titles}. Favor cards that help "
                "the user cover that ground while they still can."
            )
        if t["body"]:
            lines.append(
                "\nHow this kind of meeting is run (written for the post-meeting write-up — "
                "use it to understand tone and priorities, do NOT write sections now):\n"
                + t["body"]
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ─── writes ─────────────────────────────────────────────────────────────────────
def _dump(t: dict) -> str:
    fm: dict = {"name": t["name"], "description": t.get("description", "")}
    fm["sections"] = [{"id": s["id"], "title": s["title"]} for s in t.get("sections") or []]
    fm["extract"] = {"todos": bool(t.get("extract", {}).get("todos", True)),
                     "reminders": bool(t.get("extract", {}).get("reminders", True))}
    fm["live"] = {"watch": [w for w in (t.get("live", {}).get("watch") or []) if w in LIVE_WATCH_KINDS]}
    if t.get("agents"):
        fm["agents"] = t["agents"]
    fm["createdAt"] = t.get("createdAt") or now_iso()
    fm["updatedAt"] = now_iso()
    return render_frontmatter(fm, t.get("body") or "")


def save_template(template: dict) -> dict:
    """Create or update. Without an id, derive a unique one from the name."""
    tid = template.get("id") or _slugify(template.get("name", ""))
    if not template.get("id"):  # keep derived ids unique
        base, n = tid, 2
        while template_md_path(tid).exists():
            tid = f"{base}-{n}"
            n += 1
    existing = get_template(tid)
    rec = {
        "id": tid,
        "name": (template.get("name") or tid).strip(),
        "description": (template.get("description") or "").strip(),
        "sections": _norm_sections(template.get("sections")) or [{"id": "summary", "title": "Summary"}],
        "extract": template.get("extract") or {"todos": True, "reminders": True},
        "live": {"watch": [w for w in (template.get("live", {}) or {}).get("watch", []) if w in LIVE_WATCH_KINDS]},
        "agents": [a for a in (template.get("agents") or []) if isinstance(a, str)],
        "body": template.get("body") or "",
        "createdAt": (existing or {}).get("createdAt"),
    }
    template_md_path(tid).parent.mkdir(parents=True, exist_ok=True)
    template_md_path(tid).write_text(_dump(rec), encoding="utf-8")
    saved = get_template(tid) or rec
    events.emit("template.updated" if existing else "template.created",
                payload=_meta_only(saved), entity_id=tid, label=saved["name"])
    return saved


def delete_template(template_id: str) -> bool:
    p = template_md_path(template_id)
    if not p.exists():
        return False
    p.unlink(missing_ok=True)
    cfg = _config()
    if cfg.get("defaultTemplateId") == template_id:
        cfg["defaultTemplateId"] = None
        write_json(TEMPLATES_CONFIG_PATH, cfg)
    events.emit("template.deleted", entity_id=template_id)
    return True


def set_default(template_id: str | None) -> dict:
    if template_id is not None and not template_md_path(template_id).exists():
        raise ValueError(f"unknown template: {template_id!r}")
    cfg = _config()
    cfg["defaultTemplateId"] = template_id
    write_json(TEMPLATES_CONFIG_PATH, cfg)
    return {"defaultTemplateId": template_id}


# ─── seeding + migration ─────────────────────────────────────────────────────────
SEED_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "seeds" / "templates"
_DEFAULT_SEED_ID = "general-meeting"


def seed_default_templates() -> None:
    """Copy any bundled seed template not on disk yet, then ensure a default pointer.
    Like agents/skills: seeded once, never overwritten, so user edits survive."""
    import shutil
    seeded = 0
    if SEED_TEMPLATES_DIR.is_dir():
        for src in sorted(SEED_TEMPLATES_DIR.glob("*.md")):
            dst = template_md_path(src.stem)
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            seeded += 1
    cfg = _config()
    if not cfg.get("defaultTemplateId"):
        cfg["defaultTemplateId"] = _DEFAULT_SEED_ID if template_md_path(_DEFAULT_SEED_ID).exists() else default_template_id()
        write_json(TEMPLATES_CONFIG_PATH, cfg)
    if seeded:
        print(f"[templates] seeded {seeded} default template(s)", flush=True)
