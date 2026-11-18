"""Contract request 137 at submission: a task carrying a work contract is signed at spec format 8.

swarm-api stores the contract's canonical document at `metadata.work_contract`
and signs the task at the oldest format that covers it, which is 8; a task
with no contract keeps the format it had. Offline: FakeFirestore and a local
spec signer.
"""

from __future__ import annotations

from swarm_common import specsign

from .test_work_contract_submission import (  # noqa: F401  (the fixtures)
    _contract,
    _task,
    _tasks,
    client,
    switch_on,
)


def test_a_task_with_a_contract_is_signed_at_format_8(client, db, switch_on):
    response = _task(client, _contract())
    assert response.status_code == 201, response.text
    (task,) = _tasks(db)
    assert task["metadata"]["work_contract"]
    assert task["spec_format"] == 8 == specsign.signing_format(task)
    specsign.canonical_step_spec(task, task_id=task["id"], spec_format=8)


def test_a_task_without_a_contract_keeps_its_format(client, db, switch_on):
    assert _task(client, None).status_code == 201
    (task,) = _tasks(db)
    assert task["spec_format"] == 1
