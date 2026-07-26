# Roadmap

Where **Curry Leaves** is headed. This is a direction, not a contract — priorities shift, and
community input shapes what lands next. For what's already shipped, see the
[CHANGELOG](CHANGELOG.md); for how to help, see [CONTRIBUTING](CONTRIBUTING.md).

Have an idea or a strong opinion on ordering? [Open an issue](https://github.com/Curry-Leaves/curry-leaves-assistant/issues)
or a [discussion](https://github.com/Curry-Leaves/curry-leaves-assistant/discussions) — the roadmap
is meant to be nudged.

## Now

Hardening the 1.x foundation and closing obvious gaps.

- **A first automated test suite.** There's no `tests/` yet; a starting suite over the Work Kernel,
  stores, and the provider registry is the single highest-value contribution right now
  (`pytest` + `pytest-asyncio` are already dev deps).
- **Docker & install polish.** Smoother first run across platforms, clearer errors when a binary
  (`ffmpeg`, `espeak-ng`) is missing, and leaner images.
- **Provider coverage & resilience.** Better handling of rate limits, timeouts, and partial failures
  across Copilot / Anthropic / OpenAI / Ollama / OpenAI-compatible endpoints.
- **Docs.** Deeper design docs for the Work Kernel and knowledge base, and more of the user guide.

## Next

Making the assistant team more capable and easier to shape.

- **Better proactive work.** Smarter Todo Triage routing, richer schedules and triggers, and clearer
  review/hand-back flows on the dashboard.
- **Knowledge base depth.** Stronger linking and provenance, faster tiered search, and editing notes
  in place without breaking the graph.
- **Memory & learning.** More reliable fact/event distillation, lesson consolidation, and skill
  reflection — with everything auditable as plain files.
- **MCP ecosystem.** Easier discovery, install, and permissioning of MCP servers, with sensible
  risk defaults on the tools they expose.
- **Voice.** More wake-word options, more TTS voices/languages, and lower-latency hands-free chat.

## Later

Bigger bets, still exploratory.

- **Mobile / companion capture.** A lightweight way to record on the go and sync back to your
  machine, staying local-first.
- **Multi-device sync** of `~/.curry-leaves/` that preserves the "plain files, your machine" promise.
- **Richer artifacts.** More deliverable types and finer control over revocable public links.
- **Pluggable transcription & embedding backends** beyond the bundled defaults.

## Non-goals

To keep the project coherent, some things are deliberately *not* on the roadmap:

- **A hosted / SaaS version.** Curry Leaves is local-first by design, and the
  [license](LICENSE) (MIT + Commons Clause) does not permit selling it or hosting it for a fee.
- **Mandatory cloud accounts.** You bring your own AI provider; the app never requires a Curry
  Leaves account.
- **Sending your data anywhere by default.** Nothing leaves your machine except calls to the AI
  provider you explicitly configure.
