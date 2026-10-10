"""Admin → People and the approval flow (docs/workspaces.md §1.3, §2.1-§2.2,
§6.3-§6.4; #847, lane W7).

Every route against the shipped app over an in-memory Firestore, with a
recording publisher and an in-memory account pool on `app.state` -- nothing
patched. What is held:

* the list: teams, GitHub, workspace state, Claude account source, last
  activity, pending first; never `decision.by` and never the tenant id;
* approve (incl. a self-approval, audited), deny (reason required and shown to
  the person), retry, the ceiling, lend and reclaim, the sweep;
* every route is admin-only, and the sweep is also the swarm-tick identity's;
* every change writes an `admin_audit` entry;
* the publish payload is exactly the workspace id, the mode and the request id
  -- never an email, never the tenant id; a failed publish is not a failed
  approval;
* no log line names an email or a personal tenant id.

The people (conftest): `root` is an admin (and in eng), `alice` is in eng,
`carol` has only her personal tenant.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import people as people_mod
from swarm_api import publish_workspace as pw
from swarm_api import workspaces as ws
from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.settings import ApiSettings
from swarm_api.waker import NullWaker

from .conftest import NoForgeTokens, api_settings, auth_header, seed_tenant

CAROL = "carol@saga.xyz"
ROOT = "root@saga.xyz"
CAROL_WS = "w-c0ffee"
SWEEPER = "swarm-tick@saga-agents-staging.iam.gserviceaccount.com"
SWEEPER_HEADERS = {"Authorization": "Bearer token-sweeper"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FakePublisher:
    """Records every message; `ok` decides what a publish answers."""

    def __init__(self, *, ok: bool = True, enabled: bool = True) -> None:
        self.ok = ok
        self.enabled = enabled
        self.messages: list[dict[str, str]] = []

    def publish(self, workspace_id: str, mode: str, request_id: str) -> bool:
        # The real builder, so a payload the real publisher would refuse is
        # refused here too.
        self.messages.append(pw.message_data(workspace_id, mode, request_id))
        return self.ok


class FakePool:
    """The broker's two account routes People uses, in memory."""

    def __init__(self, *accounts: dict[str, Any]) -> None:
        self.accounts = {a["account_id"]: dict(a) for a in accounts}
        self.calls: list[tuple] = []

    def list_accounts(self, tenant_id: str) -> dict[str, Any]:
        self.calls.append(("list_accounts", tenant_id))
        return {"accounts": [dict(a) for a in self.accounts.values()
                             if a["owner_tenant"] == tenant_id or tenant_id in a["lend_to"]]}

    def set_lending(self, account_id: str, *, lend_to: list[str]) -> dict[str, Any]:
        self.calls.append(("set_lending", account_id, tuple(lend_to)))
        self.accounts[account_id]["lend_to"] = list(lend_to)
        return {"account": dict(self.accounts[account_id])}


def _pool_account(account_id: str, owner: str, *, lend_to=()) -> dict[str, Any]:
    return {"account_id": account_id, "owner_tenant": owner, "label": account_id,
            "provider": "anthropic", "state": "AVAILABLE", "lend_to": list(lend_to)}


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def pool() -> FakePool:
    return FakePool()


@pytest.fixture
def app_context(db, tokens, group_map, objects):
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    return build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )


@pytest.fixture
def client(app_context, publisher, pool) -> TestClient:
    app = create_app(app_context)
    app.state.workspace_publisher = publisher
    app.state.account_pool = pool
    return TestClient(app, raise_server_exceptions=False)


def _record(db, *, tenant_id: str = "u-carol", workspace_id: str = CAROL_WS,
            principal: str = CAROL, **fields) -> dict:
    doc = {
        "tenant_id": tenant_id, "workspace_id": workspace_id, "principal": principal,
        "state": "requested", "request_id": str(uuid.uuid4()), "requested_at": _now(),
        "requested_via": "console", "decision": None, "history": [],
        "limits": dict(ws.DEFAULT_LIMITS), "run": None, "steps": {}, "failure": None,
        "ready_at": None, "migrated": False,
    }
    doc.update(fields)
    db.docs[f"workspaces/{tenant_id}"] = doc
    db.docs[f"workspace_ids/{workspace_id}"] = {"tenant_id": tenant_id}
    return doc


def _rec(db, tenant_id: str = "u-carol") -> dict:
    return db.docs[f"workspaces/{tenant_id}"]


def _personal_tenant(db, tenant_id: str = "u-carol", principal: str = CAROL) -> None:
    db.docs[f"tenants/{tenant_id}"] = {
        "tenant_id": tenant_id, "kind": "user", "principal": principal,
        "created_at": _now(), "display_name": tenant_id, "max_active": 8,
        "capacity_units": 8, "monthly_budget_usd": None, "enabled": True,
        "credentials": [], "service_account": None, "gcs_prefix": None, "namespace": None,
    }
    db.docs[f"pools/tenant:{tenant_id}"] = {
        "name": f"tenant:{tenant_id}", "hard_limit": 8, "adaptive_target": None,
        "quota_derived_limit": None, "active": 0, "enabled": True, "updated_at": _now()}


def _audit(db, action: str | None = None) -> list[dict]:
    entries = [v for k, v in db.docs.items()
               if k.startswith(f"{AUDIT_COLLECTION}/") and k.count("/") == 1]
    return [e for e in entries if action is None or e.get("action") == action]


def _post(client, path: str, user: str = "root", **kw):
    return client.post(path, headers=auth_header(user), **kw)


# --------------------------------------------------------------------------
# the publish payload
# --------------------------------------------------------------------------

