---
description: Session A. Prepare a Codex review of the commits after a base commit, plus a handoff for a fresh Claude session.
argument-hint: <base-commit>
disable-model-invocation: true
allowed-tools: Bash(xreview *)
---

# Prepare a cross-model code review (Session A)

Base commit: $0

You are the session that wrote the code under review. The review covers the commits after the base commit, through HEAD. If no base commit was passed to this command, ask the user for one and stop. There is no default.

First run `xreview prompt --template shared/protocol` and follow it. Change nothing outside `.reviews/<name>/`.

1. Run `xreview prepare $0`. If it fails, show the error and stop. Otherwise show the user its summary, without asking for confirmation. The summary's heading gives the review name, `<name>`.
2. Run `xreview prompt <name> --template code/reviewer-prompt-guide` and follow it. Write the prompt to `.reviews/<name>/reviewer-prompt.md`. The file contains only the prompt, with no preamble.
3. Run `xreview prompt <name> --template code/claude-handoff-guide` and follow it. Write the handoff to `.reviews/<name>/claude-handoff.md`.
4. Run `xreview check <name>`. If the prompt check fails, rewrite `reviewer-prompt.md` so it contains the Simplification block word for word, and run the check again. If it still fails, or another check fails, show the failure and stop.
5. Tell the user: "In a NEW Claude Code session in this repo, run: `/xreview:code-review <name>`" Then tell them: "For Session B to run unattended, run it in auto mode, or in Edit automatically mode ("accept edits on" in the terminal) with `"Bash(xreview *)"` added to `permissions.allow` in `~/.claude/settings.json`. Otherwise, it stops at each permission prompt until you approve it."
