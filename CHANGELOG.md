# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

### Added

- **Images in knowledge notes.** Paste a screenshot, drag a file in, pick one from the toolbar's
  new Image button, or link a remote URL — the file is stored in the bundle under
  `memory/assets/` and referenced as ordinary markdown (`![alt](assets/…)`), so notes stay small
  and the vault still resolves in any other markdown tool. New endpoints
  `POST /knowledge/asset` (raw body) and `GET /knowledge/asset`. Uploads are accepted only for
  PNG/JPEG/GIF/WebP, the type is taken from the file's magic bytes rather than its name or
  declared content-type, and the stored filename is rebuilt from scratch, so a hostile name
  cannot escape the assets directory. SVG is deliberately rejected — it can carry script.
- **Per-provider enable/disable.** Every AI provider now has an on/off switch in Settings → AI
  providers, separate from whether it's connected. A disabled provider keeps its credentials but
  is hidden from the model pickers, and runs that would have used it fail with a clear message
  instead of silently falling back. New settings key `ai.providers.<id>.enabled` (absent = on);
  `/providers/catalog` reports `connected` and `enabled`, and accepts `?usable=true`.
- **Live Copilot is now opt-in, with its own settings.** The in-meeting copilot ships **off** and
  lives under Settings → Capture → Live Copilot, where you can also tune how often it speaks up:
  `minNewChars`, `cooldownSeconds`, `maxCardsPerPass`, and a per-recording `maxPasses` cap. These
  are the new `live` settings block, re-read every pass so changes apply to a recording already in
  progress. New endpoint `PATCH /settings/live`.
- **A per-recording Live Copilot toggle on the Capture screen.** A switch on the copilot rail
  overrides the app-level setting for that recording only; each new recording starts from the app
  default again. Carried as an optional `enabled` field on the `live.attach` socket frame.
- **`./start.sh local`.** Builds the sibling `curry-leaves-assistant-web` checkout from source
  and bundles that instead of the pinned published UI — the one-step version of the manual
  `npm run build` + `CURRY_LEAVES_WEB_DIR=… ./scripts/build_webui.sh` dance. It rebuilds on every
  run (so a UI edit only needs a restart), resolves the checkout from `CURRY_LEAVES_WEB_DIR` else
  `../curry-leaves-assistant-web`, and fails rather than falling back to the published bundle, so
  you can't end up debugging a change that was never loaded. Bare `./start.sh` is unchanged and
  still fetches the published version.
- **`curry-leaves-assistant stop` and `status`.** The backend records itself in
  `~/.curry-leaves/service.pid`, and the CLI can now use it: `stop` sends SIGTERM so the work
  kernel drains cleanly (escalating to SIGKILL after `--timeout`, default 10s), and `status`
  reports whether a backend is running. Both exit non-zero when nothing is running. Stale pids are
  verified against the target's command line before anything is signalled.

### Changed

- **Codex no longer ships a built-in OAuth client id.** The bundled client id (OpenAI's Codex CLI
  app) has been removed from the source; Codex sign-in now requires `CURRY_LEAVES_CODEX_CLIENT_ID`
  to point at your own registered OpenAI OAuth integration. Settings → AI providers and the
  first-run wizard now say this up front and disable the sign-in button instead of failing after
  the click, and `/providers/status` reports `codex.configured`. Existing Codex connections keep
  working until their tokens need refreshing. Most users should use the **OpenAI** API-key
  provider instead — the ChatGPT-subscription backend is gated to OpenAI's own client, so a
  third-party client generally can't use a ChatGPT plan. See
  [docs/design/oauth-provider-registration.md](docs/design/oauth-provider-registration.md).
- **Copilot's client-id option is easier to find.** The Copilot card now states that it signs in
  with the built-in Curry Leaves GitHub app and points at Advanced for supplying your own client
  ID, rather than only mentioning the override once you expand that section.

