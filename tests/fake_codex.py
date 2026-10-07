"""Fake `codex` for the tests, run through a `codex` shim on PATH.

FAKE_CODEX_CASE selects the behavior: ok | exit.
"""

import os
import sys

case = os.environ.get("FAKE_CODEX_CASE", "ok")
if "--version" in sys.argv:
    if case == "exit":
        print("codex: broken install", file=sys.stderr)
        sys.exit(7)
    print("codex-cli 0.0.0-fake")
    sys.exit(0)
