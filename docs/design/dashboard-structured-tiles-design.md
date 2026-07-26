# Dashboard tiles via structured output (`output_type`)

Status: **shipped.** This document is kept as the design rationale; the implementation
threads a per-tile output contract end-to-end and validates a tile's reply against it. Where
the design below differs from the shipped code, the code is authoritative.

## Problem

Tile rendering today is prompt-and-scrape. The dashboard asks the agent in prose to "shape
your reply as a markdown table / single headline number / …", then regex-parses whatever
free text comes back:

- **metric** grabs the *first* number in the text — "Checked 3 inboxes, 14 unread"
  becomes metric `3`, label "Checked inboxes, 14 unread".
- **table** only works if the model happened to emit pipe-style rows; otherwise the
  tile silently falls back to markdown.
- **list** treats any multi-line prose as list items.
- Emptiness is guessed by matching the whole reply against a phrase list ("no data",
  "all clear", …) — a two-clause summary that means "nothing" is missed.

The shape drifts run to run because there is no contract, only a hint.

## What the kernel already gives us

The `curry-leaves` kernel supports validated structured output. Bound to an output type, an
agent gets the type's schema injected into its system prompt every turn; the runner then
parses the final text (tolerating fenced JSON and surrounding prose), validates it against
the type, and retries a bounded number of times with a corrective prompt on failure. The
result carries both the validated instance and the raw text.

Caveat: native constrained-decoding JSON mode only engages for tool-less agents on
OpenAI-style providers. Dashboard agents *have* tools, so for tiles this is schema-guided
prompting + validation + retry — still a real contract, just not constrained decoding.

## Design

### 1. Per-format output models

One output model per structured tile format, each with an explicit "empty" flag so the
agent *declares* "nothing to report" instead of us guessing from phrasing:

- **summary** — a short markdown summary.
- **list** — one short line per item.
- **metric** — a headline value, an optional caption, and an optional trend/delta.
- **table** — a header row and body rows.
- **diff** — added / removed / context lines.

Markdown is intentionally absent from this set — it keeps the free-text path (templates,
rich docs).

**Markdown stays free-text.** A markdown tile (with or without a template) keeps the current
path untouched — no output type, no JSON. The user keeps that choice; it's the right tool
for long-form tiles.

Each model also maps to the existing tile wire shape, so the board JSON on disk and the
frontend contract don't change.

### 2. Thread `output_type` through agent_engine

The output type is plumbed from the tile down into the agent and its runner, and there is a
new structured-run entry point returning both the validated instance and the raw text. It
shares the traced body of the ordinary run path, so the existing (unstructured) callers —
pool, knowledge, generate-config — keep their signature and behavior. The output type is not
propagated to subagents; only the top-level answer must be structured.

### 3. The tile runner

Per tile, the runner looks up the output type for the tile's format. For a structured format
it does a structured run and shapes the validated result onto the wire shape; for markdown
(no output type) it does an ordinary run and shapes the raw text as before.

The old free-text shaping survives as the **fallback**, not the primary path — if validation
fails even after the kernel's retries (small local models, provider hiccups), the tile still
paints from raw text instead of going blank. The empty-phrase guessing only applies on that
fallback.

### 4. Per-format prompt trims

For structured formats, drop the two prose instructions the schema now covers:

- the "shape your reply as a {hint}" line — the injected schema is the shape;
- the 'reply with just "None"' empty-handling line — replaced by: *if there is nothing to
  report, set empty and leave the content fields minimal.*

Focus, rules, and the alert/notify directive are unchanged (the alert fires via a tool call
mid-run, orthogonal to the final structured answer). The markdown path keeps its current
wording, including the template skeleton.

### 5. Frontend

No required changes: the wire shape is identical, the tile renders as before.

One optional nicety unlocked by the metric delta: render a small trend line under the metric
label when present, plus widening the metric wire variant to carry it.

### 6. General-purpose tile agent (seed)

Tiles must bind to an agent, and today that means authoring one first. Seed a read-only
generalist so the Add Tile menu always has a sensible default: a dashboard-watcher agent that
can check the web, knowledge base, recordings, todos and reminders, with allow-all
permissions on those read-only tools and a modest step cap.

Read-only tool set + allow-all permissions so headless tile runs never stall on an approval
prompt. The step cap keeps scheduled runs cheap. Each run gets one brief (a focus and
optional constraints); it gathers just enough to answer and replies in exactly the requested
shape — terse, never an essay, never a write action.

## Follow-up (out of scope here)

- The dashboard's generate-config endpoint hand-rolls JSON extraction with a regex; it's a
  tool-less pure-extraction call, exactly the case where the kernel grants native JSON mode —
  migrate it to a structured output type and delete the regex.
- Metric history → sparklines (needs per-tile output history, a storage change; today only
  the last output is kept).

## Cost / risk notes

- The schema in the system prompt adds a few hundred tokens per tile run; retries only fire
  on parse failure. Net cost ≈ unchanged for well-behaved models.
- Weak local models may fail validation often — they land on the free-text fallback, i.e.
  exactly today's behavior, so nothing regresses.
- No storage migration: the last-output shape is unchanged; old cached outputs render as
  before.
