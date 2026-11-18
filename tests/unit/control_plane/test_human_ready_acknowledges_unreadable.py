"""A member's human-ready on a merge's unreadable owner entries is recorded (10 November 2026).

The live finding: a merge parked on two `needs_owner` entries it could not
read, and every human-ready re-ran it into the same park, because nothing
recorded the ready. `handoff.ready_writes` now records, in the transaction
that returns the park to READY, who said ready and the review, verdict digest
and head the park named (`OWNER_READY_METADATA_KEY`), so the next merge
attempt goes on past those entries (`agent_worker.merge._unreadable_hold`).

The ready still writes READY and nothing else beyond that record: no lease,
no pool count (invariants 1 and 2). No credentials, no network, no emulator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from swarm_api.handoff import HUMAN_READY_METADATA_KEY, OWNER_READY_METADATA_KEY

from .conftest import auth_header, seed_task, seed_tenant

TASK = "task_merge"
DIGEST = "a" * 64
HEAD = "c" * 40


def _basis(**change: Any) -> dict[str, Any]:
    return {"source": "review", "review_task": "task_rev", "verdict_digest": DIGEST,
            "head": HEAD, "round_heads": [], **change}


def _parked(db, *, unreadable: int = 2, basis: dict[str, Any] | None = None,
            questions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id=TASK, tenant_id="eng", state="PARKED",
                    park_reason="HUMAN_REQUIRED",
                    next_eligible_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    wait: dict[str, Any] = {"code": "owner_questions_open", "questions": questions or [],
                            "unreadable": unreadable}
    if basis is not None:
        wait["acknowledges"] = basis
    doc.update({"submitted_by": "alice@example.com", "attempt_count": 1, "blocked_by": [
        {"reason": "HUMAN_REQUIRED", "park_phase": "owner_questions", "owner_questions": wait}]})
    return doc


def test_a_ready_on_unreadable_entries_records_what_it_acknowledged(client, db):
    doc = _parked(db, basis=_basis())

    response = client.post(f"/v1/tasks/{TASK}/human-ready", headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    assert doc["state"] == "READY" and doc["blocked_by"] == []
    # MUTATION: record nothing (the live finding) and the next attempt parks again.
    record = doc["metadata"][OWNER_READY_METADATA_KEY]["review"]
    assert record["by"] == "alice@example.com"
    assert record["at"] == doc["metadata"][HUMAN_READY_METADATA_KEY]["at"]
    assert (record["review_task"], record["verdict_digest"], record["head"],
            record["round_heads"], record["unreadable"]) == ("task_rev", DIGEST, HEAD, [], 2)
    # Invariant 1: READY holds nothing; no lease was made here.
    assert not any(path.startswith("leases/") for path in db.docs)


def test_a_ready_keeps_the_other_verdicts_acknowledgement(client, db):
    doc = _parked(db, basis=_basis(source="security_review", review_task=None))
    doc["metadata"] = {OWNER_READY_METADATA_KEY: {"review": {"verdict_digest": DIGEST}}}

    assert client.post(f"/v1/tasks/{TASK}/human-ready",
                       headers=auth_header("alice")).status_code == 200

    # MUTATION: overwrite the whole record and a merge held by both verdicts
    # alternates between them for ever.
    recorded = doc["metadata"][OWNER_READY_METADATA_KEY]
    assert set(recorded) == {"review", "security_review"}
    assert recorded["security_review"]["review_task"] is None


def test_a_park_with_no_unreadable_entry_or_a_malformed_basis_records_nothing(client, db):
    doc = _parked(db, unreadable=0, basis=_basis())
    assert client.post(f"/v1/tasks/{TASK}/human-ready",
                       headers=auth_header("alice")).status_code == 200
    assert OWNER_READY_METADATA_KEY not in doc["metadata"]

    db.docs.clear()
    doc = _parked(db, basis=_basis(verdict_digest="not a digest"))
    assert client.post(f"/v1/tasks/{TASK}/human-ready",
                       headers=auth_header("alice")).status_code == 200
    # MUTATION: copy the basis unchecked and a malformed park records an acknowledgement.
    assert OWNER_READY_METADATA_KEY not in (doc.get("metadata") or {})
    assert doc["state"] == "READY"
