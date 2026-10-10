---
description: Session B or a fresh session. Implement a review's Decided Plan as one commit per group, then run the tests and record Final Changes.
argument-hint: <review-name>
disable-model-invocation: true
allowed-tools: Bash(xreview *) Bash(git add *) Bash(git commit *)
---

# Implement the Decided Plan

Review name: $0

This session implements the Decided Plan of a finished review. It may be Session B or a fresh session with no history. If no review name was passed to this command, ask the user for it and stop.

First run `xreview prompt --template shared/protocol` and follow it. Never push.

1. Run `xreview check $0`, then read `.reviews/$0/decided-plan.md`. If the file doesn't exist, tell the user and stop.
2. Implement the groups under Implement now, in order. Work from `decided-plan.md`, and read `.reviews/$0/transcript.md` only for background. For each group:
   1. Make the changes so the group's "Done when" holds, and add or update tests.
   2. Stage only that group's files, by explicit path. Never run `git add .` or `git add -A`, which would also stage untracked files, such as a spec.
   3. Commit. For the subject line, read the current branch's recent subjects with `git log --format=%s -20` and follow their convention, including any prefix such as a ticket number. If more than half of them use one of `feat:`, `fix:`, `refac:`, `docs:`, `test:` or `chore:`, use whichever of these six fits the group, in the same position. End the subject with `(xreview $0, group <n>)`, where `<n>` is the group's number in the plan.
3. After the last group, find the project's test command (in the README, Makefile, `pyproject.toml`, `package.json`, and so on) and run it. If tests fail, fix them in more commits, with subject lines in the same form, ending in `(xreview $0, test fix)`. If you can't fix them, still do step 4: mark Final Changes **Tests failing**, say what fails and what you tried, and stop after appending it.
4. Write Final Changes to `.reviews/$0/messages/final-changes.md`, then run `xreview append $0 --heading "Final Changes" --file messages/final-changes.md`. List:
   - each group's commit SHA and what changed, and any test-fix commits;
   - the test command and its result;
   - the plan's Deferred and Dropped items, copied from it;
   - any departure from the plan, with its reason;
   - any edits to untracked files, such as a spec, which aren't committed.
5. Tell the user that `git log --oneline <reviewed head>..HEAD` lists exactly this review's commits. The reviewed head is `reviewed_head` in `.reviews/$0/state.json`.
