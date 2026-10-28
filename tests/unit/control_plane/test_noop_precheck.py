"""No-op steps refused before a lease (#2524, NOOP-PRECHECK).

docs/workflows.md "Reviews only where they matter, and no-op steps refused".

  1. A merge step's CI-fix or conflict-fix round whose pull request the
     merge task records as merged or closed ends at admission, CANCELLED
     with `result_summary.skipped.reason`, no lease and no pod; an open one
     is admitted as before. Read from the merge task only: no GitHub call.
  2. A gated fix step behind a NOT_YET that lists no findings runs no fix
     agent: swarm-api publishes it as for a MERGE (`verdictpublish`), with
     `result_summary.noop_precheck.reason`. A NOT_YET with a finding still
     declines to the worker, whose agent fixes it.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

from typing import Any

import pytest

from scheduler import reviewgate
from swarm_api import mergewake, validation
from swarm_common.states import TaskState

from .conftest import seed_pool, seed_task, seed_tenant
# The verdict-publish world, reused whole: its fixtures and its workflow.
from .test_verdict_publish import (  # noqa: F401 -- fixtures are used by name
    FIX,
    _fix,
    _push,
    _workflow,
    client,
    contract,
    github,
    store,
    waker,
)

MERGE = "task_merge"
ROUND = "task_cifix"


def _merge_task(db, *, state: str = "PARKED", wake_reason: str | None = None) -> None:
    doc = seed_task(db, task_id=MERGE, tenant_id="eng", state=state,
                    park_reason="CI_PENDING" if state == "PARKED" else None)
    wait: dict[str, Any] = {"head_sha": "a" * 40, "pull_request": 41}
    if wake_reason is not None:
        wait.update({"wake_requested_at": doc["created_at"], "wake_reason": wake_reason})
    doc["metadata"] = {"merge_wait": wait}


def _round(db, *, tenant_id: str = "eng", key: str = "merge_fix_round") -> dict[str, Any]:
    doc = seed_task(db, task_id=ROUND, tenant_id=tenant_id, state="READY",
                    runner_profile="claude-code", provider="anthropic")
    doc["metadata"] = {key: {"merge_task": MERGE, "round": 1,
                             "head_sha": "a" * 40, "pull_request": 41}}
    return doc


def _base(db) -> None:
    seed_tenant(db, "eng", max_active=10, credentials=("anthropic",))
    seed_tenant(db, "research", max_active=10, credentials=("anthropic",))
    seed_pool(db, "global", hard_limit=10)


def _leases(db) -> list[dict[str, Any]]:
    """The round's leases: the merge task's own wake may lease it, which is not this test's."""
    return [d for k, d in db.docs.items() if k.startswith("leases/") and d.get("task_id") == ROUND]


@pytest.mark.parametrize("state, wake_reason, ended", [
    ("SUCCEEDED", None, "merged"),
    ("PARKED", "merged", "merged"),
    ("PARKED", "closed", "closed"),
    ("PARKED", mergewake.PULL_REQUEST_MERGED, "merged"),
    ("PARKED", mergewake.PULL_REQUEST_CLOSED, "closed"),
])
def test_a_ci_fix_round_on_an_ended_pull_request_is_skipped(
    db, make_scheduler, dispatcher, state, wake_reason, ended
):
    _base(db)
    _merge_task(db, state=state, wake_reason=wake_reason)
    _round(db)

    make_scheduler().drain()

    doc = db.docs[f"tasks/{ROUND}"]
    # MUTATION: return None from `end_if_noop` and the round is leased.
    assert doc["state"] == TaskState.CANCELLED.value
    assert doc["result_summary"]["skipped"]["reason"] == (
        f"pull request #41 is already {ended}, so the fix round has nothing to fix")
    assert doc["end_cause"] is None
    assert _leases(db) == []


@pytest.mark.parametrize("wake_reason", [None, "red", "head_moved"])
def test_a_round_on_an_open_pull_request_is_admitted(db, make_scheduler, dispatcher, wake_reason):
    _base(db)
    _merge_task(db, wake_reason=wake_reason)
    _round(db)

    make_scheduler().drain()

    assert db.docs[f"tasks/{ROUND}"]["state"] != TaskState.CANCELLED.value
    # It WAS admitted: an empty drain would prove nothing.
    assert len(_leases(db)) == 1


def test_a_conflict_fix_round_on_a_merged_pull_request_is_skipped(db, make_scheduler):
    _base(db)
    _merge_task(db, wake_reason=mergewake.PULL_REQUEST_MERGED)
    _round(db, key=validation.CONFLICT_ROUND_METADATA_KEY)

    make_scheduler().drain()

    assert db.docs[f"tasks/{ROUND}"]["state"] == TaskState.CANCELLED.value
    assert _leases(db) == []


def test_another_tenants_merge_task_is_not_read(db, make_scheduler, dispatcher):
    _base(db)
    _merge_task(db, state="SUCCEEDED")
    _round(db, tenant_id="research")

    make_scheduler().drain()

    assert db.docs[f"tasks/{ROUND}"]["state"] != TaskState.CANCELLED.value


def test_the_restated_wake_reasons_are_mergewakes():
    assert set(reviewgate.PR_ENDED_REASONS) >= {
        mergewake.PULL_REQUEST_MERGED, mergewake.PULL_REQUEST_CLOSED}
    assert reviewgate.CONFLICT_ROUND_KEY == validation.CONFLICT_ROUND_METADATA_KEY


def test_a_fix_step_behind_a_zero_finding_not_yet_is_skipped(
    db, objects, client, github, waker, contract
):
    _workflow(db, objects, verdict={"verdict": "NOT_YET", "findings": []})

    response = _push(client)

    assert response.status_code == 200, response.text
    # MUTATION: drop `noop_verdict_reason` and this declines `agent_runs`.
    assert response.json()["report"]["published"] == [FIX], response.json()
    fix = _fix(db)
    assert fix["state"] == TaskState.SUCCEEDED.value
    summary = fix["result_summary"]
    assert summary["noop_precheck"]["reason"] == (
        "the review's verdict is NOT_YET but lists no findings")
    assert summary["verdict_gate"]["agent_ran"] is False
    assert summary["skipped_agent"] == "review verdict NOT_YET with no findings"
    (pull,) = github.pulls.values()
    assert "lists no findings" in pull["body"]


def test_a_not_yet_with_a_finding_still_runs_the_fix_agent(
    db, objects, client, github, waker, contract
):
    _workflow(db, objects, verdict={"verdict": "NOT_YET", "findings": ["fix it"]})

    response = _push(client)

    assert response.json()["report"]["published"] == []
    assert _fix(db)["state"] == "PARKED"
    assert github.pulls == {}
