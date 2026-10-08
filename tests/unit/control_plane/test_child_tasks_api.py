"""Child tasks, the API half (docs/design/child-tasks.md §3.1, §3.2, §3.4, §6).

Drives the real routes over the in-memory Firestore, with a worker identity
that is a token the static verifier maps to the tenant's worker service
account, and an attempt key generated here exactly as the worker generates
one. Each test names the rule of the design it holds.
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

from swarm_api.auth import StaticTokenVerifier
from swarm_api.childkey import CHILD_KEYS, AttemptTuple, ChildKeys, nonce, request_message
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import PROJECT, api_settings, auth_header, seed_task, seed_tenant

KEY = "test-child-key-not-a-real-secret"
WORKER_TOKEN = "token-worker-eng"
OTHER_WORKER_TOKEN = "token-worker-research"
PARENT = "task_parent"
ATTEMPT = "att_parent_1"
LEASE = "lease_parent_1"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class Attempt:
    """One parent attempt: its tuple, and an attempt key generated as a worker would."""

    def __init__(self, tenant: str = "eng", task: str = PARENT, attempt: str = ATTEMPT,
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

    def sign(self, method: str, path: str, body: bytes, *, at: float | None = None) -> dict[str, str]:
        timestamp = str(int(at if at is not None else time.time()))
        signature = self.private.sign(request_message(method, path, body, timestamp))
        return {"X-Swarm-Attempt-Proof": _b64url(signature), "X-Swarm-Attempt-Timestamp": timestamp}


@pytest.fixture
def worker_tokens(tokens) -> dict[str, dict[str, Any]]:
    return {
        **tokens,
        WORKER_TOKEN: {
            "email": f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
            "email_verified": True,
            "sub": "1000000000000000000001",
        },
        OTHER_WORKER_TOKEN: {
            "email": f"swarm-agent-worker-research@{PROJECT}.iam.gserviceaccount.com",
            "email_verified": True,
            "sub": "1000000000000000000002",
        },
    }


def _client(db, worker_tokens, group_map, objects, **settings: Any) -> TestClient:
    ctx = build_context(
        settings=api_settings(**{"child_key": KEY, **settings}),
        db=db,
        verifier=StaticTokenVerifier(worker_tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


@pytest.fixture
def child_client(db, worker_tokens, group_map, objects) -> TestClient:
    return _client(db, worker_tokens, group_map, objects)


def seed_parent(db, attempt: Attempt, *, state: str = "RUNNING", **extra: Any) -> dict[str, Any]:
    t = attempt.tuple
    doc = seed_task(db, task_id=t.task_id, tenant_id=t.tenant_id, state=state)
    doc.update(
        {
            "current_lease_id": t.lease_id,
            "current_generation": t.generation,
            "submitted_by": "alice@saga.xyz",
            "priority": 7,
            "repository_url": "https://github.com/acme/widgets.git",
            "repository_ref": "main",
            "timeout_seconds": 600,
            "attempt_count": 1,
            **extra,
        }
    )
    db.docs[f"leases/{t.lease_id}"] = {
        "lease_id": t.lease_id,
        "task_id": t.task_id,
        "attempt_id": t.attempt_id,
        "tenant_id": t.tenant_id,
        "generation": t.generation,
        "pools": [],
        "units": 1,
        "state": state,
        "created_at": datetime.now(timezone.utc),
        "dispatch_deadline": datetime.now(timezone.utc) + timedelta(minutes=10),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "released_at": None,
    }
    return doc


def register(client, db, attempt: Attempt, *, token: str = WORKER_TOKEN,
             body: dict[str, Any] | None = None) -> Any:
    t = attempt.tuple
    payload = body or {
        "tenant_id": t.tenant_id,
        "task_id": t.task_id,
        "lease_id": t.lease_id,
        "generation": t.generation,
        "nonce": nonce(KEY, t),
        "public_key": attempt.public,
    }
    return client.post(
        f"/v1/attempts/{t.attempt_id}/child-key",
        content=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )


def ready_parent(client, db, attempt: Attempt, **extra: Any) -> dict[str, Any]:
    """A parent registered while STARTING, then RUNNING: what the worker leaves."""
    doc = seed_parent(db, attempt, state="STARTING", **extra)
    response = register(client, db, attempt)
    assert response.status_code == 201, response.text
    doc["state"] = "RUNNING"
    return doc


def submit(client, attempt: Attempt, request_id: str = "split-1", *,
           child: dict[str, Any] | None = None, token: str = WORKER_TOKEN,
           sign: bool = True, at: float | None = None, task: str | None = None) -> Any:
    task_id = task or attempt.tuple.task_id
    body = json.dumps(
        {**attempt.fields(), "request_id": request_id,
         "child": child or {"runner_profile": "mock", "input": {"prompt": "help"}}}
    ).encode()
    path = f"/v1/tasks/{task_id}/children"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if sign:
        headers.update(attempt.sign("POST", path, body, at=at))
    return client.post(path, content=body, headers=headers)


def _children(db, parent: str = PARENT) -> dict[str, dict[str, Any]]:
    return {
        path: doc for path, doc in db.docs.items()
        if path.startswith("tasks/") and path.count("/") == 1 and doc.get("parent_task_id") == parent
    }


# --------------------------------------------------------------------------
# §6.1a: the registration
# --------------------------------------------------------------------------


def test_a_worker_registers_its_key_once_while_starting(child_client, db):
    seed_tenant(db, "eng")
    attempt = Attempt()
    seed_parent(db, attempt, state="STARTING")
    response = register(child_client, db, attempt)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body == {"registered": "key", "attempt_id": ATTEMPT}
    # Nothing secret in the answer: not the nonce, not the attestation, not the id.
    text = response.text
    keys = ChildKeys(KEY)
    reg_id = keys.registration_ids(attempt.tuple)[0]
    stored = db.docs[f"{CHILD_KEYS}/{reg_id}"]
    for secret in (nonce(KEY, attempt.tuple), reg_id, stored["attestation"], KEY):
        assert secret not in text
    assert stored["public_key"] == attempt.public
    assert keys.attestation_holds(attempt.tuple, stored)

    again = register(child_client, db, attempt)
    assert again.status_code == 409 and again.json()["code"] == "child_key_taken"


def test_registration_is_refused_once_the_task_is_running(child_client, db):
    seed_tenant(db, "eng")
    attempt = Attempt()
    seed_parent(db, attempt, state="RUNNING")
    response = register(child_client, db, attempt)
    assert response.status_code == 409
    assert response.json()["code"] == "child_key_window_closed"
    assert not [p for p in db.docs if p.startswith(f"{CHILD_KEYS}/")]


def test_a_wrong_nonce_is_unproven(child_client, db):
    seed_tenant(db, "eng")
    attempt = Attempt()
    seed_parent(db, attempt, state="STARTING")
    t = attempt.tuple
    body = {"tenant_id": "eng", "task_id": PARENT, "lease_id": LEASE, "generation": 1,
            "nonce": nonce("some-other-key", t), "public_key": attempt.public}
    response = register(child_client, db, attempt, body=body)
    assert response.status_code == 403 and response.json()["code"] == "child_key_unproven"


def test_a_nonce_for_another_generation_is_unproven(child_client, db):
    seed_tenant(db, "eng")
    attempt = Attempt()
    seed_parent(db, attempt, state="STARTING")
    stale = AttemptTuple("eng", PARENT, ATTEMPT, LEASE, 2)
    body = {"tenant_id": "eng", "task_id": PARENT, "lease_id": LEASE, "generation": 1,
            "nonce": nonce(KEY, stale), "public_key": attempt.public}
    response = register(child_client, db, attempt, body=body)
    assert response.status_code == 403


@pytest.mark.parametrize("token", ["token-alice", "token-root", OTHER_WORKER_TOKEN])
def test_only_the_tenants_worker_identity_may_register(child_client, db, token):
    """A person has no attempt; another tenant's worker is another tenant."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    seed_parent(db, attempt, state="STARTING")
    response = register(child_client, db, attempt, token=token)
    assert response.status_code == 403
    assert response.json()["code"] == "child_submit_unauthenticated"


