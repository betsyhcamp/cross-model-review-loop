# Portions adapted from claudex-loop
# (https://github.com/chaseai-yt/claudex-loop @ 8cf5e2c), Copyright (c) 2026
# Chase AI, MIT License. See THIRD_PARTY_NOTICES.md.
"""Fake `codex` for the tests, run through a `codex` shim on PATH.

FAKE_CODEX_CASE selects the behavior: ok | exit | timeout | no_thread |
wrong_thread | bad_uuid | dup_thread | dup_completed | turn_failed |
incomplete | nonfatal_error | nonjson | empty | edit_tracked |
edit_then_exit (edits a tracked file, then fails) | commit | create_untracked
| slow (runs one command, then takes a second to finish) | active_writer
(refuses a second writer, as codex-cli 0.160.1 does) | exit_after_start
(starts the turn, then exits nonzero).

Per call, FAKE_CODEX_CASES and FAKE_CODEX_ANSWERS are JSON objects mapping
part of the run folder's name (from the -o path) to a case or a response,
and FAKE_CODEX_SLEEPS to seconds to wait before responding, so a test can
kill the runner while Codex works.

FAKE_CODEX_LOG, if set, gets one JSON line per call with the argv, stdin and
working folder. With CODEX_HOME set, each response is also kept in
CODEX_HOME/sessions/rollout.jsonl, as real Codex keeps its sessions there.
FAKE_CODEX_PIDS, if set, gets the pids of this process and of a child it
starts (timeout case), so tests can check both were killed.
Events follow the shape of real codex-cli 0.160.1 output.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

SESSION = "01a11457-66e5-73e3-8c89-991f3b54c198"
OTHER_SESSION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RESPONSE = "Finding 1: nothing real.\n\nFAKE-RESPONSE-BODY\n"


def for_call(variable, default):
    """The value for this call from a JSON object keyed by part of the run
    folder's name."""
    choices = json.loads(os.environ.get(variable) or "{}")
    folder = ""
    if "-o" in sys.argv:
        folder = Path(sys.argv[sys.argv.index("-o") + 1]).parent.name
    # The longest part that matches wins, so "fix-up-decision" beats
    # "decision" for a folder named claude-fix-up-decision-copy.
    matches = [part for part in choices if part in folder]
    return choices[max(matches, key=len)] if matches else default


case = for_call("FAKE_CODEX_CASES", os.environ.get("FAKE_CODEX_CASE", "ok"))
if "--version" in sys.argv:
    if case == "exit":
        print("codex: broken install", file=sys.stderr)
        sys.exit(7)
    print("codex-cli 0.0.0-fake")
    sys.exit(0)

prompt = sys.stdin.read()
if os.environ.get("FAKE_CODEX_LOG"):
    call = {"argv": sys.argv[1:], "stdin": prompt, "cwd": os.getcwd()}
    with open(os.environ["FAKE_CODEX_LOG"], "a") as log:
        log.write(json.dumps(call) + "\n")


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


if case == "active_writer":
    thread = "01a1c369-80f1-7912-9b6d-2e554b06f31b"
    print(
        "ERROR codex_core::session: Failed to create session: thread-store "
        f"conflict: thread {thread} already has an active writer\n"
        f"Error: thread/resume: thread/resume failed: thread {thread} already "
        "has an active writer (code -32600)",
        file=sys.stderr,
    )
    sys.exit(1)
if case == "exit":
    for i in range(1, 26):
        print(f"stderr line {i}", file=sys.stderr)
    print("Authentication failed", file=sys.stderr)
    sys.exit(7)
if case == "timeout":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    if os.environ.get("FAKE_CODEX_PIDS"):
        Path(os.environ["FAKE_CODEX_PIDS"]).write_text(
            f"{os.getpid()} {child.pid}\n"
        )
    time.sleep(60)
if case in ("edit_tracked", "edit_then_exit"):
    Path("a.txt").write_text("changed by the reviewer\n")
if case == "edit_then_exit":
    print("Codex crashed after editing", file=sys.stderr)
    sys.exit(7)
if case == "commit":
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "Reviewer commit"],
        check=True,
    )
if case == "create_untracked":
    Path("reviewer-scratch.txt").write_text("scratch\n")

session = SESSION
if "resume" in sys.argv:
    session = sys.argv[sys.argv.index("resume") + 1]
if case == "wrong_thread":
    session = OTHER_SESSION
if case == "bad_uuid":
    session = "not-a-uuid"
time.sleep(float(for_call("FAKE_CODEX_SLEEPS", 0)))
output = Path(sys.argv[sys.argv.index("-o") + 1])
response = for_call("FAKE_CODEX_ANSWERS", RESPONSE)
output.write_text("" if case == "empty" else response)
if os.environ.get("CODEX_HOME"):
    sessions = Path(os.environ["CODEX_HOME"]) / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    with (sessions / "rollout.jsonl").open("a") as rollout:
        rollout.write(json.dumps({"response": response}) + "\n")

if case == "nonjson":
    print("Reading prompt from stdin...")
threads = {"no_thread": 0, "dup_thread": 2}.get(case, 1)
for _ in range(threads):
    emit({"type": "thread.started", "thread_id": session})
emit({"type": "turn.started"})
if case == "exit_after_start":
    print("stream disconnected", file=sys.stderr)
    sys.exit(1)
if case == "slow":
    emit(
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "ls",
                "exit_code": 0,
                "status": "completed",
            },
        }
    )
    time.sleep(1)
if case == "nonfatal_error":
    emit({"type": "error", "message": "Reconnecting... 1/5"})
emit(
    {
        "type": "item.completed",
        "item": {"id": "item_0", "type": "agent_message", "text": response},
    }
)
if case == "turn_failed":
    emit({"type": "turn.failed", "error": {"message": "quota exceeded"}})
completions = {"turn_failed": 0, "incomplete": 0, "dup_completed": 2}
for _ in range(completions.get(case, 1)):
    emit(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )
