# Contributing to curry-leaves-assistant

Thanks for your interest in improving **Curry Leaves** — bug reports, features, docs, and tests are
all welcome. This guide covers how to get set up, the conventions that keep the codebase coherent,
and how to extend it.

By participating, you agree to keep interactions respectful and constructive — see the
[Code of Conduct](CODE_OF_CONDUCT.md). Found a security issue? Please report it privately per the
[Security Policy](SECURITY.md) rather than opening an issue.

## Table of contents

- [Ways to contribute](#ways-to-contribute)
- [Development setup](#development-setup)
- [Project layout](#project-layout)
- [Pull request workflow](#pull-request-workflow)
- [Code conventions](#code-conventions)
- [Extending the app](#extending-the-app)
- [Commit messages](#commit-messages)
- [Reporting bugs](#reporting-bugs)
- [License](#license)

## Ways to contribute

- **Report a bug** — open an issue with a minimal reproduction (see [Reporting bugs](#reporting-bugs)).
- **Propose a feature** — open an issue describing the use case *before* writing code, so we can
  agree on the approach.
- **Improve docs** — the README, the [user guide](docs/guide/README.md), the
  [architecture doc](docs/architecture.md), and code comments all count.
- **Add tests** — there's no suite yet; a first `tests/` covering the Work Kernel, stores, or the
  provider registry is a high-value contribution (`pytest` + `pytest-asyncio` are already dev deps).

## Development setup

Requires **Python 3.11 or 3.12** (the `kokoro` TTS dependency has no 3.13+ wheels) and **Node 20+**.

```bash
git clone https://github.com/Curry-Leaves/curry-leaves-assistant.git
cd curry-leaves-assistant
./start.sh              # sets up the venv + node deps, then runs backend + web UI
```

`start.sh` is idempotent — it creates a Python 3.11/3.12 venv, installs this backend editable
(pulling the `curry-leaves` kernel and `cl_memory` engine from PyPI), fetches the prebuilt web UI,
and best-effort installs `espeak-ng` (optional, for spoken replies). The backend serves the UI
itself — open http://127.0.0.1:5177.

Prefer to wire it yourself:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"     # backend + dev tools; kernel/engine come from PyPI
./scripts/build_webui.sh              # fetch the prebuilt web UI into the package
.venv/bin/curry-leaves-assistant      # backend serves the UI at http://127.0.0.1:5177
```

The **web UI is a separate repo** ([`curry-leaves-assistant-web`](../curry-leaves-assistant-web)) —
a standalone React app published to npm as a static bundle that this backend fetches and serves.
To change the UI, work there; to test an unpublished UI build against this backend, point
`CURRY_LEAVES_WEB_DIR` at your web checkout (with a built `dist/`) when running
`./scripts/build_webui.sh`, or run its `npm run dev` with `VITE_BACKEND_URL` set for hot reload.

The **`curry-leaves` kernel and `cl_memory` engine** are ordinary PyPI dependencies of this repo —
installed pinned from PyPI like any other dep. You don't need local checkouts of them to build or
run this backend.

Common commands:

| Command | What it does |
|---|---|
| `.venv/bin/mypy src` | Type check — **a correctness gate** |
| `./scripts/lint_layers.sh` | Enforce layer boundaries (`import-linter`) — **the architecture gate** |
| `./scripts/build_webui.sh` | Fetch the prebuilt web UI into `src/curry_leaves_assistant/webui/` |
| `./start.sh` | Set up the venv + UI and run the backend (serves the UI) |

> **Testing status:** `mypy src` and `./scripts/lint_layers.sh` are the gates every change must
> pass. There is no automated test suite yet — contributions that add one are especially welcome.
> Until then, drive the affected flow in the running app and confirm it behaves (see the
> [`verify`](.claude/skills/verify/SKILL.md) notes for the web-mode + Playwright recipe).

## Project layout

```
src/curry_leaves_assistant/       FastAPI backend, installable via pip
  app.py      composition root (wires every layer together)
  api/        HTTP + WebSocket routers
  orchestration/  the Work Kernel — lanes, bands, schedules, triggers
  agents/     agent engine, tools, chat/trace hosts
  providers/  extra LLM providers + MCP on top of the kernel
  domain/     recordings, transcription, knowledge, memory, tts, wakeword
  stores/     plain-file + SQLite-index persistence
  core/       auth, events, paths, settings, ws hub — the base layer
  seeds/      default agents & skills shipped with the app
docs/         architecture, per-page docs, design docs, user guide
scripts/      setup, build, lint, publish helpers
```

The backend is a **strict layering** — higher layers may import lower ones, never the reverse:
`api → orchestration → agents → providers → domain → stores → core`. `app.py` is the composition
root and sits outside the ordering. The boundary is enforced by `import-linter` (the
`[tool.importlinter]` contract in `pyproject.toml`); run `./scripts/lint_layers.sh` before pushing.
See [docs/architecture.md](docs/architecture.md) for the big picture.

> The **web UI** lives in the sibling [`curry-leaves-assistant-web`](../curry-leaves-assistant-web)
> repo (React + Vite, published as a static bundle this backend serves). The **Electron desktop
> shell** lives in [`curry-leaves-assistant-desktop`](../curry-leaves-assistant-desktop) — it builds
> the same web UI and spawns this same backend, so nothing is duplicated. Contribute backend changes
> here; UI changes in the web repo; window/tray/packaging changes in the desktop repo.

## Pull request workflow

1. **Open an issue first** for anything non-trivial, so we can agree on scope and approach.
2. **Fork and branch** off `main`:
   ```bash
   git checkout -b feat/short-description   # or fix/… , docs/… , test/…
   ```
3. **Make the change.** Keep the diff focused on one concern.
4. **Verify it passes:**
   ```bash
   .venv/bin/mypy src && ./scripts/lint_layers.sh
   ```
   For a change with a runtime surface, also drive the affected flow in the app and confirm it
   behaves as intended — don't rely on the static gates alone.
5. **Update docs** (README, `docs/`, code comments) when behavior or a public surface changes, and
   add an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md).
6. **Open a pull request** with a clear description of *what* changed and *why*. Link the issue.

Keep PRs small and reviewable. A large PR is easier to land when split into focused commits or
separate PRs.

## Code conventions

- **Respect the layering.** Add behavior at the right layer; never import upward. If you need a new
  cross-layer edge, it goes through `app.py` (the composition root) or is a reviewed, documented
  exception in the `[tool.importlinter]` `ignore_imports` list — not a silent new dependency.
- **Keep new backend source under `src/curry_leaves_assistant/`.** UI source lives in the separate
  [`curry-leaves-assistant-web`](../curry-leaves-assistant-web) repo.
- **Match the surrounding style** — small, single-purpose modules; clear names; comments that explain
  *why*, not *what*. Prefer boring, direct solutions.
- **Type it.** `mypy src` must stay green; public functions carry full annotations. (Frontend
  TypeScript conventions live in the web repo.)
- **Data is plain files.** Everything a user owns lives under `~/.curry-leaves/` as markdown/JSON,
  with SQLite used only as a rebuildable search/memory index. Keep it human-readable and portable.
- **Complete the change** — types, docs, a CHANGELOG entry, and (where practical) a runtime check.

## Extending the app

Common extension points:

- **Add an API route** — a router under `api/`, mounted in `app.py`. Keep business logic in
  `domain/`/`orchestration/`; routers stay thin.
- **Add an agent tool** — implement it under `agents/`, following the kernel's `Tool` protocol
  (pydantic args model + `run`), and set `risk` correctly (it drives the permission gate).
- **Add an LLM provider** — one `ProviderSpec` entry in `providers/registry.py` for a mainstream
  provider; arbitrary OpenAI-compatible endpoints need no code change (users paste a base URL).
- **Add a frontend screen** — in the [`curry-leaves-assistant-web`](../curry-leaves-assistant-web)
  repo: a folder under `src/frontend/screens/<domain>/`, an `api/` client module for its backend
  router, and a tab in `App.tsx`.
- **Ship a default agent or skill** — drop a markdown file under `src/curry_leaves_assistant/seeds/`.

## Commit messages

Use short, imperative summaries. Conventional-commit prefixes are appreciated but not required:

```
feat: add per-assistant usage chips to the Usage screen
fix: don't drop the WebSocket token on reconnect
docs: clarify the run-from-source kernel opt-in
refactor: split the dashboard runner out of agent_engine
```

## Reporting bugs

Open an issue that includes:

- What you did (the exact steps, CLI command, or a minimal reproduction).
- What you expected vs. what happened (include the full error / stack, and relevant `backend.log`).
- Your environment: `python --version`, the `curry-leaves-assistant` version, provider + model id, OS.

A minimal reproduction is the fastest path to a fix.

## License

By contributing, you agree that your contributions are licensed under the project's
[MIT License with the Commons Clause](LICENSE).
