"""Live-browser hand-off, the API half (docs/design/live-browser.md §4.1, §4.3, §5.4.1).

Lane LB-L10. Drives the real routes over the in-memory Firestore, with the
attempt key a worker generates and registers exactly as the child-task path
does (§5.4.1 reuses it), and the scheduler's real drain for the sweep. Each
test names the rule of the design it holds:

  * the worker route refuses an unsigned, stale, foreign or stale-generation
    call and writes nothing;
  * `human-ready` moves PARKED(HUMAN_REQUIRED) -> READY for an eligible caller
    only;
  * no sweep promotes HUMAN_REQUIRED;
  * past the maximum it is dead-lettered with `human_wait_expired`;
  * `human_wait_resumes` (and this lane's other keys) are reserved.

Every key and token below is built at runtime: nothing here is a credential.
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
from swarm_api.childkey import AttemptTuple, nonce, request_message
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.handoff import (
    HUMAN_HANDOFFS_METADATA_KEY,
    HUMAN_READY_METADATA_KEY,
    HUMAN_RETURNED_METADATA_KEY,
    HUMAN_WAIT_EXPIRED,
    HUMAN_WAIT_RESUMES_METADATA_KEY,
    LIVE_AUDIT,
    MAX_HANDOFFS_PER_ATTEMPT,
    REVIVED,
    handoff_message,
)
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import PROJECT, api_settings, auth_header, scheduler_settings, seed_task, seed_tenant

#: Built at runtime, never one literal (CLAUDE.md, "Nothing you add may look
#: like a credential").
KEY = "-".join(("handoff", "test", "key")) + "-" + "k" * 24
WORKER_TOKEN = "-".join(("token", "worker", "eng"))
OTHER_WORKER_TOKEN = "-".join(("token", "worker", "research"))
TASK = "task_browser"
ATTEMPT = "att_browser_1"
LEASE = "lease_browser_1"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class Attempt:
    """One attempt: its tuple, and an attempt key generated as a worker would."""

    def __init__(self, tenant: str = "eng", task: str = TASK, attempt: str = ATTEMPT,
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

    def sign(self, method: str, path: str, body: bytes, *, at: float | None = None,
             message=handoff_message) -> dict[str, str]:
        timestamp = str(int(at if at is not None else time.time()))
        signature = self.private.sign(message(method, path, body, timestamp))
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
def live_client(db, worker_tokens, group_map, objects) -> TestClient:
    return _client(db, worker_tokens, group_map, objects)


def seed_attempt(db, attempt: Attempt, *, state: str = "RUNNING", **extra: Any) -> dict[str, Any]:
    t = attempt.tuple
    doc = seed_task(db, task_id=t.task_id, tenant_id=t.tenant_id, state=state)
    doc.update(
        {
            "current_lease_id": t.lease_id,
            "current_generation": t.generation,
            "submitted_by": "alice@example.com",
            "runner_profile": "mock",
            "timeout_seconds": 3600,
            "attempt_count": 1,
            "started_at": datetime.now(timezone.utc),
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


def running(client, db, attempt: Attempt, **extra: Any) -> dict[str, Any]:
    """Registered while STARTING, then RUNNING: what the worker leaves (§5.4.1)."""
    doc = seed_attempt(db, attempt, state="STARTING", **extra)
    t = attempt.tuple
    response = client.post(
        f"/v1/attempts/{t.attempt_id}/child-key",
        content=json.dumps(
            {
                "tenant_id": t.tenant_id,
                "task_id": t.task_id,
                "lease_id": t.lease_id,
                "generation": t.generation,
                "nonce": nonce(KEY, t),
                "public_key": attempt.public,
            }
        ).encode(),
        headers={"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"},
    )
    assert response.status_code == 201, response.text
    doc["state"] = "RUNNING"
    db.docs[f"leases/{t.lease_id}"]["state"] = "RUNNING"
    return doc


def request_handoff(client, attempt: Attempt, *, request_id: str = "signin-1",
                    reason: str = "Sign in to GitHub as the bot account",
                    expect: str | None = "github.com", token: str = WORKER_TOKEN,
                    sign: bool = True, at: float | None = None, message=handoff_message,
                    task: str | None = None, fields: dict[str, Any] | None = None) -> Any:
    task_id = task or attempt.tuple.task_id
    body = json.dumps(
        {**(fields or attempt.fields()), "request_id": request_id, "reason": reason,
         "expect": expect}
    ).encode()
    path = f"/v1/tasks/{task_id}/handoff"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if sign:
        headers.update(attempt.sign("POST", path, body, at=at, message=message))
    return client.post(path, content=body, headers=headers)


def resolve_handoff(client, attempt: Attempt, *, request_id: str = "signin-1",
                    outcome: str = "expired") -> Any:
    body = json.dumps(
        {**attempt.fields(), "request_id": request_id, "outcome": outcome}
    ).encode()
    path = f"/v1/tasks/{attempt.tuple.task_id}/handoff/resolve"
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"}
    headers.update(attempt.sign("POST", path, body))
    return client.post(path, content=body, headers=headers)


def _events(db, task_id: str = TASK) -> list[dict[str, Any]]:
    prefix = f"tasks/{task_id}/events/"
    return [doc for path, doc in db.docs.items() if path.startswith(prefix)]


def _audit(db) -> list[dict[str, Any]]:
    return [doc for path, doc in db.docs.items() if path.startswith(f"{LIVE_AUDIT}/")]


# --------------------------------------------------------------------------
# §4.1 step 3: the worker route, signed with the attempt key
# --------------------------------------------------------------------------


def test_a_signed_request_sets_human_wait_and_records_it(live_client, db):
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = running(live_client, db, attempt)
    response = request_handoff(live_client, attempt)
    assert response.status_code == 201, response.text
    wait = doc["human_wait"]
    assert wait["reason"] == "Sign in to GitHub as the bot account"
    assert wait["expect"] == "github.com"
    assert wait["controller"] is None
    assert wait["attempt_id"] == ATTEMPT and wait["generation"] == 1
    # The hold is bounded (§4.3, Q3: 10 minutes), never past the attempt.
    held = wait["deadline"] - wait["requested_at"]
    assert timedelta(seconds=0) < held <= timedelta(minutes=10)
    # The task stays RUNNING (Q2: the field, not a state); nothing about
    # capacity moved (invariants 1 and 3).
    assert doc["state"] == "RUNNING"
    assert db.docs[f"leases/{LEASE}"]["released_at"] is None
    assert doc["metadata"][HUMAN_HANDOFFS_METADATA_KEY] == {"attempt_id": ATTEMPT, "count": 1}
    types = [e["type"] for e in _events(db)]
    assert types == ["human_handoff_requested"]
    # LB-V6: on the event's own fields, which the Attempts tab groups on.
    event = _events(db)[0]
    assert (event["attempt_id"], event["lease_id"], event["generation"]) == (ATTEMPT, LEASE, 1)
    entries = _audit(db)
    assert [e["entry"] for e in entries] == ["handoff_requested"]
    assert entries[0]["task_id"] == TASK and entries[0]["tenant_id"] == "eng"


# --------------------------------------------------------------------------
# HANDOFF-POLICY (part of #1030): notify, park fast, extend while watched
# --------------------------------------------------------------------------


def test_an_unwatched_hand_off_holds_for_one_minute_not_ten(live_client, db):
    """MEASURED on 26 live-browser tasks (2026-10-13): of 42 hand-offs 25
    expired, 13 of them with nobody there, each holding a 2-unit browser lease
    and its pod for the whole 10 minutes. The hold now opens at
    `human_unwatched_seconds` (60) and only a viewer or a controller moves it."""
    from swarm_api.handoff import HUMAN_HOLD_MAX_SECONDS, HUMAN_UNWATCHED_SECONDS

    assert HUMAN_UNWATCHED_SECONDS == 60
    assert HUMAN_HOLD_MAX_SECONDS == 30 * 60
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = running(live_client, db, attempt)
    assert request_handoff(live_client, attempt).status_code == 201
    wait = doc["human_wait"]
    assert wait["deadline"] - wait["requested_at"] == timedelta(seconds=60)
    assert wait["watched_at"] is None


def test_the_unwatched_hold_never_passes_the_attempts_own_deadline(live_client, db):
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = running(live_client, db, attempt,
                  started_at=datetime.now(timezone.utc) - timedelta(seconds=3590))
    assert request_handoff(live_client, attempt).status_code == 201
    wait = doc["human_wait"]
    assert wait["deadline"] <= doc["started_at"] + timedelta(seconds=3600)
    assert wait["deadline"] >= wait["requested_at"]


def test_a_request_puts_agent_needs_you_in_the_submitters_bell(live_client, db):
    """Notify: the submitter's feed has an unread needs-you item linking to the
    task's Browser tab as soon as the hand-off is recorded."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    running(live_client, db, attempt)
    before = live_client.get("/v1/notifications", headers=auth_header("alice"))
    assert before.status_code == 200, before.text
    assert not [i for i in before.json()["items"] if i["id"].startswith("task:handoff:")]
    assert request_handoff(live_client, attempt).status_code == 201
    feed = live_client.get("/v1/notifications", headers=auth_header("alice")).json()
    (item,) = [i for i in feed["items"] if i["id"].startswith("task:handoff:")]
    assert item["title"] == "Agent needs you"
    assert item["needs_you"] is True and item["read"] is False
    assert item["href"] == f"/agents/live/{TASK}/browser"
    assert item["id"].endswith(":signin-1")
    assert feed["unread_needs_you"] >= 1


