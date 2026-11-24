# source-text: contract - the #: comments above Task.attempt_totals and current_account in models.py must say READ-SIDE VIEW ONLY and name admission and capacity
"""Contract request 132 (accepted 2026-11-01): `Task.attempt_totals` and
`Task.current_account`, two READ-SIDE VIEWS of facts the attempt documents
and the account events already hold.

They exist so `GET /v1/tasks` need not read every page's attempts and
account events. NOTHING that admits, leases, schedules or counts capacity
may read either one: capacity is reserved all-or-nothing from the pool
documents in one transaction (invariant 2) and concurrency counts from
LEASED (invariant 3). A stale or missing copy must never change who is
admitted: the rule no-capacity-module-reads-stored-task-totals
(scripts/source_text_rules/services.py) refuses either name in every such module.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from swarm_common.models import Task

REPO = Path(__file__).resolve().parents[3]
NAMES = ("attempt_totals", "current_account")

def test_the_two_fields_default_to_none_and_come_last():
    names = [f.name for f in fields(Task)]
    # MUTATION: insert them before `platform_refusals`, so a positional
    # construction of a task changes meaning.
    # Contract request 133 appended `max_turns` after them, 2 November 2026.
    assert names[-3:] == ["attempt_totals", "current_account", "max_turns"]
    defaults = {f.name: f.default for f in fields(Task)}
    assert defaults["attempt_totals"] is None and defaults["current_account"] is None


def test_each_field_says_it_is_a_read_side_view_only():
    lines = (REPO / "apps/common/swarm_common/models.py").read_text().splitlines()
    for name in NAMES:
        at = next(i for i, line in enumerate(lines) if line.startswith(f"    {name}: "))
        comment = []
        for line in reversed(lines[:at]):
            if not line.strip().startswith("#:"):
                break
            comment.insert(0, line.strip()[2:].strip())
        text = " ".join(comment)
        assert "READ-SIDE VIEW ONLY" in text, name
        assert "admission" in text and "capacity" in text, name
