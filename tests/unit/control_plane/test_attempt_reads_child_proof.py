"""The state reads accept the child-purpose proof too (docs/design/agent-autonomy.md
§3.4; lane AU4-WIRE, part of #1933).

BGD, 2026-10-22 08:54Z: "Also accept the child-purpose proof
(swarm-child-req/v1) on the read route". A tenant not switched to the state
routes registers its attempt key at `/child-key`, for the `child` purpose
only, so its worker can sign the reads only under `swarm-child-req/v1`.

What this file holds, over the real route and the in-memory Firestore:

  * a child-purpose signature, by a key registered at `/child-key`, reads its
    attempt's own tenant;
  * that signature is refused exactly as a state-purpose one is -- the same
    status and the same body -- for another tenant's attempt, a stale
    generation and a released lease;
  * a signature under any other purpose is refused, and a key registered for
    `state` only (an unprotected heap) cannot sign under the child purpose.

Offline: no credentials, no emulator.
"""

from __future__ import annotations

import base64
import hashlib
import time
from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_api import attemptreads
from swarm_api.ratelimit import TokenBucketLimiter

from .test_attempt_reads import MINE, THEIRS, _world, read
from .test_attemptstate import (  # noqa: F401 - fixtures
    LEASE,
    OTHER_WORKER,
    TASK,
    Worker,
    _register_v1,
    client,
    opened,
    seed_attempt,
    worker_tokens,
)

ROUTE = "/v1/attempts/{attempt_id}/reads/{tool}"
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def fresh_budget(monkeypatch):
    monkeypatch.setattr(attemptreads, "_BUDGET", TokenBucketLimiter(120 / 3600.0, 120))
    monkeypatch.delenv(attemptreads.WORKER_LOG_VIEW_ENV, raising=False)


class ChildWorker(Worker):
    """A worker whose key signs every request under `swarm-child-req/v1`."""

    def sign(self, method: str, path: str, body: bytes, *, child: bool = True) -> dict[str, str]:
        return super().sign(method, path, body, child=True)


class PurposeWorker(Worker):
    """A worker signing the request shape under a purpose of the test's choosing."""

    purpose = "swarm-child-nonce/v1"

    def sign(self, method: str, path: str, body: bytes, *, child: bool = False) -> dict[str, str]:
        timestamp = str(int(time.time()))
        digest = hashlib.sha256(body).hexdigest()
        message = f"{self.purpose}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode()
        proof = base64.urlsafe_b64encode(self.private.sign(message)).decode().rstrip("=")
        return {"X-Swarm-Attempt-Proof": proof, "X-Swarm-Attempt-Timestamp": timestamp}


def keyed(client, db, purpose: str, worker: Worker | None = None) -> Worker:
    """An attempt RUNNING with its key registered for `purpose`, and its world.

    `state`: `state/open` registered the key (child and state purposes) and the
    worker signs under `swarm-state-req/v1`. `child`: `/child-key` registered it
    (child purpose only, the v1 registration) and the worker signs under
    `swarm-child-req/v1`."""
    if purpose == "state":
        worker = worker or Worker()
        opened(client, db, worker)
    else:
        worker = worker or ChildWorker()
        seed_attempt(db, worker, state="STARTING")
        response = _register_v1(client, worker)
        assert response.status_code == 201, response.text
        db.docs[f"tasks/{TASK}"]["state"] = "RUNNING"
        db.docs[f"leases/{LEASE}"]["state"] = "RUNNING"
    _world(db)
    return worker


def test_a_child_purpose_signature_reads_its_own_tenant(client, db):
    worker = keyed(client, db, "child")
    response = read(client, worker, "agents_list", "state=FAILED")
    # MUTATION: accepting only `swarm-state-req/v1` on the read route (today's
    # `AttemptState.call_get` alone) answers this 403 and turns it red.
    assert response.status_code == 200, response.text
    # MUTATION: scoping a child-signed read by anything but the lease's tenant turns this red.
    assert response.json()["tenant_id"] == "eng"
    assert [a["id"] for a in response.json()["result"]["agents"]] == [MINE]
    view = read(client, worker, "agent_view", f"agent_id={MINE}")
    # MUTATION: a child-purpose path that skips a handler's own reads turns this red.
    assert view.status_code == 200 and view.json()["result"]["agent"]["id"] == MINE
    theirs = read(client, worker, "agent_view", f"agent_id={THEIRS}")
    # MUTATION: a child-signed read that is not scoped to the lease's tenant serves THEIRS.
    assert theirs.status_code == 404 and "research" not in theirs.text


def test_a_state_open_key_signs_the_reads_under_either_purpose(client, db):
    worker = keyed(client, db, "state")
    assert read(client, worker, "agents_list", "state=FAILED").status_code == 200
    child = ChildWorker()
    child.private, child.public = worker.private, worker.public
    # MUTATION: refusing the child purpose for a key `state/open` registered for
    # `child` and `state` turns this red.
    assert read(client, child, "agents_list", "state=FAILED").status_code == 200


