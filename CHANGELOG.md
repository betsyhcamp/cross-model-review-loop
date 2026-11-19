# Changelog

All notable changes to this project will be documented in this file.

The format is based on **Keep a Changelog** (https://keepachangelog.com/en/1.1.0/),
and this project adheres to **Semantic Versioning** (https://semver.org/).

## [0.3.0] - 2026-11-17

### Added

- **One-session review** — `/xreview:code-review <base-commit>` runs the whole review from the session that wrote the code, through `xreview run` in the background: Codex's review, a sandboxed headless Claude session's rebuttal, Codex's response, Claude's plan and a first ranking round. At each stop, the session only carries the developer's exact words to both reviewers.
- **`/xreview:code-resume <review-name>`** — Picks up a review from any conversation.
- **Stop commands** — `xreview status`, `ask-claude`, `ask-codex [--decisions]`, `rank` and `finalize`.
- **`--claude-model` and `--claude-effort`** — Set the second Claude session's model and effort.
- **Recovery** — A rerun after an interruption waits for a live reviewer or reuses a finished answer. A call that may have run (exit code 4) is sent again only with `--retry-uncertain`.

### Changed

- **BREAKING** — **Install name is now `xreview@xreview`** — The repo and its marketplace are renamed `xreview`. Remove the `cross-model-review-loop` marketplace, add `betsyhcamp/xreview`, and install `xreview@xreview`.
- **BREAKING** — **The second Claude session needs Claude Code's sandbox and Auto mode** — It has only read, shell and web tools and can't write to the repo. Without the sandbox or Auto mode, the review stops before calling Codex (exit code 3).
- **Blind ranking is enforced** — Codex ranks first, and Claude's ranking turn can't read Codex's ranking.
- **The plugin has a version** — GitHub installs update only when a release raises it.
- **`code-implement` commit subjects** — Each group's title follows the repo's prefix, with no review labels.

### Removed

- **BREAKING** — **`code-prepare` skill** — `/xreview:code-review` now does its work and takes a base commit, not a review name.
- **BREAKING** — **`xreview start` and `xreview continue`** — Replaced by `xreview run` and the stop commands.

## [0.2.0] - 2026-11-08

### Added

- **`--no-decision-rounds`** — Skips Codex's ranking; the plan ends with Claude's own ranked options.
- **Find out first** — Both models read code, run harmless checks or search public sources before asking the developer.
- **Blocked decisions** — Options name what only the developer knows. An unanswered blocked decision is deferred unchanged.

### Changed

- **The first ranking round runs automatically** — After the plan, both models rank options for every open decision. Later rounds run on request.
- **Merged options replace the top-pick comparison** — Claude merges both rankings into one ranked set, each option tagged by source, with pros, cons and a recommendation.
- **The open-decision list has no options** — It describes each decision without hints, so Codex proposes its own options.
- **Finalizing** — Unanswered decisions take the latest recommendation.

## [0.1.0] - 2026-10-10

### Added

- **xreview plugin** — Codex reviews the committed changes, a fresh Claude session rebuts, Codex responds, and Claude writes a plan. After the developer decides the open items, Claude implements it as one commit per group. The repo is its own marketplace.
- **Skills** — `code-prepare` writes Codex's prompt and a handoff; `code-review` runs the review, optional decision rounds and the Decided Plan; `code-implement` makes one commit per plan group, then runs the tests.
- **Runner** — `bin/xreview` (Python 3.10+, standard library only) with `prepare`, `check`, `start`, `continue`, `prompt` and `append`. Each review keeps an append-only Markdown transcript in `.reviews/<name>/`.
- **Codex adapter** — Read-only sandbox, one resumed session for every exchange. A response is rejected if its event stream is malformed, it comes from a different session, or HEAD or a tracked file changed during it.

<!--
Guidelines:
- Keep entries user-facing: what changed, not how.
- Group changes under Added/Changed/Deprecated/Removed/Fixed/Security, and mark breaking changes **BREAKING**.
- Every merge to main is a release: the PR adds its version section, with the date it merges, and bumps "version" in .claude-plugin/plugin.json.
-->

[0.1.0]: https://github.com/betsyhcamp/xreview/releases/tag/v0.1.0
[0.2.0]: https://github.com/betsyhcamp/xreview/releases/tag/v0.2.0
[0.3.0]: https://github.com/betsyhcamp/xreview/releases/tag/v0.3.0
