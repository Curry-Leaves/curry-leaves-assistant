#!/usr/bin/env python3
"""Diagnose the GitHub Copilot model catalog: raw token vs exchanged session token.

Why this exists
---------------
The ``/models`` endpoint returns a catalog scoped to *who is asking* — specifically
the bearer token you present. VS Code / the Copilot CLI exchange their GitHub OAuth
token for a short-lived **Copilot session token** at ``copilot_internal/v2/token`` and
send that; the endpoint then returns the full catalog. Sending the **raw** GitHub token
(what curry-leaves currently falls back to for a custom OAuth-app client id) is treated
as a lower-privilege caller and returns a truncated list.

This script probes both paths against the live endpoint and prints a side-by-side
comparison so you can confirm the difference before/after changing the auth flow.

Usage
-----
    # Auto-discover a token (settings.json → env → Copilot CLI cache):
    python scripts/check_copilot_models.py

    # Or point at a specific GitHub OAuth token:
    python scripts/check_copilot_models.py --token ghu_xxx
    GITHUB_COPILOT_TOKEN=ghu_xxx python scripts/check_copilot_models.py

    # Show every model (not just picker-enabled), and dump one full entry:
    python scripts/check_copilot_models.py --all --dump

Exit code is 0 if the exchange path returns at least as many models as the raw path
(the expected, healthy state), 1 otherwise — so it doubles as a smoke test in CI.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import httpx

# ── endpoints / headers (mirrors providers/copilot_provider.py) ────────────────
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
MODELS_URL = "https://api.githubcopilot.com/models"

# Copilot gates access on the editor identity — these must look like a real editor.
EDITOR_HEADERS = {
    "Copilot-Integration-Id": "vscode-chat",
    "Editor-Version": "vscode/1.95.0",
    "Editor-Plugin-Version": "copilot-chat/0.22.0",
    "User-Agent": "GitHubCopilotChat/0.22.0",
    "X-GitHub-Api-Version": "2025-05-01",
}


# ── token discovery ────────────────────────────────────────────────────────────
def _from_settings() -> tuple[str | None, str]:
    """The token curry-leaves itself would use, from ~/.curry-leaves*/settings.json."""
    for path in sorted(glob.glob(os.path.expanduser("~/.curry-leaves*/settings.json"))):
        try:
            cfg = json.load(open(path))
            tok = cfg.get("ai", {}).get("providers", {}).get("copilot", {}).get("githubToken")
            if tok:
                return tok, path
        except Exception:
            continue
    return None, ""


def _from_cli_cache() -> tuple[str | None, str]:
    """The standard GitHub Copilot CLI / Neovim OAuth cache (a ghu_ token that
    exchanges cleanly — useful for verifying the exchange path works at all)."""
    path = os.path.expanduser("~/.config/github-copilot/hosts.json")
    try:
        hosts = json.load(open(path))
        for host, v in hosts.items():
            if v.get("oauth_token"):
                return v["oauth_token"], f"{path} ({host})"
    except Exception:
        pass
    return None, ""


def discover_token(explicit: str | None) -> tuple[str, str]:
    if explicit:
        return explicit, "--token"
    if os.environ.get("GITHUB_COPILOT_TOKEN"):
        return os.environ["GITHUB_COPILOT_TOKEN"], "$GITHUB_COPILOT_TOKEN"
    tok, src = _from_settings()
    if tok:
        return tok, src
    tok, src = _from_cli_cache()
    if tok:
        return tok, src
    sys.exit(
        "✗ No GitHub Copilot token found.\n"
        "  Connect Copilot in the app, set $GITHUB_COPILOT_TOKEN, or pass --token.\n"
        "  (Also checked ~/.curry-leaves*/settings.json and ~/.config/github-copilot/hosts.json.)"
    )


# ── probes ─────────────────────────────────────────────────────────────────────
def exchange(github_token: str) -> tuple[str | None, dict]:
    """Exchange the GitHub token for a Copilot session token. Returns (token, envelope)
    or (None, {}) if the endpoint rejects us (e.g. a custom OAuth-app client id 404s)."""
    try:
        r = httpx.get(
            COPILOT_TOKEN_URL,
            headers={"Authorization": f"token {github_token}", "Accept": "application/json", **EDITOR_HEADERS},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"  exchange → HTTP {r.status_code} ({r.text[:120].strip()})")
            return None, {}
        d = r.json()
        return d.get("token"), d
    except Exception as e:  # noqa: BLE001 — diagnostic tool, surface anything
        print(f"  exchange → error: {e!r}")
        return None, {}


def list_models(bearer: str, base: str = MODELS_URL) -> list[dict]:
    r = httpx.get(base, headers={"Authorization": f"Bearer {bearer}", **EDITOR_HEADERS}, timeout=30)
    r.raise_for_status()
    data = r.json()
    return (data.get("data") if isinstance(data, dict) else data) or []


def summarize(models: list[dict]) -> tuple[int, list[dict]]:
    """(total, picker-enabled-and-not-policy-disabled) — the set a UI should show."""
    picker = [
        m for m in models
        if m.get("model_picker_enabled") and (m.get("policy") or {}).get("state") != "disabled"
    ]
    return len(models), picker


def print_models(models: list[dict], show_all: bool) -> None:
    for m in sorted(models, key=lambda x: x.get("id", "")):
        picker = m.get("model_picker_enabled")
        policy = (m.get("policy") or {}).get("state")
        shown = picker and policy != "disabled"
        if not show_all and not shown:
            continue
        mark = "✓" if shown else " "
        print(f"    [{mark}] {m.get('id'):<32} picker={str(picker):<5} policy={policy}")


# ── main ───────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token", help="GitHub OAuth token to test (else auto-discovered)")
    ap.add_argument("--all", action="store_true", help="list every model, not just picker-enabled")
    ap.add_argument("--dump", action="store_true", help="print one full model entry (JSON)")
    args = ap.parse_args()

    token, src = discover_token(args.token)
    ttype = token[:4] if len(token) > 4 else "?"
    print(f"▸ token source : {src}")
    print(f"▸ token type   : {ttype}_…  ({'GitHub App user token — exchanges cleanly' if ttype == 'ghu_' else 'OAuth-App token — exchange may 404' if ttype == 'gho_' else 'unknown prefix'})")
    print()

    # ── Path A: raw GitHub token as bearer (curry-leaves' current fallback) ──────
    print("── Path A: RAW GitHub token as bearer ──────────────────────────────────")
    try:
        raw_models = list_models(token)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ /models failed: {e!r}")
        raw_models = []
    raw_total, raw_picker = summarize(raw_models)
    print(f"  total={raw_total}  picker-enabled={len(raw_picker)}")
    print_models(raw_models, args.all)
    print()

    # ── Path B: exchanged Copilot session token (what VS Code sends) ─────────────
    print("── Path B: EXCHANGED Copilot session token ─────────────────────────────")
    session, envelope = exchange(token)
    ex_models: list[dict] = []
    if session:
        base = (envelope.get("endpoints") or {}).get("api")
        base = f"{base}/models" if base else MODELS_URL
        sku = envelope.get("sku")
        print(f"  exchange OK  sku={sku}  endpoints.api={(envelope.get('endpoints') or {}).get('api')}")
        try:
            ex_models = list_models(session, base)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ /models failed: {e!r}")
    else:
        print("  (no session token — the raw-token path is all this client can reach)")
    ex_total, ex_picker = summarize(ex_models)
    print(f"  total={ex_total}  picker-enabled={len(ex_picker)}")
    print_models(ex_models, args.all)
    print()

    if args.dump and (ex_models or raw_models):
        print("── One full model entry ────────────────────────────────────────────────")
        print(json.dumps((ex_models or raw_models)[0], indent=2))
        print()

    # ── verdict ─────────────────────────────────────────────────────────────────
    print("── Verdict ─────────────────────────────────────────────────────────────")
    print(f"  raw picker-enabled      : {len(raw_picker)}")
    print(f"  exchanged picker-enabled: {len(ex_picker)}")
    if session and len(ex_picker) > len(raw_picker):
        gained = len(ex_picker) - len(raw_picker)
        print(f"  ✓ Exchange unlocks {gained} more model(s). The fix (use the session token) works.")
        return 0
    if not session:
        print("  ⚠ Exchange rejected this token — you'd stay on the truncated raw list.")
        print("    A ghu_ (GitHub App) token, or the first-party client id, is needed to exchange.")
        return 1
    print("  = No difference for this token/account (already fully entitled, or same catalog).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
