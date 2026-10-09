"""Tests for start, continue and append, run through bin/xreview against
real temporary git repos with the fake codex. There are no model calls."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from test_codex_provider import SESSION, process_gone
from test_prepare import ROOT, XREVIEW, RepoCase
from xreview import cli

STEP3 = "Step 3 — Claude rebuttal — sent to Codex"
STEP4 = "Step 4 — Codex response"
RESPONSE_MARKER = "FAKE-RESPONSE-BODY"


class ReviewCallTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.review = self.prepare()
        self.name = self.review.name
        self.write_prompt(self.review, cli.simplification_block())
        self.log = self.tmp / "codex-calls.jsonl"

    def run_xreview(self, *args: str, case: str = "ok"):
        return self.xreview(*args, FAKE_CODEX_CASE=case,
                            FAKE_CODEX_LOG=str(self.log))

    def start(self, case: str = "ok") -> subprocess.CompletedProcess:
        return self.run_xreview("start", self.name, case=case)

    def continue_(self, body: str, templates=("code/claude-handoff-guide",),
                  case: str = "ok") -> subprocess.CompletedProcess:
        args = ["continue", self.name]
        for template in templates:
            args += ["--template", template]
        args += ["--body", body, "--sent-heading", STEP3,
                 "--response-heading", STEP4]
        return self.run_xreview(*args, case=case)

    def message(self, name: str, text: str) -> str:
        (self.review / "messages").mkdir(exist_ok=True)
        (self.review / "messages" / name).write_text(text)
        return f"messages/{name}"

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def transcript(self) -> str:
        return (self.review / "transcript.md").read_text()

    def session(self) -> str | None:
        state = json.loads((self.review / "state.json").read_text())
        return state["reviewer_session_id"]

    def test_start_sends_the_prompt_unchanged_and_records_both_sides(self):
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = (self.review / "reviewer-prompt.md").read_text()
        self.assertEqual([call["stdin"] for call in self.calls()], [prompt])
        self.assertEqual(self.session(), SESSION)
        run = self.review / "runs" / "01-step-2-codex-review"
        for name in ("prompt.md", "response.md", "events.jsonl",
                     "stderr.txt"):
            self.assertTrue((run / name).is_file(), name)
        self.assertEqual((run / "prompt.md").read_text(), prompt)
        text = self.transcript()
        self.assertRegex(text, r"## Reviewer prompt — sent to Codex\n"
                               r"_[^\n]+ · run 01_\n\n" + re.escape(prompt))
        self.assertRegex(text, r"## Step 2 — Codex review\n"
                               r"_[^\n]+ · run 01 · \d+s_\n\n"
                               r"Finding 1: nothing real\.\n\n"
                               + RESPONSE_MARKER + "\n\n---\n\n$")
        status, path = result.stdout.splitlines()
        self.assertRegex(status, r"^Step 2 — Codex review: done in \d+s "
                                 r"\(run 01\)\.$")
        self.assertEqual(path, f"Response: {run / 'response.md'}")
        self.assertNotIn(RESPONSE_MARKER, result.stdout + result.stderr)

    def test_start_refuses_when_a_session_exists(self):
        self.start()
        before = self.transcript()
        result = self.start()
        self.assertEqual(result.returncode, 1)
        self.assertIn("already started", result.stderr)
        self.assertEqual((len(self.calls()), self.transcript()), (1, before))

    def test_continue_refuses_without_a_session(self):
        before = self.transcript()
        result = self.continue_(self.message("rebuttal.md", "Rebuttal\n"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("run start first", result.stderr)
        self.assertEqual((self.calls(), self.transcript()), ([], before))

    def test_moved_head_or_modified_file_refused_before_codex_runs(self):
        body = self.message("rebuttal.md", "Rebuttal\n")

        def refused(run, message):
            before = (len(self.calls()), self.transcript())
            result = run()
            self.assertEqual(result.returncode, 1)
            self.assertIn(message, result.stderr)
            self.assertEqual((len(self.calls()), self.transcript()), before)

        for label, run in (("start", self.start),
                           ("continue", lambda: self.continue_(body))):
            with self.subTest(label):
                (self.repo / "a.txt").write_text("edited\n")
                refused(run, "commit or stash")
                self.git("checkout", "--", "a.txt")
                self.commit("c.txt", "later\n", "Later commit")
                refused(run, "HEAD has moved")
                self.git("reset", "-q", "--hard", self.head)
                if label == "start":
                    self.assertEqual(self.start().returncode, 0)
        self.assertEqual(len(self.calls()), 1)

    def test_tracked_edit_during_the_call_rejects_the_response(self):
        result = self.start("edit_tracked")
        self.assertEqual(result.returncode, 3)
        self.assertIn("(FAILED)", result.stderr)
        self.assertIn("M a.txt", result.stderr)
        text = self.transcript()
        self.assertIn("## Step 2 — Codex review (FAILED)", text)
        self.assertIn("the tree changed during the call", text)
        self.assertIn(" M a.txt", text)
        self.assertNotIn(RESPONSE_MARKER, text + result.stdout)
        response = self.review / "runs/01-step-2-codex-review/response.md"
        self.assertIn(RESPONSE_MARKER, response.read_text())
        self.assertIsNone(self.session())

    def test_untracked_file_created_during_the_call_is_accepted(self):
        result = self.start("create_untracked")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.session(), SESSION)

    def test_continue_composes_byte_exact_and_resumes_the_session(self):
        self.start()
        body_text = "  Body keeps {base} and {head} as written.\n\n\n"
        body = self.message("03-rebuttal.md", body_text)
        templates = ("shared/protocol", "code/claude-handoff-guide")
        result = self.continue_(body, templates)
        self.assertEqual(result.returncode, 0, result.stderr)
        protocol = (ROOT / "templates/shared/protocol.md").read_text()
        handoff = (ROOT / "templates/code/claude-handoff-guide.md").read_text()
        handoff = handoff.replace("{base}", self.base)
        handoff = handoff.replace("{head}", self.head)
        expected = (protocol.rstrip("\n") + "\n\n" + handoff.rstrip("\n")
                    + "\n\n  Body keeps {base} and {head} as written.\n")
        call = self.calls()[1]
        self.assertEqual(call["stdin"], expected)
        self.assertEqual(call["argv"][:3], ["exec", "resume", SESSION])
        run = self.review / "runs" / "02-step-4-codex-response"
        self.assertEqual((run / "prompt.md").read_text(), expected)
        text = self.transcript()
        self.assertRegex(text, f"## {STEP3}\n_[^\n]+ · run 02_\n\n"
                               + re.escape(expected))
        self.assertIn(f"## {STEP4}\n_", text)
        self.assertNotIn(RESPONSE_MARKER, result.stdout + result.stderr)

    def test_paths_outside_the_review_folder_are_refused(self):
        self.start()
        before = (len(self.calls()), self.transcript())
        (self.tmp / "secret.md").write_text("SECRET\n")
        (self.repo / ".reviews" / "elsewhere.md").write_text("SECRET\n")
        (self.review / "messages").mkdir()
        (self.review / "messages" / "link.md").symlink_to(
            self.tmp / "secret.md")
        for path in (str(self.tmp / "secret.md"), "../elsewhere.md",
                     "messages/link.md", "messages/missing.md"):
            with self.subTest(path):
                result = self.continue_(path)
                self.assertEqual(result.returncode, 1)
                self.assertIn("isn't a file inside", result.stderr)
                result = self.run_xreview("append", self.name, "--heading",
                                          "Plan", "--file", path)
                self.assertEqual(result.returncode, 1)
        self.assertEqual((len(self.calls()), self.transcript()), before)

    def test_codex_failure_is_recorded_and_a_retry_is_marked(self):
        result = self.start("exit")
        self.assertEqual(result.returncode, 3)
        self.assertIn("Authentication failed", result.stderr)
        self.assertIn("## Step 2 — Codex review (FAILED)", self.transcript())
        self.assertIsNone(self.session())
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.transcript()
        self.assertIn("## Reviewer prompt — sent to Codex (attempt 2)\n", text)
        self.assertIn("## Step 2 — Codex review (attempt 2)\n", text)
        self.assertTrue((self.review / "runs/02-step-2-codex-review-attempt-2"
                         ).is_dir())

    def test_a_stop_signal_kills_codex_and_is_recorded(self):
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(sig.name):
                pids = self.tmp / f"pids-{sig.name}"
                env = {**self.env, "FAKE_CODEX_CASE": "timeout",
                       "FAKE_CODEX_PIDS": str(pids)}
                runner = subprocess.Popen(
                    [str(XREVIEW), "start", self.name], cwd=self.repo,
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True)
                deadline = time.monotonic() + 10
                while not pids.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                runner.send_signal(sig)
                _, stderr = runner.communicate(timeout=10)
                self.assertEqual(runner.returncode, 3, stderr)
                self.assertIn(f"stopped ({sig.name})", stderr)
                for pid in map(int, pids.read_text().split()):
                    self.assertTrue(process_gone(pid), f"{pid} survived")
                self.assertRegex(self.transcript(),
                                 r"Step 2 — Codex review( \(attempt \d\))? "
                                 rf"\(FAILED\)\n_[^\n]+_\n\nCodex call failed:"
                                 rf" The runner was stopped \({sig.name}\)")
        self.assertIsNone(self.session())

    def test_a_stop_that_also_kills_codex_is_recorded(self):
        # A stop from Claude Code signals the whole process tree at once, so
        # Codex is often dead but not yet reaped when the runner kills its
        # group. macOS reports that as EPERM rather than ESRCH.
        pids = self.tmp / "pids"
        env = {**self.env, "FAKE_CODEX_CASE": "timeout",
               "FAKE_CODEX_PIDS": str(pids)}
        runner = subprocess.Popen(
            [str(XREVIEW), "start", self.name], cwd=self.repo, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 10
        while not pids.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        codex_pid = int(pids.read_text().split()[0])
        os.killpg(codex_pid, signal.SIGKILL)
        runner.send_signal(signal.SIGTERM)
        _, stderr = runner.communicate(timeout=10)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(runner.returncode, 3, stderr)
        self.assertIn("## Step 2 — Codex review (FAILED)", self.transcript())

    def test_append_records_claudes_section_word_for_word(self):
        plan = "## Part 1\n\n- keep this line exactly\n"
        result = self.run_xreview("append", self.name, "--heading",
                                  "Step 5 — Claude final plan", "--file",
                                  self.message("plan.md", plan))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(self.transcript(),
                         r"## Step 5 — Claude final plan\n_[^\n]+_\n\n"
                         + re.escape(plan) + "\n---\n\n$")

    def test_concurrent_appends_stay_intact(self):
        bodies = {i: f"Section {i} line.\n" * 12000 for i in range(6)}
        for i, body in bodies.items():
            self.message(f"{i}.md", body)
        processes = [
            subprocess.Popen([str(XREVIEW), "append", self.name, "--heading",
                              f"Section {i}", "--file", f"messages/{i}.md"],
                             cwd=self.repo, env=self.env,
                             stdout=subprocess.DEVNULL)
            for i in bodies]
        self.assertEqual([p.wait(timeout=30) for p in processes], [0] * 6)
        text = self.transcript()
        for i, body in bodies.items():
            self.assertRegex(text, f"## Section {i}\n_[^\n]+_\n\n"
                                   + re.escape(body) + "\n---\n\n")

    def test_usage_errors_exit_2(self):
        for args in (("continue", self.name, "--template", "shared/protocol"),
                     ("append", self.name, "--file", "x.md"),
                     ("start",)):
            with self.subTest(args):
                self.assertEqual(self.run_xreview(*args).returncode, 2)


class ComposeTests(unittest.TestCase):
    def test_only_trailing_newlines_change_at_part_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            templates = Path(folder).resolve()
            (templates / "a.md").write_text("A {base}\n\n\n")
            (templates / "b.md").write_text("  B {head}")
            state = {"base": "1" * 40, "reviewed_head": "2" * 40}
            with mock.patch.object(cli, "TEMPLATES", templates):
                message = cli.compose(["a", "b", "a"], state,
                                      "C {base}\n  \n\n")
        self.assertEqual(message, f"A {'1' * 40}\n\n  B {'2' * 40}\n\n"
                                  f"A {'1' * 40}\n\nC {{base}}\n  \n")


if __name__ == "__main__":
    unittest.main()
