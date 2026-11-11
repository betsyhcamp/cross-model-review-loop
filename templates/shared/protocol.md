# xreview protocol

These rules apply to every xreview skill for the rest of this session. `<name>` is the review name. Its folder is `.reviews/<name>/` at the repo root.

- `xreview` is a command on your PATH. Run it as a bare command. Don't look for its source, templates or install folder.
- When a step says to follow `xreview prompt ...`, run that command and treat the text it prints as the instructions for that step.
- Until implementation starts, don't change tracked files or commit: the review covers the code as committed, and a change ends its stops. Other files are fine when the user asks for them, such as a research spike. Put that work in your own scratchpad unless the user names a place, so it stays out of the repo the reviewers read.
- If something you need to know can be found out, find it out before answering: read the code, run something harmless, or look it up in public sources. Say what you did and what you found. Only if you can't, say what would settle it. Harmless means it changes nothing in the repo, installs nothing, touches no real service or credentials, and finishes quickly. Keep searches generic, with no project code or names.
- Don't read other reviews' folders under `.reviews/`.
- Read files with your file-reading tool, not by printing them in Bash.
- Paths you give with `--file` are relative to `.reviews/<name>/`.
- Record your own sections with `xreview append`. Only `xreview` writes `state.json` and `transcript.md`. Never edit them yourself.
- If an `xreview` command exits non-zero, stop and show the user its message, unless the skill's step says how to handle that failure. The message says what to fix. Don't work around a failed check.
