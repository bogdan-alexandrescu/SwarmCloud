"""The claude-code-gke canary is claude-code on GKE, and nothing else (contract request 54).

TEMPORARY, like the profile: request 54 adds `claude-code-gke` to the frozen
catalogue so contract request 53 (claude-code on GKE Autopilot) can be measured
on real steps before claude-code itself moves. This file goes with the profile,
in the change that switches claude-code to GKE.

What would make the canary measure something other than claude-code on GKE:

  * a field other than `name` and `backend` differing from claude-code -- the
    image, the runner, the resource class, the timeout, the inputs;
  * the GKE pod running the CLI's default model, because a GKE Job has no
    Terraform-created Cloud Run Job to carry MODEL (request 53, follow-up 1);
  * a Cloud Run Job created for it by Terraform, which would make a "GKE"
    canary dispatchable to Cloud Run if its backend were ever misread.

MUTATION: delete the `MODEL` append in `GkeJobDispatcher._manifest`, and
`test_the_canary_pod_runs_claude_codes_model` fails; change any field of the
`replace(...)` call in profiles.py, and the identity test fails.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend

from scheduler.dispatch import GkeJobDispatcher, GkeTarget, worker_model

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import NOW, FakeBatchApi, make_lease, make_task

REPO = Path(__file__).resolve().parents[3]
CANARY = "claude-code-gke"
PINNED_MODEL = "claude-opus-5-5"


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng",
        kind="group",
        principal="eng@saga.xyz",
        created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def _pod_env(settings, tenant: Tenant, profile_name: str, *, model: str | None = None) -> dict[str, str]:
    api = FakeBatchApi()
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api)
    task = make_task(profile_name)
    if model is not None:
        task = dataclasses.replace(task, model=model)
    dispatcher.dispatch(task=task, lease=make_lease(task), profile=RUNNER_PROFILES[profile_name], tenant=tenant)
    containers = api.created[0][1]["spec"]["template"]["spec"]["containers"]
    worker = next(c for c in containers if c["name"] == "worker")
    return {e["name"]: e["value"] for e in worker["env"]}


def test_the_canary_differs_from_claude_code_only_in_name_and_backend():
    base, canary = RUNNER_PROFILES["claude-code"], RUNNER_PROFILES[CANARY]
    differing = [
        f.name for f in dataclasses.fields(base) if getattr(base, f.name) != getattr(canary, f.name)
    ]
    assert differing == ["name", "backend"], differing
    assert canary.name == CANARY
    assert resolve_backend(canary) is Backend.GKE_AUTOPILOT


def test_the_canary_pod_runs_claude_codes_model(tenant):
    settings = scheduler_settings(worker_models={"claude-code": PINNED_MODEL, CANARY: PINNED_MODEL})

    env = _pod_env(settings, tenant, CANARY)

    assert env.get("MODEL") == PINNED_MODEL
    assert env["RUNNER_PROFILE"] == CANARY


def test_a_gke_profile_with_no_pinned_model_gets_no_model_variable(tenant):
    settings = scheduler_settings(worker_models={"claude-code": PINNED_MODEL, CANARY: PINNED_MODEL})

    assert "MODEL" not in _pod_env(settings, tenant, "browser")


def test_a_tasks_own_model_never_reaches_the_pod(tenant):
    """Invariant 10: the model is the scheduler's WORKER_MODELS, never the task's."""
    settings = scheduler_settings(worker_models={CANARY: PINNED_MODEL})

    env = _pod_env(settings, tenant, CANARY, model="claude-haiku-5")

    assert env.get("MODEL") == PINNED_MODEL
    assert "claude-haiku-5" not in env.values()

    unpinned = _pod_env(scheduler_settings(), tenant, CANARY, model="claude-haiku-5")
    assert "MODEL" not in unpinned and "claude-haiku-5" not in unpinned.values()


def test_one_lookup_serves_both_backends():
    settings = scheduler_settings(worker_models={CANARY: f" {PINNED_MODEL} "})
    assert worker_model(settings, RUNNER_PROFILES[CANARY]) == PINNED_MODEL
    assert worker_model(settings, RUNNER_PROFILES["claude-code"]) is None


def _runner_models_block() -> str:
    text = (REPO / "terraform/infra/locals.tf").read_text()
    match = re.search(r"^  runner_models = \{\n(.*?)^  \}", text, re.M | re.S)
    assert match, "runner_models not found in terraform/infra/locals.tf"
    return match.group(1)


def test_terraform_pins_the_canary_to_claude_codes_model():
    """WORKER_MODELS is written from `local.runner_models`; the canary has no
    Cloud Run Job, so this map is the only way its MODEL reaches a pod."""
    models = dict(re.findall(r'^\s*"([a-z0-9-]+)"\s*=\s*"([^"]+)"', _runner_models_block(), re.M))
    assert models.get(CANARY) == models.get("claude-code") is not None, models


def test_terraform_creates_no_cloud_run_job_for_a_gke_profile():
    """The Job loop filters by backend, so the canary gets none: it is GKE only."""
    text = (REPO / "terraform/infra/locals.tf").read_text()
    entry = re.search(r'^    "claude-code-gke" = \{\n(.*?)^    \}', text, re.M | re.S)
    assert entry, "claude-code-gke is not mirrored in local.runner_profiles"
    assert re.search(r'^\s*backend\s*=\s*"GKE_AUTOPILOT"', entry.group(1), re.M)
    assert 'if profile.backend == "CLOUD_RUN_JOB"' in text, (
        "the Job matrix no longer filters on the backend; a GKE profile would get a Cloud Run Job"
    )
