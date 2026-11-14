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

import contextlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

from . import cli, decisions, procs, transcript, wake
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
LATEST_PART_4 = "your latest Part 4 for these decisions"
PLAN_PART_4 = "your Part 4 in the Step 5 plan"
IN_PLAN = "Claude's own, in Part 4 of the plan above"
# An absolute path in the handoff or the developer's words, which shares
# that file or folder with Session B.
NAMED_PATH = re.compile(
    r"(?<![\w/.~])(~/[^\s`'\"<>()\[\]]+|/[^\s`'\"<>()\[\]]+)"
)
# An answer this long or shorter is printed for Session A to show word for
# word; a longer one is left in its file.
ANSWER_LIMIT = 2000
OPERATIONS = {
    "ask-claude": "Question {n} for Claude",
    "ask-codex": "Question {n} for Codex",
    "rank": "Ranking request {n}",
    "finalize": "Finalize {n}",
}
GROUP_LIMIT = 15  # more groups than this is held for the developer (D15)
DECIDED_PLAN = "Decided Plan"
# The repo's own instruction files that go to Session B, which loads no
# CLAUDE.md itself, when they're tracked at the repo root.
INSTRUCTION_FILES = ("CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md")


def crash(point: str) -> None:
    """Test hook: die abruptly at a named point, as a kill -9 would."""
    if os.environ.get("XREVIEW_TEST_CRASH") == point:
        os._exit(137)


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def and_list(numbers: list[int]) -> str:
    """ "2", or "1, 2 and 4"."""
    if len(numbers) == 1:
        return str(numbers[0])
    return ", ".join(str(n) for n in numbers[:-1]) + f" and {numbers[-1]}"


def listing(numbers: list[int]) -> str:
    """ "decision 2", or "decisions 1, 2 and 4"."""
    plural = "decisions" if len(numbers) > 1 else "decision"
    return f"{plural} {and_list(numbers)}"


def groups(plan: str) -> int:
    """The `### Group` headings under Implement now."""
    start = re.search(r"^## Implement now.*$", plan, re.M)
    if not start:
        return 0
    rest = plan[start.end() :]
    end = re.search(r"^## ", rest, re.M)
    section = rest[: end.start()] if end else rest
    return len(re.findall(r"^### Group\b", section, re.M))


def join(*parts: str) -> str:
    """Parts one blank line apart, as cli.compose joins a message."""
    return "\n\n".join(part.rstrip("\n") for part in parts) + "\n"


def runs_with(rec: dict) -> str:
    """Session B's model and effort, as status and messages name them."""
    model = rec["model"] or rec["reported"] or "Claude Code's default model"
    effort = (
        f"effort {rec['effort']}"
        if rec["effort"]
        else "the effort your Claude Code settings set"
    )
    return f"{model}, {effort}"


