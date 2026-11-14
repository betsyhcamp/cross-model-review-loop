"""xreview status: where a review stands, on one screen.

It shows the step being worked on and for how long, the working reviewer's
tool count and last few actions, the most recent finished steps with their
times, anything uncertain or failed with what to say next, and permission
refusals counted apart from ordinary failures. At a stop it adds the
commands that open each reviewer directly, with the review's limits.
Opening one mid-step is unsafe: Claude would take a second writer and
process a message twice. Status reads the review without changing it,
except to restart the stop's wake hold.
"""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path

from . import cli, procs, runner, wake
from .providers import claude, codex

FINISHED_SHOWN = 5


def duration(seconds: float) -> str:
    return cli.duration(max(0, seconds))


def owner(review: Path) -> int | None:
    """The process ID of the command working on the review, if any."""
    try:
        held = json.loads((review / runner.LOCK).read_text())
    except (OSError, ValueError):
        return None
    if procs.alive(held.get("pid"), held.get("started")):
        return held["pid"]
    return None


def counts(review: Path, rec: dict) -> dict:
    module = claude if rec.get("provider") == "claude" else codex
    return module.stats(review / rec["run"])


def lines_for(job: runner.Runner) -> list[str]:
    st, review = job.st, job.review
    steps = [rec for rec in st["steps"].values() if rec.get("provider")]
    working = owner(review)
    lines = []
    launched = sorted(
        (r for r in steps if r["status"] == runner.LAUNCHED),
        key=lambda r: r["began"],
    )
    if working and launched:
        rec = launched[-1]
        took = duration(time.time() - rec["began"])
        lines.append(
            f"Review {job.name}: working on {rec['label']}, {took} so far."
        )
        found = counts(review, rec)
        who = "Claude" if rec["provider"] == "claude" else "Codex"
        what = "tool calls" if who == "Claude" else "commands run"
        last = "; ".join(found["actions"]) or "none yet"
        lines.append(f"{who}: {found['tools']} {what}. Last: {last}")
    elif working:
        lines.append(
            f"Review {job.name}: a command is running, between steps."
        )
    elif not steps:
        lines.append(f"Review {job.name}: prepared, not started.")
    elif st["stopped"] and not st["op"]:
        count = len(st["decisions"])
        plural = "decision" if count == 1 else "decisions"
        lines.append(
            f"Review {job.name}: at a stop, with {count or 'no'} open "
            f"{plural}."
        )
    else:
        lines.append(
            f'Review {job.name}: stopped before finishing. Say "continue" '
            "to pick it up."
        )
    finished = sorted(
        (r for r in steps if r["status"] == runner.DONE),
        key=lambda r: r.get("ended", 0),
    )
    if finished:
        earlier = len(finished) - FINISHED_SHOWN
        heading = "Finished"
        if earlier > 0:
            heading += f" (latest {FINISHED_SHOWN} of {len(finished)})"
        lines.append(f"{heading}:")
        for rec in finished[-FINISHED_SHOWN:]:
            took = duration(rec.get("ended", rec["began"]) - rec["began"])
            lines.append(f"  {rec['label']}, {took}")
    if not working:
        for rec in steps:
            if rec["status"] == runner.UNCERTAIN:
                lines.append(
                    f'Uncertain: {rec["label"]} may have run. Say "retry" to '
                    "send it again."
                )
            elif rec["status"] == runner.FAILED:
                lines.append(
                    f'Failed: {rec["label"]}. Say "retry" to try it again.'
                )
    for label in st.get("dropped", [])[-2:]:
        lines.append(f"Dropped: {label}, with nothing resent.")
    if st.get("held"):
        lines.append(
            f"Held: a Decided Plan with {st['held']['groups']} groups, "
            "waiting for your choice."
        )
    elif (review / "decided-plan.md").exists():
        lines.append("A Decided Plan is recorded.")
    lines.append(refusals(review, steps))
    if not working and st["stopped"] and not st["op"]:
        lines += reopen(job)
    return lines


def refusals(review: Path, steps: list[dict]) -> str:
    totals = {"Claude": [0, 0], "Codex": [0, 0]}
    for rec in steps:
        found = counts(review, rec)
        who = "Claude" if rec["provider"] == "claude" else "Codex"
        totals[who][0] += found["refused"]
        totals[who][1] += found["failed"]
    return (
        f"Permission refusals: Claude {totals['Claude'][0]}, Codex "
        f"{totals['Codex'][0]}. Other failed commands: Claude "
        f"{totals['Claude'][1]}, Codex {totals['Codex'][1]}."
    )


def reopen(job: runner.Runner) -> list[str]:
    """The commands to open each reviewer at the desk, with the review's
    limits: Session B's sandbox, tools and mode, and Codex read-only."""
    commands = []
    session = job.st.get("claude_session")
    if job.st.get("claude_started") and session:
        settings = Path.cwd() / job.review / "session-b-settings.json"
        settings.write_text(
            json.dumps(
                claude.settings(
                    Path.cwd(),
                    session,
                    [],
                    job.review.resolve(),
                    job.shared_paths(),
                ),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        commands.append(
            "  CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 claude --resume "
            f"{session} --settings {shlex.quote(str(settings))} --tools "
            f"{claude.TOOLS} --allowedTools {claude.ALLOWED_TOOLS} "
            "--permission-mode auto --strict-mcp-config --setting-sources="
        )
    thread = job.state.get("reviewer_session_id")
    if thread:
        commands.append(f"  codex resume {thread} -s read-only")
    if not commands:
        return []
    return [
        "To open a reviewer at your desk, only while the review is at a "
        "stop (chats there aren't recorded in the review):",
        *commands,
    ]


def status(name: str) -> None:
    job = runner.Runner(name)
    print("\n".join(lines_for(job)))
    # At a stop, asking where things stand counts as using the review, so
    # it restarts the stop's wake hold, unless a command is working.
    if job.st["stopped"] and not job.st["op"] and not owner(job.review):
        try:
            with runner.Lock(job.review):
                job.st = job.load()
                job.st["wake"] = wake.at_stop(job.st.get("wake"))
                job.save()
        except cli.CheckFailed:
            pass
