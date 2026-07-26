"""One rule for where a memory goes: **it hangs off whatever it's about.**

Every durable thing the app learns has a parent — a person, an app, a topic, an assistant. It is
filed under that parent's folder as an ordinary note, so it shows up in the tree beside its
parent, links to it, and turns up in the same searches:

    people/nambi/facts/answer-style.md      -> about the user
    apps/cbm/facts/release-cadence.md       -> about an app
    topics/stock-reports/facts/grid.md      -> about a topic
    agents/kb-filer/facts/filing-rule.md    -> about how one assistant works

There is no flat "profile bucket" any more. What used to be the user profile is simply the facts
attached to the person note marked as the user — the same shape as everything else. That
uniformity is the point: a fact about you sits next to the meetings you attended and the apps you
work on, instead of floating in a store that knows about none of them.

`type` still matters for *retrieval*, not storage:
  • ``preference`` — how the user likes things done. Injected into EVERY prompt (behavioural).
  • ``fact`` — something true. Pulled by vector search when the question is about it.
"""
from __future__ import annotations

import re
from typing import Any

_SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    return _SLUG.sub("-", (text or "note").lower()).strip("-")[:60] or "note"


def _bundle() -> Any:
    from curry_leaves_assistant.core import memory_ref

    return memory_ref.get()


def anchor_for(area: str, *, exclude: str = "") -> dict | None:
    """The note that best represents a parent, for a fact to link to.

    Prefers an overview/readme/same-name page, else the shallowest note under it. Returns None
    when the parent has no notes — the fact then carries no link rather than a dead one
    (``index.md`` is a GENERATED hub, not a note; linking there resolves to nothing)."""
    b = _bundle()
    cands = [n for n in b.notes(area) if n.get("path") != exclude]
    if not cands:
        return None

    def rank(n: dict) -> tuple[int, int, str]:
        name = (n.get("path") or "").rsplit("/", 1)[-1][:-3].lower()
        primary = 0 if name in ("overview", "readme", "index", area.split("/")[-1]) else 1
        return (primary, (n.get("path") or "").count("/"), n.get("path") or "")

    best = sorted(cands, key=rank)[0]
    return {"path": best["path"], "title": best.get("title") or area.split("/")[-1]}


def ensure_parent(area: str, *, title: str, type: str, description: str = "",
                  extra: dict | None = None) -> str:
    """Make sure a parent has a note to hang things off, and return its path.

    A fact about someone you've never recorded shouldn't be orphaned, so the parent page is
    created on demand (empty but real). Idempotent: an existing page is left exactly as it is.

    The page sits BESIDE the folder (``people/nambi.md`` next to ``people/nambi/facts/…``), not
    inside it — so the tree reads "a person, and the things known about them" rather than burying
    the person one level down among their own facts."""
    b = _bundle()
    rel = f"{area}.md"
    if b.read(rel) is not None:
        return rel
    existing = anchor_for(area)
    if existing:
        return existing["path"]
    from cl_memory.util import dump_note

    fm = {"type": type, "title": title,
          "description": description or f"{title} — {type}.", **(extra or {})}
    b.write_raw(rel, dump_note(fm, f"{description}\n"))
    return rel


def remember_about(text: str, *, area: str, type: str = "fact", subject: str | None = None,
                   source: str = "inferred") -> dict:
    """File one durable fact/preference under the parent it is ABOUT.

    `area` is the parent's folder (``people/nambi``, ``apps/cbm``, ``topics/…``). The note lands
    at ``<area>/facts/<slug>.md``, linked to the parent's page. Re-using the same `subject`
    rewrites that note in place instead of adding a near-duplicate."""
    from cl_memory.util import dump_note

    b = _bundle()
    area = area.strip("/")
    subj = (subject or text[:60]).strip()
    rel = f"{area}/facts/{slug(subj)}.md"
    existing = b.read(rel)
    fm: dict[str, Any] = {
        # A hub type (`fact`/`preference`), not a memory type — that's what puts it in the tree
        # beside its parent instead of in a separate memory view.
        "type": type,
        "id": (existing or {}).get("frontmatter", {}).get("id"),
        "title": subj,
        "description": text[:200],
        "tags": [type, area.split("/")[-1]],
        "source": {"type": source},
        "about": area,
    }
    fm = {k: v for k, v in fm.items() if v is not None}
    body = f"{text}\n"
    a = anchor_for(area, exclude=rel)
    if a:
        body += f"\nAbout: [{a['title']}](/{a['path']})\n"
    if not a:
        # No page for this parent yet — make one, so the fact links somewhere real instead of
        # dangling (or, worse, anchoring to a sibling fact and calling that the parent).
        leaf = area.split("/")[-1]
        kind = {"people": "person", "apps": "app", "agents": "agent",
                "topics": "topic"}.get(area.split("/")[0], "note")
        parent = ensure_parent(area, title=leaf.replace("-", " ").title(), type=kind)
        a = {"path": parent, "title": leaf.replace("-", " ").title()}
        body += f"\nAbout: [{a['title']}](/{a['path']})\n"
    b.write_raw(rel, dump_note(fm, body))
    return {"id": rel, "path": rel, "type": type, "subject": subj, "body": text,
            "source": source, "about": area, "_created": existing is None}


def facts_under(area: str, *, type: str | None = None) -> list[dict]:
    """The facts/preferences filed under a parent (optionally one type)."""
    b = _bundle()
    out: list[dict] = []
    for meta in b.notes(f"{area}/facts"):
        note = b.read(meta["path"])
        if not note:
            continue
        fm = note.get("frontmatter") or {}
        if type and fm.get("type") != type:
            continue
        out.append({"id": fm.get("id"), "path": note.get("path"), "type": fm.get("type"),
                    "subject": fm.get("title"), "source": (fm.get("source") or {}).get("type"),
                    "body": (note.get("body") or "").split("\n\nAbout:")[0].strip()})
    return out


__all__ = ["slug", "anchor_for", "ensure_parent", "remember_about", "facts_under"]
