"""The user, as a person in memory.

There is no separate "profile store" any more. The user is a **person note** — ``people/<name>.md``
marked ``isUser: true`` — and what used to be the profile is simply the facts filed under it, the
same shape as an app's or a topic's facts (see ``stores/memory_router``):

    people/nambi.md                     the person
    people/nambi/facts/answer-style.md  a preference  -> injected into EVERY prompt
    people/nambi/facts/user-name.md     a fact        -> pulled when the question is about it

That uniformity is the whole point: your facts sit in the tree next to the meetings you attended
and the apps you work on, linked to your person note, instead of floating in a bucket that knows
about none of them.

Retrieval splits by TYPE, not by location:
  • ``preference`` — how you like things done. Behavioural, so it matters on every turn.
  • ``fact`` — something true. Only worth prompt space when the question is about it.
"""
from __future__ import annotations

import re

from curry_leaves_assistant.stores import memory_router as router

VALID_TYPES = {"fact", "preference"}
VALID_SOURCES = {"told", "inferred"}

PEOPLE_AREA = "people"


def user_area() -> str:
    """The user's person folder (``people/<slug>``), creating the person note if absent.

    The name comes from the identity setting when set, else a recorded "user name" fact, else a
    neutral default — so a fact about you always has a person to hang off, even before you've
    told the app your name."""
    from curry_leaves_assistant.core import memory_ref
    from curry_leaves_assistant.core import settings as app_settings

    # An existing user person note wins, so the folder never moves once facts hang off it —
    # renaming the identity later must not orphan them.
    b = memory_ref.get()
    for meta in b.notes(PEOPLE_AREA, "person"):
        note = b.read(meta["path"])
        if note and (note.get("frontmatter") or {}).get("isUser"):
            # The person's page sits BESIDE their folder (people/nambi.md -> people/nambi/facts/…),
            # so the area is the path minus ".md" — not its parent directory.
            path = meta["path"]
            return path[:-3] if path.endswith(".md") else path

    name = (app_settings.identity_cfg().get("name") or "").strip() or "You"
    area = f"{PEOPLE_AREA}/{router.slug(name)}"
    router.ensure_parent(area, title=name, type="person",
                         description=f"{name} — the person using Curry Leaves.",
                         extra={"isUser": True, "tags": ["person", "user"]})
    return area


# Subjects that mean "this IS who the person is" rather than "here's a fact about them".
#
# What someone wants to be CALLED titles their note; a formal/legal name is a fact filed under it.
# Both matter and they're often different ("Nambi" vs "Ilayanambi Ponramu") — treating them alike
# meant whichever was recorded last silently renamed the person, so the preferred name could be
# overwritten by the one nobody uses.
_CALLED_SUBJECTS = {"preferred name", "name", "user name", "my name", "nickname", "goes by"}
# "The user's name is Nambi." / "Their name is Nambi" / "Call them Nambi" -> Nambi
_NAME_RE = re.compile(
    r"(?:name\s+is|called|goes\s+by|refers?\s+to\s+(?:them|themselves)\s+as|prefers?\s+"
    r"(?:to\s+be\s+called|the\s+name))\s+([^.,;\n]+)", re.I)


def _name_from(text: str) -> str | None:
    """Pull the actual name out of a name fact's prose, if it's stated plainly."""
    m = _NAME_RE.search(text or "")
    if not m:
        return None
    name = m.group(1).strip().strip("\"'“”")
    # Guard against a sentence that merely mentions naming without giving one.
    return name if name and len(name) <= 60 and not name.lower().startswith("not ") else None


def _prune_empty_dir(area: str) -> None:
    """Delete a memory folder that holds no notes — only the bundle's generated index/log files.

    Renaming a person empties their old folder, but the generated scaffolding keeps it visible in
    the tree as a branch with nothing under it. Never touches a folder with real notes."""
    from curry_leaves_assistant.core import memory_ref, paths

    try:
        b = memory_ref.get()
        if list(b.notes(area)):
            return
        root = (paths.MEMORY_DIR / area).resolve()
        if not root.is_dir() or root == paths.MEMORY_DIR.resolve():
            return
        if not root.is_relative_to(paths.MEMORY_DIR.resolve()):
            return
        if any(p.name not in ("index.md", "log.md") for p in root.rglob("*") if p.is_file()):
            return
        import shutil

        shutil.rmtree(root)
    except Exception as exc:
        print(f"[memory] could not prune {area}: {exc}", flush=True)


