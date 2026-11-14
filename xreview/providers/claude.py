"""Claude adapter: one headless turn of Session B. This is the only module
that runs `claude`.

Every turn runs in Claude Code's sandbox with Auto permission mode. The
repo is write-denied, shell commands can't escape the sandbox, and only the
read, shell and web tools exist. Session B loads none of the developer's
settings files, because those can widen the sandbox (excluded commands, or
filesystem isolation switched off); managed settings still apply, so the
setup check stops when a managed file on disk widens it. Each turn also
blocks reads of other
agents' records: other Claude Code sessions' conversations, saved output,
subagent logs, scratch folders and memory, the developer's Claude Code
history, and Codex's home folder (see blocked()). A turn can block more
paths, as Claude's blind ranking does for every copy of Codex's.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
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
# How the operating system refuses a write the sandbox blocks: macOS, then
# Linux. Unlike REFUSAL, this can't come from Auto's classifier or a rule,
# so it shows the sandbox's filesystem layer is on.
OS_DENIAL = re.compile(
    r"operation not permitted|read-only file system", re.IGNORECASE
)
SETUP = (
    "Claude Code CLI not found. xreview runs the review's Claude session "
    "with the `claude` command. Install the Claude Code CLI so `claude` is "
    "on your PATH, then run `claude` once in a terminal to sign in. The VS "
    "Code extension doesn't put `claude` on your PATH by itself."
)

# Where Claude Code reads managed settings files: managed-settings.json,
# then managed-settings.d/*.json in alphabetical order (Claude Code
# 2.1.319). MDM profiles and server-managed settings aren't files here.
MANAGED_DIR = Path(
    "/Library/Application Support/ClaudeCode"
    if sys.platform == "darwin"
    else "/etc/claude-code"
)

# The developer's model choices, which the runner copies once per review
# into Session B's settings, since Session B loads none of their settings
# files. The top-level effortLevel isn't among them: in the user file it
# doesn't apply to Opus 5.5 and later, but in --settings it would apply to
# every model. /effort saves levels per model, under modelSettings.
CHOICES = ("model", "modelSettings")

# Claude Code's documented limit for a --settings file.
SETTINGS_LIMIT = 2 * 1024 * 1024

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


# Claude Code shortens a longer folder name to its first 200 characters,
# a dash and a six-character hash of the path (Claude Code 2.1.319).
NAME_LIMIT = 200


def project_folder(parent: Path, name: str) -> Path:
    """The folder Claude Code files a project under in parent. For a long
    name, that's the one existing folder whose name starts the same; if
    there's none yet, a name no folder has."""
    if len(name) <= NAME_LIMIT:
        return parent / name
    start = name[:NAME_LIMIT]
    found = [
        p
        for p in (parent.glob(f"{start}-*") if parent.is_dir() else [])
        if len(p.name) == NAME_LIMIT + 7
    ]
    return found[0] if len(found) == 1 else parent / start


def exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:  # a name too long for the file system
        return False


def places(repo: Path, session: str | None) -> tuple[Path, Path]:
    """This project's folder of conversation logs, and its folder of
    scratch folders, as Claude Code names them for a session started in
    repo. If a folder holding this session exists elsewhere, that one is
    used, in case Claude Code names a path differently."""
    name = slug(repo.resolve())
    projects = config_dir() / "projects"
    logs = project_folder(projects, name)
    if session and not exists(logs / f"{session}.jsonl"):
        moved = list(projects.glob(f"*/{session}.jsonl"))
        if moved:
            logs = moved[0].parent
    root = Path(os.environ.get("CLAUDE_CODE_TMPDIR") or "/tmp").resolve()
    tmp_root = root / f"claude-{os.getuid()}"
    scratch = project_folder(tmp_root, name)
    if session and not exists(scratch / session):
        moved = [p for p in tmp_root.glob(f"*/{session}") if p.is_dir()]
        if moved:
            scratch = moved[0].parent
    return logs, scratch


