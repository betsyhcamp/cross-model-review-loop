"""Tests for keeping the Mac awake, checked by reading its sleep assertions
with pmset. They need macOS's caffeinate, so they're skipped elsewhere.

A stand-in for Session A is a Python interpreter reached through a link
named `claude`, which is what ps shows as its name.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from test_runner import XREVIEW, Review
from xreview import procs

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/caffeinate").exists(),
    reason="needs macOS's caffeinate and pmset",
)


def holds() -> dict[int, str]:
    """The processes holding an idle-sleep assertion, by pid."""
    out = subprocess.run(
        ["pmset", "-g", "assertions"], capture_output=True, text=True
    ).stdout
    found = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("pid ") and "PreventUserIdleSystemSleep" in line:
            pid = int(line.split()[1].split("(")[0])
            found[pid] = line
    return found


def arguments(pid: int) -> str:
    return procs.ps("-o", "args=", "-p", str(pid)).strip()


def wait_until(check, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def review(repo):
    """A review whose commands can find caffeinate, with a 4-second stop
    hold."""
    found = Review(repo)
    folder = repo.tmp / "wake-bin"
    folder.mkdir()
    (folder / "caffeinate").symlink_to("/usr/bin/caffeinate")
    found.repo.env = {
        **repo.env,
        "PATH": f"{folder}:{repo.env['PATH']}",
        "XREVIEW_WAKE_SECONDS": "4",
    }
    yield found
    stop = found.run_state().get("wake") if found.path.exists() else None
    if stop:
        procs.kill_group(stop["pid"])


def stop_hold(review) -> dict:
    return review.run_state()["wake"]


def test_a_running_command_holds_sleep_off_until_it_ends(review):
    sleeps = json.dumps({"codex-review": 3})
    runner = review.background(FAKE_CODEX_SLEEPS=sleeps)
    review.wait_for_reviewer("run:codex-review")

    def op_hold():
        for pid in holds():
            if f"-w {runner.pid}" in arguments(pid):
                return pid
        return None

    assert wait_until(lambda: op_hold() is not None), holds()
    pid = op_hold()
    assert runner.wait(timeout=30) == 0
    assert wait_until(lambda: pid not in holds())


def test_a_stop_holds_sleep_off_for_a_while_then_lets_go(review):
    assert review.run().returncode == 0
    hold = stop_hold(review)
    assert hold["pid"] in holds()
    assert "-i -t 4" in arguments(hold["pid"])
    assert wait_until(lambda: hold["pid"] not in holds(), timeout=15)


def test_each_command_replaces_the_reviews_stop_hold(review):
    assert review.run().returncode == 0
    first = stop_hold(review)
    assert review.run().returncode == 0
    second = stop_hold(review)
    assert second["pid"] != first["pid"]
    assert not procs.alive(first["pid"], first["started"])
    assert second["pid"] in holds()


def test_the_stop_hold_ends_when_session_a_closes(review):
    """Session A is the nearest ancestor named claude; its stand-in runs
    xreview, then stays open until killed."""
    link = review.repo.tmp / "session-a" / "claude"
    link.parent.mkdir()
    link.symlink_to(sys.executable)
    script = (
        "import subprocess, sys, time\n"
        f"subprocess.run([{str(XREVIEW)!r}, 'run', {review.name!r}])\n"
        "time.sleep(60)\n"
    )
    env = {**review.env(), "XREVIEW_WAKE_SECONDS": "60"}
    session = subprocess.Popen(
        [str(link), "-c", script], cwd=review.repo.path, env=env
    )
    try:
        assert wait_until(
            lambda: (review.path / "run-state.json").exists()
            and review.run_state().get("wake"),
            timeout=30,
        )
        hold = stop_hold(review)
        assert f"-w {session.pid}" in arguments(hold["pid"])
        assert hold["pid"] in holds()
    finally:
        session.kill()
        session.wait()
    assert wait_until(lambda: hold["pid"] not in holds())


def test_status_at_a_stop_restarts_the_hold(review):
    assert review.run().returncode == 0
    first = stop_hold(review)
    result = review.xreview("status", review.name)
    assert result.returncode == 0, result.stderr
    second = stop_hold(review)
    assert second["pid"] != first["pid"]
    assert not procs.alive(first["pid"], first["started"])
    assert second["pid"] in holds()
