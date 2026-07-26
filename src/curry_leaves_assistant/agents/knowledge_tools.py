"""Knowledge-base file tools for the curation agents (v2: trimmed).

Five generic, bundle-scoped operations — read / search / write / edit / delete —
plus two paged-document-reading tools for the filer. The agent decides CONTENT;
the tools do the mechanical bookkeeping in code (atomic + sandboxed writes,
validation guards, history snapshots, OKF log.md, per-dir index.md, and the
derived .index map + backlinks). Guards error with actionable messages so the
agent re-reads and corrects only when something is genuinely off.

Provenance is a `source:` frontmatter field set at write time (no separate
citation tool). Gardening runs on a schedule / HTTP route, not as an
agent-invoked tool.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from curry_leaves.core.tools import ToolResult

from curry_leaves_assistant.domain import knowledge


def _err(msg: str) -> ToolResult:
    return ToolResult(content=msg, is_error=True)


class KbReadTool:
    """Read side of the knowledge base, action-dispatched — the safe, read-only surface an
    agent uses to orient and answer. Writing notes lives in the separate `kb_write` tool
    (risk=write), so a read-only agent can hold `kb_read` without any mutate capability.
    Actions:
      • search — ranked full-text; the primary way to find notes about a topic.
      • read   — one note by bundle-relative path (or 'skill://<name>[/<path>]').
      • list   — the map of the base (path · title · type), optionally filtered.
      • links  — a note's outbound links + inbound backlinks.
      • health — the LIVE issue worklist (broken links, missing frontmatter, orphans, …)."""
    name = "kb_read"
    description = (
        "Read the KNOWLEDGE BASE — action: search | read | list | links | health.\n"
        "• search: ranked search over title/tags/aliases/body — your primary way to find notes "
        "ABOUT a topic; prefer over list. Matches on MEANING as well as wording, so search for "
        "the idea you want rather than guessing the note's exact words.\n"
        "• read: one file by bundle-relative path (e.g. 'apps/atlas/overview.md', "
        "'CONVENTIONS.md'). To read a SKILL's body or a bundled file the path MUST start "
        "'skill://<name>' (e.g. 'skill://presentation' or "
        "'skill://presentation/references/visual-craft.md'); a bare path is treated as a "
        "KB path and will 404.\n"
        "• list: the map of the base (path · title · type), optional `area`/`type` filter.\n"
        "• links: a note's outbound links and inbound backlinks — check before editing.\n"
        "• health: the LIVE repair worklist (broken links w/ suggested retargets, notes "
        "missing type/description/tags, orphans, singleton tags, established tags) — call at "
        "the START of a maintenance run, then fix each with kb_write edit.\n"
        "Notes only — NOT recordings, artifacts, or dashboards."
    )
    risk = "read"

    class Args(BaseModel):
        action: Literal["search", "read", "list", "links", "health"] = Field(
            description="search | read | list | links | health")
        # search
        query: str | None = Field(default=None, description="search: search terms.")
        limit: int = Field(default=8, description="search: max results.")
        # read / links
        path: str | None = Field(
            default=None,
            description="read/links: bundle-relative note path (or its id for links; or "
                        "'skill://<name>[/<path>]' for read).")
        # list
        area: str | None = Field(default=None, description="list: top-level area to limit to (e.g. 'people', 'apps'). Omit for all.")
        type: str | None = Field(default=None, description="list: only notes of this frontmatter `type`. Omit for all.")

    schema = Args
    timeout = None

    async def run(self, args: "KbReadTool.Args", ctx, signal) -> ToolResult:
        if args.action == "search":
            if not args.query:
                return _err("search requires `query`.")
            hits = knowledge.search(args.query, limit=args.limit)
            if not hits:
                return ToolResult(content="No matches.")
            lines = [f"- {h['path']} — \"{h['title']}\" ({h['type']}): {h['snippet']}" for h in hits]
            return ToolResult(content="\n".join(lines))

        if args.action == "read":
            if not args.path:
                return _err("read requires `path`.")
            if args.path.startswith("skill://"):
                resolver = getattr(ctx, "resolve_skill", None)
                body = resolver(args.path[len("skill://"):]) if callable(resolver) else None
                return ToolResult(content=body) if body else _err(f"No such skill: {args.path}")
            text = knowledge.read_file(args.path)
            if text is None:
                return _err(f"No such file: {args.path}")
            return ToolResult(content=text)

        if args.action == "list":
            notes = knowledge.list_notes(subdir=args.area, type=args.type)
            if not notes:
                return ToolResult(content="No notes." + (" (empty knowledge base — nothing filed yet)" if not args.area and not args.type else ""))
            lines = [f"{len(notes)} note(s):"]
            lines += [f"- /{n['path']} — {n['title'] or '(untitled)'} `{n['type'] or 'note'}`" for n in notes]
            return ToolResult(content="\n".join(lines))

        if args.action == "links":
            if not args.path:
                return _err("links requires `path`.")
            links = knowledge.note_links(args.path)
            if not links["outbound"] and not links["inbound"]:
                return ToolResult(content=f"/{args.path} has no links (orphan).")
            out = ", ".join(f"/{l['path']}" for l in links["outbound"]) or "(none)"
            inb = ", ".join(f"/{l['path']}" for l in links["inbound"]) or "(none)"
            return ToolResult(content=f"Outbound (this → others): {out}\nInbound (others → this / backlinks): {inb}")

        # health
        h = knowledge.health()

        # Suggest a retarget for each broken link so the agent applies the fix instead of
        # re-deriving it — same conservative logic the Gardener writes into its report.
        note_set = {n["path"] for n in knowledge.list_notes()}
        broken = []
        for b in h["broken_links"]:
            suggest = _suggest_retarget(b["target"], note_set)
            broken.append(f"  /{b['from']} → {b['target']}"
                          + (f"  [retarget to /{suggest}]" if suggest else "  [no match — remove the dead link]"))

        missing = sorted(set(h["missing_type"]) | set(h["missing_description"]) | set(h["missing_tags"]))
        def _gaps(p: str) -> str:
            g = [k for k, lst in (("type", h["missing_type"]), ("description", h["missing_description"]),
                                  ("tags", h["missing_tags"])) if p in lst]
            return f"  /{p} — missing {', '.join(g)}"

        established = knowledge.established_tags()
        total = (len(h["broken_links"]) + len(missing) + len(h["orphans"]) + len(h["singleton_tags"]))
        if total == 0:
            return ToolResult(content="No health issues — the knowledge base is clean.")

        sections = [f"{total} open issue(s):", ""]
        sections += ["Broken links (retarget to the suggestion if it's clearly the same note, else remove the link):",
                     *(broken or ["  (none)"]), ""]
        sections += ["Missing frontmatter (backfill from the note's own content; reuse established tags below):",
                     *([_gaps(p) for p in missing] or ["  (none)"]), ""]
        sections += ["Orphaned notes (add ONE link from a genuinely related content note — never an index.md):",
                     *([f"  /{p}" for p in h["orphans"]] or ["  (none)"]), ""]
        sections += ["Singleton tags (merge into an established tag ONLY if it's an obvious typo/variant, else leave):",
                     "  " + (", ".join(h["singleton_tags"]) or "(none)"), ""]
        sections += ["Established tags (prefer these when backfilling): " + (", ".join(established) or "(none)")]
        return ToolResult(content="\n".join(sections))


def _suggest_retarget(target: str, note_set: set[str]) -> str | None:
    """Shared with the Gardener: where a dangling link probably belongs now, or None on any
    ambiguity. Kept here so the tool and the batch report agree on every suggestion."""
    import os
    slug = os.path.splitext(os.path.basename(target))[0]
    hit = knowledge.resolve(slug.replace("-", " "))
    if hit and hit["path"] in note_set and hit["path"] != target:
        return hit["path"]
    base = os.path.basename(target)
    same_name = [rel for rel in note_set if os.path.basename(rel) == base and rel != target]
    return same_name[0] if len(same_name) == 1 else None


class KbWriteTool:
    """Write side of the knowledge base, action-dispatched (risk=write, permission-gated —
    a read-only agent holds `kb_read` and never reaches this). Actions:
      • write  — create a new note or fully rewrite one (full markdown w/ frontmatter).
      • edit   — targeted {old,new} block replacements on an existing note.
      • delete — soft-delete (moves to _archive, restorable).
    history / log.md / index.md / .index are maintained for you on every action."""
    name = "kb_write"
    description = (
        "Modify the KNOWLEDGE BASE — action: write | edit | delete.\n"
        "• write: create a NEW note or fully rewrite one — for a durable "
        "fact/decision/person/concept worth remembering long-term. `content` is complete "
        "markdown with YAML frontmatter (non-empty `type`, plus title, one-sentence "
        "description, tags). NOT a one-off deliverable (use `artifacts`) or a per-recording "
        "output (use `recording_output`). Prefer `edit` for a targeted change.\n"
        "• edit: targeted replacements on an EXISTING note — read it first. Each edit's "
        "`old` must match exactly once; replace a whole block (section/diagram/table), not a "
        "fragment. All apply atomically.\n"
        "• delete: soft-delete a note (moves to _archive, keeps history — restorable). "
        "Inbound links may dangle; ok.\n"
        "history / log.md / index.md / .index are maintained for you."
    )
    risk = "write"

    class Edit(BaseModel):
        old: str = Field(description="Exact current text to replace (unique in the file) — a whole block.")
        new: str = Field(description="Replacement text.")

    class Args(BaseModel):
        action: Literal["write", "edit", "delete"] = Field(description="write | edit | delete")
        path: str = Field(description="Bundle-relative path (write: ends in .md under a defined area, e.g. apps/, topics/).")
        # write
        content: str | None = Field(
            default=None, description="write: full markdown — ---\\ntype: ...\\ntitle: ...\\n---\\n\\nbody")
        # edit
        edits: list["KbWriteTool.Edit"] | None = Field(
            default=None, description="edit: one or more {old,new} block replacements, applied in order.")
        # delete
        reason: str | None = Field(default=None, description="delete: why it's being removed.")

    schema = Args
    timeout = None

    async def run(self, args: "KbWriteTool.Args", ctx, signal) -> ToolResult:
        if args.action == "write":
            if args.content is None:
                return _err("write requires `content`.")
            try:
                res = knowledge.write_file(args.path, args.content)
            except ValueError as exc:
                return _err(f"Rejected: {exc}")
            return ToolResult(content=f"Saved {res['type']}: {res['title']} ({res['path']})")
        if args.action == "edit":
            if not args.edits:
                return _err("edit requires at least one `edits` entry.")
            try:
                res = knowledge.kb_edit(args.path, [e.model_dump() for e in args.edits])
            except (ValueError, FileNotFoundError) as exc:
                return _err(f"Edit failed: {exc}")
            return ToolResult(content=f"Edited {res['title']} ({res['path']})")
        # delete
        ok = knowledge.delete_note(args.path, args.reason)
        if not ok:
            return _err(f"No such note: {args.path}")
        return ToolResult(content=f"Archived {args.path}" + (f" — {args.reason}" if args.reason else ""))


class InputsTool:
    """Paged reading of an ingested input document, action-dispatched:
      • outline — the chunk map (index · size · first heading) to plan what to read.
      • read    — one chunk by index; page through in order to cover the whole doc
                  without holding it all in context at once."""
    name = "inputs"
    description = (
        "Read an ingested INPUT document in chunks — action: outline | read.\n"
        "• outline: the chunk map (index · size · first heading of each) so you can see its "
        "shape and plan which chunks to read.\n"
        "• read: ONE chunk by 0-based index, with paging info (index, total, whether more "
        "follows). Read chunks in order to cover the whole document without holding it all in "
        "context at once — file the durable facts from each chunk before the next."
    )
    risk = "read"

    class Args(BaseModel):
        action: Literal["outline", "read"] = Field(description="outline | read")
        doc_id: str = Field(description="The input id (d-XXXXXXXX) from your task.")
        chunk: int = Field(default=0, description="read: 0-based chunk index to read.")

    schema = Args
    timeout = None

    async def run(self, args: "InputsTool.Args", ctx, signal) -> ToolResult:
        from curry_leaves_assistant.stores import inputs_store

        if args.action == "outline":
            o = inputs_store.outline(args.doc_id)
            if o is None:
                return _err(f"No such input: {args.doc_id}")
            lines = [f"{o['title'] or 'document'} — {o['chunkCount']} chunk(s):"]
            lines += [f"  [{c['index']}] {c['head']}  ({c['chars']} chars)" for c in o["chunks"]]
            return ToolResult(content="\n".join(lines))
        # read
        c = inputs_store.chunk(args.doc_id, args.chunk)
        if c is None:
            return _err(f"No such input/chunk: {args.doc_id} #{args.chunk}")
        more = f" · MORE follows (next: {args.chunk + 1})" if c["has_more"] else " · LAST chunk"
        header = f"[input {c['docId']} · {c['title'] or 'document'} · chunk {c['index'] + 1}/{c['total']}{more}]"
        return ToolResult(content=f"{header}\n\n{c['text']}")


KNOWLEDGE_TOOLS = [KbReadTool(), KbWriteTool(), InputsTool()]
