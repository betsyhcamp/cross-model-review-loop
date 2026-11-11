"""xreview run: the runner that drives a review.

Code owns every operation where order matters: which reviewer is asked
what, in which order, and only once. Claude and Codex own the discussion.

Each provider call is a step. A step is saved before it's sent, with its
run folder, and with the reviewer's process ID and start time once it
starts; it's saved again when its answer is recorded. A restart picks up
the pending step: it waits for a reviewer that's still running, uses an
answer that a reviewer finished on its own, and otherwise stops as
uncertain. A step that may have run is resent only with --retry-uncertain.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import uuid

from . import cli, decisions, procs, transcript
from .providers import claude, codex

STATE = "run-state.json"
LOCK = "run.lock"
DONE, LAUNCHED, FAILED, UNCERTAIN = "done", "launched", "failed", "uncertain"
REVIEWER_PROMPT_SENT = "Reviewer prompt — sent to Codex"
CODEX_REVIEW = "Step 2 — Codex review"
REBUTTAL = "Step 3 — Claude rebuttal"
REBUTTAL_SENT = "Step 3 — Claude rebuttal — sent to Codex"
CODEX_RESPONSE = "Step 4 — Codex response"
PLAN = "Step 5 — Claude final plan"
DECISION_COPY = "Decision copy"
RANKED_BY_BOTH = "the ranking you wrote before seeing the reviewer's"


def crash(point: str) -> None:
    """Test hook: die abruptly at a named point, as a kill -9 would."""
    if os.environ.get("XREVIEW_TEST_CRASH") == point:
        os._exit(137)


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def join(*parts: str) -> str:
    """Parts one blank line apart, as cli.compose joins a message."""
    return "\n\n".join(part.rstrip("\n") for part in parts) + "\n"


class Lock:
    """One xreview command at a time on a review, so each reviewer has one
    writer. The lock file names its owner by process ID and start time; a
    lock whose owner is gone is stale and is cleared."""

    def __init__(self, review: Path):
        self.path = review / LOCK

    def __enter__(self) -> Lock:
        me = {"pid": os.getpid(), "started": procs.identity(os.getpid())}
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self.held():
                    owner = json.loads(self.path.read_text())
                    raise cli.CheckFailed(
                        f"another xreview command (pid {owner['pid']}) is "
                        "working on this review. Wait for it to finish."
                    ) from None
                continue
            with os.fdopen(fd, "w") as f:
                json.dump(me, f)
            return self

    def held(self) -> bool:
        """Whether a live process holds the lock; clears it if not. A lock
        file that's still empty belongs to a command that's just starting.
        """
        try:
            text = self.path.read_text()
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        try:
            owner = json.loads(text)
        except ValueError:
            if age < 5:
                time.sleep(0.1)
                return False
            owner = {}
        if procs.alive(owner.get("pid"), owner.get("started")):
            return True
        self.path.unlink(missing_ok=True)
        return False

    def __exit__(self, *exc) -> None:
        self.path.unlink(missing_ok=True)


class Runner:
    """One xreview command on one review, holding its run state."""

    def __init__(self, name: str, retry: bool = False):
        cli.enter_repo()
        self.name = name
        self.review = cli.review_dir(name)
        self.retry = retry
        self.state = transcript.read_state(self.review)
        self.st = self.load()

    # --- state ---------------------------------------------------------------

    def load(self) -> dict:
        path = self.review / STATE
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {
            "claude_session": None,
            "claude_started": False,
            "steps": {},
            "op": None,
            "stopped": False,
            "decisions": {},
        }

    def save(self) -> None:
        write_atomic(self.review / STATE, json.dumps(self.st, indent=2) + "\n")

    def template(self, name: str, **fields: str) -> str:
        """A template with {base} and {head} filled, plus {name} and any
        fields given. Only templates are filled, never a reviewer's text
        or the developer's words."""
        text = cli.fill(cli.read_template(name), self.state)
        for key, value in {"name": self.name, **fields}.items():
            text = text.replace("{" + key + "}", value)
        return text.rstrip("\n")

    def resent(self, prompt: str) -> str:
        return join(self.template("runner/resend-note"), prompt)

    def path(self, relative: str) -> Path:
        return self.review / relative

    def new_step(self, key: str, run_dir: Path, **fields) -> dict:
        previous = self.st["steps"].get(key)
        rec = {
            "key": key,
            "status": LAUNCHED,
            "run": str(run_dir.relative_to(self.review)),
            "attempt": previous["attempt"] + 1 if previous else 1,
            "began": time.time(),
            "pid": None,
            "started": None,
            **fields,
        }
        self.st["steps"][key] = rec
        self.save()
        return rec

    def recorder(self, rec: dict):
        def record(pid: int, started: str | None) -> None:
            rec["pid"], rec["started"] = pid, started
            self.save()

        return record

    def finish(self, rec: dict) -> None:
        rec["status"] = DONE
        rec["ended"] = time.time()
        self.save()

    # --- recovering a step an earlier run left pending -----------------------

    def recover(self, rec: dict, finished, who: str) -> str | None:
        """The answer of a step an earlier run left pending, or None if
        --retry-uncertain allows resending it. A reviewer that's still
        running is waited for; one that finished on its own has its answer
        used. Otherwise the step is uncertain and the run stops."""
        run_dir = self.path(rec["run"])
        pid, started = rec.get("pid"), rec.get("started")
        if not pid:
            # The runner died before recording the process; find it by its
            # run folder, which is in its command line.
            pid, started = procs.find(str(run_dir.resolve())) or (None, None)
        if procs.alive(pid, started):
            module = claude if who == claude.DISPLAY_NAME else codex
            print(
                f"xreview: {who} is still working on {rec['label']} from an "
                "earlier run; waiting for it to finish.",
                file=sys.stderr,
                flush=True,
            )
            try:
                procs.wait_for_other(
                    pid,
                    started,
                    rec["began"] + module.TIMEOUT_SECONDS,
                    module.PROGRESS_SECONDS,
                    lambda elapsed: module.report_progress(run_dir, elapsed),
                )
            except procs.TimedOut:
                pass
            except procs.Stopped as exc:
                rec["status"] = UNCERTAIN
                self.save()
                raise procs.CallUncertain(
                    f"The runner was stopped ({exc}) while waiting for {who}, "
                    f"which was killed. {rec['label']} may have run."
                ) from exc
        text = finished(run_dir)
        if text is not None:
            return text
        if self.retry:
            return None
        rec["status"] = UNCERTAIN
        self.save()
        raise procs.CallUncertain(
            f"{rec['label']} may have run: {who} was interrupted or left an "
            "incomplete answer, so it wasn't sent again. To send it again, "
            "run the same command with --retry-uncertain."
        )

    def pending(self, key: str, finished, who: str):
        """(answer, resend) for a step that isn't done: an answer recovered
        from an earlier run, or whether the next send is a resend."""
        rec = self.st["steps"].get(key)
        if rec is None:
            return None, False
        if rec["status"] in (LAUNCHED, UNCERTAIN):
            return self.recover(rec, finished, who), True
        return None, True  # FAILED: a plain retry

    # --- Codex steps ---------------------------------------------------------

    def codex_step(
        self, key: str, message: str, sent: str, received: str
    ) -> str:
        """Send message to the review's Codex thread once, starting the
        thread if there's none, and return Codex's reply."""
        rec = self.st["steps"].get(key)
        if rec and rec["status"] == DONE:
            return self.path(rec["out"]).read_text(encoding="utf-8")
        expected = self.state["reviewer_session_id"]

        def finished(run_dir: Path) -> tuple[str, str] | None:
            try:
                return codex.parse(run_dir, expected)
            except (codex.CallFailed, codex.CallUncertain):
                return None

        found, resend = self.pending(key, finished, codex.DISPLAY_NAME)
        if found:
            return self.codex_accept(self.st["steps"][key], *found)
        problems = cli.tree_problems(self.state)
        if problems:
            raise cli.CheckFailed("\n".join(problems))
        if resend:
            message = self.resent(message)
        number = transcript.attempt(self.review, sent)
        if number > 1:
            sent += f" (attempt {number})"
            received += f" (attempt {number})"
        run_dir = transcript.new_run(self.review, received)
        rec = self.new_step(
            key,
            run_dir,
            label=received,
            received=received,
            out=str((run_dir / "response.md").relative_to(self.review)),
        )
        transcript.append(
            self.review,
            sent,
            message,
            f"{transcript.now()} · run {run_dir.name[:2]}",
        )
        crash(f"{key}:before")
        where = run_dir.resolve()
        try:
            if expected is None:
                session, reply = codex.start(
                    message, Path.cwd(), where, self.recorder(rec)
                )
            else:
                session = expected
                reply = codex.resume(
                    expected, message, Path.cwd(), where, self.recorder(rec)
                )
        except (codex.CallFailed, codex.CallUncertain) as exc:
            self.fail(rec, run_dir, exc, codex.DISPLAY_NAME)
        crash(f"{key}:after")
        return self.codex_accept(rec, session, reply)

    def codex_accept(self, rec: dict, session: str, reply: str) -> str:
        run_dir = self.path(rec["run"])
        self.check_tree(rec, run_dir, codex.DISPLAY_NAME)
        if self.state["reviewer_session_id"] is None:
            self.state["reviewer_session_id"] = session
            transcript.write_state(self.review, self.state)
        took = cli.duration(time.time() - rec["began"])
        transcript.append_once(
            self.review,
            rec["received"],
            reply,
            f"{transcript.now()} · run {run_dir.name[:2]} · {took}",
        )
        self.finish(rec)
        self.done(rec["received"], took, run_dir)
        return reply

    # --- Claude steps --------------------------------------------------------

    def claude_step(
        self,
        key: str,
        tag: str,
        label: str,
        prompt: str,
        out: str,
        heading: str | None = None,
        blocked: list[Path] | None = None,
    ) -> str:
        """One Session B turn, once. The answer is saved to out (relative
        to the review folder) and, with a heading, recorded in the
        transcript. tag names the run folder; label names the step."""
        rec = self.st["steps"].get(key)
        if rec and rec["status"] == DONE:
            return self.path(rec["out"]).read_text(encoding="utf-8")
        session_of = rec["session"] if rec else None
        found, resend = self.pending(
            key,
            lambda run_dir: claude.finished(run_dir, session_of),
            claude.DISPLAY_NAME,
        )
        if found is not None:
            return self.claude_accept(self.st["steps"][key], found)
        problems = cli.tree_problems(self.state)
        if problems:
            raise cli.CheckFailed("\n".join(problems))
        first = not self.st["claude_started"]
        if first:
            # A first turn that may have started its session can't reuse
            # the ID, so a resend starts a new session.
            self.st["claude_session"] = str(uuid.uuid4())
        session = self.st["claude_session"]
        run_dir = transcript.new_run(self.review, f"claude {tag}")
        rec = self.new_step(
            key,
            run_dir,
            label=label,
            out=out,
            heading=heading,
            session=session,
            first=first,
        )
        crash(f"{key}:before")
        try:
            text = claude.turn(
                self.resent(prompt) if resend else prompt,
                Path.cwd(),
                run_dir.resolve(),
                session,
                first,
                blocked,
                self.recorder(rec),
            )
        except (claude.CallFailed, claude.CallUncertain) as exc:
            self.fail(rec, run_dir, exc, claude.DISPLAY_NAME)
        crash(f"{key}:after")
        return self.claude_accept(rec, text)

    def claude_accept(self, rec: dict, text: str) -> str:
        run_dir = self.path(rec["run"])
        self.check_tree(rec, run_dir, claude.DISPLAY_NAME)
        if rec["first"]:
            self.st["claude_started"] = True
        write_atomic(self.path(rec["out"]), text)
        crash(f"{rec['key']}:saved")
        took = cli.duration(time.time() - rec["began"])
        if rec.get("heading"):
            transcript.append_once(
                self.review,
                rec["heading"],
                text,
                f"{transcript.now()} · run {run_dir.name[:2]} · {took}",
            )
        self.finish(rec)
        self.done(rec["label"], took, run_dir)
        return text

    # --- shared by both ------------------------------------------------------

    def check_tree(self, rec: dict, run_dir: Path, who: str) -> None:
        """The tree check after a call: an answer given while HEAD moved or
        a tracked file changed is rejected, as a known failure."""
        changed = cli.tree_changes(self.state)
        if changed:
            rec["status"] = FAILED
            self.save()
            raise cli.record_failure(
                self.review, rec["label"], run_dir, None, changed, who
            )

    def fail(
        self, rec: dict, run_dir: Path, error: Exception, who: str
    ) -> None:
        changed = cli.tree_changes(self.state)
        uncertain = isinstance(error, procs.CallUncertain)
        rec["status"] = UNCERTAIN if uncertain else FAILED
        self.save()
        raise cli.record_failure(
            self.review, rec["label"], run_dir, error, changed, who
        )

    def done(self, label: str, took: str, run_dir: Path) -> None:
        print(
            f"xreview: {label}: done in {took} (run {run_dir.name[:2]}).",
            file=sys.stderr,
            flush=True,
        )

    # --- the review ----------------------------------------------------------

    def setup_check(self) -> None:
        """Before the review's first call, show that Session B's sandbox
        runs a command and its mode is Auto. The check's turn isn't saved,
        so repeating it is always safe."""
        key = "run:setup-check"
        rec = self.st["steps"].get(key)
        if rec and rec["status"] == DONE:
            return
        run_dir = transcript.new_run(self.review, "claude setup check")
        rec = self.new_step(key, run_dir, label="Claude setup check")
        try:
            claude.preflight(Path.cwd(), run_dir.resolve())
        except (claude.CallFailed, claude.CallUncertain) as exc:
            rec["status"] = FAILED
            self.save()
            raise claude.CallFailed(
                f"{exc}\nDiagnostics: `{run_dir}/`"
            ) from exc
        self.finish(rec)

    def run(self) -> None:
        """Start the review, or pick it up from the last finished step."""
        if self.st["stopped"] and not self.st["op"]:
            self.report_stop()
            return
        if not self.st["op"]:
            self.st["op"] = {"kind": "run"}
            self.save()
        if not self.st["steps"].get("run:codex-review"):
            problems = cli.tree_problems(self.state) + cli.prompt_problems(
                self.review, self.state
            )
            if problems:
                raise cli.CheckFailed("\n".join(problems))
        self.setup_check()
        prompt = (self.review / "reviewer-prompt.md").read_text(
            encoding="utf-8"
        )
        feedback = self.codex_step(
            "run:codex-review", prompt, REVIEWER_PROMPT_SENT, CODEX_REVIEW
        )
        rebuttal = self.claude_step(
            "run:rebuttal",
            "rebuttal",
            REBUTTAL,
            join(
                self.template("runner/session-b-header"),
                self.template("runner/first-turn"),
                self.template("code/step3-claude-rebuttal"),
                feedback,
            ),
            "messages/03-rebuttal.md",
        )
        response = self.codex_step(
            "run:codex-response",
            cli.compose(
                ["code/step4-reviewer-response"], self.state, rebuttal
            ),
            REBUTTAL_SENT,
            CODEX_RESPONSE,
        )
        self.claude_step(
            "run:plan",
            "plan",
            PLAN,
            join(self.template("code/step5-claude-plan"), response),
            "messages/05-plan.md",
            heading=PLAN,
        )
        copy = self.claude_step(
            "run:decision-copy",
            "decision copy",
            DECISION_COPY,
            self.template("runner/decision-copy"),
            "messages/06-decisions-r1.md",
        )
        if not decisions.is_none(copy):
            if not self.st["decisions"]:
                copy, source = self.checked(
                    "run:decision-copy",
                    copy,
                    decisions.renumbered(copy, 1),
                    "fix-up decision copy",
                    DECISION_COPY,
                )
                self.register(copy, source)
            self.ranking_round(1, sorted(self.numbers()))
        self.stop()

    # --- the open decisions --------------------------------------------------

    def numbers(self) -> list[int]:
        return [int(n) for n in self.st["decisions"]]

    def register(self, text: str, source: str) -> None:
        """Give each decision in text its permanent number and title, with
        source as the file holding its description."""
        for n, title in decisions.headings(text):
            self.st["decisions"].setdefault(
                str(n), {"title": title, "description": source}
            )
        self.save()

    def titled(self, numbers: list[int]) -> list[tuple[int, str]]:
        return [(n, self.st["decisions"][str(n)]["title"]) for n in numbers]

    def descriptions(self, numbers: list[int]) -> str:
        """The description of each decision, as its heading and section."""
        parts = []
        for n in numbers:
            entry = self.st["decisions"][str(n)]
            text = self.path(entry["description"]).read_text(encoding="utf-8")
            parts.append(decisions.sections(text)[n])
        return "\n\n".join(parts)

    def checked(
        self,
        key: str,
        text: str,
        expected: list[tuple[int, str]],
        tag: str,
        label: str,
    ) -> tuple[str, str]:
        """(text, the file holding it), or Claude's fixed reply and its
        file if text doesn't have exactly one heading per expected
        decision. If the fixed reply doesn't either, it's recorded and the
        step stops, ready to be asked again."""
        numbers = [n for n, _ in expected]
        out = self.st["steps"][key]["out"]
        found = decisions.problems(text, numbers)
        if not found:
            return text, out
        fix_key = f"{key}:fix-up"
        out = out.replace(".md", "-fixed.md")
        fixed = self.claude_step(
            fix_key,
            tag,
            f"{label} — headings fixed",
            self.template(
                "runner/fix-up",
                headings=decisions.heading_lines(expected)
                or "`### Decision 1: <title>`, `### Decision 2: <title>` "
                "and so on, one for each decision",
                problems=" ".join(found),
            ),
            out,
        )
        still = decisions.problems(fixed, numbers)
        if not still:
            return fixed, out
        transcript.append(
            self.review,
            f"{label} (headings incomplete)",
            fixed,
            transcript.now(),
        )
        del self.st["steps"][key], self.st["steps"][fix_key]
        self.save()
        raise cli.CheckFailed(
            f"{label}: Claude's reply still doesn't have one `### Decision` "
            f"heading for each decision ({' '.join(still)}), after one "
            "request to fix it. Its reply is in the transcript. Run the same "
            "command again to ask Claude for this step again; the reviewer "
            "isn't asked again."
        )

    def ranking_round(
        self, n: int, numbers: list[int], words: str | None = None
    ) -> None:
        """A blind ranking: Codex ranks first, so Claude's ranking doesn't
        exist yet. Claude then ranks with every copy of Codex's ranking
        blocked, and merges the two with Codex's ranking in the prompt."""
        body = self.descriptions(numbers)
        if words is not None:
            body = join(body, f"My question:\n\n{words}")
        ranking = self.codex_step(
            f"round-{n}:codex",
            cli.compose(
                ["shared/decision-ranking", "shared/reviewer-guard"],
                self.state,
                body,
            ),
            f"Decision round {n} — sent to Codex",
            f"Decision round {n} — Codex ranking",
        )
        heading = f"Decision round {n} — Claude ranking"
        self.claude_step(
            f"round-{n}:claude-ranking",
            f"ranking r{n}",
            heading,
            join(
                self.template("shared/decision-ranking"),
                self.template("runner/ranking-blind"),
                body,
            ),
            f"messages/06-claude-ranking-r{n}.md",
            heading=heading,
            blocked=self.codex_copies(),
        )
        self.merge(n, numbers, ranking, RANKED_BY_BOTH)

    def codex_copies(self) -> list[Path]:
        """Everywhere a copy of Codex's ranking is kept: the review's run
        folders, its transcript, and Codex's own home folder."""
        home = os.environ.get("CODEX_HOME") or Path.home() / ".codex"
        return [
            (self.review / "runs").resolve(),
            (self.review / transcript.TRANSCRIPT).resolve(),
            Path(home).resolve(),
        ]

    def merge(
        self, n: int, numbers: list[int], reviewer: str, view: str
    ) -> None:
        """Claude merges the reviewer's view with its own into each
        decision's latest options, which the report then shows."""
        key = f"round-{n}:merge"
        label = f"Decision round {n} — Claude merged options"
        expected = self.titled(numbers)
        text = self.claude_step(
            key,
            f"merge r{n}",
            label,
            join(
                self.template("shared/decision-merge-claude"),
                reviewer,
                self.template(
                    "runner/merge-closing",
                    view=view,
                    headings=decisions.heading_lines(expected),
                ),
            ),
            f"messages/06-merge-r{n}.md",
        )
        text, source = self.checked(key, text, expected, f"fix-up r{n}", label)
        transcript.append_once(self.review, label, text, transcript.now())
        for number in numbers:
            self.st["decisions"][str(number)]["latest"] = {
                "file": source,
                "label": f"From decision round {n}",
            }
        self.save()

    # --- stops ---------------------------------------------------------------

    def stop(self) -> None:
        plan = self.path("messages/05-plan.md").read_text(encoding="utf-8")
        latest = []
        for n in sorted(self.numbers()):
            entry = self.st["decisions"][str(n)].get("latest")
            if entry:
                text = self.path(entry["file"]).read_text(encoding="utf-8")
                latest.append((entry["label"], decisions.sections(text)[n]))
        write_atomic(
            self.review / "report.md",
            decisions.report(self.name, plan, latest),
        )
        self.st["stopped"] = True
        self.st["op"] = None
        self.save()
        self.report_stop()

    def report_stop(self) -> None:
        report = Path.cwd() / self.review / "report.md"
        count = len(self.st["decisions"])
        if count:
            plural = "decision" if count == 1 else "decisions"
            what = f"with {count} open {plural}"
        else:
            what = "with no open decisions"
        print(f"The review is at a stop, {what}. Report: {report}")


def run(name: str, retry: bool) -> None:
    runner = Runner(name, retry)
    with Lock(runner.review):
        runner.st = runner.load()
        runner.run()
