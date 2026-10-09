# Portions adapted from claudex-loop
# (https://github.com/chaseai-yt/claudex-loop @ 8cf5e2c), Copyright (c) 2026
# Chase AI, MIT License. See THIRD_PARTY_NOTICES.md.
"""Codex adapter. This is the only module that runs `codex`."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

DISPLAY_NAME = "Codex"
TIMEOUT_SECONDS = 40 * 60
PROGRESS_SECONDS = 30
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


class ReviewerError(Exception):
    """A failed reviewer call.

    The message is user-facing; diagnostics stay in the run folder.
    """


class Stopped(Exception):
    """The runner received a stop signal while Codex was running."""


def version() -> str:
    """Return `codex --version` output, such as "codex-cli 0.160.1"."""
    if shutil.which("codex") is None:
        raise ReviewerError("Codex CLI not found on PATH.")
    result = subprocess.run(
        ["codex", "--version"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise ReviewerError(
            f"`codex --version` failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def start(prompt: str, repo: Path, run_dir: Path) -> tuple[str, str]:
    """Send prompt to a new read-only session; return (session_id, reply)."""
    run(command(run_dir), prompt, repo, run_dir)
    return parse(run_dir)


def resume(session_id: str, prompt: str, repo: Path, run_dir: Path) -> str:
    """Send prompt to an existing session; return the reply.

    Codex works in the folder it's resumed from, so repo must be the repo
    root, as it was for start.
    """
    run(command(run_dir, session_id), prompt, repo, run_dir)
    return parse(run_dir, session_id)[1]


def command(run_dir: Path, session_id: str | None = None) -> list[str]:
    # exec resume has no --sandbox option, so read-only is set through -c.
    if session_id:
        args = ["exec", "resume", session_id,
                "-c", 'sandbox_mode="read-only"']
    else:
        args = ["exec", "-s", "read-only"]
    args += ["-c", 'approval_policy="never"', "--json",
             "-o", str(run_dir / "response.md"), "-"]
    return ["codex", *args]


def run(argv: list[str], prompt: str, repo: Path, run_dir: Path) -> None:
    if shutil.which("codex") is None:
        raise ReviewerError("Codex CLI not found on PATH.")
    code = execute(argv, prompt, repo, run_dir)
    if code != 0:
        stderr = (run_dir / "stderr.txt").read_text(errors="replace")
        tail = "\n".join(stderr.splitlines()[-20:])
        raise ReviewerError(
            f"Codex exited with code {code}. Last lines of stderr:\n{tail}"
        )


def execute(argv: list[str], prompt: str, repo: Path, run_dir: Path) -> int:
    """Run Codex in its own process group, prompt on stdin, output to files.

    On a timeout or a stop signal (SIGTERM, SIGHUP, SIGINT), kill the whole
    group, so no Codex turn keeps running after the runner gives up on it.
    """
    previous = {sig: signal.getsignal(sig) for sig in STOP_SIGNALS}
    for sig in STOP_SIGNALS:
        signal.signal(sig, stop)
    try:
        with (run_dir / "events.jsonl").open("wb") as out, \
                (run_dir / "stderr.txt").open("wb") as err:
            with subprocess.Popen(argv, cwd=repo, stdin=subprocess.PIPE,
                                  stdout=out, stderr=err,
                                  start_new_session=True) as proc:
                try:
                    return wait_for(proc, prompt, run_dir)
                except (subprocess.TimeoutExpired, Stopped) as exc:
                    # A stop from Claude Code signals the whole process tree
                    # at once, so Codex may already be dead. Not yet reaped,
                    # macOS reports EPERM for its group; once reaped, ESRCH.
                    with contextlib.suppress(PermissionError,
                                             ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    if isinstance(exc, Stopped):
                        raise ReviewerError(
                            f"The runner was stopped ({exc}); Codex was "
                            "killed.") from exc
                    raise ReviewerError(
                        f"Codex timed out after {TIMEOUT_SECONDS // 60} "
                        "minutes and was killed.") from exc
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def stop(signum: int, frame) -> None:
    raise Stopped(signal.Signals(signum).name)


def wait_for(proc: subprocess.Popen, prompt: str, run_dir: Path) -> int:
    """Send the prompt, then wait for Codex to exit.

    Reports progress every PROGRESS_SECONDS, and raises TimeoutExpired once
    TIMEOUT_SECONDS have passed.
    """
    began = time.monotonic()
    data: bytes | None = prompt.encode("utf-8")
    while True:
        left = TIMEOUT_SECONDS - (time.monotonic() - began)
        try:
            proc.communicate(data, timeout=max(0, min(PROGRESS_SECONDS, left)))
            return proc.returncode
        except subprocess.TimeoutExpired:
            if left <= PROGRESS_SECONDS:
                raise
        data = None  # communicate keeps sending what's left of the prompt
        report_progress(run_dir, time.monotonic() - began)


def report_progress(run_dir: Path, elapsed: float) -> None:
    """Print a progress line on stderr, with none of Codex's text (D10)."""
    events = (run_dir / "events.jsonl").read_text(errors="replace")
    done = sum(1 for line in events.splitlines()
               if '"item.completed"' in line and '"command_execution"' in line)
    minutes, seconds = divmod(round(elapsed), 60)
    print(f"xreview: {DISPLAY_NAME} still working — {minutes}m {seconds}s, "
          f"{done} command{'' if done == 1 else 's'} run",
          file=sys.stderr, flush=True)


def parse(run_dir: Path, expected_session: str | None = None
          ) -> tuple[str, str]:
    """Check the event stream and return (session_id, reply).

    A top-level "error" event isn't fatal when the turn completed: Codex
    emits one for a stream error it retries by itself (spec §8.3).
    """
    events = []
    stream = (run_dir / "events.jsonl").read_text(errors="replace")
    for line in stream.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReviewerError(
                "Codex printed a line that isn't JSON; see events.jsonl."
            ) from exc
        if not isinstance(event, dict):
            raise ReviewerError("Codex printed an event that isn't an object.")
        events.append(event)
    for event in events:
        if event.get("type") == "turn.failed":
            error = event.get("error")
            detail = error.get("message") if isinstance(error, dict) else None
            raise ReviewerError(f"Codex reported a failed turn: {detail}.")
    started = [e.get("thread_id") for e in events
               if e.get("type") == "thread.started"]
    completed = [e for e in events if e.get("type") == "turn.completed"]
    if len(started) != 1 or len(completed) != 1:
        raise ReviewerError(
            f"Codex sent {len(started)} thread.started and {len(completed)} "
            "turn.completed events; expected exactly one of each.")
    try:
        session = str(uuid.UUID(started[0]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReviewerError(
            "Codex did not return a valid session UUID.") from exc
    if expected_session and session != expected_session:
        raise ReviewerError(
            "Codex resumed a different session; refusing its result.")
    response = run_dir / "response.md"
    reply = response.read_text(encoding="utf-8") if response.exists() else ""
    if not reply.strip():
        raise ReviewerError("Codex returned an empty response.")
    return session, reply