- **Live Copilot no longer replays its whole conversation on every pass.** Passes shared one
  session per recording so the agent would remember the cards it had surfaced — but a shared
  session is rehydrated in full on each run (every prior brief, tool call, tool result, and
  reply), so input cost grew *quadratically* with meeting length: by pass 100 a single pass
  resent ~140k input tokens, and a long meeting could bill millions of tokens for a handful of
  short cards. Each pass now runs in its own session and the brief carries the already-surfaced
  card texts forward explicitly (capped, newest kept) — the only part of that history that was
  actually doing any work. Per-pass input is flat instead of growing: ~25x less input over a
  100-pass meeting, with the same "don't repeat yourself" guarantee. The first pass costs
  slightly more, since it now carries meeting context that session memory used to hold.

- **Tighter Live Copilot prompts.** The per-pass brief restated the role, the tool procedure, and
  the output contract that the (cached) system prompt already carries — text billed at full price
  on every tool step of every pass. The brief is now data only: which meeting, what it watches for,
  who's present, the transcript window, the already-surfaced list, and the configured card cap and
  kind filter. The `meeting-live` seed body was tightened in place. Same behavior, ~17% less input
  per pass; the prompt prefix deliberately stays above the model's minimum cacheable size, since
  trimming below it would silently disable prompt caching and cost *more*.
- **The "record durable learnings" line is only injected for agents that can act on it.** It rode
  in every agent's system prompt describing `update_profile` / `remember` — tools most agents don't
  hold (Live Copilot holds three read-only tools), which is why it carried an "only if you hold the
  tool" hedge. It's now gated on the tools actually held and names just those, matching how the
  `ask` tool's rule already worked.

- **The console script now points at `curry_leaves_assistant.cli:main`.** `stop`/`status` no longer
  import `app.py`, dropping them from ~5s to ~0.25s. The bare command still starts the server, and
  `python -m curry_leaves_assistant.app` is unchanged.

- **Web UI bumped to 1.3.0.** Re-bundled the `curry-leaves-assistant-web` frontend, which carries
  the UI for everything above: the note-editor Image button and paste/drop handling, the
  per-provider enable/disable switches, Settings → Capture → Live Copilot, the per-recording
  copilot toggle on the Capture rail, and the Codex client-id notice. `build_webui.sh` and the
  Docker `WEB_VERSION` build arg are pinned to 1.3.0.

### Fixed

- **The `maxCardsPerPass` setting was capped at parse time.** Raising it above 2 had no effect —
  the agent's output was truncated to the old hardcoded constant before the configured limit was
  applied. Both the parse cap and the instruction in the agent's brief now follow the setting.

## [1.3.0] - 2026-07-28

### Added

- **GitHub Copilot: model catalog + connection overrides.** The Copilot model picker now filters
  to chat-selectable models (`model_picker_enabled`, excluding policy-disabled and
  utility/embedding entries), matching what editors surface. The provider card also gained an
  **Advanced** section with an optional **Client ID** override and a **custom Headers** editor
  (one `Name: value` per line). By default Curry Leaves connects as its own registered OAuth app
  with its own request identity (the GA model set, the same approach opencode uses); supplying a
  client id and/or custom headers switches to GitHub's token-exchange path and can change which
  models GitHub returns — the user's choice, on their own account, under GitHub's terms. New
  settings keys `ai.providers.copilot.clientId` and `ai.providers.copilot.headers`; overridable
  via env `CURRY_LEAVES_GITHUB_CLIENT_ID`.
- **`scripts/check_copilot_models.py`.** A CLI diagnostic that probes the Copilot models endpoint
  (default vs. token-exchange path) and reports what each returns — handy for troubleshooting an
  empty or short model list.

### Changed

- **Web UI bumped to 1.2.0.** Rebuilt and re-bundled the `curry-leaves-assistant-web` frontend
  (adds the Copilot Advanced client-id + headers fields). `build_webui.sh` and the Docker
  `WEB_VERSION` build arg are pinned to 1.2.0.

## [1.2.0] - 2026-07-27

### Added

- **Custom model id for GitHub Copilot.** The Copilot provider card's model picker is now an
  editable combobox — pick from the pulled `/models` catalog or choose *Custom…* to type a model
  id the endpoint doesn't list yet (e.g. a preview/beta model). Matches the existing behavior of
  the keyed and Ollama provider cards.
