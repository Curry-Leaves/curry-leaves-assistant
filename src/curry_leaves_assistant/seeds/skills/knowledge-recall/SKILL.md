---
name: knowledge-recall
description: How to find and answer from the user's knowledge base. Read-only — searching, reading, following links, citing. Load this before answering a question about the user's projects, people, decisions, apps, or anything they've told you before.
hide: true
---

# Knowledge Base — RETRIEVE

A base of markdown notes under areas like `apps/ topics/ people/ meetings/ notes/`.
Each note's frontmatter carries its `type`, `description` (a one-line summary you can
judge relevance from), `tags`, and often a `source` recording its provenance.

## Procedure

1. `kb_read(action='search', query=...)` -> ranked candidate note(s). Use
   `kb_read(action='list')` to browse by area when the query is vague
   ("what do we know about Atlas" → `kb_read(action='list', area='apps')`).
2. `kb_read(action='read', path=...)` the candidate; judge relevance from its
   description/frontmatter first.
3. Need more? Follow its links and `kb_read(action='links', path=<path>)` for inbound
   neighbours (backlinks); read those too.
4. Answer ONLY from the notes; cite paths. If the answer isn't there, or a link is
   broken, say so — don't guess.
5. Answer COMPLETELY in one pass: pull the fact, its context, and its source before
   replying, so the asker doesn't have to come back.

BATCH your reads: when several candidates or neighbours need reading, issue all those
`kb_read(action='read')` calls together in one turn, not one per turn.

## Reuse what the note already shows

Notes can contain rendered blocks — a `calendar` of dates, a `timeline` schedule, a
`kanban` board, a `chart`. When your answer is about content a note already presents
that way, reproduce that block rather than describing it or drawing your own diagram.

## Staying on the same note

For follow-ups ("when did we discuss this", "which meeting was that"), don't restart the
search. Check the note's own `source` frontmatter first: a note filed from a meeting
carries `source: {type: meeting, id: <recording_id>, …}`, so call
`recordings_read(action='read', recording_id=<that id>)` directly.

## Never

- Never write or edit index.md, log.md, or anything under .index / .history — the tools
  own them. To FILE new knowledge, use the `knowledge-keeper` skill (or delegate to the
  filer); this skill is read-only.
