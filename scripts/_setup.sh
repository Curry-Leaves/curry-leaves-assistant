#!/usr/bin/env bash
# Shared setup for start.sh. Sourced, not executed —
# it defines helper functions over $ROOT / $PKGDIR / $VENV, which the
# caller sets before sourcing. Keeping this in one place means the venv/deps/provider/kill
# logic can't drift between the two launchers.

# Find a Python interpreter this project can actually run on. The floor is 3.11
# (pyproject `requires-python`); the ceiling is 3.12 because `kokoro` (TTS) publishes no
# wheels for 3.13+. So the usable window is 3.11–3.12. Whatever `python3` happens to point at
# (e.g. a conda base on 3.14) is NOT assumed to fit — we probe named interpreters in
# preference order and fall back to `python3` only if it's in range. Echoes the interpreter
# path on stdout; empty output + non-zero return means "none found".
cl_find_python() {
  local py
  for py in python3.12 python3.11; do
    if command -v "$py" >/dev/null 2>&1; then command -v "$py"; return 0; fi
  done
  # Last resort: a bare `python3`, but only if it lands in [3.11, 3.13).
  if command -v python3 >/dev/null 2>&1 && python3 - <<'EOF' >/dev/null 2>&1
import sys
raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 13) else 1)
EOF
  then command -v python3; return 0; fi
  return 1
}

# Create the backend venv + install deps (kernel editable, then this package). Idempotent:
# a marker file skips reinstall until pyproject.toml changes. Delete .venv/.deps-ok to force.
# If an existing venv is on an out-of-range Python (e.g. a first run under conda 3.14), it's
# rebuilt on a compatible interpreter rather than left to fail on the kokoro install.
cl_setup_python() {
  # Rebuild the venv if it's missing OR its interpreter is outside 3.11–3.12.
  local venv_ok=0
  if [ -x "$VENV/bin/python" ]; then
    if "$VENV/bin/python" - <<'EOF' >/dev/null 2>&1
import sys
raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 13) else 1)
EOF
    then venv_ok=1
    else
      echo "▸ existing venv is on Python $("$VENV/bin/python" -V 2>&1 | awk '{print $2}') (need 3.11–3.12) — rebuilding…"
      rm -rf "$VENV"
    fi
  fi
  if [ "$venv_ok" != "1" ]; then
    local PY
    PY="$(cl_find_python)" || {
      echo "✗ No compatible Python found. Curry Leaves needs Python 3.11 or 3.12" >&2
      echo "  (the kokoro TTS dependency has no 3.13+ wheels). Install one, e.g.:" >&2
      echo "     brew install python@3.12        # macOS" >&2
      echo "     sudo apt install python3.12-venv # Debian/Ubuntu" >&2
      return 1
    }
    echo "▸ creating Python venv with $("$PY" -V 2>&1) ($PY)…"
    "$PY" -m venv "$VENV"
  fi
  if [ ! -f "$VENV/.deps-ok" ] || [ "$ROOT/pyproject.toml" -nt "$VENV/.deps-ok" ]; then
    echo "▸ installing backend deps (this can take a few minutes the first time)…"
    "$VENV/bin/pip" install -q --upgrade pip
    # This backend + all its deps, including the curry-leaves kernel and the cl_memory engine,
    # come from PyPI as ordinary pinned dependencies.
    "$VENV/bin/pip" install -q -e "$ROOT"
    touch "$VENV/.deps-ok"
  fi
}

# Ensure the prebuilt web UI is present in the package's webui/ dir so the backend can serve
# it. The frontend lives in its own repo (curry-leaves-assistant-web); scripts/build_webui.sh
# unpacks its published dist/ here. Idempotent: skips the fetch if webui/ already exists. To
# force a refresh (e.g. after bumping CURRY_LEAVES_WEB_VERSION), delete src/.../webui/ first.
# Point CURRY_LEAVES_WEB_DIR at a local web checkout with a built dist/ to use that instead.
#
# Pass "local" to build the sibling web checkout from source and use THAT instead of the
# pinned published version — see cl_build_local_webui. Deliberately opt-in: the default path
# must stay a published-version fetch, or a release would silently bundle whatever happened to
# be in someone's working tree.
cl_setup_webui() {
  if [ "${1:-}" = "local" ]; then
    cl_build_local_webui
    return $?
  fi
  if [ -d "$PKGDIR/webui" ] && [ -n "$(ls -A "$PKGDIR/webui" 2>/dev/null)" ]; then
    return 0
  fi
  echo "▸ fetching web UI…"
  "$ROOT/scripts/build_webui.sh"
}