def fenced(text: str) -> str:
    """text in a backtick fence longer than any run of backticks in it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    mark = "`" * max(3, longest + 1)
    return f"{mark}\n{text.rstrip(chr(10))}\n{mark}"


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

    def __init__(
        self,
        name: str,
        retry: bool = False,
        alone: bool = False,
        model: str | None = None,
        effort: str | None = None,
    ):
        cli.enter_repo()
        self.name = name
        self.review = cli.review_dir(name)
        self.retry = retry
        self.alone = alone  # --no-decision-rounds
        self.model, self.effort = (
            model,
            effort,
        )  # --claude-model, --claude-effort
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
            "counters": {},
            "dropped": [],
            "held": None,
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

    def shared_paths(self) -> list[Path]:
        """Paths Session B may read even where other agents' records are
        blocked: the absolute paths the handoff names, and those named in
        the developer's words so far. Naming a file shares it."""
        texts = []
        handoff = self.review / "claude-handoff.md"
        if handoff.exists():
            texts.append(handoff.read_text(encoding="utf-8"))
        for words in sorted((self.review / "messages").glob("*-words.md")):
            texts.append(words.read_text(encoding="utf-8"))
        found: list[Path] = []
        for text in texts:
            for match in NAMED_PATH.findall(text):
                path = Path(os.path.expanduser(match.rstrip(".,:;!?")))
                if len(path.parts) > 2 and path.exists() and path not in found:
                    found.append(path)
        return found

    def repo_instructions(self) -> list[str]:
        """The repo's tracked instruction files at its root, each path and
        full text, under the template that puts xreview's instructions
        first; nothing if it has none. Untracked files, such as
        CLAUDE.local.md, are never included."""
        tracked = cli.git_out("ls-files", "--", *INSTRUCTION_FILES).split()
        files = [
            f"`{name}`:\n\n"
            + fenced(Path(name).read_text(encoding="utf-8", errors="replace"))
            for name in INSTRUCTION_FILES
            if name in tracked
        ]
        if not files:
            return []
        return [
            self.template("runner/repo-instructions", files="\n\n".join(files))
        ]

    def session_b(self) -> dict:
        """How Session B runs for this review: the model and effort the
        developer gave, the model choices copied from their settings when
        the review started, and the model Claude Code reported at setup."""
        return self.st.get("session_b") or {
            "model": None,
            "effort": None,
            "choices": {},
            "reported": None,
        }

    def choose_session_b(self) -> None:
        """Settle Session B's model and effort once per review. The
        developer's model choices are copied from their settings the first
        time. A model or effort given is kept, but once Session B's first
        turn has run a different one is refused: its session is fixed."""
        rec = self.st.get("session_b")
        if rec is None:
            rec = {
                "model": None,
                "effort": None,
                "choices": claude.user_choices(),
                "reported": None,
            }
            self.st["session_b"] = rec
        given = {"model": self.model, "effort": self.effort}
        now = {
            "model": rec["model"] or rec["reported"],
            "effort": rec["effort"],
        }
        changed = [
            key for key, value in given.items() if value and value != now[key]
        ]
        if changed and self.st["claude_started"]:
            raise cli.CheckFailed(
                f"Session B's {' and '.join(changed)} can't change once its "
                f"session has started: this review runs with "
                f"{runs_with(rec)}. Start a new review to use another."
            )
        if changed:
            rec.update({key: given[key] for key in changed})
            rec["reported"] = None
            self.st["steps"].pop("run:setup-check", None)
        self.save()

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

    def settle(self, rec: dict, finished, who: str):
        """The answer of a step an earlier run left pending, or None if
        there's none. A reviewer that's still running is waited for, and
        one that finished on its own has its answer used."""
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
        return finished(run_dir)

    def recover(self, rec: dict, finished, who: str):
        """The answer of a step an earlier run left pending, or None if
        --retry-uncertain allows resending it. With neither, the step is
        uncertain and the run stops."""
        found = self.settle(rec, finished, who)
        if found is not None or self.retry:
            return found
        rec["status"] = UNCERTAIN
        self.save()
        raise procs.CallUncertain(
            f"{rec['label']} may have run: {who} was interrupted or left an "
            "incomplete answer, so it wasn't sent again. To send it again, "
            "run the same command with --retry-uncertain."
        )

    def codex_finished(self, run_dir: Path):
        try:
            return codex.parse(run_dir, self.state["reviewer_session_id"])
        except (codex.CallFailed, codex.CallUncertain):
            return None

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
        found, resend = self.pending(
            key, self.codex_finished, codex.DISPLAY_NAME
        )
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
            provider="codex",
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
            provider="claude",
            label=label,
            out=out,
            heading=heading,
            session=session,
            first=first,
        )
        crash(f"{key}:before")
        try:
            runs = self.session_b()
            text = claude.turn(
                self.resent(prompt) if resend else prompt,
                Path.cwd(),
                run_dir.resolve(),
                session,
                first,
                blocked,
                self.recorder(rec),
                review=self.review.resolve(),
                shared=self.shared_paths(),
                # With no model given, the one reported at setup keeps the
                # whole review on one model.
                model=runs["model"] or runs["reported"],
                effort=runs["effort"],
                choices=runs["choices"],
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
        rec = self.new_step(
            key, run_dir, provider="claude", label="Claude setup check"
        )
        runs = self.st["session_b"]
        try:
            runs["reported"] = claude.preflight(
                Path.cwd(),
                run_dir.resolve(),
                runs["model"],
                runs["effort"],
                runs["choices"],
            )
        except (claude.CallFailed, claude.CallUncertain) as exc:
            rec["status"] = FAILED
            self.save()
            raise claude.CallFailed(
                f"{exc}\nDiagnostics: `{run_dir}/`"
            ) from exc
        self.finish(rec)

    def run(self) -> None:
        """Start the review, or pick it up from the last finished step,
        including an operation at a stop that's still pending."""
        self.choose_session_b()
        op = self.st["op"]
        if op and op["kind"] != "run":
            self.resume(op)
            return
        if self.st["stopped"] and not op:
            self.report_stop()
            return
        if not self.st["op"]:
            self.st["op"] = {"kind": "run"}
            self.save()
        plan_done = self.st["steps"].get("run:plan", {}).get("status") == DONE
        if self.alone and not plan_done:
            self.st["no_decision_rounds"] = True
            self.save()
        alone = self.st.get("no_decision_rounds", False)
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
                *self.repo_instructions(),
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
        parts = [self.template("code/step5-claude-plan"), response]
        if alone:
            parts.append(self.template("runner/plan-part4-alone"))
        plan = self.claude_step(
            "run:plan",
            "plan",
            PLAN,
            join(*parts),
            "messages/05-plan.md",
            heading=PLAN,
        )
        if alone:
            self.plan_decisions(plan)
            self.stop()
            return
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

    def plan_decisions(self, plan: str) -> None:
        """Under --no-decision-rounds, the decisions come from the plan's
        own Part 4, which is also each one's latest options until Codex
        ranks it."""
        if self.st["decisions"] or not decisions.headings(plan):
            return
        text, source = self.checked(
            "run:plan",
            plan,
            decisions.renumbered(plan, 1),
            "fix-up plan",
            PLAN,
        )
        if source != self.st["steps"]["run:plan"]["out"]:
            transcript.append_once(
                self.review, f"{PLAN} — headings fixed", text, transcript.now()
            )
            self.st["plan"] = source
        for n, title in decisions.headings(text):
            self.st["decisions"][str(n)] = {
                "title": title,
                "latest": {"file": source, "label": IN_PLAN, "in_plan": True},
            }
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
        for number in numbers:
            self.st["decisions"][str(number)]["codex"] = True
        self.save()

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

    # --- operations at a stop ------------------------------------------------

    def words(self, given: str) -> str:
        """The developer's words, from a file in the review's inbox/."""
        path = cli.review_file(self.review, given)
        inbox = (self.review / "inbox").resolve()
        if not path.is_relative_to(inbox):
            raise cli.CheckFailed(
                f"--file must name a file in {self.review}/inbox/, such as "
                "inbox/3.txt."
            )
        return path.read_text(encoding="utf-8")

    def label(self, op: dict) -> str:
        return OPERATIONS[op["kind"]].format(n=op["n"])

    def at_stop(self, kind: str, given: str, numbers: list[int] | None):
        """Carry the developer's words to a reviewer. The same command and
        words as a pending operation pick it up; anything else drops it,
        once any answer it has left is recorded."""
        if not self.st["stopped"]:
            raise cli.CheckFailed(
                "The review hasn't reached its first stop yet, so there's "
                f"nothing to {kind} about. Run `xreview run {self.name}` to "
                "pick it up."
            )
        words = self.words(given)
        op = self.st["op"]
        if op:
            same = (
                op["kind"] == kind
                and self.path(op["words"]).read_text(encoding="utf-8") == words
                and op.get("asked") == numbers
            )
            if same:
                self.resume(op)
                return
            self.drop(op, kind)
        self.resume(self.create(kind, words, numbers))

    def create(self, kind: str, words: str, numbers: list[int] | None):
        """A new operation, numbered now, with a copy of the words, which
        the transcript records before any reviewer sees them."""
        if numbers is not None:
            unknown = [
                n for n in numbers if str(n) not in self.st["decisions"]
            ]
            if unknown:
                known = sorted(self.numbers())
                listed = ", ".join(str(n) for n in known) or "none"
                raise cli.CheckFailed(
                    f"There's no decision {unknown[0]}. The open decisions "
                    f"are {listed}. To have a new decision ranked, use rank."
                )
        counters = self.st["counters"]
        counters[kind] = counters.get(kind, 0) + 1
        op = {"kind": kind, "n": counters[kind], "asked": numbers}
        op["words"] = f"messages/{kind}-{op['n']}-words.md"
        write_atomic(self.path(op["words"]), words)
        op["prefixes"] = [f"{kind}-{op['n']}:"]

        def next_round(field: str) -> None:
            counters["round"] = max(counters.get("round", 1), 1) + 1
            op[field] = counters["round"]
            op["prefixes"].append(f"round-{op[field]}:")

        if kind == "rank":
            next_round("round")
            op["next"] = max(self.numbers(), default=0) + 1
        if kind == "ask-codex" and self.st["decisions"]:
            # Under --no-decision-rounds, a decision only Claude has ranked
            # gets a ranking round first; the rest get a follow-up round.
            asked = numbers or sorted(self.numbers())
            entries = self.st["decisions"]
            op["unranked"] = [
                n for n in asked if not entries[str(n)].get("codex")
            ]
            op["ranked"] = [n for n in asked if n not in op["unranked"]]
            if op["unranked"]:
                next_round("ranking_round")
            if op["ranked"]:
                next_round("round")
        self.st["op"] = op
        self.save()
        transcript.append_once(
            self.review,
            f"{self.label(op)} — your words",
            words,
            transcript.now(),
        )
        return op

    def drop(self, op: dict, kind: str) -> None:
        """Drop a pending operation for a new one. A reviewer still working
        on it is waited for, and any answer it finished is recorded, but
        nothing is resent. A question whose answer turns out to be
        complete isn't dropped."""
        if op["kind"] == "run":
            raise cli.CheckFailed(
                "The review hasn't reached its first stop yet. Run "
                f"`xreview run {self.name}` to pick it up."
            )
        steps = {
            key: rec
            for key, rec in self.st["steps"].items()
            if key.startswith(tuple(op["prefixes"]))
        }
        for rec in steps.values():
            if rec["status"] not in (LAUNCHED, UNCERTAIN):
                continue
            if rec["provider"] == "codex":
                found = self.settle(rec, self.codex_finished, "Codex")
                if found is not None:
                    self.codex_accept(rec, *found)
            else:
                found = self.settle(
                    rec,
                    lambda d, s=rec["session"]: claude.finished(d, s),
                    "Claude",
                )
                if found is not None:
                    self.claude_accept(rec, found)
        label = self.label(op)
        single = "round" not in op and len(steps) == 1
        if single and all(rec["status"] == DONE for rec in steps.values()):
            (rec,) = steps.values()
            print(
                f"xreview: {label} finished after all. Its answer is in "
                f"{Path.cwd() / self.path(rec['out'])}",
                file=sys.stderr,
                flush=True,
            )
        else:
            moved_to = OPERATIONS[kind].format(
                n=self.st["counters"].get(kind, 0) + 1
            )
            transcript.append(
                self.review,
                f"{label} — dropped",
                f"You moved on to {moved_to} before {label} had a complete "
                "answer, so it was dropped. Nothing was resent.",
                transcript.now(),
            )
            self.st["dropped"].append(label)
            print(f"xreview: {label} was dropped.", file=sys.stderr)
        self.st["op"] = None
        self.save()

    def resume(self, op: dict) -> None:
        handlers = {
            "ask-claude": self.ask_claude,
            "ask-codex": self.ask_codex,
            "rank": self.rank,
            "finalize": self.finalize,
        }
        handlers[op["kind"]](op)
        self.st["op"] = None
        self.save()

    def ask_claude(self, op: dict) -> None:
        """The developer's words, unchanged, to the same Session B."""
        label = self.label(op)
        out = f"messages/ask-claude-{op['n']}.md"
        answer = self.claude_step(
            f"ask-claude-{op['n']}:claude",
            f"question {op['n']}",
            f"{label} — answer",
            self.path(op["words"]).read_text(encoding="utf-8"),
            out,
            heading=f"{label} — answer",
        )
        self.show(
            f"Claude's answer to question {op['n']}",
            answer,
            Path.cwd() / self.path(out),
        )

    def ask_codex(self, op: dict) -> None:
        """The developer's words to the same Codex thread: with the latest
        options of the decisions they name, or of every decision if they
        name none, followed by Claude's merge of those decisions. With no
        open decisions, the words go alone. Codex's own reply is the
        answer."""
        words = self.path(op["words"]).read_text(encoding="utf-8")
        label = self.label(op)
        if "round" not in op and "ranking_round" not in op:
            key = f"ask-codex-{op['n']}:codex"
            reply = self.codex_step(
                key,
                cli.compose(["shared/reviewer-question"], self.state, words),
                f"{label} — sent to Codex",
                f"{label} — answer",
            )
            self.show(
                f"Codex's answer to question {op['n']}",
                reply,
                self.reply_file(key),
            )
            return
        replies, merged = [], []
        if op.get("ranking_round"):
            n, numbers = op["ranking_round"], op["unranked"]
            self.describe(op, numbers)
            key = f"round-{n}:codex"
            ranking = self.codex_step(
                key,
                cli.compose(
                    ["shared/decision-ranking", "shared/reviewer-guard"],
                    self.state,
                    join(
                        self.descriptions(numbers), f"My question:\n\n{words}"
                    ),
                ),
                f"Decision round {n} — sent to Codex",
                f"Decision round {n} — Codex ranking",
            )
            self.merge(n, numbers, ranking, PLAN_PART_4)
            for number in numbers:
                self.st["decisions"][str(number)]["codex"] = True
            self.save()
            title = (
                f"Codex's ranking of {listing(numbers)} (decision round {n})"
            )
            replies.append((title, ranking, key))
            merged += numbers
        if op.get("round"):
            n, numbers = op["round"], op["ranked"]
            body = join(
                "\n\n".join(self.latest_sections(numbers)),
                f"My question:\n\n{words}",
            )
            key = f"round-{n}:codex"
            reply = self.codex_step(
                key,
                cli.compose(
                    ["shared/decision-followup-reviewer"], self.state, body
                ),
                f"Decision round {n} — sent to Codex",
                f"Decision round {n} — Codex response",
            )
            self.merge(n, numbers, reply, LATEST_PART_4)
            replies.append(
                (f"Codex's answer to question {op['n']}", reply, key)
            )
            merged += numbers
        report = self.write_report()
        for title, reply, key in replies:
            self.show(title, reply, self.reply_file(key))
        rounds = " and ".join(
            str(op[field])
            for field in ("ranking_round", "round")
            if op.get(field)
        )
        plural = "rounds" if " and " in rounds else "round"
        print(
            f"Claude merged Codex's reply into {listing(sorted(merged))} "
            f"(decision {plural} {rounds}). Report: {report}"
        )

    def describe(self, op: dict, numbers: list[int]) -> None:
        """Under --no-decision-rounds, Codex has never seen Part 3, so
        Claude copies the descriptions of these decisions for it first."""
        missing = [
            n
            for n in numbers
            if "description" not in self.st["decisions"][str(n)]
        ]
        if not missing:
            return
        key = f"ask-codex-{op['n']}:decision-copy"
        text = self.claude_step(
            key,
            f"decision copy q{op['n']}",
            DECISION_COPY,
            self.template(
                "runner/decision-copy-chosen", numbers=and_list(missing)
            ),
            f"messages/06-decisions-q{op['n']}.md",
        )
        _, source = self.checked(
            key,
            text,
            self.titled(missing),
            f"fix-up decision copy q{op['n']}",
            DECISION_COPY,
        )
        for n in missing:
            self.st["decisions"][str(n)]["description"] = source
        self.save()

    def rank(self, op: dict) -> None:
        """New decisions to rank: Claude describes them in Part 3's
        neutral style under the next numbers, then a blind ranking round
        runs on them, with the developer's words under "My question:"."""
        n, start = op["round"], op["next"]
        words = self.path(op["words"]).read_text(encoding="utf-8")
        key = f"rank-{op['n']}:describe"
        label = f"Decision round {n} — new decisions described"
        text = self.claude_step(
            key,
            f"new decision r{n}",
            label,
            join(self.template("runner/new-decision", next=str(start)), words),
            f"messages/06-decisions-r{n}.md",
        )
        if not op.get("registered"):
            expected = decisions.renumbered(text, start) or [
                (start, "<title>")
            ]
            text, source = self.checked(
                key, text, expected, f"fix-up new decision r{n}", label
            )
            self.register(text, source)
            op["registered"], op["source"] = True, source
            self.save()
        text = self.path(op["source"]).read_text(encoding="utf-8")
        numbers = [k for k, _ in decisions.headings(text)]
        self.ranking_round(n, numbers, words)
        report = self.write_report()
        print(
            f"Claude and Codex ranked new {listing(numbers)} (decision round "
            f"{n}). Report: {report}"
        )

    def finalize(self, op: dict) -> None:
        """A numbered Finalize turn. Its plan replaces decided-plan.md, and
        every version stays in the transcript, unless it has more than 15
        groups: then it's held as a draft for the developer to choose
        what to do, and their answer goes to the next Finalize."""
        words = self.path(op["words"]).read_text(encoding="utf-8")
        held = self.st["held"]
        if held:
            prompt = join(
                self.template(
                    "runner/finalize-after-hold", count=str(held["groups"])
                ),
                words,
            )
        else:
            prompt = join(self.template("runner/finalize"), words)
        out = f"messages/finalize-{op['n']}.md"
        plan = self.claude_step(
            f"finalize-{op['n']}:claude",
            f"finalize {op['n']}",
            self.label(op),
            prompt,
            out,
        )
        if plan.strip() == "PROCEED":
            if not held:
                del self.st["steps"][f"finalize-{op['n']}:claude"]
                self.save()
                raise cli.CheckFailed(
                    "Claude replied PROCEED, but no plan is held. Finalize "
                    "again to ask Claude for the plan."
                )
            plan = self.path(held["file"]).read_text(encoding="utf-8")
        else:
            count = groups(plan)
            if count > GROUP_LIMIT:
                self.hold(op, out, count)
                return
        self.record_plan(plan)

    def hold(self, op: dict, draft: str, count: int) -> None:
        self.st["held"] = {"file": draft, "groups": count}
        self.save()
        transcript.append_once(
            self.review,
            f"{self.label(op)} — held",
            f"The plan has {count} groups, more than {GROUP_LIMIT}, so it "
            f"wasn't recorded. The draft is in `{draft}`.",
            transcript.now(),
        )
        path = Path.cwd() / self.path(draft)
        print(
            f"This plan has {count} groups, more than {GROUP_LIMIT}, which "
            "usually means the change is too big for one review. Choose "
            f"one: 1. Proceed with all {count} groups. 2. Defer "
            "lower-priority groups. 3. Split off a follow-up change.\n"
            f"The draft isn't recorded yet. It's in: {path}"
        )

    def record_plan(self, plan: str) -> None:
        path = self.review / "decided-plan.md"
        write_atomic(path, plan)
        transcript.append_once(
            self.review, DECIDED_PLAN, plan, transcript.now()
        )
        self.st["held"] = None
        self.save()
        print(
            f"Decided plan: {Path.cwd() / path} ({groups(plan)} groups). "
            f"To implement it, run /xreview:code-implement {self.name}."
        )

    def latest_sections(self, numbers: list[int]) -> list[str]:
        sections = []
        for n in numbers:
            entry = self.st["decisions"][str(n)]["latest"]
            text = self.path(entry["file"]).read_text(encoding="utf-8")
            sections.append(decisions.sections(text)[n])
        return sections

    def reply_file(self, key: str) -> Path:
        return Path.cwd() / self.path(self.st["steps"][key]["out"])

    def show(self, title: str, answer: str, path: Path) -> None:
        """Print a short answer word for word, or say where a long one is."""
        if len(answer) <= ANSWER_LIMIT:
            print(f"{title}:\n\n{answer.strip()}\n")
            print(f"Saved in: {path}")
        else:
            print(
                f"{title} is {len(answer):,} characters, so it's in a file: "
                f"{path}"
            )

    # --- stops ---------------------------------------------------------------

    def stop(self) -> None:
        self.write_report()
        self.st["stopped"] = True
        self.st["op"] = None
        self.save()
        self.report_stop()

    def write_report(self) -> Path:
        plan_file = self.st.get("plan", "messages/05-plan.md")
        plan = self.path(plan_file).read_text(encoding="utf-8")
        latest = []
        for n in sorted(self.numbers()):
            entry = self.st["decisions"][str(n)].get("latest")
            # Options that are still the plan's own Part 4 aren't repeated.
            if entry and not entry.get("in_plan"):
                text = self.path(entry["file"]).read_text(encoding="utf-8")
                latest.append((entry["label"], decisions.sections(text)[n]))
        report = self.review / "report.md"
        write_atomic(report, decisions.report(self.name, plan, latest))
        return Path.cwd() / report

    def report_stop(self) -> None:
        report = Path.cwd() / self.review / "report.md"
        count = len(self.st["decisions"])
        if count:
            plural = "decision" if count == 1 else "decisions"
            what = f"with {count} open {plural}"
        else:
            what = "with no open decisions"
        print(f"The review is at a stop, {what}. Report: {report}")


@contextlib.contextmanager
def awake(runner: Runner):
    """Hold sleep off while the command runs, then for a while at the stop
    it leaves the review at, however it ends."""
    runner.st = runner.load()
    wake.while_running()
    try:
        yield
    finally:
        runner.st["wake"] = wake.at_stop(runner.st.get("wake"))
        runner.save()


def run(
    name: str,
    retry: bool,
    alone: bool = False,
    model: str | None = None,
    effort: str | None = None,
) -> None:
    runner = Runner(name, retry, alone, model, effort)
    with Lock(runner.review), awake(runner):
        runner.run()


def at_stop(
    kind: str,
    name: str,
    given: str,
    retry: bool,
    numbers: list[int] | None = None,
) -> None:
    runner = Runner(name, retry)
    with Lock(runner.review), awake(runner):
        runner.at_stop(kind, given, numbers)
