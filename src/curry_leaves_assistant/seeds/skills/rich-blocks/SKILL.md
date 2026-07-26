---
name: rich-blocks
description: REQUIRED before drawing any diagram, chart, schedule, or calendar in a reply. This app has purpose-built blocks for dates (calendar), schedules and deployments (timeline), boards (kanban), comparisons (chart, diffchart), hierarchies (mindmap), timings (waterfall), and flows (sankey) — a mermaid diagram is the WRONG choice for any of those and renders badly. Load this to get the exact format before you write the block.
---

# rich-blocks — showing structure instead of describing it

The app renders these fenced code blocks as live visuals, in chat replies and in knowledge
notes alike. They are ordinary markdown, so a block you write in a note stays editable there and
a block you write in chat renders in the bubble.

Reach for one when the **shape** of the answer is the answer. A sentence is better for a single
fact; a block is better for five dates, two columns of work, or a before/after.

## The rule that matters most

`mermaid` is the fallback, not the default — use it only for relationships nothing below covers
(a flowchart, a sequence diagram, an architecture sketch). A date is a `calendar`. A schedule is
a `timeline`. Drawing either as a mermaid diagram produces something misshapen and hard to read.

## The blocks

**calendar** — dated events on a month grid. One `YYYY-MM-DD: title` per line.

```calendar
2026-07-15: Deployment 1
2026-07-22: Retro
```

**timeline** — a schedule / Gantt. Header row, then one task per line. `status` is `done`,
`active`, `pending`, or `milestone` (for a milestone, repeat the same date for start and end).

```timeline
task, start, end, status
Build, 2026-07-01, 2026-07-14, done
Deployment 1, 2026-07-15, 2026-07-15, milestone
Hypercare, 2026-07-16, 2026-07-23, pending
```

**kanban** — work by column. `=== Column name`, then `- card` lines.

```kanban
=== To Do
- Write migration
=== In Progress
- Auth flow
=== Done
- Kickoff
```

**chart** — grouped bars. Header names the series; one row per label.

```chart
label, Series A, Series B
Q1, 100, 200
Q2, 150, 80
```

**diffchart** — before/after comparison, drawn as dumbbells. Header must be `label, Before, After`.

```diffchart
label, Before, After
Latency (ms), 45, 12
Error rate (%), 2.1, 0.3
```

**mindmap** — a hierarchy. One item per line, **two spaces of indent per level**.

```mindmap
Comcast
  CBM
    LLE
    Unified Wallet
```

**waterfall** — timings or a cost/effort breakdown. Indent to nest a span under its parent.

```waterfall
name, start, duration, type
GET /page, 0, 1250, navigation
  DNS, 0, 12, dns
  Transfer, 237, 620, transfer
```

**sankey** — flow or allocation between things. `Source -> Target: value` per line.

```sankey
Signups -> Activated: 40
Signups -> Churned: 25
Activated -> Paid: 18
```

**tabs** — alternatives side by side. `=== Tab name`, then that tab's markdown.

```tabs
=== Option A
Cheaper, slower.
=== Option B
Faster, needs a migration.
```

**api** — a REST/GraphQL endpoint. First line is `METHOD /path "description"`; then optional
`auth:` / `env:` lines, then `# Scenarios` with `## Name` and `> request` / `> response NNN`.

```api
POST /v1/users  "Create a user"
auth: Bearer

# Scenarios

## Happy path
> response 201
{ "id": "u_123", "name": "Alice" }
```

## Using them well

- **One block, no narration.** Don't restate what the block shows — the user can see it. A short
  lead-in line is fine ("Here's the CBM schedule:"), a paragraph describing every row is not.
- **Reuse what's already there.** If the note you're answering from already holds a block, reuse
  its content rather than inventing a different visual for the same facts.
- **Real values only.** Never pad a block with invented rows to make it look fuller. If you have
  two dates, the block has two dates.
- **Prose when it's simpler.** One date in reply to one question is a sentence, not a calendar.
