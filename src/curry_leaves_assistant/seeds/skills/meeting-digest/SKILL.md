---
name: meeting-digest
description: A workflow — summarize a set of meetings in parallel, then file the combined digest into the knowledge base.
hide: true
---

# Workflow: meeting digest

A WORKFLOW you (the Orchestrator) execute by spawning other agents and combining their
results. This skill is the sequence; you are the runtime. Follow it step by step.

## Goal

Given one or more recordings/meetings, produce a combined digest and file the durable facts
into the knowledge base — doing the per-meeting work in parallel.

## Steps

1. **Identify the meetings.** From the task/user, get the list of recording ids to digest
   (use `recordings_read(action='list')` if you need to find them). If there are none, say so and stop.

2. **Summarize each meeting IN PARALLEL.** For every recording, `orchestrate(action='spawn',
   agentId='meeting-summarizer', description=<recording id + "summarize this meeting: key decisions,
   action items, open questions">)`. Spawn ALL of them before awaiting — they run concurrently.

3. **Wait for the summaries.** `orchestrate(action='await', jobIds=[...all the jobIds...])`. You'll get back each
   summary. If any failed, note it but continue with the ones that succeeded.

4. **Synthesize.** Read the summaries and write ONE combined digest: the decisions, the action
   items (with owners if stated), and the open questions across all meetings. Keep it factual —
   only what the summaries actually say.

5. **File it.** `orchestrate(action='spawn', agentId='kb-filer', description=<the combined digest +
   "file the durable facts from this meeting digest into the knowledge base">)` and
   `orchestrate(action='await', jobIds=[...])` for it. The filer runs
   in the `kb` lane, so it won't collide with other knowledge writes.

6. **Report.** Give the user a short recap: how many meetings, the top decisions/action items,
   and confirmation that it was filed.

## Notes

- Steps 2–3 are the parallel fan-out/join; step 5 is a dependent step (it needs the synthesis
  from step 4). That ordering is the whole point — independent work parallel, dependent work
  sequenced.
- Keep each spawned input self-contained: the summarizer/filer run headless and can't ask you
  to clarify.
- If filing a note would overwrite existing knowledge and you're unsure, the filer itself will
  pause for approval (it's the tools' job to guard writes) — you don't need to gate that here.
