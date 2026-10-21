"""Admission charges, pins and renders the current runner-size version
(SIZES-SCHED-2, docs/design/runner-sizes.md §4.3, §4.4, §6; part of #1689).

An admin's runner sizes are a versioned platform setting: the pointer
`control/runner_sizes` names `current_version`, and
`runner_size_versions/<version, zero-padded to 6>` holds the whole table. The
scheduler reads it once per admission pass and, for one agent:

  * charges the units derived from the version's size (`units_for`), not the
    code table's;
  * pins that size on the attempt document (contract request 111, RS-4):
    `size_version`, `sized_cpu_request`, `sized_cpu_limit`,
    `sized_memory_gib`, `sized_disk_gib`, and `sized_live_split` for a live
    pod only, equal to the two containers the manifest renders;
  * renders the GKE pod from that same pinned size, so a version written
    after the lease and before the dispatch changes nothing about this
    agent: the units the lease charged and the pod Autopilot bills agree;
  * gives a Cloud Run profile the code default and version 0 whatever the
    version says (Cloud Run fixes resources on the Job, §4.7, owner Q3 A);
  * reads a malformed version as the code defaults and logs it at ERROR,
    because a typo written straight to Firestore must not size a pod.

Each assertion carries a MUTATION comment naming the change that turns it red.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import pytest

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.conftest import (  # noqa: E402
    scheduler_settings,
    seed_pool,
    seed_task,
    seed_tenant,
)
from control_plane.fakes import FakeFirestore  # noqa: E402

from swarm_common.profiles import RUNNER_PROFILES  # noqa: E402

from scheduler.dispatch import BackendRouter, GkeJobDispatcher, GkeTarget  # noqa: E402
from scheduler.loop import Scheduler  # noqa: E402
from scheduler.metrics import SchedulerMetrics  # noqa: E402
from scheduler.store import SchedulerStore  # noqa: E402

TENANT = "eng"

#: Version 2 of the setting: `standard` grown to 6 cores / 12 GiB, bursting to
#: 12. `browser` grows with it, because a class may not be larger than the
#: one after it (CLASS_ORDER), and its live split still sums to the class.
#: The split is NOT what LIVE_DISPLAY_SHARE scaled to 6 / 12 would give
#: (worker 4 / 8 / 7, display 2 / 3 / 5), so a renderer that ignored the
#: stored split would be caught.
STANDARD_V2 = {"cpu_request": 6, "cpu_limit": 12, "memory_gib": 12, "disk_gib": 4, "units": 2}
BROWSER_V2 = {
    "cpu_request": 6, "cpu_limit": 12, "memory_gib": 12, "disk_gib": 4, "units": 2,
    "live_split": {
        "worker": {"cpu_request": 3, "cpu_limit": 6, "memory_gib": 6},
        "display": {"cpu_request": 3, "cpu_limit": 4, "memory_gib": 6},
    },
}
LARGE = {"cpu_request": 8, "cpu_limit": 16, "memory_gib": 32, "disk_gib": 16, "units": 4}


def write_version(db: FakeFirestore, version: int, classes: dict[str, Any]) -> None:
    """The two documents swarm-api's admin route writes (design §4.3)."""
    db.docs[f"runner_size_versions/{version:06d}"] = {
        "version": version,
        "previous_version": version - 1,
        "created_at": None,
        "created_by": "admin@example.com",
        "reason": "test",
        "restored_from": None,
        "classes": classes,
        "changed": [],
    }
    db.docs["control/runner_sizes"] = {
        "current_version": version,
        "updated_at": None,
        "updated_by": "admin@example.com",
    }


def version_2(db: FakeFirestore) -> None:
    write_version(db, 2, {"standard": STANDARD_V2, "browser": BROWSER_V2, "large": LARGE})


