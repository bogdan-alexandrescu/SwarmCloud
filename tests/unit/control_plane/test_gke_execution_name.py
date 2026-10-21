"""A GKE attempt is recorded as `<namespace>/<job>`, and a legacy pod name is read as its Job (#1804).

On GKE the worker used to record its POD HOSTNAME (`swarm-<task>-<gen>-<5
chars>`) as its attempt's `execution_name`: `agent_worker.lifecycle.
_execution_name()` read CLOUD_RUN_EXECUTION, K_REVISION, JOB_NAME and HOSTNAME,
and a GKE Job sets only the last. `record_attempt_start` then replaced the
dispatcher's `<namespace>/<job>` with it, so the reconciler's stop-execution
probe answered UNREADABLE ("not a <namespace>/<job> name") and a cancelled GKE
agent ran on until the sweep reached it. Pinned here:

  N-1  the GKE Job's env carries SWARM_EXECUTION_NAME, `<namespace>/<job>`, and
       it is EXACTLY the string `dispatch` returns for the attempt to record --
       one render, never recomputed. A Cloud Run execution carries none.
  N-2  the worker records that over HOSTNAME, so cancel's probe of a GKE
       attempt reads ACTIVE and the Job is stopped at once.
  N-3  a legacy pod-name record is probed as its Job, `swarm-<task>-<gen>` in
       the tenant's namespace -- only for a pod name of THAT task and
       generation; anything else stays UNREADABLE, never ABSENT.
  N-4  the mapping widens nothing: a mapped Job that is not platform-managed
       is still not ours to stop.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from agent_worker.lifecycle import _execution_name
from reconciler.backends import ProbeOutcome
from reconciler.logs import build_logger
from reconciler.stopexec import ExecutionStopper
from scheduler.dispatch import (
    GKE_EXECUTION_NAME_ENV,
    CloudRunJobDispatcher,
    GkeJobDispatcher,
    GkeTarget,
)
from swarm_common.profiles import RUNNER_PROFILES

from .test_dispatch_manifests import (  # noqa: F401
    FakeBatchApi,
    FakeJobsClient,
    make_lease,
    make_task,
    settings,
    tenant,
)
from .test_reconciler_gke_namespaced import ENG, ENG_NS, GKE, RbacBatchApi, gke, k8s_job

#: The issue's own identifiers: a live Job and the pod its worker ran in.
TASK = "task_918a848f969e406389fd"
JOB = "swarm-918a848f969e406389fd-1"
POD = "swarm-918a848f969e406389fd-1-2cq5s"


def _dispatch_gke(settings: Any, tenant: Any) -> tuple[str, dict]:  # noqa: F811
    api = FakeBatchApi()
    task = make_task("browser")
    recorded = GkeJobDispatcher(
        settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api
    ).dispatch(task=task, lease=make_lease(task), profile=RUNNER_PROFILES["browser"], tenant=tenant)
    _, body = api.created[0]
    return recorded, body


def _worker_env(body: dict) -> dict[str, str]:
    worker = next(c for c in body["spec"]["template"]["spec"]["containers"] if c["name"] == "worker")
    return {e["name"]: e.get("value") for e in worker["env"]}


# --- N-1 the dispatcher hands the worker the name it records ------------------


def test_the_gke_job_env_carries_the_execution_name_the_dispatcher_records(settings, tenant):  # noqa: F811
    recorded, body = _dispatch_gke(settings, tenant)
    env = _worker_env(body)
    # MUTATION: drop the env entry from `GkeJobDispatcher._manifest` and the
    # worker has nothing but its pod's HOSTNAME to record.
    assert GKE_EXECUTION_NAME_ENV in env, sorted(env)
    # MUTATION: render it as the bare Job name, or from a second namespace_for
    # call, and the worker's record disagrees with the dispatcher's again.
    assert env[GKE_EXECUTION_NAME_ENV] == recorded
    # MUTATION: return anything but `<namespace>/<job>` of the created Job.
    assert recorded == f"{body['metadata']['namespace']}/{body['metadata']['name']}"
    assert recorded == "swarm-tenant-eng/swarm-abc123-3"


def test_the_name_is_set_once_so_the_kubelet_never_chooses(settings, tenant):  # noqa: F811
    _, body = _dispatch_gke(settings, tenant)
    worker = body["spec"]["template"]["spec"]["containers"][0]
    names = [e["name"] for e in worker["env"]]
    # MUTATION: append it a second time (e.g. also from gke_worker_env).
    assert names.count(GKE_EXECUTION_NAME_ENV) == 1, names


def test_a_cloud_run_execution_carries_no_gke_execution_name(settings, tenant):  # noqa: F811
    """Cloud Run is unchanged: its worker still records CLOUD_RUN_EXECUTION."""
    client = FakeJobsClient()
    task = make_task("claude-code")
    CloudRunJobDispatcher(settings, client=client).dispatch(
        task=task, lease=make_lease(task), profile=RUNNER_PROFILES["claude-code"], tenant=tenant
    )
    override = client.runs[0]["overrides"].container_overrides[0]
    # MUTATION: put the variable in the shared `worker_env` and a Cloud Run
    # worker records a `<namespace>/<job>` that names nothing on Cloud Run.
    assert GKE_EXECUTION_NAME_ENV not in {e.name for e in override.env}


# --- N-2 the worker records it, and cancel's probe reads it -------------------


class _Store:
    """The two documents a stop request reads; nothing is written."""

    def __init__(self, task: dict, attempt: dict) -> None:
        self.task, self.attempt = task, attempt

    def task_and_attempt_docs(self, task_id: str, attempt_id: str):
        return self.task, self.attempt


def _stopper(backend: Any, attempt: dict) -> ExecutionStopper:
    task = {"tenant_id": ENG, "state": "RUNNING", "cancel_requested": True}
    return ExecutionStopper(
        store=_Store(task, attempt), backends={backend.name: backend},
        logger=build_logger(stream=io.StringIO()),
    )


def test_cancel_probes_a_gke_attempt_as_active_and_stops_its_job(settings, tenant, monkeypatch):  # noqa: F811
    """Dispatcher render -> worker record -> reconciler stop, end to end."""
    recorded, body = _dispatch_gke(settings, tenant)
    env = _worker_env(body)
    for name in ("CLOUD_RUN_EXECUTION", "K_REVISION", "JOB_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOSTNAME", body["metadata"]["name"] + "-2cq5s")
    monkeypatch.setenv(GKE_EXECUTION_NAME_ENV, env[GKE_EXECUTION_NAME_ENV])
    # MUTATION: leave HOSTNAME first in `_execution_name` and this is the pod.
    assert _execution_name() == recorded

    task = make_task("browser")
    job = k8s_job(task_id=task.id, generation=make_lease(task).generation, active=1)
    batch = RbacBatchApi(jobs=[job], listable={ENG_NS})
    attempt = {"task_id": task.id, "tenant_id": ENG, "backend": GKE,
               "generation": 3, "execution_name": _execution_name(), "completed_at": None}
    outcome = _stopper(gke(batch), attempt).stop(
        tenant_id=ENG, task_id=task.id, attempt_id=f"att_{task.id[5:]}")
    # MUTATION: record the pod name again and this reads "unreadable", leaving
    # the cancelled Job to run until the sweep.
    assert outcome == "stopped"
    assert batch.deleted == [recorded]


# --- N-3 a legacy pod-name record is read as its Job --------------------------


def _legacy(batch: RbacBatchApi, name: str = POD, **ids: Any):
    kwargs = {"task_id": TASK, "tenant_id": ENG, "generation": 1, **ids}
    return gke(batch).probe(name, **kwargs)


def test_a_legacy_pod_name_is_probed_as_its_live_job():
    batch = RbacBatchApi(jobs=[k8s_job(task_id=TASK, active=1)], listable={ENG_NS})
    probe = _legacy(batch)
    # MUTATION: keep answering "not a <namespace>/<job> name" for a bare name.
    assert probe.outcome is ProbeOutcome.ACTIVE, probe.detail
    # MUTATION: strip anything but the 5-character pod suffix, or read another
    # namespace than the tenant's, and the Job asked for is not the dispatched one.
    assert batch.calls == [("read_namespaced_job", ENG_NS, JOB)]
    assert probe.execution is not None and probe.execution.task_id == TASK


def test_a_legacy_pod_name_whose_job_is_really_gone_reads_absent():
    """The mapping is exact, so its 404 proves what any by-name 404 proves."""
    batch = RbacBatchApi(jobs=[], listable={ENG_NS})
    # MUTATION: answer UNREADABLE for every mapped name and a dead legacy
    # attempt is never released by the probe.
    assert _legacy(batch).outcome is ProbeOutcome.ABSENT


@pytest.mark.parametrize(
    "name,ids",
    [
        # Another task's pod: never read as this task's Job.
        ("swarm-3f3f8e2700ed46f89c17-1-2cq5s", {}),
        # This task's pod, from another generation's Job.
        ("swarm-918a848f969e406389fd-2-2cq5s", {}),
        # Not a pod suffix: four characters, six characters, upper case.
        ("swarm-918a848f969e406389fd-1-2cq5", {}),
        ("swarm-918a848f969e406389fd-1-2cq5sx", {}),
        ("swarm-918a848f969e406389fd-1-2CQ5S", {}),
        # A pod name with no task, tenant or generation to check it against.
        (POD, {"task_id": None}),
        (POD, {"tenant_id": None}),
        (POD, {"generation": None}),
        # Not a pod name at all.
        ("localhost", {}),
    ],
)
def test_a_name_that_is_not_this_attempts_pod_is_unreadable_and_never_read(name, ids):
    batch = RbacBatchApi(jobs=[k8s_job(task_id=TASK, active=1)], listable={ENG_NS})
    probe = _legacy(batch, name, **ids)
    # MUTATION: map any `swarm-...-xxxxx` name, or ignore the task id or the
    # generation, and another attempt's Job is probed (and could be stopped)
    # as this one's; or 404 it into ABSENT and a live agent is called gone.
    assert probe.outcome is ProbeOutcome.UNREADABLE, (name, probe)
    assert batch.calls == []


def test_a_recorded_namespace_job_name_is_probed_as_it_was():
    batch = RbacBatchApi(jobs=[k8s_job(task_id=TASK, active=1)], listable={ENG_NS})
    probe = gke(batch).probe(f"{ENG_NS}/{JOB}")
    # MUTATION: route `<namespace>/<job>` through the legacy mapping and a
    # caller that passes no ids loses the probe it already had.
    assert probe.outcome is ProbeOutcome.ACTIVE, probe.detail
    assert batch.calls == [("read_namespaced_job", ENG_NS, JOB)]


def test_namespace_of_maps_a_legacy_pod_name_to_the_tenants_namespace():
    backend = gke(RbacBatchApi())
    ids = {"task_id": TASK, "tenant_id": ENG, "generation": 1}
    # MUTATION: return None for every bare name and the attempt's namespace
    # comes only from the tenant fallback, never from its own record.
    assert backend.namespace_of(POD, **ids) == ENG_NS
    # MUTATION: map another task's pod and an attempt is read where its task never ran.
    assert backend.namespace_of("swarm-3f3f8e2700ed46f89c17-1-2cq5s", **ids) is None
    assert backend.namespace_of(POD) is None
    assert backend.namespace_of(f"{ENG_NS}/{JOB}") == ENG_NS


# --- N-4 the mapping widens nothing -------------------------------------------


def test_a_mapped_job_that_is_not_platform_managed_is_still_not_ours():
    job = k8s_job(task_id=TASK, active=1)
    job.metadata.labels = {}
    batch = RbacBatchApi(jobs=[job], listable={ENG_NS})
    probe = _legacy(batch)
    # MUTATION: skip `_job_view` for a mapped name and an unlabelled Job reads
    # ACTIVE -- something stopexec would then delete.
    assert probe.outcome is ProbeOutcome.UNREADABLE, probe
    assert probe.execution is None
