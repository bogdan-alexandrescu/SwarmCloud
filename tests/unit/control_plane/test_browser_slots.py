"""swarm-api provisions each browser-session secret and grants it per secret
(docs/design/live-browser.md §5.5.1, owner decision 2026-10-11; lane LB-L8c).

No worker holds a project-level `-browser-` grant or `secrets.create`; before it
writes a session it asks `POST /v1/tasks/{id}/browser-session/slot`, and
swarm-api creates the secret and binds that tenant's worker -- alone -- to
secretAccessor and secretVersionAdder on that one secret. These tests hold:

  * a non-personal tenant is refused a slot;
  * a name outside the caller's own prefix is refused, before Secret Manager
    is called;
  * a stale generation is refused, and nothing is created;
  * a granted slot binds exactly one member on exactly one secret;
  * a secret labelled another tenant's is not re-granted;
  * the carry gets the 24-hour TTL and a login its record's expiry.

The route runs over the in-memory Firestore with the attempt key a worker
registers (L9b's fixtures); the vault is an in-memory one at the route, and
the real `SecretManagerSlotVault` over a fake client below it. Every key and
token is built at runtime.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from google.api_core import exceptions as gexc
from google.iam.v1 import policy_pb2

from swarm_api.browser_slots import (
    ACCESSOR_ROLE,
    CARRY_TTL_SECONDS,
    SLOT_ROLES,
    VERSION_ADDER_ROLE,
    BrowserSlotTaken,
    SlotName,
    SecretManagerSlotVault,
    slot_name,
)

from .conftest import PROJECT, auth_header
from .test_browser_logins import (  # noqa: F401 -- fixtures, used by name
    CAROL_TENANT,
    CAROL_WORKER,
    DAVE_TENANT,
    ENG_WORKER,
    TASK,
    Attempt,
    group_map,
    live,
    seed_attempt,
    seed_login,
    tenants,
    vault,
    worker_tokens,
)

CAROL_MEMBER = f"serviceAccount:swarm-agent-worker-{CAROL_TENANT}@{PROJECT}.iam.gserviceaccount.com"
WORKFLOW = "wf_carol_qa"


class FakeSlotVault:
    """What the route asked Secret Manager for: one record per provision."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def provision(self, secret_id: str, **kw: Any) -> bool:
        self.calls.append({"secret_id": secret_id, **kw})
        return len([c for c in self.calls if c["secret_id"] == secret_id]) == 1


@pytest.fixture
def slots(live) -> FakeSlotVault:
    fake = FakeSlotVault()
    live.app.state.browser_slot_vault = fake
    return fake


def slot_post(live, attempt: Attempt, secret_id: str, *, token: str = CAROL_WORKER,
              sign: bool = True, extra: dict[str, Any] | None = None):
    path = f"/v1/tasks/{attempt.tuple.task_id}/browser-session/slot"
    body = json.dumps({**attempt.fields(), "secret_id": secret_id, **(extra or {})}).encode()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if sign:
        headers.update(attempt.sign(path, body))
    return live.post(path, content=body, headers=headers)


def carry_id(tenant: str = CAROL_TENANT, workflow: str = WORKFLOW) -> str:
    return f"swarm-tenant-{tenant}-browser-wf-{workflow}"


def in_workflow(db, workflow: str = WORKFLOW) -> None:
    db.docs[f"tasks/{TASK}"]["workflow_id"] = workflow


# --------------------------------------------------------------------------
# A granted slot: one member, one secret, two roles
# --------------------------------------------------------------------------


def test_a_carry_slot_is_created_with_the_ttl_and_bound_to_the_tenants_worker_alone(
    live, db, slots
) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)
    in_workflow(db)

    response = slot_post(live, attempt, carry_id())

    assert response.status_code == 200, response.text
    assert response.json() == {
        "secret_id": carry_id(), "kind": "carry", "created": True,
        "member": CAROL_MEMBER, "roles": [ACCESSOR_ROLE, VERSION_ADDER_ROLE],
    }
    [call] = slots.calls
    assert call["secret_id"] == carry_id()
    assert call["member"] == CAROL_MEMBER
    assert call["tenant_id"] == CAROL_TENANT
    assert call["ttl_seconds"] == CARRY_TTL_SECONDS == 24 * 3600
    assert call["expire_time"] is None
    assert call["labels"] == {"managed-by": "swarm-api", "swarm-tenant": CAROL_TENANT,
                              "swarm-browser": "carry"}