def test_the_message_is_the_workspace_id_the_mode_and_the_request_id_and_nothing_else() -> None:
    rid = str(uuid.uuid4())
    assert pw.message_data(CAROL_WS, "create", rid) == {
        "workspace_id": CAROL_WS, "mode": "create", "request_id": rid}


@pytest.mark.parametrize("workspace_id", [CAROL, "u-carol", "w-C0FFEE", "w-c0ffee0", ""])
def test_the_message_refuses_anything_but_an_opaque_workspace_id(workspace_id) -> None:
    with pytest.raises(ValueError):
        pw.message_data(workspace_id, "create", str(uuid.uuid4()))


def test_the_message_refuses_an_unknown_mode_and_a_request_id_that_is_not_a_uuid() -> None:
    with pytest.raises(ValueError):
        pw.message_data(CAROL_WS, "destroy", str(uuid.uuid4()))
    with pytest.raises(ValueError):
        pw.message_data(CAROL_WS, "create", CAROL)


class _Future:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def result(self, timeout: float | None = None) -> str:
        if self._error is not None:
            raise self._error
        return "1"


class _Client:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, bytes, dict]] = []
        self._error = error

    def publish(self, topic: str, data: bytes, **attributes: str) -> _Future:
        self.sent.append((topic, data, attributes))
        return _Future(self._error)


def test_the_pubsub_publisher_sends_the_three_fields_to_the_full_topic_path_without_attributes() -> None:
    fake = _Client()
    publisher = pw.PubSubWorkspacePublisher("swarm-workspace-apply", project_id="proj",
                                            publisher=fake)
    rid = str(uuid.uuid4())
    assert publisher.publish(CAROL_WS, "limits", rid) is True
    [(topic, data, attributes)] = fake.sent
    assert topic == "projects/proj/topics/swarm-workspace-apply"
    assert json.loads(data) == {"workspace_id": CAROL_WS, "mode": "limits", "request_id": rid}
    assert attributes == {}
    assert b"@" not in data and b"u-" not in data


def test_a_failed_publish_answers_false_and_never_raises(caplog) -> None:
    publisher = pw.PubSubWorkspacePublisher(
        "t", publisher=_Client(RuntimeError(f"denied for {CAROL}")))
    with caplog.at_level(logging.WARNING):
        assert publisher.publish(CAROL_WS, "create", str(uuid.uuid4())) is False
    assert CAROL not in caplog.text


def test_publishing_is_off_unless_the_setting_turns_it_on() -> None:
    off = api_settings()
    assert off.workspace_apply_topic == "swarm-workspace-apply"
    assert off.workspace_apply_publish is False
    assert isinstance(pw.publisher_for(off), pw.NullWorkspacePublisher)
    on = pw.publisher_for(api_settings(workspace_apply_publish=True))
    assert isinstance(on, pw.PubSubWorkspacePublisher)
    assert on.topic.endswith("/topics/swarm-workspace-apply")


def test_the_topic_and_the_switch_are_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_ID", "proj")
    monkeypatch.setenv("WORKSPACE_APPLY_TOPIC", "other-topic")
    monkeypatch.setenv("WORKSPACE_APPLY_PUBLISH", "true")
    settings = ApiSettings.from_env()
    assert settings.workspace_apply_topic == "other-topic"
    assert settings.workspace_apply_publish is True
    monkeypatch.delenv("WORKSPACE_APPLY_TOPIC")
    monkeypatch.delenv("WORKSPACE_APPLY_PUBLISH")
    settings = ApiSettings.from_env()
    assert (settings.workspace_apply_topic, settings.workspace_apply_publish) == (
        "swarm-workspace-apply", False)


# --------------------------------------------------------------------------
# approve
# --------------------------------------------------------------------------

def test_an_admin_approves_a_request_and_the_approval_publishes_the_workspace_id(
        client, db, publisher) -> None:
    record = _record(db)
    response = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/approve")
    assert response.status_code == 200, response.text
    stored = _rec(db)
    assert stored["state"] == "approved"
    assert stored["decision"]["verdict"] == "approved"
    assert stored["decision"]["by"] == ROOT
    assert publisher.messages == [
        {"workspace_id": CAROL_WS, "mode": "create", "request_id": record["request_id"]}]
    assert stored["run"]["published_at"] is not None and stored["run"]["mode"] == "create"
    assert stored["dispatch"]["attempts"] == 1 and stored["dispatch"]["last_ok"] is True
    body = response.json()
    assert body["dispatch"] == {"published": True, "mode": "create", "reason": None}
    assert body["workspace"]["state"] == "approved"
    # The admin's address never leaves Firestore, and the tenant id is a name.
    assert ROOT not in response.text and "u-carol" not in response.text
    [entry] = _audit(db, "approve")
    assert entry["target_workspace_id"] == CAROL_WS and entry["by"] == ROOT
    assert entry["detail"]["self_approval"] is False


def test_the_payload_never_carries_an_email_or_the_tenant_id(client, db, publisher) -> None:
    _record(db)
    _post(client, f"/v1/admin/workspaces/{CAROL_WS}/approve")
    [message] = publisher.messages
    assert set(message) == {"workspace_id", "mode", "request_id"}
    flat = json.dumps(message)
    assert "@" not in flat and "carol" not in flat and "u-" not in flat


def test_an_admin_may_approve_their_own_request_and_the_audit_says_so(client, db) -> None:
    root_tenant = ws.personal_tenant_id(ROOT)
    _record(db, tenant_id=root_tenant, workspace_id="w-00ab12", principal=ROOT)
    response = _post(client, "/v1/admin/workspaces/w-00ab12/approve")
    assert response.status_code == 200, response.text
    assert _rec(db, root_tenant)["state"] == "approved"
    [entry] = _audit(db, "approve")
    assert entry["detail"]["self_approval"] is True


