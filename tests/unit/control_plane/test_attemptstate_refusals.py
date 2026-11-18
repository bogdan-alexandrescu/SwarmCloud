"""The state routes refuse what an agent would forge (docs/design/agent-quality/trusted-state.md §2).

The owner's agent quality program (3 November 2026): a worker -- or an agent
that has minted the worker's token -- must not be able to act on another
tenant's task, another attempt's task, its own task after a newer
generation took it over, or any peer or policy document. `test_attemptstate.py`
holds each proof's refusal on its own; this file holds the four attacks the
inventory names, each end to end through the real app, each asserting that
nothing was written. Offline: no credentials, no emulator.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_api.attemptstate import CHILD_AWAIT_MAX_RESUMES, HUMAN_MAX_RESUMES

from .test_attemptstate import (  # noqa: F401 - fixtures
    ATTEMPT,
    FENCED_WRITES,
    GET_ROUTES,
    LEASE,
    OTHER_WORKER,
    POST_BODIES,
    TASK,
    WORKER,
    Worker,
    client,
    opened,
    seed_attempt,
    worker_tokens,
)

PEER_TASK = "task_peer_1"
PEER_ATTEMPT = "att_peer_1"
PEER_LEASE = "lease_peer_1"
#: The documents that decide what the control plane does for a tenant: no
#: state route writes any of them, whatever a body says.
POLICY_DOCS = (
    "tenants/eng",
    "tenant_settings/eng",
    "workspace_settings/eng",
    "quota/anthropic:eng",
    "control/settings",
    "repositories/eng_peer",
    "tenants/research",
    "tenant_settings/research",
)
#: Every top-level collection a state route may write for its own tuple.
WRITABLE = {"tasks", "leases", "attempts", "pools", "merge_slots", "merge_train_entries",
            "child_keys"}


def _seed_policy(db) -> None:
    db.docs["tenant_settings/eng"] = {"tenant_id": "eng", "max_concurrency": 4}
    db.docs["workspace_settings/eng"] = {"tenant_id": "eng", "merge": {"auto": False}}
    db.docs["quota/anthropic:eng"] = {"tenant_id": "eng", "state": "AVAILABLE"}
    db.docs["control/settings"] = {"additional_repositories_budget_gib": 4}
    db.docs["repositories/eng_peer"] = {"tenant_id": "eng", "url": "https://github.com/a/b"}
    db.docs["tenants/research"] = {"tenant_id": "research"}
    db.docs["tenant_settings/research"] = {"tenant_id": "research", "max_concurrency": 2}


def _policy(db) -> dict[str, Any]:
    return {path: copy.deepcopy(db.docs.get(path)) for path in POLICY_DOCS}


def _post_signed(client, signer: Worker, path_attempt: str, op: str, fields: dict[str, Any],
                 *, token: str = WORKER) -> Any:
    """A POST to `path_attempt`'s route with `fields` as the body, signed by
    `signer`'s key: the forger's request, whatever tuple it names."""
    path = f"/v1/attempts/{path_attempt}/state/{op}"
    raw = json.dumps(fields, default=str).encode()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               **signer.sign("POST", path, raw)}
    return client.post(path, content=raw, headers=headers)


# --------------------------------------------------------------------------
# 1. Another tenant's task
# --------------------------------------------------------------------------


@pytest.mark.parametrize("op", sorted(POST_BODIES))
def test_this_tenants_worker_cannot_write_another_tenants_task(client, db, op):
    """eng's worker, its own token and its own registered key, naming a task
    of `research` in the body: the token proves eng, the body says research."""
    mine = Worker()
    opened(client, db, mine)
    theirs = Worker(tenant="research", task="task_research_1", attempt="att_research_1",
                    lease="lease_research_1", token=OTHER_WORKER)
    seed_attempt(db, theirs, pools=("global", "tenant:research"))
    before = copy.deepcopy(db.docs)

    response = _post_signed(client, mine, theirs.tuple.attempt_id, op,
                            {**theirs.fields(), **POST_BODIES[op]})

    assert response.status_code == 401, response.text
    assert db.docs == before


@pytest.mark.parametrize("op", sorted(GET_ROUTES))
def test_this_tenants_worker_cannot_read_another_tenants_task(client, db, op):
    theirs = Worker(tenant="research", task="task_research_1", attempt="att_research_1",
                    lease="lease_research_1", token=OTHER_WORKER)
    seed_attempt(db, theirs, pools=("global", "tenant:research"))
    response = theirs.get(client, op, GET_ROUTES[op], token=WORKER)

    assert response.status_code == 401, response.text


# --------------------------------------------------------------------------
# 2. Another attempt's task, in the same tenant
# --------------------------------------------------------------------------


def _two_attempts(client, db) -> tuple[Worker, Worker]:
    mine = Worker()
    opened(client, db, mine)
    peer = Worker(task=PEER_TASK, attempt=PEER_ATTEMPT, lease=PEER_LEASE)
    opened(client, db, peer)
    return mine, peer


@pytest.mark.parametrize("op", sorted(POST_BODIES))
def test_an_attempts_key_cannot_write_a_peer_attempts_task(client, db, op):
    """Same tenant, same token: the token cannot tell them apart, so the key
    registered for the PEER's tuple must, at the peer's path and at its own."""
    mine, peer = _two_attempts(client, db)
    before = copy.deepcopy(db.docs)

    at_peer = _post_signed(client, mine, PEER_ATTEMPT, op, {**peer.fields(), **POST_BODIES[op]})
    at_mine = _post_signed(client, mine, ATTEMPT, op, {**peer.fields(), **POST_BODIES[op]})

    assert at_peer.status_code == 403, at_peer.text
    assert at_mine.status_code == 403, at_mine.text
    assert db.docs == before


def test_an_attempts_key_cannot_read_a_peer_attempts_task(client, db):
    mine, peer = _two_attempts(client, db)
    forged = Worker(task=PEER_TASK, attempt=PEER_ATTEMPT, lease=PEER_LEASE)
    forged.private = mine.private  # the peer's tuple, under this attempt's key

    for op in sorted(GET_ROUTES):
        assert forged.get(client, op, GET_ROUTES[op]).status_code == 403, op


def test_an_attempt_cannot_open_a_peer_attempts_tuple_a_second_time(client, db):
    """The peer already registered its key: a second registration for its
    tuple, with the forger's key, is refused and the peer's key stands."""
    mine, peer = _two_attempts(client, db)
    keys_before = {p: copy.deepcopy(d) for p, d in db.docs.items() if p.startswith("child_keys/")}
    forger = Worker(task=PEER_TASK, attempt=PEER_ATTEMPT, lease=PEER_LEASE)

    response = forger.open(client)

    assert response.status_code in (403, 409), response.text
    assert {p: d for p, d in db.docs.items() if p.startswith("child_keys/")} == keys_before
    assert peer.post(client, "heartbeat", {}).status_code == 200


# --------------------------------------------------------------------------
# 3. A stale generation, after a newer one opened
# --------------------------------------------------------------------------


def _superseded(client, db) -> tuple[Worker, Worker]:
    """Generation 1 opened and ran; the reconciler reclaimed it and the
    scheduler leased generation 2, which opened in turn. Generation 1's
    lease is left unreleased and its key registered: only the fence can
    refuse it."""
    old = Worker()
    opened(client, db, old)
    new = Worker(attempt="att_state_2", lease="lease_state_2", generation=2)
    opened(client, db, new)
    assert db.docs[f"tasks/{TASK}"]["current_generation"] == 2
    assert db.docs[f"leases/{LEASE}"]["released_at"] is None
    return old, new


@pytest.mark.parametrize("op", FENCED_WRITES)
def test_a_stale_generation_writes_nothing_after_a_newer_one_opened(client, db, op):
    old, new = _superseded(client, db)
    before = copy.deepcopy(db.docs)

    response = old.post(client, op, POST_BODIES[op])

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "attempt_fenced"
    assert db.docs == before


def test_the_newer_generation_still_writes_after_the_stale_one_was_refused(client, db):
    old, new = _superseded(client, db)
    assert old.post(client, "heartbeat", {}).status_code == 409

    assert new.post(client, "heartbeat", {}).status_code == 200
    response = new.post(client, "finish", {"request_id": "f-new", "state": "SUCCEEDED",
                                           "exit_code": 0})
    assert response.status_code == 200, response.text
    task = db.docs[f"tasks/{TASK}"]
    assert task["state"] == "SUCCEEDED" and task["current_generation"] == 2


def test_a_stale_generation_cannot_finish_over_the_newer_ones_end(client, db):
    """The newer generation parked; the stale one's finish after it would
    turn a parked task terminal. It is refused, and the park stands."""
    old, new = _superseded(client, db)
    parked = new.post(client, "park", {"request_id": "p-new",
                                       "reason": "PROVIDER_QUOTA_EXHAUSTED",
                                       "next_eligible_at": "2030-01-01T00:00:00+00:00"})
    assert parked.status_code == 200, parked.text
    before = copy.deepcopy(db.docs)

    for op in ("finish", "retry", "transition", "checkpoint", "merge-train"):
        response = old.post(client, op, POST_BODIES[op])
        assert response.status_code == 409, (op, response.text)
    assert db.docs == before
    assert db.docs[f"tasks/{TASK}"]["state"] == "PARKED"


def test_a_stale_generation_cannot_open_again(client, db):
    old, new = _superseded(client, db)
    keys_before = {p: copy.deepcopy(d) for p, d in db.docs.items() if p.startswith("child_keys/")}

    response = Worker().open(client)

    assert response.status_code in (403, 409), response.text
    assert {p: d for p, d in db.docs.items() if p.startswith("child_keys/")} == keys_before


# --------------------------------------------------------------------------
# 4. A peer's document or a policy document
# --------------------------------------------------------------------------


@pytest.mark.parametrize("op", sorted(POST_BODIES))
@pytest.mark.parametrize("field", ["document", "collection", "tenant_settings", "metadata",
                                   "runner_profile", "pools"])
def test_a_body_that_names_a_document_or_a_policy_field_is_422(client, db, op, field):
    worker = Worker()
    opened(client, db, worker)
    _seed_policy(db)
    before = copy.deepcopy(db.docs)

    response = worker.post(client, op, {**POST_BODIES[op],
                                        field: {"path": "tenant_settings/eng",
                                                "max_concurrency": 1000}})

    assert response.status_code == 422, response.text
    assert db.docs == before


@pytest.mark.parametrize("op", sorted(POST_BODIES))
def test_no_state_route_writes_a_policy_document_or_a_peers(client, db, op):
    """Each POST route, accepted with its well-formed body: what it wrote is
    this tuple's task, lease, attempt, pools, slot, entry or ledger row --
    never a tenant's settings, its quota, a registration, the platform's
    settings, or a peer task."""
    worker = Worker()
    opened(client, db, worker)
    peer = Worker(task=PEER_TASK, attempt=PEER_ATTEMPT, lease=PEER_LEASE)
    seed_attempt(db, peer)
    _seed_policy(db)
    policy = _policy(db)
    peer_docs = {p: copy.deepcopy(d) for p, d in db.docs.items()
                 if p.startswith((f"tasks/{PEER_TASK}", f"leases/{PEER_LEASE}",
                                  f"attempts/{PEER_ATTEMPT}"))}
    before = copy.deepcopy(db.docs)

    response = worker.post(client, op, POST_BODIES[op])

    assert response.status_code == 200, response.text
    assert _policy(db) == policy
    assert {p: d for p, d in db.docs.items() if p in peer_docs} == peer_docs
    changed = {p for p in set(db.docs) | set(before) if db.docs.get(p) != before.get(p)}
    assert {p.split("/", 1)[0] for p in changed} <= WRITABLE, sorted(changed)


@pytest.mark.parametrize("field", ["state", "current_generation", "tenant_id", "attempt_count",
                                   "budget_usd", "runner_profile"])
def test_usage_cannot_write_a_task_or_policy_field(client, db, field):
    worker = Worker()
    opened(client, db, worker)
    before = copy.deepcopy(db.docs)

    for kind in ("resource", "cpu", "spend"):
        response = worker.post(client, "usage", {"kind": kind, "fields": {field: 0}})
        assert response.status_code == 422, (kind, response.text)
    assert db.docs == before


def test_a_checkpoint_cannot_point_at_a_peer_tasks_prefix(client, db):
    """A peer's checkpoint pointer is the peer's: this task's pointer cannot
    be moved onto a peer's archive (whose restore would hand this task the
    peer's workspace), nor the peer's touched."""
    mine, peer = _two_attempts(client, db)
    before = copy.deepcopy(db.docs)
    body = {**POST_BODIES["checkpoint"],
            "uri": f"gs://swarm-artifacts-example-project/tenants/eng/tasks/{PEER_TASK}/attempts/{PEER_ATTEMPT}/c.tar"}

    assert mine.post(client, "checkpoint", body).status_code == 422
    assert db.docs == before


@pytest.mark.parametrize("reason,counter,bound", [
    ("CHILDREN_INCOMPLETE", "child_await_resumes", CHILD_AWAIT_MAX_RESUMES),
    ("HUMAN_REQUIRED", "human_wait_resumes", HUMAN_MAX_RESUMES),
])
def test_a_park_detail_cannot_reset_a_resume_counter(client, db, reason, counter, bound):
    """The bound on refunded resumes is read from the stored task, never from
    the body: a detail claiming zero resumes refunds nothing past the bound."""
    worker = Worker()
    opened(client, db, worker, attempt_count=2, metadata={counter: bound})

    response = worker.post(client, "park", {
        "request_id": "p-forged", "reason": reason,
        "next_eligible_at": datetime.now(timezone.utc).isoformat(),
        "detail": {counter: 0, "metadata": {counter: 0}, "attempt_count": 0}})

    assert response.status_code == 200, response.text
    task = db.docs[f"tasks/{TASK}"]
    assert response.json()["refunded"] is False
    assert task["attempt_count"] == 2
    assert task["metadata"][counter] == bound
