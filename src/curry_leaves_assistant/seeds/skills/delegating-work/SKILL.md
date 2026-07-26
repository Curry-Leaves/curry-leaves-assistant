---
name: delegating-work
description: How to hand work to a background agent with `orchestrate`, and how to plan multi-step requests with a task list. Load before assigning/spawning background work, or before starting anything with 3+ distinct steps.
hide: true
---

# Delegating and planning

## Background work (`orchestrate`)

`orchestrate(action='assign', ...)` hands side work to a background agent when the user
doesn't need to wait for the result ("also file this", "kick off X in the background").

**The background agent starts with a BLANK slate** — it sees ONLY the `description` you
pass, never this conversation. So before you call it:

- **GATHER first.** If the task refers to something from the chat ("that meeting", "the
  decision we just made", "this note"), resolve it NOW — read the recording, pull the note,
  look up the id — and put the concrete facts INTO the description. Never pass a reference
  the background agent can't resolve on its own.
- **Write a SELF-CONTAINED brief.** State in plain terms: what to do, the specific subject
  (names/ids/dates, not "it"/"that"), any constraints, and what a good result looks like.
  Assume the reader knows nothing about this chat.
- **If you can't write that brief yet, DON'T assign.** Either gather more, or `ask` the user
  the one thing you're missing. A vague task wastes a whole run.
- **Use the full `agentId`** — e.g. `kb-filer` for your filer, not the short `filer` tool name.

After handing off, report the job id briefly. When the user later asks "is it done / what did
it find", call `orchestrate(action='status', jobId=...)` and relay the outcome.

For work whose result you need to CONTINUE this turn, use `orchestrate(action='spawn')` then
`orchestrate(action='await')` instead of assign.

## Multi-step requests (task list)

For work with 3+ distinct steps across tools (a presentation, research-then-file, several
requests bundled in one message), plan first:

- ONE `task_create` call with the whole plan as its `tasks` array — not one call per step.
- Then execute: exactly one `in_progress` at a time, `task_update` to completed the moment a
  step finishes. Every call returns the updated list; never re-check it.

**The list PERSISTS across turns in this session.** If the last turn left tasks unfinished
(see the most recent list in the conversation) and the user says to continue, resume from the
first pending task instead of re-planning. If they've moved on, `task_update` the stale items
to 'deleted' before planning the new work.

Skip the task list entirely for 1–2 step work or plain questions — if the whole plan fits in
your head and finishes this turn, just do it.
