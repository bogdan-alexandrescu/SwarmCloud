"""`input_from`: a step receiving the artifact it was promised.

A workflow step may declare `input_from = {upstream_step: filename}`. The API
validates it against the DAG and the service records it on the task as
`metadata.input_from = {upstream_TASK_id: filename}`. These tests cover the half
that runs in the worker: the file arriving in the agent's working directory, and
-- at greater length, because it matters more -- every way a promised input can
be absent and what the attempt does about it.

The happy path is proved from the CHECKPOINT rather than from the live
workspace. The workspace is destroyed when the attempt ends, so asserting on it
afterwards is impossible; the final checkpoint is an archive of `work/` taken
while the agent was running, which is the same claim ("the file was in the
agent's current directory") made against evidence that outlives the run.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest

from agent_worker import inputs as inputs_mod
from agent_worker.errors import ExitCode, InputUnavailable
from agent_worker.logs import build_logger
from agent_worker.objectstore import LocalObjectStore
from swarm_common.states import TaskState

from conftest import TENANT, seed_attempt

UPSTREAM_TEXT = "the upstream step's finding\n"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def run_upstream(
    db: Any,
    worker_factory: Any,
    *,
    task_id: str = "task_up",
    attempt_id: str = "att_up",
    lease_id: str = "lease_up",
    artifact_name: str = "summary.md",
    artifact_text: str = UPSTREAM_TEXT,
) -> None:
    """Run a real attempt that succeeds and uploads one artifact."""
    seed_attempt(
        db,
        task_id=task_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        task_input={
            "prompt": "produce the thing the next step needs",
            "steps": 1,
            "sleep_seconds": 0.01,
            "artifact_name": artifact_name,
            "artifact_text": artifact_text,
        },
    )
    worker, _config, _exporter = worker_factory(
        task_id=task_id, attempt_id=attempt_id, lease_id=lease_id
    )
    assert worker.run() == ExitCode.OK
    assert db.doc(f"tasks/{task_id}")["state"] == TaskState.SUCCEEDED.value


def seed_downstream(db: Any, declaration: dict[str, str], **overrides: Any) -> None:
    seed_attempt(
        db,
        task_id="task_2",
        attempt_id="att_2",
        lease_id="lease_2",
        task_input={"prompt": "use it", "steps": 1, "sleep_seconds": 0.01},
        **overrides,
    )
    db.doc("tasks/task_2")["metadata"] = {"input_from": dict(declaration)}


def run_downstream(worker_factory: Any) -> int:
    worker, _config, _exporter = worker_factory(
        task_id="task_2", attempt_id="att_2", lease_id="lease_2"
    )
    return worker.run()


def final_checkpoint_files(store: LocalObjectStore, task_id: str, attempt_id: str) -> tarfile.TarFile:
    """The archive of `work/` taken while the agent was running."""
    keys = [
        key
        for key in store.list_keys(f"tenants/{TENANT}/tasks/{task_id}/attempts/{attempt_id}/")
        if key.endswith("/archive.tar.gz")
    ]
    assert keys, "the attempt wrote no checkpoint"
    return tarfile.open(fileobj=io.BytesIO(store.download_bytes(sorted(keys)[-1])), mode="r:gz")


def logger_for(stream: io.StringIO) -> Any:
    return build_logger(
        task_id="task_2",
        attempt_id="att_2",
        tenant_id=TENANT,
        generation=1,
        runner_profile="mock",
        stream=stream,
    )


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


def test_declared_artifact_reaches_the_agents_working_directory(db, store, worker_factory):
    run_upstream(db, worker_factory)
    seed_downstream(db, {"task_up": "summary.md"})

    assert run_downstream(worker_factory) == ExitCode.OK

    task = db.doc("tasks/task_2")
    assert task["state"] == TaskState.SUCCEEDED.value
    staged = task["result_summary"]["staged_inputs"]
    assert staged == [
        {
            "task_id": "task_up",
            "filename": "summary.md",
            "path": "summary.md",
            "bytes": len(UPSTREAM_TEXT.encode("utf-8")),
            "uri": store.uri(
                f"tenants/{TENANT}/tasks/task_up/attempts/att_up/artifacts/summary.md"
            ),
        }
    ]

    # The file itself, with the upstream's bytes, in the directory the agent ran
    # in -- and `input.json` telling the agent it was there.
    with final_checkpoint_files(store, "task_2", "att_2") as archive:
        member = archive.extractfile("summary.md")
        assert member is not None
        assert member.read().decode("utf-8") == UPSTREAM_TEXT
        payload = json.loads(archive.extractfile("input.json").read())
    assert payload["staged_inputs"][0]["filename"] == "summary.md"
    assert payload["staged_inputs"][0]["path"] == "summary.md"

    # Capacity still came back.
    assert db.doc("leases/lease_2")["released_at"] is not None
    assert db.doc("pools/global")["active"] == 0


def test_a_task_that_declares_nothing_behaves_exactly_as_before(db, worker_factory):
    """Backwards compatibility, stated as a test rather than as a comment."""
    seed_attempt(db, task_input={"prompt": "ordinary", "steps": 1, "sleep_seconds": 0.01})
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.OK
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert "staged_inputs" not in task["result_summary"]


def test_a_nested_artifact_name_keeps_its_directory(db, store, worker_factory, tmp_path, log_stream):
    """`_upload_outputs` walks `artifacts/` recursively, so a name may nest."""
    work = tmp_path / "work"
    work.mkdir()
    key = f"tenants/{TENANT}/tasks/task_up/attempts/att_up/artifacts/reports/out.json"
    store.upload_bytes(key, b'{"ok": true}')
    db.seed(
        "tasks/task_up",
        {
            "id": "task_up",
            "tenant_id": TENANT,
            "state": TaskState.SUCCEEDED.value,
            "result_summary": {
                "artifacts": [{"name": "reports/out.json", "bytes": 12, "uri": store.uri(key)}]
            },
        },
    )

    staged = inputs_mod.stage_inputs(
        inputs_mod.declared_inputs({"input_from": {"task_up": "reports/out.json"}}),
        work=work,
        store=store,
        db=db,
        tenant_id=TENANT,
        logger=logger_for(log_stream),
        resumed=False,
        max_total_bytes=1024,
        reserved=frozenset({"repo"}),
    )

    assert staged[0].path == "reports/out.json"
    assert (work / "reports" / "out.json").read_bytes() == b'{"ok": true}'


# ---------------------------------------------------------------------------
# a promised input that is not there
# ---------------------------------------------------------------------------


def test_a_missing_artifact_fails_the_attempt_and_never_starts_the_agent(
    db, store, worker_factory
):
    run_upstream(db, worker_factory, artifact_name="something-else.txt")
    seed_downstream(db, {"task_up": "summary.md"})

    assert run_downstream(worker_factory) == ExitCode.FAILED

    task = db.doc("tasks/task_2")
    assert task["state"] == TaskState.FAILED.value
    # The two strings a person needs in order to fix it.
    assert "task_up" in task["last_error"]
    assert "summary.md" in task["last_error"]
    # And what the upstream DID upload, so the fix is one edit away.
    assert "something-else.txt" in task["last_error"]

    # The agent was never started: no runner result, and nothing of its own in
    # the bucket beyond the logs the worker itself wrote.
    assert "runner" not in task["result_summary"]
    assert not [
        key
        for key in store.list_keys(f"tenants/{TENANT}/tasks/task_2/attempts/att_2/")
        if "/artifacts/" in key
    ]
    # The slot came back. A refusal must not strand capacity.
    assert db.doc("leases/lease_2")["released_at"] is not None
    assert db.doc("pools/global")["active"] == 0


def test_an_upstream_that_did_not_succeed_is_refused_by_name(db, worker_factory):
    run_upstream(db, worker_factory)
    db.doc("tasks/task_up")["state"] = TaskState.FAILED.value
    seed_downstream(db, {"task_up": "summary.md"})

    assert run_downstream(worker_factory) == ExitCode.FAILED

    error = db.doc("tasks/task_2")["last_error"]
    assert "task_up" in error and "summary.md" in error
    assert TaskState.FAILED.value in error


def test_an_artifact_skipped_for_size_says_so_rather_than_not_found(db, worker_factory):
    run_upstream(db, worker_factory, artifact_name="small.txt")
    db.doc("tasks/task_up")["result_summary"]["artifacts_skipped"] = ["huge.bin"]
    seed_downstream(db, {"task_up": "huge.bin"})

    assert run_downstream(worker_factory) == ExitCode.FAILED

    error = db.doc("tasks/task_2")["last_error"]
    assert "huge.bin" in error and "artifact size cap" in error


def test_an_upstream_task_with_no_document_is_refused(db, worker_factory):
    seed_downstream(db, {"task_gone": "summary.md"})

    assert run_downstream(worker_factory) == ExitCode.FAILED
    assert "task_gone" in db.doc("tasks/task_2")["last_error"]


# ---------------------------------------------------------------------------
# tenancy and unsafe names
# ---------------------------------------------------------------------------


def test_another_tenants_task_is_refused_but_this_attempt_still_finishes(db, worker_factory):
    """Invariant 9, and the distinction from a TENANT_MISMATCH exit.

    A foreign tenant id in OUR metadata is not "this attempt's documents belong
    to someone else" -- our documents are fine. So the attempt fails in the
    ordinary way, writing its own terminal state and giving the slot back,
    rather than exiting 79 having written nothing.
    """
    db.seed(
        "tasks/task_other",
        {
            "id": "task_other",
            "tenant_id": "another-tenant",
            "state": TaskState.SUCCEEDED.value,
            "result_summary": {"artifacts": [{"name": "secret.txt", "bytes": 3, "uri": "gs://b/x"}]},
        },
    )
    seed_downstream(db, {"task_other": "secret.txt"})

    assert run_downstream(worker_factory) == ExitCode.FAILED

    task = db.doc("tasks/task_2")
    assert task["state"] == TaskState.FAILED.value
    assert "another-tenant" in task["last_error"]
    assert db.doc("leases/lease_2")["released_at"] is not None


def test_an_artifact_recorded_outside_the_tenant_prefix_is_refused(db, worker_factory):
    """The URI is the only caller-influenced part of the object key."""
    run_upstream(db, worker_factory)
    db.doc("tasks/task_up")["result_summary"]["artifacts"] = [
        {
            "name": "summary.md",
            "bytes": 4,
            "uri": "gs://bucket/tenants/another-tenant/tasks/task_up/attempts/a/artifacts/summary.md",
        }
    ]
    seed_downstream(db, {"task_up": "summary.md"})

    assert run_downstream(worker_factory) == ExitCode.FAILED
    assert "refusing to read it" in db.doc("tasks/task_2")["last_error"]


@pytest.mark.parametrize(
    "filename",
    ["../escape.txt", "/etc/passwd", "a/../../escape.txt", "nested/../../x", "\\evil"],
)
def test_an_unsafe_artifact_name_never_reaches_the_filesystem(tmp_path: Path, filename: str):
    with pytest.raises(InputUnavailable):
        inputs_mod.destination_for(tmp_path, filename, reserved=frozenset())


@pytest.mark.parametrize(
    "filename", ["repo", ".swarm", "input.json", "result.json", "quota.json", "credential.json"]
)
def test_a_name_the_worker_owns_is_refused(tmp_path: Path, filename: str):
    reserved = frozenset(
        {"repo", ".swarm", "input.json", "result.json", "quota.json", "credential.json"}
    )
    with pytest.raises(InputUnavailable) as raised:
        inputs_mod.destination_for(tmp_path, filename, reserved=reserved)
    assert "the worker owns" in str(raised.value)


def test_a_reserved_name_fails_the_attempt_rather_than_clobbering_input_json(db, worker_factory):
    run_upstream(db, worker_factory, artifact_name="input.json")
    seed_downstream(db, {"task_up": "input.json"})

    assert run_downstream(worker_factory) == ExitCode.FAILED
    assert "input.json" in db.doc("tasks/task_2")["last_error"]


# ---------------------------------------------------------------------------
# reading the declaration itself
# ---------------------------------------------------------------------------


def test_no_declaration_reads_nothing():
    assert inputs_mod.declared_inputs(None) == []
    assert inputs_mod.declared_inputs({}) == []
    assert inputs_mod.declared_inputs({"origin": "ui"}) == []
    assert inputs_mod.declared_inputs({"input_from": {}}) == []
    # Metadata that is not a mapping cannot be carrying a declaration, so there
    # is nothing to honour -- and a task that runs today must not start failing
    # because of the shape of a field this feature is not used by.
    assert inputs_mod.declared_inputs(["nonsense"]) == []
    assert inputs_mod.declared_inputs("nonsense") == []


@pytest.mark.parametrize(
    "metadata",
    [
        {"input_from": "summary.md"},
        {"input_from": ["summary.md"]},
        {"input_from": {"task_a": None}},
        {"input_from": {"task_a": ""}},
        {"input_from": {"task_a": 7}},
        {"input_from": {"": "summary.md"}},
    ],
)
def test_a_malformed_declaration_is_refused_rather_than_ignored(metadata: dict[str, Any]):
    """Inert-on-the-dispatch-that-asked-for-it is the failure mode, not the fix."""
    with pytest.raises(InputUnavailable):
        inputs_mod.declared_inputs(metadata)


def test_two_upstreams_staging_one_filename_are_refused():
    with pytest.raises(InputUnavailable) as raised:
        inputs_mod.declared_inputs(
            {"input_from": {"task_a": "patch.diff", "task_b": "patch.diff"}}
        )
    message = str(raised.value)
    assert "task_a" in message and "task_b" in message and "patch.diff" in message


def test_declarations_are_staged_in_a_stable_order():
    declared = inputs_mod.declared_inputs(
        {"input_from": {"task_c": "c.txt", "task_a": "a.txt", "task_b": "b.txt"}}
    )
    assert [item.upstream_task_id for item in declared] == ["task_a", "task_b", "task_c"]


# ---------------------------------------------------------------------------
# resume, and the size bound
# ---------------------------------------------------------------------------


def test_a_resumed_attempt_keeps_what_the_checkpoint_carried(db, store, tmp_path, log_stream):
    """It must not re-fetch: the agent may have EDITED the staged file.

    `db` here holds no upstream task at all, so a fetch would raise. Passing it
    anyway is the assertion: nothing is read when the file is already present.
    """
    work = tmp_path / "work"
    work.mkdir()
    (work / "summary.md").write_text("edited by the agent before it was interrupted\n")

    staged = inputs_mod.stage_inputs(
        inputs_mod.declared_inputs({"input_from": {"task_up": "summary.md"}}),
        work=work,
        store=store,
        db=db,
        tenant_id=TENANT,
        logger=logger_for(log_stream),
        resumed=True,
        max_total_bytes=1024,
        reserved=frozenset({"repo"}),
    )

    assert len(staged) == 1
    assert staged[0].from_checkpoint is True
    assert staged[0].as_dict()["from_checkpoint"] is True
    assert (work / "summary.md").read_text().startswith("edited by the agent")


def test_a_first_attempt_does_not_treat_a_stray_file_as_a_staged_input(
    db, store, tmp_path, log_stream
):
    """`resumed=False` must still fetch, even if a file of that name exists."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "summary.md").write_text("stale\n")
    key = f"tenants/{TENANT}/tasks/task_up/attempts/att_up/artifacts/summary.md"
    store.upload_bytes(key, UPSTREAM_TEXT.encode("utf-8"))
    db.seed(
        "tasks/task_up",
        {
            "id": "task_up",
            "tenant_id": TENANT,
            "state": TaskState.SUCCEEDED.value,
            "result_summary": {
                "artifacts": [
                    {"name": "summary.md", "bytes": len(UPSTREAM_TEXT), "uri": store.uri(key)}
                ]
            },
        },
    )

    staged = inputs_mod.stage_inputs(
        inputs_mod.declared_inputs({"input_from": {"task_up": "summary.md"}}),
        work=work,
        store=store,
        db=db,
        tenant_id=TENANT,
        logger=logger_for(log_stream),
        resumed=False,
        max_total_bytes=1024,
        reserved=frozenset({"repo"}),
    )

    assert staged[0].from_checkpoint is False
    assert (work / "summary.md").read_text() == UPSTREAM_TEXT