@pytest.mark.parametrize("user", ["alice", "carol"])
def test_a_non_admin_cannot_approve_and_nothing_changes(client, db, publisher, user) -> None:
    _record(db)
    response = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/approve", user=user)
    assert response.status_code == 403
    assert _rec(db)["state"] == "requested"
    assert publisher.messages == [] and _audit(db) == []


def test_the_sweeper_identity_cannot_approve(client, db) -> None:
    _record(db)
    response = client.post(f"/v1/admin/workspaces/{CAROL_WS}/approve", headers=SWEEPER_HEADERS)
    assert response.status_code == 403
    assert _rec(db)["state"] == "requested"


@pytest.mark.parametrize("state", ["approved", "applying", "ready", "needs_owner", "failed"])
def test_approve_is_refused_outside_requested_and_denied(client, db, publisher, state) -> None:
    _record(db, state=state, decision={"by": ROOT, "at": _now(), "verdict": "approved",
                                       "reason": None})
    response = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/approve")
    assert response.status_code == 409
    assert response.json()["code"] == "WORKSPACE_WRONG_STATE"
    assert _rec(db)["state"] == state and publisher.messages == []


def test_a_denied_record_can_be_approved_at_any_time_and_keeps_the_denial(client, db) -> None:
    denial = {"by": ROOT, "at": _now(), "verdict": "denied", "reason": "use eng"}
    _record(db, state="denied", decision=denial)
    response = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/approve")
    assert response.status_code == 200, response.text
    stored = _rec(db)
    assert stored["state"] == "approved"
    assert stored["history"][-1]["decision"]["reason"] == "use eng"


@pytest.mark.parametrize("workspace_id", ["w-ffffff", "u-carol", "nothing"])
def test_an_unknown_workspace_id_is_one_404(client, db, workspace_id) -> None:
    _record(db)
    response = _post(client, f"/v1/admin/workspaces/{workspace_id}/approve")
    assert response.status_code == 404
    assert response.json()["code"] == "WORKSPACE_NOT_FOUND"


def test_a_failed_publish_is_not_a_failed_approval(client, db, publisher) -> None:
    publisher.ok = False
    _record(db)
    response = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/approve")
    assert response.status_code == 200
    assert response.json()["dispatch"] == {
        "published": False, "mode": "create", "reason": "publish_failed"}
    stored = _rec(db)
    assert stored["state"] == "approved"
    assert stored["dispatch"]["attempts"] == 1 and stored["dispatch"]["last_ok"] is False
    assert stored["run"] is None


def test_with_publishing_off_the_approval_stands_and_no_attempt_is_recorded(
        client, db, publisher) -> None:
    publisher.enabled = False
    _record(db)
    response = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/approve")
    assert response.status_code == 200
    assert response.json()["dispatch"]["reason"] == "publishing_off"
    assert publisher.messages == []
    assert _rec(db)["state"] == "approved" and "dispatch" not in _rec(db)


# --------------------------------------------------------------------------
# deny
# --------------------------------------------------------------------------

def test_a_denial_records_the_reason_and_the_person_is_shown_it(client, db, publisher) -> None:
    _record(db)
    response = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/deny",
                     json={"reason": "Please use the eng team space."})
    assert response.status_code == 200, response.text
    stored = _rec(db)
    assert stored["state"] == "denied"
    assert stored["decision"]["reason"] == "Please use the eng team space."
    assert publisher.messages == []
    seen = client.get("/v1/workspace", headers=auth_header("carol")).json()
    assert seen["state"] == "denied"
    assert seen["decision"]["reason"] == "Please use the eng team space."
    assert seen["request_again_at"] is not None
    [entry] = _audit(db, "deny")
    assert entry["target_workspace_id"] == CAROL_WS
    assert "Please" not in json.dumps(entry["detail"])


def test_after_a_denial_the_person_waits_a_day_to_ask_again(client, db) -> None:
    _record(db)
    _post(client, f"/v1/admin/workspaces/{CAROL_WS}/deny", json={"reason": "not now"})
    again = client.post("/v1/workspace", headers=auth_header("carol"), json={"via": "console"})
    assert again.status_code == 403
    assert again.json()["code"] == "WORKSPACE_REQUEST_TOO_SOON"


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "},
                                  {"reason": "x" * 501}])
def test_a_denial_needs_a_reason_of_at_most_500_characters(client, db, body) -> None:
    _record(db)
    response = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/deny", json=body)
    assert response.status_code == 422
    assert _rec(db)["state"] == "requested" and _audit(db) == []


def test_a_failed_record_can_be_denied_and_its_approval_is_kept(client, db) -> None:
    approval = {"by": ROOT, "at": _now(), "verdict": "approved", "reason": None}
    _record(db, state="failed", decision=approval,
            failure={"step": "A7", "code": "NAMESPACE_APPLY_FAILED", "retryable": True,
                     "at": _now()})
    response = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/deny", json={"reason": "no"})
    assert response.status_code == 200
    assert _rec(db)["history"][-1]["decision"]["verdict"] == "approved"


@pytest.mark.parametrize("state", ["approved", "applying", "ready", "denied"])
def test_deny_is_refused_outside_requested_and_failed(client, db, state) -> None:
    _record(db, state=state)
    response = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/deny", json={"reason": "no"})
    assert response.status_code == 409
    assert _rec(db)["state"] == state


def test_a_non_admin_cannot_deny(client, db) -> None:
    _record(db)
    response = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/deny", user="alice",
                     json={"reason": "no"})
    assert response.status_code == 403
    assert _rec(db)["state"] == "requested"


