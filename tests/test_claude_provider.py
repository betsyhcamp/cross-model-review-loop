"""Tests for the Claude adapter, run in-process against the fake claude.

Each test gets its own Claude Code folders: a projects folder with this
project's logs, another session's saved output and subagent logs, Session
B's own folder and the memory folder, and a scratch root with Session B's
and another session's scratch folders.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile

import pytest

from xreview.providers import claude

SESSION = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
SESSION_B_FLAGS = [
    "--tools",
    "Read,Grep,Glob,Bash,WebFetch,WebSearch",
    "--allowedTools",
    "Read,Grep,Glob,WebFetch,WebSearch",
    "--permission-mode",
    "auto",
    "--strict-mcp-config",
    "--settings",
]


class Folders:
    """A test's own Claude Code folders for the repo."""

    def __init__(self, repo):
        name = claude.slug(repo.path.resolve())
        self.config = repo.tmp / "claude-config"
        self.logs = self.config / "projects" / name
        self.tmp = repo.tmp / "claude-tmp"
        self.scratch = self.tmp / f"claude-{os.getuid()}" / name
        for path in (
            self.logs / SESSION / "tool-results",
            self.logs / OTHER / "subagents",
            self.logs / "memory",
            self.scratch / SESSION / "scratchpad",
            self.scratch / OTHER / "scratchpad",
            self.scratch / OTHER / "tasks",
        ):
            path.mkdir(parents=True)
        for path in (
            self.logs / f"{SESSION}.jsonl",
            self.logs / f"{OTHER}.jsonl",
        ):
            path.write_text("{}\n")


@pytest.fixture
def folders(repo, monkeypatch) -> Folders:
    found = Folders(repo)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(found.config))
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(found.tmp))
    return found


@pytest.fixture
def run_dir(repo) -> Path:
    path = repo.tmp / "run"
    path.mkdir()
    return path


@pytest.fixture
def call(repo, run_dir, folders, monkeypatch):
    """Run one claude.turn in run_dir. The fake logs each call to
    claude-calls.jsonl."""

    def run(
        case: str = "ok",
        first: bool = True,
        extra: list[Path] | None = None,
        prompt: str = "Rebut this.\n",
        **env: str,
    ):
        env = {
            "PATH": repo.env["PATH"],
            "FAKE_CLAUDE_CASE": case,
            "FAKE_CLAUDE_LOG": str(repo.tmp / "claude-calls.jsonl"),
            **env,
        }
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return claude.turn(prompt, repo.path, run_dir, SESSION, first, extra)

    return run


def last_call(repo) -> dict:
    log = repo.tmp / "claude-calls.jsonl"
    return json.loads(log.read_text().splitlines()[-1])


# --- the command and the boundary -------------------------------------------


def test_the_first_turn_starts_session_b_with_its_flags(repo, run_dir, call):
    assert call(prompt="Line 1\nLine 2\n") == "FAKE-CLAUDE-ANSWER"
    logged = last_call(repo)
    assert logged["argv"] == [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--session-id",
        SESSION,
        *SESSION_B_FLAGS,
        str(run_dir / "settings.json"),
    ]
    assert logged["prompt"] == "Line 1\nLine 2\n"
    assert Path(logged["cwd"]) == repo.path
    assert (run_dir / "prompt.md").read_text() == "Line 1\nLine 2\n"
    assert (run_dir / "response.md").read_text() == "FAKE-CLAUDE-ANSWER"


def test_a_later_turn_resumes_the_same_session(repo, call):
    call(first=False)
    argv = last_call(repo)["argv"]
    assert argv[4:6] == ["--resume", SESSION]
    assert "--session-id" not in argv


def test_the_sandbox_is_forced_and_the_repo_is_write_denied(repo, call):
    call()
    sandbox = last_call(repo)["settings"]["sandbox"]
    assert sandbox["enabled"] is True
    assert sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["filesystem"]["denyWrite"] == [str(repo.path)]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS's temp folder")
def test_macos_per_user_temp_folder_is_writable(repo, call):
    call()
    allowed = last_call(repo)["settings"]["sandbox"]["filesystem"]
    temp = Path(tempfile.gettempdir()).resolve()
    assert allowed["allowWrite"] == [str(temp)]


def test_the_output_style_is_the_default_whatever_the_user_chose(repo, call):
    call()
    assert last_call(repo)["settings"]["outputStyle"] == "default"


