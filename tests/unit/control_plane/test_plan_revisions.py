"""Plan revisions, the planner's rationale, and the digest that stays honest
(docs/design/plans-pipeline.md §3.3, §3.6, §4; lane P2 of §11).

  1. EVERY STORED PLAN FIXTURE KEEPS ITS DIGEST. The plans this repository's
     tests store, read back with `stored=True`, hash to the digests recorded
     here from `main` before `rationale` existed: an old run's `plan_digest`
     and `approved_digest` still verify.
  2. `rationale` (plan and step) is INSIDE the digest: the same plan with a
     reason digests differently from the plan without one.
  3. The planner's plan writes revision 1, `plan:edit` writes revision N+1
     with `replaces`, in the transaction that moves the run's plan; a failed
     transaction writes neither.
  4. A revision whose plan does not hash to its digest is served flagged
     (`verified: false`), never as valid.
  5. An old run, with no revisions kept, serves one synthesised revision with
     `history_complete: false`.
  6. `GET /v1/runs/{id}/plan-revisions/{n}` is the caller's tenant's only:
     another tenant's run answers 404, the same 404 as no run.
"""

from __future__ import annotations

import hashlib
import importlib
import json

import pytest

from swarm_api import issueruns
from swarm_api.issueruns import (
    PLAN_REVISIONS_COLLECTION,
    RUNS_COLLECTION,
    InvalidPlan,
    IssueRuns,
    PlanRevisionWrite,
    RunState,
    parse_plan,
    plan_digest,
)

from .conftest import auth_header
from .test_issue_runs import (  # noqa: F401 -- fixtures, used by name
    PLAN,
    _edit,
    _planned,
    _run,
    api_context,
    forge_tokens,
    github,
)


def _revisions(client, run_id, n, user="alice"):
    return client.get(f"/v1/runs/{run_id}/plan-revisions/{n}", headers=auth_header(user))


def _stored(db, run_id) -> dict:
    prefix = f"{RUNS_COLLECTION}/{run_id}/{PLAN_REVISIONS_COLLECTION}/"
    return {int(k[len(prefix):]): v for k, v in db.docs.items() if k.startswith(prefix)}


# --------------------------------------------------------------------------
# 1. every stored plan fixture keeps its digest
# --------------------------------------------------------------------------

#: (test module, constant, the digest `main` gave it before `rationale`).
#: Computed once from `plan_digest(parse_plan(plan, stored=True))` on main and
#: written down here, so a field defaulted into the dump would change them.
STORED_DIGESTS = [
    ("test_issue_runs", "PLAN",
     "sha256:f62179d9a131ef7824c6b7e7e3365c84f545db43da59893dce374968f61bfb3c"),
    ("test_issue_runs", "STAGED_PLAN",
     "sha256:dec7226c4b5a9a6746cf42c33b291d87f7d491ab03114a59dcb496e440080910"),
    ("test_issue_runs", "FULL_PLAN",
     "sha256:d8aeb36a8e93d4af3e035f911b96f7274859540d40648c739c29d2928944fca7"),
    ("test_issue_runs", "LEGACY_PLAN",
     "sha256:97f8815b2fa742dd8bc6108b0c22634e3e9f6134f60b3eb6d4b08f614f43c62a"),
    ("test_issue_run_keyword", "STAGED_FULL_PLAN",
     "sha256:aa83d5eb18ab7c9e96cc14517030d992b496b8fa036addb11111a55ecd40ec75"),
    ("test_review_gate_default", "PLAN",
     "sha256:c53f609fa9c869a05dd62806558db13a7628ff4dd018122169d41b0de69b965f"),
    ("test_review_context", "PLAN",
     "sha256:12fa7380702653a16a7d98b99861cbb303a85be7f36a4eb5c52ebb72b800bbdc"),
    ("test_issue_run_holds", "BOOTSTRAP_PLAN",
     "sha256:250c3a4e50c78848936cfa69feb01aca3962578c2e37ff35d57c271137a0ac59"),
    ("test_issue_run_holds", "WORKFLOW_PLAN",
     "sha256:bba430f85332b4efefa51156687b112f99b3d05dc73266cd9c03445742baeacf"),
    ("test_issue_run_holds", "PLAIN_PLAN",
     "sha256:7d95827685c1505995a1d3b0343a7962c2d3f5096d3ad36abd3e83779668f894"),
    ("test_review_fix_outside_context", "WINDOW_PLAN",
     "sha256:3ebd9eb578c579456288c1d5f421c8e70793f9c8e030c7d5eca2be1609532d6e"),
    ("test_workflows_path_hold", "WORKFLOW_PLAN",
     "sha256:bba430f85332b4efefa51156687b112f99b3d05dc73266cd9c03445742baeacf"),
]


