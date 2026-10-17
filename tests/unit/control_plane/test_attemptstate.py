"""The worker's state routes (docs/design/tenant-settings-integrity.md §4, lane ISO-STATE-API).

Drives the real routes over the in-memory Firestore. The worker identity is a
token the static verifier maps to the tenant's worker service account; the
attempt key is generated here as the worker generates one, registered through
`state/open` with the scheduler's nonce, and every later request is signed
with it. The message signed is RESTATED here (`_state_message`), as the
worker will restate it, so a drift in `childkey.state_request_message` fails
these tests rather than every live worker.

Each test names the rule of §6's ISO-STATE-API row it holds. Offline: no
credentials, no emulator.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from google.api_core import exceptions as gexc

from swarm_api.auth import StaticTokenVerifier
from swarm_api.childkey import AttemptTuple, nonce, request_message
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import PROJECT, api_settings, seed_pool, seed_task, seed_tenant
from .fakes import FakeTransaction

#: Built at runtime, never one literal (CLAUDE.md, "Nothing you add may look
#: like a credential").
KEY = "test-" + secrets.token_hex(16)
WORKER = "tok-worker-eng-" + secrets.token_hex(4)
OTHER_WORKER = "tok-worker-research-" + secrets.token_hex(4)
TASK = "task_state_1"
ATTEMPT = "att_state_1"
LEASE = "lease_state_1"
REPO_URL = "https://github.com/acme/widgets.git"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _state_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """The worker's restatement of `childkey.state_request_message` (§4.1)."""
    digest = hashlib.sha256(body).hexdigest()
    return f"swarm-state-req/v1\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode()


