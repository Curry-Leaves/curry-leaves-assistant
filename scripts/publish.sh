#!/usr/bin/env bash
# Build curry-leaves-assistant (web UI + sdist/wheel) and publish it to PyPI.
#
# Usage:
#   ./scripts/publish.sh                        # build + upload to TestPyPI (safe default)
#   ./scripts/publish.sh --prod                 # build + upload to the real PyPI (asks to confirm)
#   ./scripts/publish.sh --build-only           # build only, skip upload entirely
#   ./scripts/publish.sh patch|minor|major|X.Y.Z [--prod]
#                                               # bump version + stamp CHANGELOG + git tag, then publish
#
# The bump forms edit pyproject.toml, stamp the `## [Unreleased]` CHANGELOG section with the new
# version + today's date, commit, and tag `vX.Y.Z`. The tag/commit are pushed only after a
# successful --prod upload. A bump requires a clean git working tree.
#
# Auth: needs a PyPI/TestPyPI API token. Either set it in the environment
# (TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-...) or let twine prompt for it —
# it never needs to be typed into this script.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
cd "$ROOT"

MODE="testpypi"
BUMP=""
for arg in "$@"; do
  case "$arg" in
    --prod)       MODE="prod" ;;
    --build-only) MODE="build-only" ;;
    patch|minor|major) BUMP="$arg" ;;
    [0-9]*.[0-9]*.[0-9]*) BUMP="$arg" ;;
    *) echo "✗ unknown argument: $arg" >&2; exit 1 ;;
  esac
done

# ── venv + build/publish tooling ────────────────────────────────────────────────
if [ ! -x "$VENV/bin/python" ]; then
  echo "▸ creating venv…"
  python3 -m venv "$VENV"
fi
echo "▸ installing build + twine…"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q --upgrade build twine
PY="$VENV/bin/python"

current_version() {
  "$PY" -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])'
}

# ── optional: bump version + stamp changelog + tag ──────────────────────────────
if [ -n "$BUMP" ]; then
  if [ -n "$(git status --porcelain)" ]; then
    echo "✗ working tree is not clean — commit or stash before a release bump." >&2
    git status --short
    exit 1
  fi
  OLD_VERSION="$(current_version)"
  NEW_VERSION="$("$PY" - "$OLD_VERSION" "$BUMP" <<'EOF'
import re, sys
old, bump = sys.argv[1], sys.argv[2]
if re.fullmatch(r"\d+\.\d+\.\d+", bump):
    print(bump)
else:
    major, minor, patch = map(int, old.split("."))
    if bump == "major":   major, minor, patch = major + 1, 0, 0
    elif bump == "minor": minor, patch = minor + 1, 0
    elif bump == "patch": patch += 1
    else: sys.exit(f"error: unknown bump '{bump}'")
    print(f"{major}.{minor}.{patch}")
EOF
)"
  echo "▸ bumping version $OLD_VERSION → $NEW_VERSION"
  "$PY" - "$OLD_VERSION" "$NEW_VERSION" <<'EOF'
import sys
old, new = sys.argv[1], sys.argv[2]
path, pattern = "pyproject.toml", 'version = "{}"'
text = open(path).read()
needle, repl = pattern.format(old), pattern.format(new)
if needle not in text:
    sys.exit(f"error: {needle!r} not found in {path}")
open(path, "w").write(text.replace(needle, repl, 1))
EOF
  echo "▸ stamping CHANGELOG.md"
  "$PY" - "$NEW_VERSION" <<'EOF'
import sys
from datetime import date
new = sys.argv[1]
text = open("CHANGELOG.md").read()
if "## [Unreleased]" not in text:
    sys.exit("error: no '## [Unreleased]' section in CHANGELOG.md to stamp")
# Keep an empty Unreleased section on top; add the dated release below it.
text = text.replace(
    "## [Unreleased]",
    f"## [Unreleased]\n\n## [{new}] - {date.today().isoformat()}",
    1,
)
open("CHANGELOG.md", "w").write(text)
EOF
  git add pyproject.toml CHANGELOG.md
  git commit -m "chore: release v$NEW_VERSION"
  git tag "v$NEW_VERSION"
fi

# ── fetch the prebuilt UI, then build the sdist/wheel ───────────────────────────
echo "▸ fetching web UI…"
"$ROOT/scripts/build_webui.sh"

echo "▸ building sdist + wheel…"
rm -rf "$ROOT/dist"
"$PY" -m build

echo "▸ built artifacts:"
ls -la "$ROOT"/dist/

# ── sanity checks before anything goes near PyPI ────────────────────────────────
echo "▸ checking metadata (twine check)…"
"$VENV/bin/twine" check "$ROOT"/dist/*

WHEEL="$(ls "$ROOT"/dist/*.whl)"
# Capture the listing once, then match against the variable — piping `unzip -l` straight into
# `grep -q` is fragile under `set -o pipefail` (grep closes the pipe on first match, unzip dies
# with SIGPIPE, and the pipeline reports failure even though the file WAS found).
WHEEL_LIST="$(unzip -l "$WHEEL")"
if ! printf '%s\n' "$WHEEL_LIST" | grep -q "curry_leaves_assistant/webui/index.html"; then
  echo "✗ webui/ missing from the wheel — aborting before upload." >&2
  exit 1
fi
if printf '%s\n' "$WHEEL_LIST" | grep -qE "curry_leaves_assistant/\.env$|curry_leaves_assistant/\.env\."; then
  echo "✗ a .env file leaked into the wheel — aborting before upload." >&2
  exit 1
fi
echo "▸ sanity checks passed (webui present, no .env leak)."

if [ "$MODE" = "build-only" ]; then
  echo "▸ --build-only: stopping before upload. Artifacts are in dist/."
  exit 0
fi

# ── upload ───────────────────────────────────────────────────────────────────────
if [ "$MODE" = "prod" ]; then
  VERSION="$(basename "$WHEEL" | sed -E 's/curry_leaves_assistant-([0-9.]+)-.*/\1/')"
  echo
  echo "▸ About to upload curry-leaves-assistant@$VERSION to the REAL PyPI"
  echo "  (public, permanent — versions can't be reused)."
  read -r -p "  Type the version to confirm: " CONFIRM
  if [ "$CONFIRM" != "$VERSION" ]; then
    echo "✗ confirmation did not match version $VERSION — aborting." >&2
    exit 1
  fi
  echo "▸ uploading to PyPI…"
  "$VENV/bin/twine" upload "$ROOT"/dist/*
  if [ -n "$BUMP" ]; then
    echo "▸ pushing release commit + tag…"
    git push
    git push --tags
  fi
  echo "▸ done: published curry-leaves-assistant@$VERSION"
else
  echo "▸ uploading to TestPyPI (https://test.pypi.org)…"
  "$VENV/bin/twine" upload --repository testpypi "$ROOT"/dist/*
  echo
  echo "▸ verify with:  pip install --index-url https://test.pypi.org/simple/ curry-leaves-assistant"
  echo "▸ once it looks right, publish for real with:  ./scripts/publish.sh --prod"
  if [ -n "$BUMP" ]; then
    echo "▸ note: the release commit + tag v$NEW_VERSION are local only — they're pushed"
    echo "        after a successful --prod upload (re-run with --prod when ready)."
  fi
fi
