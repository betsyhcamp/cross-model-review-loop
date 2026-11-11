"""Claude adapter: one headless turn of Session B. This is the only module
that runs `claude`.

Every turn runs in Claude Code's sandbox with Auto permission mode. The
repo is write-denied, shell commands can't escape the sandbox, and only the
read, shell and web tools exist. Each turn also blocks reads of this
project's other conversations: their logs, saved tool output, subagent
logs and scratch folders, and the project's memory folder. A turn can block
more paths, as Claude's blind ranking does for every copy of Codex's.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Callable

from .. import procs

DISPLAY_NAME = "Claude"
TIMEOUT_SECONDS = 40 * 60
PROGRESS_SECONDS = 30
TOOLS = "Read,Grep,Glob,Bash,WebFetch,WebSearch"
ALLOWED_TOOLS = "Read,Grep,Glob,WebFetch,WebSearch"
PREFLIGHT_MARK = "xreview-sandbox-ok"
# Set by the Claude Code session that runs xreview (Session A). Session B
# starts as a fresh session, so it doesn't inherit them.
INHERITED = (
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    "CLAUDE_CODE_ENABLE_TASKS",
    "CLAUDE_CODE_QUESTION_PREVIEW_FORMAT",
    "CLAUDE_CODE_EMIT_STARTUP_TIMING",
    "CLAUDE_CODE_STARTUP_FAILURE_RESULTS",
)
# How a refused tool call reads: a deny rule, Auto's classifier, or the
# sandbox. Any other tool error is an ordinary failure.
REFUSAL = re.compile(
    r"denied by your permission settings|has been denied|auto mode "
    r"classifier|operation not permitted|<sandbox_violations>|sandbox_apply",
    re.IGNORECASE,
)
SETUP = (
    "Claude Code CLI not found. xreview runs the review's Claude session "
    "with the `claude` command. Install the Claude Code CLI so `claude` is "
    "on your PATH, then run `claude` once in a terminal to sign in. The VS "
    "Code extension doesn't put `claude` on your PATH by itself."
)

CallFailed = procs.CallFailed
CallUncertain = procs.CallUncertain
OnStart = Callable[[int, "str | None"], None] | None


# --- finding claude ----------------------------------------------------------


def locate() -> tuple[str | None, str]:
    """(path, how): `claude` on PATH, else the binary of the Claude Code
    session that runs this, from CLAUDE_CODE_EXECPATH. That variable is
    undocumented, so it's only a fallback. how is "path", "fallback" or ""
    when neither is found."""
    found = shutil.which("claude")
    if found:
        return found, "path"
    fallback = os.environ.get("CLAUDE_CODE_EXECPATH")
    if fallback and os.access(fallback, os.X_OK):
        return fallback, "fallback"
    return None, ""


def binary() -> str:
    found, _ = locate()
    if not found:
        raise CallFailed(SETUP)
    return found


# --- the boundary ------------------------------------------------------------


def slug(path: Path) -> str:
    """The folder name Claude Code files a project's sessions under."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def config_dir() -> Path:
    found = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(found) if found else Path.home() / ".claude"


def places(repo: Path, session: str | None) -> tuple[Path, Path]:
    """This project's folder of conversation logs, and its folder of
    scratch folders, as Claude Code names them for a session started in
    repo. If a folder holding this session exists elsewhere, that one is
    used, in case Claude Code names a long path differently."""
    name = slug(repo.resolve())
    projects = config_dir() / "projects"
    logs = projects / name
    if session and not (logs / f"{session}.jsonl").exists():
        moved = list(projects.glob(f"*/{session}.jsonl"))
        if moved:
            logs = moved[0].parent
    root = Path(os.environ.get("CLAUDE_CODE_TMPDIR") or "/tmp").resolve()
    tmp_root = root / f"claude-{os.getuid()}"
    scratch = tmp_root / name
    if session and not (scratch / session).is_dir():
        moved = [p for p in tmp_root.glob(f"*/{session}") if p.is_dir()]
        if moved:
            scratch = moved[0].parent
    return logs, scratch


