"""The input re-check runs after the spec verifies, on the verified snapshot.

Two checks run on the same task document, close together in `_prepare`:
`_verify_spec` (contract request 34, #342) refuses a task whose signature does
not match what swarm-api signed at submission, and `_recheck_runner_input`
(contract request 32, *Preconditions*; #345) refuses a task whose stored
input its profile does not declare. The owner's decision on their ordering,
made resolving #345 into #353: the input re-check runs AFTER the signature
verifies, and on the SAME task object `_verify_spec` checked -- never on a
fresh, unverified re-fetch, and never before verification.

A task whose signature fails verification must be refused by `_verify_spec`
without `_recheck_runner_input` ever being called: an attacker who cannot
forge a signature must not get a second worker code path to reach, let alone
one that would report something back about their tampered document.
"""

from __future__ import annotations

from typing import Any

import pytest

import spec_keys
from agent_worker import lifecycle
from agent_worker.errors import ExitCode

from conftest import seed_attempt

TASK = "task_1"


@pytest.fixture
def order(monkeypatch) -> list[str]:
    """Records, in call order, which of the two checks actually ran."""
    seen: list[str] = []

    original_verify = lifecycle.Worker._verify_spec

    def verify_spec(self, task, create_time):
        try:
            return original_verify(self, task, create_time)
        finally:
            # Recorded even on a refusal (the original raises): the whole
            # point is to see whether `recheck_runner_input` follows it.
            seen.append("verify_spec")

    monkeypatch.setattr(lifecycle.Worker, "_verify_spec", verify_spec)

    original_recheck = lifecycle._recheck_runner_input

    def recheck(runner_profile, stored):
        seen.append("recheck_runner_input")
        return original_recheck(runner_profile, stored)

    monkeypatch.setattr(lifecycle, "_recheck_runner_input", recheck)
    return seen


def test_a_verified_task_reaches_the_recheck_after_verify_spec(db, worker_factory, order):
    """The control: a signed, declared task runs both checks, verify_spec first."""
    seed_attempt(
        db, task_id=TASK, task_input={"prompt": "hello", "steps": 1, "sleep_seconds": 0.01}
    )
    worker, _, _ = worker_factory(task_id=TASK)

    rc = worker.run()

    assert rc == ExitCode.OK, rc
    assert order == ["verify_spec", "recheck_runner_input"], order


def test_a_signature_that_fails_verification_never_reaches_the_recheck(db, worker_factory, order):
    """The signature check refuses the task before the input re-check ever runs."""
    seed_attempt(
        db, task_id=TASK, task_input={"prompt": "hello", "steps": 1, "sleep_seconds": 0.01}
    )
    doc = db.doc(f"tasks/{TASK}")
    # Sign it the way swarm-api would at submission, then tamper a covered
    # field -- the same shape test_spec_signature_worker.py's
    # COVERED_REWRITES uses to break the signature.
    spec_keys.sign_document(doc, TASK)
    doc["input"]["prompt"] = "a rewrite made after the spec was signed"
    worker, _, _ = worker_factory(task_id=TASK)

    rc = worker.run()

    assert rc == ExitCode.FAILED, rc
    task = db.doc(f"tasks/{TASK}")
    assert task["end_cause"] == "spec_signature_invalid", task.get("end_cause")
    assert order == ["verify_spec"], order


def test_the_recheck_sees_the_verified_snapshot_not_a_fresh_unverified_read(
    db, worker_factory, monkeypatch
):
    """`_recheck_runner_input` is handed the same input object `_verify_spec` checked.

    A worker that re-fetched the document for this call, instead of reusing
    the one snapshot `fetch_task_snapshot` took at STEP 4, would reopen the
    TOCTOU window contract request 34 closed: the document could change
    between the verified read and a second one. This mutates the STORED
    document from inside `_recheck_runner_input` itself, simulating a write
    landing right after verification, and asserts the input the worker
    actually re-checked was the one taken before that mutation.
    """
    seed_attempt(
        db, task_id=TASK, task_input={"prompt": "hello", "steps": 1, "sleep_seconds": 0.01}
    )
    doc = db.doc(f"tasks/{TASK}")
    spec_keys.sign_document(doc, TASK)

    seen_inputs: list[Any] = []
    original_recheck = lifecycle._recheck_runner_input

    def recheck(runner_profile, stored):
        seen_inputs.append(stored)
        # A write landing between verification and a hypothetical second
        # read. If the worker re-fetched for this call it would observe it;
        # it must not, because it never re-fetches.
        doc["input"] = {"prompt": "mutated after verification", "steps": 999}
        return original_recheck(runner_profile, stored)

    monkeypatch.setattr(lifecycle, "_recheck_runner_input", recheck)
    worker, _, _ = worker_factory(task_id=TASK)
    rc = worker.run()

    assert rc == ExitCode.OK, rc
    assert seen_inputs == [{"prompt": "hello", "steps": 1, "sleep_seconds": 0.01}], seen_inputs
    # And the mutation did land -- proving this is a real race, not a no-op.
    assert db.doc(f"tasks/{TASK}")["input"] == {
        "prompt": "mutated after verification",
        "steps": 999,
    }
