"""The worker stages an opted-in step's inputs under each parent's step id (#75).

swarm-api records, for a step that opted in to `input_layout: "by_parent"`,
each parent's STEP id beside its task id in `metadata.dispatch.input_parents`
(`metadata.input_from` keeps its `{task id: filename}` shape). The worker then
stages each file at `<parent_step_id>/<filename>`, so two parents' `notes.md`
land side by side instead of one overwriting the other.

A step without `input_parents` stages exactly as before: the filename is the
path, and two parents naming one file are still refused. A present but
malformed `input_parents` fails the attempt, like a malformed `input_from`:
skipping it would stage the opted-in step's files where its prompt does not
expect them.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from agent_worker import inputs as inputs_mod
from agent_worker.errors import ExitCode, InputUnavailable
from agent_worker.logs import build_logger
from swarm_common.states import TaskState

from worker_seeds import TENANT, seed_attempt

from test_input_from import run_upstream

PARENTS = {"task_a": "scan-A", "task_b": "scan-B"}
TEXT = {"task_a": "notes from A\n", "task_b": "notes from B, which differ\n"}


def _metadata(parents: Any = None, *, files: dict[str, str] | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"input_from": files or {t: "notes.md" for t in PARENTS}}
    if parents is not None:
        metadata["dispatch"] = {
            "strategy": "collect",
            "carrier": "checkpoints",
            "input_parents": parents,
        }
    return metadata


def _seed_upstream(db: Any, store: Any, task_id: str, name: str = "notes.md") -> None:
    key = f"tenants/{TENANT}/tasks/{task_id}/attempts/att/artifacts/{name}"
    body = TEXT[task_id].encode("utf-8")
    store.upload_bytes(key, body)
    db.seed(
        f"tasks/{task_id}",
        {
            "id": task_id,
            "tenant_id": TENANT,
            "state": TaskState.SUCCEEDED.value,
            "result_summary": {
                "artifacts": [{"name": name, "bytes": len(body), "uri": store.uri(key)}]
            },
        },
    )


def _stage(
    metadata: dict[str, Any], *, db: Any, store: Any, work: Path, reserved=frozenset({"repo"})
):
    return inputs_mod.stage_inputs(
        inputs_mod.declared_inputs(metadata),
        work=work,
        store=store,
        db=db,
        tenant_id=TENANT,
        logger=build_logger(
            task_id="task_2", attempt_id="att_2", tenant_id=TENANT, generation=1,
            runner_profile="mock", stream=io.StringIO(),
        ),
        resumed=False,
        max_total_bytes=4096,
        reserved=reserved,
    )


# --------------------------------------------------------------------------
# the opted-in fan-in
# --------------------------------------------------------------------------

def test_an_opted_in_fan_in_of_two_notes_md_stages_both_under_distinct_paths(
    db, store, tmp_path
):
    work = tmp_path / "work"
    work.mkdir()
    for task_id in PARENTS:
        _seed_upstream(db, store, task_id)

    staged = _stage(_metadata(dict(PARENTS)), db=db, store=store, work=work)

    assert sorted(item.path for item in staged) == ["scan-A/notes.md", "scan-B/notes.md"]
    assert {item.filename for item in staged} == {"notes.md"}
    assert (work / "scan-A" / "notes.md").read_text() == TEXT["task_a"]
    assert (work / "scan-B" / "notes.md").read_text() == TEXT["task_b"]
    assert not (work / "notes.md").exists()


def test_an_opted_in_fan_in_runs_end_to_end(db, store, worker_factory):
    """A real downstream attempt: both files reach the agent's directory."""
    for task_id, step_id in PARENTS.items():
        run_upstream(
            db, worker_factory,
            task_id=task_id, attempt_id=f"att_{step_id}", lease_id=f"lease_{step_id}",
            artifact_name="notes.md", artifact_text=TEXT[task_id],
        )
    seed_attempt(
        db, task_id="task_2", attempt_id="att_2", lease_id="lease_2",
        task_input={"prompt": "merge", "steps": 1, "sleep_seconds": 0.01},
    )
    db.doc("tasks/task_2")["metadata"] = _metadata(dict(PARENTS))

    worker, _config, _exporter = worker_factory(
        task_id="task_2", attempt_id="att_2", lease_id="lease_2"
    )
    assert worker.run() == ExitCode.OK

    staged = db.doc("tasks/task_2")["result_summary"]["staged_inputs"]
    assert sorted(item["path"] for item in staged) == ["scan-A/notes.md", "scan-B/notes.md"]
    assert {item["bytes"] for item in staged} == {len(TEXT[t].encode()) for t in PARENTS}