def test_an_unprotected_worker_spends_the_slot_on_a_tombstone(child_client, db):
    """§5 F12: the nonce the agent can read later is worthless."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    seed_parent(db, attempt, state="STARTING")
    t = attempt.tuple
    body = {"tenant_id": "eng", "task_id": PARENT, "lease_id": LEASE, "generation": 1,
            "nonce": nonce(KEY, t), "key": None, "refused": "worker_unprotected"}
    assert register(child_client, db, attempt, body=body).status_code == 201
    # The agent, holding the nonce, registers its own key: the slot is taken.
    assert register(child_client, db, attempt).json()["code"] == "child_key_taken"
    db.docs[f"tasks/{PARENT}"]["state"] = "RUNNING"
    response = submit(child_client, attempt)
    assert response.status_code == 403 and response.json()["code"] == "child_submit_unproven"


def test_no_child_key_means_no_child_path(db, worker_tokens, group_map, objects):
    client = _client(db, worker_tokens, group_map, objects, child_key="")
    seed_tenant(db, "eng")
    attempt = Attempt()
    seed_parent(db, attempt, state="STARTING")
    response = register(client, db, attempt)
    assert response.status_code == 503
    assert response.json()["code"] == "child_submit_unavailable"


# --------------------------------------------------------------------------
# §6.1: the submission
# --------------------------------------------------------------------------


def test_a_signed_request_creates_a_child_the_platform_shapes(child_client, db):
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(child_client, db, attempt)
    response = submit(
        child_client, attempt,
        child={"runner_profile": "mock", "input": {"prompt": "help"}, "timeout_seconds": 86400},
    )
    assert response.status_code == 201, response.text
    assert response.json()["created"] is True
    served = response.json()["task"]
    assert served["parent_task_id"] == PARENT and served["parent_attempt_id"] == ATTEMPT
    (child,) = _children(db).values()
    # Invariant 1: READY costs nothing until admission leases it.
    assert child["state"] == "READY"
    assert child["tenant_id"] == "eng"
    assert child["parent_task_id"] == PARENT and child["parent_attempt_id"] == ATTEMPT
    # The parent's submitter, priority and repository; never the agent's.
    assert child["submitted_by"] == "alice@saga.xyz"
    assert child["priority"] == 7
    assert child["repository_url"] == "https://github.com/acme/widgets.git"
    assert child["metadata"]["child_request_id"] == "split-1"
    # Invariant 7: clamped to the parent's own timeout.
    assert child["timeout_seconds"] <= 600
    assert child["workflow_id"] is None and child["depends_on"] == []
    events = [d for p, d in db.docs.items() if p.startswith(f"tasks/{child['id']}/events/")]
    assert [e["detail"]["via"] for e in events] == ["worker"]


def test_a_child_is_signed_at_format_2_over_its_parent_fields(db, worker_tokens, group_map, objects):
    """Contract request 42: the child's parent is inside its signed bytes; a
    task that names no parent stays at format 1 for workers built before it."""
    from .spec_signer import LocalSpecSigner

    signer = LocalSpecSigner()
    ctx = build_context(
        settings=api_settings(child_key=KEY), db=db,
        verifier=StaticTokenVerifier(worker_tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, signer=signer,
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(client, db, attempt)
    made = submit(client, attempt)
    assert made.status_code == 201, made.text
    child_id = made.json()["task"]["id"]
    doc = db.docs[f"tasks/{child_id}"]
    assert doc["spec_format"] == 2 and doc["parent_task_id"] == PARENT
    assert signer.verifies(doc, child_id)
    assert not signer.verifies({**doc, "parent_task_id": "task_elsewhere"}, child_id)



@pytest.mark.parametrize("access", ["read", "write"])
def test_a_child_inherits_its_parents_github_credential_inside_its_signature(
    db, worker_tokens, group_map, objects, access
):
    """#780 OB7: a child runs on its parent's repository as its parent's
    submitter, so it carries the parent's resolved forge credential and mode,
    never a wider one, signed at format 3 so a rewrite fails."""
    from swarm_api.gittokens import Scope, provider_suffix

    from .spec_signer import LocalSpecSigner

    signer = LocalSpecSigner()
    ctx = build_context(
        settings=api_settings(child_key=KEY), db=db,
        verifier=StaticTokenVerifier(worker_tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, signer=signer,
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    seed_tenant(db, "eng")
    attempt = Attempt()
    slot = provider_suffix(Scope.USER, user="alice@saga.xyz")
    ready_parent(client, db, attempt, forge_credential=slot, forge_access=access)
    made = submit(client, attempt)
    assert made.status_code == 201, made.text
    child_id = made.json()["task"]["id"]
    doc = db.docs[f"tasks/{child_id}"]
    assert doc["forge_credential"] == slot and doc["forge_access"] == access
    assert doc["spec_format"] == 3
    assert signer.verifies(doc, child_id)
    assert not signer.verifies({**doc, "forge_access": "write" if access == "read" else "read"},
                               child_id)

def test_a_created_child_is_counted_as_a_submitted_task_and_a_dedupe_is_not(
    db, worker_tokens, group_map, objects
):
    metrics = ApiMetrics()
    ctx = build_context(
        settings=api_settings(child_key=KEY), db=db,
        verifier=StaticTokenVerifier(worker_tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=metrics, objects=objects,
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(client, db, attempt)

    def counted() -> float:
        return metrics.registry.get_sample_value(
            "swarm_api_tasks_submitted_total", {"tenant": "eng", "runner_profile": "mock"}
        ) or 0.0

    assert submit(client, attempt).status_code == 201
    assert counted() == 1.0
    assert submit(client, attempt).status_code == 200
    assert counted() == 1.0, "the dedupe answer made nothing"


def test_the_same_request_id_answers_with_the_child_it_already_made(child_client, db):
    """§5 F1: a resubmission after a crash creates no duplicate."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(child_client, db, attempt)
    first = submit(child_client, attempt)
    again = submit(child_client, attempt)
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["task"]["id"] == first.json()["task"]["id"]
    assert len(_children(db)) == 1


