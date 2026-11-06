"""A CI fix for a pull request is refused while another CI fix of it is live (#2763).

Measured on PR #2730: scripts/ci-fix.sh's attempt 1 (task_f11210cf) was
RUNNING on head bb31584 when attempt 2 (task_af11c3ba) was admitted for a red
run at 62a6fbac. Attempt 2 pushed 9e193de0 first, and attempt 1's publish
conflicted. `stamp`'s only check between two CI fixes was `_claim`, whose key
is (tenant, repository, pull request, head, run): another head or another run
was another failure, so both fixes cloned one branch and raced to push.

So `stamp` refuses a `metadata.ci_fix` submission while another CI fix of the
same pull request is live -- a non-terminal task continuing the same branch
task, or a claim still inside `CLAIM_GRACE_SECONDS` whose task is not
written yet -- at any head and from any run, with the one-writer-per-PR
refusal, 409 `ci_fix_lane_owns_pr`, which ships on and scripts/ci-fix.sh
already reads as a skip. No task and no claim is made. The live fix's push
runs CI again at its head, and a red run there gets its own fix. A
re-delivery of the very same failure is still `_claim`'s to answer,
`ci_fix_in_flight`.

No credentials, no network, no emulator.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from swarm_api import cifix, refusals
from swarm_api.continuation import Continuation
from swarm_api.schemas import WorkflowCreate
from swarm_api.store import Store

from .conftest import seed_task
from .test_ci_fix_postback import (  # noqa: F401 - fixtures
    HEAD,
    NOW,
    PR,
    REPO_URL,
    RUN_ID,
    _ci_fix_tasks,
    _finish,
    _fix_task,
    _members_hold_grants,
    _original,
    _red_run_payload,
    _submit,
    clock,
    fix_client,
    github,
    tenant_tokens,
)

#: The head CI went red at after the first fix's head: another 40-hex sha.
NEW_HEAD = "b" * 40
SWITCH = refusals.env_name(cifix.CiFixLaneOwnsPr.code)
IN_FLIGHT = refusals.env_name(cifix.CiFixInFlight.code)


@pytest.fixture(autouse=True)
def _shipped_position(monkeypatch):
    """`ci_fix_lane_owns_pr` as it ships: nothing in the environment."""
    monkeypatch.delenv(SWITCH, raising=False)


def _claims(db) -> list[dict[str, Any]]:
    return [doc for key, doc in db.docs.items() if key.startswith(f"{cifix.CLAIMS}/")]


def _live_fix(client, db) -> tuple[str, dict[str, Any]]:
    """The branch task and its first CI fix, admitted and left RUNNING."""
    original = _original(client)
    first = _submit(client, _red_run_payload(original, attempt=1))
    assert first.status_code == 201, first.text
    live = _fix_task(db, first)
    live["state"] = "RUNNING"
    return original, live


def _stamp(db, clock, continues: str, **record: Any) -> WorkflowCreate:
    spec = WorkflowCreate.model_validate(_red_run_payload(continues, **record))
    continuation = Continuation(root_task_id=continues, repository_url=REPO_URL,
                                task_id=continues, workflow_id="")
    return cifix.stamp(Store(db, now=clock), "eng", spec, continuation)


def test_a_ci_fix_at_a_new_head_beside_a_live_fix_of_the_pull_request_is_refused_naming_it(
    fix_client, db
):
    """PR #2730's case: attempt 2 at a new head and a new run while attempt 1
    is RUNNING. Before #2763 it was admitted 201."""
    original, live = _live_fix(fix_client, db)

    # MUTATION: drop the `_refuse_if_ci_fix_live` call from stamp -- 201.
    second = _submit(fix_client, _red_run_payload(original, attempt=2, head_sha=NEW_HEAD,
                                                  run_id=RUN_ID + 1))

    assert second.status_code == 409, second.text
    body = second.json()
    assert body["code"] == cifix.CiFixLaneOwnsPr.code
    assert body["detail"]["task_id"] == live["id"]
    assert body["detail"]["workflow_id"] == live["workflow_id"]
    assert body["detail"]["ci_fix"] is True
    assert body["detail"]["head_sha"] == HEAD
    assert body["detail"]["pull_request"] == PR
    assert [t["id"] for t in _ci_fix_tasks(db)] == [live["id"]], "a second fix task was made"
    assert len(_claims(db)) == 1, "the refused request took a claim"


def test_another_run_at_the_same_head_beside_a_live_fix_is_refused(fix_client, db):
    original, live = _live_fix(fix_client, db)

    second = _submit(fix_client, _red_run_payload(original, attempt=2, run_id=RUN_ID + 1))

    assert second.status_code == 409, second.text
    assert second.json()["code"] == cifix.CiFixLaneOwnsPr.code
    assert second.json()["detail"]["task_id"] == live["id"]
    assert len(_ci_fix_tasks(db)) == 1
    assert len(_claims(db)) == 1