- **Community & project health files.** `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1),
  `SECURITY.md` (private vulnerability reporting), `ROADMAP.md`, and GitHub issue forms under
  `.github/ISSUE_TEMPLATE/` (bug report, feature request, and a config linking to discussions,
  docs, and security reporting).

### Changed

- **Web UI bumped to 1.1.0.** Rebuilt and re-bundled the `curry-leaves-assistant-web` frontend
  into the package (includes the Copilot custom-model combobox above). `build_webui.sh` and the
  Docker `WEB_VERSION` build arg are pinned to 1.1.0.

## [1.1.0] - 2026-07-26

### Changed

- **Web UI bumped to 1.0.3.** Rebuilt and re-bundled the `curry-leaves-assistant-web`
  frontend into the package (includes the chat-page top-bar scroll fix). Docker
  `WEB_VERSION` build arg bumped to 1.0.2.

### Fixed

- **Docker transcription.** Added the `ffmpeg` binary to the image — `faster-whisper`
  decodes the audio container through it before transcription, so without it every
  transcription raised.
- **Codex (ChatGPT-subscription) login in Docker.** Published port `1455` in
  `docker-compose.yml` for the OAuth loopback callback, and the callback server now
  honors `CURRY_LEAVES_HOST` so it binds `0.0.0.0` in-container instead of refusing
  Docker's forwarded traffic on a `127.0.0.1`-only bind.

## [1.0.0] - 2026-07-26

Initial public release.

### Added

- **Voice & meeting assistant.** Record a meeting or voice note; it's transcribed
  locally (Whisper — `mlx-whisper` on Apple Silicon, `faster-whisper` elsewhere),
  summarized, and mined for action items automatically.
- **Proactive to-dos.** A built-in **Todo Triage** assistant reads every new to-do and,
  when it's something the team can act on, posts it to the pool so the Lead routes it —
  automatically. The to-do moves to *Working*, then *Review* with the finished result and
  the run's conversation attached back onto the item. Personal reminders are left alone,
  and write-backs never re-trigger triage (no loops).
- **Wake word & local voice.** A "Hey Curry" wake word (fused openWakeWord ONNX, Apache-2.0)
  and hands-free voice chat, with speech generated on-device via Kokoro. Custom wake models
  drop into `~/.curry-leaves/models/wakeword/`.
- **Event-driven agent pool.** Agents are plain markdown files (`agents/<id>.md`)
  that react to new recordings, run on a schedule, or answer on demand — each run a
  `WorkItem` through one shared Work Kernel.
- **Self-tending knowledge base.** Recordings are distilled into linked notes with
  provenance back to the exact transcript moment; the `cl_memory` engine powers
  tiered keyword→vector search across notes, facts, events, and skills.
- **Memory & learning.** A nightly pass distills durable facts and events from your
  conversations, consolidates event clusters into lessons, and a Skill Learner
  reflects on failed/inefficient runs to write better skills over time.
- **Bring your own AI.** GitHub Copilot (no key), Anthropic, OpenAI, Ollama, and any
  OpenAI-compatible endpoint, selectable in Settings.
- **Live dashboard, artifacts, trace, and usage** surfaces — standing tiles an agent
  refreshes, shareable deliverables, a per-run span tree, and a token/cost ledger.
- **One-command install.** `pip install curry-leaves-assistant` ships the backend
  *and* the bundled web UI; a first-run setup wizard picks language, transcription,
  provider, and a PIN. Docker and run-from-source paths are documented in the README.
- **User guide** published at [ilayanambi.com/curryleaves](https://ilayanambi.com/curryleaves) —
  getting started, every screen, and an FAQ.

### Notes

- **Web UI lives in its own repo** (`curry-leaves-assistant-web`). The React frontend
  is published as a static bundle that this backend fetches (`scripts/build_webui.sh`)
  and serves single-port. `pip install` and Docker ship the bundled UI, so neither needs
  Node. Set `CURRY_LEAVES_WEB_DIR` to test a local web checkout; `CURRY_LEAVES_WEB_VERSION`
  pins the fetched version.

[Unreleased]: https://github.com/Curry-Leaves/curry-leaves-assistant/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/Curry-Leaves/curry-leaves-assistant/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/Curry-Leaves/curry-leaves-assistant/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Curry-Leaves/curry-leaves-assistant/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Curry-Leaves/curry-leaves-assistant/releases/tag/v1.0.0
