"""The worker's read of its attempt's QA target, `POST /v1/attempts/{attempt_id}/qa-target`
(docs/design/visual-qa-any-app.md §2.2; lane VQ-2 TARGETS, part of #1933).

What these tests hold, over the real app, the in-memory Firestore and a real
attempt key (test_live_api's harness):

  * an agent with no `metadata.qa_target` answers 204, the common case;
  * THE TENANT IS THE LEASE'S: a target of another tenant answers exactly as
    a missing one, and so does a mark this platform did not write (no run
    record names the task for that target);
  * a pending host is 409 `qa_target_host_pending`; a target changed since
    the run started is 409;
  * the route is proven and fenced as the QA identity's: no proof, or one
    under another route's purpose, is 401; a stale generation is 409 and
    reads no credential (invariant 5);
  * an own-login target's answer carries the username and password read from
    the tenant's secret at that moment, `Cache-Control: no-store`; the
    `live_audit` row and every log line carry the target id and never the
    value; an unreachable secret is 503 `qa_credential_unavailable`;
  * the route is in `main.UNCAPTURED_ROUTES`;
  * `mutating` is true only for a flow step of the rendered run (the task's
    own server-written `step_id`, `flow-<id>`) whose flow the stored target
    declares `mutating: true`, on a `non_production` target (§3; lane
    VQ4-MUTATING, BGD 2026-10-22). Every other step answers false, and a
    caller's `mutating` in the request is refused, never read.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_api.handoff import LIVE_AUDIT
from swarm_api.main import UNCAPTURED_ROUTES
from swarm_api.qa_identity import qa_identity_message
from swarm_api.qatargets import qa_target_message

from .conftest import seed_tenant
from .test_live_api import (  # noqa: F401 - fixtures are used by name
    ATTEMPT,
    TASK,
    WORKER_TOKEN,
    Attempt,
    _b64url,
    clock,
    groups,
    ldb,
    make_client,
    people,
    relay,
    seed_running,
)
from .test_qa_target_credential import FakeQaCredentials

PATH = f"/v1/attempts/{ATTEMPT}/qa-target"
TARGET = "qat_" + "a1" * 8
WORKFLOW = "wf_qa_run_1"
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(ldb, people, groups, objects, clock, relay):
    return make_client(ldb, people, groups, objects, clock, relay)


@pytest.fixture
def store(client) -> FakeQaCredentials:
    fake = FakeQaCredentials()
    client.app.state.qa_credential_store = fake
    return fake


class CountingStore(FakeQaCredentials):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def read(self, tenant_id: str) -> dict[str, Any]:
        self.reads += 1
        return super().read(tenant_id)


def seed_target(db: Any, *, tenant: str = "eng", target_id: str = TARGET, own_login: bool = True,
                host_state: str = "approved", revision: int = 1,
                environment: str | None = None,
                flows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    sign_in: dict[str, Any] = {"kind": "none"}
    if own_login:
        sign_in = {"kind": "own_login", "login_path": "/login", "signed_in_selector": "nav .me",
                   "password_selector": "#pw"}
    doc = {
        "id": target_id, "tenant_id": tenant, "name": "Shop", "revision": revision,
        "base_url": "https://shop.example.com", "origin": "https://shop.example.com",
        "host": "shop.example.com", "repository": "https://github.com/example-org/shop",
        "repo_id": "repo_0000000000000000", "sign_in": sign_in,
        "screens": [{"id": "home", "path": "/"}],
        "host_state": host_state,
        "approved_origins": ["https://shop.example.com"] if host_state == "approved" else [],
        "credential": {"set": own_login, "set_at": NOW if own_login else None,
                       "set_by": "root@example.com" if own_login else None,
                       "version": "projects/p/secrets/swarm-tenant-eng-qa/versions/1" if own_login else None},
    }
    if environment is not None:
        doc["environment"] = environment
    if flows is not None:
        doc["flows"] = flows
    db.docs[f"qa_targets/{target_id}"] = doc
    return doc


def stamp(db: Any, task: dict[str, Any], *, target_id: str = TARGET, revision: int = 1,
          record: bool = True, run_tenant: str = "eng", task_ids: list[str] | None = None,
          step_id: str | None = None) -> None:
    """What `:run` writes: the mark on the step task, and the run record.
    `step_id` is the rendered step's id, written by workflow expansion."""
    task["workflow_id"] = WORKFLOW
    if step_id is not None:
        task["step_id"] = step_id
    task["metadata"] = {**(task.get("metadata") or {}), "qa_target": {"id": target_id, "revision": revision}}
    if record:
        db.docs[f"qa_target_runs/{WORKFLOW}"] = {
            "workflow_id": WORKFLOW, "tenant_id": run_tenant, "target_id": target_id,
            "revision": revision, "task_ids": task_ids if task_ids is not None else [TASK],
            "file": "issues", "by": "alice@example.com", "at": NOW,
        }


