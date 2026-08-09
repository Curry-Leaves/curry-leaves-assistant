#!/usr/bin/env python3
"""Minimal one-file Anthropic chat — log in with your API key and hold a back-and-forth.

"Login" = your Anthropic API key (from https://console.anthropic.com/settings/keys). The API is
billed per token and is separate from a Claude.ai Pro/Max subscription.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python scripts/anthropic_chat.py
    python scripts/anthropic_chat.py --model claude-opus-4-8 --system "You are terse."

Needs:  pip install anthropic
Type your message and press Enter. Commands: /reset clears history, /exit quits.
"""
from __future__ import annotations

import argparse
import os
import sys

try:
    from anthropic import Anthropic
except ImportError:
    sys.exit("✗ The 'anthropic' package isn't installed. Run:  pip install anthropic")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    # Default to a current, capable model. Override with --model (see console for the full list).
    ap.add_argument("--model", default="claude-sonnet-5", help="model id (default: claude-sonnet-5)")
    ap.add_argument("--system", default="You are a helpful assistant.", help="system prompt")
    ap.add_argument("--max-tokens", type=int, default=1024, help="max tokens per reply")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("✗ Set ANTHROPIC_API_KEY first:  export ANTHROPIC_API_KEY=sk-ant-…\n"
                 "  Get a key at https://console.anthropic.com/settings/keys")

    client = Anthropic(api_key=key)  # this is the "login" — the key authenticates every request

    # Full conversation history, so each turn has the context of the prior ones (real back-and-forth).
    messages: list[dict] = []

    print(f"▸ Connected. model={args.model}  (/reset to clear, /exit to quit)\n")
    while True:
        try:
            user = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            return 0
        if not user:
            continue
        if user in ("/exit", "/quit"):
            print("bye.")
            return 0
        if user == "/reset":
            messages.clear()
            print("(history cleared)\n")
            continue

        messages.append({"role": "user", "content": user})

        # Stream the reply token-by-token, and collect it so it can go back into history.
        print("claude › ", end="", flush=True)
        reply_parts: list[str] = []
        try:
            with client.messages.stream(
                model=args.model,
                max_tokens=args.max_tokens,
                system=args.system,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    print(text, end="", flush=True)
                    reply_parts.append(text)
        except Exception as e:  # noqa: BLE001 — surface any API/network error to the user
            print(f"\n✗ request failed: {e}")
            messages.pop()  # drop the user turn we couldn't answer, so history stays consistent
            continue
        print("\n")
        messages.append({"role": "assistant", "content": "".join(reply_parts)})


if __name__ == "__main__":
    raise SystemExit(main())
