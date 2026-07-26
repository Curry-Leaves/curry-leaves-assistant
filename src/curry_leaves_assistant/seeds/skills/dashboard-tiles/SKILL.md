---
name: dashboard-tiles
description: Pin a recurring ask onto the user's dashboard as a scheduled tile. Use whenever the user asks to see some data regularly ("every morning show me…", "keep an eye on…", "track X weekly") or to be alerted on a condition over time.
---

# dashboard-tiles — turning a recurring ask into a tile

The dashboard is where recurring answers live. When a chat ask is really "show me
this on a cadence", don't just answer once — pin it as a tile with
`dashboard(action='add_tile')`, then point the user at the Dashboard tab to review it.

## When to create a tile (and when not to)

CREATE when the ask has a standing timeframe or trigger:
- a cadence: "every morning at 8…", "each Monday…", "daily", "once a week"
- ongoing monitoring: "keep an eye on…", "track…", "watch for…"
- a standing alert: "let me know whenever/if…"

DO NOT create for a one-off question ("what are my todos today?") — just answer it.
If the phrasing is ambiguous ("can you check my todos in the morning?"), answer the
question now AND offer the tile; only create it if the user says yes.

## Procedure

1. `dashboard_read(action='list_boards')` first. Pick the board that fits the topic; reuse an
   existing tile's board if one already covers a similar area. If a tile with the same focus
   already exists, DON'T create a second one — `dashboard(action='update_tile')` it instead (see
   "Changing an existing tile" below).
2. Compose the tile:
   - **title**: 2-5 words, what the user would scan for.
   - **focus**: 1-3 sentences instructing the tile's agent what to gather and report
     each run. Written to the agent, not to the user.
   - **rules**: only real constraints the user stated (limits, filters, tone).
   - **output_format**: what best fits the ask — `metric` for "how many", `list` for
     items, `table` for rows with columns, `diff` for "what changed", `summary` for
     prose, `markdown` only for multi-section documents. With `markdown`, ALWAYS pass
     `markdown_template` — the exact `## ` headings, a short placeholder under each.
   - **empty_message**: short, topical ("No overdue todos — all clear").
   - **alert**: only if the user asked to be notified; the condition in plain words.
3. Schedule — mirror what the user said, nothing more:
   - "every morning at 8" → `refresh_mode: schedule, frequency: daily, time: "08:00"`
   - "every weekday at 7:45" → `frequency: weekdays, time: "07:45"` — ONE tile,
     never one tile per day
   - "each Monday at 9:30" → `frequency: weekly, time: "09:30", day_of_week: 1`
     (day_of_week is 0=Sunday .. 6=Saturday; time is 24h HH:MM)
   - "whenever a recording is transcribed" → `refresh_mode: event,
     event_type: recording.transcribed`
   - no timing stated → leave `refresh_mode: manual` and mention they can add a
     schedule from the tile's Configure menu.
4. **board**: omit for the default board unless the user named one ("on my work
   board") or the topic clearly belongs with an existing named board. A new name
   creates that board.
5. **agent_id**: leave the default `dashboard-watcher` (read-only generalist) unless
   the user names one of their own agents that plainly fits better.

## Changing an existing tile

When the user asks to CHANGE a tile they already have — "make the stock tile a grid",
"switch it to a table", "have it refresh daily instead", "rename it", "tighten the
brief" — MODIFY the existing tile. NEVER call `dashboard(action='add_tile')` for a change; that
leaves the old tile behind and adds a duplicate.

1. `dashboard_read(action='list_boards')` to get the `boardId` and `tileId` of the tile they mean
   (match by title/focus; if two could match, `ask` which one).
2. `dashboard(action='update_tile', board_id=..., tile_id=..., …)` passing ONLY the fields that change —
   everything else is preserved. Map the ask to a field:
   - "grid" / "table" / "as rows/columns" → `output_format: table`
   - "as a list" → `output_format: list`; "just the number" → `output_format: metric`;
     "a written summary" → `output_format: summary`; "a full brief with sections" →
     `output_format: markdown` (+ `markdown_template`)
   - "refresh daily at 8" → `refresh_mode: schedule, frequency: daily, time: "08:00"`
     (same schedule mapping as creation); "stop the schedule" → `refresh_mode: manual`
   - "rename to X" → `title: "X"`; "watch Y too / focus on Z" → new `focus`
3. It re-runs the tile automatically, so the user sees the new shape immediately.
   Confirm what changed in one line and point them at the Dashboard tab.

## After creating

The tool starts the tile's first refresh in the background. In your reply:
- confirm what was pinned, where, and when it runs ("Added 'Open todos' to your
  Dashboard board — refreshes daily at 08:00");
- ask the user to open the **Dashboard** tab to check the first result;
- mention it's theirs to tweak: the tile's ⋮ → Configure menu edits the brief,
  shape, schedule, and alert — or they can just ask you to change it.