# Build the sibling web repo from source and bundle THAT into the package, replacing whatever
# webui/ holds. Unconditional (no "already there" short-circuit) — the whole point is to pick
# up local UI edits, and skipping the rebuild would serve a stale bundle while looking like it
# worked, which is the failure mode this exists to avoid.
#
# The checkout is $CURRY_LEAVES_WEB_DIR if set, else the documented sibling layout
# (../curry-leaves-assistant-web). Fatal on a missing checkout or a failed build, rather than
# falling back to the published UI: silently serving a different UI than the one asked for
# would send you debugging a change that was never actually loaded.
cl_build_local_webui() {
  local web_dir="${CURRY_LEAVES_WEB_DIR:-$ROOT/../curry-leaves-assistant-web}"
  if [ ! -d "$web_dir" ]; then
    echo "✗ No web checkout at $web_dir." >&2
    echo "  Clone curry-leaves-assistant-web next to this repo, or set CURRY_LEAVES_WEB_DIR." >&2
    return 1
  fi
  web_dir="$(cd "$web_dir" && pwd)"   # absolute, so build_webui.sh's cp resolves it
  if ! command -v npm >/dev/null 2>&1; then
    echo "✗ npm not found — needed to build the web UI from source. Install Node.js." >&2
    return 1
  fi
  echo "▸ building web UI from source: $web_dir"
  # `npm install` on every run: cheap when node_modules is current, and the alternative is a
  # confusing build failure whenever package.json moved since the last local build.
  ( cd "$web_dir" && npm install --silent && npm run build ) || {
    echo "✗ web UI build failed in $web_dir" >&2
    return 1
  }
  # build_webui.sh removes webui/ first, so the stale bundle can't survive this.
  CURRY_LEAVES_WEB_DIR="$web_dir" "$ROOT/scripts/build_webui.sh"
}

# Ensure the one system binary Curry Leaves can use: espeak-ng, which Kokoro TTS phonemizes
# through for spoken replies (domain/tts.py). It's OPTIONAL — without it, voice output just
# hides itself and everything else works — so this is best-effort and never fatal: we try the
# platform package manager, and on failure print how to get it and move on.
cl_setup_system() {
  if command -v espeak-ng >/dev/null 2>&1; then return 0; fi
  echo "▸ installing espeak-ng (for spoken replies — optional)…"
  if command -v brew >/dev/null 2>&1; then
    brew install espeak-ng || true
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y espeak-ng || true
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y espeak-ng || true
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --noconfirm espeak-ng || true
  fi
  if ! command -v espeak-ng >/dev/null 2>&1; then
    echo "  ⚠ espeak-ng not installed — voice output stays off. Install it later if you want it:"
    echo "      brew install espeak-ng   |   sudo apt install espeak-ng"
  fi
}

# Default the provider to Copilot on first run (no .env yet).
cl_provider_hint() {
  if [ ! -f "$PKGDIR/.env" ]; then
    echo "▸ no src/curry_leaves_assistant/.env — defaulting to CURRY_LEAVES_PROVIDER=copilot"
    echo "CURRY_LEAVES_PROVIDER=copilot" > "$PKGDIR/.env"
  fi
}

# Stop a previously-running Curry Leaves so a stale process doesn't hold the port or shadow
# this run. Always kills the backend + web UI dev server; pass "electron" to also kill the
# Electron shell. Scoped to THIS repo's paths (via $ROOT) so it never touches other projects
# or editor tooling (e.g. LSP servers). `|| true` keeps set -e happy when nothing is running.
cl_kill_prior() {
  echo "▸ stopping any previous instance…"
  pkill -f "curry_leaves_assistant.app" 2>/dev/null || true
  pkill -f "curry-leaves-assistant" 2>/dev/null || true
  pkill -f "uvicorn curry_leaves_assistant" 2>/dev/null || true
  if [ "${1:-}" = "electron" ]; then
    pkill -f "$ROOT/node_modules/.bin/electron" 2>/dev/null || true
  fi
  sleep 0.5
}
