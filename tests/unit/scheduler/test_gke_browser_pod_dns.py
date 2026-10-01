"""A browser worker pod resolves names WITHOUT the cluster's search path (#341).

THE DEFECT THIS PINS. GKE's default pod resolver (`dnsPolicy: ClusterFirst`)
writes `ndots:5` and the search list `<ns>.svc.cluster.local svc.cluster.local
cluster.local ...`, so a short name a browser task opens -- `kubernetes.default`,
`swarm-api.swarm-system` -- is tried against every cluster suffix and reaches a
cluster Service. `url_refusal` cannot close that: it sees the string a caller
typed, never what the pod's resolver does with it.

Owner decision 2026-09-29: the browser pod carries
`dnsConfig: {options: [{name: ndots, value: "1"}], searches: []}` under a
`dnsPolicy` that does NOT merge in the cluster's search domains. Under
`ClusterFirst` a pod's `searches` are APPENDED to the cluster's own, so `[]`
there removes nothing; `None` is the policy under which the pod's dnsConfig is
the whole resolver, and it then needs the nameserver named -- the cluster's
NodeLocal DNSCache address, which is what a ClusterFirst pod on swarm-autopilot
is already given, so fully qualified names resolve exactly as before.

Both producers of the browser Job are held to it: GkeJobDispatcher._manifest,
which is what runs, and kubernetes/worker-templates/worker-job-browser.yaml,
which is what an operator reads and `make lint` renders.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from swarm_common.config import Settings
from swarm_common.models import Lease, Task, Tenant
from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend
from swarm_common.states import TaskState

from scheduler.dispatch import (
    GKE_POD_DNS_CONFIG,
    GKE_POD_DNS_POLICY,
    GkeJobDispatcher,
    GkeTarget,
)
from scheduler.settings import SchedulerSettings

ROOT = Path(__file__).resolve().parents[3]
BROWSER_TEMPLATE = ROOT / "kubernetes" / "worker-templates" / "worker-job-browser.yaml"
EGRESS_POLICY = ROOT / "kubernetes" / "network-policies" / "allow-egress.yaml"
PROJECT = "saga-agents-staging"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

WANTED_OPTIONS = [{"name": "ndots", "value": "1"}]


def _gke_profiles() -> list[str]:
    return sorted(
        name for name, profile in RUNNER_PROFILES.items()
        if resolve_backend(profile) is Backend.GKE_AUTOPILOT
    )


def _pod_spec(profile_name: str) -> dict:
    profile = RUNNER_PROFILES[profile_name]
    settings = SchedulerSettings(
        core=Settings(project_id=PROJECT, artifact_bucket=f"{PROJECT}-swarm-artifacts"),
        project_id=PROJECT,
        region="us-central1",
        artifact_registry_host=f"us-central1-docker.pkg.dev/{PROJECT}/swarm-images",
        worker_image_tag="test",
    )
    tenant = Tenant(
        tenant_id="eng",
        kind="group",
        principal="eng@saga.xyz",
        created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        namespace="swarm-tenant-eng",
    )
    task = Task(
        id="task_dns341",
        tenant_id="eng",
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.LEASED,
        runner_profile=profile_name,
        resource_class=profile.resource_class,
        input={},
        submitted_by="alice@saga.xyz",
        provider=profile.provider,
        timeout_seconds=profile.timeout_seconds,
    )
    lease = Lease(
        lease_id="lease_1",
        task_id=task.id,
        attempt_id="att_1",
        tenant_id="eng",
        generation=1,
        pools=["global"],
        units=1,
        state=TaskState.LEASED,
        created_at=NOW,
        dispatch_deadline=NOW + timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=2),
    )
    dispatcher = GkeJobDispatcher(
        settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=object()
    )
    job = dispatcher._manifest(task=task, lease=lease, profile=profile, tenant=tenant)
    return job["spec"]["template"]["spec"]


def _template_pod_spec() -> dict:
    # The placeholders are plain scalars, so the template parses unrendered.
    return yaml.safe_load(BROWSER_TEMPLATE.read_text())["spec"]["template"]["spec"]


def test_browser_is_a_gke_profile():
    """Everything below is parametrised over the GKE profiles; with none it
    would check nothing."""
    assert "browser" in _gke_profiles()


@pytest.mark.parametrize("profile_name", _gke_profiles())
def test_the_dispatched_browser_pod_drops_the_cluster_search_path(profile_name):
    pod = _pod_spec(profile_name)
    assert pod.get("dnsPolicy") == "None", (
        "under ClusterFirst a pod's `searches` are appended to the cluster's, so "
        "`searches: []` removes nothing; only dnsPolicy None makes it the whole list"
    )
    config = pod.get("dnsConfig") or {}
    assert config.get("searches") == []
    assert config.get("options") == WANTED_OPTIONS


@pytest.mark.parametrize("profile_name", _gke_profiles())
def test_the_dispatched_browser_pod_still_has_a_cluster_resolver(profile_name):
    """dnsPolicy None with no nameserver is refused by the API server, and one
    that is not the cluster's resolver would stop every name resolving."""
    nameservers = (_pod_spec(profile_name).get("dnsConfig") or {}).get("nameservers")
    assert nameservers, "dnsPolicy None needs at least one nameserver"
    for server in nameservers:
        address = ipaddress.ip_address(server)
        assert address.is_link_local or address.is_private, (
            f"{server} is not a cluster-local resolver; a public resolver would "
            "bypass the cluster's DNS and its NetworkPolicy rule 1b"
        )


def test_the_yaml_browser_template_carries_the_same_dns():
    pod = _template_pod_spec()
    assert pod.get("dnsPolicy") == "None"
    config = pod.get("dnsConfig") or {}
    assert config.get("searches") == []
    assert config.get("options") == WANTED_OPTIONS


@pytest.mark.parametrize("profile_name", _gke_profiles())
def test_the_template_and_the_dispatcher_agree_on_dns(profile_name):
    dispatched = _pod_spec(profile_name)
    template = _template_pod_spec()
    assert template.get("dnsPolicy") == dispatched.get("dnsPolicy") == GKE_POD_DNS_POLICY
    assert template.get("dnsConfig") == dispatched.get("dnsConfig") == GKE_POD_DNS_CONFIG


def test_the_nameserver_is_one_the_tenant_egress_policy_opens():
    """The tenant NetworkPolicy opens DNS to the RENDERED NodeLocal DNSCache
    address (rule 1b). The address this pod is told to use is the one recorded
    there as the nameserver a pod on swarm-autopilot is given; if that record
    changes, this pod's nameserver must change with it."""
    text = EGRESS_POLICY.read_text()
    for server in GKE_POD_DNS_CONFIG["nameservers"]:
        assert f"A pod's nameserver is {server}." in " ".join(text.replace("#", " ").split())
