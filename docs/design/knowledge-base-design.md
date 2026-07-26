# Curry Leaves Knowledge Base — Framework & Tools Design (v2: simplify + cost-optimize)

> ⚠️ **Historical design document — superseded. Do not treat as current.**
>
> This was a *forward-looking proposal*, and the simplification it argued for shipped — but in
> a materially different form. Read [architecture.md](../architecture.md) §4 for the design as
> built; the memory domain module is the authoritative source. This file is kept for the
> rationale behind the simplification (why one skill per capability beat a 4-agent write
> pipeline, why the tool layer rather than the model should carry mechanical correctness),
> which still holds.
>
> Specifically, the following in this document are **no longer true**:
>
> - **"OKF" naming and the second reference implementation** — the engine is now a standalone
>   memory package, imported under its own name; the old OKF import does not exist.
> - **The bundle root is not the knowledge dir** — it is the memory dir, and it is **one bundle
>   holding the KB *and* every memory kind** (semantic, private, episodic, consolidated),
>   partitioned by each note's `type:` field. The knowledge dir is now just a back-compat alias.
> - **§6 "Tier 3 (embeddings) stays deferred"** — flatly wrong now. The vector tier **shipped**:
>   a small local embedding model runs in-process, and search is hybrid keyword+vector fused by
>   reciprocal rank in the same index.
> - **There is no root `index.md` hub**, and no conventions file — the conventions live in the
>   memory package. The derived index files described in §6 are not the shipped indexing model
>   either.
> - **The area list** is `apps · topics · people · meetings · notes · memory` — `_archive` is
>   not an area, and the whole memory partition is.
> - **The five-tool table in §5** does not match the shipped surface. The real tools are
>   action-dispatched: one read tool (search·read·list·links·health) and one write tool
>   (write·edit·delete).
>
> ---

> An agent-managed knowledge base for curry-leaves. Captures meetings,
> brain dumps, and "remember this" chats into a portable markdown bundle that
> stays well-organized **and** fast to query as it grows without bound.

## 0. Why this revision

The current implementation already works and is *more* featureful than it needs to be:
typed graph edges with nightly-derived inverses, per-note provenance sidecars, a 4-agent
write pipeline (planner → keeper, plus a separate doc-filer and maintainer), 10 top-level
areas. Every one of those agents is pinned to a frontier model — including mechanical,
high-volume paths like filing a single meeting note or compacting an oversized file.

Comparing it against a second reference implementation (also OKF-based) surfaces the gap:
the reference gets the same job done with **one skill per capability** (explore, capture,
garden), **no separate planner/reviewer agents**, and — the important part — **every agent,
including the gardener, runs on a free/local model**, not a frontier API. It affords this
because the tool layer (not the model) carries all the mechanical correctness: path
generation, frontmatter bookkeeping, hub and log upkeep, guard rails that force a re-read
before a destructive edit. The model's job shrinks to "decide content and call the right
tool" — well within a small model's ability.

curry-leaves' tool layer already does the same kind of bookkeeping — atomic writes, stable
ids, derived indexes, history, log — so it's already most of the way to running on a cheap
model. It just carries more structure than one user's personal KB needs, and it hasn't tried
a cheap model tier at all. This revision does two things in order: **(1) simplify** the shape
down to what's actually load-bearing, **(2) cost-optimize** by moving the mechanical,
high-volume agents to a local/cheap model and reserving the frontier model for the one step
that benefits from it.

---

## 1. The one principle that makes scale + organization coexist

**Capture fast on the hot path; organize in the background on the cold path.**

Unchanged from v1. Ingestion is cheap, append-biased, never blocks the user.
A separate, batched **Maintainer** does global organization (dedup, promote,
reindex). What changes in this revision is *how much scaffolding sits on the
hot path* (less) and *what runs it* (a cheap model, mostly).

| | Hot path (per input) | Cold path (scheduled/batched) |
|---|---|---|
| Trigger | meeting / brain dump / "remember this" / doc upload | nightly + on backlog threshold |
| Goal | never lose anything, place *roughly* right | make it *well-organized* |
| Speed | seconds, non-blocking | minutes, async, off the user's path |
| Agent(s) | **one** filer agent (no planner hop) | code passes (free) + one compactor agent |
| Model | cheap/local | cheap/local (code passes are free either way) |

---

## 2. Data model — simplified

### 2.1 Bundle layout

Collapse the 10-area taxonomy to the 6 that are actually distinct concerns. Decisions and
glossary fold into topics and per-app decisions (a decision almost always belongs to an app
or project, and a glossary term is a term-typed note wherever it's first used — no separate
top-level bucket needed). Orgs is dropped until there's a real multi-org use case; add it
back the day it's needed, the same way the reference conventions file says to.

