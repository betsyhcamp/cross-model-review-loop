"""Review folder state, run folders and the append-only transcript."""

from __future__ import annotations

from datetime import datetime
import fcntl
import json
from pathlib import Path
import re

STATE = "state.json"
TRANSCRIPT = "transcript.md"


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_state(review: Path) -> dict:
    return json.loads((review / STATE).read_text(encoding="utf-8"))


def write_state(review: Path, state: dict) -> None:
    (review / STATE).write_text(
        json.dumps(state, indent=2) + "\n", encoding="utf-8"
    )


def create(
    review: Path,
    name: str,
    reviewer: str,
    base: tuple[str, str],
    head: tuple[str, str],
) -> None:
    """Write state.json and the transcript header.

    base and head are (sha, subject) pairs.
    """
    write_state(
        review,
        {
            "base": base[0],
            "reviewed_head": head[0],
            "reviewer_session_id": None,
        },
    )
    (review / TRANSCRIPT).write_text(
        f"# Review transcript — {name}\n\n"
        "- Review type: code\n"
        f"- Reviewer: {reviewer}\n"
        f"- Base: {base[0]} — {base[1]}\n"
        f"- Reviewed head: {head[0]} — {head[1]}\n"
        f"- Started: {now()}\n\n---\n\n",
        encoding="utf-8",
    )


def append(review: Path, heading: str, body: str, meta: str) -> None:
    """Append one section, word for word, holding an exclusive lock.

    A background Codex call and Claude's own append can overlap; the lock
    keeps each section intact but doesn't decide their order.
    """
    text = body.rstrip("\n")
    with (review / TRANSCRIPT).open("a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(f"## {heading}\n_{meta}_\n\n{text}\n\n---\n\n")


def attempt(review: Path, heading: str) -> int:
    """1 the first time a message is sent under heading, 2 for a retry..."""
    lines = (review / TRANSCRIPT).read_text(encoding="utf-8").splitlines()
    sent = [line for line in lines if line == f"## {heading}"
            or line.startswith(f"## {heading} (attempt ")]
    return len(sent) + 1


def new_run(review: Path, heading: str) -> Path:
    """Create runs/NN-<slug>/ for one Codex call."""
    runs = review / "runs"
    runs.mkdir(exist_ok=True)
    number = sum(1 for path in runs.iterdir() if path.is_dir()) + 1
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    run_dir = runs / f"{number:02d}-{slug}"
    run_dir.mkdir()
    return run_dir
