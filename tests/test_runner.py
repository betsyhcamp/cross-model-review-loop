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
    "claude": {
        "rebuttal": "FAKE-REBUTTAL",
        "plan": "FAKE-PLAN",
        "decision-copy": "NONE",
    },
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
        """The transcript's own section headings: each is followed by its
        italic timestamp line, which a recorded reply's headings aren't."""
        return re.findall(r"^## (.+)\n_[^\n]*_$", self.transcript(), re.M)

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
    setup, rebuttal, plan, copy = review.calls("claude")
    assert len(review.calls("codex")) == 2
    assert "--no-session-persistence" in setup["argv"]
    assert rebuttal["new"] and not plan["new"] and not copy["new"]
    assert rebuttal["session"] == plan["session"] == copy["session"]
    assert all(
        call["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        for call in review.calls("claude")
    )
    assert review.headings() == HEADINGS
    assert "FAKE-PLAN" in (review.path / "report.md").read_text()
    report = review.path / "report.md"
    assert result.stdout == (
        f"The review is at a stop, with no open decisions. Report: {report}\n"
    )
    assert review.run_state()["stopped"] is True


def test_each_message_carries_what_its_step_needs(review):
    review.run()
    state = json.loads((review.path / "state.json").read_text())
    _, rebuttal, plan, _ = review.calls("claude")
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


def test_session_b_gets_the_repos_own_instruction_files(repo):
    repo.commit("CLAUDE.md", "Use tabs.\n```sh\nmake test\n```\n", "Add rules")
    (repo.path / "docs").mkdir()
    head = repo.commit("docs/AGENTS.md", "DOCS-RULES\n", "Add docs rules")
    (repo.path / "CLAUDE.local.md").write_text("PRIVATE-NOTE\n")
    review = Review(repo)
    repo.write_prompt(
        review.path, cli.simplification_block(), (repo.base, head)
    )
    result = review.run()
    assert result.returncode == 0, result.stderr
    rebuttal = review.calls("claude")[1]["prompt"]
    template = (ROOT / "templates/runner/repo-instructions.md").read_text()
    intro = template.split("{files}")[0].rstrip("\n")
    shown = "`CLAUDE.md`:\n\n````\nUse tabs.\n```sh\nmake test\n```\n````"
    assert intro + "\n\n" + shown + "\n\n" in rebuttal
    assert rebuttal.index("Then do this step:") < rebuttal.index(intro)
    state = json.loads((review.path / "state.json").read_text())
    step3 = cli.fill(cli.read_template("code/step3-claude-rebuttal"), state)
    step3 = step3.splitlines()[0]
    assert rebuttal.index(shown) < rebuttal.index(step3)
    assert "DOCS-RULES" not in rebuttal and "PRIVATE-NOTE" not in rebuttal


def test_without_instruction_files_the_first_message_is_unchanged(review):
    review.run()
    rebuttal = review.calls("claude")[1]["prompt"]
    assert "instruction files" not in rebuttal


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
    claude_before, codex_before = review.counts()
    result = review.run()
    assert result.returncode == 0, result.stderr
    # The plan isn't asked again; only the decision copy follows it.
    assert review.counts() == (claude_before + 1, codex_before)
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
    # Setup check, rebuttal, plan and copy; review and response: none resent.
    assert review.counts() == (4, 2)
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
    resent_plan = review.calls("claude")[-2]
    assert resent_plan["prompt"].startswith(resend_note())


def test_a_resent_first_turn_starts_a_new_session(review):
    cases = json.dumps({"rebuttal": "error_after_work"})
    assert review.run(FAKE_CLAUDE_CASES=cases).returncode == 4
    result = review.run("--retry-uncertain")
    assert result.returncode == 0, result.stderr
    _, first, resent, plan, _ = review.calls("claude")
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


# --- the open decisions and round 1 ----------------------------------------

COPY = (
    "### Decision 1: First\nFAKE-DESCRIPTION-ONE\n\n"
    "### Decision 2: Second\nFAKE-DESCRIPTION-TWO\n"
)
ROUND_HEADINGS = [
    "Decision round 1 — sent to Codex",
    "Decision round 1 — Codex ranking",
    "Decision round 1 — Claude ranking",
    "Decision round 1 — Claude merged options",
]


def with_decisions(review, **claude: str) -> dict[str, str]:
    """The env for a run whose plan has two open decisions. Codex keeps
    its sessions in CODEX_HOME, and every Claude call looks for readable
    copies of Codex's ranking."""
    home = review.repo.tmp / "codex-home"
    answers = {
        **ANSWERS["claude"],
        "decision-copy": COPY,
        "ranking": "FAKE-CLAUDE-RANKING",
        "merge": "@headings",
        "fix-up-r": "@headings",
        **claude,
    }
    roots = [
        str(review.path),
        str(home),
        review.repo.env["CLAUDE_CONFIG_DIR"],
        review.repo.env["CLAUDE_CODE_TMPDIR"],
    ]
    return {
        "CODEX_HOME": str(home),
        "FAKE_CLAUDE_ANSWERS": json.dumps(answers),
        "FAKE_CODEX_ANSWERS": json.dumps(
            {**ANSWERS["codex"], "codex-ranking": "FAKE-CODEX-RANKING"}
        ),
        "FAKE_LEAK_MARK": "FAKE-CODEX-RANKING",
        "FAKE_LEAK_ROOTS": json.dumps(roots),
    }


def claude_call(review, step: str) -> dict:
    """The last Claude call whose run folder's name holds step."""
    calls = [c for c in review.calls("claude") if step in c["argv"][-1]]
    return calls[-1]


def test_round_one_has_codex_rank_first_then_claude_blind_then_merge(review):
    result = review.run(**with_decisions(review))
    assert result.returncode == 0, result.stderr
    assert review.headings() == HEADINGS + ROUND_HEADINGS
    assert len(review.calls("codex")) == 3
    sent = review.calls("codex")[-1]["stdin"]
    state = json.loads((review.path / "state.json").read_text())
    assert sent == cli.compose(
        ["shared/decision-ranking", "shared/reviewer-guard"],
        state,
        COPY.rstrip("\n"),
    )
    assert "FAKE-CLAUDE-RANKING" not in sent
    ranking = claude_call(review, "ranking-r1")
    assert ranking["prompt"].endswith(COPY)
    assert "Rank these on your own." in ranking["prompt"]
    merge = claude_call(review, "merge-r1")
    assert "FAKE-CODEX-RANKING" in merge["prompt"]
    assert merge["prompt"].endswith(
        "This round covers these decisions:\n\n"
        "### Decision 1: First\n### Decision 2: Second\n"
    )
    assert "with 2 open decisions" in result.stdout


def test_claudes_ranking_turn_can_read_no_copy_of_codexs_ranking(review):
    assert review.run(**with_decisions(review)).returncode == 0
    home = (review.repo.tmp / "codex-home").resolve()
    ranking = claude_call(review, "ranking-r1")
    assert ranking["leaks"] == {"readable": [], "in_prompt": False}
    denied = ranking["settings"]["sandbox"]["filesystem"]["denyRead"]
    for path in (
        review.path / "runs",
        review.path / "transcript.md",
        home,
    ):
        assert str(path.resolve()) in denied
    rules = ranking["settings"]["permissions"]["deny"]
    assert f"Read(/{(review.path / 'runs').resolve()}/**)" in rules
    assert f"Read(/{(review.path / 'transcript.md').resolve()})" in rules
    assert f"Read(/{home}/**)" in rules
    # The copies exist; the turn just can't read them. The next turn can.
    assert (home / "sessions/rollout.jsonl").is_file()
    merge = claude_call(review, "merge-r1")
    assert merge["leaks"]["readable"] and merge["leaks"]["in_prompt"]
    later = merge["settings"]["sandbox"]["filesystem"]["denyRead"]
    assert str((review.path / "runs").resolve()) not in later


def test_the_report_keeps_the_plan_and_each_decisions_latest_options(
    review,
):
    assert review.run(**with_decisions(review)).returncode == 0
    report = (review.path / "report.md").read_text()
    assert report.startswith(f"# Review report — {review.name}\n\nFAKE-PLAN")
    assert "## Latest options for each open decision" in report
    for n, title in ((1, "First"), (2, "Second")):
        assert (
            f"### Decision {n}: {title}\n_From decision round 1_\n"
            f"FAKE-MERGED-{n}"
        ) in report
    state = review.run_state()["decisions"]
    assert {k: v["title"] for k, v in state.items()} == {
        "1": "First",
        "2": "Second",
    }


def test_no_open_decisions_skips_the_ranking_round(review):
    assert review.run().returncode == 0
    assert review.headings() == HEADINGS
    assert len(review.calls("codex")) == 2
    report = (review.path / "report.md").read_text()
    assert "Latest options" not in report


def test_a_merge_missing_a_decision_is_fixed_once(review):
    env = with_decisions(review, merge="@headings-but-last")
    result = review.run(**env)
    assert result.returncode == 0, result.stderr
    fix = claude_call(review, "fix-up-r1")
    assert "Missing: Decision 2." in fix["prompt"]
    assert "### Decision 1: First\n### Decision 2: Second" in fix["prompt"]
    report = (review.path / "report.md").read_text()
    assert "FAKE-MERGED-2" in report
    assert review.headings()[-1] == "Decision round 1 — Claude merged options"


def test_a_merge_still_missing_a_decision_stops_without_asking_codex_again(
    review,
):
    env = with_decisions(
        review,
        merge="@headings-but-last",
        **{"fix-up-r": "@headings-but-last"},
    )
    result = review.run(**env)
    assert result.returncode == 1
    assert "still doesn't have one `### Decision` heading" in result.stderr
    assert "Missing: Decision 2." in result.stderr
    assert (
        "Decision round 1 — Claude merged options (headings incomplete)"
        in review.headings()
    )
    codex_calls = len(review.calls("codex"))
    result = review.run(**with_decisions(review))
    assert result.returncode == 0, result.stderr
    assert len(review.calls("codex")) == codex_calls
    assert "FAKE-MERGED-2" in (review.path / "report.md").read_text()


def test_a_decision_copy_with_bad_numbers_is_renumbered(review):
    env = with_decisions(
        review,
        **{
            "decision-copy": "### Decision 1: A\nx\n\n### Decision 3: B\ny\n",
            "fix-up-decision": "### Decision 1: A\nx\n\n### Decision 2: B\ny",
        },
    )
    result = review.run(**env)
    assert result.returncode == 0, result.stderr
    fix = claude_call(review, "fix-up-decision")
    assert "### Decision 1: A\n### Decision 2: B" in fix["prompt"]
    assert (
        "Missing: Decision 2. Not in this round: Decision 3."
        in (fix["prompt"])
    )
    titles = {
        k: v["title"] for k, v in review.run_state()["decisions"].items()
    }
    assert titles == {"1": "A", "2": "B"}


def test_a_crash_before_claudes_ranking_keeps_it_blind_when_resent(review):
    env = with_decisions(review)
    crash = {"XREVIEW_TEST_CRASH": "round-1:claude-ranking:before"}
    assert review.run(**env, **crash).returncode == 137
    assert review.run(**env).returncode == 4
    result = review.run("--retry-uncertain", **env)
    assert result.returncode == 0, result.stderr
    ranking = claude_call(review, "ranking-r1")
    assert ranking["prompt"].startswith(resend_note())
    assert ranking["leaks"] == {"readable": [], "in_prompt": False}
    assert len(review.calls("codex")) == 3


# --- operations at a stop --------------------------------------------------

WORDS = (
    'Re decision #2 -- is `retry_after=0` ok?? I lean "no".\n'
    "Also: teh comment at sync.py:47 is wrong."
)


def ask(review, kind, n, words=WORDS, *flags, **env):
    """Write the words to inbox/<n>.txt and run kind on them."""
    inbox = review.path / "inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / f"{n}.txt").write_text(words)
    return review.xreview(
        kind, review.name, "--file", f"inbox/{n}.txt", *flags, **env
    )


@pytest.fixture
def stopped(review):
    """A review at its first stop, with two open decisions."""
    env = with_decisions(review)
    assert review.run(**env).returncode == 0
    review.decisions_env = env
    return review


def test_ask_claude_carries_the_words_unchanged_to_the_same_session(
    stopped,
):
    result = ask(stopped, "ask-claude", 1, **stopped.decisions_env)
    assert result.returncode == 0, result.stderr
    question = stopped.calls("claude")[-1]
    assert question["prompt"] == WORDS
    rebuttal = stopped.calls("claude")[1]
    assert question["session"] == rebuttal["session"] and not question["new"]
    headings = stopped.headings()
    assert headings[-2:] == [
        "Question 1 for Claude — your words",
        "Question 1 for Claude — answer",
    ]
    assert "## Question 1 for Claude — your words\n_" in stopped.transcript()
    assert WORDS in stopped.transcript()
    answer = stopped.path / "messages/ask-claude-1.md"
    assert result.stdout == (
        "Claude's answer to question 1:\n\nFAKE-CLAUDE-ANSWER\n\n"
        f"Saved in: {answer}\n"
    )


def test_a_long_answer_is_left_in_its_file(stopped):
    env = with_decisions(stopped, **{"question-1": "x" * 2500})
    result = ask(stopped, "ask-claude", 1, **env)
    assert result.returncode == 0, result.stderr
    answer = stopped.path / "messages/ask-claude-1.md"
    assert result.stdout == (
        "Claude's answer to question 1 is 2,500 characters, so it's in a "
        f"file: {answer}\n"
    )


def test_ask_codex_sends_every_decisions_latest_options_and_the_words(
    stopped,
):
    result = ask(stopped, "ask-codex", 1, **stopped.decisions_env)
    assert result.returncode == 0, result.stderr
    state = json.loads((stopped.path / "state.json").read_text())
    body = (
        "### Decision 1: First\nFAKE-MERGED-1\n\n"
        "### Decision 2: Second\nFAKE-MERGED-2\n\n"
        f"My question:\n\n{WORDS}"
    )
    sent = stopped.calls("codex")[-1]["stdin"]
    assert sent == cli.compose(
        ["shared/decision-followup-reviewer"], state, body
    )
    assert stopped.headings()[-4:] == [
        "Question 1 for Codex — your words",
        "Decision round 2 — sent to Codex",
        "Decision round 2 — Codex response",
        "Decision round 2 — Claude merged options",
    ]
    merge = claude_call(stopped, "merge-r2")
    assert (
        "Your own view is your latest Part 4 for these decisions."
        in (merge["prompt"])
    )
    assert "Claude merged Codex's reply into decisions 1 and 2" in (
        result.stdout
    )
    assert result.stdout.startswith(
        "Codex's answer to question 1:\n\nFAKE-CODEX-RESPONSE\n"
    )
    report = (stopped.path / "report.md").read_text()
    assert report.count("_From decision round 2_") == 2


def test_ask_codex_with_numbers_covers_only_those_decisions(stopped):
    result = ask(
        stopped,
        "ask-codex",
        1,
        WORDS,
        "--decisions",
        "2",
        **stopped.decisions_env,
    )
    assert result.returncode == 0, result.stderr
    sent = stopped.calls("codex")[-1]["stdin"]
    assert "### Decision 2: Second" in sent
    assert "### Decision 1: First" not in sent
    merge = claude_call(stopped, "merge-r2")
    assert merge["prompt"].endswith(
        "This round covers these decisions:\n\n### Decision 2: Second\n"
    )
    report = (stopped.path / "report.md").read_text()
    assert "### Decision 1: First\n_From decision round 1_" in report
    assert "### Decision 2: Second\n_From decision round 2_" in report
    assert "into decision 2 (decision round 2)" in result.stdout


def test_a_decision_that_doesnt_exist_is_refused_with_a_pointer_to_rank(
    stopped,
):
    before = stopped.counts()
    result = ask(stopped, "ask-codex", 1, WORDS, "--decisions", "2,5")
    assert result.returncode == 1
    assert "There's no decision 5. The open decisions are 1, 2." in (
        result.stderr
    )
    assert "use rank" in result.stderr
    assert stopped.counts() == before
    assert stopped.run_state()["counters"].get("ask-codex") is None


def test_ask_codex_with_no_open_decisions_sends_the_words_alone(review):
    assert review.run().returncode == 0
    result = ask(review, "ask-codex", 1)
    assert result.returncode == 0, result.stderr
    state = json.loads((review.path / "state.json").read_text())
    assert review.calls("codex")[-1]["stdin"] == cli.compose(
        ["shared/reviewer-question"], state, WORDS
    )
    assert review.headings()[-3:] == [
        "Question 1 for Codex — your words",
        "Question 1 for Codex — sent to Codex",
        "Question 1 for Codex — answer",
    ]
    assert len(review.calls("claude")) == 4  # no merge
    assert "Codex's answer to question 1:" in result.stdout


@pytest.mark.parametrize("where", ["messages", "absolute"])
def test_words_from_outside_the_inbox_are_refused(stopped, where):
    outside = stopped.path / "messages" / "x.md"
    outside.write_text("words\n")
    given = "messages/x.md" if where == "messages" else str(outside)
    before = stopped.counts()
    result = stopped.xreview("ask-claude", stopped.name, "--file", given)
    assert result.returncode == 1
    assert "--file must name a file in" in result.stderr
    assert stopped.counts() == before


def test_operations_before_the_first_stop_are_refused(review):
    result = ask(review, "ask-claude", 1)
    assert result.returncode == 1
    assert "hasn't reached its first stop" in result.stderr
    assert review.counts() == (0, 0)


def test_an_uncertain_question_is_resent_only_on_request(stopped):
    cases = {
        "FAKE_CLAUDE_CASES": json.dumps({"question-1": "error_after_work"})
    }
    env = {**stopped.decisions_env, **cases}
    assert ask(stopped, "ask-claude", 1, **env).returncode == 4
    before = stopped.counts()
    assert ask(stopped, "ask-claude", 1, **env).returncode == 4
    assert stopped.counts() == before
    result = ask(
        stopped,
        "ask-claude",
        1,
        WORDS,
        "--retry-uncertain",
        **stopped.decisions_env,
    )
    assert result.returncode == 0, result.stderr
    assert stopped.calls("claude")[-1]["prompt"] == (
        resend_note() + "\n\n" + WORDS + "\n"
    )


def test_moving_on_drops_an_uncertain_question_without_resending_it(
    stopped,
):
    cases = {
        "FAKE_CLAUDE_CASES": json.dumps({"question-1": "error_after_work"})
    }
    assert (
        ask(
            stopped, "ask-claude", 1, **stopped.decisions_env, **cases
        ).returncode
        == 4
    )
    result = ask(
        stopped,
        "ask-codex",
        2,
        "Another thing.",
        "--decisions",
        "1",
        **stopped.decisions_env,
    )
    assert result.returncode == 0, result.stderr
    assert "Question 1 for Claude was dropped." in result.stderr
    assert "Question 1 for Claude — dropped" in stopped.headings()
    questions = [
        c for c in stopped.calls("claude") if "question-1" in c["argv"][-1]
    ]
    assert len(questions) == 1
    assert stopped.run_state()["dropped"] == ["Question 1 for Claude"]


def test_a_question_whose_reviewer_outlived_its_runner_is_finished(stopped):
    sleeps = {"FAKE_CLAUDE_SLEEPS": json.dumps({"question-1": 4})}
    inbox = stopped.path / "inbox"
    inbox.mkdir()
    (inbox / "1.txt").write_text(WORDS)
    runner = subprocess.Popen(
        [str(XREVIEW), "ask-claude", stopped.name, "--file", "inbox/1.txt"],
        cwd=stopped.repo.path,
        env=stopped.env(**stopped.decisions_env, **sleeps),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stopped.wait_for_reviewer("ask-claude-1:claude")
    kill(runner.pid)
    runner.wait()
    result = ask(
        stopped,
        "ask-codex",
        2,
        "Next.",
        "--decisions",
        "1",
        **stopped.decisions_env,
    )
    assert result.returncode == 0, result.stderr
    assert "Question 1 for Claude finished after all" in result.stderr
    assert "Question 1 for Claude — answer" in stopped.headings()
    assert "Question 1 for Claude — dropped" not in stopped.headings()


def test_run_picks_up_a_pending_question(stopped):
    cases = {
        "FAKE_CLAUDE_CASES": json.dumps({"question-1": "error_after_work"})
    }
    assert (
        ask(
            stopped, "ask-claude", 1, **stopped.decisions_env, **cases
        ).returncode
        == 4
    )
    assert stopped.run(**stopped.decisions_env).returncode == 4
    result = stopped.run("--retry-uncertain", **stopped.decisions_env)
    assert result.returncode == 0, result.stderr
    assert "Claude's answer to question 1:" in result.stdout


def test_a_moved_head_ends_the_stops(repo, stopped):
    repo.commit("c.txt", "later\n", "Later commit")
    result = ask(stopped, "ask-claude", 1)
    assert result.returncode == 1
    assert "HEAD has moved since prepare" in result.stderr


def test_bad_decision_numbers_are_a_usage_error(stopped):
    result = ask(stopped, "ask-codex", 1, WORDS, "--decisions", "two")
    assert result.returncode == 2


# --- new decisions and Finalize ----------------------------------------------


def plan_with(groups: int, mark: str = "PLAN") -> str:
    items = "\n".join(
        f"### Group {i}: change {i}\n- Change: {mark} {i}"
        for i in range(1, groups + 1)
    )
    return (
        f"## User decisions\n1. First → option 1 — default accepted\n\n"
        f"## Implement now — one commit per group, in order\n{items}\n\n"
        "## Deferred\n- none\n\n## Dropped\n- none"
    )


def test_rank_describes_new_decisions_then_ranks_them_blind(stopped):
    env = with_decisions(
        stopped,
        **{"new-decision": "### Decision 3: Naming\nFAKE-NEW-DESCRIPTION"},
    )
    words = 'How should the field be named ("dead_letter" vs "undelivered")?'
    result = ask(stopped, "rank", 1, words, **env)
    assert result.returncode == 0, result.stderr
    describe = claude_call(stopped, "new-decision-r2")
    assert (
        "under the heading `### Decision 3: <title>`" in (describe["prompt"])
    )
    assert describe["prompt"].endswith(words + "\n")
    sent = stopped.calls("codex")[-1]["stdin"]
    assert sent.endswith(
        "### Decision 3: Naming\nFAKE-NEW-DESCRIPTION\n\n"
        f"My question:\n\n{words}\n"
    )
    assert "### Decision 1: First" not in sent
    ranking = claude_call(stopped, "ranking-r2")
    assert ranking["leaks"] == {"readable": [], "in_prompt": False}
    assert stopped.headings()[-5:] == [
        "Ranking request 1 — your words",
        "Decision round 2 — sent to Codex",
        "Decision round 2 — Codex ranking",
        "Decision round 2 — Claude ranking",
        "Decision round 2 — Claude merged options",
    ]
    report = (stopped.path / "report.md").read_text()
    assert "### Decision 1: First\n_From decision round 1_" in report
    assert "### Decision 3: Naming\n_From decision round 2_" in report
    assert "ranked new decision 3 (decision round 2)" in result.stdout


def test_rank_gives_several_new_decisions_the_next_numbers(stopped):
    env = with_decisions(
        stopped,
        **{
            "new-decision": "### Decision 1: A\nx\n\n### Decision 1: B\ny",
            "fix-up-new-decision": "### Decision 3: A\nx\n\n"
            "### Decision 4: B\ny",
        },
    )
    result = ask(stopped, "rank", 1, "Two new ones.", **env)
    assert result.returncode == 0, result.stderr
    fix = claude_call(stopped, "fix-up-new-decision")
    assert "### Decision 3: A\n### Decision 4: B" in fix["prompt"]
    titles = {
        k: v["title"] for k, v in stopped.run_state()["decisions"].items()
    }
    assert titles == {"1": "First", "2": "Second", "3": "A", "4": "B"}
    assert "ranked new decisions 3 and 4" in result.stdout


def test_finalize_records_the_plan_and_a_later_finalize_replaces_it(
    stopped,
):
    env = with_decisions(
        stopped,
        **{
            "finalize-1": plan_with(2, "ONE"),
            "finalize-2": plan_with(3, "TWO"),
        },
    )
    result = ask(stopped, "finalize", 1, "Take option 1 everywhere.", **env)
    assert result.returncode == 0, result.stderr
    decided = stopped.path / "decided-plan.md"
    assert decided.read_text() == plan_with(2, "ONE")
    assert result.stdout == (
        f"Decided plan: {decided} (2 groups). To implement it, run "
        f"/xreview:code-implement {stopped.name}.\n"
    )
    finalize = claude_call(stopped, "finalize-1")
    template = (ROOT / "templates/runner/finalize.md").read_text()
    assert finalize["prompt"] == (
        template.rstrip("\n") + "\n\nTake option 1 everywhere.\n"
    )
    # Questions and rounds still work after a Finalize.
    assert ask(stopped, "ask-claude", 2, "One more?", **env).returncode == 0
    result = ask(stopped, "finalize", 3, "Now option 2 on the first.", **env)
    assert result.returncode == 0, result.stderr
    assert decided.read_text() == plan_with(3, "TWO")
    assert stopped.headings().count("Decided Plan") == 2
    assert "(3 groups)" in result.stdout


def test_a_plan_over_15_groups_is_held_until_the_developer_chooses(stopped):
    env = with_decisions(
        stopped, **{"finalize-1": plan_with(16), "finalize-2": "PROCEED"}
    )
    result = ask(stopped, "finalize", 1, "Finalize.", **env)
    assert result.returncode == 0, result.stderr
    assert "This plan has 16 groups, more than 15" in result.stdout
    assert "1. Proceed with all 16 groups." in result.stdout
    assert not (stopped.path / "decided-plan.md").exists()
    assert "Decided Plan" not in stopped.headings()
    assert "Finalize 1 — held" in stopped.headings()
    result = ask(stopped, "finalize", 2, "1, proceed", **env)
    assert result.returncode == 0, result.stderr
    after = claude_call(stopped, "finalize-2")
    assert after["prompt"].startswith(
        "Your last Decided Plan has 16 groups, more than 15"
    )
    assert after["prompt"].endswith("The developer's answer:\n\n1, proceed\n")
    decided = stopped.path / "decided-plan.md"
    assert decided.read_text() == plan_with(16)
    assert stopped.headings().count("Decided Plan") == 1
    assert "(16 groups)" in result.stdout


def test_a_held_plan_can_be_revised_and_is_counted_again(stopped):
    env = with_decisions(
        stopped,
        **{
            "finalize-1": plan_with(17),
            "finalize-2": plan_with(16, "AGAIN"),
            "finalize-3": plan_with(12, "SMALLER"),
        },
    )
    assert ask(stopped, "finalize", 1, "Finalize.", **env).returncode == 0
    result = ask(stopped, "finalize", 2, "Defer some.", **env)
    assert "This plan has 16 groups" in result.stdout
    assert not (stopped.path / "decided-plan.md").exists()
    result = ask(stopped, "finalize", 3, "Defer more.", **env)
    assert "(12 groups)" in result.stdout
    assert (stopped.path / "decided-plan.md").read_text() == (
        plan_with(12, "SMALLER")
    )
    assert stopped.run_state()["held"] is None


def test_groups_are_counted_only_under_implement_now():
    from xreview import runner

    plan = plan_with(2) + "\n\n## Notes\n### Group 9: not counted"
    assert runner.groups(plan) == 2
    assert runner.groups("No sections.") == 0


# --- without decision rounds -------------------------------------------------

PLAN_ALONE = (
    "FAKE-PLAN\n\nPart 3 — Decisions\n1. First: FAKE-PART3-ONE\n"
    "2. Second: FAKE-PART3-TWO\n\n"
    "## Part 4 — Options for the open decisions (Claude)\n\n"
    "### Decision 1: First\nFAKE-CLAUDE-OPTIONS-1\n\n"
    "### Decision 2: Second\nFAKE-CLAUDE-OPTIONS-2\n"
)


def alone_env(review, **claude: str) -> dict[str, str]:
    return with_decisions(
        review,
        **{
            "plan": PLAN_ALONE,
            "decision-copy-q": "### Decision 1: First\nFAKE-COPIED-ONE",
            "decision-copy-q2": "### Decision 2: Second\nFAKE-COPIED-TWO",
            **claude,
        },
    )


def test_without_decision_rounds_the_plan_ends_with_claudes_own_part_4(
    review,
):
    result = review.run("--no-decision-rounds", **alone_env(review))
    assert result.returncode == 0, result.stderr
    template = (ROOT / "templates/runner/plan-part4-alone.md").read_text()
    plan = claude_call(review, "claude-plan")
    assert plan["prompt"].endswith(template)
    assert len(review.calls("claude")) == 3  # no decision copy or ranking
    assert len(review.calls("codex")) == 2
    assert review.headings() == HEADINGS
    report = (review.path / "report.md").read_text()
    assert "FAKE-CLAUDE-OPTIONS-2" in report
    assert "Latest options" not in report
    assert "with 2 open decisions" in result.stdout
    assert review.run_state()["no_decision_rounds"] is True


def test_codex_ranks_a_decision_only_claude_has_ranked(review):
    env = alone_env(review)
    assert review.run("--no-decision-rounds", **env).returncode == 0
    result = ask(review, "ask-codex", 1, WORDS, "--decisions", "1", **env)
    assert result.returncode == 0, result.stderr
    copy = claude_call(review, "decision-copy-q1")
    assert "Part 3 descriptions of decisions 1 only" in copy["prompt"]
    sent = review.calls("codex")[-1]["stdin"]
    state = json.loads((review.path / "state.json").read_text())
    assert sent == cli.compose(
        ["shared/decision-ranking", "shared/reviewer-guard"],
        state,
        f"### Decision 1: First\nFAKE-COPIED-ONE\n\nMy question:\n\n{WORDS}",
    )
    assert not any(
        "ranking-r" in c["argv"][-1] for c in review.calls("claude")
    )
    merge = claude_call(review, "merge-r2")
    assert (
        "Your own view is your Part 4 in the Step 5 plan." in (merge["prompt"])
    )
    report = (review.path / "report.md").read_text()
    assert "### Decision 1: First\n_From decision round 2_" in report
    assert "### Decision 2: Second\n_" not in report
    assert result.stdout.startswith(
        "Codex's ranking of decision 1 (decision round 2):"
    )


def test_a_question_on_both_kinds_of_decision_ranks_first_then_follows_up(
    review,
):
    env = alone_env(review)
    assert review.run("--no-decision-rounds", **env).returncode == 0
    assert (
        ask(
            review, "ask-codex", 1, WORDS, "--decisions", "1", **env
        ).returncode
        == 0
    )
    result = ask(review, "ask-codex", 2, "Both, please.", **env)
    assert result.returncode == 0, result.stderr
    headings = review.headings()
    assert headings.index("Decision round 3 — Codex ranking") < (
        headings.index("Decision round 4 — Codex response")
    )
    ranking_sent = review.calls("codex")[-2]["stdin"]
    assert "### Decision 2: Second\nFAKE-COPIED-TWO" in ranking_sent
    follow_up_sent = review.calls("codex")[-1]["stdin"]
    assert "### Decision 1: First\nFAKE-MERGED-1" in follow_up_sent
    report = (review.path / "report.md").read_text()
    assert "### Decision 1: First\n_From decision round 4_" in report
    assert "### Decision 2: Second\n_From decision round 3_" in report
    assert "(decision rounds 3 and 4)" in result.stdout


def test_part_4_headings_out_of_sequence_are_fixed_in_the_plan(review):
    bad = PLAN_ALONE.replace("### Decision 2:", "### Decision 3:")
    env = alone_env(review, plan=bad, **{"fix-up-plan": PLAN_ALONE})
    result = review.run("--no-decision-rounds", **env)
    assert result.returncode == 0, result.stderr
    assert "Step 5 — Claude final plan — headings fixed" in review.headings()
    titles = {
        k: v["title"] for k, v in review.run_state()["decisions"].items()
    }
    assert titles == {"1": "First", "2": "Second"}
    report = (review.path / "report.md").read_text()
    assert "### Decision 2: Second" in report
    assert "### Decision 3:" not in report


def test_a_retried_operation_keeps_its_numbers(stopped):
    env = stopped.decisions_env
    question = {"FAKE_CLAUDE_CASES": json.dumps({"question-1": "no_result"})}
    assert ask(stopped, "ask-claude", 1, **env, **question).returncode == 4
    assert (
        ask(
            stopped, "ask-claude", 1, WORDS, "--retry-uncertain", **env
        ).returncode
        == 0
    )
    round_two = {"FAKE_CODEX_CASES": json.dumps({"round-2": "incomplete"})}
    assert (
        ask(stopped, "ask-codex", 2, "Again?", **env, **round_two).returncode
        == 4
    )
    assert (
        ask(
            stopped, "ask-codex", 2, "Again?", "--retry-uncertain", **env
        ).returncode
        == 0
    )
    counters = stopped.run_state()["counters"]
    assert (counters["ask-claude"], counters["ask-codex"]) == (1, 1)
    assert counters["round"] == 2
    headings = stopped.headings()
    assert "Question 1 for Claude — answer" in headings
    assert "Decision round 2 — Codex response (attempt 2)" in headings
    assert not any(h.startswith("Decision round 3") for h in headings)
    assert not any(h.startswith("Question 2") for h in headings)


# --- what Session B may read -------------------------------------------------


def other_scratch(review, name: str) -> Path:
    """A file in another session's scratch folder for this repo."""
    import os as _os

    from xreview.providers import claude as _claude

    root = Path(review.repo.env["CLAUDE_CODE_TMPDIR"]).resolve()
    folder = (
        root
        / f"claude-{_os.getuid()}"
        / _claude.slug(review.repo.path.resolve())
        / "33333333-3333-4333-8333-333333333333"
        / "scratchpad"
    )
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("x\n")
    return path


def readable(call: dict, path: Path) -> bool:
    denied = call["settings"]["sandbox"]["filesystem"]["denyRead"]
    return not any(path.resolve().is_relative_to(Path(p)) for p in denied)


def test_files_the_handoff_names_are_shared_and_their_neighbors_arent(
    review,
):
    shared = other_scratch(review, "break_sync.py")
    hidden = other_scratch(review, "mutate.py")
    (review.path / "claude-handoff.md").write_text(
        f"Read these:\n- {shared} (the script behind finding 2)\n"
    )
    assert review.run().returncode == 0
    for call in review.calls("claude")[1:]:  # every Session B turn
        assert readable(call, shared)
        assert not readable(call, hidden)


def test_a_file_named_in_your_words_is_shared_from_then_on(stopped):
    named = other_scratch(stopped, "notes.md")
    words = f"Ask about this: look at `{named}`, please."
    env = stopped.decisions_env
    assert ask(stopped, "ask-claude", 1, words, **env).returncode == 0
    assert readable(stopped.calls("claude")[-1], named)
    assert ask(stopped, "ask-claude", 2, "And again?", **env).returncode == 0
    assert readable(stopped.calls("claude")[-1], named)


def test_other_reviews_are_blocked_but_this_one_isnt(repo, review):
    earlier = repo.path / ".reviews" / "an-earlier-review"
    earlier.mkdir()
    (earlier / "transcript.md").write_text("old\n")
    assert review.run().returncode == 0
    for call in review.calls("claude")[1:]:
        assert not readable(call, earlier / "transcript.md")
        assert readable(call, review.path / "claude-handoff.md")