def other_sessions(repo: Path, session: str | None) -> list[Path]:
    """What Session B must not read of this project's other conversations:
    every top-level log (Session A's included), the memory folder, and
    every other session's folder of saved output and subagent logs and its
    scratch folder. Session B's own folders stay readable. The folders are
    listed now, so a session started during the turn isn't covered."""
    logs, scratch = places(repo, session)
    blocked = []
    if logs.is_dir():
        for entry in sorted(logs.iterdir()):
            if entry.is_file() and entry.suffix == ".jsonl":
                blocked.append(entry)
            elif entry.is_dir() and entry.name != session:
                blocked.append(entry)
    blocked.append(logs / "memory")
    if scratch.is_dir():
        for entry in sorted(scratch.iterdir()):
            if entry.is_dir() and entry.name != session:
                blocked.append(entry)
    return blocked


def escape(path: Path) -> str:
    """A path as a permission-rule pattern: // starts an absolute path,
    and the pattern characters a path may hold are escaped."""
    return "/" + re.sub(r"([*?\[\]\\])", r"\\\1", str(path))


def read_rule(path: Path, folder: bool) -> str:
    return f"Read({escape(path)}{'/**' if folder else ''})"


def user_temp() -> list[str]:
    """macOS's per-user temp folder. `mktemp -d` makes its folders there
    even when TMPDIR names another, so the sandbox must let Session B
    write there for ordinary scripts and tests to work. Linux has none."""
    if sys.platform != "darwin":
        return []
    try:
        found = subprocess.run(
            ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"],
            capture_output=True,
            text=True,
        ).stdout.strip()
    except OSError:
        return []
    return [str(Path(found).resolve())] if found else []


def settings(repo: Path, session: str | None, extra: list[Path]) -> dict:
    """Session B's settings for one turn: the sandbox, with the repo
    write-denied, temp folders writable, and the blocked paths read-denied
    for shell commands and
    their children, plus Read deny rules for the same paths, because
    Claude's own Read, Grep and Glob aren't covered by the sandbox. The
    output style is the default whatever the developer's settings say:
    the Explanatory and Learning styles add Insight blocks, which would
    go to Codex with Claude's replies."""
    logs, _ = places(repo, session)
    blocked = [*other_sessions(repo, session), *extra]
    # The pattern also covers a log created after the folder was listed.
    rules = [f"Read({escape(logs)}/*.jsonl)"]
    for path in blocked:
        rules.append(read_rule(path, folder=not path.is_file()))
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "allowWrite": user_temp(),
                "denyWrite": [str(repo)],
                "denyRead": [str(path) for path in blocked],
            },
        },
        "permissions": {"deny": rules},
        "outputStyle": "default",
    }


def command(
    claude: str, settings_file: Path, session: str | None, first: bool
) -> list[str]:
    """The argv for one turn. With no session, the turn isn't saved."""
    if session is None:
        flags = ["--no-session-persistence"]
    else:
        flags = ["--session-id" if first else "--resume", session]
    return [
        claude,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        *flags,
        "--tools",
        TOOLS,
        "--allowedTools",
        ALLOWED_TOOLS,
        "--permission-mode",
        "auto",
        "--strict-mcp-config",
        "--settings",
        str(settings_file),
    ]


def environment() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in INHERITED}
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


# --- one turn ----------------------------------------------------------------


