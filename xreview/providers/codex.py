"""Codex adapter. This is the only module that runs `codex`."""

from __future__ import annotations

import shutil
import subprocess

DISPLAY_NAME = "Codex"


class ReviewerError(Exception):
    """A failed reviewer call.

    The message is user-facing; diagnostics stay in the run folder.
    """


def version() -> str:
    """Return `codex --version` output, such as "codex-cli 0.160.1"."""
    if shutil.which("codex") is None:
        raise ReviewerError("Codex CLI not found on PATH.")
    result = subprocess.run(
        ["codex", "--version"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise ReviewerError(
            f"`codex --version` failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()
