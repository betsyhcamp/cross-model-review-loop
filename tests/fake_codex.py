# Portions adapted from claudex-loop
# (https://github.com/chaseai-yt/claudex-loop @ 8cf5e2c), Copyright (c) 2026
# Chase AI, MIT License. See THIRD_PARTY_NOTICES.md.
"""Fake `codex` for the tests, run through a `codex` shim on PATH.

FAKE_CODEX_CASE selects the behavior: ok | exit | timeout | no_thread |
wrong_thread | bad_uuid | dup_thread | dup_completed | turn_failed |
incomplete | nonfatal_error | nonjson | empty | edit_tracked | commit |
create_untracked | slow (runs one command, then takes a second to finish).

FAKE_CODEX_LOG, if set, gets one JSON line per call with the argv, stdin and
working folder. FAKE_CODEX_PIDS, if set, gets the pids of this process and
of a child it starts (timeout case), so tests can check both were killed.
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

case = os.environ.get("FAKE_CODEX_CASE", "ok")
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


if case == "exit":
    for i in range(1, 26):
        print(f"stderr line {i}", file=sys.stderr)
    print("Authentication failed", file=sys.stderr)
    sys.exit(7)
if case == "timeout":
    child = subprocess.Popen([sys.executable, "-c",
                              "import time; time.sleep(60)"])
    if os.environ.get("FAKE_CODEX_PIDS"):
        Path(os.environ["FAKE_CODEX_PIDS"]).write_text(
            f"{os.getpid()} {child.pid}\n")
    time.sleep(60)
if case == "edit_tracked":
    Path("a.txt").write_text("changed by the reviewer\n")
if case == "commit":
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m",
                    "Reviewer commit"], check=True)
if case == "create_untracked":
    Path("reviewer-scratch.txt").write_text("scratch\n")

session = SESSION
if "resume" in sys.argv:
    session = sys.argv[sys.argv.index("resume") + 1]
if case == "wrong_thread":
    session = OTHER_SESSION
if case == "bad_uuid":
    session = "not-a-uuid"
output = Path(sys.argv[sys.argv.index("-o") + 1])
output.write_text("" if case == "empty" else RESPONSE)

if case == "nonjson":
    print("Reading prompt from stdin...")
threads = {"no_thread": 0, "dup_thread": 2}.get(case, 1)
for _ in range(threads):
    emit({"type": "thread.started", "thread_id": session})
emit({"type": "turn.started"})
if case == "slow":
    emit({"type": "item.completed",
          "item": {"id": "item_1", "type": "command_execution",
                   "command": "ls", "exit_code": 0, "status": "completed"}})
    time.sleep(1)
if case == "nonfatal_error":
    emit({"type": "error", "message": "Reconnecting... 1/5"})
emit({"type": "item.completed",
      "item": {"id": "item_0", "type": "agent_message", "text": RESPONSE}})
if case == "turn_failed":
    emit({"type": "turn.failed", "error": {"message": "quota exceeded"}})
completions = {"turn_failed": 0, "incomplete": 0, "dup_completed": 2}
for _ in range(completions.get(case, 1)):
    emit({"type": "turn.completed",
          "usage": {"input_tokens": 10, "output_tokens": 5}})