def _rename_user(name: str, *, source: str = "told") -> dict:
    """Make the user's person note actually be them: retitle it, and move it (with everything
    filed under it) from ``people/<old>`` to ``people/<slug(name)>``.

    Called when a name is learned. The person page and their facts move together — leaving the
    facts behind would orphan them, and leaving the folder named ``you`` would keep the tree
    reading like a placeholder after the app knows exactly who you are."""
    from cl_memory.util import dump_note
    from curry_leaves_assistant.core import memory_ref

    b = memory_ref.get()
    old = user_area()
    new = f"{PEOPLE_AREA}/{router.slug(name)}"

    note = b.read(f"{old}.md") or {}
    fm = dict(note.get("frontmatter") or {})
    fm.update({"type": "person", "title": name, "isUser": True,
               "description": f"{name} — the person using Curry Leaves.",
               "tags": ["person", "user"], "source": {"type": source}})
    # State the name in prose, not just the title/frontmatter. Search and `recall` match on
    # BODY text, so a note titled "Nambi" whose body never says so answered "what is my
    # name?" with nothing — the KB knew the name structurally but couldn't retrieve it.
    body = (f"{name} — the person using Curry Leaves.\n\n"
            f"The user's name is {name}; call them {name}.\n")

    if new != old:
        # Move the children first so `anchor_for` on the new area sees a real page, then drop the
        # old page. Any note under the old folder comes along, not just facts/.
        for meta in list(b.notes(old)):
            p = meta["path"]
            child = b.read(p)
            if not child or p == f"{old}.md":
                continue
            cfm = dict(child.get("frontmatter") or {})
            if cfm.get("about") == old:
                cfm["about"] = new
            cbody = (child.get("body") or "").split("\n\nAbout:")[0].strip()
            cbody += f"\n\nAbout: [{name}](/{new}.md)\n"
            b.write_raw(new + p[len(old):], dump_note(cfm, cbody))
            b.delete(p, "person renamed")
        b.write_raw(f"{new}.md", dump_note(fm, body))
        if b.read(f"{old}.md") is not None:
            b.delete(f"{old}.md", "person renamed")
        # The bundle writes an index.md/log.md into every folder, so an emptied folder still
        # renders as a branch in the tree — the "you" placeholder would linger with nothing in it.
        _prune_empty_dir(old)
    else:
        b.write_raw(f"{new}.md", dump_note(fm, body))

    return {"id": fm.get("id"), "path": f"{new}.md", "type": "person", "subject": "name",
            "body": name, "source": source, "about": None, "_created": False}


def upsert(text: str, *, type: str = "fact", subject: str | None = None,
           source: str = "inferred", confidence: float = 0.8,
           about: str | None = None) -> dict:
    """Record or correct one durable fact/preference.

    `about` names the parent it belongs to (``apps/cbm``, ``topics/…``). Omit it and the fact is
    about the USER, so it's filed under their person note. Re-using a `subject` corrects the
    existing note in place rather than adding a near-duplicate."""
    if type not in VALID_TYPES:
        type = "fact"
    if source not in VALID_SOURCES:
        source = "inferred"
    area = (about or "").strip("/") or user_area()
    # What the user wants to be CALLED is not a fact to file UNDER them — it's who they are.
    # Recording it as a child note left a person called "You" with a "user name" note dangling
    # beneath, which is both redundant and the wrong thing to read in the tree. Retitle instead.
    # A formal name still files as an ordinary fact, so it's known without renaming anything.
    subj = (subject or "").strip().lower()
    if not about and subj in _CALLED_SUBJECTS:
        name = _name_from(text)
        if name:
            return _rename_user(name, source=source)
    rec = router.remember_about(text, area=area, type=type, subject=subject, source=source)
    return {**rec, "about": None if not about else rec["about"]}


def sync_identity(ident: dict) -> None:
    """Mirror Settings → Identity into the knowledge base.

    What the user types in Settings (or the first-run wizard) is the most reliable thing the
    app knows about them — they stated it directly — yet it only lived in settings.json. The
    KB would show a person note titled from it but hold none of the facts, and `recall` /
    `trace` had nothing to match on. So write it through as ordinary user-stated facts.

    Deliberately narrow:
    - `name` retitles the person note (via upsert's _CALLED_SUBJECTS path), it is NOT a
      child fact — that's the same rule `update_profile` follows.
    - Stable `subject`s mean editing Settings corrects the existing note in place instead of
      appending a near-duplicate every save.
    - `behavior` is a preference (it shapes how replies are written), the rest are facts.
    - source="told": the user stated it, so it outranks anything merely inferred.

    Fails soft — a memory hiccup must never block saving settings.
    """
    name = (ident.get("name") or "").strip()
    if name:
        try:
            _rename_user(name, source="told")
        except Exception:
            pass
        # ALSO file it as an ordinary fact. Retitling alone puts the name only on a
        # `type: person` note, and every profile-recall path (list_all, recall,
        # relevant_for_prompt, facts_under) reads `fact`/`preference` notes under
        # <area>/facts/ — so "what is my name?" matched nothing. The subject is
        # deliberately NOT one of _CALLED_SUBJECTS, or upsert would route it straight
        # back into a rename and file nothing.
        try:
            upsert(f"The user's name is {name}; call them {name}.",
                   type="fact", subject="user's name", source="told", confidence=0.99)
        except Exception:
            pass

    fields = (
        ("work", "work", f"{name or 'The user'} works on: {{}}", "fact"),
        ("workingHours", "working hours", "Usually working: {}", "fact"),
        ("behavior", "how they want assistants to work", "{}", "preference"),
    )
    for key, subject, template, type_ in fields:
        value = (ident.get(key) or "").strip()
        if not value:
            continue
        try:
            upsert(template.format(value), type=type_, subject=subject,
                   source="told", confidence=0.95)
        except Exception:
            pass