def test_the_reason_is_redacted_before_it_is_stored(live_client, db):
    """§4.1 step 1: the redaction filter runs before the reason is stored or shown."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = running(live_client, db, attempt)
    planted = "ghp_" + "A1b2C3d4" * 5
    response = request_handoff(live_client, attempt, reason=f"use token {planted} please")
    assert response.status_code == 201, response.text
    assert planted not in json.dumps(doc["human_wait"], default=str)
    assert planted not in json.dumps(_events(db), default=str)
    assert planted not in response.text


@pytest.mark.parametrize("variant", ["unsigned", "stale_timestamp", "forged", "child_purpose"])
def test_anything_but_a_fresh_handoff_signature_by_the_registered_key_is_refused(
    live_client, db, variant
):
    """§5.4.1: the agent holds the tenant's ID token and nothing that verifies.
    A signature over the child-task message is not a hand-off signature."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = running(live_client, db, attempt)
    if variant == "unsigned":
        response = request_handoff(live_client, attempt, sign=False)
    elif variant == "stale_timestamp":
        response = request_handoff(live_client, attempt, at=time.time() - 3600)
    elif variant == "forged":
        impostor = Attempt()
        impostor.tuple = attempt.tuple
        response = request_handoff(live_client, impostor)
    else:
        response = request_handoff(live_client, attempt, message=request_message)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "handoff_unproven"
    assert doc.get("human_wait") is None
    assert _events(db) == [] and _audit(db) == []