The bundle keeps a root hub and a global change log, per-application sub-bundles (each with
its own hub and log, including its own decisions), evergreen topics, people, one note per
meeting, a catch-all notes area for brain dumps and inbox-ish captures, and an archive for
soft-deleted notes (never hard-delete). Each app sub-bundle stays self-contained — that
sharding property is worth keeping as-is.

### 2.2 `type` taxonomy — unchanged, still open-ended

`app` · `component` · `decision` · `convention` · `integration` · `incident` ·
`concept` · `technique` · `architecture` · `metric` · `person` · `meeting` ·
`term` · `paper` · `fact` · `preference`

The taxonomy → directory mapping and the tag vocabulary move into a **conventions file** at
the bundle root (the reference's pattern) as the single source of truth, instead of being
duplicated across skill prose and code constants. Skills and code both read it; changing the
taxonomy is a doc edit, not a prompt edit in three places.

### 2.3 Frontmatter — trim to what's actually read

Keep the fields that are actually read on retrieval: the required note type, a title and
one-sentence description, tags, a timestamp, a stable id that survives moves and renames,
aliases, a status (active/archived/conflicted), and a source pointer to the input that
produced the note.

**Drop from the default write path** (keep the mechanism since it's cheap to leave, but stop
asking every write-agent to populate it):

- `confidence` — nothing currently reads it to route review; it's dead weight in every
  prompt. Reintroduce only if a review queue ships.
- prerequisites / typed relation keys (`part_of`, `depends_on`, etc.) and their
  nightly-derived inverses (`used_by`, `contains`, …) — real value for a large graph, but for
  a personal KB plain markdown links + a backlinks index answer "what connects to this" just
  as well, at zero gardening cost. **Keep plain links as the only edge type.** If the graph
  later needs typed, directional edges (e.g. for a proper GraphRAG hop-scoring feature),
  that's an additive change to notes that already exist — nothing to migrate away from.
- Per-note provenance sidecars and the cite tool — valuable for meeting-sourced facts
  specifically, but it added a whole tool + a call-per-note discipline to every filer prompt.
  Fold it into the source frontmatter field instead (type, source id, optional turn range).
  One field, no sidecar file, no extra tool call, same traceability.

### 2.4 Body conventions — unchanged

`# Schema` · `# Intuition` · `# When to use` · `# See also` · `# Citations`,
used only where content warrants the heading (never an empty section —
the reference's rule, worth adopting explicitly).

---

## 3. Tool surface — trim to four verbs

Keep the single-mutator tool layer (atomic writes, stable ids, hub/log upkeep, derived map
and backlinks — these are genuinely good and cheap, unrelated to model cost). Trim the tool
set exposed to agents from nine down, dropping the ones that only existed to serve machinery
we're removing. What's left: reading a note or index or skill procedure; keyword search;
creating or fully rewriting a note (with the existing structure/parse/shrink guards);
targeted block replacement on an existing note; and soft-delete to the archive.

Cut the cite tool (folded into the source frontmatter, §2.3), the document-intake discovery
tools (keep the *mechanism* for paged document intake — that's a real cost control for huge
docs — but a single filer agent can be hinted with the doc id directly instead of routing
through three separate discovery tools), and the run-gardener tool (the gardener runs on a
schedule / code trigger, not as something an agent decides to invoke mid-conversation).

---

## 4. Agents — one filer, one compactor, both cheap

Collapse the write-side pipeline (planner → keeper, plus the separate doc-filer) into **one
filer agent** that does what the doc-filer already does today: read the keeper skill once,
read-then-write directly, no separate planning hop reviewed by a second agent. The reference
runs its equivalent (capture, invoked as one skill inside one agent turn) with zero separate
planner and gets equivalent quality, because the *skill* — not a second agent's judgment — is
what enforces "search before create," "no orphans," "conflicts surface, don't overwrite."

Conceptually the write path becomes a single hop: an input (meeting outputs / brain dump /
"remember this" / doc chunk) goes to the filer agent, which reads the keeper skill and the
map, then writes or edits directly.

| Agent | Was | Model (v2) | Notes |
|---|---|---|---|
| filer | keeper + planner + doc-filer merged | **local/cheap** | One agent, one pass, same skill-enforced discipline. |
| maintainer | maintainer | **local/cheap** | Applies the Gardener's repair worklist — broken-link retargets, frontmatter backfill, orphan links, tag-typo merges, and compaction. The mechanical passes (scan + worklist) run in code (still free); the LLM only applies the fixes. |
| assistant (chat) | assistant | frontier (unchanged) | The one place model quality is user-visible in real time; keep it strong. Delegates filing to the filer, same as today. |

**Why merging planner+keeper is safe:** the planner's entire value was "decide placement +
draft the exact diff before committing." A single agent following the same skill (§5) with a
read/search before every write gets the same discipline — the reference's capture skill proves
a single-pass agent can enforce "search before create," dedup, and no-orphans without a second
agent auditing the first one's plan. Drop the review hop; keep the skill's checklist as the
thing that keeps quality up.

**Why local/cheap models work here:** the tool layer already rejects malformed writes,
enforces structure, refuses silent shrinkage, and requires unique-match edits. A weaker model
that makes a mistake gets a specific, actionable tool error and retries — the same safety net
that lets the reference run its gardener on a small local model and still produce clean diffs.
Reserve the frontier model for the interactive assistant, where response quality is directly
user-facing, and for one-off manual garden runs on a stronger model if a cheap-model pass
produces a report worth a closer look (configurable per agent, same as the reference's
"give the gardener a stronger model by editing its frontmatter" pattern — no code change).

---

## 5. The skill — one operating procedure, not three prompts

Keep the existing keeper skill essentially as-is — it already encodes exactly what the
reference splits across three skill files (structure, search-before-create, no-orphans,
conflict handling, document paging). No change needed here beyond deleting the cite step
(§2.3) and the typed-relation section (§2.3) to match the trimmed frontmatter. This single
skill continues to serve both the interactive assistant (delegating to the filer) and the
standalone filer agent — exactly the "skills carry the mechanics, any agent can load them"
pattern the reference uses for capture/explore/garden.

---

## 6. Retrieval — unchanged, already cheap

Tier 1 (progressive disclosure via the root hub) and Tier 2 (a derived map + backlinks,
regenerated on every write) already match the reference's approach almost exactly — the only
difference is curry-leaves persists the map/backlinks as derived files instead of recomputing
them per-request. That's the right call once the bundle exceeds "recompute in-memory per
request" comfort, and it costs nothing extra since it's part of the existing single-mutator
write path. **No change.** Tier 3 (embeddings) stays deferred in both designs — add only if
keyword recall proves insufficient, same trigger condition as today.

---

## 7. Gardener — code finds the work, the LLM half repairs it

The gardener's mechanical passes (edge integrity + retarget suggestions, hub/map rebuild, the
repair worklist — broken links, missing frontmatter, orphans, singleton tags — the compaction
worklist, and the stale-conflict/old-inbox sweeps) are all pure code, zero LLM cost. They no
longer just *report* the fixable Health issues: they collect them into per-issue worklists in
the gardener report, each with the maintainer's marching orders (retarget-to vs remove,
established tags to reuse, etc.), so the Health tab and the worklist never disagree about
what's broken. The LLM half (the maintainer, cheap tier per §4) then *applies* those repairs —
retargeting/removing dead links, backfilling frontmatter from note content, adding one inbound
link to each orphan, merging obvious tag typos, and compacting oversized notes — each with a
conservative "skip if unsure" guardrail so an unattended nightly run never guesses. Consider
adding the reference's **incremental gardening** optimization: hash each note's
gardening-relevant fields and skip compaction review for notes unchanged since the last run —
turns "review every oversized note nightly" into "review only what changed," which matters
once the bundle is large enough that nightly full-bundle compaction passes get expensive even
on a cheap model.

---

## 8. Scale & robustness mechanisms — unchanged

Single-writer queue, stable ids + link-by-id, sub-bundle sharding,
soft-delete only, conformance gate in tools, disposable derived state. None
of this was the source of cost or complexity; keep all of it as-is.

---

## 9. Build order (phased)

1. **Simplify** — trim frontmatter (§2.3), collapse areas (§2.1), fold provenance into the
   source field (§2.3), trim the tool surface (§3), write the conventions file as the single
   taxonomy source of truth.
2. **Merge agents** — replace planner + keeper + doc-filer with one filer (§4); update the
   triggers accordingly; delete the planner's now-unused tool/prompt.
3. **Cost tier** — set the filer and maintainer to a cheap/local model; leave the assistant on
   the frontier model. Verify tool-error recovery still works at the cheaper tier
   (retry-on-guard-rejection is what makes this safe — confirm it, don't assume it).
4. **Gardener trim** — drop the inverse-edge pass; optionally add content-hash incremental
   compaction (§7) once bundle size makes it worth the extra bookkeeping.
5. **Re-measure** — compare token spend per meeting-filed and per nightly garden run
   before/after; that number is the actual success metric for this revision.
