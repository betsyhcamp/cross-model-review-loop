"""xreview command line: prepare, check, start, continue, prompt, append.

Exit codes: 0 success, 1 failed precondition or check, 2 usage error
(argparse's own code), 3 the reviewer call failed or its response was
rejected. Error messages go to stderr, prefixed "xreview:".
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import time

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


def fill(text: str, state: dict) -> str:
    text = text.replace("{base}", state["base"])
    return text.replace("{head}", state["reviewed_head"])


def review_file(review: Path, given: str) -> Path:
    """Resolve a --body or --file path against the review folder.

    A path outside the folder is refused, so the recommended allow rule
    can't be used to send any local file to Codex without a prompt.
    """
    path = (review / given).resolve()
    if not path.is_relative_to(review.resolve()) or not path.is_file():
        raise CheckFailed(f"{given!r} isn't a file inside {review}/.")
    return path


def subject(sha: str) -> str:
    return git_out("log", "-1", "--format=%s", sha).strip()


def head() -> str:
    return git_out("rev-parse", "HEAD").strip()


def uncommitted() -> str:
    """The `git status` lines of tracked files with uncommitted changes (D7),
    or "" if there are none. Untracked files are ignored."""
    return git_out("status", "--porcelain", "--untracked-files=no").rstrip()


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
    changes = uncommitted()
    if changes:
        raise CheckFailed(
            "uncommitted changes to tracked files; commit or stash them "
            f"first:\n{changes}"
        )

    name = unused_name(name or default_name())
    exclude_reviews()
    review = REVIEWS / name
    review.mkdir(parents=True)
    (review / "messages").mkdir()
    head_sha = head()
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
    """The tree check (§8.4) on an existing review: HEAD is still the
    reviewed head, and tracked files are clean. Each problem says what to
    do about it."""
    problems = []
    reviewed, now = state["reviewed_head"], head()
    if now != reviewed:
        problems.append(
            f"HEAD has moved since prepare (reviewed {reviewed[:7]}, now "
            f"{now[:7]}). This review covers {reviewed[:7]} only. To review "
            "the new commits, re-run code-prepare in the session that wrote "
            "the code."
        )
    changes = uncommitted()
    if changes:
        problems.append(
            "uncommitted changes to tracked files; stash them to keep this "
            f"review (a commit would move HEAD):\n{changes}"
        )
    return problems


def tree_changes(state: dict) -> list[str]:
    """What changed during a reviewer call (D21), with no remedy: the change
    may be the reviewer's, so committing it would be wrong."""
    changes = []
    reviewed, now = state["reviewed_head"], head()
    if now != reviewed:
        changes.append(f"HEAD moved during the call (was {reviewed[:7]}, "
                       f"now {now[:7]}).")
    lines = uncommitted()
    if lines:
        changes.append(f"Tracked files changed during the call:\n{lines}")
    return changes


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


# --- start and continue ----------------------------------------------------


def start(name: str) -> None:
    """Send reviewer-prompt.md, unchanged, to a new Codex session (Step 2)."""
    enter_repo()
    review = review_dir(name)
    state = transcript.read_state(review)
    if state["reviewer_session_id"]:
        raise CheckFailed(
            f"review {name!r} has already started (Codex session "
            f"{state['reviewer_session_id']}); start runs once per review."
        )
    problems = tree_problems(state) + prompt_problems(review, state)
    if problems:
        raise CheckFailed("\n".join(problems))
    message = (review / "reviewer-prompt.md").read_text(encoding="utf-8")
    call_reviewer(review, state, message,
                  "Reviewer prompt — sent to Codex", "Step 2 — Codex review")


def continue_review(name: str, templates: list[str], body: str,
                    sent: str, received: str) -> None:
    """Send templates plus Claude's body to the stored Codex session."""
    enter_repo()
    review = review_dir(name)
    state = transcript.read_state(review)
    if not state["reviewer_session_id"]:
        raise CheckFailed(
            f"review {name!r} has no Codex session yet; run start first."
        )
    text = review_file(review, body).read_text(encoding="utf-8")
    message = compose(templates, state, text)
    problems = tree_problems(state)
    if problems:
        raise CheckFailed("\n".join(problems))
    call_reviewer(review, state, message, sent, received)


def compose(templates: list[str], state: dict, body: str) -> str:
    """The templates in order, then the body, one blank line apart (§8.2).

    Placeholders are filled in the templates only; the body is never changed.
    """
    parts = [fill(read_template(name), state) for name in templates]
    return "\n\n".join(part.rstrip("\n") for part in [*parts, body]) + "\n"