# --------------------------------------------------------------------------
# retry
# --------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["failed", "needs_owner"])
def test_a_retry_returns_the_record_to_approved_with_a_fresh_request_id_and_publishes(
        client, db, publisher, state) -> None:
    approval = {"by": ROOT, "at": _now(), "verdict": "approved", "reason": None}
    failure = {"step": "A7", "code": "CLUSTER_UNREACHABLE", "retryable": True, "at": _now()}
    old_request_id = _record(db, state=state, decision=approval, failure=failure)["request_id"]
    response = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/retry")
    assert response.status_code == 200, response.text
    stored = _rec(db)
    assert stored["state"] == "approved"
    assert stored["request_id"] != old_request_id
    uuid.UUID(stored["request_id"])
    assert stored["failure"] is None
    assert stored["history"][-1]["failure"]["code"] == "CLUSTER_UNREACHABLE"
    assert stored["retry"]["by"] == ROOT
    assert publisher.messages == [
        {"workspace_id": CAROL_WS, "mode": "create", "request_id": stored["request_id"]}]
    [entry] = _audit(db, "retry")
    assert entry["detail"]["previous_request_id"] == old_request_id


@pytest.mark.parametrize("state", ["requested", "approved", "applying", "ready", "denied"])
def test_retry_is_refused_outside_failed_and_needs_owner(client, db, publisher, state) -> None:
    _record(db, state=state)
    response = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/retry")
    assert response.status_code == 409
    assert publisher.messages == []


def test_a_failed_record_nobody_approved_is_not_started_by_a_retry(client, db, publisher) -> None:
    _record(db, state="failed", decision=None)
    response = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/retry")
    assert response.status_code == 409
    assert publisher.messages == [] and _rec(db)["state"] == "failed"


def test_a_non_admin_cannot_retry(client, db, publisher) -> None:
    _record(db, state="failed", decision={"by": ROOT, "at": _now(), "verdict": "approved"})
    assert _post(client, f"/v1/admin/workspaces/{CAROL_WS}/retry", user="carol").status_code == 403
    assert publisher.messages == []


# --------------------------------------------------------------------------
# the ceiling
# --------------------------------------------------------------------------

def test_the_ceiling_sets_max_active_and_capacity_units_together_and_runs_limits_when_ready(
        client, db, publisher) -> None:
    record = _record(db, state="ready")
    _personal_tenant(db)
    response = client.put(f"/v1/admin/workspaces/{CAROL_WS}/limits",
                          headers=auth_header("root"), json={"max_active": 12})
    assert response.status_code == 200, response.text
    assert _rec(db)["limits"] == {"max_active": 12, "capacity_units": 12,
                                  "quota_pods": 24, "quota_cpu": 96}
    tenant = db.docs["tenants/u-carol"]
    assert (tenant["max_active"], tenant["capacity_units"]) == (12, 12)
    assert db.docs["pools/tenant:u-carol"]["hard_limit"] == 12
    assert publisher.messages == [
        {"workspace_id": CAROL_WS, "mode": "limits", "request_id": record["request_id"]}]
    assert _rec(db)["state"] == "ready", "swarm-api never moves a ready record"
    assert response.json()["tenant_written"] is True
    [entry] = _audit(db, "limits")
    assert entry["detail"]["limits"]["max_active"] == 12
    assert entry["detail"]["previous"]["max_active"] == 8


def test_a_ceiling_before_ready_changes_the_record_and_starts_no_run(client, db, publisher) -> None:
    _record(db, state="requested")
    response = client.put(f"/v1/admin/workspaces/{CAROL_WS}/limits",
                          headers=auth_header("root"), json={"max_active": 3})
    assert response.status_code == 200, response.text
    assert _rec(db)["limits"]["capacity_units"] == 3
    assert response.json()["tenant_written"] is False and response.json()["dispatch"] is None
    assert publisher.messages == []
    assert "tenants/u-carol" not in db.docs


@pytest.mark.parametrize("body", [{"max_active": 0}, {"max_active": 101}, {},
                                  {"max_active": 4, "capacity_units": 99},
                                  {"max_active": 4, "image": "evil"}])
def test_the_ceiling_is_one_bounded_number_and_nothing_else(client, db, body) -> None:
    _record(db, state="ready")
    response = client.put(f"/v1/admin/workspaces/{CAROL_WS}/limits",
                          headers=auth_header("root"), json=body)
    assert response.status_code == 422
    assert _rec(db)["limits"] == dict(ws.DEFAULT_LIMITS)


def test_a_non_admin_cannot_change_a_ceiling(client, db) -> None:
    _record(db, state="ready")
    response = client.put(f"/v1/admin/workspaces/{CAROL_WS}/limits",
                          headers=auth_header("carol"), json={"max_active": 50})
    assert response.status_code == 403
    assert _rec(db)["limits"] == dict(ws.DEFAULT_LIMITS)


# --------------------------------------------------------------------------
# lend and reclaim
# --------------------------------------------------------------------------

def _loan(client, account_id: str, lend: bool, user: str = "root"):
    return client.put(f"/v1/admin/people/{CAROL_WS}/loan", headers=auth_header(user),
                      json={"account_id": account_id, "lend": lend})


def test_an_admin_lends_a_group_account_and_the_loan_request_is_closed(client, db, pool) -> None:
    seed_tenant(db, "eng")
    pool.accounts["acct-eng"] = _pool_account("acct-eng", "eng", lend_to=("research",))
    _record(db, state="ready")
    client.post("/v1/workspace/loan-request", headers=auth_header("carol"))
    response = _loan(client, "acct-eng", True)
    assert response.status_code == 200, response.text
    assert pool.accounts["acct-eng"]["lend_to"] == ["research", "u-carol"]
    assert response.json()["changed"] is True
    loan = db.docs["loan_requests/u-carol"]
    assert loan["state"] == "lent" and loan["decision"]["account_id"] == "acct-eng"
    [entry] = _audit(db, "lend")
    assert entry["target_workspace_id"] == CAROL_WS
    assert entry["detail"] == {"account_id": "acct-eng", "owner_tenant": "eng"}


