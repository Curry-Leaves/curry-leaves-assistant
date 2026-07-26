"""Tools that let any agent save a generated deliverable (presentation, report, page,
diagram) into the artifact store and hand the user a link — no app, no login needed to
open it. See stores/artifact_store.py for the registry and api/artifacts.py for the
public capability routes these links point at.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from curry_leaves.core.tools import ToolResult

from curry_leaves_assistant.stores import artifact_store


def _err(msg: str) -> ToolResult:
    return ToolResult(content=msg, is_error=True)


# Budget for images handed back to the model in ONE call. The kernel deliberately does
# not truncate these (its result cap counts characters of prose, which is meaningless for
# base64), so bounding them is this tool's job.
MAX_IMAGES_DEFAULT = 4      # when the model didn't name specific paths
MAX_IMAGES_EXPLICIT = 8     # when it did — still bounded
MAX_IMAGE_BYTES = 3_750_000  # ~5 MB once base64 inflates it, the provider ceiling
MAX_TOTAL_IMAGE_BYTES = 8_000_000
MAX_MANIFEST_ENTRIES = 200   # keep the text well under the kernel's result cap


def _image_blocks(artifact_id: str, assets: list[dict], wanted: list[str] | None):
    """(blocks, notes) — the assets to show the model, within budget.

    Returns no blocks at all on a kernel that predates image-carrying tool results, so
    the action degrades to a text manifest instead of failing.
    """
    try:
        from curry_leaves.core.messages import ImageBlock
    except ImportError:
        return [], ["(this kernel cannot attach images to a tool result — listing only)"]
    if "images" not in ToolResult.model_fields:
        return [], ["(this kernel cannot attach images to a tool result — listing only)"]

    import base64

    from curry_leaves_assistant.core import image_meta

    viewable = [
        a for a in assets
        if image_meta.UPLOAD_IMAGE_TYPES.get(Path(a["path"]).suffix.lower())
        in image_meta.MODEL_VIEWABLE
    ]
    if wanted is not None:
        by_path = {a["path"]: a for a in viewable}
        viewable = [by_path[p] for p in wanted if p in by_path]
        cap = MAX_IMAGES_EXPLICIT
    else:
        cap = MAX_IMAGES_DEFAULT

    blocks, notes, total = [], [], 0
    shown = 0
    for a in viewable:
        if shown >= cap:
            notes.append(
                f"Showing {shown} of {len(viewable)} viewable image(s). Call again with "
                f"paths=[...] for specific ones."
            )
            break
        if a["size"] > MAX_IMAGE_BYTES:
            notes.append(f"{a['path']} is too large to display ({a['size'] // 1024} KB).")
            continue
        if total + a["size"] > MAX_TOTAL_IMAGE_BYTES:
            notes.append(f"Stopped before {a['path']} — per-call image budget reached.")
            break
        p = artifact_store.asset_path(artifact_id, a["path"])
        if p is None:
            continue
        data = p.read_bytes()
        media = image_meta.UPLOAD_IMAGE_TYPES.get(Path(a["path"]).suffix.lower(), "image/png")
        blocks.append(ImageBlock(
            source=base64.b64encode(data).decode(),
            media_type=media,
            name=a["path"],
        ))
        total += a["size"]
        shown += 1
    return blocks, notes


def _share_url(meta: dict) -> str:
    from curry_leaves_assistant.core import server_info

    return f"{server_info.base_url()}/a/{meta['id']}/{meta['shareToken']}/"


def _save_and_share(*, title: str, content: str, kind: str, description: str | None,
                     artifact_id: str | None) -> ToolResult | dict:
    """Shared save-then-share path for the artifacts `save` action.
    Returns the saved meta on success (build the link with _share_url), or a
    ToolResult(is_error=True) if artifact_id doesn't exist."""
    from curry_leaves_assistant.core import events, trace_ctx

    if artifact_id:
        meta = artifact_store.update(
            artifact_id, content, title=title, kind=kind, description=description)
        if meta is None:
            return ToolResult(content=f"No artifact {artifact_id}", is_error=True)
    else:
        meta = artifact_store.create(
            title, content, kind=kind, description=description,
            agent_id=trace_ctx.current_agent_id())
    events.emit("artifact.saved", payload=meta, entity_id=meta["id"], label=meta["title"])
    return meta


