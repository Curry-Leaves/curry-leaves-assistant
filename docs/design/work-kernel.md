# The Work Kernel

One execution substrate for every agent run in the app. Triggers, schedules, dashboard
tiles, chat, "run from chat", and workflow steps all become a **work item** submitted through
one front door. What differs between them is *data on the work item* (mode, lane, band,
autonomy), not separate dispatch code.

Everything is submitted through that single front door. Interactive and ephemeral work runs
live (chat's own streaming path); background work is written as a durable job file and picked
up by the scheduler, which hands it to workers under lane, priority-band, and global-cap
discipline, with dead-lettering, a loop guard, crash recovery, and a per-autonomy host
(headless auto-approve, or suspend-and-ask). From there every path funnels into the same LLM
loop, which is unchanged by the kernel.

A live snapshot of the kernel is broadcast over the shared WebSocket and drives the **Work**
panel in Assistants → List (running / queued / dead-lettered).

## WorkItem

A work item carries the data that distinguishes one piece of work from another:

- **kind** — an agent run or a tile run.
- **who runs + why** — the agent and the event/payload that composes its input.
- **mode** — background (durable, recovered), interactive (chat — live), or ephemeral
  (throwaway).
- **lane** — which scheduler channel it runs in (below).
- **band** — its priority: interactive first, then event-triggered, then
  background/scheduled.
- **autonomy** — auto (approve granted tools headlessly) or ask (suspend + notify a human).
- **dedupe key** — a stable id that makes submit idempotent (same key = at most one job).
- **correlation id** — rides into the run's events, for joining and workflows.

## Lanes (the queue discipline)

A **lane** is a channel with a **width** (max concurrent jobs in it):

- KB writers → width **1** (strictly sequential — they never overlap)
- tiles → width **2** (capped parallel — no refresh stampede)
- maintenance → width **1**
- anything else → a general lane, bounded only by the global concurrency cap

An agent is assigned a lane in its frontmatter (KB filer/maintainer and the memory keeper use
the KB lane). Across lanes, jobs run by **priority band**, FIFO within a band. A worker takes
the lowest-band, oldest job whose lane has spare width and whose global cap has room.

## Governance (uniform for every job)

- **Dead-letter** — a job that crashes the worker too many times is quarantined instead of
  crash-looping.
- **Loop guard** — submit refuses a job whose causal chain (same trace) is too deep or that
  re-enters the same agent.
- **Recovery** — on boot, unstarted jobs re-queue; interrupted jobs retry with the attempt
  counter bumped.
- **Tracing** — every run gets a trace; the Assistants tab renders it as a conversation.

## Autonomy & human-in-the-loop

- **auto** (default) — a permission engine auto-approves would-be-prompted tools; fully
  headless.
- **ask** — a suspending host: on an ask/approve, the run publishes the request to its own
  channel, signals that it needs input, **releases its pool slot**, and parks (until answered
  or a timeout denies it). Answer it in the **Assistants tab** — the run renders as a
  conversation with a live approve/answer card — and it **reacquires a slot and continues**.

Releasing the slot while waiting is the key move: a suspended run never starves the pool.

## Workflows are skills

There is no workflow engine. A workflow is a **prose skill** — a sequence an agent follows,
using **one action-dispatched orchestration tool** with four actions:

- **assign** — fire-and-forget: hand a task to a background agent, get a job id back
  immediately. The chat stays responsive; the work runs durably and shows in the activity
  feed.
- **spawn** — start a background run and return a job id **without** waiting — the building
  block of a workflow. Spawn several, then await to fan out in parallel.
- **status** — poll one job's state (queued/running/done/failed/dead) and, once terminal, its
  output. Non-blocking.
- **await** — suspend (slot released) until those runs finish, then return each one's status +
  output.

The suspend/resume that powers human approvals is the *same primitive* that powers await
(waiting on child completions instead of a person). The meeting-digest seed skill is a sample
workflow.

The orchestration tool is granted to the **assistant** seed agent — there is no separate
orchestrator agent; a workflow is an agent following a prose skill.

**Writing a new workflow = writing a new skill. No code.**

One hard rule the tool enforces in its own description: a spawned or assigned agent starts with
**no memory of the calling conversation**, so the brief must be completely self-contained —
never "it" or "that".

## The pool front door

Separately from the orchestration tool, a user can post a task to the **common pool** (⌘K → New
task, or the pool column in Assistants → List). That wakes the seeded **Lead** agent; it reads
the pool, sizes up the team's current load, and routes the item to one best-fit agent via its
own assign action. Both that path and a human clicking assign go through the single pool-assign
path, so the Lead assigns work identically to a person. The Lead routes only — in its current
form it cannot hire or split a task.

## Tunables (env)

Environment variables tune the global concurrency cap, the tiles lane width, the max attempts
before dead-lettering, the max causal depth for the loop guard, the approval-wait timeout, how
many runs are retained, and the nightly garden hour.

## Known v1 limitations

- **Suspend is not durable across restart.** A run parked on an approval/await holds its
  coroutine; a process restart re-runs the job (bounded by attempts/dead-letter). Serializing
  and rehydrating run state (true durable suspend) is future work.
- An orchestrator killed mid-workflow re-runs from the start on recovery; children are deduped
  only via caller-supplied dedupe keys.
