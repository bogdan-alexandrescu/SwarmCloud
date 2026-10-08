"""A person's personal workspace: the record, the request, the loan request,
the submission gate and `WORKSPACE_GATE` (docs/workspaces.md §1, §5, §6.3;
#847, lane W1).

The people in conftest: `carol` is in no registered group, so she resolves
to her personal tenant `u-carol` -- the kind the gate judges. `alice` is in
`eng`, a group tenant, which the gate never judges (WD7).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES

from swarm_api import workspaces as ws
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker
from fastapi.testclient import TestClient

from .conftest import NoForgeTokens, api_settings, auth_header

CAROL = "carol@saga.xyz"
WORKSPACE_ID = re.compile(r"^w-[0-9a-f]{6}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _gate(api_context, on: bool) -> None:
    api_context.submissions.workspaces.gate = on


def _record(db, tenant_id: str = "u-carol", **fields) -> dict:
    doc = {
        "tenant_id": tenant_id, "workspace_id": "w-3f9a2c", "principal": CAROL,
        "state": "requested", "request_id": "req-1", "requested_at": _now(),
        "requested_via": "console", "decision": None, "history": [],
        "limits": dict(ws.DEFAULT_LIMITS), "run": None, "steps": {}, "failure": None,
        "ready_at": None, "migrated": False,
    }
    doc.update(fields)
    db.docs[f"workspaces/{tenant_id}"] = doc
    db.docs[f"workspace_ids/{doc['workspace_id']}"] = {"tenant_id": tenant_id}
    return doc


def _personal_tenant(db, tenant_id: str = "u-carol", principal: str = CAROL,
                     credentials: tuple[str, ...] = ()) -> None:
    db.docs[f"tenants/{tenant_id}"] = {
        "tenant_id": tenant_id, "kind": "user", "principal": principal,
        "created_at": _now(), "display_name": tenant_id, "max_active": 8,
        "capacity_units": 8, "monthly_budget_usd": None, "enabled": True,
        "credentials": list(credentials), "service_account": None, "gcs_prefix": None,
        "namespace": None,
    }


def _account(db, account_id: str, owner: str, *, lend_to=(), state="AVAILABLE") -> None:
    db.docs[f"accounts/{account_id}"] = {
        "account_id": account_id, "owner_tenant": owner, "label": account_id,
        "provider": "anthropic", "state": state, "lend_to": list(lend_to),
    }


def _task(client, user: str = "carol", profile: str = "mock"):
    return client.post("/v1/tasks", headers=auth_header(user),
                       json={"runner_profile": profile, "input": {"prompt": "x"}})


def _top(db, collection: str) -> dict:
    return {k: v for k, v in db.docs.items()
            if k.startswith(collection + "/") and k.count("/") == 1}


# --------------------------------------------------------------------------
# WORKSPACE_GATE ships off
# --------------------------------------------------------------------------

def test_the_gate_defaults_off_when_the_variable_is_unset_or_empty() -> None:
    assert ws.gate_from_env({}) is False
    assert ws.gate_from_env({"WORKSPACE_GATE": ""}) is False
    assert ws.gate_from_env({"WORKSPACE_GATE": "off"}) is False
    assert ws.gate_from_env({"WORKSPACE_GATE": "ON"}) is True
    assert ws.gate_from_env({"WORKSPACE_GATE": " on "}) is True


def test_a_gate_value_that_is_neither_on_nor_off_stops_the_service() -> None:
    with pytest.raises(ValueError, match="WORKSPACE_GATE"):
        ws.gate_from_env({"WORKSPACE_GATE": "onn"})


def test_the_shipped_context_builds_the_gate_off(api_context, monkeypatch) -> None:
    monkeypatch.delenv("WORKSPACE_GATE", raising=False)
    assert api_context.submissions.workspaces.gate is False
    assert ws.Workspaces(None, now=_now).gate is False


def test_with_the_gate_off_a_person_with_no_workspace_submits_as_today(client, db) -> None:
    response = _task(client)
    assert response.status_code == 201, response.text
    assert response.json()["task"]["tenant_id"] == "u-carol"
    # And the personal tenant is created on first sight, as it always was.
    assert db.docs["tenants/u-carol"]["kind"] == "user"
    assert not _top(db, "workspaces")


def test_with_the_gate_off_the_gate_reads_nothing() -> None:
    # A database of None fails on any read: the off gate must not reach it.
    gate = ws.Workspaces(None, now=_now, gate=False)
    person = SimpleNamespace(email=CAROL, member_scope="", is_rollup_sweeper=False,
                             tenant_member="")
    gate.check(person, Tenant(tenant_id="u-carol", kind="user", principal=CAROL,
                              created_at=_now()))


# --------------------------------------------------------------------------
# GET / POST /v1/workspace
# --------------------------------------------------------------------------

def test_before_any_request_the_state_is_none(client) -> None:
    response = client.get("/v1/workspace", headers=auth_header("carol"))
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "none"
    assert response.json()["setup_url"].endswith("/setup#workspace")


def test_a_request_creates_the_record_and_its_id_index_in_requested(client, db) -> None:
    response = client.post("/v1/workspace", headers=auth_header("carol"), json={"via": "console"})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["state"] == "requested"
    assert WORKSPACE_ID.fullmatch(body["workspace_id"]), body["workspace_id"]
    record = db.docs["workspaces/u-carol"]
    assert record["tenant_id"] == "u-carol" and record["principal"] == CAROL
    assert record["state"] == "requested" and record["requested_via"] == "console"
    assert record["limits"] == {"max_active": 8, "capacity_units": 8, "quota_pods": 16,
                                "quota_cpu": 64}
    assert db.docs[f"workspace_ids/{body['workspace_id']}"] == {"tenant_id": "u-carol"}
    # The person's own view never carries their address or an admin's.
    assert CAROL not in response.text


def test_a_request_is_idempotent(client, db) -> None:
    first = client.post("/v1/workspace", headers=auth_header("carol"))
    before = {k: dict(v) for k, v in db.docs.items() if k.startswith("workspace")}
    second = client.post("/v1/workspace", headers=auth_header("carol"), json={"via": "plugin"})
    assert first.status_code == 202 and second.status_code == 200, second.text
    assert second.json()["workspace_id"] == first.json()["workspace_id"]
    assert second.json()["request_id"] == first.json()["request_id"]
    assert {k: v for k, v in db.docs.items() if k.startswith("workspace")} == before
    assert len(_top(db, "workspace_ids")) == 1
    read = client.get("/v1/workspace", headers=auth_header("carol")).json()
    assert read["workspace_id"] == first.json()["workspace_id"]


def test_the_body_is_only_where_the_request_came_from(client, db) -> None:
    refused = client.post("/v1/workspace", headers=auth_header("carol"),
                          json={"tenant_id": "u-alice"})
    assert refused.status_code == 422
    unknown = client.post("/v1/workspace", headers=auth_header("carol"), json={"via": "email"})
    assert unknown.status_code == 422
    assert not _top(db, "workspaces")


def test_a_group_member_requests_their_personal_workspace_never_the_groups(client, db) -> None:
    response = client.post("/v1/workspace", headers=auth_header("alice"))
    assert response.status_code == 202, response.text
    assert list(_top(db, "workspaces")) == ["workspaces/u-alice"]
    assert db.docs["workspaces/u-alice"]["principal"] == "alice@saga.xyz"


@pytest.mark.parametrize("state", ["requested", "approved", "applying", "needs_owner", "ready"])
def test_a_request_against_a_standing_record_writes_nothing(client, db, state) -> None:
    _record(db, state=state)
    before = {k: dict(v) for k, v in db.docs.items()}
    response = client.post("/v1/workspace", headers=auth_header("carol"))
    assert response.status_code == 200, response.text
    assert response.json()["state"] == state
    assert response.json()["workspace_id"] == "w-3f9a2c"
    # Nothing but the person's sighting (people/), which is not the record.
    assert {k: v for k, v in db.docs.items() if not k.startswith("people/")} == before


def test_a_request_inside_24_hours_of_a_denial_is_refused(client, db) -> None:
    _record(db, state="denied", decision={
        "by": "root@saga.xyz", "at": _now() - timedelta(hours=23), "verdict": "denied",
        "reason": "Use the eng space."})
    before = dict(db.docs["workspaces/u-carol"])
    response = client.post("/v1/workspace", headers=auth_header("carol"))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "WORKSPACE_REQUEST_TOO_SOON"
    assert response.json()["detail"]["request_again_at"]
    assert db.docs["workspaces/u-carol"] == before


def test_a_request_after_24_hours_reopens_a_denied_record_and_keeps_the_reason(client, db) -> None:
    denial = {"by": "root@saga.xyz", "at": _now() - timedelta(hours=25), "verdict": "denied",
              "reason": "Use the eng space."}
    _record(db, state="denied", decision=denial)
    shown = client.get("/v1/workspace", headers=auth_header("carol")).json()
    assert shown["decision"] == {"verdict": "denied", "reason": "Use the eng space.",
                                 "at": denial["at"].isoformat()}
    assert "root@saga.xyz" not in str(shown)  # `decision.by` never leaves Firestore
    response = client.post("/v1/workspace", headers=auth_header("carol"))
    assert response.status_code == 202, response.text
    record = db.docs["workspaces/u-carol"]
    assert record["state"] == "requested" and record["decision"] is None
    assert record["request_id"] != "req-1"
    assert record["workspace_id"] == "w-3f9a2c"  # the same workspace, asked again
    assert record["history"] == [{"request_id": "req-1", "requested_at": record["history"][0][
        "requested_at"], "decision": denial}]


def test_a_request_against_a_failed_record_is_a_conflict_with_the_failure_copy(client, db) -> None:
    _record(db, state="failed", failure={"step": "A7", "code": "NAMESPACE_APPLY_FAILED",
                                         "retryable": True, "at": _now()})
    before = dict(db.docs["workspaces/u-carol"])
    response = client.post("/v1/workspace", headers=auth_header("carol"))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "WORKSPACE_FAILED"
    assert "could not be applied in full" in response.json()["message"]
    assert "req-1" in response.json()["message"]
    assert db.docs["workspaces/u-carol"] == before


def test_a_tenant_id_owned_by_another_principal_is_refused_without_naming_it(client, db) -> None:
    _personal_tenant(db, principal="carol@partner.example")
    response = client.post("/v1/workspace", headers=auth_header("carol"))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "WORKSPACE_ID_TAKEN"
    assert "partner.example" not in response.text
    assert not _top(db, "workspaces")


def test_a_secret_admin_cannot_request_a_workspace(db, tokens, group_map, objects) -> None:
    context = build_context(
        settings=api_settings(secret_admin_principals=(CAROL,)), db=db,
        verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, forge_tokens=NoForgeTokens())
    client = TestClient(create_app(context), raise_server_exceptions=False)
    response = client.post("/v1/workspace", headers=auth_header("carol"))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "WORKSPACE_PRINCIPAL_FORBIDDEN"
    assert not _top(db, "workspaces")


def test_a_service_account_cannot_request_a_workspace(api_context, db) -> None:
    from swarm_api.errors import WorkspaceNotForServiceAccounts
    from swarm_api.routes.workspaces import _own

    caller = SimpleNamespace(email="swarm-verify@example-proj.iam.gserviceaccount.com",
                             member_scope="", is_rollup_sweeper=False, tenant_member="")
    with pytest.raises(WorkspaceNotForServiceAccounts):
        _own(caller, api_context)  # type: ignore[arg-type]
    listed = SimpleNamespace(email="someone@saga.xyz", member_scope="continuation",
                             is_rollup_sweeper=False, tenant_member="")
    with pytest.raises(WorkspaceNotForServiceAccounts):
        _own(listed, api_context)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# the workspace id: random, unique, and never the email
# --------------------------------------------------------------------------

def test_the_id_is_drawn_from_secrets_token_hex(monkeypatch) -> None:
    drawn = []

    def token_hex(n: int) -> str:
        drawn.append(n)
        return "abc123"

    monkeypatch.setattr(ws.secrets, "token_hex", token_hex)
    assert ws.new_workspace_id() == "w-abc123"
    assert drawn == [3]


def test_two_requests_for_one_address_in_two_databases_draw_different_ids(db) -> None:
    from .fakes import FakeFirestore

    ids = set()
    for _ in range(4):
        store = ws.Workspaces(FakeFirestore(), now=_now, gate=False)
        _, record = store.request(tenant_id="u-carol", principal=CAROL, via="api")
        ids.add(record["workspace_id"])
        assert "carol" not in record["workspace_id"]
    # Derived from the address, all four would be one id.
    assert len(ids) > 1


def test_a_drawn_id_that_is_taken_is_drawn_again(db) -> None:
    db.docs["workspace_ids/w-aaaaaa"] = {"tenant_id": "u-dave"}
    draws = iter(["w-aaaaaa", "w-aaaaaa", "w-bbbbbb"])
    store = ws.Workspaces(db, now=_now, gate=False, new_id=lambda: next(draws))
    status, record = store.request(tenant_id="u-carol", principal=CAROL, via="api")
    assert status == 202 and record["workspace_id"] == "w-bbbbbb"
    assert db.docs["workspace_ids/w-aaaaaa"] == {"tenant_id": "u-dave"}
    assert db.docs["workspace_ids/w-bbbbbb"] == {"tenant_id": "u-carol"}


def test_no_email_or_personal_tenant_id_is_logged(api_context, client, db, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="swarm_api.workspaces")
    client.post("/v1/workspace", headers=auth_header("carol"))
    client.post("/v1/workspace/loan-request", headers=auth_header("carol"))
    _gate(api_context, True)
    _task(client)
    mine = [r.getMessage() for r in caplog.records if r.name == "swarm_api.workspaces"]
    assert mine, "nothing logged: the paths under test did not run"
    assert not any("carol" in line for line in mine), mine


# --------------------------------------------------------------------------
# the loan request
# --------------------------------------------------------------------------

def test_a_loan_request_needs_a_workspace_record(client, db) -> None:
    response = client.post("/v1/workspace/loan-request", headers=auth_header("carol"))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "WORKSPACE_NOT_REQUESTED"
    assert not _top(db, "loan_requests")


def test_a_loan_request_is_recorded_once(client, db) -> None:
    _record(db, state="ready")
    first = client.post("/v1/workspace/loan-request", headers=auth_header("carol"),
                        json={"via": "plugin"})
    assert first.status_code == 202, first.text
    loan = db.docs["loan_requests/u-carol"]
    assert loan["state"] == "requested" and loan["workspace_id"] == "w-3f9a2c"
    assert loan["requested_via"] == "plugin"
    again = client.post("/v1/workspace/loan-request", headers=auth_header("carol"))
    assert again.status_code == 200, again.text
    assert again.json()["request_id"] == first.json()["request_id"]
    assert db.docs["loan_requests/u-carol"] == loan


# --------------------------------------------------------------------------
# the gate, on
# --------------------------------------------------------------------------

def test_gate_on_a_person_with_no_record_is_refused_and_no_tenant_is_written(
        api_context, client, db) -> None:
    _gate(api_context, True)
    response = _task(client)
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "WORKSPACE_NOT_READY"
    assert "Finish setup: create your workspace" in body["message"]
    assert body["detail"]["state"] == "none"
    assert body["detail"]["setup_url"].endswith("/setup#workspace")
    assert body["detail"]["setup_command"] == "/sc:setup"
    # `tenant_for` stops creating u-* once the gate is on, and nothing is queued.
    assert "tenants/u-carol" not in db.docs
    assert "pools/tenant:u-carol" not in db.docs
    assert not _top(db, "tasks")
    # Looking around still works, and still writes no tenant.
    me = client.get("/v1/tenants/me", headers=auth_header("carol"))
    assert me.status_code == 200, me.text
    assert me.json()["tenant"]["tenant_id"] == "u-carol"
    assert "tenants/u-carol" not in db.docs


@pytest.mark.parametrize("state, phrase", [
    ("requested", "waiting for an admin's approval"),
    ("approved", "is being created"),
    ("applying", "is being created"),
    ("needs_owner", "the platform owner's review"),
    ("denied", "was not approved: Use the eng space."),
    ("failed", "could not be created"),
])
def test_gate_on_every_state_short_of_ready_is_refused(api_context, client, db, state,
                                                       phrase) -> None:
    _gate(api_context, True)
    _personal_tenant(db)
    _account(db, "acct-own", "u-carol")
    _record(db, state=state,
            decision={"verdict": "denied", "reason": "Use the eng space.", "at": _now(),
                      "by": "root@saga.xyz"} if state == "denied" else None,
            failure={"step": "A3", "code": "APPLY_FAILED", "retryable": True,
                     "at": _now()} if state == "failed" else None)
    response = _task(client)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "WORKSPACE_NOT_READY"
    assert phrase in response.json()["message"]
    assert response.json()["detail"] == {
        "workspace_id": "w-3f9a2c", "state": state,
        "setup_url": response.json()["detail"]["setup_url"], "setup_command": "/sc:setup"}
    assert "root@saga.xyz" not in response.text
    assert not _top(db, "tasks")


@pytest.mark.parametrize("profile", sorted(RUNNER_PROFILES))
def test_gate_on_a_ready_workspace_with_no_claude_account_runs_no_profile(
        api_context, client, db, profile) -> None:
    _gate(api_context, True)
    _personal_tenant(db)
    _record(db, state="ready")
    response = _task(client, profile=profile)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "NO_CLAUDE_ACCOUNT"
    assert response.json()["message"].startswith("No Claude account yet")
    assert response.json()["detail"]["setup_url"].endswith("/setup#claude-account")
    assert not _top(db, "tasks")


@pytest.mark.parametrize("how", ["own", "lent", "paused", "provider_key"])
def test_gate_on_a_ready_workspace_with_a_claude_account_is_admitted(api_context, client, db,
                                                                      how) -> None:
    _gate(api_context, True)
    _personal_tenant(db, credentials=("anthropic",) if how == "provider_key" else ())
    if how == "own":
        _account(db, "acct-own", "u-carol")
    elif how == "lent":
        _account(db, "acct-eng", "eng", lend_to=("u-carol",))
    elif how == "paused":
        _account(db, "acct-own", "u-carol", state="PAUSED")
    _record(db, state="ready")
    response = _task(client)
    assert response.status_code == 201, response.text
    assert response.json()["task"]["tenant_id"] == "u-carol"


@pytest.mark.parametrize("state", ["DRAINING", "REAUTH_REQUIRED"])
def test_an_account_nothing_new_can_run_on_is_not_a_claude_account(api_context, client, db,
                                                                    state) -> None:
    _gate(api_context, True)
    _personal_tenant(db)
    _account(db, "acct-own", "u-carol", state=state)
    _account(db, "acct-other", "u-dave")  # someone else's, lent to nobody
    _record(db, state="ready")
    response = _task(client)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "NO_CLAUDE_ACCOUNT"


def test_gate_on_a_group_tenant_is_never_judged(api_context, client, db) -> None:
    _gate(api_context, True)
    response = _task(client, user="alice")
    assert response.status_code == 201, response.text
    assert response.json()["task"]["tenant_id"] == "eng"
    assert not _top(db, "workspaces")


@pytest.mark.parametrize("caller", [
    SimpleNamespace(email="swarm-verify@example-proj.iam.gserviceaccount.com",
                    member_scope="", is_rollup_sweeper=False, tenant_member=""),
    SimpleNamespace(email="bot@saga.xyz", member_scope="continuation",
                    is_rollup_sweeper=False, tenant_member=""),
    SimpleNamespace(email="rollup@saga.xyz", member_scope="", is_rollup_sweeper=True,
                    tenant_member=""),
    SimpleNamespace(email="member@saga.xyz", member_scope="", is_rollup_sweeper=False,
                    tenant_member="sa@example-proj.iam.gserviceaccount.com"),
])
def test_gate_on_service_accounts_and_listed_identities_are_exempt(caller) -> None:
    # A database of None fails on any read: an exempt caller must not reach it.
    gate = ws.Workspaces(None, now=_now, gate=True)
    tenant = Tenant(tenant_id="u-sw-c90291", kind="user", principal=caller.email,
                    created_at=_now())
    gate.check(caller, tenant)
    assert gate.withholds_tenant(SimpleNamespace(**vars(caller), tenant_id="u-sw-c90291")) \
        is False


def test_gate_on_a_person_is_judged() -> None:
    from swarm_api.errors import WorkspaceNotReady
    from .fakes import FakeFirestore

    gate = ws.Workspaces(FakeFirestore(), now=_now, gate=True)
    person = SimpleNamespace(email=CAROL, member_scope="", is_rollup_sweeper=False,
                             tenant_member="", tenant_id="u-carol")
    with pytest.raises(WorkspaceNotReady):
        gate.check(person, Tenant(tenant_id="u-carol", kind="user", principal=CAROL,
                                  created_at=_now()))
    assert gate.withholds_tenant(person) is True


# --------------------------------------------------------------------------
# workflows are refused whole (§5.3)
# --------------------------------------------------------------------------

WORKFLOW = {"steps": [
    {"step_id": "a", "runner_profile": "mock", "input": {"prompt": "one"}},
    {"step_id": "b", "runner_profile": "mock", "input": {"prompt": "two"},
     "depends_on": ["a"]},
]}


def test_gate_on_a_workflow_is_refused_whole_and_creates_nothing(api_context, client, db) -> None:
    _gate(api_context, True)
    _personal_tenant(db)
    _record(db, state="requested")
    before = dict(db.docs)
    response = client.post("/v1/workflows", headers=auth_header("carol"), json=WORKFLOW)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "WORKSPACE_NOT_READY"
    assert not _top(db, "workflows")
    assert not _top(db, "tasks")
    assert not [k for k in db.docs if "/events/" in k]
    # Nothing else was written either, bar the person's sighting.
    assert {k for k in db.docs if not k.startswith("people/")} == set(before)


def test_gate_on_a_ready_workflow_with_an_account_runs_every_step(api_context, client, db) -> None:
    _gate(api_context, True)
    _personal_tenant(db)
    _account(db, "acct-own", "u-carol")
    _record(db, state="ready")
    response = client.post("/v1/workflows", headers=auth_header("carol"), json=WORKFLOW)
    assert response.status_code == 201, response.text
    assert len(_top(db, "tasks")) == 2


def test_gate_off_a_workflow_is_submitted_as_today(client, db) -> None:
    response = client.post("/v1/workflows", headers=auth_header("carol"), json=WORKFLOW)
    assert response.status_code == 201, response.text
    assert len(_top(db, "tasks")) == 2


# --------------------------------------------------------------------------
# people/ (§6.4 part 1)
# --------------------------------------------------------------------------

def test_a_sighting_is_recorded_once_per_ten_minutes(client, db) -> None:
    assert client.get("/v1/tenants/me", headers=auth_header("alice")).status_code == 200
    person = db.docs["people/u-alice"]
    assert person["principal"] == "alice@saga.xyz"
    assert person["teams"] == ["eng"]
    assert person["first_seen"] == person["last_seen"]
    seen = person["last_seen"]
    assert client.get("/v1/tenants/me", headers=auth_header("alice")).status_code == 200
    assert db.docs["people/u-alice"]["last_seen"] == seen


def test_a_sighting_keeps_first_seen(api_context, db) -> None:
    earlier = _now() - timedelta(days=3)
    db.docs["people/u-carol"] = {"principal": CAROL, "first_seen": earlier,
                                 "last_seen": earlier, "teams": []}
    caller = SimpleNamespace(email=CAROL, member_scope="", is_rollup_sweeper=False,
                             tenant_choices=())
    api_context.submissions.workspaces.touch_person(caller)
    assert db.docs["people/u-carol"]["first_seen"] == earlier
    assert db.docs["people/u-carol"]["last_seen"] > earlier


def test_no_sighting_is_recorded_for_a_service_account(api_context, db) -> None:
    caller = SimpleNamespace(email="swarm-verify@example-proj.iam.gserviceaccount.com",
                             member_scope="", is_rollup_sweeper=False, tenant_choices=())
    api_context.submissions.workspaces.touch_person(caller)
    assert not _top(db, "people")