def test_a_login_slot_takes_the_records_expiry_while_pending_for_this_task_or_active(
    live, db, slots
) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)
    pending = seed_login(db, name="github-qa", state="pending", expires_in=timedelta(days=7))
    pending["pending_task_id"] = TASK
    active = seed_login(db, name="staging", state="active", expires_in=timedelta(days=3))

    for name, doc in (("github-qa", pending), ("staging", active)):
        secret = f"swarm-tenant-{CAROL_TENANT}-browser-login-{name}"
        response = slot_post(live, attempt, secret)
        assert response.status_code == 200, response.text
        assert response.json()["kind"] == "login"
        assert slots.calls[-1]["expire_time"] == doc["expires_at"]
        assert slots.calls[-1]["ttl_seconds"] is None
        assert slots.calls[-1]["labels"]["swarm-browser"] == "login"


def test_a_login_slot_needs_a_live_record_of_this_workspace(live, db, slots) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)
    seed_login(db, name="old-login", expires_in=timedelta(days=-1))
    seed_login(db, name="half-saved", state="pending")  # pending for another task
    seed_login(db, tenant_id=DAVE_TENANT, name="daves")

    expired = slot_post(live, attempt, f"swarm-tenant-{CAROL_TENANT}-browser-login-old-login")
    assert expired.status_code == 410 and expired.json()["code"] == "browser_login_expired"
    for name in ("half-saved", "never-saved", "daves"):
        response = slot_post(live, attempt, f"swarm-tenant-{CAROL_TENANT}-browser-login-{name}")
        assert response.status_code == 404, (name, response.text)
        assert response.json()["code"] == "browser_login_unknown"
    assert slots.calls == []


# --------------------------------------------------------------------------
# Refusals: not personal, outside the prefix, stale, unproven
# --------------------------------------------------------------------------


def test_a_non_personal_tenant_is_refused_a_slot(live, db, slots) -> None:
    attempt = Attempt(tenant="eng", task="task_eng_browser")
    seed_attempt(live, db, attempt, submitted_by="alice@saga.xyz", worker_token=ENG_WORKER)
    db.docs["tasks/task_eng_browser"]["workflow_id"] = WORKFLOW

    response = slot_post(live, attempt, "swarm-tenant-eng-browser-wf-" + WORKFLOW,
                         token=ENG_WORKER)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "browser_login_personal_only"
    assert slots.calls == []


@pytest.mark.parametrize("change", [
    {"kind": "group"},              # a group whose id happens to start u-
    {"principal": "someone-else@saga.xyz"},  # kind user, principal not this id's
])
def test_a_u_tenant_that_is_not_a_persons_is_refused(live, db, slots, change) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)
    in_workflow(db)
    db.docs[f"tenants/{CAROL_TENANT}"].update(change)

    response = slot_post(live, attempt, carry_id())

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "browser_login_personal_only"
    assert slots.calls == []


@pytest.mark.parametrize("secret_id", [
    carry_id(tenant=DAVE_TENANT),                                  # another person's
    "swarm-tenant-eng-browser-wf-" + WORKFLOW,                     # a group's
    f"swarm-tenant-{CAROL_TENANT}-anthropic",                      # her provider key
    f"swarm-tenant-{CAROL_TENANT}-git",                            # her forge token
    f"swarm-tenant-{CAROL_TENANT}-git-u-0123456789abcdef",
    f"swarm-tenant-{CAROL_TENANT}-browser-",
    f"swarm-tenant-{CAROL_TENANT}-browser-other-x",
    f"swarm-tenant-{CAROL_TENANT}-browser-login-Not_A_Slug",
    f"swarm-tenant-{CAROL_TENANT}-browser-wf-{WORKFLOW}/versions/1",
    "agents-staging-browser-login-x",                              # the other team's
    "swarm-github-app-private-key",
    carry_id(workflow="wf_someone_elses"),                         # not this task's workflow
])
def test_a_name_outside_the_callers_prefix_is_refused_before_secret_manager(
    live, db, slots, secret_id
) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)
    in_workflow(db)

    response = slot_post(live, attempt, secret_id)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "browser_slot_outside_prefix"
    assert slots.calls == []


