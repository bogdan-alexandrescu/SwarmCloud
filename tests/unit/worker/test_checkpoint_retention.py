"""Checkpoint retention is decided by REFERENCE, not by a clock.

Before `reconciler/checkpoints.py` existed, the only thing that deleted a
checkpoint was the artifact bucket's own lifecycle rule -- `age =
artifact_retention_days`, 14 days in `terraform/environments/dev/dev.tfvars`.
That rule cannot see a task, a lease or an attempt, so it treats a task PARKED
on a provider quota window exactly like a task that succeeded last fortnight.
The first is an attempt whose entire value is its checkpoint; the second is
litter.

Every checkpoint in these tests is written by the production
`agent_worker.checkpoint.CheckpointManager` into a real `LocalObjectStore`, and
every decision is made by the production `reconciler.checkpoints` code reading a
real `ControlStore` over `FakeFirestore`. Nothing here asserts that a function
was called.
"""

from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from fakes import FakeFirestore, FakeTransactionRunner

from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager, checkpoint_prefix
from agent_worker.objectstore import LocalObjectStore
from reconciler.checkpoints import (
    CheckpointCollector,
    Disposition,
    classify,
    manifest_age_seconds,
    parse_checkpoint_key,
    pointer_to_prefix,
)
from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger
from reconciler.model import AttemptView, TaskView
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import utcnow
from swarm_common.states import TaskState

TENANT = "eng"
BUCKET = "saga-agents-staging-swarm-artifacts"

#: What the bucket lifecycle rule would have deleted by now. Any checkpoint
#: older than this in dev was gone, whatever the control plane thought.
OLD_LIFECYCLE_WINDOW = timedelta(days=14)


@pytest.fixture
def db_and_objects(tmp_path: Path):
    """A document store, an object store and somewhere to build workspaces."""
    return FakeFirestore(), LocalObjectStore(tmp_path / "gcs", bucket=BUCKET), tmp_path


@pytest.fixture
def log():
    return build_logger(stream=io.StringIO())


@pytest.fixture
def config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="saga-agents-staging",
        region="us-central1",
        firestore_database="swarm",
        artifact_bucket=BUCKET,
        enable_gke=False,
        enable_cloud_run=False,
    )


# ---------------------------------------------------------------------------
# Helpers that produce REAL checkpoints
# ---------------------------------------------------------------------------

def write_checkpoints(
    objects: LocalObjectStore,
    tmp_path: Path,
    log,
    *,
    task_id: str,
    attempt_id: str,
    tenant_id: str = TENANT,
    age: timedelta | None = None,
    count: int = 1,
):
    """`count` committed checkpoints, written by the production manager.

    Periodic checkpointing means one attempt leaves a SERIES behind, not a
    single object, and `count > 1` is what makes a test able to tell "the
    pointer target survived" from "all of them survived".

    `age` backdates each manifest's `created_at` in place -- the only field the
    orphan backstop reads -- so a test can put a checkpoint a month in the past
    without waiting a month or stubbing a clock into the worker.
    """
    ws = workspace_mod.create(tmp_path / f"ws-{task_id}-{attempt_id}", attempt_id)
    manager = CheckpointManager(
        store=objects,
        tenant_id=tenant_id,
        task_id=task_id,
        attempt_id=attempt_id,
        generation=1,
        logger=log,
    )
    records = []
    for step in range(count):
        (ws.work / "state.json").write_text(f"work through step {step}")
        record = manager.create(ws)
        if age is not None:
            stamp = (utcnow() - age).isoformat().replace("+00:00", "Z")
            manifest = json.loads(objects.download_bytes(record.manifest_key).decode("utf-8"))
            manifest["created_at"] = stamp
            objects.upload_bytes(record.manifest_key, json.dumps(manifest).encode("utf-8"))
        records.append(record)
    return records


def write_checkpoint(objects, tmp_path, log, **kwargs):
    return write_checkpoints(objects, tmp_path, log, **kwargs)[0]


def seed_task(
    db: FakeFirestore,
    *,
    task_id: str,
    state: TaskState,
    latest_checkpoint: str | None = None,
    tenant_id: str = TENANT,
) -> None:
    db.seed(
        f"tasks/{task_id}",
        {
            "id": task_id,
            "tenant_id": tenant_id,
            "state": state.value,
            "runner_profile": "mock",
            "resource_class": "standard",
            "current_generation": 1,
            "current_lease_id": None,
            "attempt_count": 1,
            "max_attempts": 3,
            "updated_at": utcnow(),
            "latest_checkpoint": latest_checkpoint,
        },
    )