class Worker:
    """One attempt's worker: its tuple and the key it generated."""

    def __init__(self, tenant: str = "eng", task: str = TASK, attempt: str = ATTEMPT,
                 lease: str = LEASE, generation: int = 1, token: str = WORKER) -> None:
        self.tuple = AttemptTuple(tenant, task, attempt, lease, generation)
        self.token = token
        self.private = Ed25519PrivateKey.generate()
        self.public = _b64url(self.private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    def fields(self) -> dict[str, Any]:
        t = self.tuple
        return {"tenant_id": t.tenant_id, "task_id": t.task_id, "lease_id": t.lease_id,
                "generation": t.generation}

    def query(self) -> str:
        t = self.tuple
        return (f"tenant_id={t.tenant_id}&task_id={t.task_id}&lease_id={t.lease_id}"
                f"&generation={t.generation}")

    def sign(self, method: str, path: str, body: bytes, *, child: bool = False) -> dict[str, str]:
        timestamp = str(int(time.time()))
        message = (request_message(method, path, body, timestamp) if child
                   else _state_message(method, path, body, timestamp))
        return {"X-Swarm-Attempt-Proof": _b64url(self.private.sign(message)),
                "X-Swarm-Attempt-Timestamp": timestamp}

    def post(self, client: TestClient, op: str, extra: dict[str, Any] | None = None, *,
             token: str | None = None, sign: bool = True, child_proof: bool = False,
             body: dict[str, Any] | None = None) -> Any:
        path = f"/v1/attempts/{self.tuple.attempt_id}/state/{op}"
        raw = json.dumps(body if body is not None else {**self.fields(), **(extra or {})},
                         default=str).encode()
        headers = {"Authorization": f"Bearer {token or self.token}",
                   "Content-Type": "application/json"}
        if sign:
            headers.update(self.sign("POST", path, raw, child=child_proof))
        return client.post(path, content=raw, headers=headers)

    def get(self, client: TestClient, op: str, extra: str = "", *, token: str | None = None,
            sign: bool = True) -> Any:
        path = f"/v1/attempts/{self.tuple.attempt_id}/state/{op}"
        query = self.query() + (f"&{extra}" if extra else "")
        headers = {"Authorization": f"Bearer {token or self.token}"}
        if sign:
            headers.update(self.sign("GET", f"{path}?{query}", b""))
        return client.get(f"{path}?{query}", headers=headers)

    def open(self, client: TestClient, *, unprotected: bool = False,
             public: str | None = None, token: str | None = None) -> Any:
        path = f"/v1/attempts/{self.tuple.attempt_id}/state/open"
        raw = json.dumps({**self.fields(), "nonce": nonce(KEY, self.tuple),
                          "public_key": public or self.public,
                          "unprotected": unprotected}).encode()
        return client.post(path, content=raw, headers={
            "Authorization": f"Bearer {token or self.token}", "Content-Type": "application/json"})


@pytest.fixture
def worker_tokens(tokens) -> dict[str, dict[str, Any]]:
    return {
        **tokens,
        WORKER: {"email": f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
                 "email_verified": True, "sub": "1000000000000000000001"},
        OTHER_WORKER: {"email": f"swarm-agent-worker-research@{PROJECT}.iam.gserviceaccount.com",
                       "email_verified": True, "sub": "1000000000000000000002"},
    }


@pytest.fixture
def client(db, worker_tokens, group_map, objects) -> TestClient:
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
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def seed_attempt(db, worker: Worker, *, state: str = "STARTING", pools=("global", "tenant:eng"),
                 **extra: Any) -> dict[str, Any]:
    """A task under this attempt's live lease, holding one unit of `pools`."""
    t = worker.tuple
    seed_tenant(db, t.tenant_id)
    now = datetime.now(timezone.utc)
    for name in pools:
        if f"pools/{name}" not in db.docs:
            seed_pool(db, name, hard_limit=10, active=0)
        db.docs[f"pools/{name}"]["active"] += 1
    doc = seed_task(db, task_id=t.task_id, tenant_id=t.tenant_id, state=state)
    doc.update({"current_lease_id": t.lease_id, "current_generation": t.generation,
                "submitted_by": "alice@saga.xyz", "repository_url": REPO_URL,
                "attempt_count": 1, **extra})
    db.docs[f"leases/{t.lease_id}"] = {
        "lease_id": t.lease_id, "task_id": t.task_id, "attempt_id": t.attempt_id,
        "tenant_id": t.tenant_id, "generation": t.generation, "pools": list(pools),
        "units": 1, "state": state, "created_at": now, "heartbeat_at": None,
        "dispatch_deadline": now + timedelta(minutes=10),
        "expires_at": now + timedelta(minutes=10), "released_at": None,
    }
    return doc


def opened(client, db, worker: Worker, *, state: str = "RUNNING", **extra: Any) -> dict[str, Any]:
    """A worker that opened its attempt while STARTING, now at `state`."""
    doc = seed_attempt(db, worker, **extra)
    response = worker.open(client)
    assert response.status_code == 200, response.text
    doc["state"] = state
    db.docs[f"leases/{worker.tuple.lease_id}"]["state"] = state
    return doc


def _events(db, kind: str | None = None) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items()
            if p.startswith(f"tasks/{TASK}/events/") and (kind is None or d.get("type") == kind)]


#: One well-formed body per POST route, as a worker would send it.
POST_BODIES: dict[str, dict[str, Any]] = {
    "transition": {"request_id": "t-1", "to": "RUNNING"},
    "attempt-start": {"backend": "cloud_run", "execution_name": "exec-1"},
    "checkpoint": {"checkpoint_id": "ckpt-00001",
                   "uri": f"gs://bucket/tenants/eng/tasks/{TASK}/attempts/{ATTEMPT}/c.tar",
                   "size_bytes": 10, "seq": 1, "archive_sha256": "a" * 64},
    "usage": {"kind": "spend", "fields": {"input_tokens": 3}},
    "heartbeat": {},
    "park": {"request_id": "p-1", "reason": "PROVIDER_QUOTA_EXHAUSTED",
             "next_eligible_at": "2030-01-01T00:00:00+00:00"},
    "finish": {"request_id": "f-1", "state": "SUCCEEDED", "exit_code": 0},
    "retry": {"request_id": "r-1", "error": "boom", "cause": "crash"},
    "attempt-end": {"exit_code": 0},
    "release": {"reason": "terminal:SUCCEEDED"},
    "event": {"event_id": "evt_" + "0" * 20, "type": "heartbeat"},
    "merge-slot": {"op": "acquire", "base": "main", "updates": 0},
    "merge-train": {"op": "register", "base": "main", "number": 7, "head": "b" * 40,
                    "title": "t", "message": "m"},
    "ci-fix-tasks": {"pull_request": 7, "root_task_id": TASK},
}
GET_ROUTES = {"poll": "provider=anthropic", "context": "", "upstream": "upstream_task_id=task_up",
              "repository": ""}
