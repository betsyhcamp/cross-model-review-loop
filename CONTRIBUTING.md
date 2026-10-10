# Contributing to xreview

This covers working on `xreview` itself. To use it, see the [README](README.md).

## Load the plugin from your clone

Use a local-folder marketplace, which loads the plugin in place from your clone:

```text
/plugin marketplace add /path/to/cross-model-review-loop
/plugin install xreview@cross-model-review-loop
```

- To pick up an edit, restart Claude Code or run `/reload-plugins`. `/clear` doesn't reload a plugin's skill text. A skill already invoked keeps the text it loaded, so check a skill change in a new conversation after reloading.
- **Don't use `claude --plugin-dir .` in this repository.** Claude Code makes a `--plugin-dir` folder a protected path, so every write in the repository then asks for permission or goes to auto mode's classifier.
- A marketplace name can be registered only once, and the GitHub and local marketplaces share the name `cross-model-review-loop`. To switch between them, remove one first with `/plugin marketplace remove cross-model-review-loop`, which also uninstalls the plugin, then add the other and install again.

## Set up and run the checks

Development needs [uv](https://docs.astral.sh/uv/) and [Task](https://taskfile.dev) (`brew install go-task`). None of this is needed to use the plugin.

```sh
task install    # uv sync (Python 3.12 with pytest, ruff and pre-commit), then the git hook
task check      # pre-commit on all files, the tests on 3.12 and 3.10, plugin validation
```

`task --list` shows each step on its own: `lint`, `format`, `format-check`, `test`, `test-min` and `validate`. The tests use a fake Codex and make no model calls. On each commit, the git hook checks file hygiene, runs ruff's linter and checks the formatting.

## How the launcher finds Python

The launcher, `bin/xreview`, is a POSIX `sh` script that runs the first Python 3.10 or later it finds. If uv is installed, it tries uv's choice first (`uv python find --system '>=3.10'`). Then it tries `python3`, then `python3.14` down to `python3.10` on your PATH, skipping any that's older than 3.10. If none qualifies, it exits with "needs Python 3.10 or later".

It runs that interpreter directly with `-I`, not through `uv run`, so files in the project under review can't shadow the standard library, and Codex inherits the user's environment unchanged.

## Conventions

- **Commit subjects** start with one tag, `feat:`, `fix:`, `refac:`, `docs:`, `test:` or `chore:`, then a capitalized summary in the imperative, such as `fix: Say to restore the tree after any call that changed it`.
- **The runner uses only the standard library,** so it runs on any Python 3.10 or later with nothing to install. pytest, ruff and pre-commit are development-only, in `pyproject.toml`'s dev group.
- **Python** is formatted with `ruff format`, with lines of at most 79 characters.