def test_other_sessions_are_blocked_and_session_bs_own_folders_arent(
    repo, folders, call
):
    call()
    settings = last_call(repo)["settings"]
    denied = set(settings["sandbox"]["filesystem"]["denyRead"])
    expected = {
        folders.logs / f"{SESSION}.jsonl",
        folders.logs / f"{OTHER}.jsonl",
        folders.logs / OTHER,
        folders.logs / "memory",
        folders.scratch / OTHER,
    }
    assert denied == {str(path) for path in expected}
    rules = settings["permissions"]["deny"]
    assert f"Read(/{folders.logs}/*.jsonl)" in rules
    assert f"Read(/{folders.logs / OTHER}/**)" in rules
    assert f"Read(/{folders.logs / 'memory'}/**)" in rules
    assert f"Read(/{folders.scratch / OTHER}/**)" in rules
    assert f"Read(/{folders.logs / OTHER}.jsonl)" in rules
    for own in (folders.logs / SESSION, folders.scratch / SESSION):
        assert str(own) not in denied
        assert not any(str(own) + "/" in rule for rule in rules)


def test_extra_paths_are_blocked_for_one_turn(repo, call):
    held = repo.tmp / "held"
    held.mkdir()
    (held / "ranking.md").write_text("x\n")
    call(extra=[held, held / "ranking.md"])
    settings = last_call(repo)["settings"]
    denied = settings["sandbox"]["filesystem"]["denyRead"]
    assert str(held) in denied and str(held / "ranking.md") in denied
    assert f"Read(/{held}/**)" in settings["permissions"]["deny"]
    assert f"Read(/{held / 'ranking.md'})" in settings["permissions"]["deny"]
    call(first=False)
    later = last_call(repo)["settings"]["sandbox"]["filesystem"]["denyRead"]
    assert str(held) not in later


def test_pattern_characters_in_a_path_are_escaped():
    assert claude.read_rule(Path("/a/b[1]*?"), folder=True) == (
        r"Read(//a/b\[1\]\*\?/**)"
    )


def test_a_session_filed_under_another_project_name_is_found(repo, folders):
    moved = "33333333-3333-4333-8333-333333333333"
    elsewhere = folders.config / "projects" / "-shortened-name"
    elsewhere.mkdir()
    (elsewhere / f"{moved}.jsonl").write_text("{}\n")
    scratch = folders.tmp / f"claude-{os.getuid()}" / "-shortened-name"
    (scratch / moved).mkdir(parents=True)
    assert claude.places(repo.path, moved) == (elsewhere, scratch)
    assert claude.places(repo.path, SESSION) == (folders.logs, folders.scratch)


def test_session_as_variables_are_dropped_and_memory_is_off(repo, call):
    call(
        CLAUDECODE="1",
        CLAUDE_PID="123",
        CLAUDE_EFFORT="low",
        CLAUDE_CODE_SESSION_ID=OTHER,
        CLAUDE_CODE_ENTRYPOINT="claude-vscode",
        CLAUDE_CODE_EXECPATH="/somewhere/claude",
        CLAUDE_CODE_USE_BEDROCK="0",
    )
    env = last_call(repo)["env"]
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    for name in (
        "CLAUDECODE",
        "CLAUDE_PID",
        "CLAUDE_EFFORT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
    ):
        assert env[name] is None, name
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "0"


# --- failed versus uncertain ------------------------------------------------


@pytest.mark.parametrize(
    "case, message",
    [
        ("startup_fail", "exited before starting the turn"),
        ("not_signed_in", "refused the turn: Not logged in"),
    ],
)
def test_a_turn_that_never_ran_is_failed(call, case, message):
    with pytest.raises(claude.CallFailed, match=message):
        call(case)


@pytest.mark.parametrize(
    "case, message",
    [
        ("error_after_work", "failed turn: API Error"),
        ("no_result", "no result"),
        ("wrong_session", "different session"),
        ("empty", "empty response"),
    ],
)
def test_a_turn_that_may_have_run_is_uncertain(run_dir, call, case, message):
    with pytest.raises(claude.CallUncertain, match=message):
        call(case)
    assert not (run_dir / "response.md").exists()


def test_a_timeout_kills_claude_and_is_uncertain(
    repo, call, monkeypatch, process_gone
):
    pids = repo.tmp / "pids"
    monkeypatch.setattr(claude, "TIMEOUT_SECONDS", 1)
    with pytest.raises(claude.CallUncertain, match="timed out"):
        call("timeout", FAKE_CLAUDE_PIDS=str(pids))
    assert process_gone(int(pids.read_text()))


def test_claude_missing_is_failed_with_setup_instructions(repo, call):
    with pytest.raises(claude.CallFailed, match="not found") as caught:
        call(PATH=str(repo.tools))
    assert "doesn't put `claude` on your PATH" in str(caught.value)