def turn(
    prompt: str,
    repo: Path,
    run_dir: Path,
    session: str,
    first: bool,
    extra_blocked: list[Path] | None = None,
    on_start: OnStart = None,
) -> str:
    """Send one message to Session B and return its final message, which
    is also saved as run_dir/response.md. first starts the session."""
    claude = binary()
    blocked = list(extra_blocked or [])
    settings_file = run_dir / "settings.json"
    settings_file.write_text(
        json.dumps(settings(repo, session, blocked), indent=2) + "\n",
        encoding="utf-8",
    )
    execute(
        command(claude, settings_file, session, first),
        prompt,
        repo,
        run_dir,
        on_start,
    )
    text = parse(run_dir, session)
    (run_dir / "response.md").write_text(text, encoding="utf-8")
    return text


def execute(
    argv: list[str], prompt: str, repo: Path, run_dir: Path, on_start: OnStart
) -> None:
    try:
        procs.run(
            argv,
            prompt,
            repo,
            run_dir,
            timeout=TIMEOUT_SECONDS,
            progress_every=PROGRESS_SECONDS,
            progress=lambda elapsed: report_progress(run_dir, elapsed),
            env=environment(),
            on_start=on_start,
        )
    except OSError as exc:
        raise CallFailed(f"Claude Code couldn't be started: {exc}") from exc
    except procs.Stopped as exc:
        raise CallUncertain(
            f"The runner was stopped ({exc}) and Claude was killed. It may "
            "have finished its turn first."
        ) from exc
    except procs.TimedOut as exc:
        raise CallUncertain(
            f"Claude timed out after {TIMEOUT_SECONDS // 60} minutes and "
            "was killed. It may have finished its turn first."
        ) from exc


def preflight(repo: Path, run_dir: Path) -> None:
    """One unsaved turn with Session B's flags, to show before the review
    starts that the sandbox runs a shell command and the mode is Auto.
    Claude Code starts even when its sandbox can't: only the command
    fails. And when Auto isn't available, it reports Auto at startup, then
    switches to another mode."""
    claude = binary()
    settings_file = run_dir / "settings.json"
    settings_file.write_text(
        json.dumps(settings(repo, None, []), indent=2) + "\n",
        encoding="utf-8",
    )
    prompt = (
        f"Run exactly this command with the Bash tool: echo {PREFLIGHT_MARK}"
        "\nThen reply with only what it printed."
    )
    execute(
        command(claude, settings_file, None, False),
        prompt,
        repo,
        run_dir,
        None,
    )
    events = read_events(run_dir)
    if not any(e.get("type") == "system" for e in events):
        raise CallFailed(startup_failure(run_dir))
    mode = last_mode(events)
    if mode != "auto":
        raise CallFailed(
            "Claude Code's Auto mode isn't available here (it ran in "
            f"{mode!r} mode). xreview runs the review's Claude session only "
            "in Auto mode, inside Claude Code's sandbox. Check that your "
            "plan and settings allow Auto mode (`disableAutoMode` turns it "
            "off), then run again."
        )
    results = tool_results(events)
    ran = [
        r for r in results if not r["error"] and PREFLIGHT_MARK in r["text"]
    ]
    if not ran:
        detail = results[0]["text"][:300] if results else "no command ran"
        raise CallFailed(
            "Claude Code's sandbox couldn't run a shell command, so xreview "
            f"won't start the review: {detail}\nOn Linux, the sandbox needs "
            "`bubblewrap` and `socat` installed. If xreview itself runs "
            "inside a sandbox, such as Claude Code's sandbox for the shell "
            "commands of the session you're in, the review's sandbox can't "
            "start inside it."
        )


# --- reading a turn's output -------------------------------------------------


