"""`metadata.qa_target` is reserved: written by swarm-api's `:run`, refused
from every caller (docs/design/visual-qa-any-app.md §1.1; lane VQ2-RESERVE,
part of #1933).

VQ-2 already made a caller's mark inert: the worker's route serves a target
only to a task that `:run`'s `qa_target_runs` record names, and a forged mark
answers 404 (test_qa_target_attempt_route.py's
`test_a_mark_this_platform_did_not_write_answers_as_missing`, which this lane
leaves as it is). Reserving the key is the defence in depth decided on
2026-10-22 (VQ2-TARGETS question 3): the mark cannot reach the store from a
caller at all, so nothing but `:run` writes it.

What these tests hold, over the real app and the in-memory Firestore:

  * a task, a batch item, a workflow step and a workflow's own metadata
    carrying `qa_target` are refused with the reserved-key 422 every other
    reserved key gives, naming the key, and nothing is created;
  * the `:run` exemption is the exact mark and only inside its block, as the
    schedule's is;
  * `:run` still stamps its browser steps and its tasks are admitted;
  * `qatargets.METADATA_KEY`, the key `:run` writes and the worker's route
    reads, is the reserved one, and it is served as stored
    (`task_input.PLATFORM_METADATA_KEYS`).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from swarm_api import qatargets, task_input
from swarm_api.errors import ValidationFailed
from swarm_api.validation import (
    QA_TARGET_METADATA_KEY,
    RESERVED_METADATA_KEYS,
    qa_target_mark_allowed,
    reject_reserved_metadata,
    schedule_mark_allowed,
)

from .conftest import auth_header
from .test_qa_targets import (  # noqa: F401 - fixtures are used by name
    CAROL_TENANT,
    api_context,
    approve,
    register,
    resolver,
    run,
    seeded,
)

#: The code every reserved key is refused with (test_input_from_is_reserved.py).
RESERVED_CODE = "invalid_dispatch"

#: A mark as `:run` writes it, for a target that exists nowhere.
MARK = {"id": "qat_forged", "revision": 1}


def _created(db) -> dict[str, list[str]]:
    return {"tasks": db.paths("tasks/"), "workflows": db.paths("workflows/")}


def _assert_refused(response) -> None:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == RESERVED_CODE
    assert body["detail"]["reserved_metadata_keys"] == [QA_TARGET_METADATA_KEY]
    assert f"metadata.{QA_TARGET_METADATA_KEY}" in body["message"]


# --------------------------------------------------------------------------
# A caller cannot write the mark
# --------------------------------------------------------------------------


def test_the_key_is_reserved_under_the_name_run_writes():
    # MUTATION: drop QA_TARGET_METADATA_KEY from RESERVED_METADATA_KEYS and this is red.
    assert QA_TARGET_METADATA_KEY in RESERVED_METADATA_KEYS
    # MUTATION: rename the key in validation.py alone and `:run` writes a key nothing refuses.
    assert QA_TARGET_METADATA_KEY == qatargets.METADATA_KEY == "qa_target"
    # Served as stored, unmasked, as every platform-written key is: the console
    # and the worker's route read `{id, revision}` back exactly.
    # MUTATION: give task_input its own list without the key and this is red.
    assert QA_TARGET_METADATA_KEY in task_input.PLATFORM_METADATA_KEYS


@pytest.mark.parametrize("value", [MARK, {}, None, "qat_forged"], ids=repr)
def test_reject_reserved_metadata_refuses_the_mark_whatever_its_value(value):
    with pytest.raises(ValidationFailed) as exc:
        reject_reserved_metadata({"unit": "qa", QA_TARGET_METADATA_KEY: value})
    # MUTATION: drop the key from RESERVED_METADATA_KEYS and nothing is raised.
    assert exc.value.code == RESERVED_CODE
    assert exc.value.detail == {"reserved_metadata_keys": [QA_TARGET_METADATA_KEY]}


def test_a_task_carrying_the_mark_is_refused_and_creates_nothing(client, db):
    before = _created(db)
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "x"},
              "metadata": {QA_TARGET_METADATA_KEY: dict(MARK)}},
    )
    # MUTATION: drop the key from RESERVED_METADATA_KEYS and this is 201, mark stored.
    _assert_refused(response)
    assert _created(db) == before


def test_a_batch_item_carrying_the_mark_is_refused_and_creates_nothing(client, db):
    before = _created(db)
    response = client.post(
        "/v1/tasks/batch", headers=auth_header("alice"),
        json={"tasks": [
            {"runner_profile": "mock", "input": {"prompt": "a"}},
            {"runner_profile": "mock", "input": {"prompt": "b"},
             "metadata": {QA_TARGET_METADATA_KEY: dict(MARK)}},
        ]},
    )
    # MUTATION: drop the key from RESERVED_METADATA_KEYS and the batch is created.
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reserved_metadata_keys"] == [QA_TARGET_METADATA_KEY]
    assert _created(db) == before


def _workflow(step_metadata: dict[str, Any] | None = None,
              metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    step: dict[str, Any] = {"step_id": "capture", "runner_profile": "mock",
                            "input": {"prompt": "x"}}
    if step_metadata is not None:
        step["metadata"] = step_metadata
    body: dict[str, Any] = {"steps": [step]}
    if metadata is not None:
        body["metadata"] = metadata
    return body


@pytest.mark.parametrize("where", ["step", "workflow"])
def test_a_workflow_carrying_the_mark_is_refused_and_creates_nothing(client, db, where):
    """`:run` stamps the mark on a STEP of a workflow it submits, so this is the
    shape a forger would copy; the workflow's own metadata merges into every
    step, so it is refused there too."""
    before = _created(db)
    mark = {QA_TARGET_METADATA_KEY: dict(MARK)}
    body = _workflow(step_metadata=mark) if where == "step" else _workflow(metadata=mark)
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    # MUTATION: drop the key from RESERVED_METADATA_KEYS and this is 201, mark stored.
    _assert_refused(response)
    assert _created(db) == before


def test_a_workflow_without_the_mark_is_admitted(client, db):
    # CONTROL for the two above: the same body, unmarked, is created.
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_workflow())
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------
# `:run`'s exemption
# --------------------------------------------------------------------------


def test_the_exemption_is_the_exact_mark_and_only_inside_its_block():
    with pytest.raises(ValidationFailed):
        reject_reserved_metadata({QA_TARGET_METADATA_KEY: MARK})
    with qa_target_mark_allowed(MARK):
        # MUTATION: never consult the allowed mark in `reject_reserved_metadata` and this raises.
        reject_reserved_metadata({QA_TARGET_METADATA_KEY: copy.deepcopy(MARK)})
        # MUTATION: exempt the key, not the value, and another target's mark passes.
        with pytest.raises(ValidationFailed):
            reject_reserved_metadata({QA_TARGET_METADATA_KEY: {**MARK, "id": "qat_other"}})
        with pytest.raises(ValidationFailed):
            reject_reserved_metadata({QA_TARGET_METADATA_KEY: {**MARK, "revision": 2}})
        # MUTATION: return early on an exempt mark and a reserved key beside it passes.
        with pytest.raises(ValidationFailed) as exc:
            reject_reserved_metadata({QA_TARGET_METADATA_KEY: MARK, "dispatch": {}})
        assert exc.value.detail == {"reserved_metadata_keys": ["dispatch"]}
    # MUTATION: leave the ContextVar set on exit and this passes after the block.
    with pytest.raises(ValidationFailed):
        reject_reserved_metadata({QA_TARGET_METADATA_KEY: MARK})


def test_the_schedules_exemption_does_not_admit_the_mark():
    """The two exemptions are separate: a schedule's firing cannot carry a QA
    mark, nor `:run` a schedule mark."""
    schedule = {"schedule_id": "s", "firing_id": "f", "slot": None, "type": "t"}
    with schedule_mark_allowed(schedule):
        # MUTATION: share one ContextVar between the two keys and this passes.
        with pytest.raises(ValidationFailed):
            reject_reserved_metadata({QA_TARGET_METADATA_KEY: MARK})
    with qa_target_mark_allowed(MARK):
        with pytest.raises(ValidationFailed):
            reject_reserved_metadata({"schedule": schedule})


def test_run_still_stamps_its_browser_steps_and_they_are_admitted(client, db, seeded):
    target = register(client).json()["qa_target"]
    approve(client, target["id"])
    response = run(client, target["id"], file="none")
    # MUTATION: drop `with qa_target_mark_allowed(mark)` from `run_target` and this is 422.
    assert response.status_code == 201, response.text
    answer = response.json()
    stamped = [d for p, d in db.docs.items() if p.startswith("tasks/")
               and d.get("workflow_id") == answer["workflow_id"]
               and (d.get("metadata") or {}).get(QA_TARGET_METADATA_KEY)]
    assert {t["step_id"] for t in stamped} == set(answer["browser_steps"]) == {"capture-g1"}
    for task in stamped:
        assert task["metadata"][QA_TARGET_METADATA_KEY] == {"id": target["id"], "revision": 1}
        assert task["tenant_id"] == CAROL_TENANT
        # Admitted like any root step: it waits for capacity, it is not refused.
        assert task["state"] in {"QUEUED", "READY", "PARKED"}, task["state"]
    # MUTATION: leave the exemption set after `:run` returns and this is 201.
    forged = client.post(
        "/v1/tasks", headers=auth_header("carol"),
        json={"runner_profile": "mock", "input": {"prompt": "x"},
              "metadata": {QA_TARGET_METADATA_KEY: dict(answer["qa_target"])}},
    )
    _assert_refused(forged)