@pytest.mark.parametrize("variant", ["unsigned", "stale", "forged", "other_key"])
def test_anything_but_a_fresh_signature_by_the_registered_key_is_unproven(
    child_client, db, variant
):
    """§5 F18: the agent holds the tenant's ID token and nothing that verifies."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(child_client, db, attempt)
    if variant == "unsigned":
        response = submit(child_client, attempt, sign=False)
    elif variant == "stale":
        response = submit(child_client, attempt, at=time.time() - 3600)
    elif variant == "forged":
        impostor = Attempt()
        response = submit(child_client, impostor)
    else:
        impostor = Attempt()
        impostor.tuple = attempt.tuple
        response = submit(child_client, impostor)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "child_submit_unproven"
    assert _children(db) == {}


def test_a_rewritten_registration_is_a_denial_not_a_key(child_client, db):
    """§3.2 step 4: the attestation, not the document, is what is trusted."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(child_client, db, attempt)
    impostor = Attempt()
    reg_id = ChildKeys(KEY).registration_ids(attempt.tuple)[0]
    db.docs[f"{CHILD_KEYS}/{reg_id}"]["public_key"] = impostor.public
    impostor.tuple = attempt.tuple
    assert submit(child_client, impostor).status_code == 403
    assert submit(child_client, attempt).status_code == 403
    assert _children(db) == {}


