---
description: Start or pick up an xreview review from any conversation, then carry your words to both reviewers at each stop.
argument-hint: <review-name>
disable-model-invocation: true
allowed-tools: Bash(xreview *)
---

# Pick up a cross-model code review

Review name: $0

This picks up a review that `/xreview:code-review` prepared, from any conversation. If no review name was passed to this command, ask the user for it and stop.

First run `xreview prompt --template shared/protocol` and follow it. Then run `xreview prompt --template shared/messenger` and follow it for the rest of this review, starting with `xreview run $0`. That command starts the review, picks it up where it stopped, or reports the stop it's at.
