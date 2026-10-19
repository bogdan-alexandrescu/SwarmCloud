"""The workspace record's kinds and the admin request for each
(docs/design/workspace-kinds.md §4.1, §4.2; lane WK-RECORD, part of #847).

`workspaces/{tenant_id}` gains `kind` (`personal`, `group`, `service`),
`principal`, `requested_by`, `display_name` and, for a group, `directory`.
An admin files any kind through `POST /v1/admin/workspaces`; the checks of
§4.2.3 run before any write, and a service workspace, the one kind that
admits a new principal at the front door, is approved by the platform owner
only (Q2 (a)).

Every route against the shipped app over an in-memory Firestore, with a
recording publisher on `app.state` and, for the directory check, a real
`CloudIdentityGroups` over a recording HTTP session -- nothing patched but the
one injected commit failure. No cloud credentials, no emulator.

The people: `root` is an admin and the platform owner, `dana` is an admin who
is not the owner, `carol` is nobody's admin.
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import publish_workspace as pw
from swarm_api import workspaces as ws
from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import CloudIdentityGroups, StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import (
    ADMIN_GROUP,
    ENG_GROUP,
    PROJECT,
    NoForgeTokens,
    api_settings,
    auth_header,
    seed_tenant,
)
from .fakes import FakeTransaction

ROOT = "root@saga.xyz"
DANA = "dana@saga.xyz"
CAROL = "carol@saga.xyz"
REPORTS_BOT = f"reports-bot@{PROJECT}.iam.gserviceaccount.com"
LISTED_BOT = f"listed-bot@{PROJECT}.iam.gserviceaccount.com"
ROUTE = "/v1/admin/workspaces"


class FakePublisher:
    def __init__(self) -> None:
        self.enabled = True
        self.messages: list[dict[str, str]] = []

    def publish(self, workspace_id: str, mode: str, request_id: str) -> bool:
        self.messages.append(pw.message_data(workspace_id, mode, request_id))
        return True


class _Response:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self) -> dict[str, Any]:
        return self._body


class RecordingDirectory:
    """Cloud Identity's HTTP surface, recorded. `fail` answers every
    membership check 500, so the lookup raises as a real outage would."""

    def __init__(self, *, fail: bool = False, member: bool = False) -> None:
        self.fail = fail
        self.member = member
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, "params": dict(kwargs.get("params") or {}),
                           "headers": dict(kwargs.get("headers") or {})})
        if url.endswith("groups:lookup"):
            return _Response(200, {"name": "groups/0123abcd"})
        if url.endswith(":checkTransitiveMembership"):
            if self.fail:
                return _Response(500, {"error": "backend error"})
            return _Response(200, {"hasMembership": self.member})
        return _Response(403, {"error": "not a call this platform makes"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    return {
        "alice@saga.xyz": (ENG_GROUP,),
        CAROL: (),
        ROOT: (ADMIN_GROUP, ENG_GROUP),
        DANA: (ADMIN_GROUP,),
    }


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def directory() -> RecordingDirectory:
    return RecordingDirectory()


@pytest.fixture
def client(db, tokens, group_map, objects, publisher, directory) -> TestClient:
    context = build_context(
        settings=api_settings(platform_owner=ROOT, allowed_users=(LISTED_BOT,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )
    app = create_app(context)
    app.state.workspace_publisher = publisher
    app.state.workspace_directory = CloudIdentityGroups(PROJECT, session=directory)
    return TestClient(app, raise_server_exceptions=False)


def _post(client: TestClient, body: dict[str, Any], user: str = "root"):
    return client.post(ROUTE, json=body, headers=auth_header(user))


def _workspace_docs(db) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in db.docs.items()
            if k.startswith(("workspaces/", "workspace_ids/"))}


def _audit(db) -> list[dict[str, Any]]:
    """The workspace entries; the owner's seeded admin role writes its own."""
    return [v for k, v in db.docs.items()
            if k.startswith(f"{AUDIT_COLLECTION}/") and k.count("/") == 1
            and "target_workspace_id" in v]


def _code(response) -> str:
    return response.json().get("code")


# --------------------------------------------------------------------------
# the record: kind, absent reads personal
# --------------------------------------------------------------------------

def test_a_record_with_no_kind_reads_personal() -> None:
    record = {"tenant_id": "u-carol", "workspace_id": "w-c0ffee", "state": "requested"}
    assert ws.kind_of(record) == ws.PERSONAL
    assert ws.kind_of(None) == ws.PERSONAL
    assert ws.view(record)["kind"] == ws.PERSONAL
    assert ws.KINDS == (ws.PERSONAL, ws.GROUP, ws.SERVICE)