def seed_attempt_doc(
    db: FakeFirestore,
    *,
    task_id: str,
    attempt_id: str,
    completed: bool,
    tenant_id: str = TENANT,
) -> None:
    db.seed(
        f"attempts/{attempt_id}",
        {
            "attempt_id": attempt_id,
            "task_id": task_id,
            "tenant_id": tenant_id,
            "generation": 1,
            "lease_id": f"lease-{attempt_id}",
            "backend": "CLOUD_RUN_JOB",
            "created_at": utcnow() - timedelta(hours=2),
            "started_at": utcnow() - timedelta(hours=2),
            "completed_at": utcnow() - timedelta(hours=1) if completed else None,
        },
    )


def collector(db: FakeFirestore, objects: LocalObjectStore, config: ReconcilerConfig, log):
    return CheckpointCollector(
        reader=ControlStore(db, logger=log, txn_runner=FakeTransactionRunner(db)),
        objects=objects,
        config=config,
        logger=log,
    )


def checkpoint_dir(record) -> str:
    """The bucket-relative prefix the checkpoint's objects live under."""
    return record.archive_key.rsplit("/", 1)[0]


# ---------------------------------------------------------------------------
# The three proofs the lane asked for
# ---------------------------------------------------------------------------

def test_a_parked_attempts_checkpoint_survives_past_the_old_clock_window(
    db_and_objects, config, log
):
    """A task PARKED on a provider wait keeps EVERY checkpoint, at any age.

    This is the defect: the bucket rule deleted them at 14 days, and a provider
    quota window can outlast that. Here the checkpoints are a MONTH old -- older
    than the old lifecycle window and older than the orphan backstop -- and they
    all stay, including the two the pointer does not name, because
    `find_latest()` scans the whole task prefix and a resumed worker can select
    any of them.
    """
    db, objects, tmp_path = db_and_objects
    age = timedelta(days=30)
    assert age > OLD_LIFECYCLE_WINDOW
    assert age.total_seconds() > config.checkpoint_orphan_backstop_seconds

    records = write_checkpoints(
        objects, tmp_path, log, task_id="task_parked", attempt_id="att_1", age=age, count=3
    )
    seed_task(
        db,
        task_id="task_parked",
        state=TaskState.PARKED,
        latest_checkpoint=records[-1].uri,
    )
    # The attempt itself is over -- it checkpointed, parked and exited, exactly
    # as invariant 4 requires. Its task is what is still alive.
    seed_attempt_doc(db, task_id="task_parked", attempt_id="att_1", completed=True)

    report = collector(db, objects, config, log).sweep(now=utcnow())

    assert report.checkpoints_examined == 3
    assert report.reclaimed == 0
    for record in records:
        assert objects.exists(record.manifest_key)
        assert objects.exists(record.archive_key)


def test_a_terminal_attempt_of_a_terminal_task_is_collected(db_and_objects, config, log):
    """SUCCEEDED task, completed attempt, nothing pointing at it -> gone.

    And gone IMMEDIATELY: the checkpoint here is minutes old, far inside every
    clock in the system. Age is not what is being consulted.
    """
    db, objects, tmp_path = db_and_objects
    record = write_checkpoint(
        objects, tmp_path, log, task_id="task_done", attempt_id="att_1"
    )
    seed_task(db, task_id="task_done", state=TaskState.SUCCEEDED, latest_checkpoint=record.uri)
    seed_attempt_doc(db, task_id="task_done", attempt_id="att_1", completed=True)

    report = collector(db, objects, config, log).sweep(now=utcnow())

    assert report.reclaimed == 1
    assert not objects.exists(record.manifest_key)
    assert not objects.exists(record.archive_key)
    assert objects.list_keys(checkpoint_dir(record) + "/") == []


