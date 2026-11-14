"""Fake `claude -p` for the tests, run through a `claude` shim on PATH.

FAKE_CLAUDE_CASE selects the behavior: ok | startup_fail (exits before
starting) | not_signed_in (refused before writing anything) |
error_after_work (fails after a tool call) | no_result | wrong_session |
empty | timeout (sleeps) | mode_default (Auto switches off after startup,
as when it isn't available) | sandbox_broken (the shell command fails to
start its sandbox) | edit_tracked (edits a.txt) | slow (a tool call, then
a second's wait) | refused_tools (two refused tool calls and a failed one).

The answer is FAKE_CLAUDE_TEXT if set, else "FAKE-CLAUDE-ANSWER". A
preflight prompt (it names the sandbox mark) gets a Bash call that prints
the mark. Per step, FAKE_CLAUDE_CASES and FAKE_CLAUDE_ANSWERS are JSON
objects mapping part of the run folder's name (its settings file is in the
folder) to a case or an answer, and FAKE_CLAUDE_SLEEPS to seconds to wait
before answering, so a test can kill the runner while Claude works.

An answer of "@headings" echoes every `### Decision <n>: <title>` line in
the prompt, each with a body, as a merge or a fixed reply would;
"@headings-but-last" leaves the last one out.

FAKE_LEAK_MARK and FAKE_LEAK_ROOTS (a JSON list of folders) make each call
log "leaks": every file under those folders that holds the mark and that
the call's sandbox doesn't block from reading, and whether the prompt
holds it.

FAKE_CLAUDE_LOG, if set, gets one JSON line per call with the
argv, the settings file's contents, the prompt, the working folder and the
environment variables the tests look at. FAKE_CLAUDE_PIDS, if set, gets
this process's ID. Events follow the shape of Claude Code 2.1.319 output;
the init event reports the --model given, else the settings file's model,
else "fake-default-model".
"""

import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

MARK = "xreview-sandbox-ok"
WATCHED = (
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_USE_BEDROCK",
)

argv = sys.argv[1:]
prompt = sys.stdin.read()


def flag(name):
    return argv[argv.index(name) + 1] if name in argv else None


def for_step(variable, default):
    """The value for this step from a JSON object keyed by part of the run
    folder's name."""
    choices = json.loads(os.environ.get(variable) or "{}")
    folder = Path(settings_file).parent.name if settings_file else ""
    # The longest part that matches wins, so "fix-up-decision" beats
    # "decision" for a folder named claude-fix-up-decision-copy.
    matches = [part for part in choices if part in folder]
    return choices[max(matches, key=len)] if matches else default


session = flag("--session-id") or flag("--resume") or str(uuid.uuid4())
settings_file = flag("--settings")
case = for_step("FAKE_CLAUDE_CASES", os.environ.get("FAKE_CLAUDE_CASE", "ok"))
settings = (
    json.loads(Path(settings_file).read_text()) if settings_file else None
)


def leaks():
    """Files under FAKE_LEAK_ROOTS holding FAKE_LEAK_MARK that this call's
    sandbox doesn't block."""
    mark = os.environ.get("FAKE_LEAK_MARK")
    if not mark:
        return None
    denied = [
        Path(p)
        for p in (settings or {})
        .get("sandbox", {})
        .get("filesystem", {})
        .get("denyRead", [])
    ]
    found = []
    for root in json.loads(os.environ.get("FAKE_LEAK_ROOTS") or "[]"):
        for path in Path(root).rglob("*"):
            if not path.is_file():
                continue
            if any(path == d or d in path.parents for d in denied):
                continue
            if mark in path.read_text(errors="replace"):
                found.append(str(path))
    return {"readable": sorted(found), "in_prompt": mark in prompt}


if os.environ.get("FAKE_CLAUDE_LOG"):
    call = {
        "argv": argv,
        "settings": settings,
        "prompt": prompt,
        "cwd": os.getcwd(),
        "env": {k: os.environ.get(k) for k in WATCHED},
        "session": session,
        "new": "--session-id" in argv,
        "leaks": leaks(),
    }
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
        log.write(json.dumps(call) + "\n")