def test_inputs_larger_than_one_attempt_may_hold_are_refused_before_any_download(
    db, store, tmp_path, log_stream
):
    work = tmp_path / "work"
    work.mkdir()
    for index in (1, 2):
        key = f"tenants/{TENANT}/tasks/task_{index}/attempts/att/artifacts/big-{index}.bin"
        store.upload_bytes(key, b"x" * 10)
        db.seed(
            f"tasks/task_{index}",
            {
                "id": f"task_{index}",
                "tenant_id": TENANT,
                "state": TaskState.SUCCEEDED.value,
                "result_summary": {
                    "artifacts": [
                        {"name": f"big-{index}.bin", "bytes": 600, "uri": store.uri(key)}
                    ]
                },
            },
        )

    with pytest.raises(InputUnavailable) as raised:
        inputs_mod.stage_inputs(
            inputs_mod.declared_inputs(
                {"input_from": {"task_1": "big-1.bin", "task_2": "big-2.bin"}}
            ),
            work=work,
            store=store,
            db=db,
            tenant_id=TENANT,
            logger=logger_for(log_stream),
            resumed=False,
            max_total_bytes=1000,
            reserved=frozenset({"repo"}),
        )

    assert "1200 bytes" in str(raised.value)
    # Nothing moved: the refusal happens before the first download.
    assert not list(work.iterdir())


