"""Tests for recognizing a reviewer process again: its ID plus its start
time, so a reused ID isn't mistaken for the same process."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from xreview import procs


@pytest.fixture
def sleeper():
    """A process in its own group that sleeps until killed."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    yield proc
    procs.kill_group(proc.pid)
    proc.wait()


def test_a_running_process_is_recognized_by_id_and_start_time(sleeper):
    started = procs.identity(sleeper.pid)
    assert started
    assert procs.alive(sleeper.pid, started)
    assert procs.identity(sleeper.pid) == started


def test_a_reused_id_is_not_the_same_process(sleeper):
    # A later process with the same ID has a different start time.
    assert not procs.alive(sleeper.pid, "Thu Jan  1 00:00:00 1970")
    assert not procs.alive(sleeper.pid, None)


def test_an_exited_process_is_not_alive():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    started = procs.identity(proc.pid)
    proc.wait()
    assert procs.identity(proc.pid) is None
    assert not procs.alive(proc.pid, started)


def test_find_locates_a_process_by_its_command_line(tmp_path):
    marker = str(tmp_path / "run-folder-marker")
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", marker],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        found = None
        while found is None and time.monotonic() < deadline:
            found = procs.find(marker)
        assert found == (proc.pid, procs.identity(proc.pid))
        assert procs.find(str(tmp_path / "no-such-marker")) is None
    finally:
        procs.kill_group(proc.pid)
        proc.wait()


def orphan(seconds: float) -> int:
    """Start a process that isn't this test's child, as a reviewer left by
    a dead runner isn't, so init reaps it when it exits."""
    launcher = (
        "import subprocess, sys; "
        "p = subprocess.Popen([sys.executable, '-c', "
        f"'import time; time.sleep({seconds})'], start_new_session=True, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print(p.pid)"
    )
    out = subprocess.run(
        [sys.executable, "-c", launcher], capture_output=True, text=True
    )
    return int(out.stdout)


def test_waiting_for_another_runners_process_ends_when_it_exits(
    monkeypatch,
):
    monkeypatch.setattr(procs, "OTHER_POLL_SECONDS", 0.05)
    pid = orphan(2)
    started = procs.identity(pid)
    lines = []
    began = time.monotonic()
    procs.wait_for_other(pid, started, time.time() + 10, 0.1, lines.append)
    assert time.monotonic() - began < 5
    assert not procs.alive(pid, started)
    assert lines, "no progress was reported while waiting"


def test_waiting_past_the_deadline_kills_the_process(sleeper, monkeypatch):
    monkeypatch.setattr(procs, "OTHER_POLL_SECONDS", 0.05)
    started = procs.identity(sleeper.pid)
    with pytest.raises(procs.TimedOut):
        procs.wait_for_other(
            sleeper.pid, started, time.time() + 0.3, 10, lambda _: None
        )
    assert sleeper.wait(timeout=5) == -signal.SIGKILL


def test_run_reports_the_process_as_it_starts(tmp_path):
    seen = []
    code = procs.run(
        [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
        "the prompt\n",
        tmp_path,
        tmp_path,
        timeout=10,
        progress_every=10,
        progress=lambda _: None,
        on_start=lambda pid, started: seen.append((pid, started)),
    )
    assert code == 0
    ((pid, started),) = seen
    assert pid != os.getpid() and started
    assert (tmp_path / "prompt.md").read_text() == "the prompt\n"
    assert (tmp_path / "events.jsonl").read_text() == "the prompt\n\n"


def test_a_ps_that_cant_run_recognizes_no_process(monkeypatch, sleeper):
    def refused(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted", "/bin/ps")

    monkeypatch.setattr(procs.subprocess, "run", refused)
    assert procs.identity(sleeper.pid) is None
    assert not procs.alive(sleeper.pid, "anything")
    assert procs.find("anything") is None