@pytest.mark.parametrize("missing", [("head_sha", "run_id"), ("run_id",), ("head_sha",)],
                         ids=["no-head-no-run", "head-only", "run-only"])
def test_a_request_without_a_head_or_a_run_beside_a_live_fix_is_refused(
    fix_client, db, missing
):
    """A record lacking a head or a run names no failure `_claim` answers (it
    takes no claim), so it is never the live fix's own failure: the gate
    refuses it beside the live fix. Before the review fix it got 201 and a
    second fix (#2763)."""
    original, live = _live_fix(fix_client, db)
    payload = _red_run_payload(original, attempt=2)
    for key in missing:
        del payload["metadata"]["ci_fix"][key]

    # MUTATION: `_same_failure` over only the keys the record carries -- 201.
    second = _submit(fix_client, payload)

    assert second.status_code == 409, second.text
    assert second.json()["code"] == cifix.CiFixLaneOwnsPr.code
    assert second.json()["detail"]["task_id"] == live["id"]
    assert second.json()["detail"]["ci_fix"] is True
    assert [t["id"] for t in _ci_fix_tasks(db)] == [live["id"]], "a second fix task was made"
    assert len(_claims(db)) == 1


def test_the_same_failure_is_still_answered_ci_fix_in_flight(fix_client, db, monkeypatch):
    """A re-delivery of the very failure the live fix is on is `_claim`'s:
    the new gate passes over it, so its answer is unchanged."""
    monkeypatch.setenv(IN_FLIGHT, "on")
    original, live = _live_fix(fix_client, db)

    again = _submit(fix_client, _red_run_payload(original, attempt=2))

    assert again.status_code == 409, again.text
    assert again.json()["code"] == cifix.CiFixInFlight.code
    assert again.json()["detail"]["task_id"] == live["id"]
    assert len(_ci_fix_tasks(db)) == 1


def test_a_racing_claim_at_another_head_refuses_before_its_task_exists(fix_client, db, clock):
    """Both requests reach `stamp` before the first one's task is written: the
    first's claim, inside its grace window, is the live fix."""
    original = _original(fix_client)
    _stamp(db, clock, original, attempt=1)

    with pytest.raises(cifix.CiFixLaneOwnsPr) as refused:
        _stamp(db, clock, original, attempt=2, head_sha=NEW_HEAD, run_id=RUN_ID + 1)

    assert refused.value.detail["task_id"] is None, "no task was written yet"
    assert refused.value.detail["ci_fix"] is True
    assert refused.value.detail["head_sha"] == HEAD
    assert [c["head_sha"] for c in _claims(db)] == [HEAD]


def _terminal(state: str):
    def arrange(client, db, clock) -> str:
        original, live = _live_fix(client, db)
        _finish(live, state)
        return original
    return arrange


def _other_pull_request(client, db, clock) -> str:
    original = _original(client)
    other = _submit(client, _red_run_payload(original, pull_request=PR + 1))
    assert other.status_code == 201, other.text
    return original


def _other_tenant(client, db, clock) -> str:
    original = _original(client)
    doc = seed_task(db, task_id="task_" + "e1" * 10, tenant_id="research", state="RUNNING",
                    workflow_id="wf_" + "e1" * 4)
    doc["metadata"] = {
        "dispatch": {"strategy": "direct-pr", "continues": original},
        "ci_fix": {"pull_request": PR, "head_sha": HEAD, "run_id": RUN_ID,
                   "attempt": 1, "max_attempts": 2},
    }
    return original


def _lapsed_claim(client, db, clock) -> str:
    original = _original(client)
    _stamp(db, clock, original, attempt=1)
    clock.now = NOW + timedelta(seconds=cifix.CLAIM_GRACE_SECONDS)
    return original


@pytest.mark.parametrize("arrange", [
    _terminal("FAILED"), _terminal("SUCCEEDED"), _other_pull_request, _other_tenant,
    _lapsed_claim,
], ids=["failed", "succeeded", "other-pull-request", "other-tenant", "lapsed-claim"])
def test_a_terminal_or_other_pr_or_other_tenant_or_lapsed_fix_does_not_refuse(
    fix_client, db, clock, arrange
):
    original = arrange(fix_client, db, clock)

    second = _submit(fix_client, _red_run_payload(original, attempt=2, head_sha=NEW_HEAD,
                                                  run_id=RUN_ID + 1))

    assert second.status_code == 201, second.text


def test_the_switch_off_lets_the_second_fix_through(fix_client, db, monkeypatch):
    """Off, the request goes on as it did before #2763."""
    monkeypatch.setenv(SWITCH, "off")
    original, _ = _live_fix(fix_client, db)

    second = _submit(fix_client, _red_run_payload(original, attempt=2, head_sha=NEW_HEAD,
                                                  run_id=RUN_ID + 1))

    assert second.status_code == 201, second.text
    assert len(_ci_fix_tasks(db)) == 2
