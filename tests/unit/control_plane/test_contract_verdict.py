"""JC2's verdict through the state route (`POST .../state/contract-verdict`).

docs/design/agent-quality/jev-contract.md sections 5.4 and 9, lane
CONTRACT-VERDICT-ROUTE, with the owner's answers 5 and 11 of 3 November 2026.
Drives the real route over the in-memory Firestore with the worker key the
attempt registered (`test_attemptstate.Worker`). Each refusal of section 9 is
held to write NOTHING; a send-back lands in READY through the retry
transition; the send-back cap and the last attempt hand the step to the owner;
`not_run` and `cannot_judge` leave it waiting on the control plane's re-ask.
Offline: no credentials, no emulator.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_api import contractevidence as ce
from swarm_api import contractjudge as cj
from swarm_api import contractreask as cr
from swarm_api import workcontract as wc

from .test_attemptstate import (  # noqa: F401 - fixtures
    ATTEMPT,
    LEASE,
    SIGNER,
    TASK,
    Worker,
    client,
    opened,
    worker_tokens,
)

COMMIT = "c" * 40
BASE = "b" * 40
OP = "contract-verdict"


def contract_doc(tenant: str = "eng", **over: Any) -> dict[str, Any]:
    doc = {
        "schema": 1,
        "contract_id": "wc-login-flow",
        "tenant_id": tenant,
        "revision": 1,
        "previous_digest": None,
        "strictness": "advisory",
        "requirements": [
            {"id": "REQ-1", "text": "The error copy names the field.",
             "verification": ["judgment"]},
            {"id": "REQ-2", "text": "The login page accepts SSO.",
             "verification": ["judgment", "unit_tests"]},
            {"id": "REQ-3", "text": "The parser handles empty input.",
             "verification": ["unit_tests"]},
        ],
        "limits": {"runner_profile": "mock"},
        "ownership": {"owner": "alice@example.com"},
    }
    doc.update(over)
    return doc


def contract_metadata(doc: dict[str, Any] | None = None) -> tuple[wc.WorkContract, dict]:
    contract = wc.decode(doc or contract_doc())
    compiled = wc.compile_contract(contract, step_id="s1")
    return contract, {wc.METADATA_KEY: contract.to_document(),
                      "dispatch": {wc.DISPATCH_FIELD: dict(compiled.reference)}}


def published(db, worker: Worker, head: str = COMMIT, *, event: str = "evt_" + "p" * 20,
              attempt: str | None = None, generation: int | None = None) -> None:
    t = worker.tuple
    db.docs[f"tasks/{t.task_id}/events/{event}"] = {
        "event_id": event, "task_id": t.task_id, "tenant_id": t.tenant_id,
        "type": "running", "at": datetime.now(timezone.utc),
        "attempt_id": attempt or t.attempt_id, "lease_id": t.lease_id,
        "generation": generation or t.generation,
        "detail": {"cause": "published", "head": head, "branch": "swarm/x"},
    }


def setup(client, db, *, doc: dict[str, Any] | None = None, publish: bool = True,
          **extra: Any) -> tuple[Worker, wc.WorkContract, dict[str, Any]]:
    worker = Worker()
    contract, metadata = contract_metadata(doc)
    task = opened(client, db, worker, metadata=metadata, **extra)
    if publish:
        published(db, worker)
    return worker, contract, task


def verdict(rid: str, name: str = "met", *, p: float = 0.9, item: str | None = "i0",
            reason: str | None = "satisfied") -> dict[str, Any]:
    return {"requirement_id": rid, "verdict": name, "reason": reason, "evidence_item": item,
            "p": p}


def body(contract: wc.WorkContract, *verdicts: dict[str, Any], request_id: str = "cv-1",
         **over: Any) -> dict[str, Any]:
    out = {"request_id": request_id, "digest": wc.digest(contract), "commit": COMMIT,
           "base": BASE, "outcome": "judged", "call_id": "abc123def456",
           "state_sha256": "d" * 64, "changed": ["src/login/form.py", "tests/test_form.py"],
           "mechanical": [{"kind": "unit_tests", "passed": True,
                           "named": ["tests/test_form.py"]}],
           "verdicts": list(verdicts)}
    out.update(over)
    return out


def evidence(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith(f"{ce.COLLECTION}/")]


def lease(db) -> dict[str, Any]:
    return db.docs[f"leases/{LEASE}"]


# --- what a verdict does ---------------------------------------------------------


def test_met_everywhere_writes_passed_evidence_and_keeps_the_attempt(client, db):
    worker, contract, _ = setup(client, db)
    response = worker.post(client, OP, body(contract, verdict("REQ-1"), verdict("REQ-2")))
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == cj.JUDGED_MET
    task = db.docs[f"tasks/{TASK}"]
    assert task["state"] == "RUNNING" and lease(db)["released_at"] is None
    acc = task[cr.ACCEPTANCE_FIELD]
    assert acc["status"] == cj.JUDGED_MET and acc["waiting_on"] is None
    assert acc["digest"] == wc.digest(contract) and acc["commit"] == COMMIT
    rows = sorted(evidence(db), key=lambda d: d["requirement_id"])
    assert [(r["requirement_id"], r["passed"], r["kind"]) for r in rows] == [
        ("REQ-1", True, "judgment"), ("REQ-2", True, "judgment")]
    assert rows[0]["path"] == "src/login/form.py" and rows[0]["source"] == ce.SOURCE_WORKER
    assert rows[0]["label"] == ce.LABEL and rows[0]["call_id"] == "abc123def456"
    # The store is what `unmet` reads: REQ-2 still needs its unit tests,
    # REQ-3 is mechanical only; neither is passed by a judgment.
    stored = ce.read_evidence(db, "eng", TASK)
    assert wc.unmet(contract, stored, commit=COMMIT) == ("REQ-2", "REQ-3")


def test_an_unmet_sends_the_step_back_to_ready_through_the_retry_transition(client, db):
    worker, contract, _ = setup(client, db)
    response = worker.post(client, OP, body(
        contract, verdict("REQ-1", "unmet", reason="not_in_diff", item="i1"),
        verdict("REQ-2")))
    assert response.status_code == 200, response.text
    assert response.json()["sent_back"] is True
    task = db.docs[f"tasks/{TASK}"]
    assert task["state"] == "READY"
    assert task["current_lease_id"] is None and lease(db)["released_at"] is not None
    assert task[cr.SEND_BACKS_FIELD] == 1
    assert task[cr.ACCEPTANCE_FIELD]["status"] == cj.SENT_BACK
    [block] = task[cr.SEND_BACK_FIELD]
    assert block["requirement_id"] == "REQ-1" and block["path"] == "tests/test_form.py"
    assert block["note"] == f"Jev judged this requirement unmet at commit {COMMIT}"
    by_id = {r["requirement_id"]: r["passed"] for r in evidence(db)}
    assert by_id == {"REQ-1": False, "REQ-2": True}
    [retrying] = [d for p, d in db.docs.items()
                  if p.startswith(f"tasks/{TASK}/events/") and d.get("type") == "retrying"]
    assert retrying["detail"]["cause"] == "contract_unmet"
    assert retrying["detail"]["to_state"] == "READY"


def test_past_two_send_backs_the_step_goes_to_the_owner(client, db):
    worker, contract, _ = setup(client, db, **{cr.SEND_BACKS_FIELD: cj.MAX_SEND_BACKS})
    response = worker.post(client, OP, body(
        contract, verdict("REQ-1", "unmet", reason="partially_done"), verdict("REQ-2")))
    assert response.status_code == 200, response.text
    task = db.docs[f"tasks/{TASK}"]
    assert task["state"] == "RUNNING" and lease(db)["released_at"] is None
    acc = task[cr.ACCEPTANCE_FIELD]
    assert acc["status"] == cj.JUDGMENT_PENDING and acc["waiting_on"] == cr.WAITING_OWNER
    assert acc["owner_reason"] == cr.OWNER_SEND_BACK_CAP and acc["reask"] is None
    assert task[cr.SEND_BACKS_FIELD] == cj.MAX_SEND_BACKS
    assert {r["requirement_id"]: r["passed"] for r in evidence(db)} == {
        "REQ-1": False, "REQ-2": True}


def test_a_send_back_that_would_spend_the_last_attempt_goes_to_the_owner(client, db):
    worker, contract, _ = setup(client, db, attempt_count=3, max_attempts=3)
    response = worker.post(client, OP, body(
        contract, verdict("REQ-1", "unmet", reason="partially_done"), verdict("REQ-2")))
    assert response.status_code == 200, response.text
    task = db.docs[f"tasks/{TASK}"]
    assert task["state"] == "RUNNING"
    assert task[cr.ACCEPTANCE_FIELD]["owner_reason"] == cr.OWNER_ATTEMPTS_EXHAUSTED


@pytest.mark.parametrize("posted", [
    {"outcome": "not_run", "reason": "budget_monthly", "verdicts": []},
    {"verdicts": [verdict("REQ-1", "cannot_judge", item=None, reason="evidence_missing"),
                  verdict("REQ-2")]},
    # The thresholds are swarm-api's: a `met` at p 0.5 is no verdict.
    {"verdicts": [verdict("REQ-1", p=0.5), verdict("REQ-2")]},
    # A judged requirement left out is not judged, never passed.
    {"verdicts": [verdict("REQ-2")]},
], ids=["not_run", "cannot_judge", "under_threshold", "missing"])
def test_no_verdict_waits_on_the_control_planes_reask(client, db, posted):
    worker, contract, _ = setup(client, db)
    response = worker.post(client, OP, body(contract, **posted))
    assert response.status_code == 200, response.text
    task = db.docs[f"tasks/{TASK}"]
    acc = task[cr.ACCEPTANCE_FIELD]
    assert task["state"] == "RUNNING"
    assert acc["status"] == cj.JUDGMENT_PENDING and acc["waiting_on"] == cr.WAITING_REASK
    assert acc["reask"]["tries"] == 0
    assert acc["reask"]["next_at"] - acc["at"] == cr.REASK_OFFSETS[0]
    assert acc["reask"]["deadline"] - acc["at"] == cr.REASK_WINDOW
    # Nothing is passed for a requirement Jev did not judge met.
    assert not any(r["passed"] and r["requirement_id"] == "REQ-1" for r in evidence(db))


def test_a_repeat_of_the_same_request_writes_nothing_more(client, db):
    worker, contract, _ = setup(client, db)
    payload = body(contract, verdict("REQ-1"), verdict("REQ-2"))
    assert worker.post(client, OP, payload).status_code == 200
    before = copy.deepcopy(db.docs)
    again = worker.post(client, OP, payload)
    assert again.status_code == 200 and again.json()["repeat"] is True
    assert db.docs == before


# --- section 9: every refusal writes nothing --------------------------------------


def _stale_generation(db, worker, contract):
    db.docs[f"tasks/{TASK}"]["current_generation"] = 2
    return body(contract, verdict("REQ-1"), verdict("REQ-2"))


def _superseded_attempt(db, worker, contract):
    db.docs[f"leases/{LEASE}"]["attempt_id"] = "att_newer"
    return body(contract, verdict("REQ-1"), verdict("REQ-2"))


def _another_digest(db, worker, contract):
    return body(contract, verdict("REQ-1"), verdict("REQ-2"), digest="e" * 64)


def _amended_body(db, worker, contract):
    # The stored body changed under the signed reference: the reference no
    # longer names it, so the verdict has no revision to bind.
    stored = db.docs[f"tasks/{TASK}"]["metadata"][wc.METADATA_KEY]
    stored["requirements"][0]["text"] = "Something else."
    return body(contract, verdict("REQ-1"), verdict("REQ-2"))


def _unsigned_spec(db, worker, contract):
    db.docs[f"tasks/{TASK}"]["spec_signature"] = None
    return body(contract, verdict("REQ-1"), verdict("REQ-2"))


def _another_commit(db, worker, contract):
    return body(contract, verdict("REQ-1"), verdict("REQ-2"), commit="f" * 40)


def _nothing_published(db, worker, contract):
    for path in [p for p in db.docs if p.startswith(f"tasks/{TASK}/events/")]:
        if db.docs[path].get("detail", {}).get("cause") == "published":
            del db.docs[path]
    return body(contract, verdict("REQ-1"), verdict("REQ-2"))


def _another_attempts_publish(db, worker, contract):
    _nothing_published(db, worker, contract)
    published(db, worker, attempt="att_other", event="evt_" + "q" * 20)
    return body(contract, verdict("REQ-1"), verdict("REQ-2"))


def _mechanical_requirement(db, worker, contract):
    return body(contract, verdict("REQ-1"), verdict("REQ-2"), verdict("REQ-3"))


def _unknown_requirement(db, worker, contract):
    return body(contract, verdict("REQ-1"), verdict("REQ-9"))


def _uncited_item(db, worker, contract):
    return body(contract, verdict("REQ-1", item="i7"), verdict("REQ-2"))


def _uncited_mechanical(db, worker, contract):
    return body(contract, verdict("REQ-1", item="m3"), verdict("REQ-2"))


def _open_verdict(db, worker, contract):
    return body(contract, verdict("REQ-1", "probably"), verdict("REQ-2"))


def _open_reason(db, worker, contract):
    return body(contract, verdict("REQ-1", reason="looks good"), verdict("REQ-2"))


def _twice(db, worker, contract):
    return body(contract, verdict("REQ-1"), verdict("REQ-1", "unmet"), verdict("REQ-2"))


def _already_recorded(db, worker, contract):
    db.docs[f"tasks/{TASK}"][cr.ACCEPTANCE_FIELD] = {
        "attempt_id": ATTEMPT, "generation": 1, "status": cj.JUDGMENT_PENDING}
    return body(contract, verdict("REQ-1"), verdict("REQ-2"), request_id="cv-2")


def _not_run_with_verdicts(db, worker, contract):
    return body(contract, verdict("REQ-1"), outcome="not_run", reason="transport")


REFUSALS = {
    "stale_generation": (_stale_generation, 409, "attempt_fenced"),
    "superseded_attempt": (_superseded_attempt, 409, "attempt_fenced"),
    "another_digest": (_another_digest, 409, "state_conflict"),
    "amended_body": (_amended_body, 409, "state_conflict"),
    "unsigned_spec": (_unsigned_spec, 409, "state_conflict"),
    "another_commit": (_another_commit, 409, "state_conflict"),
    "nothing_published": (_nothing_published, 409, "state_conflict"),
    "another_attempts_publish": (_another_attempts_publish, 409, "state_conflict"),
    "already_recorded": (_already_recorded, 409, "state_conflict"),
    "mechanical_requirement": (_mechanical_requirement, 422, "validation_failed"),
    "unknown_requirement": (_unknown_requirement, 422, "validation_failed"),
    "uncited_item": (_uncited_item, 422, "validation_failed"),
    "uncited_mechanical": (_uncited_mechanical, 422, "validation_failed"),
    "open_verdict": (_open_verdict, 422, "validation_failed"),
    "open_reason": (_open_reason, 422, "validation_failed"),
    "judged_twice": (_twice, 422, "validation_failed"),
    "not_run_with_verdicts": (_not_run_with_verdicts, 422, "validation_failed"),
}


@pytest.mark.parametrize("name", sorted(REFUSALS))
def test_every_check_of_section_9_refuses_with_nothing_written(client, db, name):
    make, status, code = REFUSALS[name]
    worker, contract, _ = setup(client, db)
    payload = make(db, worker, contract)
    before = copy.deepcopy(db.docs)
    response = worker.post(client, OP, payload)
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert db.docs == before


def test_an_unproven_verdict_is_refused_with_nothing_written(client, db):
    worker, contract, _ = setup(client, db)
    before = copy.deepcopy(db.docs)
    response = worker.post(client, OP, body(contract, verdict("REQ-1"), verdict("REQ-2")),
                           sign=False)
    assert response.status_code == 403
    assert db.docs == before


def test_a_caller_supplied_field_is_refused(client, db):
    """Invariant 10: an unknown key -- a backend, an image -- is a 422."""
    worker, contract, _ = setup(client, db)
    before = copy.deepcopy(db.docs)
    response = worker.post(client, OP, body(contract, verdict("REQ-1"), verdict("REQ-2"),
                                            backend="typesafe"))
    assert response.status_code == 422
    assert db.docs == before
