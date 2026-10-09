# xreview protocol

These rules apply to every xreview skill for the rest of this session. `<name>` is the review name. Its folder is `.reviews/<name>/` at the repo root.

- `xreview` is a command on your PATH. Run it as a bare command. Don't look for its source, templates or install folder.
- When a step says to follow `xreview prompt ...`, run that command and treat the text it prints as the instructions for that step.
- During the debate and decision rounds, change no file outside `.reviews/<name>/`.
- Paths you give with `--body` or `--file` are relative to `.reviews/<name>/`. Put each `--body` file in `messages/`.
- Record your own sections with `xreview append`. Only `xreview` writes `state.json` and `transcript.md`. Never edit them yourself.
- Run `xreview start` and `xreview continue` with the Bash tool's `run_in_background: true` and `timeout: 2700000`. While Codex works they print a progress line every 30 seconds; when it finishes, a status line and the path to Codex's response. You're notified when the call ends.
- Don't open a call's output (`response.md`, or `events.jsonl` while Codex is running) until the skill's next step says to. Read files with your file-reading tool, not by printing them in Bash.
- If an `xreview` command exits non-zero, stop and show the user its message, unless the skill's step says how to handle that failure. The message says what to fix. Don't work around a failed check. If it exited 3 (the Codex call failed or its response was rejected), offer one retry: the same command, run again once the user has done what the message says. If the retry fails too, stop.