def preferences_for_prompt(limit: int = 12) -> list[dict]:
    """The user's PREFERENCES — injected into every agent's prompt.

    Preferences are behavioural ("short answers", "bullets over prose"): they shape how a reply
    should be written whatever the topic, so they're always on. Facts are not — they're pulled by
    `relevant_for_prompt` only when the question is about them."""
    try:
        return router.facts_under(user_area(), type="preference")[:limit]
    except Exception:
        return []


# Cosine floor for "worth spending prompt tokens on for THIS question".
#
# Calibrated against MiniLM on real stored facts, which is the case that matters: a short question
# against a one-line fact scores far lower than intuition suggests. "what should I eat for lunch?"
# vs the user's food fact lands at ~0.29 — correct, and an order of magnitude clear of the
# unrelated facts beneath it (~0.06). A 0.45 floor (borrowed from longer-text comparisons) matched
# nothing at all, and 0.30 still clipped true hits by a hair.
#
# What makes this safe is the SEPARATION between the right answer and the noise, not the absolute
# number — so the floor sits below the true-hit band while staying far above it. A wrong fact reads
# to the model as established truth, so `limit` stays small as the real guard.
_RELEVANT_SIM = 0.25


def relevant_for_prompt(query: str, *, limit: int = 5) -> list[dict]:
    """Facts worth injecting for THIS question — across every parent.

    Searches all facts in the bundle (the user's, an app's, a topic's, an assistant's) and returns
    only those that genuinely match, so asking about CBM surfaces CBM's facts and an unrelated
    question surfaces none. Returns [] without an embedder — keyword matching on a short question
    is too noisy to trust with prompt space."""
    q = (query or "").strip()
    if not q:
        return []
    from curry_leaves_assistant.core import embeddings, memory_ref

    if not embeddings.vector_ready():
        return []
    try:
        b = memory_ref.get()
        cands: list[dict] = []
        for t in ("fact", "preference"):
            for meta in b.notes(type=t):
                note = b.read(meta["path"])
                if note:
                    cands.append(note)
        if not cands:
            return []
        texts = [f"{(n['frontmatter'] or {}).get('title') or ''} {(n.get('body') or '')}".strip()
                 for n in cands]
        vecs = embeddings.embed([q, *texts])
    except Exception as exc:
        # Never fail a run over prompt enrichment — but say so. Swallowing this silently once hid
        # a miscalibrated threshold that made the whole tier a no-op.
        print(f"[memory] relevant_for_prompt skipped: {exc}", flush=True)
        return []
    qv, rest = vecs[0], vecs[1:]
    scored = []
    for v, n in zip(rest, cands):
        sim = sum(a * c for a, c in zip(qv, v))  # L2-normalized -> dot == cosine
        if sim >= _RELEVANT_SIM:
            fm = n["frontmatter"] or {}
            scored.append((sim, {
                "id": fm.get("id"), "type": fm.get("type"),
                "subject": fm.get("subject") or fm.get("title"),
                "body": (n.get("body") or "").split("\n\nAbout:")[0].strip(),
                "path": n.get("path"), "about": fm.get("about"),
            }))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _s, d in scored[:limit]]


def recall(query: str, *, limit: int = 6) -> list[dict]:
    """Facts matching a query, for on-demand tool recall (any parent)."""
    return relevant_for_prompt(query, limit=limit)


def list_all() -> list[dict]:
    """Every fact/preference about the USER, for the UI panel."""
    try:
        return router.facts_under(user_area())
    except Exception:
        return []


def forget(fact_id: str) -> bool:
    """Soft-delete a fact by its note path (-> _archive/, restorable)."""
    from curry_leaves_assistant.core import memory_ref

    b = memory_ref.get()
    rel: str | None = fact_id if fact_id.endswith(".md") else None
    if rel is None:
        hit = next((f for f in list_all() if f.get("id") == fact_id), None)
        path = hit.get("path") if hit else None
        rel = str(path) if path else None
    if not rel or b.read(rel) is None:
        return False
    return bool(b.delete(rel, "forgotten"))


def touch(fact_ids: list[str]) -> None:
    """No-op: facts are ordinary notes now; `uses` isn't tracked on them."""


def close_thread_conn() -> None:
    """No-op: the one bundle owns its connections (kept for callers/tests)."""
