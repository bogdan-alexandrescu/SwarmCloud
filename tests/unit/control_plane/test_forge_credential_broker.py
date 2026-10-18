"""The forge-credential broker (docs/design/user-scoped-secrets.md §3.2).

`POST /v1/attempts/forge-credential` releases a person's `git-u-` slot only to
the CURRENT attempt of a task that person submitted. Driven over the real
routes and the in-memory Firestore, with a worker identity the static verifier
maps to the tenant's worker service account and an attempt key registered the
way the worker registers it (`test_child_tasks_api.py`'s helpers).

The Secret Manager reader is a fake that records every secret name it is asked
for, so "refused before any secret read" is a measurement: `store.asked`
stays empty. Every token is built at runtime.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import credbroker, forge
from swarm_api.auth import StaticTokenVerifier
from swarm_api.childkey import CHILD_KEYS, AttemptTuple, ChildKeys
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.gittokens import Scope, owner_suffix, provider_suffix, secret_name_for
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import api_settings, seed_grant, seed_tenant
from .spec_signer import KEY_VERSION, LocalSpecSigner
from .test_child_tasks_api import (  # noqa: F401 -- worker_tokens is a fixture
    KEY,
    OTHER_WORKER_TOKEN,
    WORKER_TOKEN,
    Attempt,
    register,
    seed_parent,
    worker_tokens,
)

PATH = "/v1/attempts/forge-credential"
ALICE = "alice@saga.xyz"
BOB = "bob@saga.xyz"
REPOSITORY = "acme/widgets"
REPO_URL = f"https://github.com/{REPOSITORY}.git"

SIGNER = LocalSpecSigner()


def make_token() -> str:
    # Built at runtime, never one literal (CLAUDE.md, "Nothing you add may
    # look like a credential").
    return "ghu_" + "x" * 36


class SecretStore:
    """Secret Manager's git-token slots, read through `read_slot` as swarm-api reads them.

    A slot holds only what a test `put` there. `asked` is every secret name
    read, in order, so a test can say whether -- and which -- secret was read.
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.asked: list[str] = []

    def put(self, secret_id: str, value: str) -> None:
        self.values[secret_id] = value

    def token_for(self, tenant) -> str:
        return self.read_slot(tenant, forge.GIT_PROVIDER).value

    def read_slot(self, tenant, provider: str) -> forge.SlotValue:
        secret_id = tenant.secret_name(provider)
        self.asked.append(secret_id)
        if secret_id not in self.values:
            raise forge.NoForgeCredential(f"no value stored in {secret_id}")
        return forge.SlotValue(self.values[secret_id], "1")


@pytest.fixture
def store() -> SecretStore:
    return SecretStore()