def test_a_stale_generation_is_fenced_and_writes_nothing(live_client, db):
    """Invariant 5: an old generation's worker, still holding its own key."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = running(live_client, db, attempt)
    doc["current_generation"] = 2
    response = request_handoff(live_client, attempt)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "handoff_fenced"
    assert doc.get("human_wait") is None
    assert _events(db) == [] and _audit(db) == []


@pytest.mark.parametrize(
    "change", [{"state": "PARKED"}, {"cancel_requested": True}, {"current_lease_id": "lease_x"}]
)
def test_a_task_not_running_under_this_live_lease_is_fenced(live_client, db, change):
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = running(live_client, db, attempt)
    doc.update(change)
    response = request_handoff(live_client, attempt)
    assert response.status_code == 409 and response.json()["code"] == "handoff_fenced"
    assert doc.get("human_wait") is None


def test_a_released_lease_is_fenced(live_client, db):
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = running(live_client, db, attempt)
    db.docs[f"leases/{LEASE}"]["released_at"] = datetime.now(timezone.utc)
    response = request_handoff(live_client, attempt)
    assert response.status_code == 409 and response.json()["code"] == "handoff_fenced"
    assert doc.get("human_wait") is None


@pytest.mark.parametrize("token", ["token-alice", OTHER_WORKER_TOKEN])
def test_only_the_tenants_worker_identity_may_call(live_client, db, token):
    """A person has no attempt; another tenant's worker is not this one's."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = running(live_client, db, attempt)
    response = request_handoff(live_client, attempt, token=token)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "handoff_unauthenticated"
    assert doc.get("human_wait") is None


