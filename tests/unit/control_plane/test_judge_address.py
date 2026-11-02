"""Every judging claude-code or codex pod is given the decision API (lane JEV-JUDGMENTS-RUN).

The owner chose on 2026-11-02 to build every proposal in CLD's Jev report.
Before it, nothing set SWARM_JUDGE_INTERNAL_URL, so a workspace that opted in
to Jev on the default Internal Jev backend read `no_internal_url` in the
worker's judge and made no call; and the `swarm-judge: internal` label
JUDGE-NET's NetworkPolicy (kubernetes/network-policies/allow-judge-egress.yaml)
opens the decision API to was given to live-browser pods only.

Now `GkeJobDispatcher` gives every claude-code and codex attempt whose
workspace's SIGNED `code_intelligence.jev` is on, or whose signed dispatch
block says `jev_judgments: on`, on an internal backend with the tailnet
egress on, the URL, the label and the hostAliases entry, from the same
`jev_dispatch` the live browser's pod uses. And both dispatchers hand the
worker the verdict on that block (`SWARM_JEV_JUDGMENTS`), which it treats as
the opt-in: unsigned or mismatched is off (tenant-settings-integrity.md row 7).

MUTATION: drop `_with_judge_address`'s call, or the verdict's env, and the
named tests fail.
"""

from __future__ import annotations

import dataclasses
import secrets
from typing import Any

import pytest

from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES

from scheduler.dispatch import (
    CloudRunJobDispatcher,
    GkeJobDispatcher,
    GkeTarget,
    JevJudgments,
    build_router,
    read_jev_judgments,
)
from scheduler.judgment import (
    GATES_REFUSED_MISMATCH,
    GATES_REFUSED_SWITCH_OFF,
    GATES_REFUSED_UNREADABLE,
    GATES_REFUSED_UNSIGNED,
    INTERNAL_DECISION_ORIGIN,
    JEV_JUDGMENTS_ENV,
    JEV_JUDGMENTS_REFUSED_ENV,
    JUDGE_INTERNAL_URL_ENV,
    SETTINGS_COLLECTION,
    judgments_verdict,
)

from .conftest import PROJECT, scheduler_settings
from .fakes import FakeFirestore
from .test_dispatch_manifests import NOW, FakeJobsClient, make_lease, make_task

KEY = "settings-" + secrets.token_hex(24)
JEV_URL = "https://decision-api-staging.taile0bda9.ts.net/jev/v1/decide"
LAYA_URL = "https://decision-api-staging.taile0bda9.ts.net/laya/v1/decide"
HOST_ALIASES = [{"ip": "10.48.6.120", "hostnames": ["decision-api-staging.taile0bda9.ts.net"]}]


def _signed(fields: dict[str, Any], *, tenant_id: str = "eng", key: str = KEY) -> dict[str, Any]:
    from swarm_api.judgment import sign_block

    block = dict(fields)
    block["mac"] = sign_block(key, tenant_id=tenant_id, block="code_intelligence", fields=block)
    return block


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng", kind="group", principal="eng@example.com", created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def _reading(on: bool, backend: str = "internal-jev"):
    return lambda tenant_id: JevJudgments(
        {JEV_JUDGMENTS_ENV: "true" if on else "false"}, on, backend)


def _job(tenant: Tenant, *, profile: str = "claude-code", on: bool = True,
         backend: str = "internal-jev", internal: bool = True, live: bool = False,
         dispatch: dict[str, str] | None = None, reader: Any = "default") -> dict[str, Any]:
    settings = scheduler_settings(
        live_browser_sidecar=live,
        internal_decision_api_url=INTERNAL_DECISION_ORIGIN if internal else "")
    judgments = _reading(on, backend) if reader == "default" else reader
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object(), judgment=lambda tenant_id: backend,
                                  jev_judgments=judgments)
    task = make_task(profile)
    if dispatch is not None:
        task = dataclasses.replace(task, metadata={**(task.metadata or {}), "dispatch": dispatch})
    return dispatcher._manifest(task=task, lease=make_lease(task),
                                profile=RUNNER_PROFILES[profile], tenant=tenant)