def _refusals(client, db, purpose: str) -> dict[str, Any]:
    """Each refusal's (status, body) for a worker signing under `purpose`."""
    out: dict[str, Any] = {}
    worker = keyed(client, db, purpose)
    # Another tenant's attempt, signed by this attempt's key: as that tenant's
    # worker (its token) and as this one's.
    cls = ChildWorker if purpose == "child" else Worker
    foreign = cls(tenant="research", task=THEIRS, attempt=f"att_{THEIRS}",
                  lease=f"lease_att_{THEIRS}", token=OTHER_WORKER)
    foreign.private, foreign.public = worker.private, worker.public
    db.docs[f"leases/lease_att_{THEIRS}"] = {
        **db.docs[f"leases/{LEASE}"], "lease_id": f"lease_att_{THEIRS}", "task_id": THEIRS,
        "attempt_id": f"att_{THEIRS}", "tenant_id": "research"}
    db.docs[f"tasks/{THEIRS}"].update({"state": "RUNNING", "current_lease_id":
                                       f"lease_att_{THEIRS}", "current_generation": 1})
    response = read(client, foreign, "agents_list", "state=FAILED")
    out["foreign_tenant_own_token"] = (response.status_code, response.json())
    foreign.token = worker.token
    response = read(client, foreign, "agents_list", "state=FAILED")
    out["foreign_tenant_this_token"] = (response.status_code, response.json())
    # A stale generation.
    db.docs[f"tasks/{TASK}"]["current_generation"] = 2
    db.docs[f"leases/{LEASE}"]["generation"] = 2
    response = read(client, worker, "agent_view", f"agent_id={MINE}")
    out["stale_generation"] = (response.status_code, response.json())
    # A released lease.
    db.docs[f"tasks/{TASK}"]["current_generation"] = 1
    db.docs[f"leases/{LEASE}"].update({"generation": 1, "released_at": NOW})
    response = read(client, worker, "agent_view", f"agent_id={MINE}")
    out["released_lease"] = (response.status_code, response.json())
    return out


EXPECTED = {
    "foreign_tenant_own_token": (403, "attempt_unproven"),
    "foreign_tenant_this_token": (401, "attempt_unauthenticated"),
    "stale_generation": (409, "attempt_fenced"),
    "released_lease": (409, "attempt_fenced"),
}


@pytest.mark.parametrize("purpose", ["state", "child"])
def test_a_child_signature_is_refused_where_a_state_one_is(client, db, purpose):
    refused = _refusals(client, db, purpose)
    for case, (status, code) in EXPECTED.items():
        got_status, body = refused[case]
        # MUTATION: a child-purpose path that skips the worker-token check (401),
        # the registration's attempt match (403) or the fence read (409) turns this red.
        assert (got_status, body.get("code")) == (status, code), (purpose, case, body)
        # MUTATION: a child-purpose refusal worded apart from the state one
        # (`child_submit_unproven`) is an oracle on the purpose and turns this red.
        assert "research" not in str(body) and "child" not in str(body.get("message")), case


def test_both_purposes_refuse_with_one_body(client, db):
    bodies = {}
    for purpose in ("state", "child"):
        db.docs.clear()  # the same app over an empty world for each purpose
        bodies[purpose] = _refusals(client, db, purpose)
    # MUTATION: any refusal whose status or body differs by purpose turns this red.
    assert bodies["state"] == bodies["child"]


def test_an_unknown_purpose_is_refused(client, db):
    worker = keyed(client, db, "child")
    for purpose in ("swarm-child-nonce/v1", "swarm-child-reg/v1", "swarm-state-req/v2",
                    "swarm-git-req/v1"):
        other = PurposeWorker()
        other.purpose = purpose
        other.private, other.public = worker.private, worker.public
        response = read(client, other, "agents_list", "state=FAILED")
        # MUTATION: verifying the signature without its purpose prefix, or
        # accepting any purpose but `swarm-state-req/v1` and `swarm-child-req/v1`, turns this red.
        assert response.status_code == 403, (purpose, response.text)
        assert response.json()["code"] == "attempt_unproven"


def test_a_child_key_cannot_sign_a_state_route(client, db):
    """The read route widens; the state WRITE routes do not."""
    worker = keyed(client, db, "child")
    state = Worker()
    state.private, state.public = worker.private, worker.public
    # MUTATION: letting a `/child-key` registration verify under the state
    # purpose (on the reads or the writes) turns this red.
    assert read(client, state, "agents_list", "state=FAILED").status_code == 403
    assert worker.post(client, "heartbeat", {}).status_code == 403


def test_a_state_only_key_cannot_sign_under_the_child_purpose(client, db):
    """Owner decision Q4: an unprotected heap's key signs STATE only."""
    worker = Worker()
    seed_attempt(db, worker)
    assert worker.open(client, unprotected=True).status_code == 200
    db.docs[f"tasks/{TASK}"]["state"] = "RUNNING"
    _world(db)
    assert read(client, worker, "agents_list", "state=FAILED").status_code == 200
    child = ChildWorker()
    child.private, child.public = worker.private, worker.public
    response = read(client, child, "agents_list", "state=FAILED")
    # MUTATION: accepting a child-purpose proof from a key not registered for
    # `child` (asking `_registration` for STATE on the child path) turns this red.
    assert response.status_code == 403 and response.json()["code"] == "attempt_unproven"


def test_a_child_proof_over_another_path_is_refused(client, db):
    worker = keyed(client, db, "child")
    path = ROUTE.format(attempt_id=worker.tuple.attempt_id, tool="agents_list")
    query = worker.query() + "&state=FAILED"
    headers = {"Authorization": f"Bearer {worker.token}",
               **worker.sign("GET", path, b"")}  # the path without its query
    response = client.get(f"{path}?{query}", headers=headers)
    # MUTATION: verifying a child proof over anything but the path WITH its query turns this red.
    assert response.status_code == 403 and response.json()["code"] == "attempt_unproven"
