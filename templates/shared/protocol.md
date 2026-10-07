# xreview protocol

These rules apply to every xreview skill for the rest of this session. `<name>` is the review name. Its folder is `.reviews/<name>/` at the repo root.

- `xreview` is a command on your PATH. Run it as a bare command. Don't look for its source, templates or install folder.
- When a step says to follow `xreview prompt ...`, run that command and treat the text it prints as the instructions for that step.
- Only `xreview` writes `state.json` and `transcript.md`. Never edit them yourself.
- If an `xreview` command exits non-zero, stop and show the user its message. Don't work around a failed check:
  - uncommitted changes to tracked files: ask the user to commit or stash them;
  - HEAD has moved since prepare: tell the user to re-run code-prepare.
