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

EVERY TEST HERE RUNS AGAINST A FIRESTORE THAT REFUSES LIST AND QUERY (#166,
reopened 2026-10-06). The tenant worker role (`swarmTenantWorkerFirestore`,
terraform/bootstrap/platform_roles.tf) grants get, create and update by id and
drops `datastore.entities.list` on purpose. The first carry read the parks off
a query on the task's events: green here, a 403 on every attempt in dev, and
nothing was ever carried. A regression to any list or query fails these tests.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from google.api_core.exceptions import PermissionDenied

from agent_worker.control import ControlPlane
from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger
from swarm_common.states import EventType, TaskState

from worker_seeds import TENANT, seed_attempt
from fakes import FakeDocumentRef, FakeFirestore, FakeQuery, FakeTransactionRunner


@pytest.fixture(autouse=True)
def _the_worker_role_cannot_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """A query is what the tenant worker role cannot make: refuse it as dev does."""

    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionDenied(
            "Missing or insufficient permissions: datastore.entities.list"
        )

    monkeypatch.setattr(FakeQuery, "where", refused)
    monkeypatch.setattr(FakeQuery, "stream", refused)
    monkeypatch.setattr(FakeQuery, "limit", refused)


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every document path read, by a plain get or through a transaction."""
    seen: list[str] = []
    plain_get = FakeDocumentRef.get

    def recorded(self: FakeDocumentRef, *args: Any, **kwargs: Any) -> Any:
        seen.append(self.path)
        return plain_get(self, *args, **kwargs)

    monkeypatch.setattr(FakeDocumentRef, "get", recorded)
    # `FakeTransaction.get` reads through `ref.get()`, so it is recorded too.
    return seen

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
    """The carry record is a document the tenant's agents can reach through
    nothing, but it is read as data all the same: a reference that is not an
    artifact of the parked attempt itself, of THIS task, is dropped."""
    _park_after_writing(db, worker_factory)
    db.documents["tasks/task_1/carry/att_1"]["artifacts"][0]["uri"] = (
        f"gs://bucket/tenants/{TENANT}/tasks/task_other/attempts/att_9/artifacts/notes.md"
    )

    assert _resume(db, worker_factory, write=None) == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["result_summary"]["expected_outputs_missing"] == ["notes.md"]


def _park_with(
    db: Any, worker_factory: Any, *, attempt: int, files: dict[str, str], expected: list[str]
) -> None:
    """Attempt `attempt`: its agent writes `files`, meets a 429, and the attempt parks.

    `attempt_count` stays 1: a park does not spend an attempt, and the mock
    parks on `quota_exhausted` only while the task is on its first.
    """
    attempt_id, lease_id = f"att_{attempt}", f"lease_{attempt}"
    seed_attempt(
        db,
        attempt_id=attempt_id,
        lease_id=lease_id,
        task_input={
            "prompt": "write the notes",
            "steps": 1,
            "sleep_seconds": 0.01,
            "quota_exhausted": True,
            "retry_after_seconds": 1800,
        },
        simulated={"provider": "anthropic"},
    )
    db.doc("tasks/task_1")["metadata"] = {"expected_outputs": expected}
    worker, _, _ = worker_factory(attempt_id=attempt_id, lease_id=lease_id)
    upload = worker._upload_outputs

    def written_then_upload(**kwargs: Any) -> dict[str, Any]:
        for name, text in files.items():
            (worker.ws.artifacts / name).write_text(text)
        return upload(**kwargs)

    worker._upload_outputs = written_then_upload  # type: ignore[method-assign]
    assert worker.run() == ExitCode.PARKED


def test_every_earlier_park_is_carried_and_the_later_park_wins(db, store, worker_factory):
    """EARLIER PARKED ATTEMPTS', plural: not only the most recent park. A name
    two parks uploaded points at the later park's object, which holds what the
    agent wrote last; a name only the first park uploaded is still listed."""
    expected = ["notes.md", "plan.md"]
    _park_with(
        db, worker_factory, attempt=1, expected=expected,
        files={"plan.md": "the plan\n", "notes.md": "first notes\n"},
    )
    _park_with(
        db, worker_factory, attempt=2, expected=expected,
        files={"notes.md": "the later notes\n"},
    )
    seed_attempt(
        db, attempt_id="att_3", lease_id="lease_3", attempt_count=2,
        task_input={"prompt": "finish", "steps": 1, "sleep_seconds": 0.01},
    )
    db.doc("tasks/task_1")["metadata"] = {"expected_outputs": expected}
    worker, _, _ = worker_factory(attempt_id="att_3", lease_id="lease_3")

    assert worker.run() == ExitCode.OK
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    assert _entry(db, "plan.md")["carried_from"] == "att_1"
    notes = _entry(db, "notes.md")
    assert notes["carried_from"] == "att_2"
    assert "/attempts/att_2/artifacts/notes.md" in notes["uri"]
    assert notes["bytes"] == len("the later notes\n")
    names = [e["name"] for e in task["result_summary"]["artifacts"]]
    assert names.count("notes.md") == 1
    assert "expected_outputs_missing" not in task["result_summary"]


def test_a_parked_upload_no_longer_in_the_bucket_is_not_carried(db, store, worker_factory):
    """A reference is listed only while its object exists: a dependant could
    not stage a deleted one, so the check reports the name missing instead of
    passing on a reference to nothing."""
    _park_after_writing(db, worker_factory)
    store.delete(f"tenants/{TENANT}/tasks/task_1/attempts/att_1/artifacts/notes.md")

    assert _resume(db, worker_factory, write=None) == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["result_summary"]["expected_outputs_missing"] == ["notes.md"]
    assert not any(
        e.get("carried_from") for e in task["result_summary"]["artifacts"]
        if isinstance(e, dict)
    )


def test_a_park_records_its_uploads_where_the_next_attempt_gets_them_by_id(db, worker_factory):
    """The park writes `tasks/{task}/carry/{attempt}` and lists the attempt on
    `tasks/{task}/carry/parks`: two known ids under the task's own document,
    which the finishing attempt reads with a plain get."""
    _park_after_writing(db, worker_factory)

    record = db.doc("tasks/task_1/carry/att_1")
    assert record["tenant_id"] == TENANT
    assert record["task_id"] == "task_1"
    assert record["attempt_id"] == "att_1"
    assert [a["name"] for a in record["artifacts"]] == ["notes.md"]
    assert "/attempts/att_1/artifacts/notes.md" in record["artifacts"][0]["uri"]
    index = db.doc("tasks/task_1/carry/parks")
    assert index["tenant_id"] == TENANT
    assert [p["attempt_id"] for p in index["attempts"]] == ["att_1"]


def test_the_carry_is_read_by_id_and_never_from_another_task(db, worker_factory, reads):
    """The finishing attempt reads the index and the parked attempt's record
    by id, and nothing under another task -- not even a task whose carry
    record names the same attempt id and the same file."""
    _park_after_writing(db, worker_factory)
    for path in ("tasks/task_other/carry/parks", "tasks/task_other/carry/att_1"):
        db.seed(path, {**db.doc(path.replace("task_other", "task_1")), "task_id": "task_other"})
    reads.clear()

    assert _resume(db, worker_factory, write=None) == ExitCode.OK

    assert "tasks/task_1/carry/parks" in reads
    assert "tasks/task_1/carry/att_1" in reads
    assert not [path for path in reads if path.startswith("tasks/task_other")]
    assert _entry(db, "notes.md")["carried_from"] == "att_1"


def test_a_carry_record_naming_another_task_is_not_carried(db, worker_factory):
    """A record at this task's path that says it belongs to another task, or
    to another attempt than its id, is not this task's park: refused."""
    _park_after_writing(db, worker_factory)
    db.documents["tasks/task_1/carry/att_1"]["task_id"] = "task_other"

    assert _resume(db, worker_factory, write=None) == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["result_summary"]["expected_outputs_missing"] == ["notes.md"]


