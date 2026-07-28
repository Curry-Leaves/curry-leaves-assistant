# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

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

[Unreleased]: https://github.com/Curry-Leaves/curry-leaves-assistant/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/Curry-Leaves/curry-leaves-assistant/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Curry-Leaves/curry-leaves-assistant/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Curry-Leaves/curry-leaves-assistant/releases/tag/v1.0.0