#: The routes that write a task, a lease, a slot or an entry: each fenced.
FENCED_WRITES = ("transition", "attempt-start", "checkpoint", "heartbeat", "park", "finish",
                 "retry", "merge-slot", "merge-train")


def test_every_route_in_the_row_is_mounted(client):
    """The app answers each route with a refusal of its own, never a 404."""
    for op in [*POST_BODIES, "open"]:
        response = client.post(f"/v1/attempts/{ATTEMPT}/state/{op}", content=b"{}")
        assert response.status_code == 422, (op, response.status_code)
    for op in GET_ROUTES:
        response = client.get(f"/v1/attempts/{ATTEMPT}/state/{op}")
        assert response.status_code == 422, (op, response.status_code)


# --------------------------------------------------------------------------
# 401: another tenant's token, on every route
# --------------------------------------------------------------------------


@pytest.mark.parametrize("op", sorted(POST_BODIES))
def test_another_tenants_token_is_401_on_every_post_route(client, db, op):
    worker = Worker()
    opened(client, db, worker)
    before = copy.deepcopy(db.docs)
    response = worker.post(client, op, POST_BODIES[op], token=OTHER_WORKER)
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "attempt_unauthenticated"
    assert db.docs == before


@pytest.mark.parametrize("op", sorted(GET_ROUTES))
def test_another_tenants_token_is_401_on_every_get_route(client, db, op):
    worker = Worker()
    opened(client, db, worker)
    response = worker.get(client, op, GET_ROUTES[op], token=OTHER_WORKER)
    assert response.status_code == 401, response.text


def test_another_tenants_token_cannot_open_this_attempt(client, db):
    worker = Worker()
    seed_attempt(db, worker)
    response = worker.open(client, token=OTHER_WORKER)
    assert response.status_code == 401, response.text
    assert not db.paths("child_keys/")


# --------------------------------------------------------------------------
# 403: the token alone proves the tenant, not the worker
# --------------------------------------------------------------------------


@pytest.mark.parametrize("op", sorted(POST_BODIES))
def test_a_token_without_a_proof_is_403_on_every_post_route(client, db, op):
    worker = Worker()
    opened(client, db, worker)
    before = copy.deepcopy(db.docs)
    response = worker.post(client, op, POST_BODIES[op], sign=False)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "attempt_unproven"
    assert db.docs == before


@pytest.mark.parametrize("op", sorted(GET_ROUTES))
def test_a_token_without_a_proof_is_403_on_every_get_route(client, db, op):
    worker = Worker()
    opened(client, db, worker)
    assert worker.get(client, op, GET_ROUTES[op], sign=False).status_code == 403


def test_a_key_the_agent_made_is_403(client, db):
    """The agent can mint the token, never sign with the registered key."""
    worker = Worker()
    opened(client, db, worker)
    agent = Worker()  # same tuple, its own key
    response = agent.post(client, "heartbeat", {})
    assert response.status_code == 403, response.text


def test_a_child_proof_is_not_a_state_proof(client, db):
    """Domain separation (§4.1): `swarm-child-req/v1` is never a state write."""
    worker = Worker()
    opened(client, db, worker)
    response = worker.post(client, "heartbeat", {}, child_proof=True)
    assert response.status_code == 403, response.text


def test_a_proof_signed_for_another_tuple_is_403(client, db):
    """The proof covers the body, and the body names the tuple."""
    worker = Worker()
    opened(client, db, worker)
    path = f"/v1/attempts/{ATTEMPT}/state/heartbeat"
    signed = json.dumps(worker.fields()).encode()
    sent = json.dumps({**worker.fields(), "generation": 2}).encode()
    headers = {"Authorization": f"Bearer {WORKER}", "Content-Type": "application/json",
               **worker.sign("POST", path, signed)}
    assert client.post(path, content=sent, headers=headers).status_code == 403


