"""Checks that the docs name only skills and commands that exist."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = [
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    *(ROOT / "docs").glob("*.md"),
]


def commands() -> set[str]:
    """The xreview subcommands, from its own help."""
    out = subprocess.run(
        [str(ROOT / "bin" / "xreview"), "--help"],
        capture_output=True,
        text=True,
    ).stdout
    return set(re.search(r"\{([a-z,-]+)\}", out).group(1).split(","))


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_skill_and_command_a_doc_names_exists(doc):
    text = doc.read_text()
    for skill in re.findall(r"/xreview[:-]([a-z-]+)", text):
        assert (ROOT / "skills" / skill / "SKILL.md").is_file(), skill
    known = commands()
    for command in re.findall(r"`xreview ([a-z-]+)", text):
        assert command in known, command


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_no_doc_names_a_removed_skill_or_command(doc):
    text = doc.read_text()
    for gone in ("code-prepare", "xreview start", "xreview continue"):
        assert gone not in text, gone
