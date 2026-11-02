"""The merge step's rounds wait on a live plain continuation of the lane (#2685).

`mergewake._live_writer` (#2577) waited on the merge's own rounds and on the
writers `cifix.live_pr_writer` finds, which are only the ones carrying a
writer's mark. A plain continuation of the lane -- a `continues_task`
workflow with no mark, or a `publish_to_pr` lane aimed at the root -- pushes
to the same `swarm/<root>` branch and was never read. Measured 2026-10-28 on
PR #2617: the merge step of wf_f20546f4 submitted ci-fix:1 (task_99b8e1e0,
13:50:24Z, from e80b3f8) beside the plain lane task_7ff30c32, RUNNING on the
same branch; the lane pushed 5bc405a first and the round's push was rejected
non-fast-forward. So the tick waits on `cifix.live_plain_continuation` too:
no round is claimed or submitted, and the merge stays parked on CI_PENDING.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import cifix, mergewake
from swarm_common.states import ParkReason

from .conftest import seed_task
from .test_merge_wake import (  # noqa: F401 - the fixtures are used by name
    NUMBER, ROOT, _conflict_workflows, _fix_record, _fix_workflows, _tick, _wait, clock,
    conflicting_merge, fixable_merge, github, tenant_tokens, wake_client,
)

LANE_TASK = "task_" + "7" * 20
LANE_WORKFLOW = "wf_" + "8" * 20
OTHER_ROOT = "task_" + "9" * 20


def plain_lane(db, *, via: str = "continues", state: str = "RUNNING", tenant_id: str = "eng",
               root: str = ROOT, runner_profile: str = "claude-code",
               extra: dict[str, Any] | None = None) -> dict:
    """A lane continuing `root` with no writer's mark: `continues` or `publish_to_pr`."""
    lane = seed_task(db, task_id=LANE_TASK, tenant_id=tenant_id, state=state,
                     runner_profile=runner_profile, workflow_id=LANE_WORKFLOW)
    dispatch: dict[str, Any] = (
        {"continues": root} if via == "continues"
        else {"publish_to_pr": {"task_id": root, "pull_request": NUMBER}}
    )
    lane["metadata"] = {"dispatch": dispatch, **(extra or {})}
    return lane


def _assert_waiting(db, body: dict) -> None:
    assert body["report"]["fixing"] == 0 and body["report"]["woken"] == 0, body
    assert body["report"]["waiting"] == 1, body
    task = db.docs["tasks/task_merge"]
    assert task["state"] == "PARKED" and task["park_reason"] == ParkReason.CI_PENDING.value
    assert mergewake.WAKE_MARKER not in _wait(db)


def test_a_running_plain_continuation_of_the_root_keeps_the_merge_waiting_and_submits_no_round(
    db, wake_client, github
):
    fixable_merge(db, github, rounds=2)
    plain_lane(db)

    body = _tick(wake_client).json()

    _assert_waiting(db, body)
    assert _fix_workflows(db) == [], "ci-fix:1 was submitted beside a live lane on the branch"
    assert _fix_record(db) == {}, "no round, and no re-run, is claimed beside the live lane"


def test_a_live_publish_to_pr_lane_holds_the_round_too(db, wake_client, github):
    fixable_merge(db, github, rounds=2)
    plain_lane(db, via="publish_to_pr", state="READY")

    body = _tick(wake_client).json()

    _assert_waiting(db, body)
    assert _fix_workflows(db) == []
    assert _fix_record(db) == {}


def test_a_conflict_round_waits_on_a_plain_continuation(db, wake_client, github):
    conflicting_merge(db, github)
    plain_lane(db)

    body = _tick(wake_client).json()

    _assert_waiting(db, body)
    assert _conflict_workflows(db) == [], "a conflict round was submitted beside the live lane"
    assert _fix_record(db) == {}


@pytest.mark.parametrize("lane", [
    {"state": "SUCCEEDED"},
    {"state": "FAILED"},
    {"state": "CANCELLED"},
    {"tenant_id": "research"},
    {"root": OTHER_ROOT},
    {"runner_profile": "merge"},
], ids=["succeeded", "failed", "cancelled", "another-tenant", "another-root", "a-merge-step"])
def test_a_terminal_or_other_tenant_or_other_root_continuation_does_not_hold_the_round(
    db, wake_client, github, lane
):
    fixable_merge(db, github, rounds=2)
    plain_lane(db, **lane)

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 1, body
    (workflow,) = _fix_workflows(db)
    (claimed,) = _fix_record(db)["rounds"]
    assert claimed["workflow_id"] == workflow["workflow_id"]


def test_the_merges_own_round_is_never_read_as_a_plain_continuation(db, wake_client, github):
    """ci-fix:1 continues the root and carries `merge_fix_round`: once it is
    recorded, it is the merge's own round the tick waits on, not a lane."""
    fixable_merge(db, github, rounds=2)
    _tick(wake_client)
    (claimed,) = _fix_record(db)["rounds"]
    round_task = db.docs[f"tasks/{claimed['task_id']}"]
    assert round_task["metadata"]["dispatch"]["continues"] == ROOT
    assert cifix.live_plain_continuation(db, "eng", ROOT) is None
