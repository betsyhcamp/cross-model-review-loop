# xreview

xreview is a Claude Code plugin that runs a code-review debate between Claude and Codex, with no copying and pasting between them. Codex reviews your committed changes, a fresh Claude session rebuts the review, the same Codex session responds, and Claude writes a plan. You then decide the open items, optionally after both models rank them independently, and Claude implements the decided plan as a small series of commits.

Every message sent to Codex, and every answer xreview accepts, is kept word for word in a Markdown transcript inside your project, which is never committed.

## How a review runs

1. **Session A**, the Claude session that wrote the code, runs `/xreview:code-prepare <base>` after you commit. It writes the prompt for Codex and a handoff for the next session.
2. **Session B**, a fresh Claude session, runs `/xreview:code-review <name>`:
   - Codex reviews the code (Step 2) while Claude reads the handoff.
   - Claude rebuts the review (Step 3), and the same Codex session responds (Step 4).
   - Claude writes a final plan (Step 5) and stops for you.
3. **Decision rounds**, only when you ask: Claude and Codex rank the open decisions independently, Claude compares the two, and stops. You can ask for more rounds.
4. **Finalize**: when you give your decisions, Claude writes `decided-plan.md`.
5. **Implement**: `/xreview:code-implement <name>`, in Session B or a fresh session, makes one commit per group in the plan, runs your tests, and records what changed.

## Requirements

- **Claude Code**, in the terminal or the VS Code extension.
- **The Codex CLI, logged in.** Install it with `brew install --cask codex` or OpenAI's standalone installer, then run `codex login`. `codex login status` checks it. Codex's model and reasoning effort come from your own `~/.codex/config.toml`; xreview doesn't override them.
- **Python 3.10 or later**, with nothing to install: the runner uses only the standard library. The launcher, `bin/xreview`, runs the first suitable Python it finds: uv's choice (`uv python find --system '>=3.10'`) if uv is installed, then `python3`, then `python3.14` down to `python3.10` on your PATH. It runs that interpreter directly with `-I`, not through `uv run`, so files in your project can't shadow the standard library and Codex inherits your environment unchanged.
- **git**. Reviews cover committed changes only.

## Install

| | Plugin, this repo as its own marketplace (recommended) | Symlinks into `~/.claude/skills` (fallback) |
|---|---|---|
| Install | Two commands, below | Clone, add `bin/` to PATH, link each skill |
| Runner on PATH | Automatic while the plugin is enabled | You add it |
| Updates | `claude plugin update`, or turn on auto-update | `git pull`, live at once |
| Command names | `/xreview:code-prepare` and so on | `/xreview-code-prepare` and so on |
| Work machine | Subject to your organization's plugin policy | Not affected by plugin policy |

Copying the skills into each project is possible but not recommended: it puts tooling into every repository.

### Plugin

In a Claude Code session:

```text
/plugin marketplace add betsyhcamp/cross-model-review-loop
/plugin install xreview@cross-model-review-loop
```

From your shell, the same two steps are `claude plugin marketplace add betsyhcamp/cross-model-review-loop` and `claude plugin install xreview@cross-model-review-loop`. The marketplace also works from any git host, as a clone URL, or from a local path.

**Updates.** The plugin sets no version, so each commit counts as a new version. Third-party marketplaces don't update automatically by default. To update, run `claude plugin update xreview@cross-model-review-loop`, or in `/plugin` go to **Marketplaces**, select `cross-model-review-loop`, and choose **Update marketplace**. You can turn on auto-update there too. A running session keeps the version it loaded; new sessions get the update.

