---
description: Session A. Run a Claude and Codex review of the commits after a base commit, then carry your words to both reviewers at each stop.
argument-hint: <base-commit> [--no-decision-rounds] [--claude-model <model>] [--claude-effort <level>]
disable-model-invocation: true
allowed-tools: Bash(xreview *)
---

# Cross-model code review (Session A)

Base commit: $0
Arguments: $ARGUMENTS

You are the session that wrote the code under review. The review covers the commits after the base commit, through HEAD. If no base commit was passed to this command, ask the user for one and stop. There is no default. The options, after the base commit, are `--no-decision-rounds`, `--claude-model <model>` and `--claude-effort <level>`. If the arguments hold anything else, tell the user that, and stop.

First run `xreview prompt --template shared/protocol` and follow it.

1. Run `xreview prepare $0`. If it fails, show the error and stop. Otherwise show the user its summary, without asking for confirmation. The summary's heading gives the review name, `<name>`.
2. Run `xreview prompt <name> --template code/reviewer-prompt-guide` and follow it. Write the prompt to `.reviews/<name>/reviewer-prompt.md`. The file contains only the prompt, with no preamble.
3. Run `xreview prompt <name> --template code/claude-handoff-guide` and follow it. Write the handoff to `.reviews/<name>/claude-handoff.md`.
4. Run `xreview check <name>`. If the prompt check fails, rewrite `reviewer-prompt.md` so it contains the Simplification block word for word, and run the check again. If it still fails, or another check fails, show the failure and stop.
5. Run `xreview prompt --template shared/messenger` and follow it for the rest of this review, starting the review with `xreview run <name>`, plus any of those options that were passed.
