---
name: gardener
description: Repair and tidy the knowledge base after the nightly Gardener — fix broken links, backfill missing frontmatter, link orphaned notes, merge stray tags, and compact oversized notes. Use only when explicitly asked to garden/repair/tidy the knowledge base, or when working the Gardener report's worklists.
---

# gardener — repair procedure

Explicit invocation only — never garden as a side effect of another task. Your job is to
APPLY the repairs the knowledge base needs, one category at a time, losing nothing.

Start by calling `kb_read(action='health')` — it returns the LIVE worklist: broken links (each with a
suggested retarget or "remove" instruction), notes missing frontmatter (which fields),
orphaned notes, singleton tags, and the established tags to reuse. Work the categories in
the order below. (The oversized-note compaction worklist is separate — it's in
`notes/gardener-report.md` under "Compaction worklist".) Every change goes through
`kb_write(action='edit')`; read a note in full before editing it.

Golden rule: a wrong repair is worse than an open one. When an item is ambiguous, SKIP it
— the next nightly run surfaces it again. Never invent facts, and never touch `index.md`,
`log.md`, `GRAPH.md`, or anything under `.index` / `.history` — the tools own those.

## 1. Broken links (kb_read(action='health'): "Broken links")

Each row is `/<from>` → `<target>` with an instruction:

- **retarget to `/<suggest>`** — the mechanical pass found the note this link almost
  certainly meant (a rename/move). Open `/<from>`, confirm the link text still makes sense
  for that note, and `kb_write(action='edit')` the link target from the old path to the suggested one. If
  the suggestion is obviously wrong for the sentence, treat it as the no-match case instead.
- **no match found; remove the dead link** — no target exists. Replace the markdown link
  `[text](dead.md)` with just its plain `text`, so the sentence still reads naturally.
  Never leave a half-link or an empty `[]()`.

## 2. Missing frontmatter (kb_read(action='health'): "Missing frontmatter")

Each row names a note and which of `type` / `description` / `tags` is missing. Read the
note, then backfill ONLY what its own content supports:

- `type` — pick the one that fits (e.g. note, meeting, person, topic, decision) from what
  the note actually is. Don't guess from the folder alone if the body says otherwise.
- `description` — one plain sentence summarizing the note. Draw it from the note; never
  embellish.
- `tags` — 1–4 tags. **Strongly prefer kb_read(action='health')'s "Established tags"** over new ones, so
  the vocabulary stays tight. Only add a new tag when nothing established fits.

Apply the frontmatter with a single `kb_write(action='edit')` on the note's `---` block.

## 3. Orphaned notes (kb_read(action='health'): "Orphaned notes")

Nothing links to these. For each, find ONE genuinely related content note (search/read to
confirm the relationship is real) and add a single markdown link to the orphan from that
note's body, in a place where it reads naturally. Rules:

- Link FROM a real content note, never from an `index.md` (code-owned).
- One good inbound link is enough — don't spam links to force connectivity.
- If nothing is genuinely related, LEAVE it orphaned and note that you skipped it. A truly
  standalone note is fine; a forced link is not.

## 4. Singleton tags (kb_read(action='health'): "Singleton tags")

Each tag is used by exactly one note. Merge a singleton into an established tag ONLY when
it's an obvious typo or variant of it (`ml` vs `machine-learning` is a judgment call —
skip; `kubernets` vs `kubernetes` is a typo — merge). To merge, `kb_write(action='edit')` the one note's
`tags` list, replacing the singleton with the established tag. A genuinely unique,
correctly-spelled tag is not a problem — leave it.

## 5. Compaction (report: "Compaction worklist" in notes/gardener-report.md)

For each oversized note, make it SHORTER while it still says everything it said before —
this is compaction, not summarization.

- Merge prose that repeats the same fact in different words.
- Drop items genuinely resolved or superseded — but only when the resolution is stated in
  the note; never infer that something is done.
- NEVER drop an H2 heading, a conflict block (`> ⚠️ **Conflict:** …`), a link, or any fact
  that isn't clearly redundant. Batch a note's cuts into one `kb_write(action='edit')` where possible.
- If you can't shrink it without losing information, leave it alone.

## Never

- Never create or delete notes — only repair and tighten existing ones.
- Never invent, embellish, or add facts while repairing.
- Never touch `index.md`, `log.md`, `GRAPH.md`, or anything under `.index` / `.history`.
