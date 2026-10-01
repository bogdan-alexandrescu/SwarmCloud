"""A workflow step may opt in to staging each input under its parent's step id (#75).

Owner decision 2026-10-01, option (b), OPT-IN. A step whose metadata says
`input_layout: "by_parent"` stages every `input_from` file at
`<parent_step_id>/<filename>`, so two parents that both write `notes.md` can
feed it. A step that does not opt in keeps today's layout exactly -- the
filename is the path -- and the collision #64 refuses at submission is still
refused for it.

The worker sees only TASK ids in `metadata.input_from`, so the service records
each parent's STEP id beside its task id in the step's `metadata.dispatch`
block (`input_parents`), which the spec signature covers. `input_from` itself
keeps its `{task id: filename}` shape: the UI's staged-files join, the masking
of filenames and the signature all read it as that.

The last section runs the WORKER's own `declared_inputs` on the metadata the
API just stored, so the two halves are held to one another rather than each to
its own idea of the format.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker import inputs as worker_inputs

from swarm_api.validation import (
    DEFAULT_INPUT_LAYOUT,
    INPUT_LAYOUT_METADATA_KEY,
    INPUT_LAYOUTS,
    StepSpec,
    validate_dag,
)

from .conftest import auth_header

PARENTS = ("scan-A", "scan-B")


def _post(client, steps: list[dict[str, Any]], *, metadata: dict[str, Any] | None = None):
    body: dict[str, Any] = {"steps": steps, "on_step_failure": "continue"}
    if metadata is not None:
        body["metadata"] = metadata
    return client.post("/v1/workflows", headers=auth_header("alice"), json=body)


def _root(step_id: str) -> dict[str, Any]:
    return {"step_id": step_id, "runner_profile": "mock", "input": {"prompt": f"run {step_id}"}}


def _join(*, layout: Any = None, filename: str = "notes.md", omit: bool = False) -> dict[str, Any]:
    step: dict[str, Any] = {
        "step_id": "merge",
        "runner_profile": "mock",
        "input": {"prompt": "merge the notes"},
        "depends_on": list(PARENTS),
        "input_from": {p: filename for p in PARENTS},
    }
    if not omit:
        step["metadata"] = {INPUT_LAYOUT_METADATA_KEY: layout}
    return step


def _tasks_by_step(db) -> dict[str, dict[str, Any]]:
    return {
        doc["step_id"]: doc
        for key, doc in db.docs.items()
        if key.startswith("tasks/") and doc.get("step_id")
    }


def _created(db) -> list[str]:
    return sorted(key for key in db.docs if key.startswith(("tasks/", "workflows/")))


def test_the_layouts_are_named_and_the_default_is_todays():
    assert INPUT_LAYOUTS == ("by_name", "by_parent")
    assert DEFAULT_INPUT_LAYOUT == "by_name"


# --------------------------------------------------------------------------
# 1. The opted-in fan-in is accepted, and records the parents' step ids
# --------------------------------------------------------------------------

def test_an_opted_in_fan_in_of_two_notes_md_is_accepted(client, db):
    response = _post(client, [_root(p) for p in PARENTS] + [_join(layout="by_parent")])
    assert response.status_code == 201, response.text

    tasks = _tasks_by_step(db)
    merge = tasks["merge"]["metadata"]
    parent_task = {p: tasks[p]["id"] for p in PARENTS}
    # `input_from` keeps its shape: {upstream task id: filename}.
    assert merge["input_from"] == {parent_task[p]: "notes.md" for p in PARENTS}
    # The step id of each parent, beside its task id, in the signed block.
    assert merge["dispatch"]["input_parents"] == {parent_task[p]: p for p in PARENTS}
    assert merge[INPUT_LAYOUT_METADATA_KEY] == "by_parent"
    # The parents are untouched: each still writes plain `notes.md`.
    for p in PARENTS:
        assert "input_parents" not in tasks[p]["metadata"]["dispatch"]
        assert tasks[p]["metadata"]["expected_outputs"] == ["notes.md"]


def test_the_workflow_level_layout_applies_to_every_step(client, db):
    response = _post(
        client,
        [_root(p) for p in PARENTS] + [_join(omit=True)],
        metadata={INPUT_LAYOUT_METADATA_KEY: "by_parent"},
    )
    assert response.status_code == 201, response.text
    merge = _tasks_by_step(db)["merge"]["metadata"]
    assert set(merge["dispatch"]["input_parents"].values()) == set(PARENTS)


def test_a_step_can_opt_back_out_of_a_workflow_level_layout(client, db, api_context):
    response = _post(
        client,
        [_root(p) for p in PARENTS] + [_join(layout="by_name")],
        metadata={INPUT_LAYOUT_METADATA_KEY: "by_parent"},
    )
    # By name again, so two `notes.md` collide exactly as before.
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_dag"
    assert _created(db) == []


def test_the_worker_stages_what_the_api_stored_under_distinct_paths(client, db):
    response = _post(client, [_root(p) for p in PARENTS] + [_join(layout="by_parent")])
    assert response.status_code == 201, response.text
    tasks = _tasks_by_step(db)

    declared = worker_inputs.declared_inputs(tasks["merge"]["metadata"])

    by_task = {item.upstream_task_id: item for item in declared}
    for p in PARENTS:
        item = by_task[tasks[p]["id"]]
        assert item.filename == "notes.md"
        assert item.destination == f"{p}/notes.md"


# --------------------------------------------------------------------------
# 2. A step that does not opt in is unchanged
# --------------------------------------------------------------------------

@pytest.mark.parametrize("layout", ["absent", "by_name"])
def test_a_non_opted_collision_is_still_refused(client, db, api_context, layout):
    join = _join(omit=True) if layout == "absent" else _join(layout="by_name")
    response = _post(client, [_root(p) for p in PARENTS] + [join])

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dag"
    assert body["detail"]["colliding_upstream_steps"] == sorted(PARENTS)
    assert _created(db) == []
    assert api_context.waker.calls == []


def test_a_non_opted_step_stores_exactly_what_it_stored_before(client, db):
    steps = [_root("research"), {
        "step_id": "write",
        "runner_profile": "mock",
        "input": {"prompt": "write it up"},
        "depends_on": ["research"],
        "input_from": {"research": "notes.md"},
    }]
    assert _post(client, steps).status_code == 201
    tasks = _tasks_by_step(db)
    write = tasks["write"]["metadata"]

    # Byte for byte the metadata a step got before #75: no layout key, no
    # parents block, the dispatch block's keys unchanged.
    assert sorted(write) == ["dispatch", "input_from", "workflow_step"]
    assert write["input_from"] == {tasks["research"]["id"]: "notes.md"}
    assert write["dispatch"] == {"strategy": "collect", "carrier": "checkpoints"}

    declared = worker_inputs.declared_inputs(write)
    assert declared == [worker_inputs.DeclaredInput(tasks["research"]["id"], "notes.md")]
    assert declared[0].destination == "notes.md"


# --------------------------------------------------------------------------
# 3. An invalid layout value is a 422 naming it
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value", ["flat", "BY_PARENT", "by-parent", "", None, 1, True, ["by_parent"], {"x": 1}]
)
def test_an_invalid_step_layout_is_refused_before_anything_is_created(
    client, db, api_context, value
):
    response = _post(client, [_root(p) for p in PARENTS] + [_join(layout=value)])

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dag", body
    assert body["detail"]["step_id"] == "merge"
    assert body["detail"]["path"] == "steps[merge].metadata.input_layout"
    assert body["detail"]["accepted"] == list(INPUT_LAYOUTS)
    for accepted in INPUT_LAYOUTS:
        assert accepted in body["message"]
    assert _created(db) == []
    assert api_context.waker.calls == []


def test_an_invalid_workflow_layout_is_refused_naming_the_workflow_path(client, db):
    response = _post(
        client,
        [_root(p) for p in PARENTS] + [_join(omit=True)],
        metadata={INPUT_LAYOUT_METADATA_KEY: "sideways"},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dag"
    assert body["detail"]["path"] == "metadata.input_layout"
    assert _created(db) == []


def test_a_step_metadata_reserved_key_is_refused(client, db):
    join = _join(layout="by_parent")
    join["metadata"]["input_from"] = {"x": "y"}
    response = _post(client, [_root(p) for p in PARENTS] + [join])
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_dispatch"
    assert _created(db) == []


# --------------------------------------------------------------------------
# 4. The DAG rule itself
# --------------------------------------------------------------------------

def test_validate_dag_accepts_a_same_name_fan_in_only_by_parent():
    roots = [StepSpec(step_id=p, depends_on=()) for p in PARENTS]
    by_parent = StepSpec(
        step_id="merge",
        depends_on=PARENTS,
        input_from={p: "notes.md" for p in PARENTS},
        input_layout="by_parent",
    )
    assert validate_dag([*roots, by_parent], max_steps=10)[-1] == "merge"


def test_validate_dag_still_refuses_an_unsafe_filename_by_parent():
    roots = [StepSpec(step_id=p, depends_on=()) for p in PARENTS]
    bad = StepSpec(
        step_id="merge",
        depends_on=PARENTS,
        input_from={"scan-A": "../notes.md", "scan-B": "notes.md"},
        input_layout="by_parent",
    )
    with pytest.raises(Exception) as exc:
        validate_dag([*roots, bad], max_steps=10)
    assert getattr(exc.value, "code", None) == "invalid_dag"