# --------------------------------------------------------------------------
# 409: a stale generation writes nothing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("op", FENCED_WRITES)
def test_a_stale_generation_is_409_and_writes_nothing(client, db, op):
    worker = Worker()
    opened(client, db, worker)
    # The reconciler fenced this attempt: the task is at generation 2 now,
    # under a newer lease. This attempt's lease is left unreleased, so only the
    # task-level fence can refuse the write.
    db.docs[f"tasks/{TASK}"].update({"current_generation": 2, "current_lease_id": "lease_new"})
    before = copy.deepcopy(db.docs)
    response = worker.post(client, op, POST_BODIES[op])
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "attempt_fenced"
    assert db.docs == before


def test_a_released_lease_is_409_on_heartbeat(client, db):
    worker = Worker()
    opened(client, db, worker)
    db.docs[f"leases/{LEASE}"]["released_at"] = datetime.now(timezone.utc)
    before = copy.deepcopy(db.docs)
    assert worker.post(client, "heartbeat", {}).status_code == 409
    assert db.docs == before


# --------------------------------------------------------------------------
# 422: invariant 10, and only the worker's transitions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("op", sorted(POST_BODIES))
def test_a_runner_profile_in_any_body_is_422(client, db, op):
    worker = Worker()
    opened(client, db, worker)
    before = copy.deepcopy(db.docs)
    response = worker.post(client, op, {**POST_BODIES[op], "runner_profile": "claude-code"})
    assert response.status_code == 422, response.text
    assert db.docs == before


def test_a_runner_profile_in_the_open_body_is_422(client, db):
    worker = Worker()
    seed_attempt(db, worker)
    path = f"/v1/attempts/{ATTEMPT}/state/open"
    raw = json.dumps({**worker.fields(), "nonce": nonce(KEY, worker.tuple),
                      "public_key": worker.public, "runner_profile": "x"}).encode()
    response = client.post(path, content=raw, headers={"Authorization": f"Bearer {WORKER}"})
    assert response.status_code == 422
    assert not db.paths("child_keys/")


def test_running_to_leased_is_422(client, db):
    worker = Worker()
    opened(client, db, worker)
    before = copy.deepcopy(db.docs)
    response = worker.post(client, "transition", {"request_id": "back", "to": "LEASED"})
    assert response.status_code == 422, response.text
    assert db.docs == before


def test_a_worker_cannot_dead_letter_its_task(client, db):
    """DEAD_LETTERED is not in `WORKER_TRANSITIONS` (contract request 85)."""
    worker = Worker()
    opened(client, db, worker)
    before = copy.deepcopy(db.docs)
    response = worker.post(client, "finish", {"request_id": "dl", "state": "DEAD_LETTERED"})
    assert response.status_code == 422, response.text
    assert db.docs == before


def test_the_walk_to_running_is_the_workers(client, db):
    worker = Worker()
    opened(client, db, worker, state="LEASED")
    for step, to in enumerate(("DISPATCHED", "STARTING", "RUNNING")):
        response = worker.post(client, "transition", {"request_id": f"walk-{step}", "to": to})
        assert response.status_code == 200, response.text
        assert response.json()["task"]["state"] == to
    assert db.docs[f"tasks/{TASK}"]["state"] == "RUNNING"
    assert db.docs[f"tasks/{TASK}"]["started_at"] is not None


# --------------------------------------------------------------------------
# park: PARKED and the release in ONE transaction
# --------------------------------------------------------------------------


def test_park_writes_parked_and_releases_the_lease_in_one_transaction(client, db):
    worker = Worker()
    opened(client, db, worker)
    committed = db.transactions_committed
    response = worker.post(client, "park", POST_BODIES["park"])
    assert response.status_code == 200, response.text
    assert db.transactions_committed == committed + 1
    task = db.docs[f"tasks/{TASK}"]
    assert task["state"] == "PARKED" and task["current_lease_id"] is None
    assert task["park_reason"] == "PROVIDER_QUOTA_EXHAUSTED"
    assert db.docs[f"leases/{LEASE}"]["released_at"] is not None
    assert db.docs["pools/global"]["active"] == 0
    assert db.docs["pools/tenant:eng"]["active"] == 0
    assert db.docs[f"attempts/{ATTEMPT}"]["exit_code"] == 75
    assert len(_events(db, "parked")) == 1 and len(_events(db, "lease_released")) == 1


