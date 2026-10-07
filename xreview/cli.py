"""xreview command line: prepare, check and prompt.

Exit codes: 0 success, 1 failed precondition or check, 2 usage error
(argparse's own code). Error messages go to stderr, prefixed "xreview:".
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

from . import transcript
from .providers import codex

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
REVIEWS = Path(".reviews")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
BLOCK_MARKER = "Add this block to the prompt verbatim:"
SHA_LIKE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{7,40}(?![0-9A-Fa-f])")


class CheckFailed(Exception):
    """A failed precondition or check; exits 1."""


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def git_out(*args: str) -> str:
    result = git(*args)
    if result.returncode != 0:
        raise CheckFailed(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def enter_repo() -> None:
    """Change to the repo root, so every subcommand works from a subfolder."""
    result = git("rev-parse", "--show-toplevel")
    if result.returncode != 0:
        raise CheckFailed("not inside a git repository.")
    os.chdir(result.stdout.strip())


def review_dir(name: str) -> Path:
    review = REVIEWS / name
    if not NAME.fullmatch(name) or not (review / transcript.STATE).is_file():
        raise CheckFailed(
            f"no review named {name!r} in {REVIEWS}/; run code-prepare first."
        )
    return review


def read_template(name: str) -> str:
    """Read templates/<name>.md. A name resolving outside it is refused."""
    path = (TEMPLATES / f"{name}.md").resolve()
    if not path.is_relative_to(TEMPLATES) or not path.is_file():
        raise CheckFailed(f"no template {name!r} in templates/.")
    return path.read_text(encoding="utf-8")


def subject(sha: str) -> str:
    return git_out("log", "-1", "--format=%s", sha).strip()


def uncommitted() -> str | None:
    """A failure message if tracked files have uncommitted changes (D7).

    Untracked files are ignored.
    """
    changes = git_out("status", "--porcelain", "--untracked-files=no")
    if changes.strip():
        return (
            "uncommitted changes to tracked files; commit or stash them "
            f"first:\n{changes.rstrip()}"
        )
    return None


# --- prepare ----------------------------------------------------------------


def prepare(base: str, name: str | None) -> None:
    try:
        reviewer = f"{codex.DISPLAY_NAME} ({codex.version()})"
    except codex.ReviewerError as exc:
        raise CheckFailed(
            f"{exc} Install with `brew install --cask codex`, "
            "then `codex login`."
        ) from exc
    enter_repo()
    resolved = git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    if resolved.returncode != 0:
        raise CheckFailed(f"{base!r} is not a commit.")
    base_sha = resolved.stdout.strip()
    if git("merge-base", "--is-ancestor", base_sha, "HEAD").returncode != 0:
        raise CheckFailed(f"{base} is not an ancestor of HEAD.")
    if int(git_out("rev-list", "--count", f"{base_sha}..HEAD")) == 0:
        raise CheckFailed(f"there are no commits after {base}.")
    problem = uncommitted()
    if problem:
        raise CheckFailed(problem)

    name = unused_name(name or default_name())
    exclude_reviews()
    review = REVIEWS / name
    review.mkdir(parents=True)
    head_sha = git_out("rev-parse", "HEAD").strip()
    base_commit = (base_sha, subject(base_sha))
    head_commit = (head_sha, subject(head_sha))
    transcript.create(review, name, reviewer, base_commit, head_commit)
    print_summary(name, review, base_commit, head_commit)


def default_name() -> str:
    """The branch name with / replaced by -; review-<shortsha> if detached."""
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD")
    if branch.returncode != 0:
        return "review-" + git_out("rev-parse", "--short", "HEAD").strip()
    return branch.stdout.strip().replace("/", "-")


def unused_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise CheckFailed(
            f"review name {name!r} must match {NAME.pattern}; "
            "pass a valid --name."
        )
    candidate, n = name, 2
    while (REVIEWS / candidate).exists():
        candidate, n = f"{name}-{n}", n + 1
    return candidate


def exclude_reviews() -> None:
    """List .reviews/ in the repo's info/exclude, once (D17).

    git rev-parse --git-path gives the right file in linked worktrees too.
    """
    exclude = Path(git_out("rev-parse", "--git-path", "info/exclude").strip())
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if ".reviews/" in text.splitlines():
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    separator = "\n" if text and not text.endswith("\n") else ""
    with exclude.open("a", encoding="utf-8") as f:
        f.write(separator + ".reviews/\n")


def print_summary(
    name: str, review: Path, base: tuple[str, str], head: tuple[str, str]
) -> None:
    span = f"{base[0]}..{head[0]}"
    commits = git_out("log", "--oneline", span).rstrip("\n")
    files = git_out("diff", "--name-status", span).rstrip("\n")
    untracked = git_out("ls-files", "--others", "--exclude-standard")
    print(f"## xreview review `{name}`\n")
    print(f"- Folder: `{review}/`")
    print(f"- Base: `{base[0][:7]}` {base[1]}")
    print(f"- Reviewed head: `{head[0][:7]}` {head[1]}\n")
    print(f"### Commits ({len(commits.splitlines())})\n")
    print(f"```\n{commits}\n```\n")
    print(f"### Changed files\n\n```\n{files}\n```\n")
    print("### Untracked files: not included in this review\n")
    paths = untracked.splitlines()
    print("\n".join(f"- `{path}`" for path in paths) if paths else "None.")


# --- check ------------------------------------------------------------------


def check(name: str) -> None:
    enter_repo()
    review = review_dir(name)
    state = transcript.read_state(review)
    problems = tree_problems(state) + prompt_problems(review, state)
    if problems:
        raise CheckFailed("\n".join(problems))
    print("ok")


def tree_problems(state: dict) -> list[str]:
    """The tree check (§8.4): HEAD is still the reviewed head, and tracked
    files are clean."""
    problems = []
    head = git_out("rev-parse", "HEAD").strip()
    reviewed = state["reviewed_head"]
    if head != reviewed:
        problems.append(
            f"HEAD has moved since prepare (reviewed {reviewed[:7]}, "
            f"now {head[:7]}); re-run code-prepare."
        )
    problem = uncommitted()
    if problem:
        problems.append(problem)
    return problems


def prompt_problems(review: Path, state: dict) -> list[str]:
    """The prompt check (§8.4): the Simplification block word for word, and
    both commit SHAs."""
    path = review / "reviewer-prompt.md"
    if not path.is_file():
        return [f"{path} is missing."]
    text = path.read_text(encoding="utf-8")
    problems = []
    if normalize(simplification_block()) not in normalize(text):
        problems.append(
            f"{path} doesn't contain the Simplification block word for word."
        )
    named = {h.lower() for h in SHA_LIKE.findall(text)}
    commits = (("base", state["base"]), ("head", state["reviewed_head"]))
    for label, sha in commits:
        if not any(sha.startswith(h) for h in named):
            problems.append(
                f"{path} doesn't name the {label} commit ({sha[:7]}) in full "
                "or as a prefix of at least 7 characters."
            )
    return problems


def simplification_block() -> str:
    """The text after the marker in the reviewer-prompt guide, to the end of
    that paragraph."""
    guide = read_template("code/reviewer-prompt-guide")
    return guide.split(BLOCK_MARKER, 1)[1].split("\n\n", 1)[0].strip()


def normalize(text: str) -> str:
    """Remove blockquote markers at line starts, then collapse each run of
    whitespace to one space."""
    unquoted = re.sub(r"^[ \t]*(?:>[ \t]?)+", "", text, flags=re.M)
    return " ".join(unquoted.split())


# --- prompt -----------------------------------------------------------------


def prompt(name: str | None, template: str) -> None:
    """Print a template for Claude to follow, with {base} and {head} filled."""
    enter_repo()
    text = read_template(template)
    if "{base}" in text or "{head}" in text:
        if name is None:
            raise CheckFailed(f"template {template!r} needs a review name.")
        state = transcript.read_state(review_dir(name))
        text = text.replace("{base}", state["base"])
        text = text.replace("{head}", state["reviewed_head"])
    print(text.rstrip("\n"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="xreview", description="Cross-model review runner."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="check git, create the review folder")
    p.add_argument("base", help="base commit; the review covers <base>..HEAD")
    p.add_argument("--name", help="review name (default: the branch name)")
    p = sub.add_parser("check", help="run the tree check and the prompt check")
    p.add_argument("name")
    p = sub.add_parser("prompt", help="print a template for Claude to follow")
    p.add_argument("name", nargs="?")
    p.add_argument(
        "--template", required=True, help="name under templates/, without .md"
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args.base, args.name)
        elif args.command == "check":
            check(args.name)
        else:
            prompt(args.name, args.template)
    except CheckFailed as exc:
        print(f"xreview: {exc}", file=sys.stderr)
        return 1
    return 0