def test_one_missing_input_fails_before_its_siblings_are_downloaded(
    db, store, tmp_path, log_stream
):
    work = tmp_path / "work"
    work.mkdir()
    key = f"tenants/{TENANT}/tasks/task_1/attempts/att/artifacts/present.txt"
    store.upload_bytes(key, b"here")
    db.seed(
        "tasks/task_1",
        {
            "id": "task_1",
            "tenant_id": TENANT,
            "state": TaskState.SUCCEEDED.value,
            "result_summary": {
                "artifacts": [{"name": "present.txt", "bytes": 4, "uri": store.uri(key)}]
            },
        },
    )
    db.seed(
        "tasks/task_2",
        {
            "id": "task_2",
            "tenant_id": TENANT,
            "state": TaskState.SUCCEEDED.value,
            "result_summary": {"artifacts": []},
        },
    )

    with pytest.raises(InputUnavailable):
        inputs_mod.stage_inputs(
            inputs_mod.declared_inputs(
                {"input_from": {"task_1": "present.txt", "task_2": "absent.txt"}}
            ),
            work=work,
            store=store,
            db=db,
            tenant_id=TENANT,
            logger=logger_for(log_stream),
            resumed=False,
            max_total_bytes=1024,
            reserved=frozenset({"repo"}),
        )

    assert not list(work.iterdir())