def call_reviewer(review: Path, state: dict, message: str,
                  sent: str, received: str) -> None:
    """Send message to Codex and record both sides in the transcript.

    Starts a session if none is stored, otherwise resumes it. The tree check
    runs again when Codex exits (D21). A failed call or a rejected response
    gets a (FAILED) section, listing anything that changed in the tree, and
    raises ReviewerError (exit 3). Only a status line and the response path
    are printed, never the response itself.
    """
    number = transcript.attempt(review, sent)
    if number > 1:
        sent += f" (attempt {number})"
        received += f" (attempt {number})"
    run_dir = transcript.new_run(review, received)
    run = run_dir.name[:2]
    (run_dir / "prompt.md").write_text(message, encoding="utf-8")
    transcript.append(review, sent, message, f"{transcript.now()} · run {run}")

    began = time.monotonic()
    session = state["reviewer_session_id"]
    error = None
    try:
        if session is None:
            session, response = codex.start(message, Path.cwd(), run_dir)
        else:
            response = codex.resume(session, message, Path.cwd(), run_dir)
    except codex.ReviewerError as exc:
        error = str(exc)
    changed = tree_changes(state)
    if error or changed:
        if error:
            summary = f"{codex.DISPLAY_NAME} call failed: {error}"
        else:
            summary = ("The response was rejected: the tree changed during "
                       "the call. Restore it before any retry.")
        details = "\n\n".join([summary, *changed,
                               f"Diagnostics: `{run_dir}/`"])
        transcript.append(review, f"{received} (FAILED)", details,
                          f"{transcript.now()} · run {run}")
        raise codex.ReviewerError(
            f"{details}\nThe transcript has a (FAILED) section for run {run}."
        )

    if state["reviewer_session_id"] is None:
        state["reviewer_session_id"] = session
        transcript.write_state(review, state)
    took = duration(time.monotonic() - began)
    transcript.append(review, received, response,
                      f"{transcript.now()} · run {run} · {took}")
    print(f"{received}: done in {took} (run {run}).")
    print(f"Response: {Path.cwd() / run_dir / 'response.md'}")


def duration(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


# --- prompt and append ------------------------------------------------------


def prompt(name: str | None, template: str) -> None:
    """Print a template for Claude to follow, with {base} and {head} filled."""
    enter_repo()
    text = read_template(template)
    if "{base}" in text or "{head}" in text:
        if name is None:
            raise CheckFailed(f"template {template!r} needs a review name.")
        text = fill(text, transcript.read_state(review_dir(name)))
    print(text.rstrip("\n"))


def append(name: str, heading: str, file: str) -> None:
    """Append one of Claude's sections to the transcript, word for word."""
    enter_repo()
    review = review_dir(name)
    text = review_file(review, file).read_text(encoding="utf-8")
    transcript.append(review, heading, text, transcript.now())
    print(f"Appended \"{heading}\" to {review / transcript.TRANSCRIPT}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="xreview", description="Cross-model review runner."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="check git, create the review folder")
    p.add_argument("base", help="base commit; the review covers <base>..HEAD")
    p.add_argument("--name", help="review name (default: the branch name)")
    p.set_defaults(run=lambda a: prepare(a.base, a.name))

    p = sub.add_parser("check", help="run the tree check and the prompt check")
    p.add_argument("name")
    p.set_defaults(run=lambda a: check(a.name))

    p = sub.add_parser("start", help="send the reviewer prompt to Codex")
    p.add_argument("name")
    p.set_defaults(run=lambda a: start(a.name))

    p = sub.add_parser("continue", help="send a message to the same session")
    p.add_argument("name")
    p.add_argument("--template", action="append", required=True,
                   help="name under templates/, without .md; repeatable")
    p.add_argument("--body", required=True,
                   help="Claude's file, relative to .reviews/<name>/")
    p.add_argument("--sent-heading", required=True)
    p.add_argument("--response-heading", required=True)
    p.set_defaults(run=lambda a: continue_review(
        a.name, a.template, a.body, a.sent_heading, a.response_heading))

    p = sub.add_parser("prompt", help="print a template for Claude to follow")
    p.add_argument("name", nargs="?")
    p.add_argument("--template", required=True,
                   help="name under templates/, without .md")
    p.set_defaults(run=lambda a: prompt(a.name, a.template))

    p = sub.add_parser("append", help="append Claude's section to transcript")
    p.add_argument("name")
    p.add_argument("--heading", required=True)
    p.add_argument("--file", required=True,
                   help="Claude's file, relative to .reviews/<name>/")
    p.set_defaults(run=lambda a: append(a.name, a.heading, a.file))

    args = parser.parse_args(argv)
    try:
        args.run(args)
    except CheckFailed as exc:
        print(f"xreview: {exc}", file=sys.stderr)
        return 1
    except codex.ReviewerError as exc:
        print(f"xreview: {exc}", file=sys.stderr)
        return 3
    return 0