def test_an_orphan_with_no_referent_is_collected_only_by_the_backstop(
    db_and_objects, config, log
):
    """No task document at all: the clock, and only the clock, decides.

    Inside the window it is kept; past it, collected. Both halves matter -- a
    backstop that fires immediately is not a backstop, it is the rule.
    """
    db, objects, tmp_path = db_and_objects
    window = timedelta(seconds=config.checkpoint_orphan_backstop_seconds)

    young = write_checkpoint(
        objects, tmp_path, log, task_id="task_gone", attempt_id="att_1", age=window / 2
    )
    old = write_checkpoint(
        objects, tmp_path, log, task_id="task_gone", attempt_id="att_2",
        age=window + timedelta(days=1),
    )
    # Deliberately no tasks/task_gone document and no attempt documents.

    report = collector(db, objects, config, log).sweep(now=utcnow())

    assert objects.exists(young.manifest_key), "inside the window, the backstop must not fire"
    assert not objects.exists(old.manifest_key)
    assert not objects.exists(old.archive_key)
    assert report.reclaimed == 1
    assert report.kept == 1


# ---------------------------------------------------------------------------
# Reference rules
# ---------------------------------------------------------------------------

def test_one_live_attempt_protects_every_checkpoint_of_its_task(db_and_objects, config, log):
    """`find_latest()` scans the whole task prefix, so a resumed worker can pick
    ANY checkpoint of the task -- including one an earlier, completed attempt
    wrote. One unfinished attempt on a live task therefore protects all of them,
    at any age."""
    db, objects, tmp_path = db_and_objects
    first = write_checkpoint(
        objects, tmp_path, log, task_id="task_live", attempt_id="att_1",
        age=timedelta(days=40),
    )
    second = write_checkpoint(
        objects, tmp_path, log, task_id="task_live", attempt_id="att_2",
        age=timedelta(days=39),
    )
    seed_task(db, task_id="task_live", state=TaskState.RUNNING, latest_checkpoint=second.uri)
    seed_attempt_doc(db, task_id="task_live", attempt_id="att_1", completed=True)
    seed_attempt_doc(db, task_id="task_live", attempt_id="att_2", completed=False)

    report = collector(db, objects, config, log).sweep(now=utcnow())

    assert report.reclaimed == 0
    assert report.kept == 2
    assert objects.exists(first.manifest_key)
    assert objects.exists(second.manifest_key)


def test_an_attempt_that_never_recorded_completion_is_honoured_but_floored(
    db_and_objects, config, log
):
    """`ControlPlane.finish` writes the task's terminal state BEFORE calling
    `record_attempt_end`, so a worker killed between those two writes leaves an
    attempt that is permanently incomplete on a task that is permanently
    terminal. Honouring that reference for ever is the leak; the backstop is the
    floor under it.

    Found by dry-running this collector against saga-agents-staging: 19 real
    checkpoints across 6 tasks were in exactly this state.
    """
    db, objects, tmp_path = db_and_objects
    window = config.checkpoint_orphan_backstop_seconds

    record = write_checkpoint(
        objects, tmp_path, log, task_id="task_halfdone", attempt_id="att_1",
        age=timedelta(hours=6),
    )
    seed_task(db, task_id="task_halfdone", state=TaskState.SUCCEEDED)
    seed_attempt_doc(db, task_id="task_halfdone", attempt_id="att_1", completed=False)

    # Inside the window: the wind-down is honoured.
    report = collector(db, objects, config, log).sweep(now=utcnow())
    assert report.reclaimed == 0
    assert objects.exists(record.manifest_key)

    # Past it: no worker winds down for a week.
    report = collector(db, objects, config, log).sweep(
        now=utcnow() + timedelta(seconds=window + 60)
    )
    assert report.reclaimed == 1
    assert not objects.exists(record.manifest_key)


def test_a_failed_tasks_pointer_is_kept_and_its_superseded_checkpoints_are_not(
    db_and_objects, config, log
):
    """FAILED is terminal, but the frozen state machine allows FAILED -> READY.

    A retry resumes from `task.latest_checkpoint`, so that one checkpoint stays
    while the superseded ones go. SUCCEEDED and CANCELLED have no such
    transition, which is why `test_a_terminal_attempt...` collects the pointer
    target too.
    """
    db, objects, tmp_path = db_and_objects
    early, pointed_at = write_checkpoints(
        objects, tmp_path, log, task_id="task_failed", attempt_id="att_1", count=2
    )
    seed_task(
        db, task_id="task_failed", state=TaskState.FAILED, latest_checkpoint=pointed_at.uri
    )
    seed_attempt_doc(db, task_id="task_failed", attempt_id="att_1", completed=True)

    report = collector(db, objects, config, log).sweep(now=utcnow())

    assert report.reclaimed == 1
    assert not objects.exists(early.manifest_key)
    assert objects.exists(pointed_at.manifest_key)