@pytest.mark.parametrize(
    "change",
    [
        {"state": "PARKED"},
        {"cancel_requested": True},
        {"current_lease_id": "lease_other"},
    ],
)
def test_a_parent_that_is_not_this_attempts_live_task_is_fenced(child_client, db, change):
    """Invariant 5: re-read inside the creating transaction; nothing is made."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = ready_parent(child_client, db, attempt)
    doc.update(change)
    response = submit(child_client, attempt)
    assert response.status_code == 409 and response.json()["code"] == "child_submit_fenced"
    assert _children(db) == {}


def test_a_released_lease_is_fenced(child_client, db):
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(child_client, db, attempt)
    db.docs[f"leases/{LEASE}"]["released_at"] = datetime.now(timezone.utc)
    response = submit(child_client, attempt)
    assert response.status_code == 409 and response.json()["code"] == "child_submit_fenced"


def test_a_child_may_not_have_children(child_client, db):
    """Depth 1, enforced at the route from the parent's own document."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(child_client, db, attempt, parent_task_id="task_grandparent")
    response = submit(child_client, attempt)
    assert response.status_code == 409 and response.json()["code"] == "child_depth_exceeded"


def test_the_fan_out_cap_counts_across_attempts(db, worker_tokens, group_map, objects):
    """§5 F4/F10: a retry loop cannot multiply children."""
    client = _client(db, worker_tokens, group_map, objects, max_children_per_task=2)
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(client, db, attempt)
    assert submit(client, attempt, "one").status_code == 201
    # The next attempt of the same task.
    second = Attempt(attempt="att_parent_2", lease="lease_parent_2", generation=2)
    ready_parent(client, db, second)
    assert submit(client, second, "two").status_code == 201
    third = submit(client, second, "three")
    assert third.status_code == 409 and third.json()["code"] == "child_fan_out_exceeded"
    assert len(_children(db)) == 2
    attempts = {d["parent_attempt_id"] for d in _children(db).values()}
    assert attempts == {"att_parent_1", "att_parent_2"}


