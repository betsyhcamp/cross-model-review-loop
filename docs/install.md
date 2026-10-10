# Installing `xreview`

The [README](../README.md#install) shows the usual install: the plugin, with two `/plugin` commands in a Claude Code session. This page covers the alternatives.

There are two installation approaches to consider:

| | Plugin, this repo as its own marketplace (recommended) | Symlinks into `~/.claude/skills` (fallback) |
|---|---|---|
| Install | Two `/plugin` commands, in the [README](../README.md#install) | Clone, add `bin/` to PATH, link each skill |
| Runner on PATH | Automatic while the plugin is enabled | You add it |
| Updates | `claude plugin update`, or turn on auto-update | `git pull`, changes immediately live |
| Command names | `/xreview:code-prepare` and so on | `/xreview-code-prepare` and so on |
| Company/regulated computer | Subject to your organization's plugin policy | Not affected by plugin policy |

## Plugin installation

From your shell, the same two steps are `claude plugin marketplace add betsyhcamp/cross-model-review-loop` and `claude plugin install xreview@cross-model-review-loop`. The marketplace also works from any git host, as a clone URL, or from a local path.

**On a company/regulated computer,** your organization's settings may restrict plugins through settings including `strictKnownMarketplaces`, `blockedMarketplaces` and `disableSideloadFlags`. A mirror of this repository on your company's git server is one way around a blocked GitHub. If your organization sets `allowManagedPermissionRulesOnly` (Claude Code 2.1.282 and later), your personal allow rules are ignored, so Session B asks for permission at runner calls after its first turn (see [Running Session B unattended](../README.md#running-session-b-unattended)). With the symlink fallback, the skills' own tool approvals are ignored too.

## Symlink fallback installation

```sh
git clone https://github.com/betsyhcamp/cross-model-review-loop.git ~/src/cross-model-review-loop
# Add this line to your shell profile, so Claude Code's Bash tool finds the runner:
export PATH="$HOME/src/cross-model-review-loop/bin:$PATH"
mkdir -p ~/.claude/skills
for s in code-prepare code-review code-implement; do
  ln -s ~/src/cross-model-review-loop/skills/$s ~/.claude/skills/xreview-$s
done
```

The links are named `xreview-…` so `/xreview-code-review` doesn't clash with Claude Code's built-in `/code-review`. The skills still print the plugin's command names, such as `/xreview:code-review <name>`; with symlinks, type `/xreview-code-review <name>` instead.
