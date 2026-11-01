"""A pod whose TypeSafe key the forwarder delivers carries `swarm-typesafe: inject`
(docs/design/workspace-secrets.md §3.5, SEC-MIGRATE-TYPESAFE; lane
TS-MIGRATE-POD, part of #1347).

The label is what `swarm-allow-typesafe-fwd` (the tenant side) and
`swarm-typesafe-fwd-ingress` (the forwarder's side) select on, so it is the
only way a pod reaches swarm-typesafe-fwd. The dispatcher sets it when ALL of
these hold, and each is turned off once below:

  * the forwarder is switched on (`typesafe_forwarder_enabled`, terraform's
    typesafe_forwarder.enabled once served);
  * the task's tenant is in `typesafe_forwarder_tenants`, by exact name;
  * the attempt runs a TypeSafe-backed jev or judge: a GKE live-browser pod
    (the display rollout switch on, a `live_browser` profile) whose workspace
    chose the `typesafe` judgment backend. That is the only attempt the worker
    resolves a TypeSafe key for today (`jevkey.prepare`), and the judge reads
    the key jevkey wrote, so it is the same attempt for both.

A Cloud Run Job gets no label and keeps today's path: the forwarder is
reachable only from GKE pods.

THE WORKER'S SWITCH RIDES WITH THE LABEL (lane TS-INJECT, part of #1347). The
worker takes the forwarder path only when `SWARM_TYPESAFE_INJECT=on` is in its
environment (agent_worker.typesafe_shim.SWITCH_ENV / SWITCH_ON). The same one
decision that labels the pod puts that variable in the worker container's env,
and nothing else does: a labelled pod whose worker still read the key file, or
a switched worker on a pod the policies do not open, is each half a migration.
`SWITCH` below is the worker's own pair, imported from typesafe_shim rather
than restated, so these tests compare the scheduler to the worker and not to a
third copy; test_typesafe_switch_parity.py also compares the two source files'
literals and runs the worker's real predicate.

MUTATION: drop any one condition from `typesafe_forwarder_on`, label a pod of
another backend, or match the tenant by prefix, and the named case fails.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from agent_worker import typesafe_shim
from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES

from scheduler.dispatch import (
    TYPESAFE_INJECT_ENV,
    TYPESAFE_POD_LABEL,
    CloudRunJobDispatcher,
    GkeJobDispatcher,
    GkeTarget,
    typesafe_forwarder_on,
    worker_env,
)
from scheduler.settings import SchedulerSettings

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import NOW, FakeJobsClient, make_lease, make_task

LIVE = "claude-code-browser"
LABEL = {"swarm-typesafe": "inject"}
#: What the worker reads: its own constants, not a copy of them.
SWITCH = (typesafe_shim.SWITCH_ENV, typesafe_shim.SWITCH_ON)

#: Every condition on. Each case below turns exactly one off.
ON: dict[str, Any] = dict(
    forwarder=True, tenants=("eng",), backend="typesafe", live=True, profile=LIVE,
)


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng", kind="group", principal="eng@example.com", created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def _settings(*, forwarder: bool, tenants: tuple[str, ...], live: bool) -> SchedulerSettings:
    return scheduler_settings(
        live_browser_sidecar=live,
        typesafe_forwarder_enabled=forwarder,
        typesafe_forwarder_tenants=tenants,
    )


def _pod(tenant: Tenant, *, forwarder: bool, tenants: tuple[str, ...], backend: str,
         live: bool, profile: str) -> dict[str, Any]:
    settings = _settings(forwarder=forwarder, tenants=tenants, live=live)
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object(), judgment=lambda tenant_id: backend)
    task = make_task(profile)
    job = dispatcher._manifest(task=task, lease=make_lease(task),
                               profile=RUNNER_PROFILES[profile], tenant=tenant)
    return job["spec"]["template"]


def _labels(tenant: Tenant, **conditions: Any) -> dict[str, str]:
    return _pod(tenant, **conditions)["metadata"]["labels"]


def _worker_env(tenant: Tenant, **conditions: Any) -> list[dict[str, str]]:
    """The worker container's env entries, every one, in order."""
    (worker,) = [c for c in _pod(tenant, **conditions)["spec"]["containers"]
                 if c["name"] == "worker"]
    return worker["env"]


def _switch_entries(env: list[dict[str, str]]) -> list[dict[str, str]]:
    return [e for e in env if e.get("name") == SWITCH[0]]


def test_the_label_is_the_one_both_policies_select():
    assert dict([TYPESAFE_POD_LABEL]) == LABEL


def test_every_condition_on_labels_the_pod(tenant):
    labels = _labels(tenant, **ON)
    assert labels["swarm-typesafe"] == "inject"