def test_an_admin_reclaims_an_account_and_other_borrowers_keep_it(client, db, pool) -> None:
    seed_tenant(db, "eng")
    pool.accounts["acct-eng"] = _pool_account("acct-eng", "eng", lend_to=("research", "u-carol"))
    _record(db, state="ready")
    response = _loan(client, "acct-eng", False)
    assert response.status_code == 200, response.text
    assert pool.accounts["acct-eng"]["lend_to"] == ["research"]
    assert len(_audit(db, "reclaim")) == 1


def test_an_admin_may_lend_an_account_their_own_personal_tenant_owns(client, db, pool) -> None:
    root_tenant = ws.personal_tenant_id(ROOT)
    pool.accounts["acct-root"] = _pool_account("acct-root", root_tenant)
    _record(db, state="ready")
    response = _loan(client, "acct-root", True)
    assert response.status_code == 200, response.text
    assert pool.accounts["acct-root"]["lend_to"] == ["u-carol"]


def test_an_admin_cannot_lend_another_persons_account(client, db, pool) -> None:
    pool.accounts["acct-dave"] = _pool_account("acct-dave", "u-dave")
    _record(db, state="ready")
    response = _loan(client, "acct-dave", True)
    assert response.status_code == 404
    assert response.json()["code"] == "ACCOUNT_NOT_LENDABLE"
    assert not [c for c in pool.calls if c[0] == "set_lending"]
    assert _audit(db) == []


def test_a_persons_own_account_is_not_lendable_or_reclaimable_here(client, db, pool) -> None:
    # The person IS the admin here: root's own account, root's own workspace.
    root_tenant = ws.personal_tenant_id(ROOT)
    pool.accounts["acct-root"] = _pool_account("acct-root", root_tenant)
    db.docs.pop("workspace_ids/" + CAROL_WS, None)
    _record(db, tenant_id=root_tenant, workspace_id=CAROL_WS, principal=ROOT, state="ready")
    response = _loan(client, "acct-root", False)
    assert response.status_code == 409
    assert response.json()["code"] == "ACCOUNT_IS_THEIRS"
    assert not [c for c in pool.calls if c[0] == "set_lending"]


def test_lending_twice_changes_nothing_and_writes_no_second_audit(client, db, pool) -> None:
    seed_tenant(db, "eng")
    pool.accounts["acct-eng"] = _pool_account("acct-eng", "eng")
    _record(db, state="ready")
    _loan(client, "acct-eng", True)
    again = _loan(client, "acct-eng", True)
    assert again.status_code == 200 and again.json()["changed"] is False
    assert len([c for c in pool.calls if c[0] == "set_lending"]) == 1
    assert len(_audit(db, "lend")) == 1


def test_a_non_admin_cannot_lend(client, db, pool) -> None:
    seed_tenant(db, "eng")
    pool.accounts["acct-eng"] = _pool_account("acct-eng", "eng")
    _record(db, state="ready")
    assert _loan(client, "acct-eng", True, user="alice").status_code == 403
    assert pool.accounts["acct-eng"]["lend_to"] == []


# --------------------------------------------------------------------------
# the sweep
# --------------------------------------------------------------------------

def test_the_sweep_publishes_approved_records_not_attempted_in_ten_minutes(
        client, db, publisher) -> None:
    fresh = _record(db, tenant_id="u-a", workspace_id="w-00000a", principal="a@saga.xyz",
                    state="approved")
    stale = _record(db, tenant_id="u-b", workspace_id="w-00000b", principal="b@saga.xyz",
                    state="approved",
                    dispatch={"attempts": 1, "last_attempt_at": _now() - timedelta(minutes=11),
                              "last_ok": True})
    _record(db, tenant_id="u-c", workspace_id="w-00000c", principal="c@saga.xyz",
            state="approved",
            dispatch={"attempts": 1, "last_attempt_at": _now() - timedelta(minutes=2),
                      "last_ok": True})
    _record(db, tenant_id="u-d", workspace_id="w-00000d", principal="d@saga.xyz",
            state="applying")
    response = _post(client, "/v1/admin/workspaces/sweep")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["considered"], body["published"], body["recent"]) == (3, 2, 1)
    assert sorted(m["workspace_id"] for m in publisher.messages) == ["w-00000a", "w-00000b"]
    assert {m["request_id"] for m in publisher.messages} == {fresh["request_id"],
                                                              stale["request_id"]}
    assert _rec(db, "u-b")["dispatch"]["attempts"] == 2
    # And a second sweep at once publishes nothing: each was just attempted.
    publisher.messages.clear()
    assert _post(client, "/v1/admin/workspaces/sweep").json()["published"] == 0
    assert publisher.messages == []


def test_the_swarm_tick_identity_may_call_the_sweep(client, db, publisher) -> None:
    _record(db, state="approved")
    response = client.post("/v1/admin/workspaces/sweep", headers=SWEEPER_HEADERS)
    assert response.status_code == 200, response.text
    assert [m["workspace_id"] for m in publisher.messages] == [CAROL_WS]


def test_an_ordinary_member_cannot_call_the_sweep(client, db, publisher) -> None:
    _record(db, state="approved")
    assert _post(client, "/v1/admin/workspaces/sweep", user="carol").status_code == 403
    assert publisher.messages == []