def test_a_carry_record_of_another_tenant_is_not_carried(db, worker_factory):
    _park_after_writing(db, worker_factory)
    db.documents["tasks/task_1/carry/att_1"]["tenant_id"] = "tenant_other"

    assert _resume(db, worker_factory, write=None) == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["result_summary"]["expected_outputs_missing"] == ["notes.md"]


def test_a_fenced_park_records_nothing_to_carry(db, worker_factory):
    """The record is written in the park's own fenced transaction (invariant
    5): a park refused because a newer generation owns the task leaves no
    record for that generation's finishing attempt to carry."""
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
    worker, _, _ = worker_factory(attempt_id="att_1", lease_id="lease_1")
    upload = worker._upload_outputs

    def written_then_fenced(**kwargs: Any) -> dict[str, Any]:
        (worker.ws.artifacts / "notes.md").write_text(PARKED_TEXT)
        summary = upload(**kwargs)
        db.doc("tasks/task_1")["current_generation"] = 99
        return summary

    worker._upload_outputs = written_then_fenced  # type: ignore[method-assign]
    assert worker.run() == ExitCode.GENERATION_FENCED

    assert not [path for path in db.documents if path.startswith("tasks/task_1/carry/")]


def _control(db: FakeFirestore, attempt_id: str) -> ControlPlane:
    lease_id = attempt_id.replace("att_", "lease_")
    return ControlPlane(
        db,
        task_id="task_1",
        attempt_id=attempt_id,
        lease_id=lease_id,
        tenant_id=TENANT,
        generation=1,
        logger=build_logger(
            task_id="task_1", attempt_id=attempt_id, tenant_id=TENANT,
            generation=1, runner_profile="mock", stream=io.StringIO(),
        ),
        txn_runner=FakeTransactionRunner(db),
    )


def test_an_await_park_records_its_uploads_for_the_attempt_that_finishes(db):
    """The await park (child tasks) is a park too, in its own transaction:
    what the parent uploaded before it waited is carried like any park's."""
    seed_attempt(db, state=TaskState.RUNNING)
    uri = f"gs://bucket/tenants/{TENANT}/tasks/task_1/attempts/att_1/artifacts/plan.md"
    _control(db, "att_1").park_awaiting_children(
        max_resumes=3, uploads=[{"name": "plan.md", "bytes": 9, "uri": uri}]
    )
    seed_attempt(db, attempt_id="att_2", lease_id="lease_2", state=TaskState.RUNNING)

    parks = _control(db, "att_2").parked_uploads()

    assert parks == [("att_1", [{"name": "plan.md", "bytes": 9, "uri": uri}])]


def test_a_park_without_uploads_records_nothing(db):
    seed_attempt(db, state=TaskState.RUNNING)
    _control(db, "att_1").park_awaiting_children(max_resumes=3)

    assert not [path for path in db.documents if path.startswith("tasks/task_1/carry/")]
    assert _control(db, "att_2").parked_uploads() == []