def test_no_attempt_key_configured_means_no_handoff_path(db, worker_tokens, group_map, objects):
    client = _client(db, worker_tokens, group_map, objects, child_key="")
    seed_tenant(db, "eng")
    attempt = Attempt()
    seed_attempt(db, attempt)
    response = request_handoff(client, attempt)
    assert response.status_code == 503 and response.json()["code"] == "handoff_unavailable"


@pytest.mark.parametrize(
    "change",
    [
        {"reason": "x" * 201},
        {"reason": "   "},
        {"expect": "localhost"},
        {"expect": "user@github.com"},
        {"expect": "github.com/login"},
        {"expect": "kubernetes.default.svc"},
        {"request_id": "Not/A/Name"},
    ],
)
def test_a_malformed_request_is_refused(live_client, db, change):
    """§4.1 step 1: reason at most 200 characters; expect a host url_refusal accepts."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = running(live_client, db, attempt)
    response = request_handoff(live_client, attempt, **change)
    assert response.status_code == 422, response.text
    assert doc.get("human_wait") is None


def test_the_same_request_is_answered_once(live_client, db):
    """A worker that retries after a lost answer gets the wait it already made."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = running(live_client, db, attempt)
    assert request_handoff(live_client, attempt).status_code == 201
    again = request_handoff(live_client, attempt)
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False
    assert doc["metadata"][HUMAN_HANDOFFS_METADATA_KEY]["count"] == 1
    assert len(_events(db)) == 1
    other = request_handoff(live_client, attempt, request_id="signin-2")
    assert other.status_code == 409 and other.json()["code"] == "handoff_pending"


def test_hand_offs_per_attempt_are_capped_from_the_task_document(live_client, db):
    """§5.10: 3 per attempt, enforced by swarm-api as well as the worker."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = running(live_client, db, attempt)
    for n in range(MAX_HANDOFFS_PER_ATTEMPT):
        assert request_handoff(live_client, attempt, request_id=f"r-{n}").status_code == 201
        assert resolve_handoff(live_client, attempt, request_id=f"r-{n}",
                               outcome="returned").status_code == 200
    refused = request_handoff(live_client, attempt, request_id="r-over")
    assert refused.status_code == 409 and refused.json()["code"] == "handoff_limit"
    assert doc.get("human_wait") is None


def test_resolving_clears_the_wait_and_records_it(live_client, db):
    """The worker resolves before it parks (§4.3) or on cancel (§4.4)."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = running(live_client, db, attempt)
    assert request_handoff(live_client, attempt).status_code == 201
    response = resolve_handoff(live_client, attempt, outcome="expired")
    assert response.status_code == 200, response.text
    assert doc["human_wait"] is None
    types = [e["type"] for e in _events(db)]
    assert "human_handoff_resolved" in types
    resolved = next(e for e in _events(db) if e["type"] == "human_handoff_resolved")
    assert (resolved["attempt_id"], resolved["lease_id"], resolved["generation"]) == (
        ATTEMPT, LEASE, 1)
    assert [e["entry"] for e in _audit(db)] == ["handoff_requested", "handoff_resolved"]
    # A second resolve of the same request is answered, and changes nothing.
    assert resolve_handoff(live_client, attempt, outcome="expired").status_code == 200
    assert [e["type"] for e in _events(db)].count("human_handoff_resolved") == 1


