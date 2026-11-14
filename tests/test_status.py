"""Tests for xreview status: one screen, from prepared to a stop."""

from __future__ import annotations

import json

from test_runner import SESSION, Review, with_decisions

from xreview.providers import claude

SCREEN = 16  # lines: "about 15"


def status(review) -> list[str]:
    result = review.xreview("status", review.name)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) <= SCREEN, lines
    return lines


def test_a_prepared_review_isnt_started(repo):
    review = Review(repo)
    assert status(review)[0] == f"Review {review.name}: prepared, not started."


def test_while_a_step_runs_it_shows_the_step_and_the_reviewers_progress(
    repo,
):
    review = Review(repo)
    runner = review.background(
        FAKE_CODEX_CASE="commands",
        FAKE_CODEX_SLEEPS=json.dumps({"codex-response": 3}),
    )
    review.wait_for_reviewer("run:codex-response")
    lines = status(review)
    runner.wait(timeout=30)
    assert lines[0].startswith(
        f"Review {review.name}: working on Step 4 — Codex response, "
    )
    assert lines[0].endswith(" so far.")
    assert lines[1] == "Codex: 0 commands run. Last: none yet"
    assert "  Step 2 — Codex review, 0s" in lines
    assert not any("claude --resume" in line for line in lines)


def test_at_a_stop_it_shows_decisions_refusals_and_how_to_open_each(repo):
    review = Review(repo)
    env = with_decisions(review)
    cases = {"FAKE_CODEX_CASE": "commands"}
    claude_cases = {"FAKE_CLAUDE_CASES": json.dumps({"plan": "refused_tools"})}
    assert review.run(**env, **cases, **claude_cases).returncode == 0
    lines = status(review)
    assert (
        lines[0] == f"Review {review.name}: at a stop, with 2 open decisions."
    )
    assert lines[1] == "Finished (latest 5 of 9):"
    assert lines[2].startswith("  Step 5 — Claude final plan, ")
    assert lines[6].startswith("  Decision round 1 — Claude merged options, ")
    assert (
        "Permission refusals: Claude 2, Codex 3. Other failed commands: "
        "Claude 1, Codex 3." in lines
    )
    settings = review.path / "session-b-settings.json"
    session = review.run_state()["claude_session"]
    assert lines[-2] == (
        f"  CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 claude --resume {session} "
        f"--settings '{settings}' --tools {claude.TOOLS} --allowedTools "
        f"{claude.ALLOWED_TOOLS} --permission-mode auto --strict-mcp-config "
        "--setting-sources= --model fake-default-model"
    )
    assert (
        "Session B runs on fake-default-model, the effort your Claude Code "
        "settings set." in lines
    )
    assert lines[-1] == f"  codex resume {SESSION} -s read-only"
    sandbox = json.loads(settings.read_text())["sandbox"]
    assert sandbox["filesystem"]["denyWrite"] == [str(repo.path)]
    assert sandbox["allowUnsandboxedCommands"] is False


def test_an_uncertain_step_says_what_to_say(repo):
    review = Review(repo)
    cases = json.dumps({"plan": "error_after_work"})
    assert review.run(FAKE_CLAUDE_CASES=cases).returncode == 4
    lines = status(review)
    assert lines[0] == (
        f'Review {review.name}: stopped before finishing. Say "continue" to '
        "pick it up."
    )
    assert (
        'Uncertain: Step 5 — Claude final plan may have run. Say "retry" to '
        "send it again." in lines
    )
    assert not any("--resume" in line for line in lines)


def test_a_failed_step_says_what_to_say(repo):
    review = Review(repo)
    cases = json.dumps({"plan": "startup_fail"})
    assert review.run(FAKE_CLAUDE_CASES=cases).returncode == 3
    assert (
        'Failed: Step 5 — Claude final plan. Say "retry" to try it again.'
        in status(review)
    )
