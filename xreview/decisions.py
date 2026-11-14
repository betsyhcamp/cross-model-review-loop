"""The open decisions: their fixed headings, the check that a reply has
one per decision, and the report that keeps each one's latest options.

Every decision has one permanent number, given in Part 3's order, and new
decisions continue the count. The decision copy, every merge, and the
plan's Part 4 under --no-decision-rounds give each decision exactly one
heading, `### Decision <n>: <title>`.
"""

from __future__ import annotations

from collections import Counter
import re

HEADING = re.compile(r"^### Decision (\d+): *(.*?)\s*$", re.M)
# A section ends at the next decision's heading or at a heading of level
# one or two, such as a reply's own "## Part 4 — ..." heading.
SECTION_END = re.compile(r"^(?:### Decision \d+:|#{1,2} )", re.M)


# The line a plan under --no-decision-rounds ends with when it has no open
# decisions, so that a Part 4 without headings is a fault, not "none".
NO_DECISIONS = "NO OPEN DECISIONS"


def is_none(text: str) -> bool:
    """Whether a decision copy says Part 3 has no decisions."""
    return text.strip().rstrip(".").upper() == "NONE"


def says_no_decisions(text: str) -> bool:
    """Whether a plan ends with the line that says it has no decisions."""
    lines = [line for line in text.splitlines() if line.strip()]
    last = lines[-1].strip().strip("*_`").rstrip(".") if lines else ""
    return last.upper() == NO_DECISIONS


def headings(text: str) -> list[tuple[int, str]]:
    """The (number, title) of every decision heading, in order."""
    return [(int(n), title) for n, title in HEADING.findall(text)]


def sections(text: str) -> dict[int, str]:
    """Each decision's section, from its heading to the next decision
    heading, a level-one or level-two heading, or the end. A repeated
    number keeps its first section."""
    found: dict[int, str] = {}
    for match in HEADING.finditer(text):
        n = int(match.group(1))
        rest = text[match.end() :]
        end = SECTION_END.search(rest)
        body = rest[: end.start()] if end else rest
        found.setdefault(n, (match.group(0) + body).strip("\n"))
    return found


def problems(text: str, expected: list[int]) -> list[str]:
    """What's wrong with a reply that should have exactly one heading for
    each expected number and none for any other. Empty if nothing is."""
    counts = Counter(n for n, _ in headings(text))
    found = []
    missing = [n for n in expected if counts[n] == 0]
    repeated = [n for n in expected if counts[n] > 1]
    extra = sorted(n for n in counts if n not in expected)
    for label, numbers in (
        ("Missing", missing),
        ("Repeated", repeated),
        ("Not in this round", extra),
    ):
        if numbers:
            listed = ", ".join(f"Decision {n}" for n in numbers)
            found.append(f"{label}: {listed}.")
    return found


def renumbered(text: str, start: int) -> list[tuple[int, str]]:
    """The headings a reply should have had when its numbers are meant to
    run on from start: its titles, in order, numbered from start."""
    return [(start + i, title) for i, (_, title) in enumerate(headings(text))]


def heading_lines(numbered: list[tuple[int, str]]) -> str:
    return "\n".join(f"### Decision {n}: {title}" for n, title in numbered)


def report(name: str, plan: str, latest: list[tuple[str, str]]) -> str:
    """The report: the plan, then each decision's latest section, labeled
    under its heading with where it came from. latest holds (label,
    section) in number order."""
    parts = [f"# Review report — {name}", plan.strip("\n")]
    if latest:
        parts.append("---")
        parts.append("## Latest options for each open decision")
        for label, section in latest:
            heading, _, body = section.partition("\n")
            parts.append(f"{heading}\n_{label}_\n{body}".rstrip("\n"))
    return "\n\n".join(parts) + "\n"