def test_a_person_cannot_write_human_wait_on_submit(client, db):
    """LB-C: written by swarm-api only, never accepted from a caller."""
    seed_tenant(db, "eng")
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {}, "human_wait": {"reason": "x"}},
    )
    assert response.status_code == 422, response.text


# --------------------------------------------------------------------------
# §4.3: human-ready, the only promotion of HUMAN_REQUIRED
# --------------------------------------------------------------------------


def seed_parked(db, *, task_id: str = TASK, submitted_by: str = "alice@example.com",
                reason: str = "HUMAN_REQUIRED", **extra: Any) -> dict[str, Any]:
    doc = seed_task(db, task_id=task_id, tenant_id="eng", state="PARKED", park_reason=reason,
                    next_eligible_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    doc.update({"submitted_by": submitted_by, "attempt_count": 1, **extra})
    return doc


def test_the_submitter_promotes_a_human_park_to_ready(live_client, db):
    seed_tenant(db, "eng")
    doc = seed_parked(db)
    response = live_client.post(f"/v1/tasks/{TASK}/human-ready", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert doc["state"] == "READY"
    assert doc["park_reason"] is None
    assert doc["next_eligible_at"] is None
    # Recorded, so the next attempt's worker opens the hand-off at once.
    assert doc["metadata"][HUMAN_READY_METADATA_KEY]["by"] == "alice@example.com"
    ready = [e for e in _events(db) if e["type"] == "ready"]
    assert ready and ready[0]["detail"]["reason"] == "human_ready"
    # Invariant 1: READY holds nothing; no lease was made here.
    assert not any(p.startswith("leases/") for p in db.docs)


def test_the_ready_event_names_the_attempt_whose_hand_off_parked_the_task(live_client, db):
    """LB-V6: a person's `ready` answers the hand-off of the attempt that
    parked, so it is filed under that attempt, not before any attempt."""
    seed_tenant(db, "eng")
    seed_parked(db, metadata={HUMAN_HANDOFFS_METADATA_KEY: {"attempt_id": ATTEMPT, "count": 1}})
    response = live_client.post(f"/v1/tasks/{TASK}/human-ready", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    ready = next(e for e in _events(db) if e["type"] == "ready")
    assert ready["attempt_id"] == ATTEMPT


def test_a_ready_with_no_recorded_hand_off_names_no_attempt(live_client, db):
    """The control: nothing to name is named as nothing, never guessed."""
    seed_tenant(db, "eng")
    seed_parked(db)
    response = live_client.post(f"/v1/tasks/{TASK}/human-ready", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    ready = next(e for e in _events(db) if e["type"] == "ready")
    assert ready["attempt_id"] is None


def test_a_tenant_member_who_is_not_the_submitter_may_not(live_client, db):
    """Q1: control, and so readiness, defaults to the submitter."""
    seed_tenant(db, "eng")
    doc = seed_parked(db)
    response = live_client.post(f"/v1/tasks/{TASK}/human-ready", headers=auth_header("root"))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "human_ready_not_eligible"
    assert doc["state"] == "PARKED" and doc["park_reason"] == "HUMAN_REQUIRED"


def test_another_tenants_member_is_told_the_task_does_not_exist(live_client, db):
    """LB-2 and §5.2 step 2: a refusal across tenants is a 404."""
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    doc = seed_parked(db)
    response = live_client.post(f"/v1/tasks/{TASK}/human-ready", headers=auth_header("bob"))
    assert response.status_code == 404, response.text
    assert doc["state"] == "PARKED"


def test_a_service_account_submission_with_no_human_owner_is_any_members(live_client, db):
    """§5.1 row 3: a service account cannot press a button."""
    seed_tenant(db, "eng")
    doc = seed_parked(db, submitted_by=f"swarm-ci-fix@{PROJECT}.iam.gserviceaccount.com")
    response = live_client.post(f"/v1/tasks/{TASK}/human-ready", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    assert doc["state"] == "READY"


def test_a_workflows_human_owner_stands_in_for_a_service_account(live_client, db):
    seed_tenant(db, "eng")
    db.docs["workflows/wf_1"] = {"id": "wf_1", "tenant_id": "eng",
                                 "submitted_by": "alice@example.com"}
    doc = seed_parked(db, submitted_by=f"swarm-ci-fix@{PROJECT}.iam.gserviceaccount.com",
                      workflow_id="wf_1")
    refused = live_client.post(f"/v1/tasks/{TASK}/human-ready", headers=auth_header("root"))
    assert refused.status_code == 403, refused.text
    response = live_client.post(f"/v1/tasks/{TASK}/human-ready", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert doc["state"] == "READY"


@pytest.mark.parametrize(
    "state,reason",
    [("PARKED", "MANUAL_PAUSE"), ("PARKED", "CHILDREN_INCOMPLETE"), ("RUNNING", None),
     ("READY", None)],
)
def test_only_a_human_park_is_promoted_here(live_client, db, state, reason):
    """human-ready is not a general un-park: every other reason is its sweep's."""
    seed_tenant(db, "eng")
    doc = seed_parked(db, reason=reason)
    doc["state"] = state
    response = live_client.post(f"/v1/tasks/{TASK}/human-ready", headers=auth_header("alice"))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "not_waiting_for_a_person"
    assert doc["state"] == state and doc["park_reason"] == reason


def test_a_park_on_the_last_attempt_is_not_promoted(live_client, db):
    """Admission does not check the cap: READY would lease attempt N+1 of N."""
    seed_tenant(db, "eng")
    doc = seed_parked(db, attempt_count=3, max_attempts=3)
    response = live_client.post(f"/v1/tasks/{TASK}/human-ready", headers=auth_header("alice"))
    assert response.status_code == 409, response.text
    assert doc["state"] == "PARKED"


# --------------------------------------------------------------------------
# §4.3: no sweep promotes it; past the maximum it is dead-lettered
# --------------------------------------------------------------------------


def test_no_sweep_promotes_a_human_park(db, make_scheduler):
    seed_tenant(db, "eng")
    doc = seed_parked(db)
    scheduler = make_scheduler(settings=scheduler_settings(enable_prewarm=True))
    for _ in range(3):
        scheduler.drain()
    assert doc["state"] == "PARKED" and doc["park_reason"] == "HUMAN_REQUIRED"


def test_a_human_park_past_the_maximum_is_dead_lettered(db, make_scheduler):
    seed_tenant(db, "eng")
    doc = seed_parked(db)
    doc["next_eligible_at"] = datetime.now(timezone.utc) - timedelta(hours=24, minutes=1)
    make_scheduler().drain()
    assert doc["state"] == "DEAD_LETTERED"
    assert doc["last_error"].startswith(HUMAN_WAIT_EXPIRED)
    dead = [e for e in _events(db) if e["type"] == "dead_lettered"]
    assert dead and dead[0]["detail"]["cause"] == HUMAN_WAIT_EXPIRED
    assert dead[0]["detail"]["park_reason"] == "HUMAN_REQUIRED"


def test_a_human_park_just_inside_the_maximum_waits(db, make_scheduler):
    seed_tenant(db, "eng")
    doc = seed_parked(db)
    doc["next_eligible_at"] = datetime.now(timezone.utc) - timedelta(hours=23)
    make_scheduler().drain()
    assert doc["state"] == "PARKED"


def test_a_human_park_on_its_last_attempt_is_dead_lettered_at_once(db, make_scheduler):
    seed_tenant(db, "eng")
    doc = seed_parked(db, attempt_count=3, max_attempts=3)
    make_scheduler().drain()
    assert doc["state"] == "DEAD_LETTERED"


# --------------------------------------------------------------------------
# §4.3: the keys this lane writes are reserved
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [HUMAN_WAIT_RESUMES_METADATA_KEY, HUMAN_READY_METADATA_KEY, HUMAN_HANDOFFS_METADATA_KEY,
     HUMAN_RETURNED_METADATA_KEY],
)
def test_the_hand_off_metadata_keys_are_reserved(client, db, key):
    seed_tenant(db, "eng")
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {}, "metadata": {key: 1}},
    )
    assert response.status_code == 422, response.text
    assert key in response.json()["detail"]["reserved_metadata_keys"]


def test_human_wait_resumes_is_the_designs_name():
    assert HUMAN_WAIT_RESUMES_METADATA_KEY == "human_wait_resumes"


def test_the_scheduler_restates_the_expiry_cause_swarm_api_names():
    """The scheduler's image does not carry swarm_api, so it restates it."""
    from scheduler import loop

    assert loop.HUMAN_WAIT_EXPIRED == HUMAN_WAIT_EXPIRED


# --------------------------------------------------------------------------
# LB-RETURN (part of #1030): an expiry is refused once the hold was revived
# --------------------------------------------------------------------------


def _resolve_expired(client, attempt: Attempt, seen) -> Any:
    body = json.dumps({**attempt.fields(), "request_id": "signin-1", "outcome": "expired",
                       "seen_deadline": seen.isoformat()}).encode()
    path = f"/v1/tasks/{attempt.tuple.task_id}/handoff/resolve"
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"}
    headers.update(attempt.sign("POST", path, body))
    return client.post(path, content=body, headers=headers)


def test_an_expiry_is_refused_when_a_viewer_moved_the_deadline_after_the_worker_read_it(
    live_client, db
):
    """The worker judged the hold expired on the deadline it read; a viewer
    joined before its resolve landed and moved it. The resolve is answered
    "revived" -- 200, nothing written, the wait kept -- so the worker does not
    park. An answer, not a refusal: a worker from before reads any 200."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = running(live_client, db, attempt)
    assert request_handoff(live_client, attempt).status_code == 201
    seen = datetime.now(timezone.utc) - timedelta(seconds=2)
    doc["human_wait"]["deadline"] = seen + timedelta(seconds=62)  # `LiveService.watched`
    response = _resolve_expired(live_client, attempt, seen)
    assert response.status_code == 200, response.text
    assert response.json() == {"resolved": REVIVED}
    assert isinstance(doc["human_wait"], dict)
    assert "human_handoff_resolved" not in [e["type"] for e in _events(db)]


def test_an_expiry_on_the_deadline_the_worker_read_resolves(live_client, db):
    """The control: nobody moved it, so the expiry stands."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = running(live_client, db, attempt)
    assert request_handoff(live_client, attempt).status_code == 201
    seen = datetime.now(timezone.utc) - timedelta(seconds=2)
    doc["human_wait"]["deadline"] = seen
    response = _resolve_expired(live_client, attempt, seen)
    assert response.status_code == 200, response.text
    assert response.json() == {"resolved": True}
    assert doc["human_wait"] is None


def test_a_new_hand_off_forgets_a_return_from_before_it(live_client, db):
    """A return recorded before this request answers nothing it asks."""
    seed_tenant(db, "eng")
    attempt = Attempt()
    doc = running(live_client, db, attempt, metadata={
        HUMAN_RETURNED_METADATA_KEY: {"attempt_id": ATTEMPT, "generation": 1,
                                      "by": "alice@example.com"}})
    assert request_handoff(live_client, attempt).status_code == 201
    assert HUMAN_RETURNED_METADATA_KEY not in doc["metadata"]


def test_human_ready_forgets_a_recorded_return(live_client, db):
    seed_tenant(db, "eng")
    doc = seed_parked(db, metadata={HUMAN_RETURNED_METADATA_KEY: {"attempt_id": ATTEMPT}})
    assert live_client.post(f"/v1/tasks/{TASK}/human-ready",
                            headers=auth_header("alice")).status_code == 200
    assert HUMAN_RETURNED_METADATA_KEY not in doc["metadata"]
