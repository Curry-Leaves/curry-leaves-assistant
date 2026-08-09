#!/usr/bin/env bash
# Curry Leaves launcher — sets up the Python backend venv (idempotent), fetches the prebuilt
# web UI into the package's webui/ dir, then runs the backend. The backend serves the UI
# itself, so the whole thing is reachable from any browser at http://127.0.0.1:5177.
#
# Usage:
#   ./start.sh           # bundle the pinned PUBLISHED web UI (the default)
#   ./start.sh local     # build the sibling web checkout from source and bundle that
#
# `local` is for testing an unpublished UI change end to end against this backend. It rebuilds
# every run, so a UI edit only needs a restart. Without it you're running the last published UI
# — which is the right default, but means a local UI change appears to do nothing.
#
# The frontend lives in its own repo (curry-leaves-assistant-web); `local` looks for it at
# ../curry-leaves-assistant-web unless CURRY_LEAVES_WEB_DIR says otherwise. For hot reload
# instead, run `npm run dev` there with VITE_BACKEND_URL pointed at the backend this starts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKGDIR="$ROOT/src/curry_leaves_assistant"
VENV="$ROOT/.venv"
CURRY_LEAVES_PORT="${CURRY_LEAVES_PORT:-5177}"

WEBUI_MODE=""
case "${1:-}" in
  local) WEBUI_MODE="local" ;;
  "")    ;;
  -h|--help)
    # Print the usage block from the header comment above, so the two can't drift apart.
    sed -n '/^# Usage:/,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'
    exit 0 ;;
  *)
    echo "✗ unknown argument: $1 (expected 'local', or no argument)" >&2
    exit 2 ;;
esac

echo "▸ Curry Leaves (curry-leaves)"

# shellcheck source=scripts/_setup.sh
source "$ROOT/scripts/_setup.sh"

cl_setup_python
cl_setup_webui "$WEBUI_MODE"
cl_setup_system
cl_provider_hint
cl_kill_prior

# ── Launch backend (fixed port, no Electron) — it serves the bundled web UI ───
export CURRY_LEAVES_DIR="${CURRY_LEAVES_DIR:-$HOME/.curry-leaves}"
export CURRY_LEAVES_PORT
echo "▸ starting Curry Leaves on http://127.0.0.1:$CURRY_LEAVES_PORT …"
exec "$VENV/bin/curry-leaves-assistant"
