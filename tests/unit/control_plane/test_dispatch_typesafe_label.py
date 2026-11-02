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


# --- the quality gates (the owner's pick "Label quality-gate pods too", 2026-11-02) ---
#
# jev-belay and jev-enforce call TypeSafe with the WORKSPACE's key. With the
# forwarder serving the tenant on the TypeSafe backend, a claude-code pod whose
# signed `quality_gates.jev` the scheduler vouched for carries the label and
# the worker's switch, so `qualitygates.prepare` names the shim and writes no
# key file. The verdict is the scheduler's own reading of the task's tenant's
# signed block (`judgment.read_quality_gates_env`), never the pod's.


def _gated_pod(tenant: Tenant, *, verdict: str, dispatch: dict[str, Any] | None = None,
               profile: str = "claude-code", backend: str = "typesafe", forwarder: bool = True,
               tenants: tuple[str, ...] = ("eng",), asked: list[str] | None = None
               ) -> dict[str, Any]:
    settings = _settings(forwarder=forwarder, tenants=tenants, live=True)

    def gates(tenant_id: str) -> dict[str, str]:
        if asked is not None:
            asked.append(tenant_id)
        return {"SWARM_QUALITY_GATES_JEV": verdict}

    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object(), judgment=lambda tenant_id: backend,
                                  quality_gates=gates)
    task = make_task(profile)
    if dispatch is not None:
        task = dataclasses.replace(task, metadata={"dispatch": dispatch})
    job = dispatcher._manifest(task=task, lease=make_lease(task),
                               profile=RUNNER_PROFILES[profile], tenant=tenant)
    return job["spec"]["template"]


@pytest.mark.parametrize("verdict, dispatch", [
    pytest.param("true", None, id="signed-true"),
    pytest.param("true", {"quality_gates": "on"}, id="signed-true-switch-on"),
    pytest.param("on_request", {"quality_gates": "on"}, id="signed-on-request-switch-on"),
])
def test_signed_quality_gates_label_a_claude_code_pod_and_switch_its_worker(
        tenant, verdict, dispatch):
    asked: list[str] = []
    template = _gated_pod(tenant, verdict=verdict, dispatch=dispatch, asked=asked)
    # MUTATION: drop the quality gates from the forwarder decision and this
    # pod is unlabelled, so the worker writes private/jev-gates.key.
    assert template["metadata"]["labels"]["swarm-typesafe"] == "inject"
    assert _switch_entries(_worker(template)["env"]) == [
        {"name": SWITCH[0], "value": SWITCH[1]}]
    # The setting read is the task's own tenant's, and only that.
    assert asked == ["eng"]


@pytest.mark.parametrize("verdict, dispatch, change", [
    pytest.param("false", None, {}, id="verdict-off"),
    pytest.param("on_request", None, {}, id="on-request-without-the-switch"),
    pytest.param("on_request", {"quality_gates": "off"}, {}, id="on-request-switch-off"),
    pytest.param("true", {"quality_gates": "off"}, {}, id="switch-off-narrows"),
    pytest.param("TRUE", None, {}, id="not-the-schedulers-word"),
    pytest.param("true", None, {"backend": "internal-jev"}, id="internal-backend"),
    pytest.param("true", None, {"forwarder": False}, id="forwarder-off"),
    pytest.param("true", None, {"tenants": ("research",)}, id="tenant-not-served"),
    pytest.param("true", None, {"profile": "codex"}, id="no-gates-on-codex"),
])
def test_any_gate_condition_off_leaves_the_pod_on_todays_path(tenant, verdict, dispatch, change):
    template = _gated_pod(tenant, verdict=verdict, dispatch=dispatch, **change)
    assert "swarm-typesafe" not in template["metadata"]["labels"]
    assert _switch_entries(_worker(template)["env"]) == []


def test_gates_and_judgments_together_switch_the_worker_once(tenant):
    template = _gated_pod(tenant, verdict="true", dispatch={"jev_judgments": "on"})
    assert template["metadata"]["labels"]["swarm-typesafe"] == "inject"
    assert _switch_entries(_worker(template)["env"]) == [
        {"name": SWITCH[0], "value": SWITCH[1]}]


def test_a_live_browser_pod_with_the_gates_is_switched_once(tenant):
    settings = _settings(forwarder=True, tenants=("eng",), live=True)
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object(), judgment=lambda tenant_id: "typesafe",
                                  quality_gates=lambda t: {"SWARM_QUALITY_GATES_JEV": "true"})
    task = make_task(LIVE)
    template = dispatcher._manifest(task=task, lease=make_lease(task),
                                    profile=RUNNER_PROFILES[LIVE], tenant=tenant)["spec"]["template"]
    assert template["metadata"]["labels"]["swarm-typesafe"] == "inject"
    assert _switch_entries(_worker(template)["env"]) == [
        {"name": SWITCH[0], "value": SWITCH[1]}]


def test_the_gates_reach_only_the_claude_code_runner():
    settings = _settings(forwarder=True, tenants=("eng",), live=False)
    assert typesafe_forwarder_on(settings, tenant_id="eng", profile=RUNNER_PROFILES["claude-code"],
                                 backend="typesafe", quality_gates_on=True)
    assert not typesafe_forwarder_on(settings, tenant_id="eng", profile=RUNNER_PROFILES["codex"],
                                     backend="typesafe", quality_gates_on=True)
    assert not typesafe_forwarder_on(settings, tenant_id="eng",
                                     profile=RUNNER_PROFILES["claude-code"],
                                     backend="internal-jev", quality_gates_on=True)


@pytest.mark.parametrize("env, dispatch, on", [
    ({"SWARM_QUALITY_GATES_JEV": "true"}, None, True),
    ({"SWARM_QUALITY_GATES_JEV": "true"}, {"quality_gates": "off"}, False),
    ({"SWARM_QUALITY_GATES_JEV": "on_request"}, {"quality_gates": "on"}, True),
    ({"SWARM_QUALITY_GATES_JEV": "on_request"}, {"quality_gates": True}, False),
    ({"SWARM_QUALITY_GATES_JEV": "on_request"}, None, False),
    ({"SWARM_QUALITY_GATES_JEV": "false", "SWARM_QUALITY_GATES_REFUSED": "unsigned"}, None, False),
    ({}, None, False),
])
def test_the_gates_run_exactly_where_the_worker_runs_them(env, dispatch, on):
    """`quality_gates_run` is the worker's `qualitygates.decide` over the
    scheduler's verdict: the switch only narrows."""
    from scheduler.dispatch import quality_gates_run

    task = make_task("claude-code")
    if dispatch is not None:
        task = dataclasses.replace(task, metadata={"dispatch": dispatch})
    assert quality_gates_run(env, task) is on


def test_the_gates_switch_is_the_one_the_worker_reads():
    from agent_worker import qualitygates
    from scheduler.dispatch import QUALITY_GATES_SWITCH

    assert QUALITY_GATES_SWITCH == qualitygates.SWITCH
    assert (qualitygates.ON, qualitygates.OFF) == ("on", "off")
    assert qualitygates.ON_REQUEST == "on_request"
