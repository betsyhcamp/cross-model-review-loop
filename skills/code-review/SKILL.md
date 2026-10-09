---
description: Session B. Run the Codex review debate for a prepared review, through Claude's rebuttal and final plan.
argument-hint: <review-name>
disable-model-invocation: true
allowed-tools: Bash(xreview *)
---

# Cross-model code review (Session B)

Review name: $0

This is Session B, a fresh session that runs the review debate with Codex. If no review name was passed to this command, ask the user for it and stop.

First run `xreview prompt --template shared/protocol` and follow it.

1. Start Codex's review (Step 2): run `xreview start $0` in the background. If it refuses or fails, show the message and stop. If HEAD has moved since prepare, tell the user to re-run code-prepare.
2. While Codex works, read `.reviews/$0/claude-handoff.md` and everything it lists. Don't change anything.
3. When the call finishes, run `xreview prompt $0 --template code/step3-claude-rebuttal` and follow it. The feedback it refers to is Codex's review, in the `response.md` file whose path the runner printed. Write your rebuttal to `.reviews/$0/messages/03-rebuttal.md`.
4. Send the rebuttal (Step 4): run `xreview continue $0 --template code/step4-reviewer-response --body messages/03-rebuttal.md --sent-heading "Step 3 — Claude rebuttal — sent to Codex" --response-heading "Step 4 — Codex response"` in the background, and wait for it to finish.
5. Run `xreview prompt $0 --template code/step5-claude-plan` and follow it. The reviewer's response is Codex's Step 4 response, in the `response.md` file whose path the runner printed. Write the plan to `.reviews/$0/messages/05-plan.md`, then run `xreview append $0 --heading "Step 5 — Claude final plan" --file messages/05-plan.md`. Show the plan to the user with the transcript path, `.reviews/$0/transcript.md`, and stop.
