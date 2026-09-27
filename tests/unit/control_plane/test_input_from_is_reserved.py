"""`metadata.input_from` is the service's to write, never a caller's (#151).

Owner decision, 2026-09-25, on #151: `metadata.input_from` is RESERVED, like
`metadata.dispatch`. Only workflow expansion (`SubmissionService.submit_workflow`)
writes it. It rewrites a step's `input_from = {upstream_step: filename}` to
`{upstream_TASK_id: filename}`, and that key is what the worker stages from
(`agent_worker.inputs.METADATA_KEY`).

Before this, a plain `POST /v1/tasks` could carry the key. `_build_task` copied it
verbatim and the worker honoured it, having passed none of the checks the
workflow path makes: no dependency edge, so no guarantee the upstream had run.
A bad declaration was refused only at run time, after the task had been
admitted and had held capacity.

The workflow's OWN `metadata` has the same hole one level up. `submit_workflow`
copies it onto every step's task, so a workflow-level `metadata.input_from`
reached every step that declared no `input_from` of its own, every root step
among them, and was silently replaced on the steps that did. PR #65's fix-up
noted that this was undocumented. It is refused too.

What these tests hold, in the order that matters:

  * A refusal CREATES NOTHING. That is asserted on the store, not only on the
    status code and the message. A 422 that had already written the task would
    be worse than the defect it replaced.
  * Any value is refused, `{}` and `null` included, because the key is
    reserved, not validated. Checking the value would reopen the question the
    owner closed.
  * Workflow expansion still writes the key. Reserving it from callers must not
    reserve it from the one writer that is meant to use it.

`metadata.expected_outputs` (#149, merged in #153) is the third key the
service writes and a caller may not. #153 reserved it with a separate function,
deliberately, so as not to collide with this change, and said folding it into
`RESERVED_METADATA_KEYS` once both had merged was the follow-up. The last
section holds that fold: one refusal names every reserved key the caller sent,
instead of one key per round trip.

No credentials, no network, no emulator: `runner_profile: "mock"` has no
provider, so every accepted task reaches READY or PARKED on the in-memory
Firestore.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api.errors import ValidationFailed
from swarm_api.expected_outputs import EXPECTED_OUTPUTS_METADATA_KEY
from swarm_api.validation import (
    DISPATCH_METADATA_KEY,
    INPUT_FROM_METADATA_KEY,
    RESERVED_METADATA_KEYS,
    reject_reserved_metadata,
)

from .conftest import auth_header

#: The stable code the `dispatch` reservation already answers with. The owner's
#: instruction on #151 was "like dispatch", so a caller branching on the code
#: reads both reservations the same way.
RESERVED_CODE = "invalid_dispatch"

#: Every shape a caller might send. The key is reserved, so the VALUE must not
#: matter: an empty mapping and an explicit null are refused like a real one.
CALLER_VALUES: list[Any] = [
    {"task_0123456789abcdef": "notes.md"},
    {},
    None,
    "notes.md",
    ["notes.md"],
]


def _created(db) -> dict[str, list[str]]:
    """What a submission wrote that it should not have: tasks and workflows."""
    return {"tasks": db.paths("tasks/"), "workflows": db.paths("workflows/")}


def _rejected(api_context, code: str) -> float:
    value = api_context.metrics.registry.get_sample_value(
        "swarm_api_tasks_rejected_total", {"reason": code}
    )
    return value or 0.0


def _assert_reserved_refusal(response) -> None:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == RESERVED_CODE
    assert body["detail"]["reserved_metadata_keys"] == [INPUT_FROM_METADATA_KEY]
    # The message is what a curl user reads, so it has to say both halves: who
    # sets the key, and what to submit instead.
    message = body["message"]
    assert "metadata.input_from" in message
    assert "workflow" in message.lower()
    assert "POST /v1/workflows" in message


# --------------------------------------------------------------------------
# A plain task
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", CALLER_VALUES, ids=repr)
def test_a_plain_task_carrying_metadata_input_from_is_refused_and_creates_nothing(
    client, db, value
):
    assert _created(db) == {"tasks": [], "workflows": []}, "the store was not empty"

    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={
            "runner_profile": "mock",
            "input": {"prompt": "summarise notes.md"},
            "metadata": {"unit": "payments", "input_from": value},
        },
    )

    # THE PROPERTY FIRST: nothing was written. Only then the answer's shape.
    assert _created(db) == {"tasks": [], "workflows": []}
    _assert_reserved_refusal(response)


def test_a_batch_with_one_offending_task_creates_none_of_them(client, db):
    """A batch is all-or-nothing on refusal: the clean task is not created either."""
    response = client.post(
        "/v1/tasks/batch",
        headers=auth_header("alice"),
        json={
            "tasks": [
                {"runner_profile": "mock", "metadata": {"unit": "clean"}},
                {
                    "runner_profile": "mock",
                    "metadata": {"input_from": {"task_0123456789abcdef": "notes.md"}},
                },
            ]
        },
    )

    assert _created(db) == {"tasks": [], "workflows": []}
    _assert_reserved_refusal(response)


def test_the_refusal_is_counted_like_every_other_rejected_submission(client, api_context):
    before = _rejected(api_context, RESERVED_CODE)
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "metadata": {"input_from": {}}},
    )
    assert response.status_code == 422, response.text
    assert _rejected(api_context, RESERVED_CODE) == before + 1


def test_a_plain_task_without_it_is_accepted_and_stores_no_input_from(client, db):
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={
            "runner_profile": "mock",
            "input": {"prompt": "hi"},
            "metadata": {"unit": "payments", "origin": "cli"},
        },
    )

    assert response.status_code == 201, response.text
    task_id = response.json()["task"]["id"]
    # Task documents only: an accepted task also writes its submission event
    # under tasks/<id>/events/, which the refusal tests rightly count as
    # "something created" and this one must not mistake for a second task.
    task_documents = [path for path in db.paths("tasks/") if path.count("/") == 1]
    assert task_documents == [f"tasks/{task_id}"]
    stored = db.docs[f"tasks/{task_id}"]
    assert stored["state"] == "READY"
    assert stored["metadata"]["unit"] == "payments"
    assert stored["metadata"]["origin"] == "cli"
    assert INPUT_FROM_METADATA_KEY not in stored["metadata"]


# --------------------------------------------------------------------------
# Workflow expansion is still the writer
# --------------------------------------------------------------------------

def test_workflow_expansion_still_writes_input_from_onto_its_tasks(client, db):
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "metadata": {"unit": "payments"},
            "steps": [
                {"step_id": "build", "runner_profile": "mock"},
                {"step_id": "lint", "runner_profile": "mock"},
                {
                    "step_id": "test",
                    "runner_profile": "mock",
                    "depends_on": ["build", "lint"],
                    "input_from": {"build": "artifact.tar", "lint": "lint.txt"},
                },
            ],
        },
    )

    assert response.status_code == 201, response.text
    by_step = {s["step_id"]: s["task_id"] for s in response.json()["workflow"]["steps"]}
    build = db.docs[f"tasks/{by_step['build']}"]
    lint = db.docs[f"tasks/{by_step['lint']}"]
    test = db.docs[f"tasks/{by_step['test']}"]

    # Rewritten from step ids to the task ids expansion minted, which is the
    # only form the worker can resolve.
    assert test["metadata"][INPUT_FROM_METADATA_KEY] == {
        build["id"]: "artifact.tar",
        lint["id"]: "lint.txt",
    }
    # And only on the step that declared it.
    assert INPUT_FROM_METADATA_KEY not in build["metadata"]
    assert INPUT_FROM_METADATA_KEY not in lint["metadata"]
    # The other key expansion writes, on the UPSTREAM steps (#149). Reserving it
    # from callers must not reserve it from this writer either.
    assert build["metadata"][EXPECTED_OUTPUTS_METADATA_KEY] == ["artifact.tar"]
    assert lint["metadata"][EXPECTED_OUTPUTS_METADATA_KEY] == ["lint.txt"]
    assert EXPECTED_OUTPUTS_METADATA_KEY not in test["metadata"]
    # The caller's own workflow metadata still reaches every step.
    assert {build["metadata"]["unit"], lint["metadata"]["unit"], test["metadata"]["unit"]} == {
        "payments"
    }


# --------------------------------------------------------------------------
# A workflow's own metadata
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", CALLER_VALUES, ids=repr)
def test_a_workflow_whose_own_metadata_carries_input_from_is_refused_and_creates_nothing(
    client, db, value
):
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "metadata": {"input_from": value},
            "steps": [{"step_id": "only", "runner_profile": "mock"}],
        },
    )

    assert _created(db) == {"tasks": [], "workflows": []}
    _assert_reserved_refusal(response)


def test_workflow_metadata_input_from_is_refused_when_a_step_also_declares_its_own(
    client, db
):
    """The case that used to be SILENTLY REPLACED per step (PR #65's fix-up note).

    `root` declares nothing, so it would have inherited the workflow's value
    verbatim; `child` declares its own, so the workflow's value would have been
    dropped without a word. Neither outcome is what the caller wrote, so neither
    is chosen for them.
    """
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "metadata": {"input_from": {"task_0123456789abcdef": "notes.md"}},
            "steps": [
                {"step_id": "root", "runner_profile": "mock"},
                {
                    "step_id": "child",
                    "runner_profile": "mock",
                    "depends_on": ["root"],
                    "input_from": {"root": "notes.md"},
                },
            ],
        },
    )

    assert _created(db) == {"tasks": [], "workflows": []}
    _assert_reserved_refusal(response)


def test_a_refused_workflow_is_counted_like_every_other_rejected_submission(
    client, api_context
):
    before = _rejected(api_context, RESERVED_CODE)
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "metadata": {"input_from": {}},
            "steps": [{"step_id": "only", "runner_profile": "mock"}],
        },
    )
    assert response.status_code == 422, response.text
    assert _rejected(api_context, RESERVED_CODE) == before + 1


# --------------------------------------------------------------------------
# The pure function
# --------------------------------------------------------------------------

def test_every_service_written_key_is_reserved_in_one_place():
    assert INPUT_FROM_METADATA_KEY == "input_from"
    assert RESERVED_METADATA_KEYS == (
        DISPATCH_METADATA_KEY,
        INPUT_FROM_METADATA_KEY,
        EXPECTED_OUTPUTS_METADATA_KEY,
    )


def test_the_worker_reads_the_key_this_service_reserves():
    """A reservation on a key the worker does not read would refuse nothing that matters.

    Imported directly, not through `importorskip`: a seam test that can skip
    passes by silence the day the import breaks.
    """
    from agent_worker.inputs import METADATA_KEY as WORKER_KEY

    assert WORKER_KEY == INPUT_FROM_METADATA_KEY


@pytest.mark.parametrize("value", CALLER_VALUES, ids=repr)
def test_reject_reserved_metadata_refuses_input_from_whatever_its_value(value):
    with pytest.raises(ValidationFailed) as exc:
        reject_reserved_metadata({"unit": "payments", "input_from": value})
    assert exc.value.code == RESERVED_CODE
    assert exc.value.detail == {"reserved_metadata_keys": ["input_from"]}


def test_reject_reserved_metadata_names_every_reserved_key_it_found():
    with pytest.raises(ValidationFailed) as exc:
        reject_reserved_metadata(
            {"expected_outputs": [], "input_from": {}, "dispatch": {}, "unit": "x"}
        )
    # In RESERVED_METADATA_KEYS order, whatever order the caller sent them in.
    assert exc.value.detail == {
        "reserved_metadata_keys": ["dispatch", "input_from", "expected_outputs"]
    }
    assert "metadata.dispatch" in exc.value.message
    assert "metadata.input_from" in exc.value.message
    assert "metadata.expected_outputs" in exc.value.message


# --------------------------------------------------------------------------
# One refusal for every reserved key (#153's expected_outputs, folded in)
# --------------------------------------------------------------------------

#: What a caller who sent all three is told, in RESERVED_METADATA_KEYS order.
#: Spelled out rather than read from the constant, so a key dropped from the
#: constant fails here instead of being dropped from the expectation too.
EVERY_RESERVED_KEY = ["dispatch", "input_from", "expected_outputs"]


@pytest.mark.parametrize("value", [["notes.md"], [], None], ids=repr)
def test_reject_reserved_metadata_refuses_expected_outputs_whatever_its_value(value):
    """The same function refuses all three keys. A second, separate check would
    mean a caller who sent two reserved keys learnt about them one 422 at a time."""
    with pytest.raises(ValidationFailed) as exc:
        reject_reserved_metadata({"unit": "payments", "expected_outputs": value})
    assert exc.value.code == RESERVED_CODE
    assert exc.value.detail == {"reserved_metadata_keys": ["expected_outputs"]}


def test_a_plain_task_carrying_every_reserved_key_is_told_all_of_them_and_creates_nothing(
    client, db
):
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={
            "runner_profile": "mock",
            "metadata": {
                "unit": "payments",
                "expected_outputs": ["notes.md"],
                "input_from": {"task_0123456789abcdef": "notes.md"},
                "dispatch": {"strategy": "direct-pr"},
            },
        },
    )

    assert _created(db) == {"tasks": [], "workflows": []}
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == RESERVED_CODE
    assert body["detail"]["reserved_metadata_keys"] == EVERY_RESERVED_KEY
    for key in EVERY_RESERVED_KEY:
        assert f"metadata.{key}" in body["message"], key


def test_a_workflow_whose_own_metadata_carries_input_from_and_expected_outputs_is_told_both(
    client, db, api_context
):
    """Both keys would be copied onto every step's task. One refusal, counted
    once, names both, and nothing is created."""
    before = _rejected(api_context, RESERVED_CODE)
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "metadata": {
                "expected_outputs": ["notes.md"],
                "input_from": {"task_0123456789abcdef": "notes.md"},
            },
            "steps": [
                {"step_id": "a", "runner_profile": "mock"},
                {
                    "step_id": "b",
                    "runner_profile": "mock",
                    "depends_on": ["a"],
                    "input_from": {"a": "notes.md"},
                },
            ],
        },
    )

    assert _created(db) == {"tasks": [], "workflows": []}
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == RESERVED_CODE
    assert body["detail"]["reserved_metadata_keys"] == ["input_from", "expected_outputs"]
    assert "metadata.input_from" in body["message"]
    assert "metadata.expected_outputs" in body["message"]
    assert _rejected(api_context, RESERVED_CODE) == before + 1
