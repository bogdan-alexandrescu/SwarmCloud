"""The frozen contract's acceptance comments name the owner by role (contract request 154).

The repository moves to a fresh public repository holding one commit of main's
tree, so nothing in it may identify a person (CLAUDE.md rule 4). Five comment
lines in `apps/common/swarm_common/` recorded who accepted a contract request
by a person's initials; the owner accepted on 11 November 2026 that they take
the role placeholder "the owner", with no change in behaviour. This holds each
of the five comments to the role, and request 154 to the record of it.

The lines are found by the request each comment records, not by line number,
so an edit above them does not move the test off them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
FROZEN = REPO / "apps" / "common" / "swarm_common"
REQUESTS = REPO / "docs" / "contract-change-requests.md"

#: (file, the comment's request marker, what the comment must say of the owner).
COMMENTS = (
    ("profiles.py", "CONTRACT REQUEST 117,", "accepted by the owner 2026-10-26"),
    ("profiles.py", "CONTRACT REQUEST 147,", "(the owner's pick after"),
    ("profiles.py", "CONTRACT REQUEST 124,", "accepted by the owner 2026-10-27"),
    ("states.py", "request 125 (FL-CONTRACT, fix loop request B)", "accepted by the owner"),
    ("states.py", "Contract request 123,", "accepted by the owner 2026-10-28."),
)


def _comment_line(name: str, marker: str) -> str:
    lines = [line for line in (FROZEN / name).read_text(encoding="utf-8").splitlines() if marker in line]
    assert len(lines) == 1, f"{name}: expected one comment line with {marker!r}, found {len(lines)}"
    return lines[0]


@pytest.mark.parametrize(("name", "marker", "says"), COMMENTS)
def test_each_acceptance_comment_names_the_owner_by_role(name: str, marker: str, says: str):
    line = _comment_line(name, marker)
    # MUTATION: put a person's initials back in place of "the owner" -> red.
    assert line.lstrip().startswith("#"), f"{name}: the {marker!r} line is not a comment"
    assert says in line, f"{name}: the {marker!r} comment does not say {says!r}"


def _entry(text: str, number: int) -> str:
    match = re.search(rf"^## {number}\. .*$", text, flags=re.M)
    assert match is not None, f"contract-change-requests.md has no entry {number}"
    rest = text[match.end():]
    nxt = re.search(r"^## \d+\. ", rest, flags=re.M)
    return match.group(0) + (rest[: nxt.start()] if nxt else rest)


def test_request_154_records_the_owners_acceptance_and_the_five_lines():
    text = REQUESTS.read_text(encoding="utf-8")
    body = _entry(text, 154)
    # MUTATION: drop the acceptance, or its date -> red (and
    # scripts/lib/check-frozen-contract.sh would refuse the swarm_common diff).
    assert "**Status:** ACCEPTED by the owner 11 November 2026" in body
    # MUTATION: leave a file or a line out of the record -> red.
    for name, marker, _ in COMMENTS:
        assert f"`apps/common/swarm_common/{name}`, line " in body, f"request 154 does not name {name}"
    assert len(re.findall(r"^\* `apps/common/swarm_common/(?:profiles|states)\.py`, line \d+:", body, re.M)) == 5
    assert "the role placeholder \"the owner\"" in body
    assert "Nothing. All five lines are comments" in body
    rows = [line for line in text.splitlines() if line.startswith("| 154 |")]
    assert len(rows) == 1, rows
    assert "ACCEPTED by the owner 11 November 2026" in rows[0] and "behaviour unchanged" in rows[0]
