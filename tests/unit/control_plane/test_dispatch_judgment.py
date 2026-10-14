"""A live-browser pod's Jev goes to the backend its workspace chose (JUDGE-SELECT).

Owner decisions 2026-10-14. The dispatcher reads the task tenant's
`workspace_settings/{tenant}.judgment_backend` (default internal-jev) and:

  * internal-jev / internal-laya, with the tailnet egress on (the platform's
    INTERNAL_DECISION_API_URL): JEV_API_URL is the in-cluster egress Service's
    `/jev/v1/decide` or `/laya/v1/decide`, and the pod carries
    `swarm-judge: internal`, the label JUDGE-NET's NetworkPolicy opens the
    decision API to;
  * internal with the egress off: the backend is named and NO URL is set, so
    the jev wrapper refuses rather than sending page text to TypeSafe instead;
  * typesafe: no JEV_API_URL and no label -- jev-browser's own TypeSafe default.

SWARM_JEV_BACKEND names the choice on every live-browser pod. A pod of any
other profile, or a live-browser pod while the display rollout switch is off
(no live browser, so no jev), is untouched.

MUTATION: give the label to a typesafe pod, or set JEV_API_URL with the
egress off, and the named test fails.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES

from scheduler.dispatch import GkeJobDispatcher, GkeTarget
from scheduler.judgment import (
    BACKENDS,
    DEFAULT_BACKEND,
    INTERNAL_DECISION_SERVICE,
    JEV_API_URL_ENV,
    JEV_BACKEND_ENV,
    JUDGE_POD_LABEL,
    SETTINGS_COLLECTION,
    jev_dispatch,
    read_backend,
)
from scheduler.settings import SchedulerSettings

from .conftest import PROJECT, scheduler_settings
from .fakes import FakeFirestore
from .test_dispatch_manifests import NOW, make_lease, make_task

LIVE = "claude-code-browser"
JEV_URL = f"{INTERNAL_DECISION_SERVICE}/jev/v1/decide"
LAYA_URL = f"{INTERNAL_DECISION_SERVICE}/laya/v1/decide"


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng", kind="group", principal="eng@saga.xyz", created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def _job(tenant: Tenant, backend: str | None, *, profile: str = LIVE, live: bool = True,
         internal: bool = True) -> dict[str, Any]:
    settings = scheduler_settings(
        live_browser_sidecar=live,
        internal_decision_api_url=INTERNAL_DECISION_SERVICE if internal else "")
    judgment = None if backend is None else (lambda tenant_id: backend)
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object(), judgment=judgment)
    task = make_task(profile)
    return dispatcher._manifest(task=task, lease=make_lease(task),
                                profile=RUNNER_PROFILES[profile], tenant=tenant)


def _worker_env(job: dict[str, Any]) -> dict[str, str]:
    spec = job["spec"]["template"]["spec"]
    (worker,) = [c for c in spec["containers"] if c["name"] == "worker"]
    return {e["name"]: e["value"] for e in worker["env"]}


def _pod_labels(job: dict[str, Any]) -> dict[str, str]:
    return job["spec"]["template"]["metadata"]["labels"]


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
    got_env, got_labels = jev_dispatch(backend, INTERNAL_DECISION_SERVICE if internal else "")
    assert got_env == env
    assert got_labels == (dict([JUDGE_POD_LABEL]) if labelled else {})


def test_an_unknown_stored_value_is_the_default_never_typesafe():
    env, labels = jev_dispatch("something-else", INTERNAL_DECISION_SERVICE)
    assert env[JEV_BACKEND_ENV] == DEFAULT_BACKEND == "internal-jev"
    assert env[JEV_API_URL_ENV] == JEV_URL
    assert labels == dict([JUDGE_POD_LABEL]) == {"swarm-judge": "internal"}


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
    assert INTERNAL_DECISION_SERVICE == api.INTERNAL_DECISION_SERVICE


# -- the pod -------------------------------------------------------------------------

@pytest.mark.parametrize("backend,url", [("internal-jev", JEV_URL), ("internal-laya", LAYA_URL)])
def test_an_internal_backend_pod_has_the_url_and_the_label(tenant, backend, url):
    job = _job(tenant, backend)
    env = _worker_env(job)
    assert env[JEV_API_URL_ENV] == url
    assert env[JEV_BACKEND_ENV] == backend
    assert _pod_labels(job)["swarm-judge"] == "internal"


def test_a_typesafe_pod_has_neither_the_url_nor_the_label(tenant):
    job = _job(tenant, "typesafe")
    env = _worker_env(job)
    assert JEV_API_URL_ENV not in env
    assert env[JEV_BACKEND_ENV] == "typesafe"
    assert "swarm-judge" not in _pod_labels(job)
    assert "swarm-judge" not in job["metadata"]["labels"]


def test_internal_with_the_egress_off_names_the_backend_and_no_url(tenant):
    job = _job(tenant, "internal-jev", internal=False)
    env = _worker_env(job)
    assert env[JEV_BACKEND_ENV] == "internal-jev"
    assert JEV_API_URL_ENV not in env
    assert "swarm-judge" not in _pod_labels(job)


def test_no_reader_is_the_default(tenant):
    env = _worker_env(_job(tenant, None))
    assert env[JEV_BACKEND_ENV] == "internal-jev"
    assert env[JEV_API_URL_ENV] == JEV_URL


@pytest.mark.parametrize("profile,live", [("claude-code", True), ("browser", True), (LIVE, False)])
def test_no_live_browser_no_jev(tenant, profile, live):
    job = _job(tenant, "internal-jev", profile=profile, live=live)
    env = _worker_env(job)
    assert JEV_BACKEND_ENV not in env and JEV_API_URL_ENV not in env
    assert "swarm-judge" not in _pod_labels(job)


def test_the_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("INTERNAL_DECISION_API_URL", INTERNAL_DECISION_SERVICE)
    assert SchedulerSettings.from_env().internal_decision_api_url == INTERNAL_DECISION_SERVICE
    monkeypatch.delenv("INTERNAL_DECISION_API_URL")
    assert SchedulerSettings.from_env().internal_decision_api_url == ""


def test_a_url_other_than_the_egress_service_is_refused():
    with pytest.raises(ValueError):
        scheduler_settings(internal_decision_api_url="https://decision-api-staging.example.invalid")