# Folders where Claude Code makes a file for Session B as it starts, which
# Session B's own shell then reads, such as its shell snapshot. Their
# entries are blocked one by one, so the new file stays readable.
PER_ENTRY = frozenset({"shell-snapshots", "session-env"})
# What other sessions leave at the top of Claude Code's temp root. The
# rest of it is shared temp space, Session B's own work included.
TMP_LEFTOVERS = re.compile(r"^(cache-break-state-.*\.json|bash-edit-diff)$")


def around(root: Path, keep: list[Path]) -> list[Path]:
    """Every existing entry under root to block so that only the paths in
    keep, and anything created after this, stay readable. A folder that
    holds a kept path, or is named in PER_ENTRY, is entered rather than
    blocked. Deny rules can't have exceptions, which is why the entries
    around a kept path are blocked one by one."""
    if not root.is_dir():
        return []
    found = []
    for entry in sorted(root.iterdir()):
        if entry in keep:
            continue
        holds = any(k.is_relative_to(entry) for k in keep)
        if holds or (entry.name in PER_ENTRY and entry.is_dir()):
            found += around(entry, keep)
        else:
            found.append(entry)
    return found


def codex_home() -> Path:
    found = os.environ.get("CODEX_HOME")
    return Path(found) if found else Path.home() / ".codex"


def blocked(
    repo: Path,
    session: str | None,
    review: Path | None = None,
    shared: list[Path] | tuple = (),
) -> list[Path]:
    """What Session B must not read, listed fresh for each turn: other
    agents' records and the developer's private Claude Code data.

    - Everything in Claude Code's home folder (every project's
      conversations, saved output, subagent logs and memory, the prompt
      history, file history, session keys and shell snapshots), except
      Session B's own session folder.
    - Every session folder in Claude Code's temp root, scratchpads and
      background-task output included, except Session B's own, and what
      other sessions leave at its top. The rest of that root is shared temp
      space, where Session B's own work goes too.
    - Codex's home folder, and the review's sibling review folders.

    The repo and the shared paths (files the handoff or the developer
    named) are never blocked, wherever they are. Something another session
    creates during the turn isn't covered until the next turn.
    """
    repo = repo.resolve()
    logs, scratch = places(repo, session)
    keep = [repo, *(Path(p).resolve() for p in shared)]
    if session:
        keep += [logs.resolve() / session, scratch.resolve() / session]
    found = around(config_dir().resolve(), keep)
    memory = logs.resolve() / "memory"
    if memory not in found and not exists(memory):
        found.append(memory)  # in case a memory folder appears mid-turn
    tmp_root = scratch.resolve().parent
    if tmp_root.is_dir():
        for entry in sorted(tmp_root.iterdir()):
            if entry in keep:
                continue
            if entry.name.startswith("-") and entry.is_dir():
                found += around(entry, keep)
            elif TMP_LEFTOVERS.match(entry.name):
                found.append(entry)
    home = codex_home().resolve()
    if any(k.is_relative_to(home) for k in keep):
        found += around(home, keep)
    else:
        found.append(home)
    if review is not None and review.parent.is_dir():
        here = review.resolve()
        for entry in sorted(here.parent.iterdir()):
            if not entry.is_dir() or entry == here:
                continue
            if any(k.is_relative_to(entry) for k in keep):
                found += around(entry, keep)
            else:
                found.append(entry)
    return found


def escape(path: Path) -> str:
    """A path as a permission-rule pattern: // starts an absolute path,
    and the pattern characters a path may hold are escaped."""
    return "/" + re.sub(r"([*?\[\]\\])", r"\\\1", str(path))


def read_rule(path: Path, folder: bool) -> str:
    return f"Read({escape(path)}{'/**' if folder else ''})"


def log_rules(projects: Path, shared: list[Path] | tuple) -> list[str]:
    """Read rules for every project's conversation logs. A pattern also
    covers a log created after the folders were listed. Deny rules can't
    have exceptions, so in the folder of a log the developer named, the
    other logs are listed one by one instead."""
    named = [
        path
        for path in (Path(p).resolve() for p in shared)
        if path.suffix == ".jsonl" and path.parent.parent == projects
    ]
    if not named:
        return [f"Read({escape(projects)}/*/*.jsonl)"]
    rules = []
    for folder in sorted(p for p in projects.iterdir() if p.is_dir()):
        if any(path.parent == folder for path in named):
            rules += [
                read_rule(log, folder=False)
                for log in sorted(folder.glob("*.jsonl"))
                if log not in named
            ]
        else:
            rules.append(f"Read({escape(folder)}/*.jsonl)")
    return rules


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