@pytest.mark.parametrize(
    "child",
    [
        {"runner_profile": "mock", "image": "evil:latest"},
        {"runner_profile": "mock", "cpu": "64"},
        {"runner_profile": "mock", "repository_url": "https://github.com/evil/repo"},
        {"runner_profile": "mock", "parent_task_id": "task_x"},
        {"runner_profile": "merge"},
        {"runner_profile": "no-such-profile"},
    ],
)
def test_a_child_carries_nothing_but_names(child_client, db, child):
    """Invariants 7 and 10: a name, never a figure, an image or a forge action."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(child_client, db, attempt)
    response = submit(child_client, attempt, child=child)
    assert response.status_code == 422, response.text
    assert _children(db) == {}


def test_a_person_cannot_name_a_parent_on_post_tasks(client, db):
    seed_tenant(db, "eng")
    for field in ("parent_task_id", "parent_attempt_id"):
        response = client.post(
            "/v1/tasks", headers=auth_header("alice"),
            json={"runner_profile": "mock", "input": {}, field: "task_x"},
        )
        assert response.status_code == 422, response.text


@pytest.mark.parametrize("key", ["child_request_id", "child_await_resumes", "child_cascade"])
def test_the_child_metadata_keys_are_reserved(client, db, key):
    seed_tenant(db, "eng")
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {}, "metadata": {key: 1}},
    )
    assert response.status_code == 422, response.text
    assert key in response.json()["detail"]["reserved_metadata_keys"]


# --------------------------------------------------------------------------
# §6.2 and §6.3: listing
# --------------------------------------------------------------------------


def test_the_resumed_worker_lists_its_children(child_client, db):
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(child_client, db, attempt)
    made = submit(child_client, attempt).json()["task"]["id"]
    db.docs[f"tasks/{made}"].update(
        {
            "state": "SUCCEEDED",
            "result_summary": {
                "artifacts": [{"name": "out.md", "uri": f"gs://b/tenants/eng/tasks/{made}/attempts/a/artifacts/out.md", "bytes": 3}]
            },
        }
    )
    query = f"tenant_id=eng&attempt_id={ATTEMPT}&lease_id={LEASE}&generation=1"
    path = f"/v1/tasks/{PARENT}/children"
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}",
               **attempt.sign("GET", f"{path}?{query}", b"")}
    response = child_client.get(f"{path}?{query}", headers=headers)
    assert response.status_code == 200, response.text
    (row,) = response.json()["children"]
    assert row["task_id"] == made and row["request_id"] == "split-1"
    assert row["state"] == "SUCCEEDED" and row["parent_attempt_id"] == ATTEMPT
    assert [a["name"] for a in row["artifacts"]] == ["out.md"]
    # The tuple is signed with the path: another lease id under the same proof fails.
    swapped = query.replace(LEASE, "lease_other")
    assert child_client.get(f"{path}?{swapped}", headers=headers).status_code == 403


def test_people_list_a_parents_children_in_their_own_tenant(child_client, db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    attempt = Attempt()
    ready_parent(child_client, db, attempt)
    made = submit(child_client, attempt).json()["task"]["id"]
    seed_task(db, task_id="task_unrelated", tenant_id="eng")
    mine = child_client.get(f"/v1/tasks?parent_task_id={PARENT}", headers=auth_header("alice"))
    assert mine.status_code == 200, mine.text
    assert [t["id"] for t in mine.json()["tasks"]] == [made]
    theirs = child_client.get(f"/v1/tasks?parent_task_id={PARENT}", headers=auth_header("bob"))
    assert theirs.status_code == 200 and theirs.json()["tasks"] == []
    one = child_client.get(f"/v1/tasks/{made}", headers=auth_header("alice")).json()["task"]
    assert one["parent_task_id"] == PARENT and one["parent_attempt_id"] == ATTEMPT


# --------------------------------------------------------------------------
# §3.4: the cancel cascade
# --------------------------------------------------------------------------


def test_cancelling_a_parent_cancels_its_children_in_its_tenant(child_client, db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    parent = seed_task(db, task_id=PARENT, tenant_id="eng", state="PARKED",
                       park_reason="CHILDREN_INCOMPLETE")
    del parent  # the parent holds nothing: CANCELLED at once
    idle = seed_task(db, task_id="task_child_idle", tenant_id="eng", state="READY")
    running = seed_task(db, task_id="task_child_running", tenant_id="eng", state="RUNNING")
    done = seed_task(db, task_id="task_child_done", tenant_id="eng", state="SUCCEEDED")
    foreign = seed_task(db, task_id="task_foreign", tenant_id="research", state="READY")
    for doc in (idle, running, done, foreign):
        doc["parent_task_id"] = PARENT
    response = child_client.post(f"/v1/tasks/{PARENT}/cancel", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert response.json()["children_cancelled"] == 2

    assert idle["state"] == "CANCELLED" and idle["end_cause"] == "child_cascade"
    assert idle["metadata"]["child_cascade"]["why"] == "parent_cancelled"
    # Invariant 1: a child that holds capacity is flagged, never released here.
    assert running["state"] == "RUNNING" and running["cancel_requested"] is True
    assert running["metadata"]["child_cascade"]["why"] == "parent_cancelled"
    assert done["state"] == "SUCCEEDED" and "child_cascade" not in done["metadata"]
    # §5 F11: another tenant's document naming this parent is left alone.
    assert foreign["state"] == "READY" and not foreign.get("cancel_requested")
    events = [d for p, d in db.docs.items() if p.startswith("tasks/task_child_idle/events/")]
    assert [e["detail"]["requested_by"] for e in events] == [f"cascade:{PARENT}"]


def test_cancelling_a_workflow_cancels_its_steps_children(child_client, db):
    """§3.4: "a person cancels it ... or its workflow is cancelled"."""
    seed_tenant(db, "eng")
    made = child_client.post(
        "/v1/workflows", headers=auth_header("alice"),
        json={"steps": [{"step_id": "lead", "runner_profile": "mock", "input": {}}]},
    )
    assert made.status_code == 201, made.text
    body = made.json()
    workflow_id = body["workflow"]["workflow_id"] if "workflow" in body else body["workflow_id"]
    step_task = next(p.split("/")[1] for p, d in db.docs.items()
                     if p.count("/") == 1 and p.startswith("tasks/") and d.get("workflow_id") == workflow_id)
    helper = seed_task(db, task_id="task_helper", tenant_id="eng", state="READY")
    helper["parent_task_id"] = step_task
    response = child_client.post(f"/v1/workflows/{workflow_id}/cancel", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert helper["state"] == "CANCELLED" and helper["end_cause"] == "child_cascade"


def test_an_oversized_request_is_refused_before_it_is_read(child_client, db):
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(child_client, db, attempt)
    response = submit(
        child_client, attempt, child={"runner_profile": "mock", "input": {"prompt": "x" * 400_000}}
    )
    assert response.status_code == 422, response.text
    assert _children(db) == {}


def test_a_worker_token_is_pinned_to_the_child_audience_when_one_is_configured():
    """SWARM_API_AUDIENCE: a worker mints its ID token for the custom audience
    terraform gives swarm-api, and the child routes check `aud` against exactly
    that -- never the unpinned check an unset API_AUDIENCE would leave."""
    from types import SimpleNamespace

    from swarm_api.auth import GoogleTokenVerifier
    from swarm_api.routes.children import _worker_verifier

    base = GoogleTokenVerifier("https://swarm-api-123.us-central1.run.app")
    ctx = SimpleNamespace(
        authenticator=SimpleNamespace(_verifier=base),
        settings=SimpleNamespace(child_audience="https://swarm-api.dev.swarm.internal"),
    )
    pinned = _worker_verifier(ctx)
    assert isinstance(pinned, GoogleTokenVerifier) and pinned is not base
    assert pinned._audience == "https://swarm-api.dev.swarm.internal"
    assert _worker_verifier(ctx) is pinned, "built once per audience"
    # Unconfigured, or a test's static verifier: the app's own.
    ctx.settings.child_audience = ""
    assert _worker_verifier(ctx) is base
    static = StaticTokenVerifier({})
    ctx.authenticator._verifier = static
    ctx.settings.child_audience = "https://swarm-api.dev.swarm.internal"
    assert _worker_verifier(ctx) is static


def test_swarm_api_reads_the_child_audience(monkeypatch):
    from swarm_api.settings import ApiSettings

    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.setenv("SWARM_API_AUDIENCE", "https://swarm-api.dev.swarm.internal")
    assert ApiSettings.from_env().child_audience == "https://swarm-api.dev.swarm.internal"