def _client(db, worker_tokens, group_map, objects, store, **settings: Any) -> TestClient:
    values = {
        "child_key": KEY,
        "spec_signing_key_version": KEY_VERSION,
        "spec_verify_keys": json.dumps({KEY_VERSION: SIGNER.public_pem()}),
        **settings,
    }
    ctx = build_context(
        settings=api_settings(**values),
        db=db,
        signer=SIGNER,
        verifier=StaticTokenVerifier(worker_tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=store,
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


@pytest.fixture
def client(db, worker_tokens, group_map, objects, store) -> TestClient:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    return _client(db, worker_tokens, group_map, objects, store)


def app_slot(email: str) -> str:
    return provider_suffix(Scope.USER, user=email)


def person_task(client, db, attempt: Attempt, *, submitted_by: str = ALICE,
                forge_credential: str | None = None, forge_access: str = "write",
                register_key: bool = True, **extra: Any) -> dict[str, Any]:
    """A RUNNING task whose attempt registered its key while STARTING, signed
    at submission as swarm-api signs it (format 3 covers the forge fields)."""
    doc = seed_parent(
        db, attempt, state="STARTING", submitted_by=submitted_by,
        repository_url=REPO_URL,
        forge_credential=forge_credential or app_slot(submitted_by),
        forge_access=forge_access, **extra,
    )
    if register_key:
        response = register(client, db, attempt)
        assert response.status_code == 201, response.text
    doc["state"] = "RUNNING"
    SIGNER.sign_document(doc, doc["id"])
    return doc


def body_for(attempt: Attempt, *, access: str = "write", task: str | None = None) -> bytes:
    t = attempt.tuple
    return json.dumps({
        "tenant_id": t.tenant_id, "task_id": task or t.task_id, "attempt_id": t.attempt_id,
        "lease_id": t.lease_id, "generation": t.generation, "access": access,
    }).encode()


def ask(client, attempt: Attempt, *, access: str = "write", body: bytes | None = None,
        signer: Attempt | None = None, token: str = WORKER_TOKEN, sign: bool = True,
        at: float | None = None) -> Any:
    raw = body if body is not None else body_for(attempt, access=access)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if sign:
        headers.update((signer or attempt).sign("POST", PATH, raw, at=at))
    return client.post(PATH, content=raw, headers=headers)


def _secret(suffix: str, tenant: str = "eng") -> str:
    return secret_name_for(tenant, suffix)


# --------------------------------------------------------------------------
# (1) the submitter's own attempt is released the submitter's slots
# --------------------------------------------------------------------------


def test_alices_attempt_receives_alices_slot(client, db, store):
    seed_grant(db, "eng", ALICE, REPOSITORY)
    app_token, owner_token = make_token(), "ghp_" + "y" * 36
    store.put(_secret(app_slot(ALICE)), app_token)
    store.put(_secret(owner_suffix(ALICE, "acme")), owner_token)

    via_app = Attempt(task="task_alice_app", attempt="att_a1", lease="lease_a1")
    person_task(client, db, via_app)
    response = ask(client, via_app)
    assert response.status_code == 200, response.text
    assert response.json() == {"secret": _secret(app_slot(ALICE)), "token": app_token}
    assert response.headers["cache-control"] == "no-store"

    # Her fallback token for the repository's owner (D5) is hers too.
    via_owner = Attempt(task="task_alice_own", attempt="att_a2", lease="lease_a2")
    person_task(client, db, via_owner, forge_credential=owner_suffix(ALICE, "acme"))
    response = ask(client, via_owner)
    assert response.status_code == 200, response.text
    assert response.json() == {"secret": _secret(owner_suffix(ALICE, "acme")),
                               "token": owner_token}
    assert store.asked == [_secret(app_slot(ALICE)), _secret(owner_suffix(ALICE, "acme"))]


def test_a_read_grant_releases_for_a_read(client, db, store):
    seed_grant(db, "eng", ALICE, REPOSITORY, mode="read")
    store.put(_secret(app_slot(ALICE)), make_token())
    attempt = Attempt()
    person_task(client, db, attempt, forge_access="read")
    response = ask(client, attempt, access="read")
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------
# (2) and (3) Bob's attempt cannot have Alice's slot
# --------------------------------------------------------------------------


def test_bobs_attempt_is_refused_alices_slot_and_reads_no_secret(client, db, store):
    """Bob's task names Alice's slot, signed as if submission had put it there:
    the slot is recomputed from Bob, the submitter, and is not his."""
    seed_grant(db, "eng", ALICE, REPOSITORY)
    seed_grant(db, "eng", BOB, REPOSITORY)
    store.put(_secret(app_slot(ALICE)), make_token())
    store.put(_secret(owner_suffix(ALICE, "acme")), make_token())

    for n, slot in enumerate((app_slot(ALICE), owner_suffix(ALICE, "acme"))):
        bobs = Attempt(task=f"task_bob_{n}", attempt=f"att_b{n}", lease=f"lease_b{n}")
        person_task(client, db, bobs, submitted_by=BOB, forge_credential=slot)
        response = ask(client, bobs)
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "credential_not_submitters"
        assert "token" not in response.json()
    assert store.asked == []


def test_bobs_agent_rewriting_the_submitter_fails_the_spec_check(client, db, store):
    """A tenant identity can rewrite its own task to name Alice. The signature
    made at submission no longer matches, so nothing is read."""
    seed_grant(db, "eng", ALICE, REPOSITORY)
    store.put(_secret(app_slot(ALICE)), make_token())
    bobs = Attempt()
    doc = person_task(client, db, bobs, submitted_by=BOB)
    doc["submitted_by"] = ALICE
    doc["forge_credential"] = app_slot(ALICE)
    response = ask(client, bobs)
    assert response.status_code == 403 and response.json()["code"] == "spec_unverified"
    assert store.asked == []


def test_bob_cannot_sign_alices_tuple_with_his_own_key(client, db, store):
    seed_grant(db, "eng", ALICE, REPOSITORY)
    store.put(_secret(app_slot(ALICE)), make_token())
    alices = Attempt(task="task_alice", attempt="att_a", lease="lease_a")
    bobs = Attempt(task="task_bob", attempt="att_b", lease="lease_b")
    person_task(client, db, alices)
    person_task(client, db, bobs, submitted_by=BOB)
    response = ask(client, alices, signer=bobs)
    assert response.status_code == 403
    assert response.json()["code"] == "forge_credential_unproven"
    assert store.asked == []


# --------------------------------------------------------------------------
# (4) unproven and superseded attempts read nothing
# --------------------------------------------------------------------------


def test_a_superseded_generation_is_refused_before_any_read(client, db, store):
    seed_grant(db, "eng", ALICE, REPOSITORY)
    store.put(_secret(app_slot(ALICE)), make_token())
    stale = Attempt()
    doc = person_task(client, db, stale)
    # The reconciler reclaimed generation 1 and generation 2 holds the task now.
    doc["current_generation"] = 2
    doc["current_lease_id"] = "lease_parent_2"
    response = ask(client, stale)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "attempt_superseded"
    assert store.asked == []


@pytest.mark.parametrize("how", ["released", "finished"])
def test_a_released_lease_or_finished_task_is_superseded(client, db, store, how):
    seed_grant(db, "eng", ALICE, REPOSITORY)
    store.put(_secret(app_slot(ALICE)), make_token())
    attempt = Attempt()
    doc = person_task(client, db, attempt)
    if how == "released":
        db.docs[f"leases/{attempt.tuple.lease_id}"]["released_at"] = doc["created_at"]
    else:
        doc["state"] = "SUCCEEDED"
    response = ask(client, attempt)
    assert response.status_code == 403 and response.json()["code"] == "attempt_superseded"
    assert store.asked == []


def test_an_unregistered_attempt_is_unproven(client, db, store):
    seed_grant(db, "eng", ALICE, REPOSITORY)
    store.put(_secret(app_slot(ALICE)), make_token())
    attempt = Attempt()
    person_task(client, db, attempt, register_key=False)
    response = ask(client, attempt)
    assert response.status_code == 403
    assert response.json()["code"] == "forge_credential_unproven"
    assert store.asked == []


def test_a_tombstoned_registration_is_unproven(client, db, store):
    seed_grant(db, "eng", ALICE, REPOSITORY)
    store.put(_secret(app_slot(ALICE)), make_token())
    attempt = Attempt()
    person_task(client, db, attempt, register_key=False)
    db.docs[f"tasks/{attempt.tuple.task_id}"]["state"] = "STARTING"
    t = attempt.tuple
    from swarm_api.childkey import nonce

    tombstone = {"tenant_id": t.tenant_id, "task_id": t.task_id, "lease_id": t.lease_id,
                 "generation": t.generation, "nonce": nonce(KEY, t), "key": None,
                 "refused": "worker_unprotected"}
    assert register(client, db, attempt, body=tombstone).status_code == 201
    db.docs[f"tasks/{attempt.tuple.task_id}"]["state"] = "RUNNING"
    response = ask(client, attempt)
    assert response.status_code == 403
    assert response.json()["code"] == "forge_credential_unproven"
    assert store.asked == []


def test_a_bad_signature_and_an_expired_timestamp_are_unproven(client, db, store):
    seed_grant(db, "eng", ALICE, REPOSITORY)
    store.put(_secret(app_slot(ALICE)), make_token())
    attempt = Attempt()
    person_task(client, db, attempt)

    # Signed over another body: the access was changed after signing.
    signed = attempt.sign("POST", PATH, body_for(attempt, access="read"))
    response = client.post(PATH, content=body_for(attempt, access="write"), headers={
        "Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json", **signed})
    assert response.status_code == 403
    assert response.json()["code"] == "forge_credential_unproven"

    expired = ask(client, attempt, at=time.time() - 3600)
    assert expired.status_code == 403
    assert expired.json()["code"] == "forge_credential_unproven"

    unsigned = ask(client, attempt, sign=False)
    assert unsigned.status_code == 403
    assert unsigned.json()["code"] == "forge_credential_unproven"
    assert store.asked == []


@pytest.mark.parametrize("token", ["token-alice", "token-root", OTHER_WORKER_TOKEN])
def test_only_the_tenants_worker_identity_may_ask(client, db, store, token):
    """A person has no attempt, an admin included; another tenant's worker is
    another tenant."""
    seed_grant(db, "eng", ALICE, REPOSITORY)
    store.put(_secret(app_slot(ALICE)), make_token())
    attempt = Attempt()
    person_task(client, db, attempt)
    response = ask(client, attempt, token=token)
    assert response.status_code == 403
    assert response.json()["code"] == "forge_credential_unauthenticated"
    assert store.asked == []


def test_no_child_key_means_no_broker(db, worker_tokens, group_map, objects, store):
    seed_tenant(db, "eng")
    client = _client(db, worker_tokens, group_map, objects, store, child_key="")
    attempt = Attempt()
    seed_parent(db, attempt, forge_credential=app_slot(ALICE), forge_access="write")
    response = ask(client, attempt)
    assert response.status_code == 503
    assert response.json()["code"] == "forge_credential_unavailable"
    assert store.asked == []


# --------------------------------------------------------------------------
# (5) another tenant's task is not confirmed to exist
# --------------------------------------------------------------------------


def _attest_directly(db, attempt: Attempt) -> None:
    """A registration as swarm-api would store it, written without the route:
    the route itself refuses to register a tuple whose task is another
    tenant's, so this is the only way to reach the tenant check behind it."""
    keys = ChildKeys(KEY)
    t = attempt.tuple
    db.docs[f"{CHILD_KEYS}/{keys.registration_ids(t)[0]}"] = {
        "tenant_id": t.tenant_id, "task_id": t.task_id, "attempt_id": t.attempt_id,
        "lease_id": t.lease_id, "generation": t.generation, "public_key": attempt.public,
        "refused": None, "attestation": keys.attest(t, attempt.public, None),
    }


def test_a_task_of_another_tenant_is_404_to_this_tenant(client, db, store):
    seed_grant(db, "research", ALICE, REPOSITORY)
    store.put(_secret(app_slot(ALICE), "research"), make_token())
    theirs = Attempt(tenant="research", task="task_research")
    person_task(client, db, theirs, register_key=False)
    # eng's worker names research's task under its own tenant.
    ours = Attempt(tenant="eng", task="task_research")
    _attest_directly(db, ours)
    response = ask(client, ours)
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "task_not_found"

    missing = Attempt(tenant="eng", task="task_nowhere")
    _attest_directly(db, missing)
    nowhere = ask(client, missing)
    assert nowhere.status_code == 404
    assert nowhere.json()["message"] == response.json()["message"]
    assert store.asked == []


# --------------------------------------------------------------------------
# (6) team credentials are not the broker's
# --------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["git", "git-r-" + "0" * 16])
def test_a_tenant_or_repository_slot_is_refused(client, db, store, suffix):
    seed_grant(db, "eng", ALICE, REPOSITORY)
    store.put(_secret(suffix), make_token())
    attempt = Attempt()
    person_task(client, db, attempt, forge_credential=suffix)
    response = ask(client, attempt)
    assert response.status_code == 403
    assert response.json()["code"] == "credential_not_user_slot"
    assert store.asked == []


# --------------------------------------------------------------------------
# the grant, read again at release
# --------------------------------------------------------------------------


def test_a_removed_grant_is_refused(client, db, store):
    store.put(_secret(app_slot(ALICE)), make_token())
    attempt = Attempt()
    person_task(client, db, attempt)
    response = ask(client, attempt)
    assert response.status_code == 403 and response.json()["code"] == "grant_removed"
    assert store.asked == []


def test_a_write_on_a_read_grant_is_refused(client, db, store):
    seed_grant(db, "eng", ALICE, REPOSITORY, mode="read")
    store.put(_secret(app_slot(ALICE)), make_token())
    attempt = Attempt()
    person_task(client, db, attempt)
    response = ask(client, attempt, access="write")
    assert response.status_code == 403 and response.json()["code"] == "grant_read_only"
    assert store.asked == []


def test_a_write_on_a_task_signed_for_read_is_refused(client, db, store):
    seed_grant(db, "eng", ALICE, REPOSITORY)
    store.put(_secret(app_slot(ALICE)), make_token())
    attempt = Attempt()
    person_task(client, db, attempt, forge_access="read")
    response = ask(client, attempt, access="write")
    assert response.status_code == 403 and response.json()["code"] == "grant_read_only"
    assert store.asked == []


def test_a_disconnected_slot_is_unavailable(client, db, store):
    seed_grant(db, "eng", ALICE, REPOSITORY)
    attempt = Attempt()
    person_task(client, db, attempt)
    response = ask(client, attempt)
    assert response.status_code == 403
    assert response.json()["code"] == "credential_unavailable"
    assert store.asked == [_secret(app_slot(ALICE))]


def test_no_verify_keys_releases_nothing(db, worker_tokens, group_map, objects, store):
    seed_tenant(db, "eng")
    client = _client(db, worker_tokens, group_map, objects, store, spec_verify_keys="")
    seed_grant(db, "eng", ALICE, REPOSITORY)
    store.put(_secret(app_slot(ALICE)), make_token())
    attempt = Attempt()
    person_task(client, db, attempt)
    response = ask(client, attempt)
    assert response.status_code == 403 and response.json()["code"] == "spec_unverified"
    assert store.asked == []


# --------------------------------------------------------------------------
# (7) the token is never logged or put in a header
# --------------------------------------------------------------------------


def test_the_token_is_in_no_log_and_no_header(client, db, store, caplog):
    seed_grant(db, "eng", ALICE, REPOSITORY)
    token = make_token()
    store.put(_secret(app_slot(ALICE)), token)
    attempt = Attempt()
    person_task(client, db, attempt)
    with caplog.at_level(logging.DEBUG):
        response = ask(client, attempt)
        refused = ask(client, attempt, access="write", at=time.time() - 3600)
    assert response.status_code == 200 and response.json()["token"] == token
    assert refused.status_code == 403
    for answer in (response, refused):
        for name, value in answer.headers.items():
            assert token not in name and token not in value
    assert token not in refused.text
    for record in caplog.records:
        assert token not in record.getMessage()
        assert token not in repr(record.args)
    released = [r.getMessage() for r in caplog.records if r.name == credbroker.log.name]
    assert any(
        "released" in m and attempt.tuple.task_id in m and _secret(app_slot(ALICE)) in m
        for m in released
    ), released


# --------------------------------------------------------------------------
# the decision, as a pure function
# --------------------------------------------------------------------------


def test_the_decision_derives_the_slot_from_the_signed_submitter():
    task = {"submitted_by": "Alice@Saga.xyz", "forge_credential": app_slot(ALICE),
            "forge_access": "write", "repository_url": REPO_URL}
    modes = {(ALICE, "x"): "write"}
    seen: list[tuple[str, str]] = []

    def grant_mode(tenant_id: str, email: str, repo_id: str) -> str | None:
        assert tenant_id == "eng"
        seen.append((email, repo_id))
        return modes.get((ALICE, "x"))

    release = credbroker.decide_slot(task, tenant_id="eng", access="write",
                                     grant_mode=grant_mode)
    assert release.suffix == app_slot(ALICE)
    assert seen and seen[0][0] == "Alice@Saga.xyz"

    with pytest.raises(credbroker.CredentialNotSubmitters):
        credbroker.decide_slot({**task, "submitted_by": BOB}, tenant_id="eng",
                               access="write", grant_mode=grant_mode)
    with pytest.raises(credbroker.CredentialNotSubmitters):
        credbroker.decide_slot({**task, "submitted_by": None}, tenant_id="eng",
                               access="write", grant_mode=grant_mode)
    with pytest.raises(credbroker.GrantRemoved):
        credbroker.decide_slot(task, tenant_id="eng", access="write",
                               grant_mode=lambda t, e, r: None)


def test_the_tuple_must_be_the_tasks_current_attempt():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    t = AttemptTuple("eng", "task_1", "att_1", "lease_1", 3)
    task = {"tenant_id": "eng", "state": "RUNNING", "current_lease_id": "lease_1",
            "current_generation": 3}
    lease = {"tenant_id": "eng", "task_id": "task_1", "attempt_id": "att_1", "generation": 3,
             "released_at": None, "expires_at": now + timedelta(minutes=5)}
    assert credbroker.is_current(task, lease, t, now)
    assert not credbroker.is_current({**task, "current_generation": 4}, lease, t, now)
    assert not credbroker.is_current({**task, "state": "PARKED"}, lease, t, now)
    assert not credbroker.is_current(task, {**lease, "attempt_id": "att_0"}, t, now)
    assert not credbroker.is_current(task, {**lease, "expires_at": now}, t, now)
    assert not credbroker.is_current(task, None, t, now)


# --------------------------------------------------------------------------
# (8) a child task: its slot is its parent's SIGNED submitter's, or nothing
# --------------------------------------------------------------------------
#
# Workers may update any Firestore document, so an agent can rewrite its own
# task as another member's. The child route copies `submitted_by`,
# `repository_url`, `forge_credential` and `forge_access` from the stored
# parent and signs the child with swarm-api's own key; read without checking
# the parent's signature, Bob's agent got a freshly signed child naming
# Alice's slot, and the broker released it (security review of PR 1070,
# 2026-10-18). Each test drives the child route, then the child's own attempt
# against the broker, so "no user slot released" is `store.asked`.


def _submit_child(client, attempt: Attempt, request_id: str = "split-1") -> Any:
    from .test_child_tasks_api import submit

    return submit(client, attempt, request_id)


def _children_of(db, parent: str) -> list[dict[str, Any]]:
    return [doc for path, doc in db.docs.items()
            if path.startswith("tasks/") and path.count("/") == 1
            and doc.get("parent_task_id") == parent]


def _run_child(client, db, child: dict[str, Any]) -> Attempt:
    """The child dispatched: a live lease, its key registered while STARTING,
    then RUNNING -- what the scheduler and the child's worker leave. Touches
    no signed field."""
    from datetime import datetime, timedelta, timezone

    attempt = Attempt(task=child["id"], attempt="att_child_1", lease="lease_child_1")
    now = datetime.now(timezone.utc)
    child.update({"state": "STARTING", "current_lease_id": "lease_child_1",
                  "current_generation": 1, "attempt_count": 1})
    db.docs["leases/lease_child_1"] = {
        "lease_id": "lease_child_1", "task_id": child["id"], "attempt_id": "att_child_1",
        "tenant_id": child["tenant_id"], "generation": 1, "pools": [], "units": 1,
        "state": "STARTING", "created_at": now,
        "dispatch_deadline": now + timedelta(minutes=10),
        "expires_at": now + timedelta(minutes=10), "released_at": None,
    }
    response = register(client, db, attempt)
    assert response.status_code == 201, response.text
    child["state"] = "RUNNING"
    return attempt


def test_a_child_of_bobs_signed_task_receives_bobs_slot(client, db, store):
    """The control: the child path and the broker work together, so the
    refusal below is the signature check and not a broken child path."""
    seed_grant(db, "eng", BOB, REPOSITORY)
    bob_token, alice_token = make_token(), "ghu_" + "a" * 36
    store.put(_secret(app_slot(BOB)), bob_token)
    store.put(_secret(app_slot(ALICE)), alice_token)
    parent = Attempt()
    person_task(client, db, parent, submitted_by=BOB)

    made = _submit_child(client, parent)
    assert made.status_code == 201, made.text
    (child,) = _children_of(db, parent.tuple.task_id)
    assert child["submitted_by"] == BOB and child["forge_credential"] == app_slot(BOB)
    assert SIGNER.verifies(child, child["id"]) and child["spec_format"] >= 3

    response = ask(client, _run_child(client, db, child))
    assert response.status_code == 200, response.text
    assert response.json() == {"secret": _secret(app_slot(BOB)), "token": bob_token}
    assert store.asked == [_secret(app_slot(BOB))]


@pytest.mark.parametrize("rewrite", ["submitted_by", "forge_credential", "both", "unsigned"])
def test_a_rewritten_parent_gets_no_child_and_no_user_slot(client, db, store, rewrite):
    """Bob's agent rewrites its own task as Alice's after it was signed: no
    child is made, and nothing a child could run releases Alice's slot."""
    seed_grant(db, "eng", ALICE, REPOSITORY)
    seed_grant(db, "eng", BOB, REPOSITORY)
    store.put(_secret(app_slot(ALICE)), "ghu_" + "a" * 36)
    store.put(_secret(app_slot(BOB)), make_token())
    parent = Attempt()
    doc = person_task(client, db, parent, submitted_by=BOB)
    if rewrite in ("submitted_by", "both"):
        doc["submitted_by"] = ALICE
    if rewrite in ("forge_credential", "both"):
        doc["forge_credential"] = app_slot(ALICE)
    if rewrite == "unsigned":
        doc.update({"submitted_by": ALICE, "forge_credential": app_slot(ALICE)})
        for key in ("spec_signature", "spec_key_version", "spec_format"):
            doc.pop(key, None)

    made = _submit_child(client, parent)

    # Whatever the route answered, run every child it made: none may be
    # released Alice's slot.
    for child in _children_of(db, parent.tuple.task_id):
        ask(client, _run_child(client, db, child))
    assert _secret(app_slot(ALICE)) not in store.asked, "Alice's slot was released"
    assert made.status_code == 403, made.text
    assert made.json()["code"] == "child_parent_unsigned"
    assert _children_of(db, parent.tuple.task_id) == []
    assert store.asked == []