@pytest.mark.parametrize("module,name,digest", STORED_DIGESTS,
                         ids=[f"{m}.{n}" for m, n, _ in STORED_DIGESTS])
def test_every_stored_plan_fixture_hashes_to_its_stored_digest(module, name, digest):
    plan = getattr(importlib.import_module(f"{__package__}.{module}"), name)
    read = parse_plan(plan, stored=True)
    assert plan_digest(read) == digest
    # The canonical bytes themselves, not only through `plan_digest`.
    canonical = json.dumps(read, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest() == digest
    assert "rationale" not in json.dumps(read)


# --------------------------------------------------------------------------
# 2. rationale is inside the digest
# --------------------------------------------------------------------------

def _reasoned(plan_reason: str | None = None, step_reason: str | None = None) -> dict:
    plan = json.loads(json.dumps(PLAN))
    if plan_reason is not None:
        plan["rationale"] = plan_reason
    if step_reason is not None:
        plan["steps"][0]["rationale"] = step_reason
    return plan


def test_a_plan_with_a_rationale_digests_differently_from_the_same_plan_without():
    bare = plan_digest(parse_plan(PLAN))
    with_plan_reason = parse_plan(_reasoned("The list is read by name first."))
    with_step_reason = parse_plan(_reasoned(step_reason="The key is what the header calls."))
    assert with_plan_reason["rationale"] == "The list is read by name first."
    assert with_step_reason["steps"][0]["rationale"] == "The key is what the header calls."
    assert "rationale" not in with_step_reason["steps"][1]
    assert len({bare, plan_digest(with_plan_reason), plan_digest(with_step_reason)}) == 3


@pytest.mark.parametrize("plan", [
    _reasoned("x" * 2_001), _reasoned(step_reason="x" * 1_001), _reasoned(""),
    _reasoned(step_reason="   "),
], ids=["plan-too-long", "step-too-long", "plan-empty", "step-blank"])
def test_a_rationale_out_of_its_bounds_is_refused(plan):
    with pytest.raises(InvalidPlan, match="rationale"):
        parse_plan(plan)


def test_a_rationale_at_its_bounds_is_kept():
    plan = parse_plan(_reasoned("x" * 2_000, "y" * 1_000))
    assert len(plan["rationale"]) == 2_000 and len(plan["steps"][0]["rationale"]) == 1_000


# --------------------------------------------------------------------------
# 3. the planner writes revision 1, an edit N+1, in the same transaction
# --------------------------------------------------------------------------

def test_the_planners_plan_writes_revision_one(client, db, objects):
    run = _planned(client, db, objects)
    stored = _stored(db, run["id"])
    assert list(stored) == [1]
    first = stored[1]
    assert first["tenant_id"] == "eng" and first["run_id"] == run["id"]
    assert first["revision"] == 1 == run["plan_revision"]
    assert first["plan"] == run["plan"] == PLAN
    assert first["digest"] == run["plan_digest"] == plan_digest(PLAN)
    assert first["replaces"] is None
    assert first["author"] == {"kind": "planner", "by": "planner"}
    assert first["made_by"] == "plan"
    assert first["answers"] == [] and first["changed"] == []
    assert first["at"] is not None


def test_an_edit_writes_revision_n_plus_one_that_replaces_the_old_digest(client, db, objects):
    run = _planned(client, db, objects)
    edited = json.loads(json.dumps(PLAN))
    edited["steps"][1]["prompt"] = "Make the Name header toggle the sort, both ways."
    edited["steps"][1]["rationale"] = "The header is where people look for the sort."
    answer = _edit(client, run["id"], run["plan_digest"], edited)
    assert answer.status_code == 200, answer.text
    after = answer.json()["run"]
    stored = _stored(db, run["id"])
    assert sorted(stored) == [1, 2]
    second = stored[2]
    assert second["revision"] == 2 == after["plan_revision"]
    assert second["digest"] == after["plan_digest"] == plan_digest(edited)
    assert second["replaces"] == run["plan_digest"] == stored[1]["digest"]
    assert second["author"] == {"kind": "person", "by": "alice@example.com"}
    assert second["made_by"] == "edit"
    assert {"path": "steps.1.prompt", "op": "replace"} in second["changed"]
    assert {"path": "steps.1.rationale", "op": "add"} in second["changed"]
    assert len(second["changed"]) == 2
    # The first revision is kept as it was.
    assert stored[1]["plan"] == PLAN


def test_a_stale_edit_writes_no_revision(client, db, objects):
    run = _planned(client, db, objects)
    edited = json.loads(json.dumps(PLAN))
    edited["summary"] = "Sort the widgets."
    assert _edit(client, run["id"], run["plan_digest"], edited).status_code == 200
    again = _edit(client, run["id"], run["plan_digest"], PLAN)
    assert again.status_code == 409
    assert sorted(_stored(db, run["id"])) == [1, 2]


def test_a_failed_transaction_writes_neither_the_plan_nor_the_revision(client, db, objects):
    run = _planned(client, db, objects)
    before = dict(db.docs[f"{RUNS_COLLECTION}/{run['id']}"])
    edited = json.loads(json.dumps(PLAN))
    edited["summary"] = "Sort the widgets."

    class Refused(Exception):
        pass

    def refusing_signer(_read, _written):
        raise Refused("the signature could not be made")

    with pytest.raises(Refused):
        IssueRuns(db).transition(
            "eng", run["id"], RunState.PLANNED, by="alice@example.com",
            patch=lambda latest: {"plan": edited, "plan_digest": plan_digest(edited),
                                  "plan_revision": latest.plan_revision + 1,
                                  "plan_edited_by": "alice@example.com"},
            sign=refusing_signer,
            revision=PlanRevisionWrite(author_kind="person", by="alice@example.com",
                                       made_by="edit"),
        )
    assert db.docs[f"{RUNS_COLLECTION}/{run['id']}"]["plan"] == before["plan"] == PLAN
    assert sorted(_stored(db, run["id"])) == [1]


def test_a_transition_that_moves_no_plan_writes_no_revision(client, db, objects):
    run = _planned(client, db, objects)
    assert client.post(f"/v1/runs/{run['id']}/plan:reject", headers=auth_header("alice"),
                       json={"plan_digest": run["plan_digest"]}).status_code == 200
    assert sorted(_stored(db, run["id"])) == [1]


def test_a_revision_write_without_a_plan_is_refused_before_anything_is_written(
        client, db, objects):
    run = _planned(client, db, objects)
    with pytest.raises(ValueError, match="plan"):
        IssueRuns(db).transition(
            "eng", run["id"], RunState.CANCELLED, by="alice@example.com",
            patch={"plan": None},
            revision=PlanRevisionWrite(author_kind="person", by="alice@example.com",
                                       made_by="edit"),
        )
    assert db.docs[f"{RUNS_COLLECTION}/{run['id']}"]["state"] == "PLANNED"


# --------------------------------------------------------------------------
# what the run read serves
# --------------------------------------------------------------------------

def test_the_run_serves_every_revision_newest_first_without_its_plan(client, db, objects):
    run = _planned(client, db, objects)
    edited = json.loads(json.dumps(PLAN))
    edited["summary"] = "Sort the widgets by name."
    assert _edit(client, run["id"], run["plan_digest"], edited).status_code == 200
    served = _run(client, run["id"]).json()["run"]
    revisions = served["plan_revisions"]
    assert [r["revision"] for r in revisions] == [2, 1]
    assert all("plan" not in r for r in revisions)
    assert all(r["verified"] is True for r in revisions)
    assert served["plan_history_complete"] is True
    assert revisions[0]["replaces"] == revisions[1]["digest"]
    assert set(revisions[0]) == {"revision", "digest", "replaces", "author", "made_by",
                                 "answers", "changed", "cost", "at", "verified"}


def test_one_revision_is_served_whole(client, db, objects):
    run = _planned(client, db, objects)
    edited = json.loads(json.dumps(PLAN))
    edited["summary"] = "Sort the widgets by name."
    assert _edit(client, run["id"], run["plan_digest"], edited).status_code == 200
    first = _revisions(client, run["id"], 1)
    assert first.status_code == 200, first.text
    body = first.json()["revision"]
    assert body["plan"] == PLAN and body["revision"] == 1 and body["verified"] is True
    assert _revisions(client, run["id"], 2).json()["revision"]["plan"] == edited
    assert _revisions(client, run["id"], 3).status_code == 404


# --------------------------------------------------------------------------
# 4. a revision whose plan does not hash to its digest is flagged
# --------------------------------------------------------------------------

def test_a_revision_whose_plan_does_not_hash_to_its_digest_is_served_flagged(
        client, db, objects):
    run = _planned(client, db, objects)
    key = f"{RUNS_COLLECTION}/{run['id']}/{PLAN_REVISIONS_COLLECTION}/1"
    db.docs[key]["plan"] = {**PLAN, "summary": "Something nobody approved."}
    served = _run(client, run["id"]).json()["run"]
    assert served["plan_revisions"][0]["verified"] is False
    whole = _revisions(client, run["id"], 1).json()["revision"]
    assert whole["verified"] is False
    # The run's own plan, which is what is approved, is untouched.
    assert served["plan"] == PLAN and served["plan_digest"] == plan_digest(PLAN)


def test_a_revision_document_naming_another_tenant_is_not_served(client, db, objects):
    run = _planned(client, db, objects)
    key = f"{RUNS_COLLECTION}/{run['id']}/{PLAN_REVISIONS_COLLECTION}/1"
    db.docs[key]["tenant_id"] = "research"
    db.docs[key]["plan"] = {**PLAN, "summary": "Another tenant's plan."}
    # Read as not kept: the run's own plan is served, synthesised from it.
    whole = _revisions(client, run["id"], 1).json()["revision"]
    assert whole["plan"] == PLAN and whole["history_complete"] is False
    served = _run(client, run["id"]).json()["run"]
    assert [r.get("history_complete") for r in served["plan_revisions"]] == [False]
    assert "Another tenant" not in json.dumps(served)


# --------------------------------------------------------------------------
# 5. an old run serves one synthesised revision
# --------------------------------------------------------------------------

def _forget_revisions(db, run_id) -> None:
    for key in [k for k in db.docs if k.startswith(f"{RUNS_COLLECTION}/{run_id}/")]:
        del db.docs[key]


def test_an_old_run_serves_one_synthesised_revision_with_history_incomplete(
        client, db, objects):
    run = _planned(client, db, objects)
    _forget_revisions(db, run["id"])
    served = _run(client, run["id"]).json()["run"]
    assert served["plan_history_complete"] is False
    [only] = served["plan_revisions"]
    assert only["revision"] == 1 and only["digest"] == run["plan_digest"]
    assert only["author"] == {"kind": "planner", "by": "planner"}
    assert only["history_complete"] is False and only["verified"] is True
    whole = _revisions(client, run["id"], 1).json()["revision"]
    assert whole["plan"] == PLAN and whole["history_complete"] is False
    assert _revisions(client, run["id"], 2).status_code == 404


def test_an_old_edited_run_names_its_editor_and_says_the_earlier_ones_were_not_kept(
        client, db, objects):
    run = _planned(client, db, objects)
    edited = json.loads(json.dumps(PLAN))
    edited["summary"] = "Sort the widgets by name."
    assert _edit(client, run["id"], run["plan_digest"], edited).status_code == 200
    _forget_revisions(db, run["id"])
    [only] = _run(client, run["id"]).json()["run"]["plan_revisions"]
    assert only["revision"] == 2
    assert only["author"] == {"kind": "person", "by": "alice@example.com"}
    assert only["made_by"] == "edit" and only["history_complete"] is False


def test_an_edit_of_an_old_run_keeps_the_new_revision_and_says_history_is_incomplete(
        client, db, objects):
    run = _planned(client, db, objects)
    _forget_revisions(db, run["id"])
    edited = json.loads(json.dumps(PLAN))
    edited["summary"] = "Sort the widgets by name."
    assert _edit(client, run["id"], run["plan_digest"], edited).status_code == 200
    served = _run(client, run["id"]).json()["run"]
    assert [r["revision"] for r in served["plan_revisions"]] == [2]
    assert served["plan_revisions"][0]["replaces"] == run["plan_digest"]
    assert served["plan_history_complete"] is False


def test_a_run_without_a_plan_has_no_revisions(client, db, objects):
    created = client.post("/v1/runs", headers=auth_header("alice"),
                          json={"issue": "example-org/widgets#42"})
    served = _run(client, created.json()["run"]["id"]).json()["run"]
    assert served["plan_revisions"] == [] and served["plan_history_complete"] is True
    assert _revisions(client, served["id"], 1).status_code == 404


# --------------------------------------------------------------------------
# 6. the tenant boundary
# --------------------------------------------------------------------------

def test_another_tenants_plan_revision_is_404_like_no_run(client, db, objects):
    run = _planned(client, db, objects, user="alice")
    theirs = _revisions(client, run["id"], 1, user="bob")
    missing = _revisions(client, "run_doesnotexist", 1, user="bob")
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json()["message"].replace(run["id"], "X") == missing.json()["message"].replace(
        "run_doesnotexist", "X")


@pytest.mark.parametrize("revision", ["0", "-1", "x", "99999999999"])
def test_a_revision_number_out_of_range_is_refused_or_missing(client, db, objects, revision):
    run = _planned(client, db, objects)
    assert _revisions(client, run["id"], revision).status_code in (404, 422)


# --------------------------------------------------------------------------
# the planner is asked for its reasons
# --------------------------------------------------------------------------

def test_the_planner_is_asked_for_a_rationale_for_the_plan_and_each_step():
    shape = issueruns._PLAN_SHAPE
    assert shape.count('"rationale"') == 2
    # The model-class rewrite still finds the step's last line.
    assert issueruns._PLAN_SHAPE_LAST in shape