@pytest.mark.parametrize("off", [
    pytest.param({"forwarder": False}, id="forwarder-off"),
    pytest.param({"tenants": ()}, id="no-tenant-listed"),
    pytest.param({"tenants": ("research",)}, id="another-tenant-listed"),
    # Exact membership: a tenant whose name starts the same is not this one.
    pytest.param({"tenants": ("en", "eng-x")}, id="a-prefix-is-not-the-tenant"),
    pytest.param({"backend": "internal-jev"}, id="internal-jev-backend"),
    pytest.param({"backend": "internal-laya"}, id="internal-laya-backend"),
    pytest.param({"live": False}, id="display-switch-off"),
    pytest.param({"profile": "claude-code"}, id="not-a-live-browser-profile"),
])
def test_any_condition_off_leaves_the_pod_unlabelled(tenant, off):
    labels = _labels(tenant, **{**ON, **off})
    assert "swarm-typesafe" not in labels


def test_the_switch_is_the_name_and_value_the_worker_reads():
    # MUTATION: set `SWITCH_ON = "yes"` in agent_worker/typesafe_shim.py (or
    # rename SWITCH_ENV) and this fails. Against the third copy it replaced,
    # `("SWARM_TYPESAFE_INJECT", "on")` written here, it passed.
    assert TYPESAFE_INJECT_ENV == (typesafe_shim.SWITCH_ENV, typesafe_shim.SWITCH_ON)


def test_every_condition_on_switches_the_worker(tenant):
    """Exactly one entry, `on`, on the worker container: the worker's
    `switched_on` compares the stripped value to `on` exactly."""
    assert _switch_entries(_worker_env(tenant, **ON)) == [{"name": SWITCH[0], "value": SWITCH[1]}]


@pytest.mark.parametrize("off", [
    pytest.param({"forwarder": False}, id="forwarder-off"),
    pytest.param({"tenants": ()}, id="no-tenant-listed"),
    pytest.param({"tenants": ("research",)}, id="another-tenant-listed"),
    pytest.param({"tenants": ("en", "eng-x")}, id="a-prefix-is-not-the-tenant"),
    pytest.param({"backend": "internal-jev"}, id="internal-jev-backend"),
    pytest.param({"backend": "internal-laya"}, id="internal-laya-backend"),
    pytest.param({"live": False}, id="display-switch-off"),
    pytest.param({"profile": "claude-code"}, id="not-a-live-browser-profile"),
])
def test_any_condition_off_leaves_the_worker_unswitched(tenant, off):
    """Not `off`, not empty: absent, so nothing the worker reads changes."""
    conditions = {**ON, **off}
    assert _switch_entries(_worker_env(tenant, **conditions)) == []
    # The label and the switch are the one decision: neither without the other.
    assert "swarm-typesafe" not in _labels(tenant, **conditions)


def test_the_switch_changes_nothing_else_in_the_worker_env(tenant):
    """The same pod with the forwarder off differs in its worker's env by the
    switch alone: no other variable moves with it."""
    on = _worker_env(tenant, **ON)
    off = _worker_env(tenant, **{**ON, "forwarder": False})
    assert [e for e in on if e.get("name") != SWITCH[0]] == off


def test_the_label_changes_nothing_else_on_the_pod(tenant):
    """The forwarder's label is added and nothing else moves: the same pod
    with the forwarder off differs by that one label."""
    on = _labels(tenant, **ON)
    off = _labels(tenant, **{**ON, "forwarder": False})
    assert {k: v for k, v in on.items() if k != "swarm-typesafe"} == off


def test_the_pure_decision_reads_configuration_only():
    """Nothing on the task is read (invariant 10): the switch, the tenant and
    the workspace's stored backend decide it."""
    live = RUNNER_PROFILES[LIVE]
    settings = _settings(forwarder=True, tenants=("eng",), live=True)
    assert typesafe_forwarder_on(settings, tenant_id="eng", profile=live, backend="typesafe")
    assert not typesafe_forwarder_on(settings, tenant_id="eng", profile=live, backend="internal-jev")
    assert not typesafe_forwarder_on(settings, tenant_id="research", profile=live, backend="typesafe")
    assert not typesafe_forwarder_on(
        settings, tenant_id="eng", profile=RUNNER_PROFILES["claude-code"], backend="typesafe")
    assert not typesafe_forwarder_on(
        dataclasses.replace(settings, live_browser_sidecar=False),
        tenant_id="eng", profile=live, backend="typesafe")
    assert not typesafe_forwarder_on(
        dataclasses.replace(settings, typesafe_forwarder_enabled=False),
        tenant_id="eng", profile=live, backend="typesafe")


def test_a_cloud_run_job_gets_no_label(tenant):
    """GKE only: a Cloud Run Job keeps today's path even with every switch on."""
    settings = _settings(forwarder=True, tenants=("eng",), live=True)
    job = CloudRunJobDispatcher(settings, client=FakeJobsClient())._build_job(
        RUNNER_PROFILES[LIVE], tenant)
    assert "swarm-typesafe" not in dict(job.labels)
    assert "swarm-typesafe" not in dict(job.template.labels or {})


