"""Contract request 132 (accepted 2026-11-01): `Task.attempt_totals` and
`Task.current_account`, two READ-SIDE VIEWS of facts the attempt documents
and the account events already hold.

They exist so `GET /v1/tasks` need not read every page's attempts and
account events. NOTHING that admits, leases, schedules or counts capacity
may read either one: capacity is reserved all-or-nothing from the pool
documents in one transaction (invariant 2) and concurrency counts from
LEASED (invariant 3). A stale or missing copy must never change who is
admitted, so this file greps every such module for either name.
"""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

from swarm_common.models import Task

REPO = Path(__file__).resolve().parents[3]
NAMES = ("attempt_totals", "current_account")

#: Every module that admits, leases, dispatches, schedules or counts
#: capacity: the scheduler, the reconciler, the quota broker (account
#: leasing), the frozen admission transaction and its states, swarm-api's
#: capacity and headroom readers and the worker's account lease.
ADMISSION_AND_CAPACITY = (
    "apps/scheduler/scheduler",
    "apps/reconciler/reconciler",
    "apps/quota-broker/quota_broker",
    "apps/common/swarm_common/admission.py",
    "apps/common/swarm_common/states.py",
    "apps/swarm-api/swarm_api/headroom.py",
    "apps/swarm-api/swarm_api/served_limits.py",
    "apps/swarm-api/swarm_api/accountholds.py",
    "apps/swarm-api/swarm_api/waiting.py",
    "apps/agent-worker/agent_worker/accountlease.py",
)


def test_the_two_fields_default_to_none_and_come_last():
    names = [f.name for f in fields(Task)]
    # MUTATION: insert them before `platform_refusals`, so a positional
    # construction of a task changes meaning.
    assert names[-2:] == ["attempt_totals", "current_account"]
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


#: The field as a document key or an attribute: `"attempt_totals"`,
#: `.current_account`. A local variable of the same spelling (the scheduler's
#: `current_account` is a GKE service account) is not a read of the field.
READ = re.compile(r"""(["'.])(""" + "|".join(NAMES) + r")\b")


def test_the_guard_sees_a_read_of_either_field():
    for line in ('task.get("current_account")', "doc['attempt_totals']",
                 "if task.attempt_totals:", "x = task.current_account"):
        assert READ.search(line), line
    assert not READ.search('current_account = str(template.service_account or "")')


def _python_files(rel: str) -> list[Path]:
    root = REPO / rel
    return [root] if root.is_file() else sorted(root.rglob("*.py"))


def test_no_admission_leasing_scheduler_or_capacity_module_reads_either_field():
    pattern = READ
    visited = 0
    offenders: list[str] = []
    for rel in ADMISSION_AND_CAPACITY:
        files = _python_files(rel)
        assert files, f"{rel} has no Python file: the guard would pass on nothing"
        for path in files:
            visited += 1
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    # The sweep ran: an empty result is only a pass if files were read.
    assert visited >= 30, visited
    assert offenders == [], offenders