def test_an_old_record_is_served_as_personal_to_an_admin(client, db) -> None:
    db.docs["workspaces/u-carol"] = {
        "tenant_id": "u-carol", "workspace_id": "w-c0ffee", "principal": CAROL,
        "state": "requested", "request_id": str(uuid.uuid4()), "requested_at": _now(),
        "requested_via": "console", "decision": None, "history": [],
        "limits": dict(ws.DEFAULT_LIMITS), "run": None, "steps": {}, "failure": None,
        "ready_at": None, "migrated": False,
    }
    db.docs["workspace_ids/w-c0ffee"] = {"tenant_id": "u-carol"}
    response = client.get(f"{ROUTE}/w-c0ffee", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    assert response.json()["workspace"]["kind"] == ws.PERSONAL
    listed = client.get(ROUTE, params={"kind": "personal"}, headers=auth_header("root"))
    assert [r["workspace_id"] for r in listed.json()["workspaces"]] == ["w-c0ffee"]
    assert client.get(ROUTE, params={"kind": "group"},
                      headers=auth_header("root")).json()["workspaces"] == []


def test_a_persons_own_request_records_the_personal_kind(client, db) -> None:
    response = client.post("/v1/workspace", json={"via": "console"},
                           headers=auth_header("carol"))
    assert response.status_code == 202, response.text
    record = db.docs["workspaces/u-carol"]
    assert record["kind"] == ws.PERSONAL
    assert record["requested_by"] == CAROL
    assert response.json()["kind"] == ws.PERSONAL


# --------------------------------------------------------------------------
# who may file one
# --------------------------------------------------------------------------

def test_a_non_admin_is_refused_and_nothing_is_written(client, db, publisher) -> None:
    response = _post(client, {"kind": "group", "principal": "reports@saga.xyz"},
                     user="carol")
    assert response.status_code == 403
    assert _workspace_docs(db) == {}
    assert _audit(db) == []
    assert publisher.messages == []


def test_an_extra_field_is_refused(client, db) -> None:
    response = _post(client, {"kind": "group", "principal": "reports@saga.xyz",
                              "image": "evil:latest"})
    assert response.status_code == 422
    response = _post(client, {"kind": "group", "principal": "reports@saga.xyz",
                              "limits": {"max_active": 4, "cpu_request": 64}})
    assert response.status_code == 422
    assert _workspace_docs(db) == {}


# --------------------------------------------------------------------------
# group
# --------------------------------------------------------------------------

def test_an_admins_group_request_is_approved_with_one_audit_row(client, db, publisher,
                                                                directory) -> None:
    response = _post(client, {"kind": "group", "principal": "Reports@saga.xyz",
                              "display_name": "Reports"})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["kind"] == ws.GROUP
    assert body["principal"] == "reports@saga.xyz"
    assert body["display_name"] == "Reports"
    assert body["state"] == ws.APPROVED
    record = db.docs["workspaces/reports"]
    assert record["kind"] == ws.GROUP
    assert record["requested_by"] == ROOT
    assert record["directory"]["readable"] is True
    assert record["limits"] == dict(ws.DEFAULT_LIMITS)
    assert len(_audit(db)) == 1
    assert _audit(db)[0]["target_workspace_id"] == body["workspace_id"]
    assert [m["workspace_id"] for m in publisher.messages] == [body["workspace_id"]]


def test_the_directory_check_asks_one_group_once_with_the_billing_header(client,
                                                                          directory) -> None:
    response = _post(client, {"kind": "group", "principal": "reports@saga.xyz"})
    assert response.status_code == 202, response.text
    checks = [c for c in directory.calls if c["url"].endswith(":checkTransitiveMembership")]
    assert len(checks) == 1
    assert checks[0]["headers"] == {"x-goog-user-project": PROJECT}
    assert ROOT in checks[0]["params"]["query"]
    assert not any("searchTransitiveGroups" in c["url"] for c in directory.calls)
    assert all(c["headers"].get("x-goog-user-project") == PROJECT for c in directory.calls)


def test_an_unreadable_group_is_refused_and_nothing_is_written(client, db, publisher,
                                                               directory) -> None:
    directory.fail = True
    response = _post(client, {"kind": "group", "principal": "reports@saga.xyz"})
    assert response.status_code == 503
    assert _code(response) == "WORKSPACE_GROUP_UNREADABLE"
    assert _workspace_docs(db) == {}
    assert _audit(db) == []
    assert publisher.messages == []
    checks = [c for c in directory.calls if c["url"].endswith(":checkTransitiveMembership")]
    assert len(checks) == 1
    assert checks[0]["headers"] == {"x-goog-user-project": PROJECT}


def test_a_group_whose_id_starts_u_is_reserved(client, db) -> None:
    response = _post(client, {"kind": "group", "principal": "u-eng@saga.xyz"})
    assert response.status_code == 403
    assert _code(response) == "WORKSPACE_GROUP_ID_RESERVED"
    assert _workspace_docs(db) == {}


def test_an_admin_group_is_refused(client, db) -> None:
    response = _post(client, {"kind": "group", "principal": ADMIN_GROUP})
    assert response.status_code == 403
    assert _code(response) == "WORKSPACE_GROUP_IS_ADMIN"
    assert _workspace_docs(db) == {}


def test_a_group_outside_the_allowed_domains_is_refused(client, db) -> None:
    response = _post(client, {"kind": "group", "principal": "eng@partner.example"})
    assert response.status_code == 403
    assert _code(response) == "WORKSPACE_DOMAIN_NOT_ALLOWED"
    assert _workspace_docs(db) == {}


def test_a_terraform_tenants_group_is_refused(client, db) -> None:
    response = _post(client, {"kind": "group", "principal": ENG_GROUP})
    assert response.status_code == 403
    assert _code(response) == "WORKSPACE_ID_TERRAFORM"
    assert _workspace_docs(db) == {}


@pytest.mark.parametrize("existing,requested", [
    ("eng", "eng-git-u-x@saga.xyz"),
    ("ops-git-u-x", "ops@saga.xyz"),
])
def test_nested_ids_collide(client, db, existing, requested) -> None:
    seed_tenant(db, existing)
    response = _post(client, {"kind": "group", "principal": requested})
    assert response.status_code == 403
    assert _code(response) == "WORKSPACE_ID_NESTED"
    assert _workspace_docs(db) == {}


def test_an_id_taken_by_another_principal_is_refused(client, db) -> None:
    seed_tenant(db, "reports")
    db.docs["tenants/reports"]["principal"] = "reports@partner.example"
    response = _post(client, {"kind": "group", "principal": "reports@saga.xyz"})
    assert response.status_code == 403
    assert _code(response) == "WORKSPACE_ID_TAKEN"
    assert _workspace_docs(db) == {}


def test_an_injected_commit_failure_leaves_neither_record_nor_audit(client, db, publisher,
                                                                    monkeypatch) -> None:
    class Failing(FakeTransaction):
        def _commit(self) -> list[Any]:
            raise RuntimeError("injected commit failure")

    monkeypatch.setattr(db, "transaction", lambda **kw: Failing(db), raising=False)
    response = _post(client, {"kind": "group", "principal": "reports@saga.xyz"})
    assert response.status_code == 500
    assert _workspace_docs(db) == {}
    assert _audit(db) == []
    assert publisher.messages == []


def test_a_second_identical_request_is_200_and_writes_nothing(client, db, publisher) -> None:
    first = _post(client, {"kind": "group", "principal": "reports@saga.xyz"})
    assert first.status_code == 202
    before = copy.deepcopy(db.docs)
    messages = len(publisher.messages)
    second = _post(client, {"kind": "group", "principal": "reports@saga.xyz"})
    assert second.status_code == 200, second.text
    assert second.json()["workspace_id"] == first.json()["workspace_id"]
    assert db.docs == before
    assert len(publisher.messages) == messages


# --------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------

def test_a_service_request_by_a_non_owner_admin_waits_for_the_owner(client, db,
                                                                    publisher) -> None:
    response = _post(client, {"kind": "service", "principal": REPORTS_BOT}, user="dana")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["state"] == ws.REQUESTED
    assert body["kind"] == ws.SERVICE
    assert body["display_name"] == "reports-bot"
    tenant_id = ws.personal_tenant_id(REPORTS_BOT)
    record = db.docs[f"workspaces/{tenant_id}"]
    assert record["limits"]["max_active"] == 4
    assert record["limits"]["capacity_units"] == 8
    assert len(_audit(db)) == 1
    assert publisher.messages == []

    workspace_id = body["workspace_id"]
    refused = client.post(f"{ROUTE}/{workspace_id}/approve", headers=auth_header("dana"))
    assert refused.status_code == 403
    assert _code(refused) == "WORKSPACE_APPROVAL_OWNER_ONLY"
    assert db.docs[f"workspaces/{tenant_id}"]["state"] == ws.REQUESTED
    assert publisher.messages == []

    approved = client.post(f"{ROUTE}/{workspace_id}/approve", headers=auth_header("root"))
    assert approved.status_code == 200, approved.text
    assert db.docs[f"workspaces/{tenant_id}"]["state"] == ws.APPROVED
    assert [m["workspace_id"] for m in publisher.messages] == [workspace_id]


def test_the_owners_service_request_is_approved_at_once(client, db, publisher) -> None:
    response = _post(client, {"kind": "service", "principal": REPORTS_BOT})
    assert response.status_code == 202, response.text
    assert response.json()["state"] == ws.APPROVED
    assert len(_audit(db)) == 1


@pytest.mark.parametrize("principal,code", [
    (f"swarm-api@{PROJECT}.iam.gserviceaccount.com", "WORKSPACE_SERVICE_PLATFORM"),
    (f"swarm-workspace-deployer@{PROJECT}.iam.gserviceaccount.com",
     "WORKSPACE_SERVICE_PLATFORM"),
    ("123456789012-compute@developer.gserviceaccount.com", "WORKSPACE_SERVICE_PLATFORM"),
    (LISTED_BOT, "WORKSPACE_SERVICE_ALREADY_RESOLVES"),
    ("reports-bot@another-project.iam.gserviceaccount.com",
     "WORKSPACE_SERVICE_NOT_IN_PROJECT"),
    (CAROL, "WORKSPACE_SERVICE_NOT_IN_PROJECT"),
])
def test_a_service_account_that_may_not_have_one_is_refused(client, db, principal,
                                                            code) -> None:
    response = _post(client, {"kind": "service", "principal": principal})
    assert response.status_code == 403, response.text
    assert _code(response) == code
    assert _workspace_docs(db) == {}


def test_a_service_listed_by_a_tenant_is_refused(db, tokens, group_map, objects) -> None:
    from swarm_common.identity import TenantMember

    member = TenantMember(email=REPORTS_BOT, kind="group", principal=ENG_GROUP,
                          uid="112233445566778899")
    context = build_context(
        settings=api_settings(platform_owner=ROOT, tenant_service_accounts=(member,)),
        db=db, verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, forge_tokens=NoForgeTokens())
    app = create_app(context)
    app.state.workspace_publisher = FakePublisher()
    client = TestClient(app, raise_server_exceptions=False)
    response = _post(client, {"kind": "service", "principal": REPORTS_BOT})
    assert response.status_code == 403
    assert _code(response) == "WORKSPACE_SERVICE_ALREADY_RESOLVES"
    assert _workspace_docs(db) == {}


# --------------------------------------------------------------------------
# personal, named by an admin (Q1 (a))
# --------------------------------------------------------------------------

def test_an_admin_may_file_a_persons_workspace_and_it_is_approved(client, db,
                                                                  publisher) -> None:
    response = _post(client, {"kind": "personal", "principal": CAROL})
    assert response.status_code == 202, response.text
    record = db.docs["workspaces/u-carol"]
    assert record["kind"] == ws.PERSONAL
    assert record["principal"] == CAROL
    assert record["requested_by"] == ROOT
    assert record["state"] == ws.APPROVED
    assert len(publisher.messages) == 1


@pytest.mark.parametrize("principal,code", [
    (REPORTS_BOT, "WORKSPACE_NOT_FOR_SERVICE_ACCOUNTS"),
    ("carol@partner.example", "WORKSPACE_DOMAIN_NOT_ALLOWED"),
])
def test_a_personal_request_for_who_may_not_have_one_is_refused(client, db, principal,
                                                                code) -> None:
    response = _post(client, {"kind": "personal", "principal": principal})
    assert response.status_code == 403
    assert _code(response) == code
    assert _workspace_docs(db) == {}


# --------------------------------------------------------------------------
# privacy
# --------------------------------------------------------------------------

def test_no_response_names_requested_by(client, db) -> None:
    created = _post(client, {"kind": "service", "principal": REPORTS_BOT}, user="dana")
    group = _post(client, {"kind": "group", "principal": "reports@saga.xyz"})
    workspace_id = created.json()["workspace_id"]
    bodies = [
        created.text,
        group.text,
        client.get(ROUTE, headers=auth_header("root")).text,
        client.get(f"{ROUTE}/{workspace_id}", headers=auth_header("root")).text,
    ]
    for text in bodies:
        assert "requested_by" not in text
        assert DANA not in text
    # People serves the audit, whose `by` is an admin's address by W2's
    # design, so only the field is held there.
    assert "requested_by" not in client.get("/v1/admin/people",
                                            headers=auth_header("root")).text