def git_folders(repo: Path) -> list[Path]:
    """The repo's own Git folder and its shared one, resolved. In a linked
    worktree both lie outside the working tree, and a commit writes there,
    so they're write-denied too."""
    try:
        found = subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir", "--git-common-dir"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    if found.returncode != 0:
        return []
    return [(repo / line).resolve() for line in found.stdout.splitlines()]


def user_choices() -> dict:
    """The developer's model choices from their user settings file, for
    Session B to start with."""
    try:
        data = json.loads(
            (config_dir() / "settings.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in CHOICES if key in data}


def settings(
    repo: Path,
    session: str | None,
    extra: list[Path],
    review: Path | None = None,
    shared: list[Path] | tuple = (),
    choices: dict | None = None,
) -> dict:
    """Session B's settings for one turn: the sandbox, with the repo
    write-denied, temp folders writable, and the blocked paths read-denied
    for shell commands and
    their children, plus Read deny rules for the same paths, because
    Claude's own Read, Grep and Glob aren't covered by the sandbox. The
    output style is the default whatever the developer's settings say:
    the Explanatory and Learning styles add Insight blocks, which would
    go to Codex with Claude's replies. choices are the developer's model
    choices copied for the review; only the keys in CHOICES are used."""
    denied = [*blocked(repo, session, review, shared), *extra]
    rules = log_rules(config_dir().resolve() / "projects", shared)
    for path in denied:
        rules.append(read_rule(path, folder=not path.is_file()))
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "disabled": False,
                "allowWrite": user_temp(),
                "denyWrite": [
                    str(repo),
                    *dict.fromkeys(
                        str(path)
                        for path in git_folders(repo)
                        if not path.is_relative_to(repo.resolve())
                    ),
                ],
                "denyRead": [str(path) for path in denied],
            },
        },
        "permissions": {"deny": rules},
        "outputStyle": "default",
        **{k: v for k, v in (choices or {}).items() if k in CHOICES},
    }


def write_settings(path: Path, data: dict) -> None:
    """Write Session B's settings file without indentation, refusing one
    over Claude Code's limit before Claude starts. Its read blocks grow
    with the developer's Claude Code history."""
    text = json.dumps(data) + "\n"
    size = len(text.encode("utf-8"))
    if size > SETTINGS_LIMIT:
        raise CallFailed(
            f"Session B's settings file would be {size / 2**20:.1f} MiB, over "
            f"Claude Code's {SETTINGS_LIMIT // 2**20} MiB limit, so xreview "
            "didn't start Claude. It lists every path Session B mustn't "
            "read, and that list grows with your Claude Code history: each "
            "conversation, scratch folder and saved output Claude Code keeps."
        )
    path.write_text(text, encoding="utf-8")


def command(
    claude: str,
    settings_file: Path,
    session: str | None,
    first: bool,
    model: str | None = None,
    effort: str | None = None,
) -> list[str]:
    """The argv for one turn. With no session, the turn isn't saved. A
    model or effort the developer gave goes here rather than in the
    settings file, because these flags outrank both the settings files
    and ANTHROPIC_MODEL."""
    if session is None:
        flags = ["--no-session-persistence"]
    else:
        flags = ["--session-id" if first else "--resume", session]
    if model:
        flags += ["--model", model]
    if effort:
        flags += ["--effort", effort]
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
        "--setting-sources=",
        "--settings",
        str(settings_file),
    ]