# --------------------------------------------------------------------------
# a step that did not opt in
# --------------------------------------------------------------------------

def test_a_non_opted_step_parses_exactly_as_before():
    declared = inputs_mod.declared_inputs({"input_from": {"task_a": " notes.md "}})
    assert declared == [inputs_mod.DeclaredInput("task_a", "notes.md")]
    assert declared[0].parent_step_id is None
    assert declared[0].destination == "notes.md"


def test_a_non_opted_step_stages_at_the_filename(db, store, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _seed_upstream(db, store, "task_a")

    staged = _stage({"input_from": {"task_a": "notes.md"}}, db=db, store=store, work=work)

    assert [item.path for item in staged] == ["notes.md"]
    assert staged[0].as_dict()["path"] == "notes.md"


def test_a_non_opted_collision_is_still_refused():
    with pytest.raises(InputUnavailable, match="notes.md"):
        inputs_mod.declared_inputs(_metadata(None))


def test_a_dispatch_block_without_input_parents_is_not_an_opt_in():
    metadata = _metadata(None)
    metadata["dispatch"] = {"strategy": "collect", "carrier": "checkpoints"}
    with pytest.raises(InputUnavailable, match="overwrite"):
        inputs_mod.declared_inputs(metadata)


# --------------------------------------------------------------------------
# a malformed opt-in fails the attempt
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "parents",
    [
        pytest.param(["scan-A"], id="not-a-mapping"),
        pytest.param("scan-A", id="a-string"),
        pytest.param({"task_a": "scan-A"}, id="a-parent-missing"),
        pytest.param({**PARENTS, "task_z": "scan-Z"}, id="a-parent-not-declared"),
        pytest.param({"task_a": "scan-A", "task_b": ""}, id="empty-step-id"),
        pytest.param({"task_a": "scan-A", "task_b": 7}, id="not-a-string"),
        pytest.param({"task_a": "scan-A", "task_b": ".."}, id="traversal"),
        pytest.param({"task_a": "scan-A", "task_b": "."}, id="dot"),
        pytest.param({"task_a": "scan-A", "task_b": "x/y"}, id="two-segments"),
        pytest.param({"task_a": "scan-A", "task_b": "a\\b"}, id="backslash"),
        pytest.param({"task_a": "scan-A", "task_b": "scan-A"}, id="two-tasks-one-step"),
    ],
)
def test_a_malformed_input_parents_is_refused_rather_than_ignored(parents):
    with pytest.raises(InputUnavailable, match="input_parents"):
        inputs_mod.declared_inputs(_metadata(parents))


def test_a_parent_step_id_the_worker_owns_is_refused_at_staging(db, store, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(InputUnavailable, match="repo"):
        _stage(_metadata({"task_a": "repo", "task_b": "scan-B"}), db=db, store=store, work=work)


def test_distinct_destinations_are_still_checked_on_destinations():
    """Defence in depth: two entries landing on one path are refused even
    when each looks fine on its own."""
    clash = [
        inputs_mod.DeclaredInput("task_a", "notes.md", parent_step_id="scan-A"),
        inputs_mod.DeclaredInput("task_b", "notes.md", parent_step_id="scan-A"),
    ]
    with pytest.raises(InputUnavailable, match="scan-A/notes.md"):
        inputs_mod._assert_distinct_destinations(clash)
