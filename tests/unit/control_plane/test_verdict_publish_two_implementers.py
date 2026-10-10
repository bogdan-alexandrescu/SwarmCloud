"""A two-implementer integrator whose `builds_on` implementer changed nothing is the worker's (#978).

Contract request 52 lets swarm-api open a MERGE verdict's pull request
without a worker, but only for ONE contributor that pushed a branch: it
creates `swarm/<fix>` at that implementer's recorded tip and nothing else.
#978's run had two implementers, and the one the integrator builds on (the
last in plan order) ended `no_change` -- it pushed nothing, and the other
one's work exists only on its own branch. Publishing the `builds_on` tip
would publish nothing; publishing the other's would drop the merge the
worker does. So the step is DECLINED: nothing published, no branch created,
the decline marker written, and the parent's wake rung so the scheduler
admits it to the worker, which merges every contributor that changed
something and opens the one pull request.

Both guards are held: `contributors` (it merges more than the one it builds
on) and, behind it, `implementer_no_change`, should the dispatch block ever
name only the `builds_on` implementer.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

import pytest

from swarm_api import verdictpublish
from swarm_common.states import ParkReason, TaskState

from .test_verdict_publish import (  # noqa: F401  (fixtures, imported to be used)
    FIX,
    HEAD,
    IMPL,
    REVIEW,
    _finished,
    _fix,
    _push,
    _sign,
    _workflow,
    _writes,
    client,
    contract,
    github,
    store,
    waker,
)

#: The implementer that DID change something: the first in plan order.
OTHER = "task_impl000002"
OTHER_HEAD = "e" * 40


@pytest.mark.parametrize("integrates,code", [
    ([OTHER, IMPL], "contributors"),
    ([IMPL], "implementer_no_change"),
], ids=["as_compiled", "builds_on_only"])
def test_an_integrator_building_on_a_no_change_implementer_is_declined_to_the_worker(
    db, objects, client, github, waker, contract, integrates, code
):
    fix = _workflow(db, objects, integrates=integrates)
    # The implementer the integrator builds on changed nothing and pushed nothing.
    implementer = db.docs[f"tasks/{IMPL}"]
    implementer["result_summary"] = {
        "no_change": True,
        "git": {"patch_cause": "empty_diff", "commit_count": 0, "dirty_count": 0},
        "artifacts": [],
    }
    del github.branches[f"swarm/{IMPL}"]
    # The other one pushed its branch: the work that must not be stranded.
    _finished(db, objects, OTHER, files={"pr-title.txt": "Add the sort key\n"},
              git={"branch": f"swarm/{OTHER}", "pushed_head": OTHER_HEAD, "published": True})
    github.branches[f"swarm/{OTHER}"] = OTHER_HEAD
    fix["depends_on"] = [OTHER, IMPL, REVIEW]
    _sign(fix)
    branches_before = dict(github.branches)

    response = _push(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report"]["published"] == [], body
    assert body["report"]["declined"] == [{"task_id": FIX, "code": code}], body
    # Not published: still PARKED for the scheduler, no lease, no summary.
    fix = _fix(db)
    assert fix["state"] == TaskState.PARKED.value
    assert fix["park_reason"] == ParkReason.DEPENDENCY_INCOMPLETE.value
    assert not fix.get("result_summary")
    marker = fix["metadata"][verdictpublish.CONTROL_PUBLISH_METADATA_KEY]
    assert marker["state"] == "declined" and marker["code"] == code
    # No branch created, no pull request opened, nothing written to GitHub.
    assert github.branches == branches_before
    assert f"swarm/{FIX}" not in github.branches
    assert github.pulls == {}
    assert _writes(github) == []
    # The parent's wake: the scheduler admits the step to the worker, now unheld.
    assert waker.rung == [("task_finished",
                           {"task_id": REVIEW, "tenant_id": "eng", "state": "SUCCEEDED"})]
    assert HEAD not in github.branches.values()