**On a work machine,** your organization's settings may restrict plugins: `strictKnownMarketplaces`, `blockedMarketplaces` and `disableSideloadFlags`. A mirror of this repository on your company's git server is one way around a blocked GitHub. If your organization sets `allowManagedPermissionRulesOnly` (Claude Code 2.1.282 and later), your personal allow rules are ignored, so Session B asks for permission at runner calls after its first turn (see [Running Session B unattended](#running-session-b-unattended)). With the symlink fallback, the skills' own tool approvals are ignored too.

### Symlink fallback

```sh
git clone https://github.com/betsyhcamp/cross-model-review-loop.git ~/src/cross-model-review-loop
# Add this line to your shell profile, so Claude Code's Bash tool finds the runner:
export PATH="$HOME/src/cross-model-review-loop/bin:$PATH"
mkdir -p ~/.claude/skills
for s in code-prepare code-review code-implement; do
  ln -s ~/src/cross-model-review-loop/skills/$s ~/.claude/skills/xreview-$s
done
```

The links are named `xreview-…` so `/xreview-code-review` doesn't clash with Claude Code's built-in `/code-review`. The skills still print the plugin's command names, such as `/xreview:code-review <name>`; with symlinks, type `/xreview-code-review <name>` instead. `git pull` updates everything at once.

## Usage

### Session A: prepare the review

In the session that wrote the code, commit your work, then run:

```text
/xreview:code-prepare a1b2c3d
```

- **The base commit is required.** The review covers exactly the commits after it, `<base>..HEAD`.
- **Only committed changes are reviewed.** Uncommitted edits to tracked files stop it, and it asks you to commit or stash them. Untracked files, such as notes or a spec, are listed as "not included" and never block it.
- Claude writes `reviewer-prompt.md` and `claude-handoff.md` in `.reviews/<name>/`, checks the prompt, and prints the command for Session B.

### Session B: the debate

Open a **new** Claude Code session in the same repository, and run the command Session A printed:

```text
/xreview:code-review feat-my-branch
```

Codex's review usually takes 4–9 minutes, and its response to the rebuttal 1–3 minutes. Session B stops at the Step 5 plan and shows it, with the path to the transcript.

### Decision rounds and the Decided Plan

At the Step 5 stop, talk through the open decisions. Then, if you want both models' views:

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

Most of Session B runs in later turns: a turn starts when a background Codex call finishes, and whenever you send a message. A skill's own tool approval lasts only for the turn that invokes it, so without more setup Session B stops at the next runner call and waits for your approval. There are two ways to let it run on its own:

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

**Choosing the mode:**
- **Terminal:** start with `claude --permission-mode acceptEdits`, or press Shift+Tab until the status bar shows `⏵⏵ accept edits on`.
- **VS Code:** pick **Edit automatically** in the mode selector below the prompt box. A new conversation starts in the mode you picked last.

`code-implement` in Edit automatically mode still asks before running your test command, because no rule can cover every project's command.

## While Codex is working

- **Progress.** Every 30 seconds the runner prints a line such as `xreview: Codex still working — 2m 30s, 4 commands run`. It contains none of Codex's text. The count covers finished commands, so it says "0 commands run" while the first one is still running.
- **Is Codex still running?** `pgrep -lf "^codex exec"`. The `^` matters: Codex notify hooks can linger after a turn and would match otherwise.
- **To stop a call,** ask Claude to stop the background task. The runner kills Codex and records the call as `(FAILED)` in the transcript. A call that runs 40 minutes is stopped the same way.
- **Don't edit tracked files or commit while a call runs.** After every call, the runner checks that HEAD and your tracked files haven't changed, and rejects Codex's response if they have. Its message then tells you to restore the tree, which may mean throwing away your own edit.
- **When a call fails,** the transcript gets a `(FAILED)` section, and Claude shows the runner's message, which says what to fix. Claude offers one retry once you've fixed it. Nothing retries automatically, and a failed resume never starts a new Codex session in its place.

## What "read-only" covers

Codex runs with its sandbox set to read-only and approvals set to never. That covers Codex's own shell commands and file edits. It doesn't cover:

- **MCP tools** from your Codex configuration, which run outside the sandbox. xreview doesn't disable them: the only clean switch, `--ignore-user-config`, would also drop your model and effort settings.
- **Codex notify hooks**, such as a Computer Use notifier.

The check after each call catches a change to a tracked file or a new commit, but only after it happens. Untracked files aren't watched.

## Files in your project

```text
.reviews/<name>/
├── state.json            # base, reviewed head, Codex session ID
├── reviewer-prompt.md    # written by Session A
├── claude-handoff.md     # written by Session A
├── messages/             # Claude's messages and sections (rebuttal, plan, rankings, ...)
├── runs/NN-<step>/       # one per Codex call: prompt.md, response.md, events.jsonl, stderr.txt
├── decided-plan.md       # written when you finalize
└── transcript.md         # the append-only record of the whole review
```

- `.reviews/` is added to `.git/info/exclude`, so nothing is committed and nothing in your repository changes.
- `<name>` is the branch name with `/` replaced by `-`, or `review-<short sha>` on a detached HEAD. A second review of the same branch gets `-2`, then `-3`. Apart from `/`, a branch name with characters other than letters, digits, `.`, `_` and `-` is refused.
- `runs/` keeps each Codex call's raw output for diagnosing failures. The transcript holds only Codex's final answer to each message.

## Developing xreview

Use a local-folder marketplace, which loads the plugin in place from your clone:

```text
/plugin marketplace add /path/to/cross-model-review-loop
/plugin install xreview@cross-model-review-loop
```

- To pick up an edit, restart Claude Code or run `/reload-plugins`. `/clear` doesn't reload a plugin's skill text. A skill already invoked keeps the text it loaded, so check a skill change in a new conversation after reloading.
- **Don't use `claude --plugin-dir .` in this repository.** Claude Code makes a `--plugin-dir` folder a protected path, so every write in the repository then asks for permission or goes to auto mode's classifier.
- A marketplace name can be registered only once, and the GitHub and local marketplaces share the name `cross-model-review-loop`. To switch between them, remove one first with `/plugin marketplace remove cross-model-review-loop`, which also uninstalls the plugin, then add the other and install again.

Development needs [uv](https://docs.astral.sh/uv/) and [Task](https://taskfile.dev) (`brew install go-task`). None of this is needed to use the plugin.

```sh
task install    # uv sync (Python 3.12 with pytest, ruff and pre-commit), then the git hook
task check      # pre-commit on all files, the tests on 3.12 and 3.10, plugin validation
```

`task --list` shows each step on its own: `lint`, `format`, `format-check`, `test`, `test-min` and `validate`. The tests use a fake Codex and make no model calls. On each commit, the git hook checks file hygiene, runs ruff's linter and checks the formatting.

## Future directions

Recorded, not built:

- **Version-drift safeguards** (the next planned change): an `xreview selftest` that checks Codex still resumes sessions read-only, a notice when the installed Codex version changes, and the Claude Code version in the transcript header. Until then, after a Codex update, check that a resumed session still stays read-only.
- Running the review in a separate git worktree, so uncommitted edits never block it.
- Extra debate rounds before Step 5.
- A fresh Codex review of the implemented fixes.
- Spec review, as a second set of skills.
- A GitHub Copilot reviewer, and a way to choose between reviewers.
- Launching Session B automatically.
- Resuming an interrupted Session B, or `code-implement` partway through a plan.

## License

MIT. See [LICENSE](LICENSE). Parts of the Codex adapter and its tests are adapted from Chase AI's [claudex-loop](https://github.com/chaseai-yt/claudex-loop), also MIT; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