def test_an_injected_failure_in_a_park_leaves_neither(client, db, monkeypatch):
    worker = Worker()
    opened(client, db, worker)
    real = FakeTransaction.update

    def failing(self, ref, data):
        if ref.path.startswith("pools/"):
            raise gexc.ServiceUnavailable("injected")
        return real(self, ref, data)

    monkeypatch.setattr(FakeTransaction, "update", failing)
    before = copy.deepcopy(db.docs)
    response = worker.post(client, "park", POST_BODIES["park"])
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "state_unavailable"
    assert db.docs == before


def test_an_await_park_refunds_by_the_servers_bound(client, db):
    worker = Worker()
    opened(client, db, worker, attempt_count=2)
    response = worker.post(client, "park", {"request_id": "await", "reason": "CHILDREN_INCOMPLETE"})
    assert response.status_code == 200, response.text
    task = db.docs[f"tasks/{TASK}"]
    assert task["attempt_count"] == 1
    assert task["metadata"]["child_await_resumes"] == 1


# --------------------------------------------------------------------------
# release: this tuple's lease, whatever the body names
# --------------------------------------------------------------------------


def test_release_releases_only_this_tuples_lease(client, db):
    worker = Worker()
    opened(client, db, worker)
    seed_pool(db, "tenant:research", hard_limit=5, active=3)
    seed_pool(db, "provider:anthropic", hard_limit=5, active=2)
    db.docs["leases/lease_other"] = {**db.docs[f"leases/{LEASE}"], "lease_id": "lease_other",
                                     "task_id": "task_other", "attempt_id": "att_other",
                                     "pools": ["provider:anthropic"]}
    before = copy.deepcopy(db.docs)
    named = worker.post(client, "release", {"reason": "terminal:SUCCEEDED",
                                            "pools": ["tenant:research", "provider:anthropic"],
                                            "lease": "lease_other"})
    assert named.status_code == 422, named.text
    assert db.docs == before

    response = worker.post(client, "release", {"reason": "terminal:SUCCEEDED"})
    assert response.status_code == 200, response.text
    assert response.json()["released"] is True
    assert db.docs["pools/global"]["active"] == 0
    assert db.docs["pools/tenant:eng"]["active"] == 0
    assert db.docs["pools/tenant:research"]["active"] == 3
    assert db.docs["pools/provider:anthropic"]["active"] == 2
    assert db.docs["leases/lease_other"]["released_at"] is None

    again = worker.post(client, "release", {"reason": "terminal:SUCCEEDED"})
    assert again.json()["released"] is False
    assert db.docs["pools/global"]["active"] == 0


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------


def test_a_repeated_finish_with_one_request_id_writes_once(client, db):
    worker = Worker()
    opened(client, db, worker)
    db.docs["pools/global"]["active"] = 4  # other tasks' slots, which a second release would take
    first = worker.post(client, "finish", POST_BODIES["finish"])
    assert first.status_code == 200, first.text
    after_first = copy.deepcopy(db.docs)
    second = worker.post(client, "finish", POST_BODIES["finish"])
    assert second.status_code == 200, second.text
    assert second.json()["repeat"] is True
    assert db.docs == after_first
    assert db.docs["pools/global"]["active"] == 3
    assert len(_events(db, "succeeded")) == 1
    assert db.docs[f"tasks/{TASK}"]["state"] == "SUCCEEDED"


def test_a_finish_with_a_new_request_id_after_the_end_is_fenced(client, db):
    worker = Worker()
    opened(client, db, worker)
    assert worker.post(client, "finish", POST_BODIES["finish"]).status_code == 200
    after = copy.deepcopy(db.docs)
    response = worker.post(client, "finish", {**POST_BODIES["finish"], "request_id": "f-2"})
    assert response.status_code == 409
    assert db.docs == after


# --------------------------------------------------------------------------
# upstream: only what the signed spec declares
# --------------------------------------------------------------------------


