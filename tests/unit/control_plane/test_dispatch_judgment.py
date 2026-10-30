"""A live-browser pod's Jev goes to the backend its workspace chose (JUDGE-SELECT).

Owner decisions 2026-10-14. The dispatcher reads the task tenant's
`workspace_settings/{tenant}.judgment_backend` (default internal-jev) and:

  * internal-jev / internal-laya, with the tailnet egress on (the platform's
    INTERNAL_DECISION_API_URL): JEV_API_URL is the decision API's
    `/jev/v1/decide` or `/laya/v1/decide` BY ITS TAILNET NAME over https, the
    pod carries `swarm-judge: internal`, the label JUDGE-NET's NetworkPolicy
    opens the decision API to, and a hostAliases entry pinning the tailnet
    name to the egress Service's fixed clusterIP (owner decision 2026-10-15,
    #1380) -- all three together or none;
  * internal with the egress off: the backend is named and NO URL is set, so
    the jev wrapper refuses rather than sending page text to TypeSafe instead;
  * typesafe: no JEV_API_URL, no label and no hostAliases -- jev-browser's own
    TypeSafe default.

SWARM_JEV_BACKEND names the choice on every live-browser pod. A pod of any
other profile, or a live-browser pod while the display rollout switch is off
(no live browser, so no jev), is untouched.

Every restatement of the tailnet origin, the tailnet name and the Service's
clusterIP -- swarm-api's, terraform's, the manifest's and the jev wrapper's --
is held equal to this module's here.

MUTATION: give the label or the hostAliases entry to a typesafe pod, set
JEV_API_URL with the egress off, or change the IP in one place only, and the
named test fails.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest

from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES

from scheduler.dispatch import (
    TYPESAFE_INJECT_ENV,
    TYPESAFE_POD_LABEL,
    GkeJobDispatcher,
    GkeTarget,
    build_router,
    read_judge_backend,
)
from scheduler.judgment import (
    BACKENDS,
    DECISION_API_CLUSTER_IP,
    DECISION_API_TAILNET_NAME,
    DEFAULT_BACKEND,
    INTERNAL_DECISION_ORIGIN,
    JEV_API_URL_ENV,
    JEV_BACKEND_ENV,
    JUDGE_POD_LABEL,
    SETTINGS_COLLECTION,
    TYPESAFE_BACKEND,
    jev_dispatch,
    read_backend,
)
from scheduler.settings import SchedulerSettings

from .conftest import PROJECT, scheduler_settings
from .fakes import FakeFirestore
from .test_dispatch_manifests import NOW, make_lease, make_task

REPO = Path(__file__).resolve().parents[3]
TAILSCALE_TF = REPO / "terraform" / "infra" / "tailscale.tf"
LOCALS_TF = REPO / "terraform" / "infra" / "locals.tf"
WRAPPER = REPO / "images" / "agent-runtime-browser" / "jev-browser" / "bin" / "jev-browser-mcp"

LIVE = "claude-code-browser"
#: What #1380 measured on 2026-10-15: these are the URLs that work.
JEV_URL = "https://decision-api-staging.taile0bda9.ts.net/jev/v1/decide"
LAYA_URL = "https://decision-api-staging.taile0bda9.ts.net/laya/v1/decide"
PINNED_IP = "10.48.6.120"
HOST_ALIASES = [{"ip": PINNED_IP, "hostnames": ["decision-api-staging.taile0bda9.ts.net"]}]
#: The URL the code sent before #1380: the Service has no port 80.
OLD_SERVICE = "http://decision-api.swarm-system.svc.cluster.local"


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng", kind="group", principal="eng@example.com", created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def _job(tenant: Tenant, backend: str | None, *, profile: str = LIVE, live: bool = True,
         internal: bool = True, task_input: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = scheduler_settings(
        live_browser_sidecar=live,
        internal_decision_api_url=INTERNAL_DECISION_ORIGIN if internal else "")
    judgment = None if backend is None else (lambda tenant_id: backend)
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object(), judgment=judgment)
    task = make_task(profile)
    if task_input is not None:
        task = dataclasses.replace(task, input=task_input)
    return dispatcher._manifest(task=task, lease=make_lease(task),
                                profile=RUNNER_PROFILES[profile], tenant=tenant)


def _worker_env(job: dict[str, Any]) -> dict[str, str]:
    spec = job["spec"]["template"]["spec"]
    (worker,) = [c for c in spec["containers"] if c["name"] == "worker"]
    return {e["name"]: e["value"] for e in worker["env"]}


def _pod_labels(job: dict[str, Any]) -> dict[str, str]:
    return job["spec"]["template"]["metadata"]["labels"]


def _host_aliases(job: dict[str, Any]) -> list[dict[str, Any]] | None:
    return job["spec"]["template"]["spec"].get("hostAliases")


# -- the pure decision ------------------------------------------------------------

@pytest.mark.parametrize("backend,internal,env,labelled", [
    ("internal-jev", True, {JEV_BACKEND_ENV: "internal-jev", JEV_API_URL_ENV: JEV_URL}, True),
    ("internal-laya", True, {JEV_BACKEND_ENV: "internal-laya", JEV_API_URL_ENV: LAYA_URL}, True),
    ("typesafe", True, {JEV_BACKEND_ENV: "typesafe"}, False),
    ("internal-jev", False, {JEV_BACKEND_ENV: "internal-jev"}, False),
    ("internal-laya", False, {JEV_BACKEND_ENV: "internal-laya"}, False),
    ("typesafe", False, {JEV_BACKEND_ENV: "typesafe"}, False),
])
def test_env_and_label_per_backend(backend, internal, env, labelled):
    got_env, got_labels, got_aliases = jev_dispatch(
        backend, INTERNAL_DECISION_ORIGIN if internal else "")
    assert got_env == env
    assert got_labels == (dict([JUDGE_POD_LABEL]) if labelled else {})
    # The hostAliases entry comes with the label and only with it.
    assert got_aliases == (HOST_ALIASES if labelled else [])


def test_an_unknown_stored_value_is_the_default_never_typesafe():
    env, labels, aliases = jev_dispatch("something-else", INTERNAL_DECISION_ORIGIN)
    assert env[JEV_BACKEND_ENV] == DEFAULT_BACKEND == "internal-jev"
    assert env[JEV_API_URL_ENV] == JEV_URL
    assert labels == dict([JUDGE_POD_LABEL]) == {"swarm-judge": "internal"}
    assert aliases == HOST_ALIASES


# -- the stored setting --------------------------------------------------------------

def test_the_reader_defaults_to_internal_jev_and_reads_the_stored_value():
    db = FakeFirestore()
    assert read_backend(db, "eng") == "internal-jev"
    db.docs[f"{SETTINGS_COLLECTION}/eng"] = {"judgment_backend": "typesafe"}
    assert read_backend(db, "eng") == "typesafe"
    db.docs[f"{SETTINGS_COLLECTION}/eng"] = {"judgment_backend": 7}
    assert read_backend(db, "eng") == "internal-jev"


def test_a_failed_read_is_the_default():
    class Broken:
        def collection(self, name):
            raise RuntimeError("firestore down")

    assert read_backend(Broken(), "eng") == "internal-jev"


def test_the_backends_are_swarm_apis():
    from swarm_api import judgment as api

    assert BACKENDS == api.BACKENDS
    assert DEFAULT_BACKEND == api.DEFAULT_BACKEND
    assert SETTINGS_COLLECTION == api.SETTINGS_COLLECTION
    assert INTERNAL_DECISION_ORIGIN == api.INTERNAL_DECISION_ORIGIN


# -- the pod -------------------------------------------------------------------------

@pytest.mark.parametrize("backend,url", [("internal-jev", JEV_URL), ("internal-laya", LAYA_URL)])
def test_an_internal_backend_pod_has_the_url_the_label_and_the_host_alias(tenant, backend, url):
    job = _job(tenant, backend)
    env = _worker_env(job)
    assert env[JEV_API_URL_ENV] == url
    assert env[JEV_BACKEND_ENV] == backend
    assert _pod_labels(job)["swarm-judge"] == "internal"
    assert _host_aliases(job) == HOST_ALIASES


def test_a_typesafe_pod_has_none_of_the_three(tenant):
    job = _job(tenant, "typesafe")
    env = _worker_env(job)
    assert JEV_API_URL_ENV not in env
    assert env[JEV_BACKEND_ENV] == "typesafe"
    assert "swarm-judge" not in _pod_labels(job)
    assert "swarm-judge" not in job["metadata"]["labels"]
    assert _host_aliases(job) is None


def test_internal_with_the_egress_off_names_the_backend_and_nothing_else(tenant):
    job = _job(tenant, "internal-jev", internal=False)
    env = _worker_env(job)
    assert env[JEV_BACKEND_ENV] == "internal-jev"
    assert JEV_API_URL_ENV not in env
    assert "swarm-judge" not in _pod_labels(job)
    assert _host_aliases(job) is None


def test_no_reader_is_the_default(tenant):
    job = _job(tenant, None)
    env = _worker_env(job)
    assert env[JEV_BACKEND_ENV] == "internal-jev"
    assert env[JEV_API_URL_ENV] == JEV_URL
    assert _host_aliases(job) == HOST_ALIASES


def test_the_url_and_the_ip_are_the_platforms_never_the_callers(tenant):
    """Invariants 7 and 10: a task's input naming another URL, host or IP
    changes nothing -- the URL comes from the scheduler's setting, the IP and
    the name from constants -- and the pod's resources stay the platform's:
    memory and disk requests == limits, CPU within its burst ratio (RS-1)."""
    sent = {"JEV_API_URL": "https://attacker.invalid/jev/v1/decide",
            "hostAliases": [{"ip": "10.0.0.1", "hostnames": [DECISION_API_TAILNET_NAME]}],
            "internal_decision_api_url": "https://attacker.invalid"}
    job = _job(tenant, "internal-jev", task_input=sent)
    assert job == _job(tenant, "internal-jev")
    assert _worker_env(job)[JEV_API_URL_ENV] == JEV_URL
    assert _host_aliases(job) == HOST_ALIASES
    for container in job["spec"]["template"]["spec"]["containers"]:
        resources = container.get("resources") or {}
        for resource in ("memory", "ephemeral-storage"):
            assert resources["requests"][resource] == resources["limits"][resource], container["name"]
        request, limit = (int(resources[d]["cpu"].removesuffix("m")) for d in ("requests", "limits"))
        assert request <= limit <= 2 * request, container["name"]


@pytest.mark.parametrize("profile,live", [("claude-code", True), ("browser", True), (LIVE, False)])
def test_no_live_browser_no_jev(tenant, profile, live):
    job = _job(tenant, "internal-jev", profile=profile, live=live)
    env = _worker_env(job)
    assert JEV_BACKEND_ENV not in env and JEV_API_URL_ENV not in env
    assert "swarm-judge" not in _pod_labels(job)


def test_the_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.setenv("INTERNAL_DECISION_API_URL", INTERNAL_DECISION_ORIGIN)
    assert SchedulerSettings.from_env().internal_decision_api_url == INTERNAL_DECISION_ORIGIN
    monkeypatch.delenv("INTERNAL_DECISION_API_URL")
    assert SchedulerSettings.from_env().internal_decision_api_url == ""


@pytest.mark.parametrize("value", [
    OLD_SERVICE,  # the old in-cluster http URL: no port 80 (#1380)
    "https://decision-api.swarm-system.svc.cluster.local",  # the certificate is the tailnet name's
    "http://decision-api-staging.taile0bda9.ts.net",  # the name without TLS
    "https://decision-api-staging.example.invalid",
    "https://decision-api-staging.taile0bda9.ts.net.attacker.invalid",
    f"https://{PINNED_IP}",
])
def test_a_url_other_than_the_tailnet_origin_is_refused(value):
    with pytest.raises(ValueError):
        scheduler_settings(internal_decision_api_url=value)


@pytest.mark.parametrize("value", ["", "https://decision-api-staging.taile0bda9.ts.net",
                                   "https://decision-api-staging.taile0bda9.ts.net/"])
def test_off_or_the_tailnet_origin_is_accepted(value):
    assert scheduler_settings(internal_decision_api_url=value).internal_decision_api_url == value


# -- one source each ------------------------------------------------------------------

def test_the_origin_is_the_tailnet_name_over_https():
    assert DECISION_API_TAILNET_NAME == "decision-api-staging.taile0bda9.ts.net"
    assert INTERNAL_DECISION_ORIGIN == f"https://{DECISION_API_TAILNET_NAME}"


def test_the_cluster_ip_is_terraforms_one_value():
    """tailscale.tf states the Service's clusterIP once and renders it into the
    manifest; the scheduler's copy, which every judging pod's hostAliases
    carries, is held equal to it."""
    tf = TAILSCALE_TF.read_text()
    stated = re.findall(r'^\s*decision_api_cluster_ip\s*=\s*"([^"]+)"', tf, re.M)
    assert stated == [DECISION_API_CLUSTER_IP] == [PINNED_IP]
    assert '"__DECISION_API_CLUSTER_IP__", local.decision_api_cluster_ip' in tf
    # Nowhere else in terraform/infra is the address written out.
    infra = [f for f in TAILSCALE_TF.parent.glob("*.tf") if PINNED_IP in f.read_text()]
    assert infra == [TAILSCALE_TF]
    assert tf.count(f'"{PINNED_IP}"') == 1


def test_the_tailnet_name_is_terraforms_egress_target():
    # No default since DAYONE-TF-1: dev.tfvars names the deployment's tailnet.
    tfvars = (TAILSCALE_TF.parents[1] / "environments" / "dev" / "dev.tfvars").read_text()
    assert f'tailscale_egress_target = "{DECISION_API_TAILNET_NAME}"' in tfvars


def test_terraforms_url_is_the_origin_built_from_the_egress_target():
    locals_tf = LOCALS_TF.read_text()
    assert ('internal_decision_api_url = var.internal_decision_api_enabled ? '
            '"https://${var.tailscale_egress_target}" : ""') in locals_tf
    assert "decision-api.swarm-system.svc.cluster.local" not in locals_tf


def test_the_jev_wrapper_accepts_the_same_origin():
    (stated,) = re.findall(r'^DECISION_SERVICE="([^"]+)"$', WRAPPER.read_text(), re.M)
    assert stated == INTERNAL_DECISION_ORIGIN


# -- the pod reads the backend the router reads (#1334 c31) ------------------------

@pytest.mark.parametrize("doc", [
    pytest.param({"code_intelligence": {"judgment_backend": "typesafe"},
                  "judgment_backend": "internal-jev"}, id="nested-typesafe-top-internal"),
    pytest.param({"code_intelligence": {"judgment_backend": "internal-laya"},
                  "judgment_backend": "typesafe"}, id="nested-internal-top-typesafe"),
    pytest.param({"code_intelligence": {"judgment_backend": "typesafe"}},
                 id="nested-typesafe-no-top"),
])
def test_the_gke_pod_reads_the_backend_the_router_reads(monkeypatch, tenant, doc):
    """`build_router` wires one reading into both halves: the router's
    `typesafe_judge_needs_gke` and the GKE pod's SWARM_JEV_BACKEND, swarm-judge
    and swarm-typesafe label. Read with two precedences, a workspace whose
    `code_intelligence.judgment_backend` is typesafe was routed to GKE for a
    TypeSafe judge and then rendered a pod that judges internally (and the
    reverse).

    MUTATION: wire GkeJobDispatcher's `judgment` back to `read_backend`
    (top-level only) and every case fails on SWARM_JEV_BACKEND."""
    for name in ("GKE_ENDPOINT", "GKE_CA_CERT_B64"):
        monkeypatch.delenv(name, raising=False)
    settings = scheduler_settings(
        live_browser_sidecar=True, internal_decision_api_url=INTERNAL_DECISION_ORIGIN,
        typesafe_forwarder_enabled=True, typesafe_forwarder_tenants=("eng",))
    db = FakeFirestore()
    db.docs[f"{SETTINGS_COLLECTION}/eng"] = doc
    router_reads = read_judge_backend(db, "eng")
    gke = build_router(settings, db=db)._gke
    task = make_task(LIVE)
    job = gke._manifest(task=task, lease=make_lease(task), profile=RUNNER_PROFILES[LIVE],
                        tenant=tenant)
    env, labels = _worker_env(job), _pod_labels(job)
    assert env[JEV_BACKEND_ENV] == router_reads
    internal = router_reads != TYPESAFE_BACKEND
    assert (labels.get(JUDGE_POD_LABEL[0]) == JUDGE_POD_LABEL[1]) is internal
    assert (labels.get(TYPESAFE_POD_LABEL[0]) == TYPESAFE_POD_LABEL[1]) is not internal
    assert (env.get(TYPESAFE_INJECT_ENV[0]) == TYPESAFE_INJECT_ENV[1]) is not internal


def test_an_unreadable_document_is_the_default_on_the_pod(monkeypatch, tenant):
    """`read_judge_backend` says None for an unreadable document (the router
    fails closed on it); the pod renders the default, as before."""
    class Broken:
        def collection(self, name):
            raise RuntimeError("firestore down")

    for name in ("GKE_ENDPOINT", "GKE_CA_CERT_B64"):
        monkeypatch.delenv(name, raising=False)
    gke = build_router(scheduler_settings(live_browser_sidecar=True), db=Broken())._gke
    assert gke._judgment("eng") == DEFAULT_BACKEND
