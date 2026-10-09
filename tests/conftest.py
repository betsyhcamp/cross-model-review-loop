"""Shared fixtures for the tests.

Each test gets its own copy of a temporary git repo with two commits after
a base commit. PATH holds only git, a python3 that runs this test's
interpreter, and a codex shim that runs tests/fake_codex.py. There are no
model calls.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
XREVIEW = ROOT / "bin" / "xreview"
FAKE_CODEX = ROOT / "tests" / "fake_codex.py"
sys.path.insert(0, str(ROOT))


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


@dataclass
class Shared:
    """The shims and the template repo, built once per session and copied
    per test: macOS adds ~0.1s to the first run of each new executable, and
    git calls are slow too."""

    tools: Path  # git and python3, without codex
    template: Path
    env: dict[str, str]
    base: str
    head: str


@pytest.fixture(scope="session")
def shared(tmp_path_factory: pytest.TempPathFactory) -> Shared:
    root = tmp_path_factory.mktemp("shared")
    tools, codex_dir = root / "tools", root / "codex-bin"
    template = root / "repo"
    for folder in (tools, codex_dir, template):
        folder.mkdir()
    (tools / "git").symlink_to(shutil.which("git"))
    write_exe(tools / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    write_exe(codex_dir / "codex",
              f'#!/bin/sh\nexec python3 "{FAKE_CODEX}" "$@"\n')
    env = {
        **os.environ,
        "PATH": f"{codex_dir}:{tools}",
        "HOME": str(root),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    env.pop("FAKE_CODEX_CASE", None)
    run_git(template, env, "init", "-q", "-b", "main")
    base = commit(template, env, "a.txt", "one\n", "Base commit")
    commit(template, env, "b.txt", "two\n", "Add b")
    head = commit(template, env, "a.txt", "one\nmore\n", "Extend a")
    return Shared(tools, template, env, base, head)


class Repo:
    """A test's own copy of the template repo, with helpers to run git and
    bin/xreview in it."""

    def __init__(self, shared: Shared, tmp: Path):
        self.tools, self.env = shared.tools, shared.env
        self.base, self.head = shared.base, shared.head
        self.tmp = tmp
        self.path = tmp / "repo with spaces"
        shutil.copytree(shared.template, self.path, symlinks=True)

    def git(self, *args: str) -> str:
        return run_git(self.path, self.env, *args)

    def commit(self, path: str, text: str, message: str) -> str:
        return commit(self.path, self.env, path, text, message)

    def xreview(self, *args: str, cwd: Path | None = None,
                **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(XREVIEW), *args], cwd=cwd or self.path,
                              env={**self.env, **env},
                              capture_output=True, text=True)

    def prepare(self, *args: str) -> Path:
        """Run prepare on the base commit; return the review folder."""
        before = set((self.path / ".reviews").glob("*"))
        result = self.xreview("prepare", self.base, *args)
        assert result.returncode == 0, result.stderr
        (review,) = set((self.path / ".reviews").glob("*")) - before
        return review

    def write_prompt(self, review: Path, block: str,
                     shas: tuple[str, ...] | None = None) -> None:
        refs = " and ".join(shas or (self.base, self.head))
        (review / "reviewer-prompt.md").write_text(
            f"Review the commits {refs}.\n\n{block}\n\n"
            "You have read-only access.\n")


@pytest.fixture
def repo(shared: Shared, tmp_path: Path) -> Repo:
    return Repo(shared, tmp_path)


@pytest.fixture
def process_gone():
    """A check that a process has exited, waiting up to `wait` seconds."""

    def gone(pid: int, wait: float = 5.0) -> bool:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False

    return gone
