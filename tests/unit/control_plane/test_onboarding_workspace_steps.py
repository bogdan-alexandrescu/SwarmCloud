"""The `workspace` and `claude_account` onboarding steps (docs/workspaces.md
§6.1; #847, lane W1).

They are served always, from the person's own workspace record, and hold
the checklist back only while WORKSPACE_GATE is on and the gate judges the
caller's tenant. With the gate off, as it ships, `next_step` and `complete`
are exactly what they were.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_api.onboarding import Caller, derive

from .conftest import auth_header

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
CAROL = Caller(email="carol@saga.xyz", tenant_id="u-carol")


def _derive(*, workspace=None, accounts=None, loan=None, required=False, caller=CAROL) -> dict:
    return derive(caller, records=[], pair_docs=[], registrations=[], tenant_lists_git=False,
                  now=T0, workspace=workspace, accounts=accounts, loan=loan,
                  workspace_required=required)


def _step(view: dict, name: str) -> dict:
    return next(s for s in view["steps"] if s["step"] == name)


def _record(state: str, **fields) -> dict:
    return {"tenant_id": "u-carol", "workspace_id": "w-3f9a2c", "state": state,
            "request_id": "req-1", "requested_at": T0, **fields}


@pytest.mark.parametrize("state, shown", [
    (None, "todo"),
    ("requested", "in_progress"),
    ("approved", "in_progress"),
    ("applying", "in_progress"),
    ("needs_owner", "in_progress"),
    ("denied", "failed"),
    ("failed", "failed"),
    ("ready", "done"),
])
def test_the_workspace_step_follows_the_record(state, shown) -> None:
    view = _derive(workspace=None if state is None else _record(state))
    step = _step(view, "workspace")
    assert step["state"] == shown
    assert step["evidence"]["state"] == (state or "none")
    if state is not None:
        assert step["evidence"]["workspace_id"] == "w-3f9a2c"


def test_a_denial_shows_its_reason_and_when_to_ask_again() -> None:
    record = _record("denied", decision={"verdict": "denied", "reason": "Use eng.",
                                         "at": T0, "by": "root@saga.xyz"})
    step = _step(_derive(workspace=record), "workspace")
    assert step["copy"] == "Not approved: Use eng."
    assert step["evidence"]["request_again_at"] == (T0 + timedelta(hours=24)).isoformat()
    assert "root@saga.xyz" not in str(step)


def test_a_failure_shows_its_code_and_its_copy() -> None:
    record = _record("failed", failure={"step": "A7", "code": "CLUSTER_UNREACHABLE",
                                        "retryable": True, "at": T0})
    step = _step(_derive(workspace=record), "workspace")
    assert step["code"] == "CLUSTER_UNREACHABLE"
    assert step["copy"].startswith("Your workspace is made except its Kubernetes namespace")


def test_another_persons_record_is_never_shown() -> None:
    other = {**_record("ready"), "tenant_id": "u-dave"}
    assert _step(_derive(workspace=other), "workspace")["state"] == "todo"


@pytest.mark.parametrize("accounts, loan, shown", [
    ({}, None, "todo"),
    ({"own": 0, "lent": 0, "provider_key": False, "has_account": False},
     {"tenant_id": "u-carol", "state": "requested"}, "in_progress"),
    ({"own": 1, "lent": 0, "provider_key": False, "has_account": True}, None, "done"),
    ({"own": 0, "lent": 1, "provider_key": False, "has_account": True}, None, "done"),
    ({"own": 0, "lent": 0, "provider_key": True, "has_account": True}, None, "done"),
])
def test_the_claude_account_step_follows_the_gates_rule(accounts, loan, shown) -> None:
    step = _step(_derive(accounts=accounts, loan=loan), "claude_account")
    assert step["state"] == shown


def test_not_required_the_steps_hold_nothing_back() -> None:
    view = _derive(required=False)
    assert _step(view, "workspace")["required"] is False
    assert _step(view, "claude_account")["required"] is False
    # As before W1: the first step that is not done is GitHub.
    assert view["next_step"] == "github_connected"
    assert _step(view, "ready")["evidence"]["waiting_for"] == "github_connected"


def test_required_the_workspace_comes_first_then_the_account() -> None:
    view = _derive(required=True)
    assert _step(view, "workspace")["required"] is True
    assert view["next_step"] == "workspace"
    assert _step(view, "ready")["evidence"]["waiting_for"] == "workspace"
    ready = _derive(required=True, workspace=_record("ready"))
    assert ready["next_step"] == "claude_account"
    served = _derive(required=True, workspace=_record("ready"),
                     accounts={"own": 1, "has_account": True})
    assert served["next_step"] == "github_connected"


# -- the route: required only with the gate on, and only for a person's own tenant --


def test_the_route_serves_the_steps_unrequired_with_the_gate_off(client) -> None:
    body = client.get("/v1/onboarding", headers=auth_header("carol")).json()
    assert _step(body, "workspace")["required"] is False
    assert body["next_step"] == "github_connected"


def test_the_route_requires_them_for_a_person_with_the_gate_on(api_context, client, db) -> None:
    api_context.submissions.workspaces.gate = True
    body = client.get("/v1/onboarding", headers=auth_header("carol")).json()
    assert _step(body, "workspace")["required"] is True
    assert body["next_step"] == "workspace"
    # Read-only, even so: no tenant and no record are written by the read.
    assert "tenants/u-carol" not in db.docs and "workspaces/u-carol" not in db.docs


def test_the_route_never_requires_them_in_a_group_tenant(api_context, client) -> None:
    api_context.submissions.workspaces.gate = True
    body = client.get("/v1/onboarding", headers=auth_header("alice")).json()
    assert body["tenant_id"] == "eng"
    assert _step(body, "workspace")["required"] is False
    assert body["next_step"] == "github_connected"


def test_the_route_reads_the_persons_own_workspace_from_a_group_tenant(client, db) -> None:
    db.docs["workspaces/u-alice"] = {"tenant_id": "u-alice", "workspace_id": "w-0a0a0a",
                                     "state": "requested", "request_id": "r"}
    db.docs["accounts/acct"] = {"account_id": "acct", "owner_tenant": "u-alice",
                                "provider": "anthropic", "state": "AVAILABLE", "lend_to": []}
    body = client.get("/v1/onboarding", headers=auth_header("alice")).json()
    assert _step(body, "workspace")["evidence"]["workspace_id"] == "w-0a0a0a"
    assert _step(body, "claude_account")["state"] == "done"


def test_an_approval_nothing_will_build_says_so_in_the_steps_evidence() -> None:
    record = _record("approved", decision={"verdict": "approved", "at": T0 - timedelta(hours=3),
                                           "by": "root@saga.xyz", "reason": None})
    view = derive(CAROL, records=[], pair_docs=[], registrations=[], tenant_lists_git=False,
                  now=T0, workspace=record, workspace_publishing=False)
    evidence = _step(view, "workspace")["evidence"]
    assert evidence["provisioning"] == {"available": False, "waiting_because": "publishing_off",
                                        "approved_minutes_ago": 180}
    assert evidence["request_id"] == "req-1"
    # A caller that does not know the publisher sends no block, not a guess.
    assert "provisioning" not in _step(_derive(workspace=record), "workspace")["evidence"]