def ask(client, attempt: Attempt, *, signed: bool = True, message=qa_target_message, **over: Any):
    body = attempt.body(**over)
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"}
    if signed:
        stamp_ = str(int(time.time()))
        headers["X-Swarm-Attempt-Proof"] = _b64url(attempt.private.sign(message("POST", PATH, body, stamp_)))
        headers["X-Swarm-Attempt-Timestamp"] = stamp_
    return client.post(PATH, content=body, headers=headers)


def issued_rows(db: Any) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith(f"{LIVE_AUDIT}/") and d.get("entry") == "qa_target_issued"]


def secret() -> tuple[str, str]:
    return "qa-" + secrets.token_hex(4), secrets.token_hex(16)


@pytest.fixture
def running(client, ldb):
    attempt = Attempt()
    doc = seed_running(client, ldb, attempt)
    return attempt, doc


def test_an_agent_with_no_target_is_204(client, ldb, store, running):
    attempt, _ = running
    response = ask(client, attempt)
    # MUTATION: answer {} with 200 for no mark in `issue_qa_target` and this fails.
    assert response.status_code == 204, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert issued_rows(ldb) == []


def test_an_own_login_target_answers_its_sign_in_and_credential(client, ldb, store, running, caplog):
    caplog.set_level(logging.DEBUG)
    attempt, doc = running
    seed_target(ldb)
    stamp(ldb, doc)
    username, password = secret()
    store.values["eng"] = {TARGET: {"username": username, "password": password}}
    response = ask(client, attempt)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["target_id"] == TARGET and body["revision"] == 1
    assert body["origins"] == ["https://shop.example.com"]
    sign_in = body["sign_in"]
    assert sign_in["login_url"] == "https://shop.example.com/login"
    assert sign_in["signed_in_selector"] == "nav .me" and sign_in["password_selector"] == "#pw"
    # Read from the tenant's secret at this moment (§2.2 step 1).
    assert (sign_in["username"], sign_in["password"]) == (username, password)
    assert "/logout" in body["exclusions"]["paths"]
    rows = issued_rows(ldb)
    # MUTATION: drop the live_audit write from `QaTargetIssuer.issue` and this is [].
    assert len(rows) == 1
    assert rows[0]["detail"]["target_id"] == TARGET and rows[0]["generation"] == 1
    for value in (username, password):
        # MUTATION: put the answer in the audit row's detail and this fails.
        assert value not in json.dumps(ldb.docs, default=str)
        # MUTATION: log the answer in `issue` and this fails.
        assert value not in caplog.text


def test_a_public_target_reads_no_secret(client, ldb, running):
    counting = CountingStore()
    client.app.state.qa_credential_store = counting
    attempt, doc = running
    seed_target(ldb, own_login=False)
    stamp(ldb, doc)
    response = ask(client, attempt)
    assert response.status_code == 200, response.text
    assert response.json()["sign_in"] == {"kind": "none"}
    assert counting.reads == 0


def test_another_tenants_target_answers_as_a_missing_one(client, ldb, store, running):
    attempt, doc = running
    seed_tenant(ldb, "research")
    seed_target(ldb, tenant="research")
    stamp(ldb, doc, run_tenant="research")
    theirs = ask(client, attempt)
    seed_target(ldb, target_id="qat_" + "b2" * 8, tenant="eng")
    del ldb.docs[f"qa_targets/{TARGET}"]
    missing = ask(client, attempt)
    # MUTATION: read the target by its id alone in `issue` and the first is 200.
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json() == missing.json()
    assert issued_rows(ldb) == []


