"""Named browser logins and the workflow carry, the API half (docs/design/live-browser.md §5.5.1, LB-7).

Lane LB-L9b. Drives the real routes over the in-memory Firestore, with the
attempt key a worker generates and registers exactly as the hand-off path does
(§5.4.1), and an in-memory Secret Manager that stands in for the one swarm-api
destroys secrets in. Each test names the rule of the design it holds:

  * only the workspace's own person can list or revoke its logins; anyone
    else gets 404;
  * a group-tenant task naming `browser_login` is refused with
    `browser_login_personal_only`;
  * a value is never returned, and no route accepts one;
  * save takes a name and an expiry of at most 30 days;
  * revoke destroys every version;
  * a workflow's carry secret is deleted at its terminal state;
  * the audit records names only.

Every key, token and cookie below is built at runtime: nothing here is a
credential.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from swarm_common.identity import tenant_id_for_user

from swarm_api.auth import StaticTokenVerifier
from swarm_api.browser_logins import (
    AUDIT_ENTRIES,
    BROWSER_LOGIN_PERSONAL_ONLY,
    CARRY_REAPED,
    COLLECTION,
    DEFAULT_EXPIRY_DAYS,
    MAX_EXPIRY_DAYS,
    browser_login_message,
    carry_secret_id,
    login_secret_id,
)
from swarm_api.childkey import AttemptTuple, nonce
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.handoff import LIVE_AUDIT, handoff_message
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import PROJECT, api_settings, auth_header, seed_task, seed_tenant

#: Built at runtime, never one literal (CLAUDE.md, "Nothing you add may look
#: like a credential").
KEY = "-".join(("login", "vault", "test", "key")) + "-" + "k" * 24
CAROL = "carol@example.com"
DAVE = "dave@example.com"
CAROL_TENANT = tenant_id_for_user(CAROL)
DAVE_TENANT = tenant_id_for_user(DAVE)
CAROL_WORKER = "-".join(("token", "worker", "carol"))
ENG_WORKER = "-".join(("token", "worker", "eng"))
TASK = "task_carol_browser"
ATTEMPT = "att_carol_1"
LEASE = "lease_carol_1"
#: What a cookie value would look like if one leaked: searched for everywhere.
JAR_MARKER = "jar" + "v" * 12 + "x" * 8


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class FakeVault:
    """Secret Manager as swarm-api sees it: secrets with versions, destroyed by id."""

    def __init__(self) -> None:
        self.secrets: dict[str, list[str]] = {}
        self.calls: list[str] = []

    def put(self, secret_id: str, *states: str) -> None:
        self.secrets[secret_id] = list(states or ("ENABLED",))

    def destroy(self, secret_id: str) -> int:
        self.calls.append(secret_id)
        versions = self.secrets.pop(secret_id, None)
        if versions is None:
            return 0
        return sum(1 for state in versions if state != "DESTROYED")


class Attempt:
    """One attempt: its tuple, and an attempt key generated as a worker would."""

    def __init__(self, tenant: str = CAROL_TENANT, task: str = TASK, attempt: str = ATTEMPT,
                 lease: str = LEASE, generation: int = 1) -> None:
        self.tuple = AttemptTuple(tenant, task, attempt, lease, generation)
        self.private = Ed25519PrivateKey.generate()
        self.public = _b64url(
            self.private.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )

    def fields(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tuple.tenant_id,
            "attempt_id": self.tuple.attempt_id,
            "lease_id": self.tuple.lease_id,
            "generation": self.tuple.generation,
        }

    def sign(self, path: str, body: bytes, *, message=browser_login_message) -> dict[str, str]:
        timestamp = str(int(time.time()))
        signature = self.private.sign(message("POST", path, body, timestamp))
        return {"X-Swarm-Attempt-Proof": _b64url(signature), "X-Swarm-Attempt-Timestamp": timestamp}


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    # Carol and Dave are in no group: each has a personal tenant of their own.
    return {
        "alice@example.com": ("eng@example.com",),
        "bob@example.com": ("research@example.com",),
        CAROL: (),
        DAVE: (),
        "root@example.com": ("swarm-admins@example.com", "eng@example.com"),
    }


@pytest.fixture
def worker_tokens(tokens) -> dict[str, dict[str, Any]]:
    return {
        **tokens,
        CAROL_WORKER: {
            "email": f"swarm-agent-worker-{CAROL_TENANT}@{PROJECT}.iam.gserviceaccount.com",
            "email_verified": True,
            "sub": "1000000000000000000011",
        },
        ENG_WORKER: {
            "email": f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
            "email_verified": True,
            "sub": "1000000000000000000012",
        },
    }


@pytest.fixture
def vault() -> FakeVault:
    return FakeVault()


@pytest.fixture
def live(db, worker_tokens, group_map, objects, vault) -> TestClient:
    ctx = build_context(
        settings=api_settings(child_key=KEY),
        db=db,
        verifier=StaticTokenVerifier(worker_tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )
    app = create_app(ctx)
    app.state.browser_vault = vault
    return TestClient(app, raise_server_exceptions=False)


def personal_tenant(db, tenant_id: str, principal: str) -> None:
    db.docs[f"tenants/{tenant_id}"] = {
        "tenant_id": tenant_id, "kind": "user", "principal": principal,
        "created_at": datetime.now(timezone.utc), "display_name": tenant_id,
        "max_active": 8, "capacity_units": 8, "monthly_budget_usd": None, "enabled": True,
        "credentials": [], "service_account": None, "gcs_prefix": None, "namespace": None,
    }


@pytest.fixture(autouse=True)
def tenants(db) -> None:
    personal_tenant(db, CAROL_TENANT, CAROL)
    personal_tenant(db, DAVE_TENANT, DAVE)
    seed_tenant(db, "eng")


def seed_login(db, *, tenant_id: str = CAROL_TENANT, name: str = "github-qa",
               state: str = "active", expires_in: timedelta = timedelta(days=10),
               site: str | None = "github.com") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    doc = {
        "tenant_id": tenant_id,
        "name": name,
        "state": state,
        "site": site,
        "saved_at": now,
        "saved_by": CAROL,
        "expires_at": now + expires_in,
        "last_used_at": None,
        "refreshed_at": None,
        "pending_task_id": None,
        "secret_id": login_secret_id(tenant_id, name),
        "versions": 1,
    }
    db.docs[f"{COLLECTION}/{tenant_id}:{name}"] = doc
    return doc


def seed_attempt(live, db, attempt: Attempt, *, profile: str = "claude-code-browser",
                 submitted_by: str = CAROL, worker_token: str = CAROL_WORKER) -> dict[str, Any]:
    """Registered while STARTING, then RUNNING: what the worker leaves (§5.4.1)."""
    t = attempt.tuple
    doc = seed_task(db, task_id=t.task_id, tenant_id=t.tenant_id, state="STARTING")
    doc.update({
        "current_lease_id": t.lease_id, "current_generation": t.generation,
        "submitted_by": submitted_by, "runner_profile": profile, "resource_class": "browser",
        "timeout_seconds": 3600, "attempt_count": 1, "started_at": datetime.now(timezone.utc),
    })
    now = datetime.now(timezone.utc)
    db.docs[f"leases/{t.lease_id}"] = {
        "lease_id": t.lease_id, "task_id": t.task_id, "attempt_id": t.attempt_id,
        "tenant_id": t.tenant_id, "generation": t.generation, "pools": [], "units": 2,
        "state": "STARTING", "created_at": now,
        "dispatch_deadline": now + timedelta(minutes=10),
        "expires_at": now + timedelta(minutes=10), "released_at": None,
    }
    response = live.post(
        f"/v1/attempts/{t.attempt_id}/child-key",
        content=json.dumps({
            "tenant_id": t.tenant_id, "task_id": t.task_id, "lease_id": t.lease_id,
            "generation": t.generation, "nonce": nonce(KEY, t), "public_key": attempt.public,
        }).encode(),
        headers={"Authorization": f"Bearer {worker_token}", "Content-Type": "application/json"},
    )
    assert response.status_code == 201, response.text
    doc["state"] = "RUNNING"
    db.docs[f"leases/{t.lease_id}"]["state"] = "RUNNING"
    return doc


def worker_post(live, attempt: Attempt, route: str, extra: dict[str, Any], *,
                token: str = CAROL_WORKER, sign: bool = True, message=browser_login_message):
    path = f"/v1/tasks/{attempt.tuple.task_id}/browser-login/{route}"
    body = json.dumps({**attempt.fields(), **extra}).encode()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if sign:
        headers.update(attempt.sign(path, body, message=message))
    return live.post(path, content=body, headers=headers)


def audit(db) -> list[dict[str, Any]]:
    return [doc for key, doc in db.docs.items()
            if key.startswith(f"{LIVE_AUDIT}/") and doc.get("entry") in AUDIT_ENTRIES]


def everything(db) -> str:
    return json.dumps(db.docs, default=str)


# --------------------------------------------------------------------------
# Only the workspace's own person can list or revoke; anyone else gets 404
# --------------------------------------------------------------------------


def test_the_workspace_person_lists_their_logins_and_never_a_value(live, db) -> None:
    seed_login(db, name="github-qa")
    seed_login(db, name="staging-admin", state="pending", site=None)
    seed_login(db, tenant_id=DAVE_TENANT, name="daves-login")

    response = live.get("/v1/browser-logins", headers=auth_header("carol"))

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [i["name"] for i in items] == ["github-qa", "staging-admin"]
    # A closed set of keys: no value, no secret id, no version payload.
    assert set(items[0]) == {"name", "site", "state", "saved_at", "expires_at",
                             "last_used_at", "refreshed_at"}
    assert items[0]["site"] == "github.com"


@pytest.mark.parametrize("user", ["alice", "bob", "root"])
def test_anyone_outside_a_personal_workspace_gets_404_listing(live, db, user) -> None:
    seed_login(db)
    response = live.get("/v1/browser-logins", headers=auth_header(user))
    assert response.status_code == 404, response.text
    assert "github-qa" not in response.text


def test_another_person_sees_only_their_own_workspace(live, db) -> None:
    seed_login(db)
    response = live.get("/v1/browser-logins", headers=auth_header("dave"))
    assert response.status_code == 200
    assert response.json()["items"] == []


@pytest.mark.parametrize("user", ["alice", "dave", "root"])
def test_anyone_else_revoking_gets_404_and_nothing_is_destroyed(live, db, vault, user) -> None:
    login = seed_login(db)
    vault.put(login["secret_id"], "ENABLED")

    response = live.delete("/v1/browser-logins/github-qa", headers=auth_header(user))

    assert response.status_code == 404, response.text
    assert vault.calls == []
    assert login["secret_id"] in vault.secrets
    assert f"{COLLECTION}/{CAROL_TENANT}:github-qa" in db.docs
    assert audit(db) == []


def test_revoking_a_name_that_does_not_exist_is_404(live, db, vault) -> None:
    response = live.delete("/v1/browser-logins/nothing-here", headers=auth_header("carol"))
    assert response.status_code == 404
    assert vault.calls == []


# --------------------------------------------------------------------------
# Revoke destroys every version
# --------------------------------------------------------------------------


def test_revoke_destroys_every_version_and_the_record(live, db, vault) -> None:
    login = seed_login(db)
    vault.put(login["secret_id"], "ENABLED", "DISABLED", "ENABLED", "DESTROYED")

    response = live.delete("/v1/browser-logins/github-qa", headers=auth_header("carol"))

    assert response.status_code == 200, response.text
    assert response.json() == {"name": "github-qa", "revoked": True, "versions_destroyed": 3}
    assert vault.calls == [login_secret_id(CAROL_TENANT, "github-qa")]
    assert login["secret_id"] not in vault.secrets
    assert f"{COLLECTION}/{CAROL_TENANT}:github-qa" not in db.docs
    [entry] = audit(db)
    assert entry["entry"] == "browser_login_revoked"
    assert entry["email"] == CAROL and entry["tenant_id"] == CAROL_TENANT


def test_a_vault_that_cannot_destroy_keeps_the_record_so_revoke_can_be_retried(
    live, db, vault, monkeypatch
) -> None:
    seed_login(db)

    def broken(secret_id: str) -> int:
        raise RuntimeError("secret manager is down")

    monkeypatch.setattr(vault, "destroy", broken)
    response = live.delete("/v1/browser-logins/github-qa", headers=auth_header("carol"))

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "browser_login_vault_unavailable"
    assert f"{COLLECTION}/{CAROL_TENANT}:github-qa" in db.docs
    assert audit(db) == []


# --------------------------------------------------------------------------
# Save takes a name and an expiry of at most 30 days
# --------------------------------------------------------------------------


def _save(live, body: dict[str, Any], user: str = "carol", task: str = TASK):
    return live.post(f"/v1/tasks/{task}/browser-login", headers=auth_header(user), json=body)


def test_save_records_a_pending_login_with_the_default_expiry(live, db) -> None:
    seed_attempt(live, db, Attempt())
    before = datetime.now(timezone.utc)

    response = _save(live, {"name": "github-qa"})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["name"] == "github-qa" and body["state"] == "pending"
    doc = db.docs[f"{COLLECTION}/{CAROL_TENANT}:github-qa"]
    assert doc["pending_task_id"] == TASK and doc["saved_by"] == CAROL
    days = (doc["expires_at"] - before).total_seconds() / 86400
    assert DEFAULT_EXPIRY_DAYS - 0.01 < days < DEFAULT_EXPIRY_DAYS + 0.01
    [entry] = audit(db)
    assert entry["entry"] == "browser_login_saved" and entry["detail"] == {"name": "github-qa"}


def test_save_takes_an_expiry_of_up_to_30_days_and_no_more(live, db) -> None:
    seed_attempt(live, db, Attempt())
    assert MAX_EXPIRY_DAYS == 30
    ok = _save(live, {"name": "thirty", "expires_in_days": 30})
    assert ok.status_code == 202, ok.text
    for days in (31, 365, 0, -1):
        refused = _save(live, {"name": f"bad-{abs(days)}", "expires_in_days": days})
        assert refused.status_code == 422, (days, refused.text)
        assert f"{COLLECTION}/{CAROL_TENANT}:bad-{abs(days)}" not in db.docs


@pytest.mark.parametrize("name", ["", "Has-Caps", "-leading", "trailing-", "a b", "x" * 41,
                                  "../etc", "a_b"])
def test_save_refuses_a_name_that_is_not_a_plain_slug(live, db, name) -> None:
    seed_attempt(live, db, Attempt())
    assert _save(live, {"name": name}).status_code == 422


def test_save_takes_a_name_and_an_expiry_and_nothing_else(live, db) -> None:
    seed_attempt(live, db, Attempt())
    for extra in ({"cookies": [{"name": "sid", "value": JAR_MARKER}]}, {"value": JAR_MARKER},
                  {"secret_id": "swarm-tenant-eng-browser-login-x"}, {"site": "evil.example"}):
        response = _save(live, {"name": "github-qa", **extra})
        assert response.status_code == 422, (extra, response.text)
        assert JAR_MARKER not in response.text
    assert JAR_MARKER not in everything(db)


def test_save_in_a_group_tenant_is_refused_personal_only(live, db) -> None:
    seed_attempt(live, db, Attempt(tenant="eng", task="task_eng_browser"),
                 submitted_by="alice@example.com", worker_token=ENG_WORKER)
    response = _save(live, {"name": "github-qa"}, user="alice", task="task_eng_browser")
    assert response.status_code == 403, response.text
    assert response.json()["code"] == BROWSER_LOGIN_PERSONAL_ONLY
    assert not [k for k in db.docs if k.startswith(f"{COLLECTION}/")]


def test_save_on_another_workspaces_task_is_404(live, db) -> None:
    seed_attempt(live, db, Attempt())
    response = _save(live, {"name": "github-qa"}, user="dave")
    assert response.status_code == 404
    assert not [k for k in db.docs if k.startswith(f"{COLLECTION}/")]


def test_save_on_a_task_without_a_live_browser_is_refused(live, db) -> None:
    seed_attempt(live, db, Attempt(), profile="claude-code")
    response = _save(live, {"name": "github-qa"})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "browser_login_not_live"


def test_save_on_a_task_that_is_not_running_is_refused(live, db) -> None:
    seed_attempt(live, db, Attempt())
    db.docs[f"tasks/{TASK}"]["state"] = "SUCCEEDED"
    response = _save(live, {"name": "github-qa"})
    assert response.status_code == 409, response.text


# --------------------------------------------------------------------------
# The worker's half: what to store, and that it stored it
# --------------------------------------------------------------------------


def test_the_worker_reads_the_pending_save_and_reports_it_stored(live, db) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)
    assert _save(live, {"name": "github-qa", "expires_in_days": 7}).status_code == 202

    pending = worker_post(live, attempt, "pending", {})
    assert pending.status_code == 200, pending.text
    [item] = pending.json()["items"]
    assert item["name"] == "github-qa"
    assert item["secret_id"] == login_secret_id(CAROL_TENANT, "github-qa")
    assert item["expire_time"]

    stored = worker_post(live, attempt, "report",
                         {"event": "stored", "name": "github-qa", "site": "github.com"})
    assert stored.status_code == 200, stored.text
    doc = db.docs[f"{COLLECTION}/{CAROL_TENANT}:github-qa"]
    assert doc["state"] == "active" and doc["site"] == "github.com"
    assert doc["pending_task_id"] is None
    assert worker_post(live, attempt, "pending", {}).json()["items"] == []


def test_an_unsigned_or_foreign_worker_call_is_refused_and_writes_nothing(live, db) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)
    _save(live, {"name": "github-qa"})
    before = dict(db.docs[f"{COLLECTION}/{CAROL_TENANT}:github-qa"])

    unsigned = worker_post(live, attempt, "report", {"event": "stored", "name": "github-qa"},
                           sign=False)
    assert unsigned.status_code == 403
    # A hand-off signature is not a browser-login signature.
    crossed = worker_post(live, attempt, "report", {"event": "stored", "name": "github-qa"},
                          message=handoff_message)
    assert crossed.status_code == 403
    foreign = worker_post(live, attempt, "pending", {}, token=ENG_WORKER)
    assert foreign.status_code == 403
    person = live.post(f"/v1/tasks/{TASK}/browser-login/pending", headers=auth_header("carol"),
                       json=attempt.fields())
    assert person.status_code == 403
    assert db.docs[f"{COLLECTION}/{CAROL_TENANT}:github-qa"] == before


def test_a_stale_generation_is_fenced(live, db) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)
    db.docs[f"tasks/{TASK}"]["current_generation"] = 2
    response = worker_post(live, attempt, "report", {"event": "used", "name": "github-qa"})
    assert response.status_code == 409, response.text


def test_the_worker_report_never_takes_a_value(live, db) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)
    seed_login(db)
    response = worker_post(live, attempt, "report",
                           {"event": "refreshed", "name": "github-qa", "value": JAR_MARKER})
    assert response.status_code == 422
    assert JAR_MARKER not in response.text
    assert JAR_MARKER not in everything(db)


def test_a_named_login_is_used_only_while_it_is_live(live, db) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)
    seed_login(db, name="github-qa")
    seed_login(db, name="old-login", expires_in=timedelta(days=-1))
    seed_login(db, name="half-saved", state="pending")

    used = worker_post(live, attempt, "report", {"event": "used", "name": "github-qa"})
    assert used.status_code == 200, used.text
    assert used.json()["secret_id"] == login_secret_id(CAROL_TENANT, "github-qa")
    assert db.docs[f"{COLLECTION}/{CAROL_TENANT}:github-qa"]["last_used_at"] is not None

    expired = worker_post(live, attempt, "report", {"event": "used", "name": "old-login"})
    assert expired.status_code == 410 and expired.json()["code"] == "browser_login_expired"
    unknown = worker_post(live, attempt, "report", {"event": "used", "name": "never-saved"})
    assert unknown.status_code == 404 and unknown.json()["code"] == "browser_login_unknown"
    pending = worker_post(live, attempt, "report", {"event": "used", "name": "half-saved"})
    assert pending.status_code == 404 and pending.json()["code"] == "browser_login_unknown"


def test_a_group_tenants_worker_has_no_login_to_use(live, db) -> None:
    attempt = Attempt(tenant="eng", task="task_eng_browser")
    seed_attempt(live, db, attempt, submitted_by="alice@example.com", worker_token=ENG_WORKER)
    seed_login(db, tenant_id="eng", name="github-qa")
    response = worker_post(live, attempt, "report", {"event": "used", "name": "github-qa"},
                           token=ENG_WORKER)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == BROWSER_LOGIN_PERSONAL_ONLY


# --------------------------------------------------------------------------
# A group-tenant task naming browser_login is refused at submission
# --------------------------------------------------------------------------


def test_a_group_tenant_task_naming_browser_login_is_refused(live, db) -> None:
    response = live.post("/v1/tasks", headers=auth_header("alice"), json={
        "runner_profile": "claude-code-browser",
        "input": {"prompt": "check the dashboard", "browser_login": "github-qa"},
    })
    assert response.status_code == 422, response.text
    assert response.json()["code"] == BROWSER_LOGIN_PERSONAL_ONLY
    assert not [k for k, d in db.docs.items() if k.startswith("tasks/") and d["tenant_id"] == "eng"]


def test_a_group_tenant_workflow_step_naming_browser_login_is_refused(live, db) -> None:
    response = live.post("/v1/workflows", headers=auth_header("alice"), json={"steps": [
        {"step_id": "look", "runner_profile": "claude-code-browser",
         "input": {"prompt": "look", "browser_login": "github-qa"}},
    ]})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == BROWSER_LOGIN_PERSONAL_ONLY
    assert response.json()["detail"]["step_id"] == "look"


def test_a_personal_tenant_task_naming_browser_login_is_not_refused_as_personal_only(
    live, db
) -> None:
    response = live.post("/v1/tasks", headers=auth_header("carol"), json={
        "runner_profile": "claude-code-browser",
        "input": {"prompt": "check the dashboard", "browser_login": "github-qa"},
    })
    # Whatever else the catalogue says of the key, it is not this refusal.
    assert response.json().get("code") != BROWSER_LOGIN_PERSONAL_ONLY, response.text


# --------------------------------------------------------------------------
# A workflow's carry secret is deleted at its terminal state
# --------------------------------------------------------------------------


def seed_workflow(db, workflow_id: str, tenant_id: str, state: str) -> None:
    now = datetime.now(timezone.utc)
    db.docs[f"workflows/{workflow_id}"] = {
        "workflow_id": workflow_id, "tenant_id": tenant_id, "created_at": now - timedelta(hours=2),
        "updated_at": now, "state": state, "submitted_by": CAROL, "steps": [],
        "on_step_failure": "fail_workflow", "priority": 0, "cancel_requested": False,
    }


def test_the_carry_secret_is_deleted_when_the_workflow_ends(live, db, vault) -> None:
    seed_workflow(db, "wf_done", CAROL_TENANT, "SUCCEEDED")
    seed_workflow(db, "wf_failed", CAROL_TENANT, "FAILED")
    seed_workflow(db, "wf_running", CAROL_TENANT, "RUNNING")
    seed_workflow(db, "wf_eng", "eng", "SUCCEEDED")
    for wf in ("wf_done", "wf_failed", "wf_running"):
        vault.put(carry_secret_id(CAROL_TENANT, wf), "ENABLED")

    response = live.post("/v1/admin/browser-logins/carry-sweep", headers=auth_header("root"))

    assert response.status_code == 200, response.text
    assert response.json()["reaped"] == 2
    assert carry_secret_id(CAROL_TENANT, "wf_done") not in vault.secrets
    assert carry_secret_id(CAROL_TENANT, "wf_failed") not in vault.secrets
    assert carry_secret_id(CAROL_TENANT, "wf_running") in vault.secrets
    # A group tenant never carries, so its workflows are never even named.
    assert not [c for c in vault.calls if "-eng-" in c]
    assert f"{CARRY_REAPED}/wf_done" in db.docs

    # Once reaped, a later sweep does not ask again.
    vault.calls.clear()
    again = live.post("/v1/admin/browser-logins/carry-sweep", headers=auth_header("root"))
    assert again.json()["reaped"] == 0
    assert carry_secret_id(CAROL_TENANT, "wf_done") not in vault.calls


def _finished(live, task_id: str, tenant_id: str):
    envelope = {"message": {"attributes": {"reason": "task_finished", "task_id": task_id,
                                           "tenant_id": tenant_id}}}
    return live.post("/v1/admin/tasks/finished", headers=auth_header("root"), json=envelope)


def test_the_carry_is_deleted_on_the_push_that_ends_the_workflow(live, db, vault) -> None:
    now = datetime.now(timezone.utc)
    seed_workflow(db, "wf_two", CAROL_TENANT, "RUNNING")
    db.docs["workflows/wf_two"]["steps"] = [
        {"step_id": "a", "runner_profile": "claude-code-browser", "input": {},
         "depends_on": [], "task_id": "task_a"},
        {"step_id": "b", "runner_profile": "claude-code-browser", "input": {},
         "depends_on": ["a"], "task_id": "task_b"},
    ]
    for task_id, state in (("task_a", "SUCCEEDED"), ("task_b", "RUNNING")):
        doc = seed_task(db, task_id=task_id, tenant_id=CAROL_TENANT, state=state,
                        workflow_id="wf_two", runner_profile="claude-code-browser")
        doc.update({"step_id": task_id[-1], "submitted_by": CAROL, "completed_at": now})
    secret = carry_secret_id(CAROL_TENANT, "wf_two")
    vault.put(secret, "ENABLED")

    # The first step ends: the workflow is still running, its carry is kept.
    assert _finished(live, "task_a", CAROL_TENANT).status_code == 200
    assert secret in vault.secrets

    db.docs["tasks/task_b"]["state"] = "SUCCEEDED"
    assert _finished(live, "task_b", CAROL_TENANT).status_code == 200
    assert secret not in vault.secrets
    assert f"{CARRY_REAPED}/wf_two" in db.docs


def test_a_push_for_a_group_tenants_workflow_names_no_carry(live, db, vault) -> None:
    seed_workflow(db, "wf_eng_done", "eng", "SUCCEEDED")
    seed_task(db, task_id="task_eng_done", tenant_id="eng", state="SUCCEEDED",
              workflow_id="wf_eng_done")
    assert _finished(live, "task_eng_done", "eng").status_code == 200
    assert vault.calls == []


def test_the_carry_sweep_is_not_a_members_route(live, db) -> None:
    assert live.post("/v1/admin/browser-logins/carry-sweep",
                     headers=auth_header("carol")).status_code == 403


# --------------------------------------------------------------------------
# The audit records names only
# --------------------------------------------------------------------------


def test_the_audit_records_names_only(live, db, vault) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)
    assert _save(live, {"name": "github-qa"}).status_code == 202
    assert worker_post(live, attempt, "report",
                       {"event": "stored", "name": "github-qa", "site": "github.com"}).status_code == 200
    assert worker_post(live, attempt, "report",
                       {"event": "used", "name": "github-qa"}).status_code == 200
    assert worker_post(live, attempt, "report",
                       {"event": "refreshed", "name": "github-qa"}).status_code == 200
    vault.put(login_secret_id(CAROL_TENANT, "github-qa"), "ENABLED", "ENABLED")
    assert live.delete("/v1/browser-logins/github-qa", headers=auth_header("carol")).status_code == 200

    entries = audit(db)
    assert sorted(e["entry"] for e in entries) == sorted(
        ["browser_login_saved", "browser_login_used", "browser_login_refreshed",
         "browser_login_revoked"]
    )
    for entry in entries:
        assert entry["detail"] == {"name": "github-qa"}, entry
        assert "secret_id" not in entry and "site" not in entry
    assert JAR_MARKER not in everything(db)