def test_a_carry_slot_outside_a_workflow_is_refused(live, db, slots) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)  # no workflow_id on the task

    response = slot_post(live, attempt, carry_id())

    assert response.status_code == 403
    assert response.json()["code"] == "browser_slot_outside_prefix"
    assert slots.calls == []


def test_a_stale_generation_is_refused_and_nothing_is_created(live, db, slots) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)
    in_workflow(db)
    db.docs[f"tasks/{TASK}"]["current_generation"] = 2

    response = slot_post(live, attempt, carry_id())

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "browser_login_fenced"
    assert slots.calls == []


def test_an_unsigned_foreign_or_personal_call_is_refused(live, db, slots) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)
    in_workflow(db)

    assert slot_post(live, attempt, carry_id(), sign=False).status_code == 403
    assert slot_post(live, attempt, carry_id(), token=ENG_WORKER).status_code == 403
    person = live.post(f"/v1/tasks/{TASK}/browser-session/slot", headers=auth_header("carol"),
                       json={**attempt.fields(), "secret_id": carry_id()})
    assert person.status_code == 403
    assert slots.calls == []


def test_the_slot_takes_no_value_and_no_resource_parameter(live, db, slots) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)
    in_workflow(db)
    for extra in ({"payload": "x" * 16}, {"ttl_seconds": 999_999}, {"member": "allUsers"}):
        response = slot_post(live, attempt, carry_id(), extra=extra)
        assert response.status_code == 422, (extra, response.text)
    assert slots.calls == []


def test_slot_name_reads_only_the_tenants_own_carry_and_login() -> None:
    assert slot_name("u-a", "swarm-tenant-u-a-browser-wf-wf_1").kind == "carry"
    assert slot_name("u-a", "swarm-tenant-u-a-browser-login-github-qa").suffix == "github-qa"
    # A nested tenant id cannot be reached through the shorter one's prefix.
    assert slot_name("u-a", "swarm-tenant-u-a-browser-x-browser-login-y") is None
    assert slot_name("eng", "swarm-tenant-eng-browser-wf-wf_1") is None
    assert slot_name("u-", "swarm-tenant-u--browser-wf-wf_1") is None


# --------------------------------------------------------------------------
# The real vault, over a fake Secret Manager client
# --------------------------------------------------------------------------


class FakeSecretClient:
    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, Any]] = {}
        self.policies: dict[str, policy_pb2.Policy] = {}
        self.calls: list[str] = []

    def create_secret(self, request: dict[str, Any]) -> None:
        self.calls.append("create_secret")
        name = f"{request['parent']}/secrets/{request['secret_id']}"
        if name in self.secrets:
            raise gexc.AlreadyExists("exists")
        self.secrets[name] = request["secret"]
        self.policies[name] = policy_pb2.Policy(etag=b"e0")

    def get_secret(self, request: dict[str, Any]) -> Any:
        self.calls.append("get_secret")
        secret = self.secrets[request["name"]]
        return type("Secret", (), {"labels": secret.get("labels", {})})()

    def get_iam_policy(self, request: dict[str, Any]) -> policy_pb2.Policy:
        self.calls.append("get_iam_policy")
        policy = policy_pb2.Policy()
        policy.CopyFrom(self.policies[request["resource"]])
        return policy

    def set_iam_policy(self, request: dict[str, Any]) -> None:
        self.calls.append("set_iam_policy")
        self.policies[request["resource"]] = request["policy"]


def _bindings(policy: policy_pb2.Policy) -> dict[str, list[str]]:
    return {b.role: list(b.members) for b in policy.bindings}


def test_the_vault_binds_exactly_one_member_on_exactly_one_secret() -> None:
    client = FakeSecretClient()
    other = f"projects/{PROJECT}/secrets/swarm-tenant-u-b-browser-wf-wf_2"
    client.secrets[other] = {"labels": {"swarm-tenant": "u-b"}}
    client.policies[other] = policy_pb2.Policy(
        bindings=[policy_pb2.Binding(role=ACCESSOR_ROLE, members=["serviceAccount:b@x"])]
    )
    vault = SecretManagerSlotVault(PROJECT, client=client)
    member = "serviceAccount:swarm-agent-worker-u-a@p.iam.gserviceaccount.com"

    created = vault.provision("swarm-tenant-u-a-browser-wf-wf_1", tenant_id="u-a", member=member,
                              labels={"swarm-tenant": "u-a"}, ttl_seconds=CARRY_TTL_SECONDS)

    assert created is True
    name = f"projects/{PROJECT}/secrets/swarm-tenant-u-a-browser-wf-wf_1"
    assert client.secrets[name]["ttl"] == {"seconds": CARRY_TTL_SECONDS}
    assert _bindings(client.policies[name]) == {role: [member] for role in SLOT_ROLES}
    assert client.policies[name].etag == b"e0"
    # The other secret's policy is untouched.
    assert _bindings(client.policies[other]) == {ACCESSOR_ROLE: ["serviceAccount:b@x"]}


