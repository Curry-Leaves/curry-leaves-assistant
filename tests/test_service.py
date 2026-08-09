"""The pid-file lifecycle behind `curry-leaves-assistant stop`.

The property worth pinning down is the stale-pid one: pids get recycled, so a pid file
left behind by a `kill -9` can point at some unrelated process, and `stop` must refuse to
signal it. Everything else here guards the surrounding bookkeeping.
"""
from __future__ import annotations

import os
import subprocess

from curry_leaves_assistant.core import service
from curry_leaves_assistant.core.paths import SERVICE_PID_PATH


def _clear() -> None:
    if SERVICE_PID_PATH.exists():
        SERVICE_PID_PATH.unlink()


def test_write_then_read_roundtrips_our_pid():
    _clear()
    service.write_pid()
    assert service.read_pid() == os.getpid()


def test_read_pid_returns_none_on_garbage():
    SERVICE_PID_PATH.write_text("not-a-pid")
    assert service.read_pid() is None


def test_clear_pid_removes_our_own_record():
    service.write_pid()
    service.clear_pid()
    assert not SERVICE_PID_PATH.exists()


def test_clear_pid_leaves_another_instances_record_alone():
    """A second backend that took over the file must stay stoppable after we exit."""
    SERVICE_PID_PATH.write_text("999999")
    service.clear_pid()
    assert SERVICE_PID_PATH.read_text().strip() == "999999"


def test_dead_pid_is_not_running():
    assert not service.is_running(999999)


def test_cmdline_matcher_accepts_every_real_launch_form():
    """The three ways a backend actually starts must all be recognized — a false negative
    here means a running instance reports as stopped and can't be stopped."""
    for cmd in (
        "/repo/.venv/bin/curry-leaves-assistant",
        "/repo/.venv/bin/curry-leaves-assistant start",
        "/usr/bin/python3.12 -m curry_leaves_assistant.app",
        "/usr/bin/python -m curry_leaves_assistant",
        "/usr/bin/python /repo/src/curry_leaves_assistant/app.py",
    ):
        assert any(p.search(cmd) for p in service._CMDLINE_PATTERNS), cmd


def test_cmdline_matcher_rejects_processes_that_merely_mention_the_package():
    """Pid recycling means a stale pid file can land on an unrelated live process. Anything
    that only *mentions* the package must not be mistaken for the backend and killed."""
    for cmd in (
        'python -c "import curry_leaves_assistant"',
        "pytest /repo/src/curry_leaves_assistant/tests",
        "vim /repo/src/curry_leaves_assistant/app.py.bak",
        "grep -r curry_leaves_assistant .",
    ):
        assert not any(p.search(cmd) for p in service._CMDLINE_PATTERNS), cmd


def test_stop_refuses_a_stale_pid_and_leaves_that_process_alive():
    """The dangerous case, end to end: the pid file points at a live process that isn't a
    backend. stop() must clear the file and leave that process running."""
    victim = subprocess.Popen(["sleep", "30"])
    try:
        SERVICE_PID_PATH.write_text(str(victim.pid))
        stopped, message = service.stop(timeout=1.0)
        assert not stopped
        assert "stale" in message
        assert not SERVICE_PID_PATH.exists()  # cleared, so the next start isn't confused
        assert victim.poll() is None, "stop() killed an unrelated process"
    finally:
        victim.kill()
        victim.wait()


def test_stop_with_no_pid_file_reports_not_running():
    _clear()
    stopped, message = service.stop(timeout=1.0)
    assert not stopped
    assert "no running" in message


def test_status_reflects_absence():
    _clear()
    running, message = service.status()
    assert not running
    assert "not running" in message
