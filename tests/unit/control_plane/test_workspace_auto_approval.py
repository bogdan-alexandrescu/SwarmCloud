"""An admin's own workspace request is approved automatically
(docs/workspaces.md §1.3, owner decision 2026-10-09; part of #847).

`POST /v1/workspace` by a caller who `is_admin` (§6.5) performs, in the
request's own transaction, the approval `POST /v1/admin/workspaces/{id}/approve`
performs -- the same code (`People._approve_in`) -- and then the same publish.
What is held:

* an admin's request goes straight to `approved`, with
  `decision = {by: <the admin>, auto: true, reason: "requester is an admin"}`,
  one `admin_audit` entry `approve_own_workspace_auto`, and one publish;
* a non-admin's request is unchanged: `requested`, nothing published, no audit;
* an admin whose personal tenant predates the workspace job (Terraform's
  `managed_by = swarm-terraform`, or a record with `migrated = true`, §3.3) is
  left `requested`, told it is being migrated, and never published -- the
  apply's squat check would fail `IDENTITY_NOT_OURS` -- and a manual approval
  of it is refused too;
* every refusal a request or an approval applies still applies to an admin;
* an approval creates no task and no lease (invariant 1).

`root` is an admin (conftest's group map); `carol` is not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api import people as people_mod
from swarm_api import workspaces as ws
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import NoForgeTokens, api_settings, auth_header
from .test_people_admin import FakePublisher, _audit

ROOT = "root@saga.xyz"
ROOT_TENANT = ws.personal_tenant_id(ROOT)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _context(db, tokens, group_map, objects, **settings):
    return build_context(
        settings=api_settings(**settings), db=db,
        verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, forge_tokens=NoForgeTokens())


def _client(context, publisher) -> TestClient:
    app = create_app(context)
    app.state.workspace_publisher = publisher
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def client(db, tokens, group_map, objects, publisher) -> TestClient:
    return _client(_context(db, tokens, group_map, objects), publisher)


def _request(client, user: str = "root"):
    return client.post("/v1/workspace", headers=auth_header(user), json={"via": "console"})


def _rec(db, tenant_id: str = ROOT_TENANT) -> dict:
    return db.docs[f"workspaces/{tenant_id}"]


def _tenant(db, tenant_id: str = ROOT_TENANT, principal: str = ROOT, **fields) -> None:
    doc = {
        "tenant_id": tenant_id, "kind": "user", "principal": principal,
        "created_at": _now(), "display_name": tenant_id, "max_active": 80,
        "capacity_units": 80, "monthly_budget_usd": None, "enabled": True,
        "credentials": [], "service_account": None, "gcs_prefix": None, "namespace": None,
    }
    doc.update(fields)
    db.docs[f"tenants/{tenant_id}"] = doc


def _record(db, *, tenant_id: str = ROOT_TENANT, principal: str = ROOT, **fields) -> dict:
    doc = {
        "tenant_id": tenant_id, "workspace_id": "w-0a0b0c", "principal": principal,
        "state": "requested", "request_id": "6a1f2c4e-0000-4000-8000-000000000001",
        "requested_at": _now(), "requested_via": "console", "decision": None, "history": [],
        "limits": dict(ws.DEFAULT_LIMITS), "run": None, "steps": {}, "failure": None,
        "ready_at": None, "migrated": False,
    }
    doc.update(fields)
    db.docs[f"workspaces/{tenant_id}"] = doc
    db.docs[f"workspace_ids/{doc['workspace_id']}"] = {"tenant_id": tenant_id}
    return doc


def _no_demand(db) -> None:
    """Invariant 1: an approval is a record and a message, never a task or a lease."""
    made = [k for k in db.docs if k.split("/", 1)[0] in {"tasks", "leases", "workflows"}]
    assert made == []


# --------------------------------------------------------------------------
# an admin's request
# --------------------------------------------------------------------------

def test_an_admins_request_is_approved_audited_and_published_once(client, db, publisher) -> None:
    response = _request(client)
    assert response.status_code == 202, response.text
    record = _rec(db)
    assert record["state"] == "approved"
    assert record["principal"] == ROOT
    assert record["decision"]["by"] == ROOT
    assert record["decision"]["verdict"] == "approved"
    assert record["decision"]["auto"] is True
    assert record["decision"]["reason"] == "requester is an admin"
    assert record["limits"] == dict(ws.DEFAULT_LIMITS)
    [entry] = _audit(db)
    assert entry["action"] == "approve_own_workspace_auto"
    assert entry["target_workspace_id"] == record["workspace_id"]
    assert entry["by"] == ROOT
    assert entry["detail"]["auto"] is True and entry["detail"]["self_approval"] is True
    assert entry["detail"]["request_id"] == record["request_id"]
    assert publisher.messages == [{"workspace_id": record["workspace_id"], "mode": "create",
                                   "request_id": record["request_id"]}]
    # The publish is recorded exactly as an admin's click records it.
    assert record["dispatch"]["attempts"] == 1 and record["dispatch"]["last_ok"] is True
    assert record["run"]["mode"] == "create"
    body = response.json()
    assert body["state"] == "approved"
    assert body["decision"]["auto"] is True
    assert body["decision"]["reason"] == "requester is an admin"
    # `decision.by` never leaves Firestore.
    assert ROOT not in response.text
    _no_demand(db)


def test_asking_again_after_the_auto_approval_writes_and_publishes_nothing(
        client, db, publisher) -> None:
    first = _request(client)
    before = {k: v for k, v in db.docs.items() if not k.startswith("people/")}
    second = _request(client)
    assert second.status_code == 200, second.text
    assert second.json()["workspace_id"] == first.json()["workspace_id"]
    assert len(publisher.messages) == 1
    assert {k: v for k, v in db.docs.items() if not k.startswith("people/")} == before


def test_an_admins_standing_request_is_approved_when_they_ask_again(client, db, publisher) -> None:
    # Made before the requester was an admin, or before this decision.
    record = _record(db)
    response = _request(client)
    assert response.status_code == 202, response.text
    assert _rec(db)["state"] == "approved"
    assert _rec(db)["request_id"] == record["request_id"]
    [entry] = _audit(db, "approve_own_workspace_auto")
    assert entry["detail"]["from_state"] == "requested"
    assert publisher.messages == [{"workspace_id": "w-0a0b0c", "mode": "create",
                                   "request_id": record["request_id"]}]


def test_an_admins_request_after_a_denial_is_reopened_and_approved_keeping_the_denial(
        client, db, publisher) -> None:
    denial = {"by": "other@saga.xyz", "at": _now() - timedelta(hours=25),
              "verdict": "denied", "reason": "Use the eng space."}
    old_request_id = _record(db, state="denied", decision=denial)["request_id"]
    response = _request(client)
    assert response.status_code == 202, response.text
    record = _rec(db)
    assert record["state"] == "approved" and record["decision"]["auto"] is True
    assert record["request_id"] != old_request_id
    [previous] = record["history"]
    assert previous["decision"]["reason"] == "Use the eng space."
    assert [m["request_id"] for m in publisher.messages] == [record["request_id"]]


def test_with_publishing_off_the_auto_approval_stands_and_nothing_is_sent(
        db, tokens, group_map, objects) -> None:
    off = FakePublisher(enabled=False)
    client = _client(_context(db, tokens, group_map, objects), off)
    response = _request(client)
    assert response.status_code == 202, response.text
    assert _rec(db)["state"] == "approved"
    assert off.messages == []
    assert "dispatch" not in _rec(db)  # the sweep finds it unpublished
    assert len(_audit(db, "approve_own_workspace_auto")) == 1


def test_a_failed_publish_is_not_a_failed_auto_approval(db, tokens, group_map, objects) -> None:
    failing = FakePublisher(ok=False)
    client = _client(_context(db, tokens, group_map, objects), failing)
    response = _request(client)
    assert response.status_code == 202, response.text
    assert _rec(db)["state"] == "approved"
    assert _rec(db)["dispatch"]["last_ok"] is False
    assert len(failing.messages) == 1


def test_a_tenant_ensure_tenant_wrote_on_first_sight_does_not_hold_the_approval(
        client, db, publisher) -> None:
    # §3.3: a `u-*` document with no infrastructure behind it is adopted by A8.
    _tenant(db)
    response = _request(client)
    assert response.status_code == 202, response.text
    assert _rec(db)["state"] == "approved"
    assert len(publisher.messages) == 1


# --------------------------------------------------------------------------
# a non-admin's request is unchanged
# --------------------------------------------------------------------------

def test_a_non_admins_request_waits_for_an_admin_and_nothing_is_published(
        client, db, publisher) -> None:
    response = _request(client, "carol")
    assert response.status_code == 202, response.text
    record = _rec(db, "u-carol")
    assert record["state"] == "requested" and record["decision"] is None
    assert "held" not in record or record["held"] is None
    assert response.json()["state"] == "requested"
    assert response.json()["held"] is None
    assert publisher.messages == []
    assert _audit(db) == []
    _no_demand(db)


# --------------------------------------------------------------------------
# a personal tenant that predates the workspace job (§3.3)
# --------------------------------------------------------------------------

def test_an_admin_whose_terraform_tenant_exists_is_left_requested_and_never_published(
        client, db, publisher) -> None:
    _tenant(db, credentials=["anthropic"], managed_by=people_mod.TERRAFORM_MANAGED)
    response = _request(client)
    assert response.status_code == 202, response.text
    record = _rec(db)
    assert record["state"] == "requested"
    assert record["decision"] is None
    assert record["held"] == ws.HELD_MIGRATING
    assert record["migrated"] is False  # W9's to write, with its allow-list
    body = response.json()
    assert body["state"] == "requested"
    assert body["held"] == {"reason": "migrating", "copy": ws.MIGRATING_COPY}
    assert "migrated" in ws.MIGRATING_COPY
    assert publisher.messages == []
    assert _audit(db) == []
    # Asking again changes nothing and still publishes nothing.
    again = _request(client)
    assert again.status_code == 200, again.text
    assert _rec(db)["state"] == "requested" and publisher.messages == []
    _no_demand(db)


def test_a_manual_approval_of_a_workspace_being_migrated_is_refused(client, db, publisher) -> None:
    _tenant(db, managed_by=people_mod.TERRAFORM_MANAGED)
    workspace_id = _request(client).json()["workspace_id"]
    response = client.post(f"/v1/admin/workspaces/{workspace_id}/approve",
                           headers=auth_header("root"))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "WORKSPACE_MIGRATING"
    assert _rec(db)["state"] == "requested"
    assert publisher.messages == [] and _audit(db) == []


def test_an_admin_whose_record_is_marked_migrated_is_not_auto_approved(
        client, db, publisher) -> None:
    _record(db, migrated=True)
    response = _request(client)
    assert response.status_code == 200, response.text
    assert _rec(db)["state"] == "requested"
    assert response.json()["held"]["reason"] == "migrating"
    assert publisher.messages == [] and _audit(db) == []


def test_the_migrating_copy_replaces_waiting_for_an_admin_in_the_refusal() -> None:
    record = {"state": "requested", "held": ws.HELD_MIGRATING}
    assert "migrated" in ws.not_ready_message(record)
    assert "admin's approval" not in ws.not_ready_message(record)


# --------------------------------------------------------------------------
# every refusal still applies to an admin
# --------------------------------------------------------------------------

def test_an_admin_who_is_a_secret_admin_is_refused_and_nothing_is_published(
        db, tokens, group_map, objects, publisher) -> None:
    client = _client(_context(db, tokens, group_map, objects,
                              secret_admin_principals=(ROOT,)), publisher)
    response = _request(client)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "WORKSPACE_PRINCIPAL_FORBIDDEN"
    assert f"workspaces/{ROOT_TENANT}" not in db.docs
    assert publisher.messages == [] and _audit(db) == []


def test_an_admin_whose_derived_id_belongs_to_another_principal_is_refused(
        client, db, publisher) -> None:
    _tenant(db, principal="root@partner.example")
    response = _request(client)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "WORKSPACE_ID_TAKEN"
    assert f"workspaces/{ROOT_TENANT}" not in db.docs
    assert publisher.messages == [] and _audit(db) == []


def test_an_admin_inside_24_hours_of_a_denial_is_refused(client, db, publisher) -> None:
    _record(db, state="denied", decision={
        "by": "other@saga.xyz", "at": _now() - timedelta(hours=2), "verdict": "denied",
        "reason": "No."})
    response = _request(client)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "WORKSPACE_REQUEST_TOO_SOON"
    assert _rec(db)["state"] == "denied"
    assert publisher.messages == [] and _audit(db) == []


@pytest.mark.parametrize("state", ["approved", "applying", "needs_owner", "ready"])
def test_an_admins_standing_record_past_requested_is_left_alone(
        client, db, publisher, state) -> None:
    _record(db, state=state)
    response = _request(client)
    assert response.status_code == 200, response.text
    assert _rec(db)["state"] == state
    assert publisher.messages == [] and _audit(db) == []


def test_an_admins_failed_record_is_a_conflict_not_an_approval(client, db, publisher) -> None:
    _record(db, state="failed", failure={"step": "A7", "code": "CLUSTER_UNREACHABLE",
                                         "retryable": True, "at": _now()})
    response = _request(client)
    assert response.status_code == 409, response.text
    assert _rec(db)["state"] == "failed"
    assert publisher.messages == [] and _audit(db) == []
