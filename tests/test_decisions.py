"""Tests for the decision headings, their check and the report."""

from __future__ import annotations

import pytest

from xreview import decisions

MERGE = """## Part 4 — Options for the open decisions (Claude and Codex)

### Decision 1: Retry policy
1. Retry on 429 only (both).
   - Pros: safe.

### Decision 3: Field name
1. dead_letter (Claude).

## Positions changed
None.
"""


def test_headings_are_read_in_order_with_their_titles():
    assert decisions.headings(MERGE) == [
        (1, "Retry policy"),
        (3, "Field name"),
    ]


def test_a_section_runs_to_the_next_decision_or_a_higher_heading():
    found = decisions.sections(MERGE)
    assert found[1] == (
        "### Decision 1: Retry policy\n"
        "1. Retry on 429 only (both).\n"
        "   - Pros: safe."
    )
    assert found[3] == "### Decision 3: Field name\n1. dead_letter (Claude)."


def test_a_level_three_heading_inside_a_section_stays_in_it():
    text = "### Decision 2: X\nIntro.\n### Option A\nDetail.\n"
    assert decisions.sections(text)[2] == text.rstrip("\n")


@pytest.mark.parametrize(
    "text, expected, found",
    [
        (MERGE, [1, 3], []),
        (MERGE, [1, 2, 3], ["Missing: Decision 2."]),
        (MERGE, [1], ["Not in this round: Decision 3."]),
        (
            MERGE + "\n### Decision 1: Again\nx\n",
            [1, 3],
            ["Repeated: Decision 1."],
        ),
        ("No headings at all.", [1], ["Missing: Decision 1."]),
    ],
)
def test_problems_name_what_is_missing_repeated_or_extra(
    text, expected, found
):
    assert decisions.problems(text, expected) == found


def test_headings_can_be_renumbered_from_a_start():
    text = "### Decision 1: A\nx\n\n### Decision 3: B\ny\n"
    assert decisions.renumbered(text, 1) == [(1, "A"), (2, "B")]
    assert decisions.renumbered(text, 5) == [(5, "A"), (6, "B")]
    assert decisions.heading_lines([(5, "A"), (6, "B")]) == (
        "### Decision 5: A\n### Decision 6: B"
    )


@pytest.mark.parametrize(
    "text, none", [("NONE", True), (" none.\n", True), ("None of them", False)]
)
def test_a_copy_saying_none_is_recognized(text, none):
    assert decisions.is_none(text) is none


def test_the_report_labels_each_decision_under_its_heading():
    report = decisions.report(
        "r1",
        "The plan.\n",
        [("From decision round 2", "### Decision 1: A\nBody one.")],
    )
    assert report == (
        "# Review report — r1\n\n"
        "The plan.\n\n"
        "---\n\n"
        "## Latest options for each open decision\n\n"
        "### Decision 1: A\n_From decision round 2_\nBody one.\n"
    )


def test_a_report_with_no_decisions_is_the_plan():
    assert decisions.report("r1", "The plan.", []) == (
        "# Review report — r1\n\nThe plan.\n"
    )