def test_a_cloud_run_job_never_switches_the_worker(tenant):
    """Neither the Job's template env nor the per-execution override env
    (`worker_env`) carries the switch, with every switch on: the forwarder is
    a cluster Service a Cloud Run Job cannot reach, and the worker would also
    refuse it off GKE."""
    settings = _settings(forwarder=True, tenants=("eng",), live=True)
    job = CloudRunJobDispatcher(settings, client=FakeJobsClient())._build_job(
        RUNNER_PROFILES[LIVE], tenant)
    template_env = [e.name for c in job.template.template.containers for e in c.env]
    assert SWITCH[0] not in template_env
    task = make_task(LIVE)
    assert SWITCH[0] not in worker_env(task=task, lease=make_lease(task), tenant=tenant,
                                       settings=settings)


def test_the_switch_ships_off_and_reads_its_environment(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("REGION", "us-central1")
    for name in ("TYPESAFE_FORWARDER_ENABLED", "TYPESAFE_FORWARDER_TENANTS"):
        monkeypatch.delenv(name, raising=False)
    off = SchedulerSettings.from_env()
    assert off.typesafe_forwarder_enabled is False
    assert off.typesafe_forwarder_tenants == ()
    monkeypatch.setenv("TYPESAFE_FORWARDER_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_FORWARDER_TENANTS", "eng, u-alice")
    on = SchedulerSettings.from_env()
    assert on.typesafe_forwarder_enabled is True
    assert on.typesafe_forwarder_tenants == ("eng", "u-alice")


# --- the workflow switch `jev_judgments: on` (2026-11-01) ---------------------


def _switched_pod(tenant: Tenant, *, dispatch: dict[str, Any] | None, profile: str = "claude-code",
                  backend: str = "typesafe", forwarder: bool = True,
                  tenants: tuple[str, ...] = ("eng",)) -> dict[str, Any]:
    settings = _settings(forwarder=forwarder, tenants=tenants, live=True)
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object(), judgment=lambda tenant_id: backend)
    task = make_task(profile)
    if dispatch is not None:
        task = dataclasses.replace(task, metadata={"dispatch": dispatch})
    job = dispatcher._manifest(task=task, lease=make_lease(task),
                               profile=RUNNER_PROFILES[profile], tenant=tenant)
    return job["spec"]["template"]


def _worker(template: dict[str, Any]) -> dict[str, Any]:
    (worker,) = [c for c in template["spec"]["containers"] if c["name"] == "worker"]
    return worker


def test_judgments_switched_on_label_a_claude_code_pod_and_switch_its_worker(tenant):
    """The worker resolves the workspace's TypeSafe key for the judgments of a
    step whose signed switch says on; with the forwarder serving the tenant,
    the shim serves it, as it does the browser's."""
    template = _switched_pod(tenant, dispatch={"jev_judgments": "on"})
    # MUTATION: drop the `elif jev_judgments_switched_on(task)` branch and
    # this pod is unlabelled.
    assert template["metadata"]["labels"]["swarm-typesafe"] == "inject"
    assert _switch_entries(_worker(template)["env"]) == [
        {"name": SWITCH[0], "value": SWITCH[1]}]


@pytest.mark.parametrize("dispatch, change", [
    pytest.param(None, {}, id="no-block"),
    pytest.param({"strategy": "collect"}, {}, id="no-switch"),
    pytest.param({"jev_judgments": "off"}, {}, id="switch-off"),
    pytest.param({"jev_judgments": True}, {}, id="not-the-word"),
    pytest.param({"jev_judgments": "on"}, {"backend": "internal-jev"}, id="internal-backend"),
    pytest.param({"jev_judgments": "on"}, {"forwarder": False}, id="forwarder-off"),
    pytest.param({"jev_judgments": "on"}, {"tenants": ("research",)}, id="tenant-not-listed"),
])
def test_anything_else_leaves_a_claude_code_pod_as_it_was(tenant, dispatch, change):
    template = _switched_pod(tenant, dispatch=dispatch, **change)
    assert "swarm-typesafe" not in template["metadata"]["labels"]
    assert _switch_entries(_worker(template)["env"]) == []


def test_the_switch_reaches_only_a_profile_that_carries_the_jev_client():
    settings = _settings(forwarder=True, tenants=("eng",), live=False)
    profile = RUNNER_PROFILES["claude-code"]
    assert not typesafe_forwarder_on(settings, tenant_id="eng", profile=profile,
                                     backend="typesafe")
    assert typesafe_forwarder_on(settings, tenant_id="eng", profile=profile,
                                 backend="typesafe", judgments_switched_on=True)
    mock = RUNNER_PROFILES["mock"]
    assert not typesafe_forwarder_on(settings, tenant_id="eng", profile=mock,
                                     backend="typesafe", judgments_switched_on=True)
