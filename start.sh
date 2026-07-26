#!/usr/bin/env bash
# Curry Leaves launcher — sets up the Python backend venv (idempotent), fetches the prebuilt
# web UI into the package's webui/ dir, then runs the backend. The backend serves the UI
# itself, so the whole thing is reachable from any browser at http://127.0.0.1:5177.
#
# The frontend lives in its own repo (curry-leaves-assistant-web). For live UI development,
# run `npm run dev` in that repo with VITE_BACKEND_URL pointed at the backend this script starts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKGDIR="$ROOT/src/curry_leaves_assistant"
VENV="$ROOT/.venv"
CURRY_LEAVES_PORT="${CURRY_LEAVES_PORT:-5177}"

echo "▸ Curry Leaves (curry-leaves)"

# shellcheck source=scripts/_setup.sh
source "$ROOT/scripts/_setup.sh"

cl_setup_python
cl_setup_webui
cl_setup_system
cl_provider_hint
cl_kill_prior

# ── Launch backend (fixed port, no Electron) — it serves the bundled web UI ───
export CURRY_LEAVES_DIR="${CURRY_LEAVES_DIR:-$HOME/.curry-leaves}"
export CURRY_LEAVES_PORT
echo "▸ starting Curry Leaves on http://127.0.0.1:$CURRY_LEAVES_PORT …"
exec "$VENV/bin/curry-leaves-assistant"