def test_with_publishing_off_the_sweep_says_so_and_publishes_nothing(client, db, publisher) -> None:
    publisher.enabled = False
    _record(db, state="approved")
    body = _post(client, "/v1/admin/workspaces/sweep").json()
    assert body["publishing"] is False and body["published"] == 0
    assert publisher.messages == []
    # No attempt is recorded; the one write is the stuck report's dedupe stamp.
    assert "dispatch" not in _rec(db)


def test_the_sweep_publishes_at_most_its_limit_per_call(db, monkeypatch) -> None:
    monkeypatch.setattr(people_mod, "SWEEP_LIMIT", 2)
    for i in range(4):
        _record(db, tenant_id=f"u-p{i}", workspace_id=f"w-00010{i}",
                principal=f"p{i}@saga.xyz", state="approved")
    publisher = FakePublisher()
    report = people_mod.People(db, publisher=publisher, now=_now).sweep()
    assert report["published"] == 2 and len(publisher.messages) == 2


# --------------------------------------------------------------------------
# the list
# --------------------------------------------------------------------------

def _person(db, tenant_id: str, email: str, *, teams=(), seen: datetime | None = None) -> None:
    db.docs[f"people/{tenant_id}"] = {"principal": email, "first_seen": seen or _now(),
                                      "last_seen": seen or _now(), "teams": list(teams)}