def _worker_env(job: dict[str, Any]) -> dict[str, str]:
    spec = job["spec"]["template"]["spec"]
    (worker,) = [c for c in spec["containers"] if c["name"] == "worker"]
    return {e["name"]: e["value"] for e in worker["env"]}


def _labels(job: dict[str, Any]) -> dict[str, str]:
    return job["spec"]["template"]["metadata"]["labels"]


def _aliases(job: dict[str, Any]) -> list[dict[str, Any]] | None:
    return job["spec"]["template"]["spec"].get("hostAliases")


# -- the address -------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["claude-code", "codex"])
@pytest.mark.parametrize("backend,url", [("internal-jev", JEV_URL), ("internal-laya", LAYA_URL)])
def test_an_opted_in_internal_backend_attempt_gets_the_address_and_the_label(
        tenant, profile, backend, url):
    job = _job(tenant, profile=profile, backend=backend)
    assert _worker_env(job)[JUDGE_INTERNAL_URL_ENV] == url
    assert _labels(job)["swarm-judge"] == "internal"
    assert _aliases(job) == HOST_ALIASES


def test_the_signed_workflow_switch_on_gets_the_address_without_the_workspace(tenant):
    job = _job(tenant, on=False, dispatch={"jev_judgments": "on"})
    assert _worker_env(job)[JUDGE_INTERNAL_URL_ENV] == JEV_URL
    assert _labels(job)["swarm-judge"] == "internal"


@pytest.mark.parametrize("kwargs", [
    pytest.param({"on": False}, id="not-opted-in"),
    pytest.param({"backend": "typesafe"}, id="typesafe"),
    pytest.param({"internal": False}, id="egress-off"),
    pytest.param({"dispatch": {"jev_judgments": "off"}}, id="switch-off"),
    pytest.param({"profile": "browser"}, id="no-jev-client"),
    pytest.param({"reader": None}, id="no-settings-reader"),
])
def test_no_address_and_no_label_otherwise(tenant, kwargs):
    job = _job(tenant, **kwargs)
    assert JUDGE_INTERNAL_URL_ENV not in _worker_env(job)
    assert "swarm-judge" not in _labels(job)
    assert _aliases(job) is None


def test_a_live_browser_pod_gets_the_workers_url_and_one_host_alias(tenant):
    job = _job(tenant, profile="claude-code-browser", live=True)
    assert _worker_env(job)[JUDGE_INTERNAL_URL_ENV] == JEV_URL
    assert _labels(job)["swarm-judge"] == "internal"
    assert _aliases(job) == HOST_ALIASES


def test_the_url_variable_is_the_worker_judges():
    from agent_worker.judge import settings as worker_settings

    assert JUDGE_INTERNAL_URL_ENV == worker_settings.INTERNAL_URL_ENV


# -- the verdict -----------------------------------------------------------------


def test_a_signed_on_is_on():
    doc = {"code_intelligence": _signed({"jev": True})}
    assert judgments_verdict(doc, tenant_id="eng", key=KEY) == (True, None)


@pytest.mark.parametrize("doc,key,why", [
    pytest.param({"code_intelligence": {"jev": True}}, KEY, GATES_REFUSED_UNSIGNED, id="unsigned"),
    pytest.param({"code_intelligence": {**_signed({"jev": False}), "jev": True}}, KEY,
                 GATES_REFUSED_MISMATCH, id="mac-over-false"),
    pytest.param({"code_intelligence": _signed({"jev": True}, tenant_id="research")}, KEY,
                 GATES_REFUSED_MISMATCH, id="another-tenant"),
    pytest.param({"code_intelligence": _signed({"jev": True})}, None,
                 GATES_REFUSED_SWITCH_OFF, id="mac-switch-off"),
])
def test_unsigned_or_mismatched_is_off(doc, key, why):
    assert judgments_verdict(doc, tenant_id="eng", key=key) == (False, why)


