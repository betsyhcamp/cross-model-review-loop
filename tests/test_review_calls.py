"""Tests for start, continue and append, run through bin/xreview against
real temporary git repos with the fake codex. There are no model calls."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

import pytest

from xreview import cli

ROOT = Path(__file__).resolve().parents[1]
XREVIEW = ROOT / "bin" / "xreview"
SESSION = "01a11457-66e5-73e3-8c89-991f3b54c198"  # the fake codex's
STEP3 = "Step 3 — Claude rebuttal — sent to Codex"
STEP4 = "Step 4 — Codex response"
RESPONSE_MARKER = "FAKE-RESPONSE-BODY"


class Review:
    """A prepared review with a valid reviewer prompt, and helpers to run
    xreview on it against the fake codex."""

    def __init__(self, repo):
        self.repo = repo
        self.path = repo.prepare()
        self.name = self.path.name
        repo.write_prompt(self.path, cli.simplification_block())
        self.log = repo.tmp / "codex-calls.jsonl"

    def xreview(self, *args: str, case: str = "ok"):
        return self.repo.xreview(*args, FAKE_CODEX_CASE=case,
                                 FAKE_CODEX_LOG=str(self.log))

    def start(self, case: str = "ok") -> subprocess.CompletedProcess:
        return self.xreview("start", self.name, case=case)

    def continue_(self, body: str, templates=("code/claude-handoff-guide",),
                  case: str = "ok") -> subprocess.CompletedProcess:
        args = ["continue", self.name]
        for template in templates:
            args += ["--template", template]
        args += ["--body", body, "--sent-heading", STEP3,
                 "--response-heading", STEP4]
        return self.xreview(*args, case=case)

    def start_in_background(self, pids: Path) -> subprocess.Popen:
        """Start the runner with a codex that hangs; return once codex has
        written its pids."""
        env = {**self.repo.env, "FAKE_CODEX_CASE": "timeout",
               "FAKE_CODEX_PIDS": str(pids)}
        runner = subprocess.Popen(
            [str(XREVIEW), "start", self.name], cwd=self.repo.path, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 10
        while not pids.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        return runner

    def message(self, name: str, text: str) -> str:
        (self.path / "messages").mkdir(exist_ok=True)
        (self.path / "messages" / name).write_text(text)
        return f"messages/{name}"

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def transcript(self) -> str:
        return (self.path / "transcript.md").read_text()

    def session(self) -> str | None:
        state = json.loads((self.path / "state.json").read_text())
        return state["reviewer_session_id"]


@pytest.fixture
def review(repo) -> Review:
    return Review(repo)


def test_start_sends_the_prompt_unchanged_and_records_both_sides(review):
    result = review.start()
    assert result.returncode == 0, result.stderr
    prompt = (review.path / "reviewer-prompt.md").read_text()
    assert [call["stdin"] for call in review.calls()] == [prompt]
    assert review.session() == SESSION
    run = review.path / "runs" / "01-step-2-codex-review"
    for name in ("prompt.md", "response.md", "events.jsonl", "stderr.txt"):
        assert (run / name).is_file(), name
    assert (run / "prompt.md").read_text() == prompt
    text = review.transcript()
    assert re.search(r"## Reviewer prompt — sent to Codex\n"
                     r"_[^\n]+ · run 01_\n\n" + re.escape(prompt), text)
    assert re.search(r"## Step 2 — Codex review\n"
                     r"_[^\n]+ · run 01 · \d+s_\n\n"
                     r"Finding 1: nothing real\.\n\n"
                     + RESPONSE_MARKER + "\n\n---\n\n$", text)
    status, path = result.stdout.splitlines()
    assert re.search(r"^Step 2 — Codex review: done in \d+s \(run 01\)\.$",
                     status)
    assert path == f"Response: {run / 'response.md'}"
    assert RESPONSE_MARKER not in result.stdout + result.stderr


def test_start_refuses_when_a_session_exists(review):
    review.start()
    before = review.transcript()
    result = review.start()
    assert result.returncode == 1
    assert "already started" in result.stderr
    assert (len(review.calls()), review.transcript()) == (1, before)


def test_continue_refuses_without_a_session(review):
    before = review.transcript()
    result = review.continue_(review.message("rebuttal.md", "Rebuttal\n"))
    assert result.returncode == 1
    assert "run start first" in result.stderr
    assert (review.calls(), review.transcript()) == ([], before)


def test_moved_head_or_modified_file_refused_before_codex_runs(repo, review):
    body = review.message("rebuttal.md", "Rebuttal\n")

    def refused(run, message):
        before = (len(review.calls()), review.transcript())
        result = run()
        assert result.returncode == 1
        assert message in result.stderr
        assert (len(review.calls()), review.transcript()) == before

    for label, run in (("start", review.start),
                       ("continue", lambda: review.continue_(body))):
        (repo.path / "a.txt").write_text("edited\n")
        refused(run, "commit or stash")
        repo.git("checkout", "--", "a.txt")
        repo.commit("c.txt", "later\n", "Later commit")
        refused(run, "HEAD has moved")
        repo.git("reset", "-q", "--hard", repo.head)
        if label == "start":
            assert review.start().returncode == 0
    assert len(review.calls()) == 1


def test_tracked_edit_during_the_call_rejects_the_response(review):
    result = review.start("edit_tracked")
    assert result.returncode == 3
    assert "(FAILED)" in result.stderr
    assert "M a.txt" in result.stderr
    text = review.transcript()
    assert "## Step 2 — Codex review (FAILED)" in text
    assert "the tree changed during the call" in text
    assert " M a.txt" in text
    assert RESPONSE_MARKER not in text + result.stdout
    response = review.path / "runs/01-step-2-codex-review/response.md"
    assert RESPONSE_MARKER in response.read_text()
    assert review.session() is None


def test_untracked_file_created_during_the_call_is_accepted(review):
    result = review.start("create_untracked")
    assert result.returncode == 0, result.stderr
    assert review.session() == SESSION


def test_continue_composes_byte_exact_and_resumes_the_session(repo, review):
    review.start()
    body_text = "  Body keeps {base} and {head} as written.\n\n\n"
    body = review.message("03-rebuttal.md", body_text)
    templates = ("shared/protocol", "code/claude-handoff-guide")
    result = review.continue_(body, templates)
    assert result.returncode == 0, result.stderr
    protocol = (ROOT / "templates/shared/protocol.md").read_text()
    handoff = (ROOT / "templates/code/claude-handoff-guide.md").read_text()
    handoff = handoff.replace("{base}", repo.base)
    handoff = handoff.replace("{head}", repo.head)
    expected = (protocol.rstrip("\n") + "\n\n" + handoff.rstrip("\n")
                + "\n\n  Body keeps {base} and {head} as written.\n")
    call = review.calls()[1]
    assert call["stdin"] == expected
    assert call["argv"][:3] == ["exec", "resume", SESSION]
    run = review.path / "runs" / "02-step-4-codex-response"
    assert (run / "prompt.md").read_text() == expected
    text = review.transcript()
    assert re.search(f"## {STEP3}\n_[^\n]+ · run 02_\n\n"
                     + re.escape(expected), text)
    assert f"## {STEP4}\n_" in text
    assert RESPONSE_MARKER not in result.stdout + result.stderr


@pytest.mark.parametrize("path", ["absolute", "../elsewhere.md",
                                  "messages/link.md", "messages/missing.md"])
def test_paths_outside_the_review_folder_are_refused(repo, review, path):
    review.start()
    before = (len(review.calls()), review.transcript())
    (repo.tmp / "secret.md").write_text("SECRET\n")
    (repo.path / ".reviews" / "elsewhere.md").write_text("SECRET\n")
    (review.path / "messages").mkdir()
    (review.path / "messages" / "link.md").symlink_to(repo.tmp / "secret.md")
    if path == "absolute":
        path = str(repo.tmp / "secret.md")
    result = review.continue_(path)
    assert result.returncode == 1
    assert "isn't a file inside" in result.stderr
    result = review.xreview("append", review.name, "--heading", "Plan",
                            "--file", path)
    assert result.returncode == 1
    assert (len(review.calls()), review.transcript()) == before


def test_codex_failure_is_recorded_and_a_retry_is_marked(review):
    result = review.start("exit")
    assert result.returncode == 3
    assert "Authentication failed" in result.stderr
    assert "## Step 2 — Codex review (FAILED)" in review.transcript()
    assert review.session() is None
    result = review.start()
    assert result.returncode == 0, result.stderr
    text = review.transcript()
    assert "## Reviewer prompt — sent to Codex (attempt 2)\n" in text
    assert "## Step 2 — Codex review (attempt 2)\n" in text
    assert (review.path / "runs/02-step-2-codex-review-attempt-2").is_dir()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP,
                                 signal.SIGINT], ids=lambda sig: sig.name)
def test_a_stop_signal_kills_codex_and_is_recorded(repo, review,
                                                   process_gone, sig):
    pids = repo.tmp / "pids"
    runner = review.start_in_background(pids)
    runner.send_signal(sig)
    _, stderr = runner.communicate(timeout=10)
    assert runner.returncode == 3, stderr
    assert f"stopped ({sig.name})" in stderr
    for pid in map(int, pids.read_text().split()):
        assert process_gone(pid), f"{pid} survived"
    assert re.search(r"Step 2 — Codex review \(FAILED\)\n_[^\n]+_\n\n"
                     rf"Codex call failed: The runner was stopped "
                     rf"\({sig.name}\)", review.transcript())
    assert review.session() is None


def test_a_stop_that_also_kills_codex_is_recorded(repo, review):
    # A stop from Claude Code signals the whole process tree at once, so
    # Codex is often dead but not yet reaped when the runner kills its
    # group. macOS reports that as EPERM rather than ESRCH.
    pids = repo.tmp / "pids"
    runner = review.start_in_background(pids)
    codex_pid = int(pids.read_text().split()[0])
    os.killpg(codex_pid, signal.SIGKILL)
    runner.send_signal(signal.SIGTERM)
    _, stderr = runner.communicate(timeout=10)
    assert "Traceback" not in stderr
    assert runner.returncode == 3, stderr
    assert "## Step 2 — Codex review (FAILED)" in review.transcript()


def test_append_records_claudes_section_word_for_word(review):
    plan = "## Part 1\n\n- keep this line exactly\n"
    result = review.xreview("append", review.name, "--heading",
                            "Step 5 — Claude final plan", "--file",
                            review.message("plan.md", plan))
    assert result.returncode == 0, result.stderr
    assert re.search(r"## Step 5 — Claude final plan\n_[^\n]+_\n\n"
                     + re.escape(plan) + "\n---\n\n$", review.transcript())


def test_concurrent_appends_stay_intact(repo, review):
    bodies = {i: f"Section {i} line.\n" * 12000 for i in range(6)}
    for i, body in bodies.items():
        review.message(f"{i}.md", body)
    processes = [
        subprocess.Popen([str(XREVIEW), "append", review.name, "--heading",
                          f"Section {i}", "--file", f"messages/{i}.md"],
                         cwd=repo.path, env=repo.env,
                         stdout=subprocess.DEVNULL)
        for i in bodies]
    assert [p.wait(timeout=30) for p in processes] == [0] * 6
    text = review.transcript()
    for i, body in bodies.items():
        assert re.search(f"## Section {i}\n_[^\n]+_\n\n"
                         + re.escape(body) + "\n---\n\n", text)


@pytest.mark.parametrize("args", [
    ("continue", "{name}", "--template", "shared/protocol"),
    ("append", "{name}", "--file", "x.md"),
    ("start",),
])
def test_usage_errors_exit_2(review, args):
    args = [arg.format(name=review.name) for arg in args]
    assert review.xreview(*args).returncode == 2


def test_only_trailing_newlines_change_at_part_boundaries(tmp_path,
                                                          monkeypatch):
    (tmp_path / "a.md").write_text("A {base}\n\n\n")
    (tmp_path / "b.md").write_text("  B {head}")
    state = {"base": "1" * 40, "reviewed_head": "2" * 40}
    monkeypatch.setattr(cli, "TEMPLATES", tmp_path)
    message = cli.compose(["a", "b", "a"], state, "C {base}\n  \n\n")
    assert message == (f"A {'1' * 40}\n\n  B {'2' * 40}\n\n"
                       f"A {'1' * 40}\n\nC {{base}}\n  \n")
