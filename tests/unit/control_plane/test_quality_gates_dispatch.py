"""The scheduler hands the worker the quality gates' verdict (contract request 128).

The owner's pick on PR 2955, "Scheduler hands a verdict": the MAC over
`workspace_settings/{tenant}.quality_gates` is an HMAC under the one
`swarm-settings-key`, which no worker holds, so the scheduler, which does,
verifies the block at dispatch and writes the verdict into the job's
environment (`judgment.read_quality_gates_env`, `dispatch.quality_gates_
dispatch_env`) on both backends. Blocks are signed here with swarm-api's own
`sign_block`, so these tests also hold the signer and this verifier equal.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES

from scheduler.dispatch import (
    CloudRunJobDispatcher,
    GkeJobDispatcher,
    GkeTarget,
    build_router,
    quality_gates_dispatch_env,
    runs_quality_gates,
)
from scheduler.judgment import (
    GATES_REFUSED_MISMATCH,
    GATES_REFUSED_NO_KEY,
    GATES_REFUSED_SWITCH_OFF,
    GATES_REFUSED_UNREADABLE,
    GATES_REFUSED_UNSIGNED,
    QUALITY_GATES_JEV_ENV,
    QUALITY_GATES_REFUSED_ENV,
    SETTINGS_COLLECTION,
    quality_gates_verdict,
    read_quality_gates_env,
)

from .conftest import PROJECT, scheduler_settings
from .fakes import FakeFirestore
from .test_dispatch_manifests import NOW, FakeJobsClient, make_lease, make_task

KEY = "settings-" + secrets.token_hex(24)


def _signed(jev: bool, *, tenant_id: str = "eng", key: str = KEY) -> dict[str, Any]:
    from swarm_api.judgment import sign_block

    block: dict[str, Any] = {"jev": jev, "set_by": "root@example.com"}
    block["mac"] = sign_block(key, tenant_id=tenant_id, block="quality_gates", fields=block)
    return block


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng", kind="group", principal="eng@example.com", created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


# --- the verdict ---------------------------------------------------------------


def test_a_signed_on_is_on():
    doc = {"quality_gates": _signed(True)}
    assert quality_gates_verdict(doc, tenant_id="eng", key=KEY) == (True, None)


@pytest.mark.parametrize("block, why", [
    pytest.param({"jev": True}, GATES_REFUSED_UNSIGNED, id="unsigned"),
    pytest.param({**_signed(False), "jev": True}, GATES_REFUSED_MISMATCH, id="mac-over-false"),
    pytest.param(_signed(True, tenant_id="research"), GATES_REFUSED_MISMATCH, id="another-tenant"),
    pytest.param(_signed(True, key="other-" + secrets.token_hex(8)), GATES_REFUSED_MISMATCH,
                 id="another-key"),
])
def test_a_stored_on_swarm_api_did_not_sign_is_off(block, why):
    # MUTATION: return on for `jev is True` without checking the MAC -> every case fails.
    assert quality_gates_verdict({"quality_gates": block}, tenant_id="eng", key=KEY) == (False, why)


def test_no_key_or_the_switch_off_is_off_even_for_a_signed_on():
    doc = {"quality_gates": _signed(True)}
    assert quality_gates_verdict(doc, tenant_id="eng", key="") == (False, GATES_REFUSED_NO_KEY)
    # SETTINGS_MAC_REQUIRED off: this service verifies nothing, so nothing is vouched for.
    assert quality_gates_verdict(doc, tenant_id="eng", key=None) == (False, GATES_REFUSED_SWITCH_OFF)


@pytest.mark.parametrize("doc", [{}, {"quality_gates": _signed(False)},
                                 {"quality_gates": {"jev": "true"}}, {"quality_gates": "on"}])
def test_a_block_that_does_not_say_on_is_off_with_no_reason(doc):
    assert quality_gates_verdict(doc, tenant_id="eng", key=KEY) == (False, None)


def test_the_env_always_says_and_an_unreadable_document_is_off():
    db = FakeFirestore()
    assert read_quality_gates_env(db, "eng", mac_key=KEY) == {QUALITY_GATES_JEV_ENV: "false"}
    db.docs[f"{SETTINGS_COLLECTION}/eng"] = {"quality_gates": {"jev": True}}
    assert read_quality_gates_env(db, "eng", mac_key=KEY) == {
        QUALITY_GATES_JEV_ENV: "false", QUALITY_GATES_REFUSED_ENV: GATES_REFUSED_UNSIGNED}
    db.docs[f"{SETTINGS_COLLECTION}/eng"] = {"quality_gates": _signed(True)}
    assert read_quality_gates_env(db, "eng", mac_key=KEY) == {QUALITY_GATES_JEV_ENV: "true"}

    class Broken:
        def collection(self, name):
            raise RuntimeError("firestore down")

    assert read_quality_gates_env(Broken(), "eng", mac_key=KEY) == {
        QUALITY_GATES_JEV_ENV: "false", QUALITY_GATES_REFUSED_ENV: GATES_REFUSED_UNREADABLE}


def test_only_the_tenants_own_document_is_read():
    db = FakeFirestore()
    db.docs[f"{SETTINGS_COLLECTION}/research"] = {"quality_gates": _signed(True, tenant_id="research")}
    # Another tenant's signed "on" is not this tenant's (invariant 9).
    assert read_quality_gates_env(db, "eng", mac_key=KEY) == {QUALITY_GATES_JEV_ENV: "false"}


# --- the dispatch ----------------------------------------------------------------


def test_only_the_claude_code_runner_carries_the_verdict():
    assert runs_quality_gates(RUNNER_PROFILES["claude-code"])
    calls: list[str] = []

    def gates(tenant_id: str) -> dict[str, str]:
        calls.append(tenant_id)
        return {QUALITY_GATES_JEV_ENV: "true"}

    others = [p for name, p in RUNNER_PROFILES.items() if not runs_quality_gates(p)]
    assert others, "every profile runs claude-code?"
    for profile in others:
        assert quality_gates_dispatch_env(gates, "eng", profile) == {}
    assert calls == []
    # No settings reader in this deployment: an explicit off, never omitted.
    assert quality_gates_dispatch_env(None, "eng", RUNNER_PROFILES["claude-code"]) == {
        QUALITY_GATES_JEV_ENV: "false"}


def _router(monkeypatch, doc: dict[str, Any] | None, **settings_kw: Any):
    for name in ("GKE_ENDPOINT", "GKE_CA_CERT_B64"):
        monkeypatch.delenv(name, raising=False)
    settings = scheduler_settings(**settings_kw)
    db = FakeFirestore()
    if doc is not None:
        db.docs[f"{SETTINGS_COLLECTION}/eng"] = doc
    return settings, build_router(settings, db=db)


def _cloud_run_env(router: Any, settings: Any, tenant: Tenant) -> dict[str, str]:
    cloud_run: CloudRunJobDispatcher = router._cloud_run
    client = FakeJobsClient()
    cloud_run._client = client
    task = make_task()
    cloud_run.dispatch(task=task, lease=make_lease(task), profile=RUNNER_PROFILES["claude-code"],
                       tenant=tenant)
    return {e.name: e.value for e in client.runs[0]["overrides"].container_overrides[0].env}


def _gke_env(router: Any, tenant: Tenant) -> dict[str, str]:
    gke: GkeJobDispatcher = router._gke
    gke._target = GkeTarget("https://k8s", "/ca.pem")
    task = make_task()
    job = gke._manifest(task=task, lease=make_lease(task), profile=RUNNER_PROFILES["claude-code"],
                        tenant=tenant)
    (worker,) = [c for c in job["spec"]["template"]["spec"]["containers"] if c["name"] == "worker"]
    return {e["name"]: e["value"] for e in worker["env"]}


@pytest.mark.parametrize("block, jev, refused", [
    pytest.param(_signed(True), "true", None, id="signed-on"),
    pytest.param({"jev": True}, "false", GATES_REFUSED_UNSIGNED, id="forged-on"),
    pytest.param(_signed(True, tenant_id="research"), "false", GATES_REFUSED_MISMATCH,
                 id="copied-on"),
    pytest.param(_signed(False), "false", None, id="off"),
])
def test_both_backends_carry_the_verdict_build_router_wires(monkeypatch, tenant, block, jev, refused):
    # MUTATION: drop `quality_gates=gates` from either dispatcher in build_router
    # -> that backend says "false" for the signed "on".
    settings, router = _router(monkeypatch, {"quality_gates": block},
                               settings_mac_required=True, settings_mac_key=KEY)
    for env in (_cloud_run_env(router, settings, tenant), _gke_env(router, tenant)):
        assert env[QUALITY_GATES_JEV_ENV] == jev
        assert env.get(QUALITY_GATES_REFUSED_ENV) == refused


def test_with_the_switch_off_a_signed_on_is_off_on_both_backends(monkeypatch, tenant):
    settings, router = _router(monkeypatch, {"quality_gates": _signed(True)},
                               settings_mac_required=False, settings_mac_key=KEY)
    for env in (_cloud_run_env(router, settings, tenant), _gke_env(router, tenant)):
        assert env[QUALITY_GATES_JEV_ENV] == "false"
        assert env[QUALITY_GATES_REFUSED_ENV] == GATES_REFUSED_SWITCH_OFF