class ArtifactsReadTool:
    """Read side of artifacts, action-dispatched: find them or read one. Read-only, so it
    never prompts for permission. Saving/updating an artifact lives in the separate
    `artifacts` write tool. Revising an existing one is: list → read (get current content)
    → artifacts(action='save') with the same artifact_id."""
    name = "artifacts_read"
    description = (
        "Read standalone shareable ARTIFACTS — action: list | read | assets.\n"
        "• list: existing artifacts (id, title, kind, description, updated-at) — NOT dashboard "
        "tiles (use `dashboard_read`), NOT recordings (use `recordings_read`), NOT KB notes "
        "(use `kb_read`).\n"
        "• read: one artifact's entry file by id — ALWAYS read before updating.\n"
        "• assets: the reference images the USER uploaded for this artifact (logos, photos, "
        "screenshots) — returns the file list AND SHOWS you the images themselves. Check this "
        "before designing a presentation or page: a real photo the user supplied beats anything "
        "you can draw. Reference them in your HTML by the relative path shown "
        "(<img src=\"assets/logo.png\">).\n"
        "To save or update an artifact, use the `artifacts` tool."
    )
    risk = "read"

    class Args(BaseModel):
        action: Literal["list", "read", "assets"] = Field(description="list | read | assets")
        # read / assets
        artifact_id: str | None = Field(default=None, description="read/assets: the id (a-XXXXXXXX).")
        # list
        limit: int = Field(default=20, description="list: max artifacts to return (newest first).")
        # assets
        paths: list[str] | None = Field(
            default=None,
            description="assets: show these specific asset paths (as listed). Omit to see the "
                        "first few.")
        include_images: bool = Field(
            default=True,
            description="assets: set false for a filename listing only, without the images.")

    schema = Args
    timeout = None

    async def run(self, args: "ArtifactsReadTool.Args", ctx, signal) -> ToolResult:
        if args.action == "list":
            items = artifact_store.list_artifacts()[: max(1, args.limit)]
            if not items:
                return ToolResult(content="No artifacts yet.")
            view = [{"id": m["id"], "title": m["title"], "kind": m["kind"],
                     "description": m.get("description"), "updatedAt": m.get("updatedAt")} for m in items]
            return ToolResult(content=json.dumps(view, indent=2))

        if args.action == "assets":
            if not args.artifact_id:
                return _err("assets requires `artifact_id`.")
            meta = artifact_store.read_meta(args.artifact_id)
            if meta is None:
                return _err(f"No artifact {args.artifact_id}")
            assets = artifact_store.list_assets(args.artifact_id)
            if not assets:
                return ToolResult(
                    content=f"[{meta['id']} · {meta['title']}] has no reference assets. "
                            "The user can add images to it from the Artifacts screen.")
            listed = assets[:MAX_MANIFEST_ENTRIES]
            lines = [json.dumps(listed, indent=2)]
            if len(assets) > len(listed):
                lines.append(f"({len(assets) - len(listed)} more not listed.)")
            blocks, notes = ([], [])
            if args.include_images:
                blocks, notes = _image_blocks(args.artifact_id, assets, args.paths)
            lines.extend(notes)
            lines.append(
                "Reference these in the artifact's HTML by the RELATIVE path exactly as "
                "shown above, e.g. <img src=\"assets/logo.png\">. Do not inline them as "
                "data URIs, do not use absolute URLs, and do not invent a path that isn't "
                "listed here."
            )
            if blocks:
                lines.append(f"The {len(blocks)} image(s) above are attached as visual content.")
            head = f"[{meta['id']} · {meta['title']}] {len(assets)} reference asset(s):"
            text = "\n\n".join([head, *lines])
            # `images` only exists on kernel >= 2.1; _image_blocks returns none below that,
            # and passing the field at all would be a validation error there.
            if blocks:
                return ToolResult(content=text, images=blocks)
            return ToolResult(content=text)

        # read
        if not args.artifact_id:
            return _err("read requires `artifact_id`.")
        meta = artifact_store.read_meta(args.artifact_id)
        if meta is None:
            return _err(f"No artifact {args.artifact_id}")
        header = f"[{meta['id']} · {meta['title']} · {meta['kind']} · updated {meta.get('updatedAt')}]"
        # Note: some legacy decks carry a source.md sidecar from the retired markdown
        # builder — we deliberately ignore it and return the rendered HTML entry, since
        # the only edit path now is hand-written HTML via the save action.
        entry = artifact_store.entry_path(args.artifact_id)
        if entry is None:
            return _err(f"Artifact {args.artifact_id} has no entry file")
        try:
            body = entry.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _err(f"{header} entry file is binary — cannot display as text")
        return ToolResult(content=f"{header} entry file ({meta['entry']}) — update via "
                                  f"artifacts(action='save', artifact_id=...):\n\n{body}")


