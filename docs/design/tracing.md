# Curry Leaves Tracing — End-to-End

> How Curry Leaves reconstructs *what actually happened* when it does something: a causal
> tree of **events → agent runs → LLM turns → tool calls** with timing, drillable
> down to each run's full conversation. Powers the **Trace** page.

## 1. Why

Before tracing, the runtime was opaque. Events were logged but carried **no causal link** —
an agent creating a todo didn't record which run caused it. Pool agent runs kept only their
final text; the **internal LLM conversation** (system prompt, messages, thinking, tool I/O,
tokens) was discarded. And real work nests several levels deep — a recording is transcribed,
which triggers a summarizer run, which saves a summary, which triggers a knowledge-filer run,
which writes notes, which announces that knowledge was ingested.

Tracing reconstructs that whole tree, for autonomous runs **and** Ask AI chat.

## 2. The model — OpenTelemetry-lite spans

A **trace** is a tree of **spans**, one uniform shape written append-only. Every span carries
its trace id, its own id, and its parent's id, plus a kind, a human name, a status
(ok/error/running), start and end timestamps and a duration, a bag of kind-specific
attributes, and an optional error.

The span kinds form the causal vocabulary: a root **action** (a user action that starts a
trace, like finalizing a recording); a **session** root that groups all of a chat
conversation's turns; an **event** (an app event recorded inside a trace, carrying what caused
it); an **agent run** (top-level) and a **subagent run** (one nested inside another); an **LLM
turn** (one model call); a **tool call**; and **approval**/**ask** (a mid-run
human-in-the-loop request).

Large message and tool bodies are truncated so a trace file stays bounded.

## 3. What a trace looks like

Stop a recording → the whole chain is **one trace**, rendered as a tree / waterfall. The root
action (recording finalized) contains the transcription event, under which the Meeting
Summarizer run nests its LLM turns and tool calls; the save-summary tool emits an event that
triggers a nested knowledge-filer run, whose own tool call fans out into subagent runs and
ends by announcing knowledge ingested; a todo-creating tool call likewise nests the resulting
todo event; and the run closes with a final summarizing turn. Each level is a child of
"whoever was running when it started."

Click any span → inspector: an agent run shows its model, tokens, system prompt, input,
output; an event shows its payload and a "caused by →" jump; a tool call shows args/result;
an LLM turn shows thinking + text.

## 4. How parent ↔ child is tied (the core trick)

Every span carries its own id and its parent's id. Parent = "whoever was running when the
child started." Two mechanisms set it.

### 4a. Live nesting — same async context

The current span is held in async-context state; opening a child reads it for the parent and
trace ids. That context **propagates** across awaits, task creation (copied at creation), and
thread offloading (copied) — so everything running *inside* a span nests automatically, with
zero id threading. This covers an agent run's LLM turns and tool calls, and a tool's emitted
events.

### 4b. Deferred nesting — ids travel on the event/job (across time & tasks)

The agent pool is asynchronous: an event emitted now triggers a run *later*, in a **different
task**, where the live context is already gone. So the link is carried **on the data**:

1. When an event is emitted inside a trace, it is stamped with the trace id and the "caused
   by" link, recorded as an event span, and given its own span id *before* trigger handlers
   run.
2. The pool enqueues a job that stores that whole trigger event.
3. Later, the Work Kernel worker runs the job re-entered into the trace with the **event span
   as parent**. The agent run then opens under it.

If the trigger carries no ids (e.g. a schedule, or a re-emit), re-entry is a no-op and the run
opens its **own root trace**.

### Chat sessions — one trace per conversation

Each chat turn is its own agent run, but they're grouped: the chat run path uses a
deterministic per-session trace id and a session root span (written once), then runs each turn
re-entered into that session trace. So **every turn of a conversation — and all its agent runs,
tools, web calls, and approvals — nests under the one session trace**, instead of scattering
one root trace per message.

### 4c. Roots & one deliberate hand-off

A user action with no current span opens a **new root**. The recording-finalize handler wraps
its work in an action span and passes the trace and span ids into the background transcription
task, so the transcription event — and everything it triggers — stays in the **same** trace
instead of starting a second root.

## 5. How curry-leaves is captured — its Host seam

Curry Leaves doesn't scrape output or fork the engine. The curry-leaves kernel already has the
exact observability channel: a **Host** that fires for **every** engine event, in both the
non-streaming (pool/pipeline) and streaming (chat) run paths, and a runner already accepts a
host.

So a **tracing host** is attached to every runner and records leaf spans under the run's agent
run span:

- message start / update / thinking events → accumulate text and thinking for the turn;
- message end → write an LLM turn span (model, text, thinking, tokens);
- tool start / end → write a tool call span (name, args, result, error flag);
- an approval or ask request → write an approval / ask span, then delegate.

The tracing host **composes**: chat wraps the real chat host, so approvals/asks still drive
the UI *and* get recorded; pool/pipeline wrap the default no-op host. Because the non-streaming
path also emits to the host, the simple run path keeps its plain return contract — no switch to
streaming needed.

The run span itself is opened where a run begins. Its kind is a **subagent run** when already
inside an agent context, else an **agent run** — that's how an agent's delegated sub-agents show
as nested sub-runs.

## 6. Storage, retention, lifecycle

- **One file per trace.** Each span is appended twice — an **open** record (status running) and
  a **close** record (final attrs + end time); readers merge by span id (last wins), so an
  in-flight trace is still listable.
- **Writes are serialized** by a lock (parallel branches of one trace can append concurrently)
  and are **best-effort** — tracing never raises into the thing it traces.
- **Retention:** the oldest trace files are pruned to a cap (env-tunable), run amortized.
- **Listing** scans the dir, derives each root span, and summarizes (status, duration, span
  count). No separate index to keep consistent.
- Trace writes are **deliberately not** routed through the event bus — no feedback loops.

## 7. API

The trace API lists recent trace summaries (newest first), reads one trace as a flat list of
spans (the client builds the tree from each span's parent id), deletes one trace, and clears
all.

## 8. Frontend — the Trace page

- **Left:** recent traces (root name · time · duration · status dot · span count).
- **Center:** the selected trace as an indented **tree** with a per-row **waterfall bar**
  (offset+width from each span's start/duration vs. the trace window).
- **Right:** an **inspector** for the clicked span — agent runs show model / tokens / system
  prompt / input / output; an LLM turn shows thinking + text; a tool call shows input/output;
  an event shows payload + a "caused by → jump" link.
- **Live:** while the open trace has any unfinished span it polls for updates on a short
  interval, refreshing the list on the same tick.

## 9. Coverage & limits

- **Covered:** pool (event-triggered), scheduled, common-pool, **chat** (grouped per session),
  and nested pipeline sub-agents — anything through the agent engine + event bus. All tools are
  captured (including network tools — web search, web fetch, browser); a curry-leaves
  sub-agent's own tools/turns are unwrapped into spans too. Tools interrupted before they
  finish are flushed as error spans at run end.
- **System prompt** currently stores the agent's *instructions*, not the fully composed prompt
  (tool list / memories / env). Capturing the exact resolved prompt needs a small curry-leaves
  hook — a clean follow-up.
- **Tool → event linkage:** curry-leaves runs approved tools concurrently in copied contexts,
  so a tool's emitted events nest under the **agent run** (via the "caused by" link), not the
  specific tool call span. The tool span is a sibling with its args/result.
- **Single process** only. No cross-process / remote tracing, metrics, or alerting.
- **Phase 2 (not yet):** pushing trace updates over the shared WebSocket instead of polling,
  filters/search, and export-as-JSON.
