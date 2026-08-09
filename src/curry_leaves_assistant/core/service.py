"""The running backend's PID file — what lets `curry-leaves-assistant stop` find it.

A pip install has no supervisor: `curry-leaves-assistant` starts a process and the user
is left with a terminal. So the server records itself here on boot and clears the record
on clean exit, and the `stop` subcommand reads it back.

The file is ~/.curry-leaves/service.pid (see paths.SERVICE_PID_PATH), which the Electron
shell already writes and SIGKILLs — same path on purpose, so the CLI can stop a backend
the desktop app started and vice versa. It holds a bare integer pid for that reason: the
shell's `parseInt(readFileSync(...))` must keep working, so nothing richer (JSON with the
port, say) can go in there.

Staleness is the interesting case. A pid file outlives a `kill -9`, and pids are recycled,
so the pid alone can't say whether *our* backend is alive. Every read therefore verifies
liveness with signal 0 and cross-checks the process's command line before signalling it —
otherwise `stop` after a crash could kill an unrelated process that inherited the pid.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time

from curry_leaves_assistant.core.paths import SERVICE_PID_PATH

# How a Curry Leaves backend can actually have been launched: the console script, or
# `python -m curry_leaves_assistant[.app]` / a direct path to app.py.
#
# These are matched as regexes rather than by plain substring on purpose. A bare
# "curry_leaves_assistant" substring test also matches any process that merely MENTIONS
# the package — `python -c "import curry_leaves_assistant..."`, a pytest run whose args
# include the repo path, an editor indexing the tree. Since a recycled pid could land on
# exactly such a process, a loose match would let stop() kill it.
_CMDLINE_PATTERNS = (
    re.compile(r"(^|/)curry-leaves-assistant(\s|$)"),      # console script
    re.compile(r"-m\s+curry_leaves_assistant(\.app)?(\s|$)"),  # python -m …
    re.compile(r"(^|/)curry_leaves_assistant/app\.py(\s|$)"),  # python …/app.py
)


def write_pid() -> None:
    """Record this process as the running backend. Best-effort — never fatal at boot."""
    try:
        SERVICE_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        SERVICE_PID_PATH.write_text(str(os.getpid()))
    except OSError as exc:
        print(f"[service] could not write pid file: {exc}", flush=True)


def clear_pid() -> None:
    """Remove our pid file on clean shutdown, if it's still ours.

    The ownership check matters when a second backend has since taken over the file: our
    exit shouldn't erase the live instance's record and make it unstoppable.
    """
    try:
        if read_pid() == os.getpid():
            SERVICE_PID_PATH.unlink()
    except OSError:
        pass


def read_pid() -> int | None:
    """The pid recorded in the file, or None if absent/garbage."""
    try:
        pid = int(SERVICE_PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _cmdline(pid: int) -> str:
    """The process's command line, or "" if it can't be read.

    `ps` rather than psutil — no new dependency for one lookup, and it's on every
    platform we support (macOS + Linux; Windows falls through to "" and skips the check).
    """
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def is_running(pid: int) -> bool:
    """Whether `pid` is alive AND actually looks like a Curry Leaves backend.

    Signal 0 only proves *something* holds the pid. Since pids get recycled, a stale file
    can point at an unrelated process, so we also require a matching command line before
    treating it as ours. An unreadable command line (permissions, Windows) is accepted —
    failing open there matches the pre-existing shell behavior and only risks the case
    where the user already told us to stop this pid.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False  # someone else's process — definitively not our backend
    cmd = _cmdline(pid)
    return not cmd or any(p.search(cmd) for p in _CMDLINE_PATTERNS)


def stop(timeout: float = 10.0) -> tuple[bool, str]:
    """Stop the recorded backend. Returns (stopped, human-readable message).

    SIGTERM first so uvicorn runs its shutdown — orchestration.stop() drains the worker
    fleet and the queue is left consistent. Only if it hasn't exited within `timeout`
    do we escalate to SIGKILL, which skips that drain.
    """
    pid = read_pid()
    if pid is None:
        return False, "no running Curry Leaves backend found (no pid file)"
    if not is_running(pid):
        try:
            SERVICE_PID_PATH.unlink()
        except OSError:
            pass
        return False, f"no running backend (stale pid {pid} cleared)"

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return False, f"could not signal pid {pid}: {exc}"

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running(pid):
            try:
                SERVICE_PID_PATH.unlink()
            except OSError:
                pass
            return True, f"stopped Curry Leaves backend (pid {pid})"
        time.sleep(0.2)

    # Graceful shutdown overran — a stuck worker or a download in flight. Force it.
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        return False, f"pid {pid} ignored SIGTERM and could not be killed: {exc}"
    time.sleep(0.3)
    try:
        SERVICE_PID_PATH.unlink()
    except OSError:
        pass
    return True, f"force-stopped Curry Leaves backend (pid {pid}) after {timeout:.0f}s"


def status() -> tuple[bool, str]:
    """Whether a backend is running, and a message describing it."""
    pid = read_pid()
    if pid is None:
        return False, "Curry Leaves is not running"
    if not is_running(pid):
        return False, f"Curry Leaves is not running (stale pid file: {pid})"
    return True, f"Curry Leaves is running (pid {pid})"