def read_events(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    events = []
    if not path.exists():
        return events
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def last_mode(events: list[dict]) -> str | None:
    """The permission mode Claude last reported: at startup, then in any
    status event that changed it."""
    mode = None
    for event in events:
        if event.get("type") == "system" and event.get("permissionMode"):
            mode = event["permissionMode"]
    return mode


def blocks(
    events: list[dict], kind: str, role: str | None = None
) -> list[dict]:
    """The content blocks of one kind, from messages of one role if
    given: tool_use and text come from "assistant", tool_result from
    "user"."""
    found = []
    for event in events:
        if role and event.get("type") != role:
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            found += [
                b
                for b in content
                if isinstance(b, dict) and b.get("type") == kind
            ]
    return found


def tool_results(events: list[dict]) -> list[dict]:
    results = []
    for block in blocks(events, "tool_result", "user"):
        content = block.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(c.get("text", "")) for c in content if isinstance(c, dict)
            )
        results.append(
            {
                "id": block.get("tool_use_id"),
                "error": bool(block.get("is_error")),
                "text": str(content or ""),
            }
        )
    return results


def startup_failure(run_dir: Path) -> str:
    path = run_dir / "stderr.txt"
    stderr = path.read_text(errors="replace") if path.exists() else ""
    tail = "\n".join(stderr.splitlines()[-20:]) or "(no output)"
    return (
        "Claude Code exited before starting the turn. Last lines of "
        f"stderr:\n{tail}"
    )


def parse(run_dir: Path, session: str) -> str:
    """Check the event stream and return Claude's final message.

    Failed means the turn provably didn't run: Claude Code exited before
    starting, or the API refused the turn before Claude wrote anything,
    as when it isn't signed in. A missing, failed or empty result after
    Claude wrote something is uncertain, and so is another session's.
    """
    events = read_events(run_dir)
    if not any(e.get("type") == "system" for e in events):
        raise CallFailed(startup_failure(run_dir))
    results = [e for e in events if e.get("type") == "result"]
    wrote = bool(
        blocks(events, "text", "assistant")
        or blocks(events, "tool_use", "assistant")
    )
    if not results:
        raise CallUncertain("Claude sent no result; see events.jsonl.")
    result = results[-1]
    if result.get("is_error") or result.get("subtype") != "success":
        detail = str(result.get("result"))[:300]
        if not wrote:
            raise CallFailed(f"Claude Code refused the turn: {detail}")
        raise CallUncertain(f"Claude reported a failed turn: {detail}")
    if result.get("session_id") != session:
        raise CallUncertain(
            "Claude answered in a different session; refusing its result."
        )
    text = result.get("result") or ""
    if not text.strip():
        raise CallUncertain("Claude returned an empty response.")
    return text


def finished(run_dir: Path, session: str) -> str | None:
    """The answer of a turn that finished on its own, after the runner
    that started it died; None unless the result is complete and this
    session's."""
    try:
        return parse(run_dir, session)
    except (CallFailed, CallUncertain):
        return None


def stats(run_dir: Path) -> dict:
    """Tool calls so far, refusals counted apart from ordinary failures,
    and the last few actions, with none of Claude's text."""
    events = read_events(run_dir)
    uses = blocks(events, "tool_use", "assistant")
    results = tool_results(events)
    refused = sum(
        1 for r in results if r["error"] and REFUSAL.search(r["text"])
    )
    failed = sum(1 for r in results if r["error"]) - refused
    actions = []
    for use in uses[-3:]:
        given = use.get("input") if isinstance(use.get("input"), dict) else {}
        target = (
            given.get("command")
            or given.get("file_path")
            or given.get("pattern")
            or given.get("url")
            or ""
        )
        target = " ".join(str(target).split())
        actions.append(f"{use.get('name')} {target[:60]}".rstrip())
    return {
        "tools": len(uses),
        "refused": refused,
        "failed": failed,
        "actions": actions,
        "mode": last_mode(events),
    }


def report_progress(run_dir: Path, elapsed: float) -> None:
    """Print a progress line on stderr, with none of Claude's text."""
    counts = stats(run_dir)
    minutes, seconds = divmod(round(elapsed), 60)
    calls = counts["tools"]
    print(
        f"xreview: {DISPLAY_NAME} still working — {minutes}m {seconds}s, "
        f"{calls} tool call{'' if calls == 1 else 's'}",
        file=sys.stderr,
        flush=True,
    )