def managed_problems() -> list[str]:
    """The keys in managed settings files on disk that widen Session B's
    sandbox: excluded commands, or filesystem isolation switched off.
    Managed settings outrank Session B's own, so xreview can't undo them."""
    files = [MANAGED_DIR / "managed-settings.json"]
    drop_ins = MANAGED_DIR / "managed-settings.d"
    if drop_ins.is_dir():
        files += sorted(
            p for p in drop_ins.glob("*.json") if not p.name.startswith(".")
        )
    found = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sandbox = data.get("sandbox") if isinstance(data, dict) else None
        if not isinstance(sandbox, dict):
            continue
        if sandbox.get("excludedCommands"):
            found.append(f"`sandbox.excludedCommands` in {path}")
        filesystem = sandbox.get("filesystem")
        if isinstance(filesystem, dict) and filesystem.get("disabled") is True:
            found.append(f"`sandbox.filesystem.disabled` in {path}")
    return found


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
    review: Path | None = None,
    shared: list[Path] | tuple = (),
    model: str | None = None,
    effort: str | None = None,
    choices: dict | None = None,
) -> str:
    """Send one message to Session B and return its final message, which
    is also saved as run_dir/response.md. first starts the session.
    review is the review's folder, so its sibling reviews are blocked;
    shared are paths the handoff or the developer named, which stay
    readable. model and effort are the review's, if set; choices are the
    developer's model choices copied for the review."""
    claude = binary()
    extra = list(extra_blocked or [])
    settings_file = run_dir / "settings.json"
    write_settings(
        settings_file, settings(repo, session, extra, review, shared, choices)
    )
    execute(
        command(claude, settings_file, session, first, model, effort),
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


def preflight(
    repo: Path,
    run_dir: Path,
    model: str | None = None,
    effort: str | None = None,
    choices: dict | None = None,
) -> str | None:
    """One unsaved turn with Session B's flags, to show before the review
    starts that the sandbox runs a shell command and the mode is Auto.
    Claude Code starts even when its sandbox can't: only the command
    fails. And when Auto isn't available, it reports Auto at startup, then
    switches to another mode. Returns the model Claude Code reported, if
    any."""
    claude = binary()
    widened = managed_problems()
    if widened:
        raise CallFailed(
            "Your organization's managed settings widen the sandbox the "
            "review's Claude session runs in, so xreview won't start the "
            f"review: {'; '.join(widened)}. Managed settings outrank "
            "xreview's own, so ask whoever manages this machine whether "
            "that can change."
        )
    settings_file = run_dir / "settings.json"
    write_settings(settings_file, settings(repo, None, [], choices=choices))
    probe = run_dir / "probe"
    prompt = (
        "Run exactly these two commands with the Bash tool, one at a time: "
        f"`echo {PREFLIGHT_MARK}`, then `touch {shlex.quote(str(probe))}`. "
        "If a command fails, don't retry it or try another way. Then reply "
        "with only what the first one printed."
    )
    execute(
        command(claude, settings_file, None, False, model, effort),
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
            "in Auto mode, inside Claude Code's sandbox. Session B doesn't "
            "load your settings files, so check that your plan allows Auto "
            "mode, and that your organization's managed settings, if any, "
            "don't switch it off with `disableAutoMode`. Then run again."
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
    write_refused(events, probe)
    reported = [
        e.get("model")
        for e in events
        if e.get("type") == "system" and e.get("subtype") == "init"
    ]
    return reported[0] if reported and reported[0] else None


def write_refused(events: list[dict], probe: Path) -> None:
    """Show that the setup check's write into the repo was refused by the
    sandbox: no file afterwards, and an operating-system denial. A refusal
    by Auto's classifier or a rule doesn't show the sandbox is on."""
    if probe.exists():
        probe.unlink()
        raise CallFailed(
            "Claude Code's sandbox let a shell command write to the repo, so "
            "xreview won't start the review. Check your Claude Code sandbox "
            "settings."
        )
    uses = {b.get("id"): b for b in blocks(events, "tool_use", "assistant")}

    def command_of(result: dict) -> str:
        given = uses.get(result["id"], {}).get("input")
        return str(given.get("command", "")) if isinstance(given, dict) else ""

    touched = [r for r in tool_results(events) if "touch" in command_of(r)]
    if not any(OS_DENIAL.search(r["text"]) for r in touched):
        detail = touched[0]["text"][:300] if touched else "no write was tried"
        raise CallFailed(
            "xreview couldn't confirm that Claude Code's sandbox blocks "
            f"writes to the repo, so it won't start the review: {detail}"
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
