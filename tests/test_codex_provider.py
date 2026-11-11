# Portions adapted from claudex-loop
# (https://github.com/chaseai-yt/claudex-loop @ 8cf5e2c), Copyright (c) 2026
# Chase AI, MIT License. See THIRD_PARTY_NOTICES.md.
"""Tests for the Codex adapter, run in-process against the fake codex."""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from xreview.providers import codex

SESSION = "01a11457-66e5-73e3-8c89-991f3b54c198"
RESPONSE = "Finding 1: nothing real.\n\nFAKE-RESPONSE-BODY\n"


@pytest.fixture
def run_dir(repo) -> Path:
    path = repo.tmp / "run"
    path.mkdir()
    return path


@pytest.fixture
def call(repo, run_dir, monkeypatch):
    """Run codex.start, or codex.resume given a session, in run_dir.

    The fake codex logs each call to codex-calls.jsonl in the test's folder.
    """

    def run(
        case: str = "ok",
        session: str | None = None,
        prompt: str = "Review this.\n",
        **env: str,
    ):
        env = {
            "PATH": repo.env["PATH"],
            "FAKE_CODEX_CASE": case,
            "FAKE_CODEX_LOG": str(repo.tmp / "codex-calls.jsonl"),
            **env,
        }
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        if session:
            return codex.resume(session, prompt, repo.path, run_dir)
        return codex.start(prompt, repo.path, run_dir)

    return run


def last_call(repo) -> dict:
    log = repo.tmp / "codex-calls.jsonl"
    return json.loads(log.read_text().splitlines()[-1])


def test_start_sends_stdin_read_only_and_returns_session(repo, run_dir, call):
    assert call(prompt="Line 1\nLine 2\n") == (SESSION, RESPONSE)
    logged = last_call(repo)
    assert logged["argv"] == [
        "exec",
        "-s",
        "read-only",
        "-c",
        'approval_policy="never"',
        "--json",
        "-o",
        str(run_dir / "response.md"),
        "-",
    ]
    assert logged["stdin"] == "Line 1\nLine 2\n"
    assert Path(logged["cwd"]) == repo.path


def test_resume_keeps_read_only_and_the_explicit_session(repo, run_dir, call):
    assert call(session=SESSION) == RESPONSE
    logged = last_call(repo)
    assert logged["argv"] == [
        "exec",
        "resume",
        SESSION,
        "-c",
        'sandbox_mode="read-only"',
        "-c",
        'approval_policy="never"',
        "--json",
        "-o",
        str(run_dir / "response.md"),
        "-",
    ]
    assert Path(logged["cwd"]) == repo.path


def test_wrong_session_is_refused_as_uncertain(call):
    with pytest.raises(codex.CallUncertain, match="different session"):
        call("wrong_thread", session=SESSION)


@pytest.mark.parametrize(
    "case, message",
    [
        ("no_thread", "0 thread.started"),
        ("bad_uuid", "valid session UUID"),
        ("dup_thread", "2 thread.started"),
        ("dup_completed", "2 turn.completed"),
        ("incomplete", "0 turn.completed"),
        ("nonjson", "isn't JSON"),
        ("empty", "empty response"),
    ],
)
def test_a_missing_or_unreadable_result_is_uncertain(
    run_dir, call, case, message
):
    with pytest.raises(codex.CallUncertain, match=message):
        call(case)
    assert (run_dir / "events.jsonl").exists()


def test_a_turn_codex_reports_as_failed_is_failed(call):
    with pytest.raises(codex.CallFailed, match="failed turn: quota exceeded"):
        call("turn_failed")


def test_error_event_followed_by_completion_is_accepted(call):
    assert call("nonfatal_error") == (SESSION, RESPONSE)


def test_nonzero_exit_before_the_turn_is_failed_with_the_stderr_tail(call):
    with pytest.raises(codex.CallFailed) as caught:
        call("exit")
    lines = str(caught.value).splitlines()
    assert "Codex exited with code 7. Last lines of stderr:" in lines
    assert lines[-20:-18] == ["stderr line 7", "stderr line 8"]
    assert lines[-1] == "Authentication failed"
    assert "stderr line 6" not in lines


def test_nonzero_exit_after_the_turn_started_is_uncertain(call):
    with pytest.raises(codex.CallUncertain, match="started the turn"):
        call("exit_after_start")


def test_a_refused_second_writer_is_failed(call):
    with pytest.raises(codex.CallFailed, match="another Codex process"):
        call("active_writer", session=SESSION)


def test_timeout_kills_the_process_group(
    repo, call, monkeypatch, process_gone
):
    pids = repo.tmp / "pids"
    monkeypatch.setattr(codex, "TIMEOUT_SECONDS", 1)
    with pytest.raises(codex.CallUncertain, match="timed out"):
        call("timeout", FAKE_CODEX_PIDS=str(pids))
    for pid in map(int, pids.read_text().split()):
        assert process_gone(pid), f"process {pid} survived"


def test_progress_lines_show_only_time_and_commands_run(
    call, monkeypatch, capsys
):
    monkeypatch.setattr(codex, "PROGRESS_SECONDS", 0.25)
    assert call("slow") == (SESSION, RESPONSE)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) >= 2, lines
    for line in lines:
        assert re.search(
            r"^xreview: Codex still working — 0m \ds, "
            r"(0 commands|1 command) run$",
            line,
        ), line
    assert lines[-1].endswith(", 1 command run")


def test_codex_missing_is_failed(repo, call):
    with pytest.raises(codex.CallFailed, match="not found"):
        call(PATH=str(repo.tools))


def test_codex_that_cant_be_started_is_failed(repo, call):
    broken = repo.tmp / "broken-bin"
    broken.mkdir()
    (broken / "codex").write_text("#!/nonexistent/interpreter\n")
    (broken / "codex").chmod(0o755)
    with pytest.raises(codex.CallFailed, match="couldn't be started"):
        call(PATH=f"{broken}:{repo.tools}")