def test_the_tenant_is_the_leases_not_the_bodys(client, ldb, store, running):
    attempt, doc = running
    seed_tenant(ldb, "research")
    seed_target(ldb, tenant="research")
    stamp(ldb, doc, run_tenant="research")
    response = ask(client, attempt, tenant_id="research")
    # The eng worker naming research is refused before anything is read.
    assert response.status_code == 401, response.text
    assert issued_rows(ldb) == []


@pytest.mark.parametrize("record,task_ids", [(False, None), (True, ["task_someone_else"])],
                         ids=["no-run-record", "run-names-another-task"])
def test_a_mark_this_platform_did_not_write_answers_as_missing(client, ldb, store, running,
                                                               record, task_ids):
    attempt, doc = running
    seed_target(ldb, own_login=False)
    stamp(ldb, doc, record=record, task_ids=task_ids)
    response = ask(client, attempt)
    # MUTATION: skip `_run_names` in `issue` and a caller's own mark is served.
    assert response.status_code == 404, response.text


def test_a_pending_host_is_refused(client, ldb, store, running):
    attempt, doc = running
    seed_target(ldb, host_state="pending")
    stamp(ldb, doc)
    response = ask(client, attempt)
    # MUTATION: drop the host_state check in `issue` and this is 200.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "qa_target_host_pending"


def test_a_target_changed_since_the_run_started_is_refused(client, ldb, store, running):
    attempt, doc = running
    seed_target(ldb, own_login=False, revision=2)
    stamp(ldb, doc, revision=1)
    response = ask(client, attempt)
    # MUTATION: drop the revision check in `issue` and this is 200.
    assert response.status_code == 409, response.text


def test_a_stale_generation_is_409_and_reads_no_credential(client, ldb, running):
    counting = CountingStore()
    client.app.state.qa_credential_store = counting
    attempt, doc = running
    seed_target(ldb)
    stamp(ldb, doc)
    doc["current_generation"] = 2
    response = ask(client, attempt)
    # MUTATION: skip the generation check in `_fence` and this reads the secret.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "stale_generation"
    assert counting.reads == 0


def test_without_the_attempt_proof_it_is_bad_signature(client, ldb, store, running):
    attempt, doc = running
    seed_target(ldb)
    stamp(ldb, doc)
    response = ask(client, attempt, signed=False)
    # MUTATION: return early from `_prove` and this is 200.
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "bad_signature"


def test_a_qa_identity_signature_does_not_sign_here(client, ldb, store, running):
    attempt, doc = running
    seed_target(ldb)
    stamp(ldb, doc)
    # MUTATION: sign this route under QA_IDENTITY_PURPOSE and this is 200.
    response = ask(client, attempt, message=qa_identity_message)
    assert response.status_code == 401, response.text


def test_an_unreachable_secret_is_503(client, ldb, store, running):
    attempt, doc = running
    seed_target(ldb)
    stamp(ldb, doc)
    store.fail = True
    response = ask(client, attempt)
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "qa_credential_unavailable"
    assert issued_rows(ldb) == []


def test_a_credential_missing_from_the_secret_is_503(client, ldb, store, running):
    attempt, doc = running
    seed_target(ldb)
    stamp(ldb, doc)
    # The target says set; the map has no entry (a version lost to a race).
    response = ask(client, attempt)
    # MUTATION: answer the sign-in without a password in `_credential` and this is 200.
    assert response.status_code == 503, response.text


def test_the_route_is_never_captured():
    # MUTATION: drop ISSUE_ROUTE from main.UNCAPTURED_ROUTES and this fails.
    assert PATH.replace(ATTEMPT, "{attempt_id}") in UNCAPTURED_ROUTES


# --------------------------------------------------------------------------
# `mutating` (§3; lane VQ4-MUTATING, part of #1933)
# --------------------------------------------------------------------------