def test_upstream_refuses_an_id_outside_depends_on(client, db):
    worker = Worker()
    opened(client, db, worker, depends_on=["task_up"])
    seed_task(db, task_id="task_up", tenant_id="eng", state="SUCCEEDED")
    seed_task(db, task_id="task_sibling", tenant_id="eng", state="SUCCEEDED")
    ok = worker.get(client, "upstream", "upstream_task_id=task_up")
    assert ok.status_code == 200, ok.text
    assert ok.json()["task"]["state"] == "SUCCEEDED"
    refused = worker.get(client, "upstream", "upstream_task_id=task_sibling")
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "upstream_not_declared"


def test_upstream_refuses_a_declared_id_of_another_tenant(client, db):
    worker = Worker()
    opened(client, db, worker, depends_on=["task_theirs"])
    seed_task(db, task_id="task_theirs", tenant_id="research", state="SUCCEEDED")
    response = worker.get(client, "upstream", "upstream_task_id=task_theirs")
    assert response.status_code == 403
    assert "research" not in response.text


# --------------------------------------------------------------------------
# merge slot: a wake only for this tenant's listed waiter
# --------------------------------------------------------------------------


def _slot_waiting_on(db, waiter: str) -> str:
    from swarm_api.attemptstate import slot_id

    slot = slot_id("eng", "acme/widgets", "main")
    now = datetime.now(timezone.utc)
    db.docs[f"merge_slots/{slot}"] = {
        "tenant_id": "eng", "repository": "acme/widgets", "base": "main", "generation": 3,
        "holder": {"task_id": TASK, "attempt_id": ATTEMPT, "submitted_at": now,
                   "generation": 3, "expires_at": now + timedelta(minutes=30)},
        "waiters": [{"task_id": waiter, "submitted_at": now, "seen_at": now}],
    }
    return slot


def _parked_on_slot(db, task_id: str, tenant: str) -> None:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant, state="PARKED",
                    park_reason="CI_PENDING")
    doc["metadata"] = {"merge_wait": {"code": "merge_slot_wait"}}


def test_merge_slot_wake_refuses_another_tenants_waiter(client, db):
    worker = Worker()
    opened(client, db, worker)
    _slot_waiting_on(db, "task_theirs")
    _parked_on_slot(db, "task_theirs", "research")
    before = copy.deepcopy(db.docs)
    response = worker.post(client, "merge-slot", {"op": "wake", "base": "main",
                                                  "wake_task_id": "task_theirs"})
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "merge_wake_refused"
    assert db.docs == before


def test_merge_slot_wake_refuses_a_task_the_slot_does_not_list(client, db):
    worker = Worker()
    opened(client, db, worker)
    _slot_waiting_on(db, "task_listed")
    _parked_on_slot(db, "task_unlisted", "eng")
    before = copy.deepcopy(db.docs)
    response = worker.post(client, "merge-slot", {"op": "wake", "base": "main",
                                                  "wake_task_id": "task_unlisted"})
    assert response.status_code == 403
    assert db.docs == before


def test_merge_slot_wake_marks_this_tenants_listed_waiter(client, db):
    worker = Worker()
    opened(client, db, worker)
    _slot_waiting_on(db, "task_listed")
    _parked_on_slot(db, "task_listed", "eng")
    response = worker.post(client, "merge-slot", {"op": "wake", "base": "main",
                                                  "wake_task_id": "task_listed"})
    assert response.status_code == 200, response.text
    assert response.json()["woken"] is True
    assert db.docs["tasks/task_listed"]["metadata"]["merge_wait"]["wake_requested_at"]


# --------------------------------------------------------------------------
# registrations: v1 is child-only; v2 is what state/open writes
# --------------------------------------------------------------------------


def _register_v1(client, worker: Worker) -> Any:
    t = worker.tuple
    raw = json.dumps({"tenant_id": t.tenant_id, "task_id": t.task_id, "lease_id": t.lease_id,
                      "generation": t.generation, "nonce": nonce(KEY, t),
                      "public_key": worker.public}).encode()
    return client.post(f"/v1/attempts/{t.attempt_id}/child-key", content=raw,
                       headers={"Authorization": f"Bearer {WORKER}"})


