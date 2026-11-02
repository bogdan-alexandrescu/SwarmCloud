"""A pod that calls TypeSafe DIRECTLY carries `swarm-jev: typesafe-direct`, and
no other pod does (the TypeSafe egress allowlist, docs/runbooks/typesafe-egress.md;
part of #2688).

The label is what `swarm-allow-typesafe-direct`
(kubernetes/network-policies/fqdn/allow-typesafe-direct.yaml) selects: the one
tenant-side FQDN rule that names api.typesafe.ai. The dispatcher sets it from
the pod it has just rendered, and from nothing a caller sent (invariant 10):

  * the worker's environment says the pod will call TypeSafe itself -- the
    live browser's SWARM_JEV_BACKEND is `typesafe` (the workspace's stored
    judgment backend), or the quality gates' verdict SWARM_QUALITY_GATES_JEV
    is on (`true` or `on_request`), whose hooks always call TypeSafe;
  * AND the pod is not labelled `swarm-typesafe: inject`. A forwarded pod
    reaches TypeSafe only through swarm-typesafe-fwd: it has no direct
    allow of its own.

MUTATION: label an inject pod too, drop either source of "calls TypeSafe",
or label an internal-backend pod, and the named case fails.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES

from scheduler.dispatch import (
    TYPESAFE_DIRECT_POD_LABEL,
    CloudRunJobDispatcher,
    GkeJobDispatcher,
    GkeTarget,
)
from scheduler.judgment import QUALITY_GATES_JEV_ENV, quality_gates_env

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import NOW, FakeJobsClient, make_lease, make_task

LIVE = "claude-code-browser"
DIRECT = {"swarm-jev": "typesafe-direct"}


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng", kind="group", principal="eng@example.com", created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def _template(tenant: Tenant, *, profile: str = LIVE, backend: str = "typesafe",
              forwarder: bool = False, gates: str | None = None) -> dict[str, Any]:
    settings = scheduler_settings(
        live_browser_sidecar=True,
        typesafe_forwarder_enabled=forwarder,
        typesafe_forwarder_tenants=("eng",),
    )
    on: bool | str = {"true": True, "on_request": "on_request"}.get(gates or "", False)
    verdict = (lambda tenant_id: quality_gates_env(on, None)) if gates else None
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object(), judgment=lambda tenant_id: backend,
                                  quality_gates=verdict)
    task = make_task(profile)
    job = dispatcher._manifest(task=task, lease=make_lease(task),
                               profile=RUNNER_PROFILES[profile], tenant=tenant)
    return job["spec"]["template"]


def _labels(tenant: Tenant, **kwargs: Any) -> dict[str, str]:
    return _template(tenant, **kwargs)["metadata"]["labels"]


def test_the_label_is_the_one_the_fqdn_rule_selects():
    assert dict([TYPESAFE_DIRECT_POD_LABEL]) == DIRECT


def test_a_typesafe_backed_live_pod_without_the_forwarder_is_labelled_direct(tenant):
    labels = _labels(tenant)
    assert labels["swarm-jev"] == "typesafe-direct"
    assert "swarm-typesafe" not in labels


def test_a_forwarded_pod_has_no_direct_label(tenant):
    """With the forwarder serving the tenant the pod reaches TypeSafe only
    through swarm-typesafe-fwd: inject, never direct."""
    labels = _labels(tenant, forwarder=True)
    assert labels["swarm-typesafe"] == "inject"
    assert "swarm-jev" not in labels


@pytest.mark.parametrize("change", [
    pytest.param({"backend": "internal-jev"}, id="internal-jev"),
    pytest.param({"backend": "internal-laya"}, id="internal-laya"),
    pytest.param({"profile": "claude-code"}, id="not-live-and-no-gates"),
    pytest.param({"profile": "claude-code", "gates": "false"}, id="gates-off"),
])
def test_a_pod_that_does_not_call_typesafe_is_not_labelled(tenant, change):
    assert "swarm-jev" not in _labels(tenant, **change)


@pytest.mark.parametrize("word", ["true", "on_request"])
def test_a_pod_whose_quality_gates_are_on_is_labelled_direct(tenant, word):
    """The lane hooks call https://api.typesafe.ai themselves whatever the
    judgment backend is (docs/jev-data-flow.md, The lane hooks)."""
    template = _template(tenant, profile="claude-code", backend="internal-jev", gates=word)
    (worker,) = [c for c in template["spec"]["containers"] if c["name"] == "worker"]
    assert {"name": QUALITY_GATES_JEV_ENV, "value": word} in worker["env"]
    assert template["metadata"]["labels"]["swarm-jev"] == "typesafe-direct"


def test_gates_on_a_forwarded_pod_still_get_no_direct_label(tenant):
    """The forwarder's switch hands the gates the loopback shim too."""
    labels = _labels(tenant, forwarder=True, gates="true")
    assert labels["swarm-typesafe"] == "inject"
    assert "swarm-jev" not in labels


def test_the_label_changes_nothing_else_on_the_pod(tenant):
    direct = _labels(tenant)
    internal = _labels(tenant, backend="internal-jev")
    # The internal pod also carries no `swarm-judge` here: no tailnet URL is set.
    assert {k: v for k, v in direct.items() if k != "swarm-jev"} == internal


def test_a_cloud_run_job_gets_no_label(tenant):
    """A Cloud Run Job is under no NetworkPolicy, so there is nothing to select."""
    settings = scheduler_settings(live_browser_sidecar=True)
    job = CloudRunJobDispatcher(settings, client=FakeJobsClient())._build_job(
        RUNNER_PROFILES[LIVE], tenant)
    assert "swarm-jev" not in dict(job.labels)
    assert "swarm-jev" not in dict(job.template.labels or {})