def test_a_control_plane_read_that_fails_keeps_everything(db_and_objects, config, log):
    """"The task is gone" and "I could not look" must not delete the same thing.

    This is the shape this repository keeps producing: a guard whose failure is
    indistinguishable from the condition it checks.
    """
    db, objects, tmp_path = db_and_objects
    record = write_checkpoint(
        objects, tmp_path, log, task_id="task_unreadable", attempt_id="att_1",
        age=timedelta(days=60),
    )

    class ExplodingReader:
        def task_and_attempts(self, task_id: str):
            raise RuntimeError("firestore unavailable")

    sweeper = CheckpointCollector(
        reader=ExplodingReader(), objects=objects, config=config, logger=log
    )
    report = sweeper.sweep(now=utcnow())

    assert report.reclaimed == 0
    assert objects.exists(record.manifest_key)
    assert any("firestore unavailable" in e for e in report.errors)


def test_a_tenant_mismatch_between_object_and_task_is_never_collected(
    db_and_objects, config, log
):
    db, objects, tmp_path = db_and_objects
    record = write_checkpoint(
        objects, tmp_path, log, task_id="task_x", attempt_id="att_1", tenant_id="eng"
    )
    # The task document claims a different tenant than the object's own prefix.
    seed_task(db, task_id="task_x", state=TaskState.SUCCEEDED, tenant_id="finance")
    seed_attempt_doc(db, task_id="task_x", attempt_id="att_1", completed=True,
                     tenant_id="finance")

    report = collector(db, objects, config, log).sweep(now=utcnow())

    assert report.reclaimed == 0
    assert objects.exists(record.manifest_key)


def test_a_corrupt_manifest_holds_the_orphan_rather_than_dating_it(
    db_and_objects, config, log
):
    db, objects, tmp_path = db_and_objects
    record = write_checkpoint(
        objects, tmp_path, log, task_id="task_gone", attempt_id="att_1",
        age=timedelta(days=90),
    )
    objects.upload_bytes(record.manifest_key, b"{ this is not json")

    report = collector(db, objects, config, log).sweep(now=utcnow())

    assert report.reclaimed == 0
    assert objects.exists(record.archive_key)


def test_a_manifest_that_cannot_be_FETCHED_holds_the_orphan_too(
    db_and_objects, config, log
):
    """Distinct from a corrupt manifest, and it has its own branch: a transient
    503 or a permission change makes the read RAISE rather than return nonsense.
    Both must hold the object; neither may be read as "old enough"."""
    db, objects, tmp_path = db_and_objects
    record = write_checkpoint(
        objects, tmp_path, log, task_id="task_gone", attempt_id="att_1",
        age=timedelta(days=90),
    )

    class UnreadableStore:
        def list_keys(self, prefix):
            return objects.list_keys(prefix)

        def download_bytes(self, key):
            raise RuntimeError("503 backend error")

        def delete(self, key):
            raise AssertionError(f"deleted {key} without being able to date it")

    report = CheckpointCollector(
        reader=ControlStore(db, logger=log, txn_runner=FakeTransactionRunner(db)),
        objects=UnreadableStore(),
        config=config,
        logger=log,
    ).sweep(now=utcnow())

    assert report.reclaimed == 0
    assert objects.exists(record.manifest_key)
    assert [o.skipped for o in report.outcomes] == ["manifest unreadable: 503 backend error"]


def test_an_attempt_document_that_is_gone_falls_to_the_backstop_not_to_reclaim(
    db_and_objects, config, log
):
    """The task is terminal but the attempt record is missing, so "the attempt
    finished" cannot be proven. That is a leak, not a proof, and leaks are what
    the backstop is for."""
    db, objects, tmp_path = db_and_objects
    young = write_checkpoint(
        objects, tmp_path, log, task_id="task_noattempt", attempt_id="att_1",
        age=timedelta(minutes=5),
    )
    seed_task(db, task_id="task_noattempt", state=TaskState.SUCCEEDED)

    report = collector(db, objects, config, log).sweep(now=utcnow())

    assert report.reclaimed == 0
    assert objects.exists(young.manifest_key)

    # ... and past the window, the same object goes.
    stale = utcnow() + timedelta(seconds=config.checkpoint_orphan_backstop_seconds + 60)
    report = collector(db, objects, config, log).sweep(now=stale)
    assert report.reclaimed == 1
    assert not objects.exists(young.manifest_key)