def test_the_vault_drops_any_other_binding_on_an_existing_secret_of_the_same_tenant() -> None:
    client = FakeSecretClient()
    name = f"projects/{PROJECT}/secrets/swarm-tenant-u-a-browser-login-github-qa"
    client.secrets[name] = {"labels": {"swarm-tenant": "u-a"}}
    client.policies[name] = policy_pb2.Policy(bindings=[
        policy_pb2.Binding(role=ACCESSOR_ROLE, members=["serviceAccount:someone@x", "user:x@y"]),
        policy_pb2.Binding(role="roles/secretmanager.admin", members=["user:x@y"]),
    ])
    member = "serviceAccount:swarm-agent-worker-u-a@p.iam.gserviceaccount.com"

    created = SecretManagerSlotVault(PROJECT, client=client).provision(
        "swarm-tenant-u-a-browser-login-github-qa", tenant_id="u-a", member=member,
        labels={"swarm-tenant": "u-a"}, expire_time=datetime.now(timezone.utc),
    )

    assert created is False
    assert _bindings(client.policies[name]) == {role: [member] for role in SLOT_ROLES}


def test_the_vault_never_regrants_a_secret_labelled_another_tenants() -> None:
    client = FakeSecretClient()
    name = f"projects/{PROJECT}/secrets/swarm-tenant-u-a-browser-login-github-qa"
    client.secrets[name] = {"labels": {"swarm-tenant": "u-a-browser"}}
    client.policies[name] = policy_pb2.Policy()

    with pytest.raises(BrowserSlotTaken):
        SecretManagerSlotVault(PROJECT, client=client).provision(
            "swarm-tenant-u-a-browser-login-github-qa", tenant_id="u-a",
            member="serviceAccount:w@p", labels={"swarm-tenant": "u-a"},
        )
    assert "set_iam_policy" not in client.calls


@pytest.mark.parametrize("secret_id", [
    "swarm-tenant-eng-browser-wf-wf_1",
    "swarm-tenant-u-a-anthropic",
    "agents-staging-browser-login-x",
    "swarm-tenant-u-a-browser-other-x",
])
def test_the_vault_refuses_a_non_browser_name_before_any_call(secret_id) -> None:
    client = FakeSecretClient()
    with pytest.raises(ValueError):
        SecretManagerSlotVault(PROJECT, client=client).provision(
            secret_id, tenant_id="u-a", member="serviceAccount:w@p", labels={},
        )
    assert client.calls == []


# --------------------------------------------------------------------------
# A park's slot (live-browser §5.5.2, issue #1362): this task's own, 24 hours
# --------------------------------------------------------------------------


def park_id(task: str = TASK, tenant: str = CAROL_TENANT) -> str:
    return f"swarm-tenant-{tenant}-browser-park-{task}"


def test_a_park_slot_is_this_tasks_own_with_the_carry_ttl(live, db, slots) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)  # a park needs no workflow

    response = slot_post(live, attempt, park_id())

    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "park"
    [call] = slots.calls
    assert call["secret_id"] == park_id() and call["member"] == CAROL_MEMBER
    assert call["ttl_seconds"] == CARRY_TTL_SECONDS
    assert call["labels"]["swarm-browser"] == "park"


def test_another_tasks_park_slot_is_refused_before_secret_manager(live, db, slots) -> None:
    attempt = Attempt()
    seed_attempt(live, db, attempt)

    response = slot_post(live, attempt, park_id(task="task_someone_elses"))

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "browser_slot_outside_prefix"
    assert slots.calls == []


def test_slot_name_reads_a_park_only_in_the_tenants_own_prefix() -> None:
    assert slot_name("u-a", "swarm-tenant-u-a-browser-park-task_1") == SlotName("park", "task_1")
    assert slot_name("eng", "swarm-tenant-eng-browser-park-task_1") is None
    assert slot_name("u-a", "swarm-tenant-u-a-browser-park-") is None
