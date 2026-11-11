"""Tests for xreview run, through bin/xreview against real temporary git
repos with the fake codex and the fake claude. There are no model calls.

They cover the order of the calls, one Session B throughout, and what a
restart does with a step an earlier run left pending: it uses an answer
that's on disk, waits for a reviewer that's still running, and otherwise
stops as uncertain until an explicit retry.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import signal
import subprocess
import time

import pytest

from xreview import cli

ROOT = Path(__file__).resolve().parents[1]
XREVIEW = ROOT / "bin" / "xreview"
SESSION = "01a11457-66e5-73e3-8c89-991f3b54c198"  # the fake codex's thread
ANSWERS = {
    "claude": {"rebuttal": "FAKE-REBUTTAL", "plan": "FAKE-PLAN"},
    "codex": {
        "codex-review": "FAKE-CODEX-REVIEW",
        "codex-response": "FAKE-CODEX-RESPONSE",
    },
}
HEADINGS = [
    "Reviewer prompt — sent to Codex",
    "Step 2 — Codex review",
    "Step 3 — Claude rebuttal — sent to Codex",
    "Step 4 — Codex response",
    "Step 5 — Claude final plan",
]


class Review:
    """A prepared review with a valid prompt and handoff, and helpers to
    run xreview on it against the fakes."""

    def __init__(self, repo):
        self.repo = repo
        self.path = repo.prepare()
        self.name = self.path.name
        repo.write_prompt(self.path, cli.simplification_block())
        (self.path / "claude-handoff.md").write_text("Handoff.\n")
        self.codex_log = repo.tmp / "codex-calls.jsonl"
        self.claude_log = repo.tmp / "claude-calls.jsonl"

    def env(self, **extra: str) -> dict[str, str]:
        return {
            **self.repo.env,
            "FAKE_CODEX_LOG": str(self.codex_log),
            "FAKE_CLAUDE_LOG": str(self.claude_log),
            "FAKE_CLAUDE_ANSWERS": json.dumps(ANSWERS["claude"]),
            "FAKE_CODEX_ANSWERS": json.dumps(ANSWERS["codex"]),
            **extra,
        }

    def xreview(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(XREVIEW), *args],
            cwd=self.repo.path,
            env=self.env(**env),
            capture_output=True,
            text=True,
        )

    def run(self, *flags: str, **env: str) -> subprocess.CompletedProcess:
        return self.xreview("run", self.name, *flags, **env)

    def background(self, *flags: str, **env: str) -> subprocess.Popen:
        return subprocess.Popen(
            [str(XREVIEW), "run", self.name, *flags],
            cwd=self.repo.path,
            env=self.env(**env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def calls(self, who: str) -> list[dict]:
        log = self.claude_log if who == "claude" else self.codex_log
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def counts(self) -> tuple[int, int]:
        return len(self.calls("claude")), len(self.calls("codex"))

    def transcript(self) -> str:
        return (self.path / "transcript.md").read_text()

    def headings(self) -> list[str]:
        return re.findall(r"^## (.+)$", self.transcript(), re.M)

    def run_state(self) -> dict:
        return json.loads((self.path / "run-state.json").read_text())

    def step(self, key: str) -> dict:
        return self.run_state()["steps"].get(key, {})

    def wait_for_reviewer(self, key: str, timeout: float = 15) -> int:
        """The process ID of the reviewer working on step key, once it's
        recorded."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pid = self.step(key).get("pid")
            except (FileNotFoundError, ValueError):
                pid = None
            if pid:
                return pid
            time.sleep(0.05)
        raise AssertionError(f"no reviewer started for {key}")


@pytest.fixture
def review(repo) -> Review:
    return Review(repo)


def kill(pid: int) -> None:
    try:
        import os

        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def resend_note() -> str:
    return (ROOT / "templates/runner/resend-note.md").read_text().rstrip("\n")


# --- the normal path ---------------------------------------------------------