# ---------------------------------------------------------------------------
# Mechanics
# ---------------------------------------------------------------------------

def test_artifacts_and_logs_of_the_same_attempt_are_never_touched(
    db_and_objects, config, log
):
    """The collector shares an attempt's prefix with its results and its logs.

    The nested artifact matters: it has the same number of path segments as a
    checkpoint object, so nothing but the `checkpoints` segment itself tells the
    two apart. An attempt's output surviving the collection of its checkpoint is
    the whole point -- the artifacts are what the tenant asked for.
    """
    db, objects, tmp_path = db_and_objects
    record = write_checkpoint(objects, tmp_path, log, task_id="task_done", attempt_id="att_1")
    flat = "tenants/eng/tasks/task_done/attempts/att_1/artifacts/result.json"
    nested = "tenants/eng/tasks/task_done/attempts/att_1/artifacts/report/summary.md"
    logfile = "tenants/eng/tasks/task_done/attempts/att_1/logs/runner/stdout.log"
    for key in (flat, nested, logfile):
        objects.upload_bytes(key, b"tenant output")
    seed_task(db, task_id="task_done", state=TaskState.SUCCEEDED)
    seed_attempt_doc(db, task_id="task_done", attempt_id="att_1", completed=True)

    report = collector(db, objects, config, log).sweep(now=utcnow())

    assert report.checkpoints_examined == 1, "an artifact must not be examined as a checkpoint"
    assert not objects.exists(record.manifest_key)
    for key in (flat, nested, logfile):
        assert objects.exists(key)


def test_the_manifest_is_deleted_before_the_archive(db_and_objects, config, log):
    """Reverse of the creation order. A delete that dies half way must leave an
    undiscoverable archive, never a manifest pointing at an archive that is no
    longer there -- the second turns a resume into a hard failure."""
    db, objects, tmp_path = db_and_objects
    record = write_checkpoint(objects, tmp_path, log, task_id="task_done", attempt_id="att_1")
    seed_task(db, task_id="task_done", state=TaskState.SUCCEEDED)
    seed_attempt_doc(db, task_id="task_done", attempt_id="att_1", completed=True)

    deleted: list[str] = []

    class RecordingStore:
        def list_keys(self, prefix):
            return objects.list_keys(prefix)

        def download_bytes(self, key):
            return objects.download_bytes(key)

        def delete(self, key):
            deleted.append(key)
            objects.delete(key)

    CheckpointCollector(
        reader=ControlStore(db, logger=log, txn_runner=FakeTransactionRunner(db)),
        objects=RecordingStore(),
        config=config,
        logger=log,
    ).sweep(now=utcnow())

    assert deleted[0] == record.manifest_key
    assert record.archive_key in deleted


def test_a_dry_run_deletes_nothing_and_still_reports_what_it_would_delete(
    db_and_objects, config, log
):
    """And its counters must not contradict its own outcome list.

    The first version of this reported `reclaimed: 0, kept: 142` beside 123
    reclaimable outcomes, which is a report an operator would read as "nothing
    to collect". Caught by running the collector against saga-agents-staging.
    """
    db, objects, tmp_path = db_and_objects
    records = write_checkpoints(
        objects, tmp_path, log, task_id="task_done", attempt_id="att_1", count=2
    )
    seed_task(db, task_id="task_done", state=TaskState.SUCCEEDED)
    seed_attempt_doc(db, task_id="task_done", attempt_id="att_1", completed=True)

    report = collector(db, objects, replace(config, dry_run=True), log).sweep(now=utcnow())

    for record in records:
        assert objects.exists(record.manifest_key)
        assert objects.exists(record.archive_key)
    assert report.reclaimed == 0
    assert report.reclaimable == 2
    assert report.kept == 0
    assert [o.skipped for o in report.outcomes] == ["dry_run", "dry_run"]


