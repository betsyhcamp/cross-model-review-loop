"""Tests for the calls to Codex that xreview run makes, and for append:
what each call records, the tree check around it, and how a failed,
uncertain or stopped call is recorded. bin/xreview runs against real
temporary git repos with the fake codex and the fake claude. There are no
model calls."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

import pytest

from test_runner import SESSION, XREVIEW, Review
from xreview import cli

# Run 01 is Claude's setup check, so Codex's review is run 02.
FIRST = "02-step-2-codex-review"
REMEDIES = ("commit or stash", "stash them", "run /xreview:code-review again")


@pytest.fixture
def review(repo) -> Review:
    return Review(repo)


def session(review) -> str | None:
    state = json.loads((review.path / "state.json").read_text())
    return state["reviewer_session_id"]


def with_codex(case: str, step: str = "codex-review") -> dict[str, str]:
    return {"FAKE_CODEX_CASES": json.dumps({step: case})}


def test_the_reviewer_prompt_goes_unchanged_and_both_sides_are_recorded(
    review,
):
    result = review.run()
    assert result.returncode == 0, result.stderr
    prompt = (review.path / "reviewer-prompt.md").read_text()
    assert review.calls("codex")[0]["stdin"] == prompt
    assert session(review) == SESSION
    run = review.path / "runs" / FIRST
    for name in ("prompt.md", "response.md", "events.jsonl", "stderr.txt"):
        assert (run / name).is_file(), name
    assert (run / "prompt.md").read_text() == prompt
    text = review.transcript()
    assert re.search(
        r"## Reviewer prompt — sent to Codex\n_[^\n]+ · run 02_\n\n"
        + re.escape(prompt),
        text,
    )
    assert re.search(
        r"## Step 2 — Codex review\n_[^\n]+ · run 02 · \d+s_\n\n"
        r"FAKE-CODEX-REVIEW\n\n---\n\n",
        text,
    )
    assert re.search(
        r"^xreview: Step 2 — Codex review: done in \d+s \(run 02\)\.$",
        result.stderr,
        re.M,
    )
    # Session A sees status lines and a report path, never Codex's text.
    assert "FAKE-CODEX-REVIEW" not in result.stdout + result.stderr


def test_a_moved_head_or_changed_file_stops_the_run_before_codex(repo, review):
    (repo.path / "a.txt").write_text("edited\n")
    result = review.run()
    assert result.returncode == 1
    assert "stash them to keep this review" in result.stderr
    repo.git("checkout", "--", "a.txt")
    repo.commit("c.txt", "later\n", "Later commit")
    result = review.run()
    assert result.returncode == 1
    assert f"This review covers {repo.head[:7]} only" in result.stderr
    assert "run /xreview:code-review again" in result.stderr
    assert review.counts() == (0, 0)


def test_a_tracked_edit_during_the_call_rejects_the_response(review):
    result = review.run(**with_codex("edit_tracked"))
    assert result.returncode == 3
    assert "(FAILED)" in result.stderr
    text = review.transcript()
    assert "## Step 2 — Codex review (FAILED)" in text
    assert "the tree changed during the call" in text
    assert (
        "Tracked files changed during the call:\n M a.txt\n\n"
        "Restore the tree before any retry."
    ) in text
    assert "FAKE-CODEX-REVIEW" not in text + result.stdout
    response = review.path / "runs" / FIRST / "response.md"
    assert "FAKE-CODEX-REVIEW" in response.read_text()
    assert session(review) is None
    # The edit is the reviewer's, so the remedies for the user's own
    # changes don't apply.
    for remedy in REMEDIES:
        assert remedy not in result.stderr + text


def test_a_commit_during_the_call_rejects_the_response(repo, review):
    result = review.run(**with_codex("commit"))
    assert result.returncode == 3
    now = repo.git("rev-parse", "--short=7", "HEAD").strip()
    moved = (
        f"HEAD moved during the call (was {repo.head[:7]}, now {now})."
        "\n\nRestore the tree before any retry."
    )
    assert moved in result.stderr
    assert moved in review.transcript()
    for remedy in REMEDIES:
        assert remedy not in result.stderr + review.transcript()
    assert session(review) is None


def test_a_failed_call_that_changed_the_tree_says_to_restore_it(review):
    # Without the restore line, a retry would reach the pre-call check,
    # which says to stash: that keeps the reviewer's edit.
    result = review.run(**with_codex("edit_then_exit"))
    assert result.returncode == 3
    for output in (result.stderr, review.transcript()):
        assert "Codex exited with code 7" in output
        assert (
            "Tracked files changed during the call:\n M a.txt\n\n"
            "Restore the tree before any retry."
        ) in output
        for remedy in REMEDIES:
            assert remedy not in output
    assert session(review) is None


def test_an_untracked_file_created_during_the_call_is_accepted(review):
    result = review.run(**with_codex("create_untracked"))
    assert result.returncode == 0, result.stderr
    assert session(review) == SESSION


def test_a_failed_call_is_recorded_and_its_retry_is_marked(review):
    result = review.run(**with_codex("exit"))
    assert result.returncode == 3
    assert "Authentication failed" in result.stderr
    assert "## Step 2 — Codex review (FAILED)" in review.transcript()
    assert session(review) is None
    # Nothing changed in the tree, so there's nothing to restore.
    assert "Restore the tree" not in result.stderr + review.transcript()
    result = review.run()
    assert result.returncode == 0, result.stderr
    text = review.transcript()
    assert "## Reviewer prompt — sent to Codex (attempt 2)\n" in text
    assert "## Step 2 — Codex review (attempt 2)\n" in text
    assert list((review.path / "runs").glob("*-step-2-codex-review-attempt-2"))


def test_an_unreadable_result_is_recorded_as_uncertain_and_exits_4(review):
    result = review.run(**with_codex("incomplete"))
    assert result.returncode == 4, result.stderr
    assert "Codex call is uncertain" in result.stderr
    assert "an (UNCERTAIN) section for run 02" in result.stderr
    text = review.transcript()
    assert "## Step 2 — Codex review (UNCERTAIN)" in text
    assert "(FAILED)" not in text
    assert session(review) is None


def test_a_refused_second_writer_is_recorded_as_failed(review):
    result = review.run(**with_codex("active_writer", "codex-response"))
    assert result.returncode == 3, result.stderr
    assert "another Codex process" in result.stderr
    assert "## Step 4 — Codex response (FAILED)" in review.transcript()


def start_hanging(review, pids: Path) -> subprocess.Popen:
    """Run the review with a Codex review that hangs; return once Codex
    has written its pids."""
    runner = subprocess.Popen(
        [str(XREVIEW), "run", review.name],
        cwd=review.repo.path,
        env=review.env(**with_codex("timeout"), FAKE_CODEX_PIDS=str(pids)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 15
    while not pids.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return runner


@pytest.mark.parametrize(
    "sig",
    [signal.SIGTERM, signal.SIGHUP, signal.SIGINT],
    ids=lambda sig: sig.name,
)
def test_a_stop_signal_kills_codex_and_is_recorded_as_uncertain(
    repo, review, process_gone, sig
):
    pids = repo.tmp / "pids"
    runner = start_hanging(review, pids)
    runner.send_signal(sig)
    _, stderr = runner.communicate(timeout=10)
    assert runner.returncode == 4, stderr
    assert f"stopped ({sig.name})" in stderr
    for pid in map(int, pids.read_text().split()):
        assert process_gone(pid), f"{pid} survived"
    assert re.search(
        r"Step 2 — Codex review \(UNCERTAIN\)\n_[^\n]+_\n\n"
        rf"Codex call is uncertain: The runner was stopped "
        rf"\({sig.name}\) and Codex was killed",
        review.transcript(),
    )
    assert session(review) is None


def test_a_stop_that_also_kills_codex_is_recorded(repo, review):
    # A stop from Claude Code signals the whole process tree at once, so
    # Codex is often dead but not yet reaped when the runner kills its
    # group. macOS reports that as EPERM rather than ESRCH.
    pids = repo.tmp / "pids"
    runner = start_hanging(review, pids)
    codex_pid = int(pids.read_text().split()[0])
    os.killpg(codex_pid, signal.SIGKILL)
    runner.send_signal(signal.SIGTERM)
    _, stderr = runner.communicate(timeout=10)
    assert "Traceback" not in stderr
    assert runner.returncode == 4, stderr
    assert "## Step 2 — Codex review (UNCERTAIN)" in review.transcript()


# --- append ----------------------------------------------------------------


def message(review, name: str, text: str) -> str:
    (review.path / "messages" / name).write_text(text)
    return f"messages/{name}"


def test_append_records_claudes_section_word_for_word(review):
    plan = "## Part 1\n\n- keep this line exactly\n"
    result = review.xreview(
        "append",
        review.name,
        "--heading",
        "Final Changes",
        "--file",
        message(review, "final.md", plan),
    )
    assert result.returncode == 0, result.stderr
    assert re.search(
        r"## Final Changes\n_[^\n]+_\n\n" + re.escape(plan) + "\n---\n\n$",
        review.transcript(),
    )


@pytest.mark.parametrize(
    "path",
    ["absolute", "../elsewhere.md", "messages/link.md", "messages/missing.md"],
)
def test_append_refuses_paths_outside_the_review_folder(repo, review, path):
    before = review.transcript()
    (repo.tmp / "secret.md").write_text("SECRET\n")
    (repo.path / ".reviews" / "elsewhere.md").write_text("SECRET\n")
    (review.path / "messages" / "link.md").symlink_to(repo.tmp / "secret.md")
    if path == "absolute":
        path = str(repo.tmp / "secret.md")
    result = review.xreview(
        "append", review.name, "--heading", "Plan", "--file", path
    )
    assert result.returncode == 1
    assert "isn't a file inside" in result.stderr
    assert review.transcript() == before


def test_concurrent_appends_stay_intact(repo, review):
    bodies = {i: f"Section {i} line.\n" * 12000 for i in range(6)}
    for i, body in bodies.items():
        message(review, f"{i}.md", body)
    processes = [
        subprocess.Popen(
            [
                str(XREVIEW),
                "append",
                review.name,
                "--heading",
                f"Section {i}",
                "--file",
                f"messages/{i}.md",
            ],
            cwd=repo.path,
            env=repo.env,
            stdout=subprocess.DEVNULL,
        )
        for i in bodies
    ]
    assert [p.wait(timeout=30) for p in processes] == [0] * 6
    text = review.transcript()
    for i, body in bodies.items():
        assert re.search(
            f"## Section {i}\n_[^\n]+_\n\n" + re.escape(body) + "\n---\n\n",
            text,
        )


@pytest.mark.parametrize(
    "args",
    [
        ("append", "{name}", "--file", "x.md"),
        ("run",),
        ("ask-claude", "{name}"),
        ("start", "{name}"),
        ("continue", "{name}"),
    ],
)
def test_usage_errors_exit_2(review, args):
    args = [arg.format(name=review.name) for arg in args]
    assert review.xreview(*args).returncode == 2


def test_only_trailing_newlines_change_at_part_boundaries(
    tmp_path, monkeypatch
):
    (tmp_path / "a.md").write_text("A {base}\n\n\n")
    (tmp_path / "b.md").write_text("  B {head}")
    state = {"base": "1" * 40, "reviewed_head": "2" * 40}
    monkeypatch.setattr(cli, "TEMPLATES", tmp_path)
    message = cli.compose(["a", "b", "a"], state, "C {base}\n  \n\n")
    assert message == (
        f"A {'1' * 40}\n\n  B {'2' * 40}\n\nA {'1' * 40}\n\nC {{base}}\n  \n"
    )
