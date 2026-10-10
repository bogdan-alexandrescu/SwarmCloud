"""An issue run whose workflow changed something but opened no pull request fails NAMING WHY (#978).

wf_ca1807e43ac64d6b8afd (2026-10-09): two implement steps, the first pushed
`swarm/<its id>`, the second -- the one the review and the integrator
`builds_on` -- changed nothing. The worker skipped the integrator in 0.4 s
(`result_summary.skipped`, no `git.publish_reason`), the workflow SUCCEEDED,
and the run ended FAILED with `... opened no pull request` and nothing after
it: no skip, no upstream, no word of the first step's pushed branch. The
worker no longer skips that integrator; this holds that the control plane
never fails such a run silently whatever the worker does. What is held:

  1. A SKIPPED INTEGRATOR beside a build that changed something: FAILED,
     naming the skip's reason and upstream, every build step that changed
     something with its task id and pushed branch -- the stranded work, and
     where it is -- and every build step that left nothing, and how.
  2. AN INTEGRATOR THAT RAN and opened nothing: FAILED, naming its
     `git.publish_reason`, and the same build steps.
  3. NOTHING CHANGED ANYWHERE: DONE `already_on_main`, exactly as #646 left
     it, with no error.

No credentials, no network, no emulator.
"""

from __future__ import annotations

from swarm_api import issueruns

from .test_issue_run_already_on_main import _running, _table, _workflow_changed_nothing
from .test_issue_run_ci import (  # noqa: F401  (fixtures, imported to be used)
    _read,
    _step_task,
    _stored,
    api_context,
    clock,
    forge_tokens,
    writes,
)

#: FULL_PLAN's build steps, in plan order: the integrator `builds_on` the last.
CHANGED_STEP = "impl-sort-key"
UNCHANGED_STEP = "impl-ui"


def _first_changed_last_did_not(db, objects, workflow_id: str) -> tuple[dict, dict, dict]:
    """#978's shape: the first build pushed its branch, the last changed nothing."""
    _workflow_changed_nothing(db, objects, workflow_id, {UNCHANGED_STEP: _table(True, True)})
    changed = _step_task(db, workflow_id, CHANGED_STEP)
    changed["result_summary"] = {
        "git": {
            "commit_count": 1, "published": True,
            "branch": f"swarm/{changed['id']}", "pushed_head": "a" * 40,
        },
        "branch": {"name": f"swarm/{changed['id']}", "head": "a" * 40},
    }
    unchanged = _step_task(db, workflow_id, UNCHANGED_STEP)
    integrator = _step_task(db, workflow_id, issueruns.FIX_STEP)
    return changed, unchanged, integrator


def test_a_skipped_integrator_beside_a_changed_build_fails_naming_the_skip_and_the_stranded_branch(
    client, db, objects, writes, clock
):
    running = _running(client, db, objects)
    changed, unchanged, integrator = _first_changed_last_did_not(
        db, objects, running["workflow_id"]
    )
    # Exactly what the worker's `_finish_nothing_to_change` records: no git.
    integrator["result_summary"] = {
        "skipped": {"reason": "nothing to change", "upstream": [unchanged["id"]]},
    }

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED" and run["outcome"] is None
    error = run["error"]
    assert f"its integrator {integrator['id']} opened no pull request" in error
    # The skip, and what it was skipped behind.
    assert "skipped (nothing to change)" in error
    assert f"behind {unchanged['id']}" in error
    # Whose work is stranded, and where.
    assert f"changed: {CHANGED_STEP} {changed['id']} (branch swarm/{changed['id']})" in error
    # And who left nothing.
    assert f"left nothing: {UNCHANGED_STEP} {unchanged['id']} (no_change)" in error
    assert _stored(db, running["id"])["pr_task_id"] == integrator["id"]
    # Not the already_on_main answer: something did change.
    assert _stored(db, running["id"]).get("verification") is None


def test_an_integrator_that_ran_and_opened_nothing_fails_naming_its_publish_reason(
    client, db, objects, writes, clock
):
    running = _running(client, db, objects)
    changed, unchanged, integrator = _first_changed_last_did_not(
        db, objects, running["workflow_id"]
    )
    integrator["result_summary"] = {
        "git": {"published": False, "publish_reason": "the push was refused"},
    }

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED" and run["outcome"] is None
    error = run["error"]
    assert f"its integrator {integrator['id']} opened no pull request: it ran: " \
           "the push was refused" in error
    assert f"changed: {CHANGED_STEP} {changed['id']} (branch swarm/{changed['id']})" in error
    assert f"left nothing: {UNCHANGED_STEP} {unchanged['id']} (no_change)" in error


def test_an_integrator_that_ran_and_recorded_no_reason_says_so(
    client, db, objects, writes, clock
):
    running = _running(client, db, objects)
    _, _, integrator = _first_changed_last_did_not(db, objects, running["workflow_id"])
    integrator["result_summary"] = {"git": {"published": False}}

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert "it ran and recorded no publish reason" in run["error"]


def test_nothing_changed_is_still_already_on_main(client, db, objects, writes, clock):
    running = _running(client, db, objects)
    _workflow_changed_nothing(db, objects, running["workflow_id"], {
        CHANGED_STEP: _table(True, True), UNCHANGED_STEP: _table(True, True),
    })

    run = _read(client, clock, running["id"])

    assert run["state"] == "DONE"
    assert run["outcome"] == issueruns.OUTCOME_ALREADY_ON_MAIN
    assert run["error"] is None and run["pull_request"] is None
    assert run["requirements_met"] is True