if os.environ.get("FAKE_CLAUDE_PIDS"):
    Path(os.environ["FAKE_CLAUDE_PIDS"]).write_text(f"{os.getpid()}\n")


def emit(event):
    print(json.dumps(event), flush=True)


def assistant(*content):
    emit(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": list(content)},
        }
    )


def tool_round(tool_id, name, given, text, error=False):
    assistant(
        {"type": "tool_use", "id": tool_id, "name": name, "input": given}
    )
    emit(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": text,
                        "is_error": error,
                    }
                ],
            },
        }
    )


def result(text, error=False, subtype="success", sid=None):
    emit(
        {
            "type": "result",
            "subtype": subtype,
            "is_error": error,
            "result": text,
            "session_id": sid or session,
            "permission_denials": [],
        }
    )


if case == "startup_fail":
    print("Error: could not start", file=sys.stderr)
    sys.exit(1)

tmp = os.environ.get("CLAUDE_CODE_TMPDIR") or "/tmp"
emit(
    {
        "type": "system",
        "subtype": "init",
        "session_id": session,
        "permissionMode": flag("--permission-mode"),
        "model": flag("--model")
        or (settings or {}).get("model")
        or "fake-default-model",
        "cwd": os.getcwd(),
        "scratchpad_path": (
            f"{tmp}/claude-{os.getuid()}/fake/{session}/scratchpad"
        ),
    }
)
if case == "mode_default":
    emit(
        {
            "type": "system",
            "subtype": "status",
            "status": None,
            "permissionMode": "default",
        }
    )
if case == "not_signed_in":
    result("Not logged in · Please run /login", error=True)
    sys.exit(1)
if case == "timeout":
    time.sleep(60)
if case == "edit_tracked":
    Path("a.txt").write_text("changed by Claude\n")

time.sleep(float(for_step("FAKE_CLAUDE_SLEEPS", 0)))
answer = for_step(
    "FAKE_CLAUDE_ANSWERS",
    os.environ.get("FAKE_CLAUDE_TEXT", "FAKE-CLAUDE-ANSWER"),
)
if answer.startswith("@headings"):
    lines = list(
        dict.fromkeys(re.findall(r"^### Decision \d+: .*$", prompt, re.M))
    )
    if answer == "@headings-but-last":
        lines = lines[:-1]
    sections = [
        f"{line}\nFAKE-MERGED-{line.split()[2].rstrip(':')}" for line in lines
    ]
    answer = "\n\n".join(
        ["## Part 4 — Options for the open decisions (Claude and Codex)"]
        + sections
    )
if MARK in prompt:
    if case == "sandbox_broken":
        tool_round(
            "toolu_1",
            "Bash",
            {"command": f"echo {MARK}"},
            "Exit code 71\n"
            "sandbox-exec: sandbox_apply: Operation not permitted",
            error=True,
        )
        answer = "The sandbox failed to start."
    else:
        tool_round("toolu_1", "Bash", {"command": f"echo {MARK}"}, f"{MARK}\n")
        answer = MARK
elif case == "refused_tools":
    tool_round(
        "toolu_1",
        "Read",
        {"file_path": "/elsewhere/log.jsonl"},
        "File is in a directory that is denied by your permission settings.",
        error=True,
    )
    tool_round(
        "toolu_2",
        "Bash",
        {"command": "echo x > a.txt"},
        "Exit code 1\n(eval):1: operation not permitted: a.txt",
        error=True,
    )
    tool_round(
        "toolu_3",
        "Bash",
        {"command": "pytest -q"},
        "Exit code 1\n1 failed",
        error=True,
    )
elif case in ("slow", "error_after_work", "no_result"):
    tool_round("toolu_1", "Read", {"file_path": "a.txt"}, "one\n")
    if case == "slow":
        time.sleep(1)

if case == "error_after_work":
    result(
        "API Error: overloaded", error=True, subtype="error_during_execution"
    )
    sys.exit(1)
if case == "no_result":
    sys.exit(0)
assistant({"type": "text", "text": answer})
if case == "wrong_session":
    result(answer, sid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
elif case == "empty":
    result("")
else:
    result(answer)