def test_off_says_no_reason():
    assert judgments_verdict({"code_intelligence": {"jev": False}}, tenant_id="eng",
                             key=KEY) == (False, None)


def test_the_reader_gives_the_verdict_and_the_trusted_backend():
    db = FakeFirestore()
    db.docs[f"{SETTINGS_COLLECTION}/eng"] = {"code_intelligence": _signed(
        {"jev": True, "judgment_backend": "internal-laya"})}
    reading = read_jev_judgments(db, "eng", mac_key=KEY)
    assert reading.on and reading.backend == "internal-laya"
    assert reading.env == {JEV_JUDGMENTS_ENV: "true"}
    db.docs[f"{SETTINGS_COLLECTION}/eng"] = {"code_intelligence": {
        "jev": True, "judgment_backend": "internal-laya"}}
    reading = read_jev_judgments(db, "eng", mac_key=KEY)
    assert not reading.on and reading.backend == "internal-jev"
    assert reading.env == {JEV_JUDGMENTS_ENV: "false",
                           JEV_JUDGMENTS_REFUSED_ENV: GATES_REFUSED_UNSIGNED}


def test_an_unreadable_document_is_off():
    class Broken:
        def collection(self, name):
            raise RuntimeError("firestore down")

    reading = read_jev_judgments(Broken(), "eng", mac_key=KEY)
    assert not reading.on
    assert reading.env[JEV_JUDGMENTS_REFUSED_ENV] == GATES_REFUSED_UNREADABLE


def test_the_gke_pod_carries_the_verdict(tenant):
    assert _worker_env(_job(tenant))[JEV_JUDGMENTS_ENV] == "true"
    assert _worker_env(_job(tenant, on=False))[JEV_JUDGMENTS_ENV] == "false"
    assert JEV_JUDGMENTS_ENV not in _worker_env(_job(tenant, profile="browser"))


def test_build_router_wires_the_reader_into_both_dispatchers(monkeypatch, tenant):
    for name in ("GKE_ENDPOINT", "GKE_CA_CERT_B64"):
        monkeypatch.delenv(name, raising=False)
    settings = scheduler_settings(internal_decision_api_url=INTERNAL_DECISION_ORIGIN,
                                  settings_mac_required=True, settings_mac_key=KEY)
    db = FakeFirestore()
    db.docs[f"{SETTINGS_COLLECTION}/eng"] = {"code_intelligence": _signed({"jev": True})}
    router = build_router(settings, db=db)
    task = make_task("claude-code")
    job = router._gke._manifest(task=task, lease=make_lease(task),
                                profile=RUNNER_PROFILES["claude-code"], tenant=tenant)
    assert _worker_env(job)[JEV_JUDGMENTS_ENV] == "true"
    assert _worker_env(job)[JUDGE_INTERNAL_URL_ENV] == JEV_URL
    assert router._cloud_run._jev_judgments is not None
    assert router._cloud_run._jev_judgments("eng").on


def test_the_cloud_run_job_carries_the_verdict(tenant):
    client = FakeJobsClient()
    dispatcher = CloudRunJobDispatcher(scheduler_settings(), jev_judgments=_reading(True))
    dispatcher._client = client
    task = make_task("claude-code")
    dispatcher.dispatch(task=task, lease=make_lease(task),
                        profile=RUNNER_PROFILES["claude-code"], tenant=tenant)
    env = {e.name: e.value for e in client.runs[0]["overrides"].container_overrides[0].env}
    assert env[JEV_JUDGMENTS_ENV] == "true"
    # A Cloud Run Job cannot reach the decision API's egress Service.
    assert JUDGE_INTERNAL_URL_ENV not in env
