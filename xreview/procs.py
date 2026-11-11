"""Running a reviewer process, and recognizing it again later.

A reviewer runs in its own process group with its prompt on stdin, its
output in its run folder, a progress line every so often, and a time
limit. Its process ID and start time are recorded as it starts, so a later
runner can tell whether that same process is still running.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from typing import Callable

STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
POLL_SECONDS = 0.2  # for our own child, which wait() checks cheaply
OTHER_POLL_SECONDS = 1.0  # for another runner's child, which needs ps


class CallFailed(Exception):
    """A reviewer call that provably didn't run its turn, such as a missing
    binary or a startup error, or whose result was rejected. A plain retry
    is safe. Exits 3; the message is user-facing."""


class CallUncertain(Exception):
    """A reviewer call that may have run its turn: a timeout, a stop
    signal, a killed process, or a missing or unreadable result. It isn't
    resent without an explicit retry. Exits 4; the message is user-facing.
    """


class Stopped(Exception):
    """The runner received a stop signal while a reviewer was running."""


class TimedOut(Exception):
    """A reviewer ran past its time limit and was killed."""


def stop(signum: int, frame) -> None:
    raise Stopped(signal.Signals(signum).name)


def run(
    argv: list[str],
    prompt: str,
    cwd: Path,
    run_dir: Path,
    *,
    timeout: float,
    progress_every: float,
    progress: Callable[[float], None],
    env: dict[str, str] | None = None,
    on_start: Callable[[int, str | None], None] | None = None,
) -> int:
    """Run argv and return its exit code.

    The prompt is written to run_dir/prompt.md and fed to stdin from there;
    stdout and stderr go to events.jsonl and stderr.txt. on_start gets the
    process ID and start time as soon as the process exists. On a timeout
    or a stop signal (SIGTERM, SIGHUP, SIGINT), the whole process group is
    killed before TimedOut or Stopped is raised, so no reviewer keeps
    running after the runner gives up on it.
    """
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    previous = {sig: signal.getsignal(sig) for sig in STOP_SIGNALS}
    for sig in STOP_SIGNALS:
        signal.signal(sig, stop)
    try:
        with (
            (run_dir / "prompt.md").open("rb") as stdin,
            (run_dir / "events.jsonl").open("wb") as out,
            (run_dir / "stderr.txt").open("wb") as err,
        ):
            with subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=stdin,
                stdout=out,
                stderr=err,
                start_new_session=True,
            ) as proc:
                try:
                    if on_start:
                        on_start(proc.pid, identity(proc.pid))
                    return wait(proc, timeout, progress_every, progress)
                except (TimedOut, Stopped):
                    kill_group(proc.pid)
                    proc.wait()
                    raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def wait(
    proc: subprocess.Popen,
    timeout: float,
    progress_every: float,
    progress: Callable[[float], None],
) -> int:
    """Wait for proc to exit, calling progress every progress_every
    seconds. Raises TimedOut once timeout seconds have passed."""
    began = time.monotonic()
    reported = began
    while True:
        try:
            return proc.wait(timeout=POLL_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if now - began >= timeout:
            raise TimedOut()
        if now - reported >= progress_every:
            reported = now
            progress(now - began)


def wait_for_other(
    pid: int,
    started: str,
    deadline: float,
    progress_every: float,
    progress: Callable[[float], None],
) -> None:
    """Wait for a process this runner didn't start to exit.

    deadline is a time.time() value. Past it, the process group is killed
    and TimedOut raised. A stop signal kills it too, then raises Stopped.
    """
    previous = {sig: signal.getsignal(sig) for sig in STOP_SIGNALS}
    for sig in STOP_SIGNALS:
        signal.signal(sig, stop)
    began = time.monotonic()
    reported = began
    try:
        while alive(pid, started):
            if time.time() >= deadline:
                kill_group(pid)
                raise TimedOut()
            time.sleep(OTHER_POLL_SECONDS)
            now = time.monotonic()
            if now - reported >= progress_every:
                reported = now
                progress(now - began)
    except Stopped:
        kill_group(pid)
        raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def kill_group(pid: int) -> None:
    # A stop from Claude Code signals the whole process tree at once, so the
    # reviewer may already be dead. Not yet reaped, macOS reports EPERM for
    # its group; once reaped, ESRCH.
    with contextlib.suppress(PermissionError, ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def ps(*args: str) -> str:
    """ps output, with a fixed locale and time zone so start times compare
    equal across calls, or "" if ps can't run. ps is looked up on PATH,
    then in /bin."""
    command = shutil.which("ps") or "/bin/ps"
    env = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
    try:
        result = subprocess.run(
            [command, *args], capture_output=True, text=True, env=env
        )
    except OSError:
        # Inside another sandbox, macOS may refuse to run ps. Then no
        # process can be recognized, which is the cautious answer.
        return ""
    return result.stdout


def identity(pid: int) -> str | None:
    """The process's start time as ps prints it, or None if there's no
    such process. Together with the ID, it tells a process apart from a
    later one that reuses the ID."""
    return ps("-o", "lstart=", "-p", str(pid)).strip() or None


def alive(pid: int | None, started: str | None) -> bool:
    """Whether the process recorded as (pid, started) is still running."""
    return bool(pid and started) and identity(pid) == started


def find(text: str) -> tuple[int, str] | None:
    """A running process whose command line contains text, as (pid, start
    time), for when a runner died before it could record the process."""
    for line in ps("-axo", "pid=,command=").splitlines():
        pid, _, command = line.strip().partition(" ")
        if text in command and pid.isdigit() and int(pid) != os.getpid():
            started = identity(int(pid))
            if started:
                return int(pid), started
    return None
