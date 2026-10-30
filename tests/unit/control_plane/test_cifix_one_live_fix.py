"""One failing (pull request, head, run) gets at most one live CI-fix task (epic #1334).

Measured 2026-10-15 ~01:50Z, all at PR 1338's head 5b6ee956 and red run
38663457125: task_fcdff76b (wf_87a59d23) fixed and pushed, while
task_41ae250f (wf_90e00296, attempt 2) was a second fix for the same failure.
`stamp` counted attempts against the per-PR cap and nothing else, so a second
red-run delivery for one failure was simply attempt 2.

`stamp` now takes a CLAIM on (tenant, repository, pull request, head, run),
in one Firestore transaction, before anything is built. While the task that
holds it is not terminal -- or, for a claim whose task is not written yet,
for `CLAIM_GRACE_SECONDS` -- a second request is answered 409
`ci_fix_in_flight` naming the live task and workflow, and no task is made.

`ci_fix_in_flight` is a new refusal, so it ships report-only
(`swarm_api.refusals`): these tests switch it on, and the last one holds
what the off position does.

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

SWITCH = refusals.SWITCHES[cifix.CiFixInFlight.code].env


@pytest.fixture(autouse=True)
def _refusal_on(monkeypatch):
    monkeypatch.setenv(SWITCH, "on")


def test_a_second_request_for_a_failure_with_a_live_fix_is_answered_with_the_live_one(fix_client, db):
    original = _original(fix_client)
    first = _submit(fix_client, _red_run_payload(original, attempt=1))
    assert first.status_code == 201, first.text
    live = _fix_task(db, first)

    # The same red run delivered again -- the script numbering it attempt 2.
    second = _submit(fix_client, _red_run_payload(original, attempt=2))

    assert second.status_code == 409, second.text
    body = second.json()
    assert body["code"] == cifix.CiFixInFlight.code
    assert body["detail"]["task_id"] == live["id"]
    assert body["detail"]["workflow_id"] == live["workflow_id"]
    assert len(_ci_fix_tasks(db)) == 1, "a second fix task was made for one failure"


def test_once_the_live_fix_has_ended_the_same_failure_may_be_fixed_again(fix_client, db):
    original = _original(fix_client)
    live = _fix_task(db, _submit(fix_client, _red_run_payload(original, attempt=1)))
    _finish(live, "FAILED", last_error="evicted")

    again = _submit(fix_client, _red_run_payload(original, attempt=2))

    assert again.status_code == 201, again.text
    assert len(_ci_fix_tasks(db)) == 2


def test_another_pull_request_is_another_failure(fix_client, db):
    """Another pull request's failure gets its own fix beside a live one.

    Another run (or head) of the same pull request no longer does (#2763):
    on PR #2730 attempt 2 at a new head was admitted while attempt 1 was
    RUNNING, pushed first, and attempt 1's publish conflicted. One writer per
    pull request, so it is refused `ci_fix_lane_owns_pr` naming the live fix
    (test_cifix_one_fix_per_pr.py), not answered `ci_fix_in_flight`."""
    original = _original(fix_client)
    first = _submit(fix_client, _red_run_payload(original, attempt=1))
    assert first.status_code == 201
    other_run = _submit(fix_client, _red_run_payload(original, attempt=2, run_id=RUN_ID + 1))
    assert other_run.status_code == 409, other_run.text
    assert other_run.json()["code"] == cifix.CiFixLaneOwnsPr.code
    assert other_run.json()["detail"]["task_id"] == _fix_task(db, first)["id"]

    other = _original(fix_client)
    other_pr = _submit(fix_client, _red_run_payload(other, pull_request=PR + 1))
    assert other_pr.status_code == 201, other_pr.text
    assert len(_ci_fix_tasks(db)) == 2


def test_the_claim_rides_on_the_fix_task_and_a_caller_cannot_send_one(fix_client, db):
    original = _original(fix_client)
    task = _fix_task(db, _submit(fix_client, _red_run_payload(original)))
    assert isinstance(task["metadata"]["ci_fix"][cifix.CLAIM_FIELD], str)

    forged = _submit(fix_client, _red_run_payload(original, run_id=RUN_ID + 5, claim="x"))
    assert forged.status_code == 422, forged.text
    assert forged.json()["code"] == "invalid_dispatch"


# --------------------------------------------------------------------------
# the concurrent case: two requests inside one submission's window
# --------------------------------------------------------------------------

def _stamp(db, clock, continues: str, **record: Any) -> WorkflowCreate:
    spec = WorkflowCreate.model_validate(_red_run_payload(continues, **record))
    continuation = Continuation(root_task_id=continues, repository_url=REPO_URL,
                                task_id=continues, workflow_id="")
    return cifix.stamp(Store(db, now=clock), "eng", spec, continuation)


def test_two_requests_racing_before_either_task_exists_make_one_claim(fix_client, db, clock):
    """Both requests reach `stamp` before the first one's task is written: the
    claim is the serialisation point, taken in a transaction, so the second
    is refused although there is no task yet to find."""
    original = _original(fix_client)
    before = db.transactions_committed

    winner = _stamp(db, clock, original, attempt=1)
    with pytest.raises(cifix.CiFixInFlight) as refused:
        _stamp(db, clock, original, attempt=2)

    assert winner.metadata["ci_fix"][cifix.CLAIM_FIELD]
    assert refused.value.detail["task_id"] is None, "no task was written yet"
    claims = [doc for key, doc in db.docs.items() if key.startswith(f"{cifix.CLAIMS}/")]
    assert len(claims) == 1
    assert claims[0]["head_sha"] == HEAD and claims[0]["run_id"] == RUN_ID
    assert claims[0]["claim_id"] == winner.metadata["ci_fix"][cifix.CLAIM_FIELD]
    assert db.transactions_committed > before, "the claim was not taken in a transaction"


def test_a_claim_whose_submission_never_made_a_task_lapses(fix_client, db, clock):
    """A submission refused after `stamp` (a grant, a graph check) holds its
    claim only for the grace window, never for good."""
    original = _original(fix_client)
    _stamp(db, clock, original, attempt=1)

    clock.now = NOW + timedelta(seconds=cifix.CLAIM_GRACE_SECONDS - 1)
    with pytest.raises(cifix.CiFixInFlight):
        _stamp(db, clock, original, attempt=1)

    clock.now = NOW + timedelta(seconds=cifix.CLAIM_GRACE_SECONDS)
    taken = _stamp(db, clock, original, attempt=1)
    assert taken.metadata["ci_fix"][cifix.CLAIM_FIELD]


def test_a_task_the_racing_winner_wrote_is_found_by_its_claim(fix_client, db, clock):
    """The first request's task written after the claim: past the grace window
    a second request is still refused, because the claim's task is live."""
    original = _original(fix_client)
    live = _fix_task(db, _submit(fix_client, _red_run_payload(original, attempt=1)))

    clock.now = NOW + timedelta(hours=2)
    with pytest.raises(cifix.CiFixInFlight) as refused:
        _stamp(db, clock, original, attempt=2)
    assert refused.value.detail["task_id"] == live["id"]


def test_switched_off_a_second_request_goes_through_as_before_the_claim(fix_client, db, monkeypatch):
    """Report-only: the request is let through, holding no claim of its own,
    and the claim stays with the live fix."""
    monkeypatch.setenv(SWITCH, "off")
    original = _original(fix_client)
    first = _fix_task(db, _submit(fix_client, _red_run_payload(original, attempt=1)))

    second = _submit(fix_client, _red_run_payload(original, attempt=2))

    assert second.status_code == 201, second.text
    assert len(_ci_fix_tasks(db)) == 2
    assert cifix.CLAIM_FIELD not in _fix_task(db, second)["metadata"]["ci_fix"]
    claims = [doc for key, doc in db.docs.items() if key.startswith(f"{cifix.CLAIMS}/")]
    assert [c["claim_id"] for c in claims] == [first["metadata"]["ci_fix"][cifix.CLAIM_FIELD]]
