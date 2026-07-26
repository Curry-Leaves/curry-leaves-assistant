# Artifact Store & Presentation Skill — design

> **Status: shipped.** The store, its agent tools, its API, its app page, and the
> presentation seed skill all exist as designed below; §7's implementation plan and
> §"Decisions taken (flag if you disagree)" are kept as the record of *why*, not as open
> questions. Two details have drifted: the knowledge area is now an alias for the memory
> bundle (§1), and the source-priority rule in §6 cites the pre-bundle tool names.
> Artifacts are also indexed as one of the six ⌘K search silos, which this document predates.

Two features that ship together:

1. **Artifact store** — a registry for everything agents generate that is a *deliverable*
   (HTML presentations, reports, one-page sites, diagrams…), with its own directory under
   the app's home dir, an app page to browse it, a tool so agents can save into it, and a
   shareable URL the user can open in any browser — no app, no login — and download from.
2. **Presentation skill** — a seed skill that teaches agents to build self-contained HTML
   slide decks from the knowledge hub (and recordings), touching the web only when the
   user explicitly asks.

The store is deliberately generic: presentations are just the first *kind*. Anything an
LLM generates that outlives the chat turn belongs here.

---

## 1. Storage layout & registry

The store is a new sibling to the knowledge and recordings areas under the app's home dir.
Each artifact is one directory holding a small registry record plus its entry file
(self-contained HTML preferred) and any extra assets, referenced with same-dir relative
paths only.

The registry record for an artifact captures its id, title, kind (presentation, report,
page, diagram, or other), a short description, which agent generated it, optional
provenance back to the chat that produced it, a secret share token, and timestamps.

There is **no central index file** — the registry *is* the directory scan (house style:
recordings work the same way). One artifact = one dir; delete the dir, it's gone from
the registry. Human-readable, hand-editable, rsync-able. The store exposes create, update,
list (newest first), read, delete, and safe path resolution, and it announces saves and
deletions on the event stream so the activity feed sees them.

## 2. Sharing model — capability URLs

Requirement: the user (or someone they send the link to) opens the artifact in a plain
browser without the app or the PIN. The backend already runs an HTTP server; we add a
**public, unguessable capability URL** per artifact — a path that serves the entry file,
its extra assets, and a download endpoint.

- Auth gets one new rule: the artifact path prefix is public — the token IS the auth
  (per-artifact, minted at create, compared in constant time). Regenerating the token
  (a "revoke link" action) invalidates old links.
- Download v1 = the entry HTML file (decks are single-file by design). If an artifact
  has extra assets, download zips the artifact dir on the fly.
- Caveat (known web-mode issue): links embed the port, and web mode currently picks a
  random free port per launch — durable links need the port pinned. The design works
  either way; the docs/UI copy should mention it.

## 3. Agent tool surface

Two new agent tools, mirroring the existing save-output tool:

- **Save artifact** (risk `write`) — takes a title, the full content of the entry file, a
  kind, an optional description, and optionally an existing artifact id (present → update
  that artifact in place instead of creating a new one; the share link stays stable).
  Resolves the generating agent from the trace context. Returns the share URL plus a
  one-line confirmation the agent can relay verbatim: the chat UI already renders markdown
  links.
- **List artifacts** (risk `read`) — id, title, kind, updated date; lets an agent find and
  update an existing deck ("add a slide about X") instead of minting a duplicate.

The assistant seed gains both tools (allow permissions) plus a short "Deliverables" prompt
section: *when the user asks for a presentation/report/page, load the presentation skill,
build it, save the artifact, and reply with the link.* (Seeding is once-only — existing
installs update the agent by hand or delete + restart.)

## 4. App page — Artifacts screen

A new top-level screen (added to the screen set, sidebar, and keep-alive tabs), backed by
its own frontend API against the artifact routes.

The Artifacts screen is a card grid, one card per artifact — kind badge, title, description,
generating agent, updated date. Actions per card:

- **Open** — opens the capability URL in the system browser / new tab (the artifact is
  *experienced* exactly as a recipient would see it — same URL, no special in-app viewer).
- **Copy link** / **Download** / **Revoke link** (regenerate token) / **Delete** (confirm).

An inline sandboxed-iframe preview thumbnail is a nice-to-have, not v1.

## 5. API router

A new router adds token-guarded routes for the registry (list, read one, delete, regenerate
token) and public capability routes (serve entry file, serve escape-checked asset, download
as attachment).

Served HTML gets Content-Security-Policy headers that block outbound requests except
inline images — decks are self-contained by convention (the skill enforces it), and the
CSP keeps a prompt-injected artifact from phoning home from the viewer's browser.

## 6. Presentation skill (seed)

The presentation seed ships an always-loaded operating procedure plus three on-demand
references the agent pulls when it needs them:

- deck types — the 12 common deck types, each with its required slide sequence, what every
  slide must cover, length, and failure mode.
- visual craft — how to draw inline-SVG visuals that read as designed rather than as
  wireframes (fill over outline, layered depth, weight contrast, gradients/patterns/masks),
  plus layout, type, color and motion.
- HTML scaffold — the deck plumbing (slide nav, progress, speaker notes, print-to-PDF,
  light/dark) so design effort goes into the design, not the mechanics.

There is no fill-in deck template: every deck is hand-designed HTML. The references teach
craft and structure; they are deliberately not a house style.

Core rules of the skill:

- **Source priority, hard order**: ① knowledge hub (read the map, search, read the notes;
  cite note paths) → ② recordings when the topic is a meeting or the KB is thin → ③ **web
  only if the user explicitly asked for web content** — never as a silent fallback; if the
  local sources can't fill the deck, say what's missing instead of googling.
- **Output**: ONE self-contained HTML file — inline CSS/JS, no CDN, no external fonts;
  images as inline SVG or data URIs (external image URLs only when the user explicitly
  asked for web images). Keyboard nav, slide counter, click-to-advance, print-to-PDF
  friendly (one slide per page), light/dark following system preference, responsive.
- **Layout menu** (the scaffold ships all of these; pick per-slide): title, agenda,
  bullets, two-column, image+caption, big-number/stat, quote, timeline, comparison
  table, closing/next-steps. Sensible defaults: 8–14 slides, one idea per slide,
  ≤5 bullets of ≤10 words.
- **Last slide = Sources**: note paths / recording ids (+ URLs only if web was used).
- **Delivery**: save the artifact as a presentation, then reply with the share link and
  a one-line description. Updating an existing deck reuses its id.
- **Least back-and-forth**: proceed with defaults (audience: the user; tone: concise
  professional; length: content-driven) and state assumptions in the reply. At most one
  bundled question and only if the topic itself is ambiguous.

## 7. Implementation plan

The build lands in order: paths + store + events; the agent tools; the API plus public
capability routes and CSP; the frontend screen and its API; the seeds (presentation skill
and the assistant prompt update); and a verification pass (agent run → save → open the
capability URL logged out → download; registry list/delete in the UI). The store, tools,
and API must land in that order; the frontend and seeds can proceed in parallel once the
API exists.

## Decisions taken (flag if you disagree)

- **Capability URL over public-by-id**: unguessable token in the path, revocable per
  artifact. No accounts, works cross-device on the LAN.
- **Directory scan over a central index file**: matches recordings; no index to corrupt.
- **Single-file HTML as the convention**, multi-file tolerated: keeps download + share
  trivial and decks portable.
- **No in-app viewer**: Open = the same public URL a recipient sees. One rendering path.
- **Save replaces by id, never versions**: history/versioning is a later feature; the
  registry stays a flat "current state" store.
