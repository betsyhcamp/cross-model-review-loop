# xreview

[![CI](https://github.com/betsyhcamp/cross-model-review-loop/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/betsyhcamp/cross-model-review-loop/actions/workflows/ci.yml) ![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg) [![License: MIT](https://img.shields.io/badge/license-MIT-cyan.svg)](LICENSE)

`xreview` is a Claude Code plugin that runs a code review debate between Claude and Codex, with no copying and pasting between them. You start it from the Claude session that wrote the code, and you stay in that session throughout: at each stop you talk to both reviewers through it, at your desk or from your phone. You decide on the open items that couldn't be resolved during the review. For each open decision, Claude and Codex come up with solutions and rank them independently, and Claude merges them into one ranked set, saying when it's a coin flip or there's genuinely only one good solution. You then finalize a plan, and Claude implements it as a small series of commits.

Every message sent to either reviewer, and every answer `xreview` accepts, is kept word for word in a Markdown transcript inside your project.

## How a review runs

In the session that wrote the code, **Session A**, commit your work and run `/xreview:code-review <base>`. Session A writes the prompt for Codex and a handoff for Claude, then starts the review in the background. From there, `xreview` runs each step in order:

1. Codex reviews the code.
2. **Session B**, a separate Claude session that `xreview` starts and drives, reads the handoff and rebuts Codex's review.
3. The same Codex session responds.
4. Session B writes a plan.
5. For the plan's open decisions, Codex and Session B rank solutions without seeing each other's ranking, and Session B merges the two.

Then the review stops for you, usually 20 to 30 minutes after it started, and Session A gives you the path to the report. You can ask either reviewer questions, ask for more rounds and give your decisions, all in Session A. When you finalize, Session B writes `decided-plan.md`, and `/xreview:code-implement <name>` implements it.

Session A only carries messages. It never reads the debate or looks into your questions, so its context stays small for implementing the fixes.

## Requirements

- **Claude Code, logged in**, in the terminal or the VS Code extension.
- **The `claude` command on your PATH.** `xreview` runs Session B with it. The VS Code extension doesn't put `claude` on your PATH by itself, so install the Claude Code CLI too. If it's missing but you're in a Claude Code session, `xreview` uses that session's own binary and tells you so.
- **Claude Code's sandbox and Auto mode.** Session B runs only inside the sandbox, in Auto mode. `xreview` checks both before Codex's review starts, and stops with instructions if either is missing. On Linux, the sandbox needs `bubblewrap` and `socat`.
- **Codex CLI, logged in.** Codex's model and reasoning effort come from your own `~/.codex/config.toml`; `xreview` doesn't override them.
- **Python 3.10 or later**, with nothing to install: the runner uses only the standard library. The launcher, `bin/xreview`, runs the first suitable Python it finds.
- **git**. Reviews cover committed changes only.
- **macOS or Linux including WSL**. Windows isn't supported. Only macOS has been tested end to end.

## Install

In a Claude Code session:

```text
/plugin marketplace add betsyhcamp/cross-model-review-loop
/plugin install xreview@cross-model-review-loop
```

**Updates.** The plugin has no version number, so every commit is an update. Third-party marketplaces don't update automatically: run `claude plugin update xreview@cross-model-review-loop`, or turn on auto-update under Marketplaces in `/plugin`.

For other ways to install, see [docs/install.md](docs/install.md) including from your shell or another git host, on a company or regulated computer, or with symlinks instead of the plugin system.

## Usage

### Start a review

In the session that wrote the code, commit your work. Then find the **base commit**, which is the last commit before the work you want reviewed. The review covers every commit after the base commit, through HEAD.

```text
$ git log --oneline
f9e8d7c Add retry tests              ← to review
4b5c6d7 Add retry to the client      ← to review
a1b2c3d Merge pull request #12       ← base commit
```

Run `code-review` with the base commit's hash:

```text
/xreview:code-review a1b2c3d
```

- **Only committed changes are reviewed.** Uncommitted edits to tracked files stop it, and it asks you to commit or stash them. Untracked files, such as notes or a spec, are listed as "not included" and never block it.
- Session A writes `reviewer-prompt.md` and `claude-handoff.md` in `.reviews/<name>/`, checks the prompt, starts the review in the background, and tells you when it stops. See [docs/reference.md](docs/reference.md) for a file list.
- To have the plan end with Session B's own ranking of the open decisions, with no Codex round, add `--no-decision-rounds`. You can still ask for Codex's take later.
- To start or pick up a review from another conversation, run `/xreview:code-resume <name>`.

### At a stop

When the review stops, Session A gives you a few lines and the path to the report: the plan, then the latest options for each open decision. Write to Session A in plain words. For example:

- **"Where are we?"** shows the review's status on one screen.
- **"Ask Claude: is the retry change safe for 429s?"** goes to Session B, word for word.
- **"Ask Codex about 2 and 4: what's the cheapest way to settle these?"** sends Codex the latest options for decisions 2 and 4, followed by your words. You see Codex's own answer, and Session B merges it into those two decisions. Without numbers, every decision goes. With no open decisions, your words go to Codex alone.
- **"New decision: how should the dead-letter field be named?"** has Session B describe it neutrally, under the next decision number, and both reviewers rank it without seeing each other's ranking.
- **"Finalize: option 2 on decision 1, defaults for the rest."** has Session B write `decided-plan.md`.

Your words are recorded in the transcript, exactly as you wrote them, before either reviewer sees them. Short answers are shown in full in Session A; long ones go to a file.

### Decision rounds and the Decided Plan

Part 3 of the plan describes each open decision, up to seven, without suggesting solutions, so Codex comes up with its own. Each decision gets a permanent number, and a new decision continues the count. In the first decision round, Codex ranks solutions for every open decision first, then Session B ranks them with every copy of Codex's ranking blocked from it. Session B then merges the two into **Part 4, Options for the open decisions**: every option from both, plus any new one, ranked best to worst with pros and cons, a recommendation, and a call when it's a coin flip or there's only one good option. Each option is tagged by where it came from (both, Claude, Codex or new), and each decision says whether the two top picks matched. The report always shows each decision's latest options, labeled with the round they came from.

With `--no-decision-rounds`, Part 4 is Session B's ranking alone, at the end of the plan. Asking for Codex's take on a decision then has Codex rank it, with Session B's Part 4 as Session B's side of the merge. In that mode Session B's ranking is already on disk when Codex ranks, so Codex is only trusted not to read it.

When you give your decisions, or say to finalize, Session B writes `.reviews/<name>/decided-plan.md`: your decisions, the groups to implement in order, and what's deferred or dropped. Unanswered decisions take the latest recommendation, the top option in the latest Part 4. If that option needs something only you know, Part 4 says "Needs from you", and an unanswered decision is deferred with no change made. If the plan has **more than 15 groups**, it isn't recorded yet: you get three choices, to proceed, defer lower-priority groups, or split off a follow-up change, and your answer goes to the next Finalize. You can finalize again until implementation starts. Each Finalize replaces `decided-plan.md`, and every version stays in the transcript.

### Implement

Run `/xreview:code-implement <name>` in Session A, or in a fresh session if Session A's context is long. It works only from `decided-plan.md`:

- one commit per group, in order, staged by explicit path, with the group's title as the subject line, after any prefix your repository's convention uses, so each commit and its group share a label;
- then your project's test command, with fixes for any failures in extra commits;
- then a **Final Changes** section in the transcript: each commit, the test result, what was deferred or dropped, and any departures from the plan.

It never pushes. `git log --oneline <reviewed head>..HEAD` lists exactly the review's commits.

## Running unattended and from your phone

Session A runs an `xreview` command for every step you take, and each review step runs in the background. For that to work without stopping at each command, and from your phone:

1. **An allow rule** in `~/.claude/settings.json`. A skill's own permission ends at your next message, so this is what lets Session A run `xreview` at each stop:

   ```json
   {
     "permissions": {
       "allow": ["Bash(xreview *)"]
     }
   }
   ```

   The rule approves only `xreview` commands, in every session. It lives in your user settings, not in your project.
2. **Auto mode or Edit automatically mode** for Session A, so it can write your words to the review's inbox without asking.
3. **Remote Control**, to use Session A from your phone.
4. **"Push when Claude decides"** turned on in `/config`, for a notification at each stop.

With a phone connected, Session A sends each report, long answer or Decided Plan to your phone as a Markdown file, with a one-line push notification. Without one, it gives you the path; in VS Code the path opens as rendered Markdown. If a file won't open on your phone, say "show it here", and Session A pastes it in full.

`code-implement` in Edit automatically mode still asks before running your test command, because no rule can cover every project's command.

## While a review runs

- **Progress.** Every 30 seconds the runner prints a line such as `xreview: Codex still working — 2m 30s, 4 commands run`. Ask Session A where things stand at any time.
- **Don't change tracked files or commit until implementation starts.** The review covers the code as committed. A change to a tracked file, or a commit, ends the review's stops, and one made while a reviewer is working also rejects that reviewer's answer. **Untracked files are fine.** You can ask Session A for a research spike, for example, during a review or at a stop. It works in its own scratchpad unless you name a place, which keeps the spike out of the repo the reviewers read.
- **Interruptions.** Each reviewer call has a 40-minute limit. If a call is interrupted, `xreview` never sends it again by itself, because the reviewer may already have answered. When an interrupted reviewer finished anyway, its answer is used. Otherwise the step is reported as uncertain, and saying "retry" sends it again. At a stop, if you ask for something else instead, the uncertain question is dropped, with nothing resent.
- **If Session A closes,** the review stops. Run `/xreview:code-resume <name>` in any session to pick it up from the last finished step.
- **Keeping your Mac awake.** While any `xreview` command runs, the Mac is kept from sleeping. At a stop it's kept awake for 1.5 hours after the last command, or until Session A closes. A Mac with its lid closed on battery still sleeps.
- **Opening a reviewer at your desk.** At a stop, the status lists commands that open Session B and the Codex session directly, with the same limits as during the review. Chats there aren't recorded in the review. Don't open one while a step is running: the reviewer would get two messages at once.

## What the reviewers can do

Both reviewers can read all the code and run things to check their claims. Neither can change your repository.

- **Session B** runs every turn inside Claude Code's sandbox, in Auto mode, with only the read, shell and web tools. Its shell commands can write only to temporary folders and have no network access; it looks things up with the web tools. It can't read other AI sessions' records, such as `~/.claude` and `~/.codex`, or your other reviews, unless the handoff or your messages name a file by its full path. It doesn't load your Claude Code settings files, which could widen its sandbox. Instead, its first message holds the repository's own `CLAUDE.md` and `AGENTS.md`, and the handoff carries your personal rules. Managed settings still apply: `xreview` stops if a managed settings file widens the sandbox, but can't see exclusions set by MDM profiles or server-managed settings. It runs in Claude Code's default output style, whatever yours is, so no Insight blocks reach Codex. If the sandbox can't start or Auto mode isn't available, `xreview` stops rather than run it unprotected.
- **Codex** runs with its sandbox set to read-only and approvals set to never. Its prompt asks it to stay out of the same records, but nothing enforces that.
- **Blind ranking.** Codex ranks first, so Session B's ranking doesn't exist yet. During Session B's ranking turn, the review's run folders and transcript are blocked from it, and Codex's ranking isn't in its message. The block lasts that one turn.

`xreview` has been checked against `codex-cli 0.160.1` and Claude Code 2.1.319. A later version of either could change how a resumed session behaves.

## Files in your project generated by `xreview`

Each review keeps its files in `.reviews/<name>/`, which is added to `.git/info/exclude`, so nothing is committed. [docs/reference.md](docs/reference.md) lists each file and how `<name>` is chosen.

## Contributing
To work on `xreview` itself, see [CONTRIBUTING.md](CONTRIBUTING.md). Planned work is tracked as [enhancement issues](https://github.com/betsyhcamp/cross-model-review-loop/issues?q=is%3Aissue+label%3Aenhancement).

## License

MIT. See [LICENSE](LICENSE). Parts of the Codex adapter and its tests are adapted from Chase AI's [claudex-loop](https://github.com/chaseai-yt/claudex-loop), also MIT; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
