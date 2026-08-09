"""The `curry-leaves-assistant` command line.

Deliberately the console-script target instead of `app:main`: importing app.py pulls in
FastAPI, uvicorn, the model catalog and every router — ~5s and a hard failure on a
half-broken install. `stop` and `status` only need a pid and a signal, so this module
imports nothing heavier than argparse and core.service, and defers the app import to the
`start` path where it's genuinely needed.

`start` is the default and subcommands are optional, so the historical bare
`curry-leaves-assistant` still boots the server. app.main() remains a working entrypoint
for `python -m curry_leaves_assistant.app` (what the Electron shell spawns).
"""
from __future__ import annotations

import argparse

from curry_leaves_assistant.core import service


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="curry-leaves-assistant",
        description="Curry Leaves Assistant — local-first voice/meeting assistant.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("start", help="run the backend (default)")
    stop_cmd = sub.add_parser("stop", help="stop a backend started earlier")
    stop_cmd.add_argument(
        "--timeout", type=float, default=10.0,
        help="seconds to wait for a graceful shutdown before SIGKILL (default: 10)",
    )
    sub.add_parser("status", help="report whether a backend is running")
    args = parser.parse_args(argv)

    if args.command == "stop":
        ok, message = service.stop(timeout=args.timeout)
        print(message)
        raise SystemExit(0 if ok else 1)
    if args.command == "status":
        running, message = service.status()
        print(message)
        raise SystemExit(0 if running else 1)

    from curry_leaves_assistant.app import serve  # heavy — only the start path pays for it

    serve()


if __name__ == "__main__":
    main()