def test_a_run_reaches_claudes_plan_in_order(review):
    result = review.run()
    assert result.returncode == 0, result.stderr
    setup, rebuttal, plan = review.calls("claude")
    assert len(review.calls("codex")) == 2
    assert "--no-session-persistence" in setup["argv"]
    assert rebuttal["new"] and not plan["new"]
    assert rebuttal["session"] == plan["session"]
    assert all(
        call["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        for call in review.calls("claude")
    )
    assert review.headings() == HEADINGS
    assert "FAKE-PLAN" in (review.path / "report.md").read_text()
    report = review.path / "report.md"
    assert result.stdout == f"The review is at a stop. Report: {report}\n"
    assert review.run_state()["stopped"] is True


def test_each_message_carries_what_its_step_needs(review):
    review.run()
    state = json.loads((review.path / "state.json").read_text())
    _, rebuttal, plan = review.calls("claude")
    header = (ROOT / "templates/runner/session-b-header.md").read_text()
    assert rebuttal["prompt"].startswith(header.rstrip("\n") + "\n\n")
    assert (
        f"First read `.reviews/{review.name}/claude-handoff.md` and "
        "everything it lists. Then do this step:" in rebuttal["prompt"]
    )
    step3 = cli.fill(cli.read_template("code/step3-claude-rebuttal"), state)
    assert step3.rstrip("\n") + "\n\nFAKE-CODEX-REVIEW\n" in rebuttal["prompt"]
    step5 = cli.fill(cli.read_template("code/step5-claude-plan"), state)
    assert plan["prompt"] == step5.rstrip("\n") + "\n\nFAKE-CODEX-RESPONSE\n"
    first, second = review.calls("codex")
    prompt = (review.path / "reviewer-prompt.md").read_text()
    assert first["stdin"] == prompt
    assert second["stdin"] == cli.compose(
        ["code/step4-reviewer-response"], state, "FAKE-REBUTTAL"
    )
    assert second["argv"][:3] == ["exec", "resume", SESSION]


def test_run_at_a_stop_reports_it_and_sends_nothing(review):
    review.run()
    before = review.counts()
    result = review.run()
    assert result.returncode == 0, result.stderr
    assert "at a stop" in result.stdout
    assert review.counts() == before


# --- a crash at each point ---------------------------------------------------


@pytest.mark.parametrize("key", ["run:rebuttal", "run:plan"])
def test_a_crash_before_a_claude_call_is_uncertain_and_nothing_is_resent(
    review, key
):
    assert review.run(XREVIEW_TEST_CRASH=f"{key}:before").returncode == 137
    before = review.counts()
    result = review.run()
    assert result.returncode == 4, result.stderr
    assert "may have run" in result.stderr
    assert "--retry-uncertain" in result.stderr
    assert review.counts() == before
    result = review.run("--retry-uncertain")
    assert result.returncode == 0, result.stderr
    calls = review.calls("claude")
    resent = calls[1] if key == "run:rebuttal" else calls[2]
    assert resent["prompt"].startswith(resend_note() + "\n\n")
    # The plan resumes the rebuttal's session, resent or not.
    assert calls[2]["session"] == calls[1]["session"]
    assert review.headings() == HEADINGS


@pytest.mark.parametrize("point", ["after", "saved"])
def test_a_claude_answer_on_disk_after_a_crash_is_used(review, point):
    result = review.run(XREVIEW_TEST_CRASH=f"run:plan:{point}")
    assert result.returncode == 137
    before = review.counts()
    result = review.run()
    assert result.returncode == 0, result.stderr
    assert review.counts() == before
    assert review.headings() == HEADINGS
    assert "FAKE-PLAN" in (review.path / "report.md").read_text()


@pytest.mark.parametrize("key", ["run:codex-review", "run:codex-response"])
def test_a_codex_answer_on_disk_after_a_crash_is_used(review, key):
    assert review.run(XREVIEW_TEST_CRASH=f"{key}:after").returncode == 137
    result = review.run()
    assert result.returncode == 0, result.stderr
    # Each Codex step was sent once: the crashed one isn't repeated.
    assert len(review.calls("codex")) == 2
    assert review.headings() == HEADINGS
    state = json.loads((review.path / "state.json").read_text())
    assert state["reviewer_session_id"] == SESSION


# --- a reviewer that outlives its runner -------------------------------------


@pytest.mark.parametrize(
    "key, sleeps",
    [
        ("run:plan", {"FAKE_CLAUDE_SLEEPS": json.dumps({"plan": 4})}),
        (
            "run:codex-response",
            {"FAKE_CODEX_SLEEPS": json.dumps({"codex-response": 4})},
        ),
    ],
)
def test_a_reviewer_still_running_after_its_runner_died_is_waited_for(
    review, key, sleeps
):
    runner = review.background(**sleeps)
    review.wait_for_reviewer(key)
    kill(runner.pid)
    runner.wait()
    result = review.run(**sleeps)
    assert result.returncode == 0, result.stderr
    assert "from an earlier run; waiting for it to finish" in result.stderr
    # Setup check, rebuttal and plan; review and response: none resent.
    assert review.counts() == (3, 2)
    assert review.headings() == HEADINGS


@pytest.mark.parametrize(
    "key, sleeps",
    [
        ("run:plan", {"FAKE_CLAUDE_SLEEPS": json.dumps({"plan": 30})}),
        (
            "run:codex-response",
            {"FAKE_CODEX_SLEEPS": json.dumps({"codex-response": 30})},
        ),
    ],
)
def test_a_reviewer_killed_with_its_runner_leaves_the_step_uncertain(
    review, key, sleeps, process_gone
):
    runner = review.background(**sleeps)
    reviewer = review.wait_for_reviewer(key)
    kill(runner.pid)
    kill(reviewer)
    runner.wait()
    assert process_gone(reviewer)
    before = review.counts()
    result = review.run()
    assert result.returncode == 4, result.stderr
    assert review.counts() == before
    result = review.run("--retry-uncertain")
    assert result.returncode == 0, result.stderr
    assert review.headings()[-1] == "Step 5 — Claude final plan"


def test_a_stop_signal_kills_the_reviewer_and_leaves_the_step_uncertain(
    review, process_gone
):
    sleeps = {"FAKE_CODEX_SLEEPS": json.dumps({"codex-response": 30})}
    runner = review.background(**sleeps)
    reviewer = review.wait_for_reviewer("run:codex-response")
    runner.send_signal(signal.SIGTERM)
    _, stderr = runner.communicate(timeout=15)
    assert runner.returncode == 4, stderr
    assert "Traceback" not in stderr
    assert process_gone(reviewer)
    assert not (review.path / "run.lock").exists()
    assert "## Step 4 — Codex response (UNCERTAIN)" in review.transcript()
    assert review.run().returncode == 4
    result = review.run("--retry-uncertain")
    assert result.returncode == 0, result.stderr


# --- failed, uncertain and refused calls -------------------------------------


def test_a_failed_call_is_retried_by_a_plain_rerun(review):
    cases = json.dumps({"plan": "startup_fail"})
    result = review.run(FAKE_CLAUDE_CASES=cases)
    assert result.returncode == 3, result.stderr
    assert "## Step 5 — Claude final plan (FAILED)" in review.transcript()
    result = review.run()
    assert result.returncode == 0, result.stderr
    assert review.headings()[-1] == "Step 5 — Claude final plan"


def test_an_uncertain_call_is_resent_only_on_request(review):
    cases = json.dumps({"plan": "error_after_work"})
    result = review.run(FAKE_CLAUDE_CASES=cases)
    assert result.returncode == 4, result.stderr
    assert "## Step 5 — Claude final plan (UNCERTAIN)" in review.transcript()
    before = review.counts()
    assert review.run().returncode == 4
    assert review.counts() == before
    result = review.run("--retry-uncertain")
    assert result.returncode == 0, result.stderr
    assert review.calls("claude")[-1]["prompt"].startswith(resend_note())


def test_a_resent_first_turn_starts_a_new_session(review):
    cases = json.dumps({"rebuttal": "error_after_work"})
    assert review.run(FAKE_CLAUDE_CASES=cases).returncode == 4
    result = review.run("--retry-uncertain")
    assert result.returncode == 0, result.stderr
    _, first, resent, plan = review.calls("claude")
    assert first["new"] and resent["new"]
    assert resent["session"] != first["session"]
    assert plan["session"] == resent["session"] and not plan["new"]


def test_a_failed_setup_check_stops_before_any_codex_call(review):
    cases = json.dumps({"setup-check": "mode_default"})
    result = review.run(FAKE_CLAUDE_CASES=cases)
    assert result.returncode == 3, result.stderr
    assert "Auto mode isn't available" in result.stderr
    assert review.calls("codex") == []
    result = review.run()
    assert result.returncode == 0, result.stderr


# --- one command at a time ---------------------------------------------------


def test_a_second_command_is_refused_while_one_runs(review):
    sleeps = json.dumps({"codex-review": 3})
    runner = review.background(FAKE_CODEX_SLEEPS=sleeps)
    review.wait_for_reviewer("run:codex-review")
    result = review.run()
    assert result.returncode == 1
    assert "another xreview command" in result.stderr
    assert runner.wait(timeout=30) == 0


def test_a_lock_left_by_a_dead_command_is_cleared(review):
    (review.path / "run.lock").write_text(
        json.dumps({"pid": 999999, "started": "Thu Jan  1 00:00:00 1970"})
    )
    result = review.run()
    assert result.returncode == 0, result.stderr


# --- the tree check ----------------------------------------------------------


def test_a_tracked_edit_during_a_claude_call_rejects_its_answer(repo, review):
    cases = json.dumps({"plan": "edit_tracked"})
    result = review.run(FAKE_CLAUDE_CASES=cases)
    assert result.returncode == 3, result.stderr
    assert "Restore the tree before any retry." in result.stderr
    assert "## Step 5 — Claude final plan (FAILED)" in review.transcript()
    repo.git("checkout", "--", "a.txt")
    result = review.run()
    assert result.returncode == 0, result.stderr


def test_uncommitted_changes_stop_the_run_before_any_call(repo, review):
    (repo.path / "a.txt").write_text("edited\n")
    result = review.run()
    assert result.returncode == 1
    assert "stash them to keep this review" in result.stderr
    assert review.counts() == (0, 0)


def test_a_bad_reviewer_prompt_stops_the_run_before_any_call(review):
    (review.path / "reviewer-prompt.md").write_text("Review it.\n")
    result = review.run()
    assert result.returncode == 1
    assert "Simplification block" in result.stderr
    assert review.counts() == (0, 0)
