Write a handoff for a fresh Claude Code session that will evaluate an external reviewer's critique of the code committed after {base} (through {head}). That session has no history with this work. Include:

- What these commits do and why: the problem, then the approach, in a few lines.
- Full paths of everything it should read before the feedback arrives: the spec or plan, every changed file, and any existing code, tests, or docs the changes depend on.
- Deliberate decisions and tradeoffs, each with its reason, stated as fact, not argument — so the session can recognize findings that are correct but missing context.
- Known gaps, shortcuts, and anything you weren't sure about. Be specific.
- The command that shows the changes: git diff {base}..{head}

End with: "Read these now. Don't change any files. The reviewer's feedback will follow." Output only the handoff itself, with no preamble.
