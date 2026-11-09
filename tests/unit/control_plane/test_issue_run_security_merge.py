"""An issue run that asked for the security review merges only on its MERGE too (owner, 2026-11-04).

The run's merge is a merge-only continuation in a workflow of its own, so the
CI loop reads the verdicts for it before submitting: the re-review's
`verdict.json`, and for a run with `security_review` the security review's
`security-verdict.json` (`issueci._security_verdict`). A NOT_YET, or a
verdict that does not read, fails the run with the pull request left open
for a person and submits no merge. GitHub is the CI-loop tests' fake
transport; no credentials, no network, no emulator.
"""

from __future__ import annotations

import copy
import json

import pytest

from swarm_api import issueruns
from swarm_api.validation import SECURITY_VERDICT_FILE

from .test_issue_run_auto_merge import _checking, _merge_workflows
from .test_issue_run_ci import (  # noqa: F401  (fixtures, imported to be used)
    SHA_A,
    _read,
    _step_task,
    api_context,
    clock,
    forge_tokens,
    writes,
)
from .test_issue_runs import BUCKET


def _with_security_review(db, objects, running: dict, verdict: str | None) -> None:
    """The run asked for the review, and its workflow's security review wrote
    `verdict` (None: it wrote no verdict file)."""
    db.docs[f"issue_runs/{running['id']}"]["security_review"] = True
    fix = _step_task(db, running["workflow_id"], issueruns.FIX_STEP)
    task = copy.deepcopy(fix)
    task["id"] = "task_security_review"
    task["result_summary"] = {"artifacts": []}
    if verdict is not None:
        content = json.dumps({"verdict": verdict, "findings": [], "needs_owner": []})
        key = (f"tenants/{task['tenant_id']}/tasks/{task['id']}/attempts/att_1/artifacts/"
               f"{SECURITY_VERDICT_FILE}")
        objects.put(key, content)
        task["result_summary"] = {"artifacts": [{
            "name": SECURITY_VERDICT_FILE, "bytes": len(content.encode()),
            "uri": f"gs://{BUCKET}/{key}",
        }]}
    db.docs[f"tasks/{task['id']}"] = task
    steps = db.docs[f"workflows/{running['workflow_id']}"]["steps"]
    (fix_step,) = [s for s in steps if s["step_id"] == issueruns.FIX_STEP]
    steps.append({**copy.deepcopy(fix_step), "step_id": issueruns.SECURITY_REVIEW_STEP,
                  "task_id": task["id"]})


def test_a_security_review_that_says_merge_lets_the_merge_go(client, db, objects, writes, clock):
    running = _checking(client, db, objects, writes)
    _with_security_review(db, objects, running, "MERGE")
    writes.check(SHA_A, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING", run.get("error")
    assert len(_merge_workflows(db)) == 1


@pytest.mark.parametrize("verdict, words", [
    ("NOT_YET", "the security review's verdict is NOT_YET, not MERGE"),
    (None, f"wrote no {SECURITY_VERDICT_FILE}"),
])
def test_a_security_review_that_does_not_say_merge_fails_the_merge_and_submits_none(
        client, db, objects, writes, clock, verdict, words):
    """MUTATION: delete the `if run.security_review:` block in `issueci._merge`
    and the run submits its merge over a NOT_YET security review."""
    running = _checking(client, db, objects, writes)
    _with_security_review(db, objects, running, verdict)
    writes.check(SHA_A, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert words in run["error"]
    assert _merge_workflows(db) == []