FLOWS = [
    {"id": "checkout", "goal": "Buy one item", "start_path": "/", "mutating": True},
    {"id": "browse", "goal": "Find an item", "start_path": "/", "mutating": False},
]


def mutating_answer(client, ldb, running, *, environment: str = "non_production",
                    flows: list[dict[str, Any]] | None = None, step_id: str | None = "flow-checkout",
                    **over: Any):
    attempt, doc = running
    seed_target(ldb, own_login=False, environment=environment,
                flows=FLOWS if flows is None else flows)
    stamp(ldb, doc, step_id=step_id)
    return ask(client, attempt, **over)


def test_a_mutating_flow_step_on_a_non_production_target_is_mutating(client, ldb, running):
    response = mutating_answer(client, ldb, running)
    assert response.status_code == 200, response.text
    # MUTATION: leave `mutating` out of `QaTargetIssuer._answer` and this fails.
    assert response.json()["mutating"] is True


def test_the_same_flow_on_a_production_target_is_not_mutating(client, ldb, running):
    response = mutating_answer(client, ldb, running, environment="production")
    assert response.status_code == 200, response.text
    # MUTATION: drop the `non_production` check in `QaTargetIssuer._mutating` and this is true.
    assert response.json()["mutating"] is False


def test_a_target_with_no_environment_is_production_and_not_mutating(client, ldb, running):
    attempt, doc = running
    seed_target(ldb, own_login=False, flows=FLOWS)
    stamp(ldb, doc, step_id="flow-checkout")
    response = ask(client, attempt)
    assert response.status_code == 200, response.text
    # MUTATION: default a missing environment to non_production in `_mutating` and this is true.
    assert response.json()["mutating"] is False


def test_a_non_mutating_flow_is_not_mutating(client, ldb, running):
    response = mutating_answer(client, ldb, running, step_id="flow-browse")
    assert response.status_code == 200, response.text
    # MUTATION: answer true for any declared flow in `_mutating` and this is true.
    assert response.json()["mutating"] is False


@pytest.mark.parametrize("flags", [None, "true", 1])
def test_only_a_boolean_true_declaration_is_mutating(client, ldb, running, flags):
    flows = [{"id": "checkout", "goal": "Buy one item", "start_path": "/", "mutating": flags}]
    response = mutating_answer(client, ldb, running, flows=flows)
    assert response.status_code == 200, response.text
    # MUTATION: test the declaration's truthiness, not `is True`, and "true" and 1 are mutating.
    assert response.json()["mutating"] is False


@pytest.mark.parametrize("step_id", ["capture-home", "discover"])
def test_a_capture_step_is_not_mutating(client, ldb, running, step_id):
    response = mutating_answer(client, ldb, running, step_id=step_id)
    assert response.status_code == 200, response.text
    # MUTATION: answer true whenever the target declares a mutating flow and this is true.
    assert response.json()["mutating"] is False


@pytest.mark.parametrize("step_id", ["flow-search", "flow-", "flow-checkout-1", "checkout", None],
                         ids=["another-flow", "empty-flow", "suffixed", "no-prefix", "no-step-id"])
def test_a_step_naming_another_flow_or_none_is_not_mutating(client, ldb, running, step_id):
    response = mutating_answer(client, ldb, running, step_id=step_id)
    assert response.status_code == 200, response.text
    # MUTATION: match the flow by prefix (`startswith`) in `_mutating` and "suffixed" is true;
    # read any flow's declaration in place of the named one and every case is true.
    assert response.json()["mutating"] is False


def test_a_caller_supplied_mutating_is_refused_and_never_read(client, ldb, running):
    response = mutating_answer(client, ldb, running, step_id="flow-browse", mutating=True)
    # MUTATION: let `QaTargetRequest` accept extra fields and read `body.mutating`, and this is 200 true.
    assert response.status_code == 422, response.text
    assert issued_rows(ldb) == []
    plain = ask(client, running[0])
    assert plain.status_code == 200, plain.text
    # MUTATION: carry anything from the request into `_mutating` and this differs.
    assert plain.json()["mutating"] is False
