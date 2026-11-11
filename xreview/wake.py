"""Keeping the computer awake while a review needs it.

While any xreview command runs, sleep is held off for as long as it runs.
At a stop, it's held off for 1.5 hours from the last command for that
review, ending early if Session A closes. A review has one stop hold at a
time: a new one replaces the old. macOS uses caffeinate; Linux uses
systemd-inhibit where it's available; otherwise there's no hold.

A hold can't keep a Mac with its lid closed on battery from sleeping. Phone
messages and Claude's own work don't keep a Mac awake either, because
Claude Code holds no sleep assertion.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

from . import procs

STOP_SECONDS = 90 * 60


def stop_seconds() -> int:
    """How long a stop hold lasts. XREVIEW_WAKE_SECONDS shortens it for
    tests."""
    return int(os.environ.get("XREVIEW_WAKE_SECONDS") or STOP_SECONDS)


def session_a() -> int | None:
    """Session A's process: the nearest ancestor named `claude`, which is
    the CLI and VS Code's bundled binary alike. None if there's none."""
    pid = os.getppid()
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        line = procs.ps("-o", "ppid=,comm=", "-p", str(pid)).strip()
        if not line:
            return None
        parent, _, command = line.partition(" ")
        if Path(command.strip()).name == "claude":
            return pid
        pid = int(parent)
    return None


def holder(seconds: int | None, until: int | None) -> list[str] | None:
    """The command that holds sleep off for seconds, or until the process
    until exits, whichever comes first; None where there's no way to."""
    caffeinate = shutil.which("caffeinate")
    if sys.platform == "darwin" and caffeinate:
        argv = [caffeinate, "-i"]
        if seconds:
            argv += ["-t", str(seconds)]
        if until:
            argv += ["-w", str(until)]
        return argv
    inhibit = shutil.which("systemd-inhibit")
    if inhibit:
        argv = [
            inhibit,
            "--what=idle:sleep",
            "--who=xreview",
            "--why=A code review is in progress",
        ]
        if seconds:
            argv += ["timeout", str(seconds)]
        if until:
            return argv + ["tail", f"--pid={until}", "-f", "/dev/null"]
        return argv + ["sleep", str(seconds)]
    return None


def start(argv: list[str]) -> dict:
    """Start a hold on its own, so it outlives this command."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"pid": proc.pid, "started": procs.identity(proc.pid)}


def while_running() -> None:
    """Hold sleep off for as long as this command runs."""
    argv = holder(None, os.getpid())
    if argv:
        start(argv)


def at_stop(previous: dict | None) -> dict | None:
    """Replace the review's stop hold with a new one, and return it."""
    if previous and procs.alive(previous.get("pid"), previous.get("started")):
        procs.kill_group(previous["pid"])
    argv = holder(stop_seconds(), session_a())
    return start(argv) if argv else None
