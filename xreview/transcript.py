"""Review folder state and the transcript header."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

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