class FakeBatch:
    """The Kubernetes BatchV1Api the GKE dispatcher creates Jobs through."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create_namespaced_job(self, *, namespace: str, body: dict[str, Any]) -> dict[str, Any]:
        self.created.append(body)
        return {"metadata": {"name": body["metadata"]["name"]}}


class FakeCloudRun:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def dispatch(self, *, task, lease, profile, tenant) -> str:  # noqa: ANN001
        self.calls.append({"task_id": task.id, "units": lease.units})
        return f"executions/{task.id}"


@pytest.fixture
def db() -> FakeFirestore:
    store = FakeFirestore()
    seed_tenant(store, TENANT, max_active=20, credentials=("anthropic", "openai"))
    seed_pool(store, "global", hard_limit=40)
    return store


@pytest.fixture
def batch() -> FakeBatch:
    return FakeBatch()


@pytest.fixture
def cloud_run() -> FakeCloudRun:
    return FakeCloudRun()


def make_scheduler(db: FakeFirestore, batch: FakeBatch, cloud_run: FakeCloudRun,
                   *, live: bool = False) -> Scheduler:
    settings = scheduler_settings(live_browser_sidecar=live)
    gke = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=batch)
    router = BackendRouter(cloud_run=cloud_run, gke=gke, settings=settings)
    return Scheduler(settings=settings, store=SchedulerStore(db), router=router,
                     metrics=SchedulerMetrics())


def admit(db: FakeFirestore, scheduler: Scheduler, task_id: str, profile: str,
          resource_class: str | None = None) -> None:
    seed_task(db, task_id=task_id, tenant_id=TENANT, runner_profile=profile,
              resource_class=resource_class or RUNNER_PROFILES[profile].resource_class,
              provider=RUNNER_PROFILES[profile].provider)
    report = scheduler.drain()
    assert report.leased == 1, report.to_dict()


def lease_of(db: FakeFirestore, task_id: str) -> dict[str, Any]:
    (lease,) = [d for k, d in db.docs.items() if k.startswith("leases/") and d["task_id"] == task_id]
    return lease


def attempt_of(db: FakeFirestore, task_id: str) -> dict[str, Any]:
    (attempt,) = [d for k, d in db.docs.items()
                  if k.startswith("attempts/") and d["task_id"] == task_id]
    return attempt


def container(job: dict[str, Any], name: str) -> dict[str, Any]:
    spec = job["spec"]["template"]["spec"]
    (found,) = [c for c in spec["containers"] + spec.get("initContainers", []) if c["name"] == name]
    return found


def cores(quantity: str) -> float:
    return int(quantity[:-1]) / 1000 if quantity.endswith("m") else float(quantity)


def gib(quantity: str) -> int:
    assert quantity.endswith("Gi"), quantity
    return int(quantity[:-2])


# --- a GKE agent follows the version ----------------------------------------------


def test_a_claude_code_agent_is_leased_and_rendered_at_version_2(db, batch, cloud_run):
    version_2(db)
    admit(db, make_scheduler(db, batch, cloud_run), "task_cc", "claude-code")

    # MUTATION: keep `units = RESOURCE_CLASSES[task.resource_class].units` in
    # Scheduler._admit_one and the lease charges 1 for a 6-core, 12 GiB pod.
    assert lease_of(db, "task_cc")["units"] == 2
    assert db.docs["pools/global"]["active"] == 2

    (job,) = batch.created
    resources = container(job, "worker")["resources"]
    # MUTATION: render from `RESOURCE_CLASSES[...]` in GkeJobDispatcher._manifest
    # instead of the pinned size and this reads 4 / 8 GiB.
    assert resources["requests"]["cpu"] == "6000m"
    assert resources["limits"]["cpu"] == "12000m"
    # MUTATION: render the version's memory in one direction only and memory
    # bursts (invariant 7).
    assert resources["requests"]["memory"] == resources["limits"]["memory"] == "12Gi"


def test_the_attempt_records_the_version_and_the_size_it_was_rendered_at(db, batch, cloud_run):
    version_2(db)
    admit(db, make_scheduler(db, batch, cloud_run), "task_cc", "claude-code")

    attempt = attempt_of(db, "task_cc")
    # MUTATION: drop the sized fields from SchedulerStore.create_attempt and
    # nothing records what an older attempt ran at after a resize.
    assert attempt["size_version"] == 2
    assert (attempt["sized_cpu_request"], attempt["sized_cpu_limit"]) == (6, 12)
    assert (attempt["sized_memory_gib"], attempt["sized_disk_gib"]) == (12, 4)
    # MUTATION: record a split on a pod that has no display and a reader
    # draws two containers that never existed.
    assert attempt["sized_live_split"] is None


def test_the_worker_is_told_the_size_it_was_rendered_at(db, batch, cloud_run):
    version_2(db)
    admit(db, make_scheduler(db, batch, cloud_run), "task_cc", "claude-code")

    (job,) = batch.created
    env = {e["name"]: e.get("value") for e in container(job, "worker")["env"]}
    # MUTATION: leave SWARM_SIZED_* out of the GKE manifest and the worker's
    # `_cpu_limit` fallback reads the catalogue's 8, not the 12 it was given
    # (agent_worker/config.py, SIZES-WORKER-1).
    assert (env["SWARM_SIZED_CPU_REQUEST"], env["SWARM_SIZED_CPU_LIMIT"],
            env["SWARM_SIZED_MEMORY_GIB"]) == ("6", "12", "12")


def test_a_live_browser_attempt_records_the_split_its_manifest_renders(db, batch, cloud_run):
    version_2(db)
    admit(db, make_scheduler(db, batch, cloud_run, live=True), "task_live", "claude-code-browser")

    (job,) = batch.created
    rendered = {}
    for name in ("worker", "display"):
        r = container(job, name)["resources"]
        assert r["requests"]["memory"] == r["limits"]["memory"]
        rendered[name] = {"cpu_request": cores(r["requests"]["cpu"]),
                          "cpu_limit": cores(r["limits"]["cpu"]),
                          "memory_gib": gib(r["requests"]["memory"])}
    # MUTATION: render the split from LIVE_DISPLAY_SHARE scaled to the size
    # (drop `split=` from with_live_display's call in _manifest, or the stored
    # split from `live_split_for`) and the worker reads 4 / 8 / 7.
    assert rendered == {"worker": {"cpu_request": 3, "cpu_limit": 6, "memory_gib": 6},
                        "display": {"cpu_request": 3, "cpu_limit": 4, "memory_gib": 6}}
    attempt = attempt_of(db, "task_live")
    # MUTATION: record the version's split while the dispatcher renders
    # another and the record stops describing the pod.
    assert attempt["sized_live_split"] == rendered
    assert attempt["size_version"] == 2
    env = {e["name"]: e.get("value") for e in container(job, "worker")["env"]}
    # MUTATION: tell the worker the CLASS's numbers on a live pod and it reads
    # a 12 GiB ceiling in a 6 GiB container.
    assert (env["SWARM_SIZED_CPU_LIMIT"], env["SWARM_SIZED_MEMORY_GIB"]) == ("6", "6")


def test_a_live_capable_profile_with_the_sidecar_off_records_no_split(db, batch, cloud_run):
    version_2(db)
    admit(db, make_scheduler(db, batch, cloud_run, live=False), "task_b", "claude-code-browser")

    (job,) = batch.created
    spec = job["spec"]["template"]["spec"]
    assert not spec.get("initContainers")
    # MUTATION: decide "live" from `profile.live_browser` alone and an attempt
    # with no display records one.
    assert attempt_of(db, "task_b")["sized_live_split"] is None
    resources = container(job, "worker")["resources"]
    assert (resources["requests"]["cpu"], resources["limits"]["cpu"]) == ("6000m", "12000m")


# --- the size is pinned at admission --------------------------------------------


def test_a_version_written_after_the_lease_does_not_change_the_manifest(db, batch, cloud_run):
    version_2(db)
    scheduler = make_scheduler(db, batch, cloud_run)
    store = scheduler.store
    acquire = store.acquire_lease

    def acquire_then_resize(*args: Any, **kwargs: Any):
        lease = acquire(*args, **kwargs)
        # Another admin saves version 3 between the lease and the dispatch.
        write_version(db, 3, {
            "standard": {"cpu_request": 2, "cpu_limit": 4, "memory_gib": 4, "disk_gib": 2},
            "browser": BROWSER_V2, "large": LARGE})
        return lease

    store.acquire_lease = acquire_then_resize  # type: ignore[method-assign]
    admit(db, scheduler, "task_cc", "claude-code")

    (job,) = batch.created
    resources = container(job, "worker")["resources"]
    # MUTATION: have the GKE dispatcher re-read the current version instead of
    # rendering the size admission pinned and this reads 2000m / 4Gi -- a pod
    # smaller than the 2 units its lease charged.
    assert (resources["requests"]["cpu"], resources["requests"]["memory"]) == ("6000m", "12Gi")
    assert lease_of(db, "task_cc")["units"] == 2
    assert attempt_of(db, "task_cc")["size_version"] == 2


def test_the_version_is_read_once_per_admission_pass(db, batch, cloud_run):
    version_2(db)
    scheduler = make_scheduler(db, batch, cloud_run)
    reads: list[int] = []
    original = scheduler.store.runner_size_documents

    def counting() -> Any:
        reads.append(1)
        return original()

    scheduler.store.runner_size_documents = counting  # type: ignore[method-assign]
    for n in range(3):
        seed_task(db, task_id=f"task_{n}", tenant_id=TENANT, runner_profile="claude-code",
                  provider="anthropic")
    report = scheduler.drain()
    assert report.leased == 3
    # MUTATION: read the version per agent instead of per pass and this is 3.
    # (The second pass finds nothing READY and reads nothing.)
    assert len(reads) == 1


# --- Cloud Run keeps the default ------------------------------------------------


def test_a_mock_agent_on_cloud_run_keeps_the_default_whatever_the_version(db, batch, cloud_run):
    version_2(db)
    admit(db, make_scheduler(db, batch, cloud_run), "task_mock", "mock")

    # MUTATION: size a Cloud Run profile from the version (drop the backend
    # from `sizes.for_task`) and the lease charges 2 for a Job that runs at 4 / 8.
    assert cloud_run.calls == [{"task_id": "task_mock", "units": 1}]
    assert lease_of(db, "task_mock")["units"] == 1
    attempt = attempt_of(db, "task_mock")
    # MUTATION: record the version read rather than the one applied and a
    # Cloud Run attempt claims version 2.
    assert attempt["size_version"] == 0
    assert (attempt["sized_cpu_request"], attempt["sized_cpu_limit"]) == (4, 8)
    assert (attempt["sized_memory_gib"], attempt["sized_disk_gib"]) == (8, 4)
    assert attempt["sized_live_split"] is None


# --- no version, and a malformed one ---------------------------------------------


def test_with_no_version_written_the_code_defaults_are_version_0(db, batch, cloud_run):
    admit(db, make_scheduler(db, batch, cloud_run), "task_cc", "claude-code")

    (job,) = batch.created
    resources = container(job, "worker")["resources"]
    assert (resources["requests"]["cpu"], resources["limits"]["cpu"]) == ("4000m", "8000m")
    assert lease_of(db, "task_cc")["units"] == 1
    attempt = attempt_of(db, "task_cc")
    # MUTATION: record None for "no version" and a reader cannot tell the
    # code defaults from an attempt written before the change.
    assert attempt["size_version"] == 0
    assert (attempt["sized_cpu_request"], attempt["sized_memory_gib"]) == (4, 8)


@pytest.mark.parametrize("why, classes", [
    ("a limit past twice the request", {
        "standard": {**STANDARD_V2, "cpu_limit": 30}, "browser": BROWSER_V2, "large": LARGE}),
    ("a field that is not a number", {
        "standard": {**STANDARD_V2, "memory_gib": "12"}, "browser": BROWSER_V2, "large": LARGE}),
    ("a field missing", {
        "standard": {"cpu_request": 6, "cpu_limit": 12, "memory_gib": 12},
        "browser": BROWSER_V2, "large": LARGE}),
    ("standard larger than browser", {"standard": STANDARD_V2}),
    ("a class that does not exist", {
        "standard": STANDARD_V2, "browser": BROWSER_V2, "large": LARGE, "huge": LARGE}),
    ("classes that are not a map", ["standard"]),
])
def test_a_malformed_version_leases_at_the_default_and_logs_at_error(
        db, batch, cloud_run, caplog, why, classes):
    write_version(db, 7, classes)
    with caplog.at_level(logging.ERROR, logger="scheduler.sizes"):
        admit(db, make_scheduler(db, batch, cloud_run), "task_cc", "claude-code")

    (job,) = batch.created
    resources = container(job, "worker")["resources"]
    # MUTATION: use a version without `check_size` (or without the shape
    # check) and a typo written to Firestore sizes a pod.
    assert (resources["requests"]["cpu"], resources["requests"]["memory"]) == ("4000m", "8Gi"), why
    assert lease_of(db, "task_cc")["units"] == 1
    assert attempt_of(db, "task_cc")["size_version"] == 0
    # MUTATION: fall back silently (log at WARNING, or not at all) and nobody
    # learns the setting is broken.
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and r.name == "scheduler.sizes"]
    assert errors, why
    assert "7" in errors[0].getMessage()


def test_a_pointer_to_a_missing_version_reads_as_the_defaults(db, batch, cloud_run, caplog):
    db.docs["control/runner_sizes"] = {"current_version": 4}
    with caplog.at_level(logging.ERROR, logger="scheduler.sizes"):
        admit(db, make_scheduler(db, batch, cloud_run), "task_cc", "claude-code")

    # MUTATION: treat a missing version document as "no version" without a
    # log line and a half-written save goes unnoticed.
    assert any(r.levelno >= logging.ERROR for r in caplog.records if r.name == "scheduler.sizes")
    assert attempt_of(db, "task_cc")["size_version"] == 0
    assert lease_of(db, "task_cc")["units"] == 1

