"""The scheduler never writes a worker's verification settings per task.

Contract request 34, section 3. The public keys a worker verifies with, the
key they belong to and the rollout mode are PLATFORM configuration: on Cloud
Run they are on the Job (terraform's, or one this scheduler creates, from its
own settings), and on GKE they are a ConfigMap Terraform applies, mounted
read-only. `worker_env()` -- the per-execution override on Cloud Run and the
template substitution on GKE, both shaped by a task -- must never set any of
them, or a document the tenant can write would choose the keys it is checked
against. One allow-list, both dispatchers.
"""

from __future__ import annotations

import json

import pytest

from scheduler.dispatch import (
    SPEC_VERIFY_KEYS_CONFIG_MAP,
    SPEC_VERIFY_KEYS_MOUNT,
    CloudRunJobDispatcher,
    GkeJobDispatcher,
    GkeTarget,
    gke_worker_env,
    worker_env,
)
from swarm_common.profiles import RUNNER_PROFILES

from .conftest import scheduler_settings
from .test_dispatch_manifests import FakeBatchApi, FakeJobsClient, make_lease, make_task, tenant  # noqa: F401

#: What only the platform may set on a worker.
SPEC_SETTINGS = {"SPEC_VERIFY_KEYS", "SPEC_SIGNING_KEY", "SPEC_SIGNATURE_MODE", "SPEC_LEGACY_CUTOVER"}

VERSION = (
    "projects/p/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/"
    "cryptoKeyVersions/1"
)
KEYS = json.dumps({VERSION: "-----BEGIN PUBLIC KEY-----\nMFkw\n-----END PUBLIC KEY-----\n"})


@pytest.fixture
def settings():
    return scheduler_settings(
        spec_verify_keys=KEYS,
        spec_signing_key=VERSION.rsplit("/cryptoKeyVersions/", 1)[0],
        spec_signature_mode="legacy",
        spec_legacy_cutover="2026-09-29T12:00:00Z",
    )


@pytest.mark.parametrize("profile", sorted(RUNNER_PROFILES))
def test_neither_worker_env_sets_a_verification_setting(settings, tenant, profile):  # noqa: F811
    task = make_task(profile)
    for env in (
        worker_env(task=task, lease=make_lease(task), tenant=tenant, settings=settings),
        gke_worker_env(task=task, lease=make_lease(task), tenant=tenant, settings=settings),
    ):
        assert not SPEC_SETTINGS & set(env), sorted(SPEC_SETTINGS & set(env))


def test_the_cloud_run_execution_override_sets_none_of_them(settings, tenant):  # noqa: F811
    client = FakeJobsClient()
    task = make_task("claude-code")
    CloudRunJobDispatcher(settings, client=client).dispatch(
        task=task, lease=make_lease(task), profile=RUNNER_PROFILES["claude-code"], tenant=tenant
    )
    override = client.runs[0]["overrides"].container_overrides[0]
    assert not SPEC_SETTINGS & {e.name for e in override.env}


def test_a_job_this_scheduler_creates_carries_them_from_its_own_settings(settings, tenant):  # noqa: F811
    """A tenant Terraform does not list, or a smaller class, gets a Job from
    `_build_job`; without these its worker could verify nothing."""
    job = CloudRunJobDispatcher(settings, client=FakeJobsClient())._build_job(
        RUNNER_PROFILES["claude-code"], tenant
    )
    env = {e.name: e.value for e in job.template.template.containers[0].env}
    assert env["SPEC_VERIFY_KEYS"] == KEYS
    assert env["SPEC_SIGNING_KEY"] == settings.spec_signing_key
    assert env["SPEC_SIGNATURE_MODE"] == "legacy"
    assert env["SPEC_LEGACY_CUTOVER"] == "2026-09-29T12:00:00Z"


def test_the_gke_job_mounts_the_config_map_read_only_and_sets_no_env(settings, tenant):  # noqa: F811
    api = FakeBatchApi()
    task = make_task("browser")
    GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api).dispatch(
        task=task, lease=make_lease(task), profile=RUNNER_PROFILES["browser"], tenant=tenant
    )
    _, body = api.created[0]
    pod = body["spec"]["template"]["spec"]
    container = pod["containers"][0]
    names = {e["name"] for e in container["env"]}
    assert not SPEC_SETTINGS & names, sorted(SPEC_SETTINGS & names)
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    volumes = {v["name"]: v for v in pod["volumes"]}
    mount = mounts["spec-verify-keys"]
    assert mount["mountPath"] == SPEC_VERIFY_KEYS_MOUNT == "/etc/swarm/spec-verify-keys"
    assert mount["readOnly"] is True
    assert volumes["spec-verify-keys"]["configMap"]["name"] == SPEC_VERIFY_KEYS_CONFIG_MAP
    assert SPEC_VERIFY_KEYS_CONFIG_MAP == "swarm-spec-verify-keys"


def test_the_gke_job_names_itself_in_runner_job_name(settings, tenant):  # noqa: F811
    """RUNNER_JOB_NAME and `metadata.name` come from ONE render, so the
    worker's comparison is a self-consistency check, no more (section 5)."""
    api = FakeBatchApi()
    task = make_task("browser")
    GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api).dispatch(
        task=task, lease=make_lease(task), profile=RUNNER_PROFILES["browser"], tenant=tenant
    )
    _, body = api.created[0]
    env = {e["name"]: e["value"] for e in body["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["RUNNER_JOB_NAME"] == body["metadata"]["name"]


def test_the_scheduler_refuses_malformed_verification_settings():
    with pytest.raises(ValueError):
        scheduler_settings(spec_verify_keys="{not json")
    with pytest.raises(ValueError):
        scheduler_settings(spec_signature_mode="off")