def test_the_list_shows_everyone_with_teams_github_workspace_account_and_activity(
        client, db, pool) -> None:
    seed_tenant(db, "eng")
    long_ago = _now() - timedelta(days=2)
    _person(db, "u-alice", "alice@saga.xyz", teams=("eng",), seen=long_ago)
    _person(db, "u-carol", CAROL, seen=_now() - timedelta(minutes=3))
    _person(db, "u-bob", "bob@saga.xyz", teams=("research",), seen=_now())
    _record(db, state="requested")
    _record(db, tenant_id="u-alice", workspace_id="w-a11ce0", principal="alice@saga.xyz",
            state="ready", decision={"by": ROOT, "at": _now(), "verdict": "approved",
                                     "reason": None})
    db.docs["accounts/acct-alice"] = _pool_account("acct-alice", "u-alice")
    db.docs["accounts/acct-eng"] = _pool_account("acct-eng", "eng", lend_to=("u-bob",))
    db.docs["git_tokens/t1"] = {"tenant_id": "u-alice", "scope": "user",
                                "user": "alice@saga.xyz", "state": "active"}
    pool.accounts["acct-eng"] = _pool_account("acct-eng", "eng", lend_to=("u-bob",))

    response = client.get("/v1/admin/people", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    body = response.json()
    rows = {r["email"]: r for r in body["people"]}
    assert set(rows) == {"alice@saga.xyz", CAROL, "bob@saga.xyz"}
    assert body["count"] == 3 and body["pending"] == 1
    assert body["people"][0]["email"] == CAROL, "pending requests sort first"
    assert body["people"][1]["email"] == "bob@saga.xyz", "then the most recently active"

    alice = rows["alice@saga.xyz"]
    assert alice["teams"] == ["eng"]
    assert alice["github"] == "connected"
    assert alice["workspace"]["state"] == "ready"
    assert alice["workspace"]["workspace_id"] == "w-a11ce0"
    assert alice["claude_account"]["source"] == "own"
    assert alice["last_active"] == long_ago.isoformat()

    bob = rows["bob@saga.xyz"]
    assert bob["workspace"]["state"] == "none"
    assert bob["claude_account"]["source"] == "lent"
    assert bob["claude_account"]["lent_by"] == ["eng"]
    assert bob["github"] == "none"

    assert rows[CAROL]["claude_account"]["source"] == "none"
    assert rows[CAROL]["workspace"]["state"] == "requested"

    assert [a["account_id"] for a in body["lendable_accounts"]] == ["acct-eng"]
    # decision.by never leaves Firestore; the tenant ids are names.
    assert ROOT not in json.dumps(body["people"])
    assert all("tenant_id" not in r["workspace"] for r in body["people"])


def test_the_list_carries_the_last_audit_entries_newest_first(client, db) -> None:
    _record(db)
    _post(client, f"/v1/admin/workspaces/{CAROL_WS}/deny", json={"reason": "no"})
    _record(db, tenant_id="u-z", workspace_id="w-00000f", principal="z@saga.xyz")
    _post(client, "/v1/admin/workspaces/w-00000f/approve")
    audit = client.get("/v1/admin/people", headers=auth_header("root")).json()["audit"]
    assert [e["action"] for e in audit[:2]] == ["approve", "deny"]


def test_the_list_is_served_when_the_broker_does_not_answer(client, db, pool) -> None:
    def broken(tenant_id: str) -> dict:
        raise RuntimeError("broker down")

    pool.list_accounts = broken  # type: ignore[method-assign]
    _person(db, "u-carol", CAROL)
    body = client.get("/v1/admin/people", headers=auth_header("root")).json()
    assert body["lendable_accounts"] is None and body["lendable_error"]
    assert [r["email"] for r in body["people"]] == [CAROL]


@pytest.mark.parametrize("user", ["alice", "carol"])
def test_a_non_admin_cannot_list_people(client, db, user) -> None:
    _person(db, "u-carol", CAROL)
    assert client.get("/v1/admin/people", headers=auth_header(user)).status_code == 403


def test_the_sweeper_cannot_list_people(client, db) -> None:
    assert client.get("/v1/admin/people", headers=SWEEPER_HEADERS).status_code == 403


# --------------------------------------------------------------------------
# logs name the workspace id, never a person
# --------------------------------------------------------------------------

def test_no_log_line_names_an_email_or_a_personal_tenant_id(client, db, pool, caplog) -> None:
    seed_tenant(db, "eng")
    pool.accounts["acct-eng"] = _pool_account("acct-eng", "eng")
    _record(db)
    with caplog.at_level(logging.DEBUG, logger="swarm_api"):
        _post(client, f"/v1/admin/workspaces/{CAROL_WS}/approve")
        db.docs["workspaces/u-carol"]["state"] = "failed"
        _post(client, f"/v1/admin/workspaces/{CAROL_WS}/retry")
        db.docs["workspaces/u-carol"]["state"] = "ready"
        client.put(f"/v1/admin/workspaces/{CAROL_WS}/limits", headers=auth_header("root"),
                   json={"max_active": 4})
        _loan(client, "acct-eng", True)
        db.docs["workspaces/u-carol"]["state"] = "requested"
        _post(client, f"/v1/admin/workspaces/{CAROL_WS}/deny", json={"reason": "no"})
        _post(client, "/v1/admin/workspaces/sweep")
        client.get("/v1/admin/people", headers=auth_header("root"))
    people_lines = [r.getMessage() for r in caplog.records
                    if r.name in ("swarm_api.people", "swarm_api.publish_workspace")]
    assert any(CAROL_WS in line for line in people_lines), "the sweep of log lines ran"
    for line in people_lines:
        assert "@" not in line, line
        assert "u-carol" not in line, line


# --------------------------------------------------------------------------
# an approved record nothing advances (the 2026-10-09 incident, w-752763)
# --------------------------------------------------------------------------

def _approved(db, *, minutes_ago: float, **fields) -> dict:
    at = _now() - timedelta(minutes=minutes_ago)
    return _record(db, state="approved", requested_at=at,
                   decision={"by": ROOT, "at": at, "verdict": "approved", "reason": None},
                   **fields)


def _stuck_entries(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == "workspace_stuck"]


def test_with_publishing_off_the_sweep_still_reports_an_approved_record_as_stuck(
        client, db, publisher, caplog) -> None:
    publisher.enabled = False
    _approved(db, minutes_ago=19 * 60)
    caplog.set_level(logging.INFO, logger="swarm_api")
    body = _post(client, "/v1/admin/workspaces/sweep").json()
    assert body["publishing"] is False
    assert (body["considered"], body["stuck"], body["stuck_reported"]) == (1, 1, 1)
    assert body["stuck_reasons"] == {"publishing_off": 1}
    [entry] = _stuck_entries(caplog)
    assert entry.workspace_id == CAROL_WS and entry.reason == "publishing_off"
    assert 19 * 60 - 1 <= entry.minutes_waiting <= 19 * 60
    assert isinstance(entry.approved_at, str)
    assert isinstance(_rec(db)["stuck_reported_at"], datetime)


def test_the_stuck_entry_is_one_json_line_with_the_contracts_fields_and_no_person(
        client, db, publisher, caplog) -> None:
    from swarm_common.logging_setup import CloudLoggingFormatter

    publisher.enabled = False
    _approved(db, minutes_ago=30)
    caplog.set_level(logging.INFO, logger="swarm_api")
    _post(client, "/v1/admin/workspaces/sweep")
    [entry] = _stuck_entries(caplog)
    payload = json.loads(CloudLoggingFormatter().format(entry))
    assert payload["event"] == "workspace_stuck"
    assert {"workspace_id", "reason", "approved_at", "minutes_waiting"} <= set(payload)
    line = json.dumps(payload)
    assert CAROL not in line and "u-carol" not in line


def test_a_stuck_record_is_reported_at_most_once_an_hour(client, db, publisher, caplog) -> None:
    publisher.enabled = False
    _approved(db, minutes_ago=120)
    caplog.set_level(logging.INFO, logger="swarm_api")
    assert _post(client, "/v1/admin/workspaces/sweep").json()["stuck_reported"] == 1
    second = _post(client, "/v1/admin/workspaces/sweep").json()
    # Still stuck, and counted so, but not reported again within the hour.
    assert (second["stuck"], second["stuck_reported"]) == (1, 0)
    assert len(_stuck_entries(caplog)) == 1
    _rec(db)["stuck_reported_at"] = _now() - timedelta(minutes=61)
    assert _post(client, "/v1/admin/workspaces/sweep").json()["stuck_reported"] == 1
    assert len(_stuck_entries(caplog)) == 2


def test_with_publishing_on_a_record_never_dispatched_for_15_minutes_is_stuck(
        client, db, publisher, caplog) -> None:
    _approved(db, minutes_ago=20)
    _approved(db, tenant_id="u-a", workspace_id="w-00000a", principal="a@saga.xyz",
              minutes_ago=5)
    caplog.set_level(logging.INFO, logger="swarm_api")
    body = _post(client, "/v1/admin/workspaces/sweep").json()
    assert body["publishing"] is True and body["stuck_reasons"] == {"never_dispatched": 1}
    assert [e.workspace_id for e in _stuck_entries(caplog)] == [CAROL_WS]
    # Both are still sent: a report never replaces the dispatch.
    assert body["published"] == 2


def test_with_publishing_on_a_dispatch_unclaimed_for_30_minutes_is_stuck(
        client, db, publisher, caplog) -> None:
    rec = _approved(db, minutes_ago=90)
    rec["dispatch"] = {"attempts": 5, "request_id": rec["request_id"],
                       "first_attempt_at": _now() - timedelta(minutes=40),
                       "last_attempt_at": _now() - timedelta(minutes=2), "last_ok": True}
    young = _approved(db, tenant_id="u-a", workspace_id="w-00000a", principal="a@saga.xyz",
                      minutes_ago=90)
    young["dispatch"] = {"attempts": 1, "request_id": young["request_id"],
                         "first_attempt_at": _now() - timedelta(minutes=20),
                         "last_attempt_at": _now() - timedelta(minutes=2), "last_ok": True}
    caplog.set_level(logging.INFO, logger="swarm_api")
    body = _post(client, "/v1/admin/workspaces/sweep").json()
    assert body["stuck_reasons"] == {"dispatched_unclaimed": 1}
    assert [e.workspace_id for e in _stuck_entries(caplog)] == [CAROL_WS]


def test_an_attempt_for_an_earlier_request_is_not_a_dispatch_of_this_one() -> None:
    now = _now()
    record = {"state": "approved", "request_id": "new",
              "decision": {"verdict": "approved", "at": now - timedelta(minutes=20)},
              "dispatch": {"request_id": "old", "first_attempt_at": now - timedelta(hours=3),
                           "last_attempt_at": now - timedelta(hours=3)}}
    assert ws.waiting_because(record, publishing=True, now=now) == "never_dispatched"


@pytest.mark.parametrize("state", ["requested", "applying", "needs_owner", "ready", "failed"])
def test_only_an_approved_record_is_ever_stuck(state) -> None:
    now = _now()
    record = {"state": state, "requested_at": now - timedelta(days=2)}
    assert ws.waiting_because(record, publishing=False, now=now) is None
    assert ws.waiting_because(record, publishing=True, now=now) is None


def test_the_first_attempt_is_kept_across_attempts_and_restarts_with_a_new_request(
        db) -> None:
    clock = {"t": _now()}
    rec = _record(db, state="approved")
    people = people_mod.People(db, publisher=FakePublisher(), now=lambda: clock["t"])
    first = clock["t"]
    people._dispatch(rec, pw.MODE_CREATE)
    clock["t"] = first + timedelta(minutes=11)
    people._dispatch(rec, pw.MODE_CREATE)
    stored = _rec(db)["dispatch"]
    assert stored["first_attempt_at"] == first and stored["last_attempt_at"] == clock["t"]
    assert stored["request_id"] == rec["request_id"]
    retried = {**rec, "request_id": str(uuid.uuid4())}
    _rec(db)["request_id"] = retried["request_id"]
    clock["t"] = first + timedelta(minutes=50)
    people._dispatch(retried, pw.MODE_CREATE)
    assert _rec(db)["dispatch"]["first_attempt_at"] == clock["t"]


# -- the provisioning block and the approval's answer ------------------------------

def test_the_persons_view_says_an_approval_waits_because_publishing_is_off(
        client, db, publisher) -> None:
    publisher.enabled = False
    _approved(db, minutes_ago=42)
    body = client.get("/v1/workspace", headers=auth_header("carol")).json()
    assert body["provisioning"]["available"] is False
    assert body["provisioning"]["waiting_because"] == "publishing_off"
    assert 41 <= body["provisioning"]["approved_minutes_ago"] <= 42
    assert set(body["provisioning"]) == {"available", "waiting_because", "approved_minutes_ago"}


def test_the_persons_view_names_an_unclaimed_dispatch_and_nothing_for_a_moving_one(
        client, db, publisher) -> None:
    rec = _approved(db, minutes_ago=60)
    rec["dispatch"] = {"attempts": 3, "request_id": rec["request_id"],
                       "first_attempt_at": _now() - timedelta(minutes=55),
                       "last_attempt_at": _now() - timedelta(minutes=5), "last_ok": True}
    body = client.get("/v1/workspace", headers=auth_header("carol")).json()
    assert body["provisioning"]["available"] is True
    assert body["provisioning"]["waiting_because"] == "dispatched_unclaimed"
    rec["dispatch"]["first_attempt_at"] = _now() - timedelta(minutes=3)
    body = client.get("/v1/workspace", headers=auth_header("carol")).json()
    assert body["provisioning"]["waiting_because"] is None


def test_a_view_with_no_record_or_one_not_approved_carries_the_block_without_a_wait(
        client, db, publisher) -> None:
    publisher.enabled = False
    body = client.get("/v1/workspace", headers=auth_header("carol")).json()
    assert body["provisioning"] == {"available": False, "waiting_because": None,
                                    "approved_minutes_ago": None}
    _record(db, state="ready")
    body = client.get("/v1/workspace", headers=auth_header("carol")).json()
    assert body["provisioning"]["waiting_because"] is None


def test_with_publishing_off_the_approval_says_it_was_not_sent_for_building(
        client, db, publisher) -> None:
    publisher.enabled = False
    _record(db)
    body = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/approve").json()
    assert body["sent_for_building"] is False
    assert "NOT sent for building" in body["message"]
    assert "provisioning is off" in body["message"]
    assert body["workspace"]["provisioning"]["waiting_because"] == "publishing_off"
    assert _rec(db)["state"] == "approved"


def test_with_publishing_on_the_approval_says_it_was_sent(client, db, publisher) -> None:
    _record(db)
    body = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/approve").json()
    assert body["sent_for_building"] is True and body["message"] == people_mod.SENT
    assert body["workspace"]["provisioning"] == {
        "available": True, "waiting_because": None, "approved_minutes_ago": 0}


def test_the_people_list_carries_the_provisioning_figures_for_its_banner(
        client, db, publisher) -> None:
    publisher.enabled = False
    _approved(db, minutes_ago=10)
    body = client.get("/v1/admin/people", headers=auth_header("root")).json()
    assert body["provisioning"] == {"available": False, "approved_waiting": 1}
    row = next(r for r in body["people"] if r["email"] == CAROL)
    assert row["workspace"]["provisioning"]["waiting_because"] == "publishing_off"
