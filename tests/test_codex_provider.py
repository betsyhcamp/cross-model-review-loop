# Portions adapted from claudex-loop
# (https://github.com/chaseai-yt/claudex-loop @ 8cf5e2c), Copyright (c) 2026
# Chase AI, MIT License. See THIRD_PARTY_NOTICES.md.
"""Tests for the Codex adapter, run in-process against the fake codex."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import time
import unittest
from unittest import mock

from test_prepare import RepoCase
from xreview.providers import codex

SESSION = "01a11457-66e5-73e3-8c89-991f3b54c198"
RESPONSE = "Finding 1: nothing real.\n\nFAKE-RESPONSE-BODY\n"


def process_gone(pid: int, wait: float = 5.0) -> bool:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class CodexProviderTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.log = self.tmp / "codex-calls.jsonl"
        self.runs = 0

    def call(self, case: str = "ok", session: str | None = None,
             prompt: str = "Review this.\n", **env: str):
        """Run start (or resume, given a session) in a fresh run folder."""
        self.runs += 1
        self.run_dir = self.tmp / f"run-{self.runs}"
        self.run_dir.mkdir()
        env = {"PATH": self.env["PATH"], "FAKE_CODEX_CASE": case,
               "FAKE_CODEX_LOG": str(self.log), **env}
        with mock.patch.dict(os.environ, env):
            if session:
                return codex.resume(session, prompt, self.repo, self.run_dir)
            return codex.start(prompt, self.repo, self.run_dir)

    def last_call(self) -> dict:
        return json.loads(self.log.read_text().splitlines()[-1])

    def test_start_sends_stdin_read_only_and_returns_session(self):
        self.assertEqual(self.call(prompt="Line 1\nLine 2\n"),
                         (SESSION, RESPONSE))
        call = self.last_call()
        self.assertEqual(call["argv"], [
            "exec", "-s", "read-only", "-c", 'approval_policy="never"',
            "--json", "-o", str(self.run_dir / "response.md"), "-"])
        self.assertEqual(call["stdin"], "Line 1\nLine 2\n")
        self.assertEqual(Path(call["cwd"]), self.repo)

    def test_resume_keeps_read_only_and_the_explicit_session(self):
        self.assertEqual(self.call(session=SESSION), RESPONSE)
        call = self.last_call()
        self.assertEqual(call["argv"], [
            "exec", "resume", SESSION, "-c", 'sandbox_mode="read-only"',
            "-c", 'approval_policy="never"',
            "--json", "-o", str(self.run_dir / "response.md"), "-"])
        self.assertEqual(Path(call["cwd"]), self.repo)

    def test_wrong_session_is_refused(self):
        with self.assertRaisesRegex(codex.ReviewerError, "different session"):
            self.call("wrong_thread", session=SESSION)

    def test_bad_event_streams_raise(self):
        cases = (
            ("no_thread", "0 thread.started"),
            ("bad_uuid", "valid session UUID"),
            ("dup_thread", "2 thread.started"),
            ("dup_completed", "2 turn.completed"),
            ("incomplete", "0 turn.completed"),
            ("turn_failed", "failed turn: quota exceeded"),
            ("nonjson", "isn't JSON"),
            ("empty", "empty response"),
        )
        for case, message in cases:
            with self.subTest(case):
                with self.assertRaisesRegex(codex.ReviewerError, message):
                    self.call(case)
                self.assertTrue((self.run_dir / "events.jsonl").exists())

    def test_error_event_followed_by_completion_is_accepted(self):
        self.assertEqual(self.call("nonfatal_error"), (SESSION, RESPONSE))

    def test_nonzero_exit_includes_the_stderr_tail(self):
        with self.assertRaises(codex.ReviewerError) as caught:
            self.call("exit")
        lines = str(caught.exception).splitlines()
        self.assertIn("Codex exited with code 7. Last lines of stderr:", lines)
        self.assertEqual(lines[-20:-18], ["stderr line 7", "stderr line 8"])
        self.assertEqual(lines[-1], "Authentication failed")
        self.assertNotIn("stderr line 6", lines)

    def test_timeout_kills_the_process_group(self):
        pids = self.tmp / "pids"
        with mock.patch.object(codex, "TIMEOUT_SECONDS", 1):
            with self.assertRaisesRegex(codex.ReviewerError, "timed out"):
                self.call("timeout", FAKE_CODEX_PIDS=str(pids))
        for pid in map(int, pids.read_text().split()):
            self.assertTrue(process_gone(pid), f"process {pid} survived")

    def test_progress_lines_show_only_time_and_commands_run(self):
        stderr = io.StringIO()
        with mock.patch.object(codex, "PROGRESS_SECONDS", 0.25), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(self.call("slow"), (SESSION, RESPONSE))
        lines = stderr.getvalue().splitlines()
        self.assertGreaterEqual(len(lines), 2, lines)
        for line in lines:
            self.assertRegex(line, r"^xreview: Codex still working — 0m \ds, "
                                   r"(0 commands|1 command) run$")
        self.assertTrue(lines[-1].endswith(", 1 command run"))

    def test_codex_missing_raises(self):
        with self.assertRaisesRegex(codex.ReviewerError, "not found"):
            self.call(PATH=str(self.tools))


if __name__ == "__main__":
    unittest.main()
