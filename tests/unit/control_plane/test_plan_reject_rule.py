"""Rejecting a run's plan asks who may approve it (PLAN-REJECT-RULE, part of #2417).

BGD 2026-10-26 (~23:40Z, "Yes, in a follow-up lane"): POST
/v1/runs/{id}/plan:reject asks the rule PR 2501 gave the approvals inbox's
`:reject` -- ONLY SOMEONE WHO MAY APPROVE MAY REJECT, and a workspace admin
decides a `second_member` hold alone. The rule is the inbox's own
(`routes.approvals._reject_rule`), behind the same switch
(`REFUSAL_REJECT_APPROVER_REQUIRED`, on by default), answering the same 403
`reject_approver_required`. Off, any member rejects, as before.

No credentials, no network, no emulator.
"""

from __future__ import annotations

from .conftest import auth_header
from .test_approval_decision_fields import (  # noqa: F401  (fixtures)
    REJECT_SWITCH,
    _name_admin,
    api_context,
)
from .test_approvals import (  # noqa: F401  (fixtures)
    _bootstrap_plan,
    _grants,
    _planned,
    built_types,
    client,
    clock,
    group_map,
    pr_files,
    switch_on,
    writes,
)


def _reject(client, run: dict, user="alice"):
    return client.post(f"/v1/runs/{run['id']}/plan:reject", headers=auth_header(user),
                       json={"plan_digest": run["plan_digest"], "reason": "out of scope"})


def _approve(client, run: dict, user="alice"):
    return client.post(f"/v1/runs/{run['id']}/plan:approve", headers=auth_header(user),
                       json={"plan_digest": run["plan_digest"]})


def test_a_member_who_may_approve_a_held_plan_may_reject_it(client, db, objects, switch_on):
    run = _planned(client, db, objects, _bootstrap_plan())
    assert run["approval_hold"]["approvers"] == "second_member"
    response = _reject(client, run, user="dave")
    assert response.status_code == 200, response.text
    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "REJECTED"
    assert db.docs[f"issue_runs/{run['id']}"]["rejected_by"] == "dave@example.com"


def test_a_member_who_may_not_approve_is_refused_with_the_approvals_code(client, db, objects, switch_on):
    run = _planned(client, db, objects, _bootstrap_plan())
    # The approve refuses alice, the run's creator, under a second_member hold...
    assert _approve(client, run).json()["code"] == "hold_approver_required"
    # ...so the reject does, with the inbox's code, words and detail.
    refused = _reject(client, run)
    assert refused.status_code == 403, refused.text
    body = refused.json()
    assert body["code"] == "reject_approver_required"
    assert body["message"].startswith("only someone who may approve this item may reject it: ")
    assert body["detail"]["approve_refusal"] == "hold_approver_required"
    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "PLANNED"


def test_a_workspace_admin_rejects_their_own_second_member_plan_alone(client, db, objects, switch_on):
    _name_admin(client, "alice@example.com")
    run = _planned(client, db, objects, _bootstrap_plan())
    assert run["approval_hold"]["approvers"] == "second_member"
    response = _reject(client, run)
    assert response.status_code == 200, response.text
    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "REJECTED"


def test_with_the_switch_off_any_member_rejects_as_before(client, db, objects, switch_on, monkeypatch):
    monkeypatch.setenv(REJECT_SWITCH, "off")
    run = _planned(client, db, objects, _bootstrap_plan())
    response = _reject(client, run)
    assert response.status_code == 200, response.text
    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "REJECTED"


def test_a_decided_plan_is_still_the_transitions_conflict_not_a_who_refusal(client, db, objects, switch_on):
    run = _planned(client, db, objects, _bootstrap_plan())
    assert _reject(client, run, user="dave").status_code == 200
    again = _reject(client, run)
    assert again.status_code == 409, again.text
