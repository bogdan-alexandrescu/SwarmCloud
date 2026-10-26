"""A MERGE fix step that reaches its implementer through the review is published without a pod (#2322).

The lane template makes the fix step depend on the review alone, and the
review depend on (and build on) the implementer. Audited 2026-10-24/25 over
tenant eng, swarm-api declined all 69 MERGE fix steps "the step builds on no
upstream branch", because it accepted the implementer only as a DIRECT
dependency, and every one started a worker pod that ran no agent.

What these tests hold:

  1. MERGE, fix -> review -> implement, every link signed: the step is
     published by the control plane, ends SUCCEEDED never leased, its
     SUCCEEDED event names the publisher, and the scheduler then dispatches
     nothing for it.
  2. A `builds_on` that is not upstream through the review (the review
     builds on another task, or does not depend on it) is still declined
     `contributors` with today's reason; a review whose spec no longer
     verifies, or an implementer of another workflow, is declined too.
  3. NOT_YET is declined `agent_runs` exactly as before, and the scheduler
     promotes the step to a worker at once.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

import pytest

from swarm_api import verdictpublish
from swarm_common.states import EventType, ParkReason, TaskState

from .test_verdict_publish import (  # noqa: F401  (fixtures, imported to be used)
    FIX,
    HEAD,
    IMPL,
    REVIEW,
    _fix,
    _push,
    _release,
    _sign,
    _workflow,
    _writes,
    allow_in_scheduler,
    client,
    contract,
    github,
    store,
    waker,
)

#: The reason the publisher gave every one of the 69 audited steps.
NOT_UPSTREAM = "the step builds on no upstream branch"


def _lane(db, objects, *, review_builds_on: str | None = IMPL,
          review_depends_on: tuple[str, ...] = (IMPL,), **kwargs) -> dict:
    """The lane template's shape: fix depends on review only, review on implement."""
    fix = _workflow(db, objects, **kwargs)
    review = db.docs[f"tasks/{REVIEW}"]
    review["depends_on"] = list(review_depends_on)
    block = {"strategy": "integrate", "carrier": "patches", "role": "contributor"}
    if review_builds_on is not None:
        block["builds_on"] = review_builds_on
    review["metadata"] = {"dispatch": block}
    _sign(review)
    fix["depends_on"] = [REVIEW]
    return _sign(fix)


def _events(db, task_id: str) -> list[dict]:
    prefix = f"tasks/{task_id}/events/"
    return [doc for key, doc in db.docs.items() if key.startswith(prefix)]


def _assert_declined(db, github, waker, body: dict, code: str) -> dict:
    assert body["report"]["published"] == [], body
    assert body["report"]["declined"] == [{"task_id": FIX, "code": code}], body
    fix = _fix(db)
    assert fix["state"] == TaskState.PARKED.value
    assert fix["park_reason"] == ParkReason.DEPENDENCY_INCOMPLETE.value
    assert "published_by" not in (fix.get("result_summary") or {})
    assert github.pulls == {}
    assert ("POST", "/repos/example-org/widgets/pulls") not in _writes(github)
    # The parent's wake: the scheduler promotes the step to a worker at once.
    assert waker.rung == [("task_finished",
                           {"task_id": REVIEW, "tenant_id": "eng", "state": "SUCCEEDED"})]
    marker = fix["metadata"][verdictpublish.CONTROL_PUBLISH_METADATA_KEY]
    assert marker["state"] == "declined" and marker["code"] == code
    return marker


def test_merge_through_the_review_is_published_without_a_pod(
    db, objects, client, github, waker, contract, make_scheduler, dispatcher, allow_in_scheduler
):
    _lane(db, objects)

    body = _push(client).json()

    assert body["report"]["published"] == [FIX], body
    assert body["report"]["declined"] == [], body
    assert github.branches[f"swarm/{FIX}"] == HEAD
    assert len(github.pulls) == 1
    fix = _fix(db)
    assert fix["state"] == TaskState.SUCCEEDED.value
    summary = fix["result_summary"]
    assert summary["published_by"] == "control_plane"
    assert summary["verdict_gate"]["agent_ran"] is False
    assert summary["git"]["integrated"]["merged"] == [f"swarm/{IMPL}"]
    # Never leased: no lease, no attempt, no pool touched.
    assert fix["current_lease_id"] is None
    assert fix["attempt_count"] == 0
    assert not [key for key in db.docs if key.startswith("leases/")]
    assert not [key for key in db.docs if key.startswith(f"tasks/{FIX}/attempts/")]
    # The publish event names the publisher and the pull request.
    published = [e for e in _events(db, FIX) if e.get("type") == EventType.SUCCEEDED.value]
    assert len(published) == 1, _events(db, FIX)
    assert published[0]["detail"]["published_by"] == "control_plane"
    assert published[0]["detail"]["pull_request"] == summary["git"]["pull_request"]["url"]
    # The step's own wake releases the merge step; the scheduler starts no pod for it.
    assert waker.rung == [("task_finished",
                           {"task_id": FIX, "tenant_id": "eng", "state": "SUCCEEDED"})]
    _release(make_scheduler)
    assert _fix(db)["state"] == TaskState.SUCCEEDED.value
    assert dispatcher.dispatched == []


@pytest.mark.parametrize("review_builds_on,review_depends_on", [
    ("task_elsewhere01", (IMPL,)),
    (None, (IMPL,)),
    (IMPL, ()),
], ids=["review_builds_on_another", "review_builds_on_nothing", "review_not_after_implementer"])
def test_a_builds_on_not_upstream_through_the_review_is_still_refused(
    db, objects, client, github, waker, contract, review_builds_on, review_depends_on
):
    _lane(db, objects, review_builds_on=review_builds_on, review_depends_on=review_depends_on)

    marker = _assert_declined(db, github, waker, _push(client).json(), "contributors")

    assert marker["reason"] == NOT_UPSTREAM


def test_a_review_whose_spec_was_rewritten_publishes_nothing(
    db, objects, client, github, waker, contract
):
    _lane(db, objects)
    # The review's links read as the lane's, but were written after signing.
    review = db.docs[f"tasks/{REVIEW}"]
    review["depends_on"] = [IMPL, "task_extra00001"]

    marker = _assert_declined(db, github, waker, _push(client).json(), "credential")

    assert marker["reason"].startswith("the review the step builds through")
    assert f"swarm/{FIX}" not in github.branches


def test_an_implementer_of_another_workflow_publishes_nothing(
    db, objects, client, github, waker, contract
):
    _lane(db, objects)
    db.docs[f"tasks/{IMPL}"]["workflow_id"] = "wf_0000000002"

    _assert_declined(db, github, waker, _push(client).json(), "workflow")


def test_not_yet_through_the_review_still_goes_to_a_worker(
    db, objects, client, github, waker, contract, make_scheduler, dispatcher, allow_in_scheduler
):
    _lane(db, objects, verdict={"verdict": "NOT_YET", "findings": ["fix it"]})

    _assert_declined(db, github, waker, _push(client).json(), "agent_runs")

    report = _release(make_scheduler)
    assert report.held_dependencies == 0, report.to_dict()
    assert report.promoted_dependencies == 1, report.to_dict()
    # Promoted out of the dependency park and into admission for a worker
    # (which, in this fake world with no forge secret, parks it on that).
    assert _fix(db)["park_reason"] != ParkReason.DEPENDENCY_INCOMPLETE.value
