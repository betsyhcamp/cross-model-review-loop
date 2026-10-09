"""Tests for prepare, check, prompt and the launcher.

They run bin/xreview against real temporary git repos with a fake codex.
There are no model calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sys
import textwrap

import pytest

from xreview import cli

ROOT = Path(__file__).resolve().parents[1]
BLOCK = cli.simplification_block()


# --- prepare ----------------------------------------------------------------


def test_creates_state_header_and_summary(repo):
    result = repo.xreview("prepare", repo.base[:7])
    assert result.returncode == 0, result.stderr
    review = repo.path / ".reviews" / "main"
    state = json.loads((review / "state.json").read_text())
    assert state == {"base": repo.base, "reviewed_head": repo.head,
                     "reviewer_session_id": None}
    assert (review / "messages").is_dir()
    header = (review / "transcript.md").read_text()
    assert header.startswith(
        "# Review transcript — main\n\n"
        "- Review type: code\n"
        "- Reviewer: Codex (codex-cli 0.0.0-fake)\n"
        f"- Base: {repo.base} — Base commit\n"
        f"- Reviewed head: {repo.head} — Extend a\n"
        "- Started: "), header
    assert header.endswith("\n\n---\n\n")
    for expected in ("`.reviews/main/`", "Add b", "Extend a",
                     "A\tb.txt", "M\ta.txt"):
        assert expected in result.stdout


def test_works_from_a_subfolder(repo):
    (repo.path / "sub").mkdir()
    result = repo.xreview("prepare", repo.base, cwd=repo.path / "sub")
    assert result.returncode == 0, result.stderr
    assert (repo.path / ".reviews/main/state.json").is_file()


def test_untracked_files_are_listed_and_not_included(repo):
    (repo.path / "notes").mkdir()
    (repo.path / "notes" / "spec.md").write_text("draft\n")
    repo.prepare()
    result = repo.xreview("prepare", repo.base)
    assert result.returncode == 0, result.stderr
    untracked = result.stdout.split("not included in this review")[1]
    assert "- `notes/spec.md`" in untracked
    assert ".reviews" not in untracked


@pytest.mark.parametrize("staged", [False, True], ids=["unstaged", "staged"])
def test_uncommitted_tracked_edits_are_refused(repo, staged):
    (repo.path / "a.txt").write_text("edited\n")
    if staged:
        repo.git("add", "a.txt")
    result = repo.xreview("prepare", repo.base)
    assert result.returncode == 1
    assert "commit or stash" in result.stderr
    assert not (repo.path / ".reviews").exists()


@pytest.mark.parametrize("case, reason", [("missing", "not found on PATH"),
                                          ("broken", "broken install")])
def test_codex_missing_or_broken_is_refused(repo, case, reason):
    env = {"missing": {"PATH": str(repo.tools)},
           "broken": {"FAKE_CODEX_CASE": "exit"}}[case]
    result = repo.xreview("prepare", repo.base, **env)
    assert result.returncode == 1
    assert reason in result.stderr
    assert "brew install --cask codex" in result.stderr


def test_outside_a_git_repo_is_refused(repo):
    result = repo.xreview("prepare", repo.base, cwd=repo.tmp)
    assert result.returncode == 1
    assert "not inside a git repository" in result.stderr


def test_bad_base_is_refused(repo):
    result = repo.xreview("prepare", "no-such-ref")
    assert result.returncode == 1
    assert "is not a commit" in result.stderr


def test_base_that_is_not_an_ancestor_is_refused(repo):
    repo.git("checkout", "-q", "-b", "side", repo.base)
    side = repo.commit("c.txt", "side\n", "Side commit")
    repo.git("checkout", "-q", "main")
    result = repo.xreview("prepare", side)
    assert result.returncode == 1
    assert "not an ancestor of HEAD" in result.stderr


def test_empty_range_is_refused(repo):
    result = repo.xreview("prepare", "HEAD")
    assert result.returncode == 1
    assert "no commits after" in result.stderr


def test_names_get_suffixes_and_exclude_entry_is_added_once(repo):
    names = [repo.prepare().name for _ in range(3)]
    names += [repo.prepare("--name", "fix").name for _ in range(2)]
    assert names == ["main", "main-2", "main-3", "fix", "fix-2"]
    exclude = (repo.path / ".git/info/exclude").read_text().splitlines()
    assert exclude.count(".reviews/") == 1


def test_exclude_entry_goes_on_its_own_line(repo):
    exclude = repo.path / ".git/info/exclude"
    exclude.write_text("*.log")
    repo.prepare()
    assert exclude.read_text() == "*.log\n.reviews/\n"


def test_detached_head_gets_a_name(repo):
    repo.git("checkout", "-q", "--detach")
    short = repo.git("rev-parse", "--short", "HEAD").strip()
    assert repo.prepare().name == f"review-{short}"


@pytest.mark.parametrize("name", ["../x", "a/b", "-x", ".hidden", "x y"])
def test_names_outside_the_pattern_are_refused(repo, name):
    result = repo.xreview("prepare", repo.base, f"--name={name}")
    assert result.returncode == 1
    assert not (repo.path / ".reviews").exists()


def test_a_branch_name_outside_the_pattern_needs_a_name(repo):
    repo.git("checkout", "-q", "-b", "fix/a+b")
    result = repo.xreview("prepare", repo.base)
    assert result.returncode == 1
    assert "--name" in result.stderr
    assert repo.prepare("--name", "fix-ab").name == "fix-ab"


# --- check ------------------------------------------------------------------


@pytest.fixture
def review(repo) -> Path:
    return repo.prepare()


def check(repo, review: Path):
    return repo.xreview("check", review.name)


def test_block_is_the_whole_paragraph():
    assert BLOCK.startswith("Simplification. Measure in")
    assert BLOCK.endswith("don't propose it.")


def test_passes_on_a_valid_review(repo, review):
    repo.write_prompt(review, BLOCK)
    result = check(repo, review)
    assert (result.returncode, result.stdout) == (0, "ok\n"), result.stderr


def test_tree_check(repo, review):
    repo.write_prompt(review, BLOCK)
    (repo.path / "scratch.txt").write_text("untracked\n")
    assert check(repo, review).returncode == 0
    (repo.path / "a.txt").write_text("edited\n")
    result = check(repo, review)
    assert result.returncode == 1
    assert "a.txt" in result.stderr
    assert "stash them to keep this review" in result.stderr
    repo.git("checkout", "--", "a.txt")
    later = repo.commit("c.txt", "new\n", "Later commit")
    result = check(repo, review)
    assert result.returncode == 1
    assert (f"HEAD has moved since prepare (reviewed {repo.head[:7]}, now "
            f"{later[:7]}). This review covers {repo.head[:7]} only. To "
            "review the new commits, re-run code-prepare in the session "
            "that wrote the code.") in result.stderr


@pytest.mark.parametrize("quoted", [False, True], ids=["reflowed", "quoted"])
def test_prompt_check_accepts_reflowed_and_blockquoted_copies(repo, review,
                                                              quoted):
    intro, *bullets = BLOCK.split(" - ")
    parts = [textwrap.fill(part, 72) for part in [intro, *bullets]]
    block = "\n- ".join(parts)
    if quoted:
        block = "\n".join("> " + line for line in block.splitlines())
    repo.write_prompt(review, block)
    result = check(repo, review)
    assert result.returncode == 0, result.stderr


def test_prompt_check_accepts_seven_character_prefixes(repo, review):
    repo.write_prompt(review, BLOCK, (repo.base[:7], repo.head[:7]))
    assert check(repo, review).returncode == 0


@pytest.mark.parametrize("case, message", [
    ("paraphrased", "Simplification block"),
    ("no base", "base commit"),
    ("short base", "base commit"),
])
def test_prompt_check_rejects_paraphrase_and_missing_commits(repo, review,
                                                             case, message):
    block, shas = {
        "paraphrased": (BLOCK.replace("deleting them", "removing them"),
                        None),
        "no base": (BLOCK, (repo.head,)),
        "short base": (BLOCK, (repo.base[:6], repo.head)),
    }[case]
    repo.write_prompt(review, block, shas)
    result = check(repo, review)
    assert result.returncode == 1
    assert message in result.stderr


def test_missing_prompt_fails(repo, review):
    result = check(repo, review)
    assert result.returncode == 1
    assert "reviewer-prompt.md is missing" in result.stderr


@pytest.mark.usefixtures("review")
@pytest.mark.parametrize("name", ["nosuch", "../.."])
def test_unknown_review_is_refused(repo, name):
    result = repo.xreview("check", name)
    assert result.returncode == 1
    assert "no review named" in result.stderr


# --- prompt -----------------------------------------------------------------


def test_fills_base_and_head(repo):
    review = repo.prepare()
    result = repo.xreview("prompt", review.name,
                          "--template", "code/reviewer-prompt-guide")
    assert result.returncode == 0, result.stderr
    guide = ROOT / "templates/code/reviewer-prompt-guide.md"
    expected = guide.read_text().replace("{base}", repo.base)
    expected = expected.replace("{head}", repo.head).rstrip("\n") + "\n"
    assert result.stdout == expected


def test_name_needed_only_for_placeholders(repo):
    result = repo.xreview("prompt", "--template", "shared/protocol")
    assert result.returncode == 0, result.stderr
    protocol = ROOT / "templates/shared/protocol.md"
    assert result.stdout == protocol.read_text()
    result = repo.xreview("prompt", "--template", "code/reviewer-prompt-guide")
    assert result.returncode == 1
    assert "needs a review name" in result.stderr


def test_every_template_a_skill_names_exists():
    names = set()
    for skill in (ROOT / "skills").glob("*/SKILL.md"):
        text = skill.read_text()
        found = re.findall(r"--template(?:=|\s+)([^\s`]+)", text)
        # Every --template must match, so a form the pattern doesn't know
        # fails here instead of being skipped.
        assert text.count("--template") == len(found), skill
        names.update(found)
    assert len(names) >= 5, names
    for name in sorted(names):
        assert (ROOT / "templates" / f"{name}.md").is_file(), name


@pytest.mark.parametrize("kind", ["absolute", "relative", "missing"])
def test_templates_outside_templates_folder_are_refused(repo, kind):
    secret = repo.tmp / "secret"
    secret.with_suffix(".md").write_text("SECRET\n")
    name = {"absolute": str(secret),
            "relative": os.path.relpath(secret, ROOT / "templates"),
            "missing": "code/no-such-template"}[kind]
    result = repo.xreview("prompt", "--template", name)
    assert result.returncode == 1
    assert "SECRET" not in result.stdout
    assert "no template" in result.stderr


@pytest.mark.parametrize("args", [("prepare",), ("bogus",),
                                  ("prompt", "main")])
def test_usage_errors_exit_2(repo, args):
    assert repo.xreview(*args).returncode == 2


# --- launcher ---------------------------------------------------------------

TOO_OLD = "#!/bin/sh\nexit 1\n"


def run_with(repo, tools: dict[str, str]):
    """Run `xreview prompt` with PATH holding git and the given tools."""
    folder = repo.tmp / "launcher-bin"
    folder.mkdir()
    (folder / "git").symlink_to(shutil.which("git"))
    for name, text in tools.items():
        (folder / name).write_text(text)
        (folder / name).chmod(0o755)
    return repo.xreview("prompt", "--template", "shared/protocol",
                        PATH=str(folder))


@pytest.mark.parametrize("found", ["python3.N on PATH", "uv's choice"])
def test_skips_a_python_that_is_too_old(repo, found):
    good = f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'
    uv = f'#!/bin/sh\necho "{repo.tools / "python3"}"\n'
    tools = {"python3.N on PATH": {"python3.12": good},
             "uv's choice": {"uv": uv}}[found]
    result = run_with(repo, {"python3": TOO_OLD, **tools})
    assert result.returncode == 0, result.stderr


def test_reports_when_no_python_is_found(repo):
    result = run_with(repo, {"python3": TOO_OLD})
    assert result.returncode == 1
    assert "needs Python 3.10 or later" in result.stderr