def test_a_bucket_that_cannot_be_listed_reports_an_error_and_sweeps_nothing(
    db_and_objects, config, log
):
    db, objects, _ = db_and_objects

    class BlindStore:
        def list_keys(self, prefix):
            raise RuntimeError("403 storage.objects.list")

        def download_bytes(self, key):  # pragma: no cover - never reached
            raise AssertionError("must not read after a failed listing")

        def delete(self, key):  # pragma: no cover - never reached
            raise AssertionError("must not delete after a failed listing")

    report = CheckpointCollector(
        reader=ControlStore(db, logger=log, txn_runner=FakeTransactionRunner(db)),
        objects=BlindStore(),
        config=config,
        logger=log,
    ).sweep(now=utcnow())

    assert report.ran is False
    assert report.reclaimed == 0
    assert any("403" in e for e in report.errors)


# ---------------------------------------------------------------------------
# The pure decision, and the layout it depends on
# ---------------------------------------------------------------------------

def test_the_key_parser_agrees_with_the_layout_the_worker_writes():
    """Guards the one rule this module restates.

    `reconciler.checkpoints` re-derives tenant, task, attempt and checkpoint id
    from an object key; `agent_worker.checkpoint.checkpoint_prefix` is what
    produces that key. Every rule restated twice in this repository has since
    drifted, so the two are compared directly rather than trusted.
    """
    prefix = checkpoint_prefix(
        tenant_id="eng", task_id="task_1", attempt_id="att_9", checkpoint_id="ckpt-00007"
    )
    ref = parse_checkpoint_key(f"{prefix}/manifest.json")
    assert ref is not None
    assert (ref.tenant_id, ref.task_id, ref.attempt_id, ref.checkpoint_id) == (
        "eng", "task_1", "att_9", "ckpt-00007"
    )
    assert ref.prefix == prefix
    assert ref.manifest_key == f"{prefix}/manifest.json"
    assert parse_checkpoint_key(f"{prefix}/archive.tar.gz") == ref


@pytest.mark.parametrize(
    "key",
    [
        "tenants/eng/tasks/t/attempts/a/artifacts/out.json",
        "tenants/eng/tasks/t/attempts/a/logs/runner.log",
        "tenants/eng/tasks/t/attempts/a/checkpoints/ckpt-1",   # the directory itself
        "tenants/eng/tasks/t/checkpoints/ckpt-1/manifest.json",
        "somewhere/else/entirely.txt",
        "",
    ],
)
def test_the_key_parser_refuses_anything_it_does_not_recognise(key):
    assert parse_checkpoint_key(key) is None


def test_a_pointer_matches_a_whole_prefix_not_a_string_prefix():
    """`ckpt-00001` and `ckpt-000010` share a string prefix. A substring test
    would protect the wrong object and collect the right one."""
    base = "tenants/eng/tasks/t/attempts/a/checkpoints"
    assert pointer_to_prefix(f"gs://b/{base}/ckpt-00001/") == f"{base}/ckpt-00001"
    assert pointer_to_prefix(f"file:///tmp/x/{base}/ckpt-00001/") == f"{base}/ckpt-00001"
    assert pointer_to_prefix(f"gs://b/{base}/ckpt-00001/manifest.json") == f"{base}/ckpt-00001"
    assert pointer_to_prefix(f"gs://b/{base}/ckpt-000010/") != f"{base}/ckpt-00001"
    assert pointer_to_prefix("") is None
    assert pointer_to_prefix(None) is None
    assert pointer_to_prefix("gs://b/no-tenants-segment/x") is None


def _ref(task_id="t", attempt_id="a", checkpoint_id="ckpt-00001"):
    return parse_checkpoint_key(
        checkpoint_prefix(
            tenant_id=TENANT, task_id=task_id, attempt_id=attempt_id,
            checkpoint_id=checkpoint_id,
        )
        + "/manifest.json"
    )


def _task(state: TaskState, latest_checkpoint: str | None = None) -> TaskView:
    return TaskView(
        task_id="t", tenant_id=TENANT, state=state, generation=1, lease_id=None,
        runner_profile="mock", resource_class="standard", updated_at=utcnow(),
        latest_checkpoint=latest_checkpoint,
    )


