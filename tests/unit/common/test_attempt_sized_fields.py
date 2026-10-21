"""Contract request 111: `Attempt` records the size it was rendered at (SIZES-CONTRACT-2, part of #1689).

docs/design/runner-sizes.md §5 request RS-4, accepted by the owner on
2026-10-20 (§7). Once an admin can resize a class, the catalogue no longer
says what an older attempt ran at, so the scheduler's `create_attempt` (lane
SIZES-SCHED-2) pins it on the attempt. What is held here:

  * `size_version`, `sized_cpu_request`, `sized_cpu_limit`,
    `sized_memory_gib`, `sized_disk_gib`, `sized_live_split` exist, default to
    None, and sit at the end of the class, so no positional construction
    changes meaning;
  * they round-trip through the document shape (`asdict` and back), the live
    split with both its keys;
  * a live split missing `worker` or `display`, carrying a third key, or
    carrying anything but the three numbers is refused at construction;
  * none of them is in the signed step spec: the worker acts on its cgroup,
    not on them, so they are not a task field and a task document that
    carried them signs the same bytes.

MUTATION: drop `sized_live_split`'s check from `Attempt.__post_init__` and
`test_a_malformed_live_split_is_refused` fails; give `sized_cpu_limit` a
default of 8 and `test_the_sized_fields_default_to_none` fails.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from swarm_common.models import Attempt, Task
from swarm_common.specsign import canonical_step_spec

REQUESTS = Path(__file__).resolve().parents[3] / "docs" / "contract-change-requests.md"

SIZED = (
    "size_version",
    "sized_cpu_request",
    "sized_cpu_limit",
    "sized_memory_gib",
    "sized_disk_gib",
    "sized_live_split",
)

LIVE = {
    "worker": {"cpu_request": 2, "cpu_limit": 4, "memory_gib": 3},
    "display": {"cpu_request": 2, "cpu_limit": 3, "memory_gib": 5},
}


def _attempt(**sized) -> Attempt:
    return Attempt(
        attempt_id="att_1",
        task_id="task_1",
        tenant_id="eng",
        generation=1,
        lease_id="lease_1",
        backend="GKE_AUTOPILOT",
        created_at=datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc),
        **sized,
    )


def test_the_sized_fields_default_to_none():
    attempt = _attempt()
    # MUTATION: default any of them to a number and an attempt from before the
    # change would claim a size nobody recorded.
    for name in SIZED:
        assert getattr(attempt, name) is None, name


def test_the_sized_fields_are_the_last_of_the_class():
    names = [f.name for f in dataclasses.fields(Attempt)]
    # MUTATION: insert them beside `cpu_limit_cores` and every positional
    # construction after it shifts.
    assert tuple(names[-len(SIZED):]) == SIZED


def test_a_plain_attempt_round_trips():
    attempt = _attempt(
        size_version=2,
        sized_cpu_request=6,
        sized_cpu_limit=12,
        sized_memory_gib=12,
        sized_disk_gib=6,
    )
    doc = dataclasses.asdict(attempt)
    assert {k: doc[k] for k in SIZED} == {
        "size_version": 2,
        "sized_cpu_request": 6,
        "sized_cpu_limit": 12,
        "sized_memory_gib": 12,
        "sized_disk_gib": 6,
        "sized_live_split": None,
    }
    assert Attempt(**doc) == attempt


def test_a_live_attempt_round_trips_with_both_keys():
    attempt = _attempt(
        size_version=0,
        sized_cpu_request=4,
        sized_cpu_limit=8,
        sized_memory_gib=8,
        sized_disk_gib=4,
        sized_live_split=LIVE,
    )
    doc = dataclasses.asdict(attempt)
    assert doc["sized_live_split"] == LIVE
    back = Attempt(**doc)
    assert back == attempt
    assert set(back.sized_live_split) == {"worker", "display"}


@pytest.mark.parametrize(
    "split",
    [
        {"worker": LIVE["worker"]},
        {"display": LIVE["display"]},
        {**LIVE, "agent": LIVE["worker"]},
        {**LIVE, "worker": {"cpu_request": 2, "cpu_limit": 4}},
        {**LIVE, "worker": {**LIVE["worker"], "disk_gib": 1}},
        {**LIVE, "display": {**LIVE["display"], "memory_gib": "5"}},
        {**LIVE, "display": {**LIVE["display"], "cpu_limit": True}},
        {**LIVE, "worker": None},
        [LIVE["worker"], LIVE["display"]],
    ],
    ids=[
        "no-display",
        "no-worker",
        "third-key",
        "missing-memory",
        "extra-field",
        "string-number",
        "bool-number",
        "worker-none",
        "a-list",
    ],
)
def test_a_malformed_live_split_is_refused(split):
    # MUTATION: accept any mapping and a reader that trusts the two keys
    # (SIZES-DETAIL, the preview's by_size_version) breaks on the record.
    with pytest.raises(ValueError, match="sized_live_split"):
        _attempt(sized_live_split=split)


def test_no_sized_field_is_a_task_field():
    task_fields = {f.name for f in dataclasses.fields(Task)}
    # MUTATION: add one to Task and it becomes part of what a caller submits
    # (invariant 10) and of what the step spec must sign.
    assert not task_fields & set(SIZED)


def test_the_sized_fields_are_outside_the_signed_step_spec():
    doc = {
        "tenant_id": "eng",
        "runner_profile": "claude-code",
        "resource_class": "standard",
        "timeout_seconds": 3600,
        "max_attempts": 3,
        "input": {"prompt": "p"},
        "metadata": {},
    }
    signed = canonical_step_spec(doc, task_id="task_1")
    pinned = dataclasses.asdict(_attempt(sized_cpu_limit=16, sized_live_split=LIVE))
    carrying = {**doc, **{name: pinned[name] for name in SIZED}}
    # The worker acts on its cgroup, not on these; a task document that
    # somehow carried them must neither verify differently nor fail to verify.
    assert canonical_step_spec(carrying, task_id="task_1") == signed
    for name in SIZED:
        assert name.encode() not in signed


def test_request_rs_4_is_filed_as_entry_111():
    text = REQUESTS.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("| 111 |")]
    assert len(rows) == 1, rows
    assert "SIZES-CONTRACT-2" in rows[0] and "RS-4" in rows[0]
    assert "APPLIED" in rows[0]
    assert re.search(r"^## 111\. .*\(SIZES-CONTRACT-2, runner sizes request RS-4, part of #1689\)$", text, re.M)
