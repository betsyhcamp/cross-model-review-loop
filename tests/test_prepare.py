"""Tests for prepare, check, prompt and the launcher.

They run bin/xreview against real temporary git repos with a fake codex.
There are no model calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
XREVIEW = ROOT / "bin" / "xreview"
FAKE_CODEX = ROOT / "tests" / "fake_codex.py"
sys.path.insert(0, str(ROOT))
from xreview import cli  # noqa: E402


def write_exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def run_git(repo: Path, env: dict, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                            capture_output=True, text=True)
    return result.stdout


def commit(repo: Path, env: dict, path: str, text: str, message: str) -> str:
    (repo / path).write_text(text)
    run_git(repo, env, "add", path)
    run_git(repo, env, "commit", "-q", "-m", message)
    return run_git(repo, env, "rev-parse", "HEAD").strip()


class RepoCase(unittest.TestCase):
    """A temporary repo with two commits after a base commit. PATH holds only
    git, a python3 that runs this test's interpreter, and the fake codex."""

    tools: Path
    codex_dir: Path
    template: Path
    env: dict[str, str]
    base: str
    head: str

    @classmethod
    def setUpClass(cls):
        # Built once per class and copied per test: macOS adds ~0.1s to the
        # first run of each new executable, and git calls are slow too.
        temp = tempfile.TemporaryDirectory(prefix="xreview-test-")
        cls.addClassCleanup(temp.cleanup)
        shared = Path(temp.name).resolve()
        cls.tools = shared / "tools"
        cls.codex_dir = shared / "codex-bin"
        cls.template = shared / "repo"
        for folder in (cls.tools, cls.codex_dir, cls.template):
            folder.mkdir()
        (cls.tools / "git").symlink_to(shutil.which("git"))
        write_exe(cls.tools / "python3",
                  f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        write_exe(cls.codex_dir / "codex",
                  f'#!/bin/sh\nexec python3 "{FAKE_CODEX}" "$@"\n')
        cls.env = {
            **os.environ,
            "PATH": f"{cls.codex_dir}:{cls.tools}",
            "HOME": str(shared),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
        cls.env.pop("FAKE_CODEX_CASE", None)
        repo, env = cls.template, cls.env
        run_git(repo, env, "init", "-q", "-b", "main")
        cls.base = commit(repo, env, "a.txt", "one\n", "Base commit")
        commit(repo, env, "b.txt", "two\n", "Add b")
        cls.head = commit(repo, env, "a.txt", "one\nmore\n", "Extend a")

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="xreview-test-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.repo = self.tmp / "repo with spaces"
        shutil.copytree(self.template, self.repo, symlinks=True)

    def git(self, *args: str) -> str:
        return run_git(self.repo, self.env, *args)

    def commit(self, path: str, text: str, message: str) -> str:
        return commit(self.repo, self.env, path, text, message)

    def xreview(self, *args: str, cwd: Path | None = None,
                **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(XREVIEW), *args], cwd=cwd or self.repo,
                              env={**self.env, **env},
                              capture_output=True, text=True)

    def prepare(self, *args: str) -> Path:
        """Run prepare on self.base; return the review folder."""
        before = set((self.repo / ".reviews").glob("*"))
        result = self.xreview("prepare", self.base, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        (review,) = set((self.repo / ".reviews").glob("*")) - before
        return review

    def write_prompt(self, review: Path, block: str,
                     shas: tuple[str, ...] | None = None) -> None:
        refs = " and ".join(shas or (self.base, self.head))
        (review / "reviewer-prompt.md").write_text(
            f"Review the commits {refs}.\n\n{block}\n\n"
            "You have read-only access.\n")


class PrepareTests(RepoCase):
    def test_creates_state_header_and_summary(self):
        result = self.xreview("prepare", self.base[:7])
        self.assertEqual(result.returncode, 0, result.stderr)
        review = self.repo / ".reviews" / "main"
        state = json.loads((review / "state.json").read_text())
        self.assertEqual(state, {"base": self.base, "reviewed_head": self.head,
                                 "reviewer_session_id": None})
        header = (review / "transcript.md").read_text()
        self.assertTrue(header.startswith(
            "# Review transcript — main\n\n"
            "- Review type: code\n"
            "- Reviewer: Codex (codex-cli 0.0.0-fake)\n"
            f"- Base: {self.base} — Base commit\n"
            f"- Reviewed head: {self.head} — Extend a\n"
            "- Started: "), header)
        self.assertTrue(header.endswith("\n\n---\n\n"))
        for expected in ("`.reviews/main/`", "Add b", "Extend a",
                         "A\tb.txt", "M\ta.txt"):
            self.assertIn(expected, result.stdout)

    def test_works_from_a_subfolder(self):
        (self.repo / "sub").mkdir()
        result = self.xreview("prepare", self.base, cwd=self.repo / "sub")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / ".reviews/main/state.json").is_file())

    def test_untracked_files_are_listed_and_not_included(self):
        (self.repo / "notes").mkdir()
        (self.repo / "notes" / "spec.md").write_text("draft\n")
        self.prepare()
        result = self.xreview("prepare", self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        untracked = result.stdout.split("not included in this review")[1]
        self.assertIn("- `notes/spec.md`", untracked)
        self.assertNotIn(".reviews", untracked)

    def test_uncommitted_tracked_edits_are_refused(self):
        (self.repo / "a.txt").write_text("edited\n")
        for label in ("unstaged", "staged"):
            with self.subTest(label):
                if label == "staged":
                    self.git("add", "a.txt")
                result = self.xreview("prepare", self.base)
                self.assertEqual(result.returncode, 1)
                self.assertIn("commit or stash", result.stderr)
                self.assertFalse((self.repo / ".reviews").exists())

    def test_codex_missing_or_broken_is_refused(self):
        cases = (
            ("missing", {"PATH": str(self.tools)}, "not found on PATH"),
            ("broken", {"FAKE_CODEX_CASE": "exit"}, "broken install"),
        )
        for label, env, reason in cases:
            with self.subTest(label):
                result = self.xreview("prepare", self.base, **env)
                self.assertEqual(result.returncode, 1)
                self.assertIn(reason, result.stderr)
                self.assertIn("brew install --cask codex", result.stderr)

    def test_outside_a_git_repo_is_refused(self):
        result = self.xreview("prepare", self.base, cwd=self.tmp)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not inside a git repository", result.stderr)

    def test_bad_base_is_refused(self):
        result = self.xreview("prepare", "no-such-ref")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not a commit", result.stderr)

    def test_base_that_is_not_an_ancestor_is_refused(self):
        self.git("checkout", "-q", "-b", "side", self.base)
        side = self.commit("c.txt", "side\n", "Side commit")
        self.git("checkout", "-q", "main")
        result = self.xreview("prepare", side)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not an ancestor of HEAD", result.stderr)

    def test_empty_range_is_refused(self):
        result = self.xreview("prepare", "HEAD")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no commits after", result.stderr)

    def test_names_get_suffixes_and_exclude_entry_is_added_once(self):
        names = [self.prepare().name for _ in range(3)]
        names += [self.prepare("--name", "fix").name for _ in range(2)]
        self.assertEqual(names, ["main", "main-2", "main-3", "fix", "fix-2"])
        exclude = (self.repo / ".git/info/exclude").read_text().splitlines()
        self.assertEqual(exclude.count(".reviews/"), 1)

    def test_exclude_entry_goes_on_its_own_line(self):
        exclude = self.repo / ".git/info/exclude"
        exclude.write_text("*.log")
        self.prepare()
        self.assertEqual(exclude.read_text(), "*.log\n.reviews/\n")

    def test_detached_head_gets_a_name(self):
        self.git("checkout", "-q", "--detach")
        short = self.git("rev-parse", "--short", "HEAD").strip()
        self.assertEqual(self.prepare().name, f"review-{short}")

    def test_names_outside_the_pattern_are_refused(self):
        for name in ("../x", "a/b", "-x", ".hidden", "x y"):
            with self.subTest(name):
                result = self.xreview("prepare", self.base, f"--name={name}")
                self.assertEqual(result.returncode, 1)
                self.assertFalse((self.repo / ".reviews").exists())
        self.git("checkout", "-q", "-b", "fix/a+b")
        result = self.xreview("prepare", self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn("--name", result.stderr)
        self.assertEqual(self.prepare("--name", "fix-ab").name, "fix-ab")


class CheckTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.review = self.prepare()
        self.block = cli.simplification_block()

    def check(self) -> subprocess.CompletedProcess:
        return self.xreview("check", self.review.name)

    def test_block_is_the_whole_paragraph(self):
        self.assertTrue(self.block.startswith("Simplification. Measure in"))
        self.assertTrue(self.block.endswith("don't propose it."))

    def test_passes_on_a_valid_review(self):
        self.write_prompt(self.review, self.block)
        result = self.check()
        self.assertEqual((result.returncode, result.stdout), (0, "ok\n"),
                         result.stderr)

    def test_tree_check(self):
        self.write_prompt(self.review, self.block)
        (self.repo / "scratch.txt").write_text("untracked\n")
        self.assertEqual(self.check().returncode, 0)
        (self.repo / "a.txt").write_text("edited\n")
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("a.txt", result.stderr)
        self.git("checkout", "--", "a.txt")
        self.commit("c.txt", "new\n", "Later commit")
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("HEAD has moved", result.stderr)

    def test_prompt_check_accepts_reflowed_and_blockquoted_copies(self):
        intro, *bullets = self.block.split(" - ")
        parts = [textwrap.fill(part, 72) for part in [intro, *bullets]]
        reflowed = "\n- ".join(parts)
        quoted = "\n".join("> " + line for line in reflowed.splitlines())
        for label, block in (("reflowed", reflowed), ("quoted", quoted)):
            with self.subTest(label):
                self.write_prompt(self.review, block)
                result = self.check()
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_prompt_check_accepts_seven_character_prefixes(self):
        shas = (self.base[:7], self.head[:7])
        self.write_prompt(self.review, self.block, shas)
        self.assertEqual(self.check().returncode, 0)

    def test_prompt_check_rejects_paraphrase_and_missing_commits(self):
        paraphrase = self.block.replace("deleting them", "removing them")
        cases = (
            ("paraphrased", paraphrase, None, "Simplification block"),
            ("no base", self.block, (self.head,), "base commit"),
            ("short base", self.block, (self.base[:6], self.head),
             "base commit"),
        )
        for label, block, shas, message in cases:
            with self.subTest(label):
                self.write_prompt(self.review, block, shas)
                result = self.check()
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_missing_prompt_fails(self):
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("reviewer-prompt.md is missing", result.stderr)

    def test_unknown_review_is_refused(self):
        for name in ("nosuch", "../.."):
            with self.subTest(name):
                result = self.xreview("check", name)
                self.assertEqual(result.returncode, 1)
                self.assertIn("no review named", result.stderr)


class PromptTests(RepoCase):
    def test_fills_base_and_head(self):
        review = self.prepare()
        result = self.xreview("prompt", review.name,
                              "--template", "code/reviewer-prompt-guide")
        self.assertEqual(result.returncode, 0, result.stderr)
        guide = ROOT / "templates/code/reviewer-prompt-guide.md"
        expected = guide.read_text().replace("{base}", self.base)
        expected = expected.replace("{head}", self.head).rstrip("\n") + "\n"
        self.assertEqual(result.stdout, expected)

    def test_name_needed_only_for_placeholders(self):
        result = self.xreview("prompt", "--template", "shared/protocol")
        self.assertEqual(result.returncode, 0, result.stderr)
        protocol = ROOT / "templates/shared/protocol.md"
        self.assertEqual(result.stdout, protocol.read_text())
        result = self.xreview("prompt", "--template",
                              "code/reviewer-prompt-guide")
        self.assertEqual(result.returncode, 1)
        self.assertIn("needs a review name", result.stderr)

    def test_templates_outside_templates_folder_are_refused(self):
        (self.tmp / "secret.md").write_text("SECRET\n")
        names = (str(self.tmp / "secret"),
                 os.path.relpath(self.tmp / "secret", ROOT / "templates"),
                 "code/no-such-template")
        for name in names:
            with self.subTest(name):
                result = self.xreview("prompt", "--template", name)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("SECRET", result.stdout)
                self.assertIn("no template", result.stderr)

    def test_usage_errors_exit_2(self):
        for args in (("prepare",), ("bogus",), ("prompt", "main")):
            with self.subTest(args):
                self.assertEqual(self.xreview(*args).returncode, 2)


class LauncherTests(RepoCase):
    TOO_OLD = "#!/bin/sh\nexit 1\n"

    def run_with(self, tools: dict[str, str]) -> subprocess.CompletedProcess:
        """Run `xreview prompt` with PATH holding git and the given tools."""
        folder = self.tmp / "launcher-bin"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir()
        (folder / "git").symlink_to(shutil.which("git"))
        for name, text in tools.items():
            write_exe(folder / name, text)
        return self.xreview("prompt", "--template", "shared/protocol",
                            PATH=str(folder))

    def test_skips_a_python_that_is_too_old(self):
        good = f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'
        uv = f'#!/bin/sh\necho "{self.tools / "python3"}"\n'
        cases = (("python3.N on PATH", {"python3.12": good}),
                 ("uv's choice", {"uv": uv}))
        for label, tools in cases:
            with self.subTest(label):
                result = self.run_with({"python3": self.TOO_OLD, **tools})
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_reports_when_no_python_is_found(self):
        result = self.run_with({"python3": self.TOO_OLD})
        self.assertEqual(result.returncode, 1)
        self.assertIn("needs Python 3.10 or later", result.stderr)


if __name__ == "__main__":
    unittest.main()