def test_the_session_binary_is_a_fallback_when_claude_isnt_on_path(repo, call):
    shim = str(repo.claude_bin / "claude")
    assert call(PATH=str(repo.tools), CLAUDE_CODE_EXECPATH=shim)
    assert claude.locate() == (shim, "fallback")
    os.environ["PATH"] = repo.env["PATH"]
    assert claude.locate() == (shim, "path")


def test_the_process_is_reported_as_it_starts(call, monkeypatch):
    seen = []
    monkeypatch.setenv("FAKE_CLAUDE_CASE", "ok")

    def started(pid, when):
        seen.append((pid, when))

    def run(*args, **kwargs):
        return original(*args, **kwargs, on_start=started)

    original = claude.turn
    monkeypatch.setattr(claude, "turn", run)
    call()
    assert len(seen) == 1 and seen[0][1]


# --- reading a finished turn -----------------------------------------------


def test_a_finished_turns_answer_is_recognized(repo, run_dir, call):
    call()
    assert claude.finished(run_dir, SESSION) == "FAKE-CLAUDE-ANSWER"
    assert claude.finished(run_dir, OTHER) is None


def test_an_unfinished_turn_has_no_answer(run_dir, call):
    with pytest.raises(claude.CallUncertain):
        call("no_result")
    assert claude.finished(run_dir, SESSION) is None


def write_events(run_dir: Path, results: list[tuple[str, bool]]) -> None:
    lines = []
    for i, (text, error) in enumerate(results):
        tool = {"type": "tool_use", "id": f"t{i}", "name": "Bash"}
        tool["input"] = {"command": f"command number {i}"}
        lines.append({"type": "assistant", "message": {"content": [tool]}})
        output = {
            "type": "tool_result",
            "tool_use_id": f"t{i}",
            "content": text,
            "is_error": error,
        }
        lines.append({"type": "user", "message": {"content": [output]}})
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines)
    )


def test_refusals_are_counted_apart_from_ordinary_failures(run_dir):
    write_events(
        run_dir,
        [
            ("ok", False),
            (
                "File is in a directory that is denied by your permission "
                "settings.",
                True,
            ),
            (
                "Permission for this action was denied by the Claude Code "
                "auto "
                "mode classifier.",
                True,
            ),
            ("Exit code 1\n(eval):1: operation not permitted: hello.py", True),
            (
                "Exit code 56\n<sandbox_violations>\ndeny network-outbound",
                True,
            ),
            ("Exit code 1\nAssertionError: 2 != 3", True),
        ],
    )
    counts = claude.stats(run_dir)
    assert (counts["tools"], counts["refused"], counts["failed"]) == (6, 4, 1)
    assert counts["actions"] == [
        "Bash command number 3",
        "Bash command number 4",
        "Bash command number 5",
    ]


def test_progress_lines_show_only_time_and_tool_calls(
    call, monkeypatch, capsys
):
    monkeypatch.setattr(claude, "PROGRESS_SECONDS", 0.25)
    call("slow")
    lines = capsys.readouterr().err.splitlines()
    assert lines, "no progress line"
    for line in lines:
        assert re.search(
            r"^xreview: Claude still working — 0m \ds, (0 tool calls|1 tool "
            r"call)$",
            line,
        ), line


# --- the setup check -------------------------------------------------------


@pytest.fixture
def preflight(repo, run_dir, folders, monkeypatch):
    def run(case: str = "ok"):
        monkeypatch.setenv("PATH", repo.env["PATH"])
        monkeypatch.setenv("FAKE_CLAUDE_CASE", case)
        monkeypatch.setenv(
            "FAKE_CLAUDE_LOG", str(repo.tmp / "claude-calls.jsonl")
        )
        claude.preflight(repo.path, run_dir)

    return run


def test_the_setup_check_runs_a_command_in_an_unsaved_turn(
    repo, run_dir, preflight
):
    preflight()
    logged = last_call(repo)
    assert logged["argv"][4] == "--no-session-persistence"
    assert logged["argv"][5:] == [
        *SESSION_B_FLAGS,
        str(run_dir / "settings.json"),
    ]
    assert "echo xreview-sandbox-ok" in logged["prompt"]


@pytest.mark.parametrize(
    "case, message",
    [
        ("mode_default", "Auto mode isn't available here"),
        ("sandbox_broken", "sandbox couldn't run a shell command"),
        ("startup_fail", "exited before starting"),
    ],
)
def test_the_setup_check_stops_without_auto_or_a_working_sandbox(
    preflight, case, message
):
    with pytest.raises(claude.CallFailed, match=message):
        preflight(case)
