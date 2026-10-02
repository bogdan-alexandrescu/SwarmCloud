"""A parked attempt's uploads are part of the task's result (#166).

Owner decision, 2026-09-28. An attempt that parks uploads what its agent wrote
so far (`_upload_outputs` runs on every park), and the next attempt resumes
the work. Until now only the FINISHING attempt's own uploads made up
`result_summary.artifacts`, so a file written before the park and not written
again was missing: the attempt failed its expected-output check, and a
dependant could not stage it, though the object sat in the bucket.

The finishing attempt now lists those uploads BY REFERENCE --
`{name, bytes, uri: <the parked attempt's object>, carried_from: <its id>}` --
with nothing downloaded again. A name the finishing attempt uploaded itself
wins. The dependant's staging already accepts an object under any attempt of
its upstream task (`inputs.artifact_key`), and that is pinned here too.
"""

from __future__ import annotations

from typing import Any

from agent_worker.errors import ExitCode
from swarm_common.states import EventType, TaskState

from conftest import TENANT, seed_attempt

PARKED_TEXT = "written before the park\n"


def _park_after_writing(db: Any, worker_factory: Any, *, name: str = "notes.md") -> None:
    """Attempt 1: its agent writes `name`, meets a 429, and the attempt parks for real.

    The mock writes its artifact only after a clean run, so the file is put in
    the artifacts folder just before the park's upload: what an agent that
    wrote a file and was then rate-limited leaves there.
    """
    seed_attempt(
        db,
        attempt_id="att_1",
        lease_id="lease_1",
        task_input={
            "prompt": "write the notes",
            "steps": 1,
            "sleep_seconds": 0.01,
            "quota_exhausted": True,
            "retry_after_seconds": 1800,
        },
        simulated={"provider": "anthropic"},
    )
    db.doc("tasks/task_1")["metadata"] = {"expected_outputs": [name]}
    worker, _, _ = worker_factory(attempt_id="att_1", lease_id="lease_1")
    upload = worker._upload_outputs

    def written_then_upload(**kwargs: Any) -> dict[str, Any]:
        (worker.ws.artifacts / name).write_text(PARKED_TEXT)
        return upload(**kwargs)

    worker._upload_outputs = written_then_upload  # type: ignore[method-assign]
    assert worker.run() == ExitCode.PARKED
    parked = [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value]
    assert [a["name"] for a in parked[-1]["detail"]["artifacts"]] == [name]


def _resume(db: Any, worker_factory: Any, *, write: str | None, name: str = "notes.md") -> int:
    """Attempt 2 of the same task: writes `write` into `name`, or nothing."""
    task_input: dict[str, Any] = {"prompt": "finish", "steps": 1, "sleep_seconds": 0.01}
    if write is not None:
        task_input.update({"artifact_name": name, "artifact_text": write})
    seed_attempt(
        db, attempt_id="att_2", lease_id="lease_2", attempt_count=2, task_input=task_input
    )
    db.doc("tasks/task_1")["metadata"] = {"expected_outputs": [name]}
    worker, _, _ = worker_factory(attempt_id="att_2", lease_id="lease_2")
    return worker.run()


def _entry(db: Any, name: str) -> dict[str, Any]:
    entries = db.doc("tasks/task_1")["result_summary"]["artifacts"]
    return next(e for e in entries if e["name"] == name)


def test_a_file_written_before_a_park_is_carried_by_reference(db, store, worker_factory):
    _park_after_writing(db, worker_factory)
    before = set(store.list_keys(f"tenants/{TENANT}/tasks/task_1/attempts/att_2/"))

    assert _resume(db, worker_factory, write=None) == ExitCode.OK

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    entry = _entry(db, "notes.md")
    assert entry["carried_from"] == "att_1"
    assert "/attempts/att_1/artifacts/notes.md" in entry["uri"]
    assert entry["bytes"] == len(PARKED_TEXT)
    assert "expected_outputs_missing" not in task["result_summary"]
    # BY REFERENCE: nothing was copied under the finishing attempt's prefix.
    after = set(store.list_keys(f"tenants/{TENANT}/tasks/task_1/attempts/att_2/"))
    assert not any(key.endswith("/artifacts/notes.md") for key in after - before)


def test_the_dependant_stages_the_carried_file(db, store, worker_factory):
    _park_after_writing(db, worker_factory)
    assert _resume(db, worker_factory, write=None) == ExitCode.OK

    seed_attempt(
        db, task_id="task_2", attempt_id="att_d", lease_id="lease_d",
        task_input={"prompt": "use it", "steps": 1, "sleep_seconds": 0.01},
    )
    db.doc("tasks/task_2")["metadata"] = {"input_from": {"task_1": "notes.md"}}
    downstream, _, _ = worker_factory(task_id="task_2", attempt_id="att_d", lease_id="lease_d")

    assert downstream.run() == ExitCode.OK, db.doc("tasks/task_2").get("last_error")
    staged = db.doc("tasks/task_2")["result_summary"]["staged_inputs"]
    assert [item["filename"] for item in staged] == ["notes.md"]
    assert "/attempts/att_1/" in staged[0]["uri"]


def test_a_name_the_finishing_attempt_wrote_points_at_its_own_object(db, worker_factory):
    _park_after_writing(db, worker_factory)

    assert _resume(db, worker_factory, write="the final notes\n") == ExitCode.OK

    entry = _entry(db, "notes.md")
    assert "carried_from" not in entry
    assert "/attempts/att_2/artifacts/notes.md" in entry["uri"]
    names = [e["name"] for e in db.doc("tasks/task_1")["result_summary"]["artifacts"]]
    assert names.count("notes.md") == 1


def test_a_carried_reference_outside_the_task_is_not_carried(db, worker_factory):
    """The event's detail is a document the tenant's agents can reach through
    nothing, but it is read as data all the same: a reference that is not an
    artifact of the parked attempt itself, of THIS task, is dropped."""
    _park_after_writing(db, worker_factory)
    parked = next(
        path for path, doc in db.documents.items()
        if path.startswith("tasks/task_1/events/") and doc["type"] == EventType.PARKED.value
    )
    db.documents[parked]["detail"]["artifacts"][0]["uri"] = (
        f"gs://bucket/tenants/{TENANT}/tasks/task_other/attempts/att_9/artifacts/notes.md"
    )

    assert _resume(db, worker_factory, write=None) == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["result_summary"]["expected_outputs_missing"] == ["notes.md"]
