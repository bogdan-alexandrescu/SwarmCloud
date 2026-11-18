"""Contract request 137 at the worker: the stored work contract is inside the step-spec signature.

A tenant worker can write its task document, never the signature. Since spec
format 8 the stored `metadata.work_contract` is in the signed bytes, so a
rewrite of it is refused by the spec check before anything runs, and the
plugins selection's digest check (`agent_worker.plugins.selected`) is the
second guard behind it. Offline: the fake store and local keys.
"""

from __future__ import annotations

import copy
from typing import Any

import spec_keys
from agent_worker.errors import ExitCode
from swarm_common.states import TaskState
from swarm_common.workcontract import contract_digest
from test_spec_signature_worker import (  # noqa: F401  (the fixture)
    TASK,
    _assert_refused,
    _rich_task,
    witness,
)
from worker_seeds import seed_attempt


def _contract(*skills: str) -> dict[str, Any]:
    return {
        "schema": 1, "contract_id": "wc-review", "tenant_id": "eng", "revision": 1,
        "strictness": "advisory",
        "requirements": [{"id": "REQ-1", "text": "Review it.", "verification": []}],
        "capabilities": {"runtime": [], "plugins": (
            {"gstack": {"mode": "unattended", "skills": list(skills)}} if skills else {})},
    }


def _with_contract(doc: dict[str, Any], contract: dict[str, Any]) -> None:
    doc["metadata"]["work_contract"] = copy.deepcopy(contract)
    doc["metadata"]["dispatch"]["work_contract"] = {
        "id": "wc-review", "revision": 1, "digest": contract_digest(contract),
        "strictness": "advisory", "requirements": ["REQ-1"]}


def test_a_rewritten_contract_is_refused_before_anything_runs(db, worker_factory, witness):
    doc = _rich_task(db)
    _with_contract(doc, _contract("review"))
    spec_keys.sign_document(doc, TASK, spec_format=8)
    # The rewrite widens the step's skills; before format 8 only the digest
    # check, after the spec check had passed, stood between it and the agent.
    doc["metadata"]["work_contract"]["capabilities"]["plugins"]["gstack"]["skills"] = [
        "review", "ship"]
    worker, _, _ = worker_factory(task_id=TASK, runner_profile="claude-code")
    _assert_refused(db, witness, "signature_mismatch", rc=worker.run())


def test_a_contract_added_to_a_task_signed_before_format_8_is_refused(
    db, worker_factory, witness
):
    doc = _rich_task(db)
    spec_keys.sign_document(doc, TASK, spec_format=7)
    _with_contract(doc, _contract("review"))
    worker, _, _ = worker_factory(task_id=TASK, runner_profile="claude-code")
    _assert_refused(db, witness, "not_canonical", rc=worker.run())


def test_a_task_signed_at_format_8_with_its_contract_runs(db, worker_factory):
    seed_attempt(db, task_id=TASK)
    doc = db.doc(f"tasks/{TASK}")
    doc.setdefault("metadata", {}).setdefault("dispatch", {})
    _with_contract(doc, _contract())
    spec_keys.sign_document(doc, TASK)
    assert doc["spec_format"] == 8
    worker, _, _ = worker_factory(task_id=TASK)
    rc = worker.run()
    task = db.doc(f"tasks/{TASK}")
    assert rc == ExitCode.OK, (rc, task.get("last_error"), task.get("result_summary"))
    assert task["state"] == TaskState.SUCCEEDED.value