class ArtifactsTool:
    """Write side of standalone shareable deliverables (presentations, reports, pages,
    diagrams), each with its own public link — action: save. To find or read artifacts use
    the read-only `artifacts_read` tool. Revising an existing one is: artifacts_read list →
    read (get current content) → save with the same artifact_id."""
    name = "artifacts"
    description = (
        "Save standalone shareable ARTIFACTS with a public link — action: save.\n"
        "• save: save a HAND-WRITTEN deliverable (presentation/deck, report, one-page site, "
        "diagram) and get back a link the user opens in any browser (no app, no login). This "
        "is THE way to build a presentation: author ONE self-contained HTML file you design "
        "from scratch (layout, palette, slide nav, print-to-PDF, theming) as `content` — no "
        "templated deck tool, you own the design. Inline all CSS/JS (no external requests or "
        "fonts) unless plain text/markdown suits. IMAGES: reference images the user uploaded "
        "to this artifact by relative path (<img src=\"assets/logo.png\">) — see "
        "`artifacts_read(action='assets')`; otherwise draw inline SVG or use data: URIs. "
        "Pass `artifact_id` to UPDATE in "
        "place (link stays the same); omit to create. Before revising, "
        "`artifacts_read(action='read')` the current content first so the new version "
        "doesn't drop what the old one had.\n"
        "To find or read existing artifacts, use `artifacts_read` (action: list | read | assets)."
    )
    risk = "write"

    class Args(BaseModel):
        action: Literal["save"] = Field(default="save", description="save")
        title: str | None = Field(default=None, description="save: display title, e.g. 'Q3 Atlas Review'.")
        content: str | None = Field(default=None, description="save: full entry-file content (typically complete HTML).")
        kind: str = Field(default="presentation", description="save: presentation | report | page | diagram | other.")
        description: str | None = Field(default=None, description="save: one factual sentence about what it contains.")
        artifact_id: str | None = Field(
            default=None,
            description="save: existing artifact id to UPDATE in place (omit to create).")

    schema = Args
    timeout = None

    async def run(self, args: "ArtifactsTool.Args", ctx, signal) -> ToolResult:
        # save
        if not args.title or args.content is None:
            return _err("save requires `title` and `content`.")
        result = _save_and_share(
            title=args.title, content=args.content, kind=args.kind,
            description=args.description, artifact_id=args.artifact_id)
        if isinstance(result, ToolResult):
            return result
        if args.artifact_id:
            # Older decks (built via the retired markdown path) may have a source.md
            # sidecar; the entry is hand-written now, so drop it — it no longer matches.
            artifact_store.delete_asset(args.artifact_id, "source.md")
        return ToolResult(
            content=f"Saved '{args.title}' ({result['id']}). Share link: {_share_url(result)}\n"
                    f"Relay this link back to the user as a clickable markdown link.")


ARTIFACT_TOOLS = [ArtifactsReadTool(), ArtifactsTool()]