def _attempt(attempt_id="a", completed=True) -> AttemptView:
    return AttemptView(
        attempt_id=attempt_id, task_id="t", tenant_id=TENANT, generation=1,
        backend="CLOUD_RUN_JOB", execution_name=None, created_at=utcnow(),
        completed_at=utcnow() if completed else None,
    )


@pytest.mark.parametrize(
    "state",
    [s for s in TaskState if s not in (TaskState.SUCCEEDED, TaskState.FAILED,
                                       TaskState.CANCELLED, TaskState.DEAD_LETTERED)],
)
def test_every_non_terminal_state_keeps_its_checkpoints(state):
    """Exhaustive over the state machine rather than over the states someone
    remembered: a state added to the frozen contract must not silently become
    collectable."""
    decision = classify(_ref(), task=_task(state), attempts=[_attempt()])
    assert decision.disposition is Disposition.KEEP


@pytest.mark.parametrize(
    "state,expected",
    [
        (TaskState.SUCCEEDED, Disposition.RECLAIM),
        (TaskState.CANCELLED, Disposition.RECLAIM),
        (TaskState.DEAD_LETTERED, Disposition.RECLAIM),
        # FAILED -> READY is legal, so the pointer target stays.
        (TaskState.FAILED, Disposition.KEEP),
    ],
)
def test_the_pointer_target_of_each_terminal_state(state, expected):
    ref = _ref()
    pointer = f"gs://{BUCKET}/{ref.prefix}/"
    decision = classify(ref, task=_task(state, pointer), attempts=[_attempt()])
    assert decision.disposition is expected


def test_manifest_age_refuses_to_guess():
    now = utcnow()
    assert manifest_age_seconds(b"not json", now=now) is None
    assert manifest_age_seconds(b"{}", now=now) is None
    assert manifest_age_seconds(b'{"created_at": "yesterday"}', now=now) is None
    stamp = (now - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    age = manifest_age_seconds(json.dumps({"created_at": stamp}).encode(), now=now)
    assert age is not None and 3 * 3600 - 5 < age < 3 * 3600 + 5


# ---------------------------------------------------------------------------
# Wiring: the sweep runs from a reconciliation pass, on its own clock
# ---------------------------------------------------------------------------

def test_the_sweep_runs_from_a_pass_and_then_not_again_until_its_interval(
    db_and_objects, config, log
):
    """A full-bucket listing on every five-minute tick is a bill. The claim is
    persisted in Firestore, not in memory, because swarm-reconciler scales to
    zero and an in-process timer resets on every cold start."""
    db, objects, tmp_path = db_and_objects
    first = write_checkpoint(objects, tmp_path, log, task_id="task_done", attempt_id="att_1")
    second = write_checkpoint(objects, tmp_path, log, task_id="task_done", attempt_id="att_2")
    seed_task(db, task_id="task_done", state=TaskState.SUCCEEDED)
    seed_attempt_doc(db, task_id="task_done", attempt_id="att_1", completed=True)
    seed_attempt_doc(db, task_id="task_done", attempt_id="att_2", completed=True)

    store = ControlStore(db, logger=log, txn_runner=FakeTransactionRunner(db))
    reconciler = Reconciler(
        store=store, backends=[], config=config, logger=log, checkpoint_store=objects
    )

    report = reconciler.run_once()
    assert report.checkpoint_sweep is not None
    assert report.checkpoint_sweep["reclaimed"] == 2
    assert not objects.exists(first.manifest_key)
    assert not objects.exists(second.manifest_key)

    # A second pass moments later must not relist the bucket.
    again = reconciler.run_once()
    assert again.checkpoint_sweep is None


def test_a_reconciler_with_no_bucket_configured_sweeps_nothing(db_and_objects, config, log):
    db, objects, tmp_path = db_and_objects
    record = write_checkpoint(objects, tmp_path, log, task_id="task_done", attempt_id="att_1")
    seed_task(db, task_id="task_done", state=TaskState.SUCCEEDED)
    seed_attempt_doc(db, task_id="task_done", attempt_id="att_1", completed=True)

    store = ControlStore(db, logger=log, txn_runner=FakeTransactionRunner(db))
    report = Reconciler(
        store=store, backends=[], config=config, logger=log, checkpoint_store=None
    ).run_once()

    assert report.checkpoint_sweep is None
    assert objects.exists(record.manifest_key)
