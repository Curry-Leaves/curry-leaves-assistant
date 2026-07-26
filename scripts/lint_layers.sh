#!/usr/bin/env bash
# Enforce the backend's layer boundaries (api → orchestration → agents → providers →
# domain → stores → core). Fails if any lower layer imports a higher one — see the
# [tool.importlinter] contract in pyproject.toml. Run locally before pushing; wire the
# same command into CI.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Prefer the repo venv's lint-imports; fall back to whatever's on PATH.
if [ -x ".venv/bin/lint-imports" ]; then
  exec .venv/bin/lint-imports "$@"
else
  exec lint-imports "$@"
fi
