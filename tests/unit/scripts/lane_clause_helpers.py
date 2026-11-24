"""The lane clause every CI job's `if:` ends with, and the condition before it
(issue 71: moved out of test_fast_checks_on_lane_prs.py, which no longer reads
the workflows, so the tests that import these do not import a test module).

Pure string helpers: nothing here opens a file.
"""

from __future__ import annotations

import re

#: The clause every job's `if:` in application.yml, security.yml and
#: terraform.yml ends with (`&& LANE_SKIP`): false on a lane's pull request.
LANE_SKIP = ("!(github.event_name == 'pull_request' && startsWith(github.head_ref, 'swarm/') "
             "&& github.event.pull_request.head.repo.full_name == github.repository)")


def _balanced(text: str) -> bool:
    depth = 0
    for char in re.sub(r"'[^']*'", "", text):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth < 0:
            return False
    return depth == 0


def _without_the_clause(condition: str) -> str:
    """The job's `if:` before the lane clause, unwrapped: what it ran before
    2026-11-12."""
    inner = condition.strip().removeprefix("${{").removesuffix("}}").strip()
    if inner == LANE_SKIP:
        return ""
    assert inner.endswith(" && " + LANE_SKIP), inner
    before = inner[: -len(" && " + LANE_SKIP)]
    if before.startswith("(") and before.endswith(")") and _balanced(before[1:-1]):
        before = before[1:-1]
    return before


def without_lane_clause(condition: object, *, required: bool = True) -> str:
    """A job's `if:` as it was written before the lane clause, `${{ }}` and
    all. Fails when the clause is missing, unless `required` is false (a sweep
    over every job, ci-gate included)."""
    text = str(condition or "").strip()
    inner = text.removeprefix("${{").removesuffix("}}").strip()
    if not required and not (inner == LANE_SKIP or inner.endswith(" && " + LANE_SKIP)):
        return text
    before = _without_the_clause(text)
    return "${{ " + before + " }}" if text.startswith("${{") and before else before