def _submit_child(client, worker: Worker) -> Any:
    body = json.dumps({"tenant_id": "eng", "attempt_id": ATTEMPT, "lease_id": LEASE,
                       "generation": 1, "request_id": "c-1",
                       "child": {"runner_profile": "mock", "input": {"prompt": "x"}}}).encode()
    path = f"/v1/tasks/{TASK}/children"
    return client.post(path, content=body, headers={
        "Authorization": f"Bearer {WORKER}", "Content-Type": "application/json",
        **worker.sign("POST", path, body, child=True)})


def test_a_v1_child_attestation_verifies_for_child_routes_and_not_for_state(client, db):
    worker = Worker()
    seed_attempt(db, worker, state="STARTING")
    assert _register_v1(client, worker).status_code == 201
    db.docs[f"tasks/{TASK}"]["state"] = "RUNNING"
    assert _submit_child(client, worker).status_code == 201
    before = copy.deepcopy(db.docs)
    response = worker.post(client, "heartbeat", {})
    assert response.status_code == 403, response.text
    assert db.docs == before


def test_a_key_state_open_registered_signs_children_too(client, db):
    worker = Worker()
    opened(client, db, worker)
    assert _submit_child(client, worker).status_code == 201
    assert worker.post(client, "heartbeat", {}).status_code == 200


def test_an_unprotected_heap_registers_flagged_and_writes_state_only(client, db):
    """Owner decision Q4: flagged and accepted. Its key signs state; a child
    route refuses it, as a tombstone's worker has run without children."""
    worker = Worker()
    seed_attempt(db, worker)
    response = worker.open(client, unprotected=True)
    assert response.status_code == 200, response.text
    assert response.json()["registration"]["unprotected"] is True
    (reg,) = [d for p, d in db.docs.items() if p.startswith("child_keys/")]
    assert reg["unprotected"] is True and reg["purpose"] == ["state"]
    db.docs[f"tasks/{TASK}"]["state"] = "RUNNING"
    assert worker.post(client, "heartbeat", {}).status_code == 200
    assert _submit_child(client, worker).status_code == 403


def test_widening_a_stored_registration_breaks_it(client, db):
    """A v1 document given `purpose` by a tenant identity is a denial, never a widened key."""
    worker = Worker()
    seed_attempt(db, worker, state="STARTING")
    assert _register_v1(client, worker).status_code == 201
    db.docs[f"tasks/{TASK}"]["state"] = "RUNNING"
    (path,) = [p for p in db.docs if p.startswith("child_keys/")]
    db.docs[path].update({"purpose": ["child", "state"], "version": 2, "unprotected": False})
    assert worker.post(client, "heartbeat", {}).status_code == 403
    assert _submit_child(client, worker).status_code == 403


def test_open_spends_the_nonce_once_per_generation(client, db):
    worker = Worker()
    seed_attempt(db, worker, state="LEASED")
    first = worker.open(client)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["registration"]["created"] is True
    assert body["task"]["id"] == TASK and body["lease"]["lease_id"] == LEASE
    retry = worker.open(client)
    assert retry.status_code == 200 and retry.json()["registration"]["created"] is False
    other_key = Worker()
    taken = worker.open(client, public=other_key.public)
    assert taken.status_code == 409, taken.text


def test_open_refuses_a_bad_nonce(client, db):
    worker = Worker()
    seed_attempt(db, worker)
    path = f"/v1/attempts/{ATTEMPT}/state/open"
    raw = json.dumps({**worker.fields(), "nonce": secrets.token_hex(32),
                      "public_key": worker.public}).encode()
    response = client.post(path, content=raw, headers={"Authorization": f"Bearer {WORKER}"})
    assert response.status_code == 403
    assert not db.paths("child_keys/")


def test_open_refuses_once_the_task_is_running(client, db):
    """The window is LEASED, DISPATCHED or STARTING (§4.3): the agent exists
    only after RUNNING, so it can never be the one that registers."""
    worker = Worker()
    seed_attempt(db, worker, state="RUNNING")
    assert worker.open(client).status_code == 409
    assert not db.paths("child_keys/")
