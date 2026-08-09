#!/usr/bin/env bash
# Fetches the prebuilt Curry Leaves web UI and drops it into
# src/curry_leaves_assistant/webui/ so it ships as package data inside the pip
# wheel — run this before `python -m build`.
#
# The frontend lives in its own repo (curry-leaves-assistant-web) and is published
# to npm with a static dist/ bundle. We don't build it here; we just unpack a
# pinned, published version. Override the version with CURRY_LEAVES_WEB_VERSION.
#
# Local override: if CURRY_LEAVES_WEB_DIR points at a checkout of the web repo that
# has a built dist/, that dist/ is used instead of the npm tarball — handy for
# testing an unpublished UI change end to end.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEBUI_DIR="$ROOT/src/curry_leaves_assistant/webui"
WEB_PKG="curry-leaves-assistant-web"
WEB_VERSION="${CURRY_LEAVES_WEB_VERSION:-1.3.0}"

rm -rf "$WEBUI_DIR"

# ── Local checkout override ────────────────────────────────────────────────────
if [ -n "${CURRY_LEAVES_WEB_DIR:-}" ]; then
  if [ ! -d "$CURRY_LEAVES_WEB_DIR/dist" ]; then
    echo "✗ CURRY_LEAVES_WEB_DIR=$CURRY_LEAVES_WEB_DIR has no dist/ — run 'npm run build' there first." >&2
    exit 1
  fi
  echo "▸ using local web build from $CURRY_LEAVES_WEB_DIR/dist"
  cp -R "$CURRY_LEAVES_WEB_DIR/dist" "$WEBUI_DIR"
  echo "▸ done: $WEBUI_DIR"
  exit 0
fi

# ── Published tarball ──────────────────────────────────────────────────────────
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "▸ fetching $WEB_PKG@$WEB_VERSION from npm…"
# `npm pack` downloads the tarball (respecting the package's "files": ["dist"]).
TARBALL="$(cd "$TMP" && npm pack "$WEB_PKG@$WEB_VERSION" 2>/dev/null)"

echo "▸ unpacking…"
tar -xzf "$TMP/$TARBALL" -C "$TMP"        # → $TMP/package/dist/

if [ ! -d "$TMP/package/dist" ]; then
  echo "✗ $WEB_PKG@$WEB_VERSION contains no dist/ — was it built before publish?" >&2
  exit 1
fi

cp -R "$TMP/package/dist" "$WEBUI_DIR"
echo "▸ done: $WEBUI_DIR"
