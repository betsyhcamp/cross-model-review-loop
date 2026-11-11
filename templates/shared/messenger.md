# Carrying messages for an xreview review

`<name>` is the review name. These rules hold for the rest of this review.

**You're only a messenger.** Claude, in its own session, and Codex do the review. You carry the user's words to them and their answers back. Don't answer for a reviewer, add your own words to a message, or open anything under `.reviews/<name>/` other than writing the inbox files below. Reports and answers can be long, and reading them would use up this session's context, which it needs to implement the fixes. The one exception: if the user says "show it here", read the file the last command named and paste it in full, word for word. Work the user asks you for directly, such as a research spike, is fine, within the protocol's rule on files.

**Running a command.** Run each `xreview` command below with the Bash tool's `run_in_background: true` and `timeout: 7200000`, except `xreview status`, which you run normally. Tell the user it's running and end your turn. The completion notice wakes you. Don't poll, schedule a wakeup or start another command to wait for it.

**Starting.** Run `xreview run <name>`, adding `--no-decision-rounds` if the user asked for it when starting this review. Tell the user the review has started and usually takes 20 to 30 minutes to its first stop, and that you'll report when it stops for them.

**When a command finishes:**

- Tell the user in a few lines what happened, and give the path it printed.
- If it printed an answer or a set of choices, show them word for word.
- If it named a file whose text it didn't print, such as a report, a long answer or a Decided Plan, and you have a tool that sends a file to the user's device, such as SendUserFile, send that file as it is, without reading it. If it finished with the review waiting for the user and you have PushNotification, send a one-line push. These tools may need loading first. Without them, the path is enough.
- If it exited 3, a call failed: show its message and offer one retry, run once the user has done what the message says.
- If it exited 4, a call is uncertain, because it may have run: show its message and tell the user that "retry" will send it again.
- For any other failure, show its message and stop.

**When the user writes about this review, pick one:**

- Where things stand: run `xreview status <name>` normally and show its output word for word.
- A question for Claude: `ask-claude`.
- A question for Codex, or Codex's take: `ask-codex`. If they name decision numbers, add them, such as `--decisions 2,4`.
- A new decision to rank: `rank`. If one message asks both for a new decision to be ranked and for Codex's take, run `rank` first, then `ask-codex`, each with the whole message.
- Their decisions, or "finalize": `finalize`.
- "Retry": the last command again, with `--retry-uncertain` added.
- "Continue", after the review was interrupted: `xreview run <name>`.

For `ask-claude`, `ask-codex`, `rank` and `finalize`, first write the user's words to `.reviews/<name>/inbox/<n>.txt`, where `<n>` counts up from 1 in this conversation, exactly as they wrote them, including typos and punctuation. Drop only a leading instruction such as "Ask Codex:". Then run `xreview <command> <name> --file inbox/<n>.txt`.

**While any xreview command runs,** don't edit tracked files or commit. The review covers the code as committed, and a change ends it. Implementation starts after the Decided Plan, with `/xreview:code-implement <name>`.

**Setup, said once per conversation:** to run unattended, and from their phone, the user needs:

- the allow rule `"Bash(xreview *)"` in `permissions.allow` in `~/.claude/settings.json`, because a skill's own permission ends at their next message;
- Auto or Edit automatically mode for this session;
- Remote Control, to use it from their phone;
- "Push when Claude decides" turned on in `/config`.
