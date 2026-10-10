# xreview

`xreview` is a Claude Code plugin that runs a code review debate between Claude and Codex, with no copying and pasting between them. You decide on the open items that couldn't be resolved during the review. For any open decision items, you can optionally have both models rank possible solutions, explain if it's a coin flip, or say there's genuinely only one good solution. Claude proposes a plan and implements the plan you've approved as a small series of commits.

Every message sent to Codex, and every answer `xreview` accepts, is kept word for word in a Markdown transcript inside your project.

## How a review runs

**I. Session A**, the Claude session that wrote the code, runs `/xreview:code-prepare <base>` after you commit. It writes the prompt for Codex and a handoff for the next session.

**II. Session B**, a fresh Claude session where five steps are done:

1. Run `/xreview:code-review <name>`.
2. Codex reviews the code while Claude reads the handoff.
3. Claude rebuts the review from Codex.
4. The same Codex session that gave the initial review responds.
5. Claude writes a final plan and stops for you.

**III. Decision rounds**, only when you ask: Claude and Codex rank the open decisions independently, Claude compares the two, and stops. You can ask for more rounds.

**IV. Finalize decisions**: when you give your decisions, Claude writes `decided-plan.md`.

**V. Implement the plan**: `/xreview:code-implement <name>`, in Session B or a fresh session, makes one commit per group in the plan, runs your tests, and records what changed.

## Requirements

- **Claude Code, logged in**, in the terminal or the VS Code extension.
- **Codex CLI, logged in.** Codex's model and reasoning effort come from your own `~/.codex/config.toml`; `xreview` doesn't override them.
- **Python 3.10 or later**, with nothing to install: the runner uses only the standard library. The launcher, `bin/xreview`, runs the first suitable Python it finds.
- **git**. Reviews cover committed changes only.
- **macOS or Linux including WSL**. Windows isn't supported.

## Install

In a Claude Code session:

```text
/plugin marketplace add betsyhcamp/cross-model-review-loop
/plugin install xreview@cross-model-review-loop
```

**Updates.** The plugin has no version number, so every commit is an update. Third-party marketplaces don't update automatically: run `claude plugin update xreview@cross-model-review-loop`, or turn on auto-update under Marketplaces in `/plugin`.

For other ways to install, see [docs/install.md](docs/install.md) including from your shell or another git host, on a company or regulated computer, or with symlinks instead of the plugin system.

## Usage

### Session A: prepare the review

In the session that wrote the code, commit your work. Then find the **base commit** which is the last commit before the work you want reviewed. The review covers every commit after the base commit, through HEAD.

```text
$ git log --oneline
f9e8d7c Add retry tests              ← to review
4b5c6d7 Add retry to the client      ← to review
a1b2c3d Merge pull request #12       ← base commit
```

Run `code-prepare` with the base commit's hash:

```text
/xreview:code-prepare a1b2c3d
```

- **Only committed changes are reviewed.** Uncommitted edits to tracked files stop it, and it asks you to commit or stash them. Untracked files, such as notes or a spec, are listed as "not included" and never block it.
- Claude writes `reviewer-prompt.md` and `claude-handoff.md` in `.reviews/<name>/`, checks the prompt, and prints the command for Session B. See [docs/reference.md](docs/reference.md) for a file list.

### Session B: the debate

Open a **new** Claude Code session in the same repository, and run the command Session A printed:

```text
/xreview:code-review feat-my-branch
```

Session B stops at the Step 5 plan and shows it, with the path to the transcript.

### Decision rounds and the Decided Plan

At the Step 5 stop, you can talk through the open decisions with one model or have a cross model interchange round on the open decisions. For example:

- **"Get Codex's take on 2 and 4"** starts a ranking round. Codex ranks those decisions while Claude ranks them on its own, without looking at Codex's answer. Claude then compares the two rankings and stops. Add "My question: …" to send Codex a question word for word.
- **"Another round on those"** starts a follow-up round. Claude's comparison goes back to Codex, and Claude compares again.
- **A decision no round has ranked yet** gets its own ranking round.

When you give your decisions, or say to finalize, Claude writes `.reviews/<name>/decided-plan.md`: your decisions, the groups to implement in order, and what's deferred or dropped. Unanswered decisions take their stated default. If the plan has **more than 15 groups**, Claude warns you before recording it and offers three choices: proceed, defer lower-priority groups, or split off a follow-up change.

### Implement

Run `/xreview:code-implement <name>` in Session B, or in a fresh session if Session B's context is long. It works only from `decided-plan.md`:

- one commit per group, in order, staged by explicit path, with a subject line that follows your repository's convention and ends in `(xreview <name>, group <n>)`;
- then your project's test command, with fixes for any failures in extra commits;
- then a **Final Changes** section in the transcript: each commit, the test result, what was deferred or dropped, and any departures from the plan.

It never pushes. `git log --oneline <reviewed head>..HEAD` lists exactly the review's commits.

## Running Session B unattended

Session B stops at its next runner call and waits for your approval.

There are two ways to let it run on its own:

1. **Auto mode.** No setup.
2. **Edit automatically mode plus an allow rule.** Add this to `~/.claude/settings.json`:

   ```json
   {
     "permissions": {
       "allow": ["Bash(xreview *)"]
     }
   }
   ```

   The rule approves only `xreview` commands, in every session. It lives in your user settings, not in your project. It doesn't cover Claude's file writes, which is why Session B also needs Edit automatically mode. In Manual mode it still stops at each write.

`code-implement` in Edit automatically mode still asks before running your test command, because no rule can cover every project's command.

## While Codex is working

- **Progress.** Every 30 seconds the runner prints a line such as `xreview: Codex still working — 2m 30s, 4 commands run`.
- **To stop a call,** ask Claude to stop the background task. The runner kills Codex and records the call as `(FAILED)` in the transcript. The runner also stops a call itself after 40 minutes.
- **Don't edit tracked files or commit in the same git worktree while a call runs.** After every call, the runner checks that HEAD and your tracked files haven't changed, and rejects Codex's response if they have. Its message then tells you to restore the tree, which may mean throwing away your own edit.
- **When a call fails,** the transcript gets a `(FAILED)` section, and Claude shows the runner's message, which says what to fix. Claude offers you one retry once you've fixed it.

## What "read-only" covers

Codex runs with its sandbox set to read-only and approvals set to never. That covers Codex's own shell commands and file edits. `xreview` has been checked against `codex-cli 0.160.1`. A later Codex version could change how a resumed session behaves.

## Files in your project generated by `xreview`

Each review keeps its files in `.reviews/<name>/`, which is added to `.git/info/exclude`, so nothing is committed. [docs/reference.md](docs/reference.md) lists each file and how `<name>` is chosen.

## Contributing
To work on `xreview` itself, see [CONTRIBUTING.md](CONTRIBUTING.md). Planned work is tracked as [enhancement issues](https://github.com/betsyhcamp/cross-model-review-loop/issues?q=is%3Aissue+label%3Aenhancement).

## License

MIT. See [LICENSE](LICENSE). Parts of the Codex adapter and its tests are adapted from Chase AI's [claudex-loop](https://github.com/chaseai-yt/claudex-loop), also MIT; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
