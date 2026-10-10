"""The re-review runs only after a fix, and `merge_policy` "inherit" (merge chain follow-up, part of #295).

Owner decisions of 2026-10-10, following PR 998:

  1. The re-review `metadata.merge` "on_merge_verdict" appends is GATED on
     the first review, on the same verdicts as the integrator (NOT_YET in
     implement -> review -> fix). On MERGE its agent does not run, and the
     worker passes the first review's verdict on as its artifact
     (tests/unit/worker/test_shut_gate_passes_the_verdict_on.py), so the
     merge keeps reading the re-review's verdict file. Under `integrate` the
     re-review is the one step besides the integrator that may be gated; a
     caller's own gated non-integrator is still refused.
  2. A gated step may be staged from, but only for the verdict file it
     staged, the one file a shut gate still publishes.
  3. PATCH `merge_policy: "inherit"` clears a repository's override back to
     the platform default (stored null): a platform admin's only, audited as
     every other value is.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import pytest

from swarm_api.validation import DagError, StepSpec, rereview_step_for, validate_step_routing

from .conftest import auth_header, seed_tenant
from .test_merge_verdict_workflows import _by_id, _post, _review_shape, _task
from .test_repository_merge_policy import (  # noqa: F401  (fixtures)
    ADMIN,
    MEMBER,
    _audit,
    _patch,
    _register,
    commits,
    make,
)


# --------------------------------------------------------------------------
# 1: the re-review is gated like the integrator
# --------------------------------------------------------------------------

def test_the_rereview_is_gated_on_the_first_reviews_not_yet(client, db):
    response = _post(client, _review_shape(metadata={"merge": "on_merge_verdict"}))
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())

    block = _task(db, steps["re-review"]["task_id"])["metadata"]["dispatch"]
    assert block["verdict_gate"] == {
        "task_id": steps["review"]["task_id"],
        "verdict_in": ["NOT_YET"],
    }
    # It stages the verdict its gate reads, and the merge still reads the
    # re-review's verdict file -- the re-review's own, or the first review's
    # passed on.
    assert steps["re-review"]["input_from"]["review"] == "verdict.json"
    assert steps["merge"]["input_from"] == {"re-review": "verdict.json"}
    # Not the publisher: no pull request role, and the scheduler does not hold
    # it for swarm-api's MERGE publish (`held_by_scheduler`).
    assert block["role"] == "contributor"


def test_the_rereview_gate_mirrors_the_integrators_verdicts():
    """The re-review runs exactly when the fix's agent did."""
    review = {"step_id": "review", "runner_profile": "mock", "depends_on": ["implement"],
              "input_from": {"implement": "swarm-work.patch"}, "input": {"prompt": "review"}}
    for verdicts in (("NOT_YET",), ("MERGE", "NOT_YET")):
        specs = [
            StepSpec("implement", ()),
            StepSpec("review", ("implement",), input_from={"implement": "swarm-work.patch"}),
            StepSpec("fix", ("review",), input_from={"review": "verdict.json"},
                     when_step="review", when_verdicts=verdicts, builds_on="implement"),
        ]
        step = rereview_step_for(specs, review, "integrate")
        assert step is not None
        assert step["when"] == {"step": "review", "verdict_in": list(verdicts)}


def test_the_rereview_files_no_minors_and_takes_no_pull_request_label(client, db):
    """The integrator reads the same verdict and files its minors once; the
    re-review, gated on it too, must not file them a second time."""
    seed_tenant(db, "eng")
    assert client.put("/v1/admin/tenants/eng/findings-epic", headers=auth_header("root"),
                      json={"findings_epic": 638}).status_code == 200
    response = _post(client, _review_shape(metadata={"merge": "on_merge_verdict",
                                                     "title": "the widget fix"}))
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    fix = _task(db, steps["fix"]["task_id"])["metadata"]["dispatch"]
    rereview = _task(db, steps["re-review"]["task_id"])["metadata"]["dispatch"]
    assert fix.get("findings_epic") == 638
    assert "findings_epic" not in rereview
    assert "pr_label" not in rereview


def test_a_callers_own_gated_non_integrator_is_still_refused(client, db):
    spec = _review_shape()
    spec["steps"].append({"step_id": "ship", "runner_profile": "mock", "depends_on": ["fix"]})
    response = _post(client, spec)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["step_id"] == "fix"


# --------------------------------------------------------------------------
# 2: a gated step is staged from only for its verdict
# --------------------------------------------------------------------------

def _gated_then(filename: str) -> list[StepSpec]:
    return [
        StepSpec("review", ()),
        StepSpec("fix", ("review",), input_from={"review": "verdict.json"},
                 when_step="review", when_verdicts=("NOT_YET",)),
        StepSpec("after", ("fix",), input_from={"fix": filename}),
    ]


def test_a_gated_step_may_be_staged_from_for_the_verdict_it_passes_on():
    validate_step_routing(_gated_then("verdict.json"), strategy="collect",
                          integrator_step_id=None)


def test_a_gated_step_is_still_refused_as_the_source_of_any_other_file():
    with pytest.raises(DagError) as refused:
        validate_step_routing(_gated_then("notes.md"), strategy="collect",
                              integrator_step_id=None)
    assert refused.value.detail["staged_by"] == ["after"]
    assert refused.value.detail["passed_on"] == "verdict.json"


# --------------------------------------------------------------------------
# 3: merge_policy "inherit"
# --------------------------------------------------------------------------

def test_inherit_clears_the_override_to_null_and_audits_it(make, db, commits):  # noqa: F811
    client, _ = make()
    record = _register(client).json()["repository"]
    path = f"repositories/{record['repo_id']}"
    assert _patch(client, record["repo_id"], {"merge_policy": "on_merge_verdict"},
                  user=ADMIN).status_code == 200
    commits.clear()

    response = _patch(client, record["repo_id"], {"merge_policy": "inherit"}, user=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json()["repository"]["merge_policy"] is None
    assert db.docs[path]["merge_policy"] is None
    cleared = _audit(db)[-1]
    assert cleared["action"] == "repository_merge_policy_set"
    assert cleared["by"] == "root@saga.xyz"
    assert cleared["detail"]["merge_policy"] == {"from": "on_merge_verdict", "to": None}
    writing = [paths for paths in commits if path in paths]
    assert len(writing) == 1
    assert sorted(p.split("/")[0] for p in writing[0]) == ["admin_audit", "repositories"]

    # Nothing left to clear: no change, no second audit entry.
    assert _patch(client, record["repo_id"], {"merge_policy": "inherit"},
                  user=ADMIN).status_code == 200
    assert len(_audit(db)) == 2
    got = client.get(f"/v1/repositories/{record['repo_id']}", headers=auth_header(MEMBER))
    assert got.json()["repository"]["merge_policy"] is None


def test_a_member_cannot_set_inherit(make, db):
    client, _ = make()
    record = _register(client).json()["repository"]
    assert _patch(client, record["repo_id"], {"merge_policy": "off"}, user=ADMIN).status_code == 200
    before = dict(db.docs[f"repositories/{record['repo_id']}"])
    audited = len(_audit(db))

    response = _patch(client, record["repo_id"], {"merge_policy": "inherit"})
    assert response.status_code == 403, response.text
    assert db.docs[f"repositories/{record['repo_id']}"] == before
    assert len(_audit(db)) == audited


def test_a_create_does_not_take_inherit(make, db):
    client, _ = make()
    response = _register(client, user=ADMIN, merge_policy="inherit")
    assert response.status_code == 422, response.text
    assert _audit(db) == []
