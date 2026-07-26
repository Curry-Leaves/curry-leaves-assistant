---
name: knowledge-keeper
description: How the knowledge base is organized, and how to store and retrieve from it.
hide: true
---

# Knowledge Base — operating procedure

A base of markdown notes. The TOOLS maintain index.md, log.md, history, and the
derived .index caches, and the mechanical frontmatter (id, timestamp). You
decide CONTENT only.

## Structure

`kb_read(action='read', path='CONVENTIONS.md')` for the areas, type taxonomy, tag vocabulary, and
house rules — it's the single source of truth; read it once per session before
your first write.

## Note format
```
---
type: <taxonomy>          # required
title: <title>
description: <one factual sentence>   # the note's summary
tags: [..]
source: {type: meeting, id: m-xxxx, turn_range: [12,18]}   # optional provenance
---
Terse, faithful body.
```
ONE CONCEPT PER NOTE — keep notes small/focused; separate person / decision /
component / term / meeting notes. Never build a mega-note.

## Icons (small clip-art, for readability)

Give every note a SMALL leading icon so it reads at a glance. Keep it tiny and
decorative — an aid, never the content.
- Prefix the note's `# Title` heading, and each major `##` section heading, with
  ONE emoji that fits the type/section. Type defaults:
  👤 person · 🧩 component/app · ✅ decision · 📅 meeting · 📖 term/glossary ·
  📌 fact · ⚠️ risk/conflict · 🔗 reference. Pick the closest fit for sections
  (e.g. `## 📋 Notes`, `## 🔨 Action items`). ONE icon per heading — never a row
  of them, never in body sentences.
- When a small pictorial mark adds real clarity (a status badge, a tiny logo-ish
  glyph, a 2–3 part diagram), emit a SMALL inline `<svg>` — the app renders and
  sanitizes it. Cap it at ~16–24px (`width`/`height` on the root `<svg>`), keep
  it a few shapes, and NO `<script>`, `<a>`, `<foreignObject>`, or external refs
  (they're stripped anyway). Prefer emoji for the common case; reach for SVG only
  when an emoji can't express it. Never make the icon large or the focal point.

## Linking (REQUIRED — every note connects to the graph)

NO ORPHANS. Every note MUST have at least one plain markdown link, in either
direction. For EVERY note you create or edit:
- Link to every other note it references, using ABSOLUTE paths:
  `[Title](/area/.../note.md)`.
- Links are ASSOCIATIVE — connect a note to the broader subject it was
  introduced WITH, even without an explicit "X relates to Y" sentence:
    · a PERSON mentioned with an app -> link to that app
      (e.g. "Sara is the data lead on Atlas" -> link sara-kim to /apps/atlas/overview.md)
    · a GLOSSARY term / concept defined in some context -> link to the app or
      concept it came up with
- If you truly can't find a specific relationship, link the note to the MOST
  RELEVANT note from the same input. A standalone note is a bug.

## Conflicts — surface, never silently overwrite

When new input CONTRADICTS an existing note on the same subject and neither is
clearly newer, do NOT replace the old value. Add a conflict block to the note
body and set frontmatter `status: conflicted`:
`> ⚠️ **Conflict:** <A> (<who/meeting>) vs <B> (<who/meeting>) — unresolved.`
Only replace when the new fact clearly SUPERSEDES the old (same subject,
genuinely newer).

## Provenance

When filing from a meeting, set `source: {type: meeting, id: <meeting_id>,
turn_range: [i,j], section: <heading>}` in the note's frontmatter (the
meeting id comes from your context; the turn range is where the fact came
from, when you can tell). This is the only citation step — no separate tool
call. If your context lists a meeting as already ingested, EDIT those notes
with any new facts instead of creating duplicates (idempotent filing).

## STORE (file new knowledge) — measure twice, cut once

0. Nothing durable in the input? File nothing and say so in one line — done. Read
   CONVENTIONS.md ONCE per run for the taxonomy/vocabulary.
1. For each subject, `kb_read(action='search', query=<subject>)` to check whether a note already
   exists — this is your primary lookup (ranked full-text, and it reflects notes you filed EARLIER in
   this same run, so it prevents intra-run duplicates). `kb_read(action='list')` (or
   `kb_read(action='list', area=…)`) when you want to browse everything or an area's contents.
2. Identify the target(s) from the search/list results; `kb_read(action='read', path=...)` the note(s) you'll change.
3. `kb_read(action='links', path=<path>)` for the target's neighbors (inbound backlinks + outbound); read a
   neighbor's frontmatter only if the change might ripple to it.
4. Per durable fact (capture ONLY what was stated — never invent):
   - an exact/near match exists -> `kb_write(action='edit')` it (append a section or update a value). No duplicate.
   - genuinely new -> `kb_write(action='write', path=..., content=...)` a new note under the right area; link it to related notes.
5. Draft the EXACT change: full content for new notes; block `{old,new}` edits
   for updates (replace a whole section/diagram/table block, not a fragment).
   Apply with kb_write(action='write') / kb_write(action='edit'). If a tool errors (old not found ·
   malformed · off-structure · would-shrink), re-read that one file and fix.

## STORE a DOCUMENT (a paged input — uploaded file or pasted text)

The document is already converted to markdown and stored under an Input id
(d-XXXXXXXX); you read it through with tools and file the whole thing in ONE
run — you are not handed the text inline, and you never have to load it all
at once:
1. `inputs(action='outline', doc_id=docId)` → see the document's shape (chunk index · first heading).
2. Go through EVERY chunk in order: `inputs(action='read', doc_id=docId, chunk=i)` for i = 0 … last. From each, pull
   the DURABLE facts. `kb_read(action='search', query=<subject>)` / read the target note FIRST so you MERGE into
   existing notes (an entity may already have a note from an earlier chunk — search reflects
   what you filed this run) instead of duplicating; then `kb_write(action='write')` / `kb_write(action='edit')`, linking
   every note. File as you go, then read the next chunk. Don't stop until the last chunk is read.
3. Documents are not meetings → do NOT set a `source: {type: meeting, ...}` field.
   Finish with a one-line summary of everything you filed.

## RETRIEVE (answer a question)

1. `kb_read(action='search', query=...)` -> ranked candidate note(s). `kb_read(action='list')` to browse by area if the
   query is vague ("what do we know about Atlas" → `kb_read(action='list', area='apps')`).
2. `kb_read(action='read', path=...)` the candidate; judge relevance from its description/frontmatter first.
3. Need more? follow its ABSOLUTE links and `kb_read(action='links', path=<path>)` for inbound neighbors
   (backlinks); read them.
4. Answer ONLY from the notes; cite paths. If absent or a link is broken, say so — don't guess.
5. Answer COMPLETELY in one pass: pull everything the question needs (the fact, its
   context, its source) before replying, so the asker doesn't have to come back.

BATCH your reads: when more than one candidate/neighbor needs reading, issue all of
those `kb_read(action='read')` calls together in one turn, not one call per turn.

## Never

- Never write or edit index.md, log.md, or anything under .index / .history — the tools own them.
