"""Jev on the coding profiles, the scheduler's half (lane jev-on-coding-profiles,
the owner's choice of 4 November 2026; part of #2688).

On the TypeSafe backend a pod's way to Jev is a label the GKE render gives it:
`swarm-typesafe: inject` with SWARM_TYPESAFE_INJECT=on when the forwarder
serves the tenant (its ingress, allow-typesafe-fwd.yaml, selects that label;
the worker then serves the loopback shim the judge calls), otherwise
`swarm-jev: typesafe-direct` (allow-typesafe-direct.yaml). A live browser got
them from its profile; a claude-code or claude-code-large pod got them only
under the workflow switch `jev_judgments: on`, never under the workspace's
signed `code_intelligence.jev` alone -- so on a forwarder tenant its worker
had no shim, and its judge no key. Now the render follows
`jev_judgments_run`: the signed opt-in, or the switch, never `off`.

The worker's half: tests/unit/worker/test_jev_on_coding_profiles.py.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES

from scheduler.dispatch import (
    TYPESAFE_DIRECT_POD_LABEL,
    TYPESAFE_INJECT_ENV,
    TYPESAFE_POD_LABEL,
    GkeJobDispatcher,
    GkeTarget,
    JevJudgments,
    jev_judgments_run,
)
from scheduler.judgment import judgments_env

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import NOW, make_lease, make_task

CODING = ("claude-code", "claude-code-large")


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng", kind="group", principal="eng@example.com", created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def _reading(on: bool, backend: str = "typesafe") -> JevJudgments:
    return JevJudgments(judgments_env(on, None), on, backend)


def _pod(tenant: Tenant, profile: str, *, signed_on: bool, forwarder: bool,
         backend: str = "typesafe", dispatch: dict[str, Any] | None = None
         ) -> tuple[dict[str, str], dict[str, str]]:
    settings = scheduler_settings(
        live_browser_sidecar=True,
        typesafe_forwarder_enabled=forwarder,
        typesafe_forwarder_tenants=("eng",),
    )
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object(), judgment=lambda tenant_id: backend,
                                  jev_judgments=lambda tenant_id: _reading(signed_on, backend))
    task = make_task(profile)
    if dispatch is not None:
        task = dataclasses.replace(task, metadata={"dispatch": dispatch})
    job = dispatcher._manifest(task=task, lease=make_lease(task),
                               profile=RUNNER_PROFILES[profile], tenant=tenant)
    template = job["spec"]["template"]
    (worker,) = [c for c in template["spec"]["containers"] if c["name"] == "worker"]
    env = {e["name"]: e.get("value") for e in worker["env"]}
    return template["metadata"]["labels"], env


@pytest.mark.parametrize("profile", CODING)
def test_a_signed_opt_in_coding_pod_on_a_forwarder_tenant_is_forwarded(tenant, profile):
    """MUTATION: put back `jev_judgments_switched_on(task)` in `_manifest`'s
    branch and neither the label nor the variable is set."""
    labels, env = _pod(tenant, profile, signed_on=True, forwarder=True)
    assert labels.get(TYPESAFE_POD_LABEL[0]) == TYPESAFE_POD_LABEL[1]
    assert env.get(TYPESAFE_INJECT_ENV[0]) == TYPESAFE_INJECT_ENV[1]
    assert TYPESAFE_DIRECT_POD_LABEL[0] not in labels


@pytest.mark.parametrize("profile", CODING)
def test_a_signed_opt_in_coding_pod_without_the_forwarder_is_labelled_direct(tenant, profile):
    labels, env = _pod(tenant, profile, signed_on=True, forwarder=False)
    assert labels.get(TYPESAFE_DIRECT_POD_LABEL[0]) == TYPESAFE_DIRECT_POD_LABEL[1]
    assert TYPESAFE_POD_LABEL[0] not in labels
    assert TYPESAFE_INJECT_ENV[0] not in env


@pytest.mark.parametrize("profile", CODING)
def test_a_coding_pod_gets_the_route_a_browser_pod_gets(tenant, profile):
    for forwarder in (True, False):
        coding, _ = _pod(tenant, profile, signed_on=True, forwarder=forwarder)
        browser, _ = _pod(tenant, "claude-code-browser", signed_on=True, forwarder=forwarder)
        for label in (TYPESAFE_POD_LABEL, TYPESAFE_DIRECT_POD_LABEL):
            assert coding.get(label[0]) == browser.get(label[0]), (forwarder, label)


@pytest.mark.parametrize("change", [
    pytest.param({"signed_on": False}, id="not-opted-in"),
    pytest.param({"dispatch": {"jev_judgments": "off"}}, id="switch-off"),
    pytest.param({"backend": "internal-jev"}, id="internal-backend"),
])
@pytest.mark.parametrize("forwarder", [True, False])
def test_no_typesafe_route_where_the_step_does_not_judge_on_typesafe(
        tenant, change, forwarder):
    kwargs: dict[str, Any] = {"signed_on": True, "forwarder": forwarder, **change}
    labels, env = _pod(tenant, "claude-code", **kwargs)
    assert TYPESAFE_POD_LABEL[0] not in labels
    assert TYPESAFE_DIRECT_POD_LABEL[0] not in labels
    assert TYPESAFE_INJECT_ENV[0] not in env


def test_jev_judgments_run_reads_the_signed_reading_and_the_switch():
    task = make_task("claude-code")
    assert jev_judgments_run(_reading(True), task)
    assert not jev_judgments_run(_reading(False), task)
    assert not jev_judgments_run(None, task)
    on = dataclasses.replace(task, metadata={"dispatch": {"jev_judgments": "on"}})
    off = dataclasses.replace(task, metadata={"dispatch": {"jev_judgments": "off"}})
    assert jev_judgments_run(_reading(False), on)
    assert not jev_judgments_run(_reading(True), off)
