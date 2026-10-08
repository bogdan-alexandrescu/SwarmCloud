"""claude-code runs on GKE Autopilot, and its canary is gone (contract requests 53 and 55).

Owner decision of 2026-10-08, after request 55's canary (`claude-code-gke`)
ran 5/5 real steps on GKE with DISPATCHED -> RUNNING p50 ~23 s, max 44 s,
against Cloud Run's p50 128 s / p90 212 s: claude-code itself moves to GKE,
and the canary profile is removed in the same change.

What would make the switch wrong:

  * claude-code still resolving to Cloud Run, or the canary still in the
    catalogue as a second name for the same agent;
  * the GKE pod running the CLI's default model, because a GKE Job has no
    Terraform-created Cloud Run Job to carry MODEL (request 53, follow-up 1);
  * Terraform destroying the claude-code Cloud Run Jobs the owner kept until
    2026-10-15 as the rollback, because its Job loop filters on the backend.

MUTATION: delete the `MODEL` append in `GkeJobDispatcher._manifest`, and
`test_the_claude_code_pod_runs_its_pinned_model` fails; drop "claude-code"
from `cloud_run_fallback_profiles` in terraform/infra/locals.tf, and
`test_terraform_keeps_the_claude_code_cloud_run_jobs_as_the_rollback` fails.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend

from scheduler.dispatch import GkeJobDispatcher, GkeTarget, profile_model

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import NOW, FakeBatchApi, make_lease, make_task

REPO = Path(__file__).resolve().parents[3]
PROFILE = "claude-code"
CANARY = "claude-code-gke"
PINNED_MODEL = "claude-opus-5-5"
LOCALS = REPO / "terraform/infra/locals.tf"


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


def test_claude_code_resolves_to_gke_autopilot():
    assert RUNNER_PROFILES[PROFILE].backend is Backend.GKE_AUTOPILOT
    assert resolve_backend(RUNNER_PROFILES[PROFILE]) is Backend.GKE_AUTOPILOT


def test_the_canary_profile_is_removed():
    """Request 55 was temporary: removed in the change that moved claude-code."""
    assert CANARY not in RUNNER_PROFILES


def test_the_claude_code_pod_runs_its_pinned_model(tenant):
    settings = scheduler_settings(worker_models={PROFILE: PINNED_MODEL})

    env = _pod_env(settings, tenant, PROFILE)

    assert env.get("MODEL") == PINNED_MODEL
    assert env["RUNNER_PROFILE"] == PROFILE


def test_a_gke_profile_with_no_pinned_model_gets_no_model_variable(tenant):
    settings = scheduler_settings(worker_models={PROFILE: PINNED_MODEL})

    assert "MODEL" not in _pod_env(settings, tenant, "browser")


def test_a_tasks_own_model_never_reaches_the_pod(tenant):
    """Invariant 10: the model is the scheduler's WORKER_MODELS, never the task's."""
    settings = scheduler_settings(worker_models={PROFILE: PINNED_MODEL})

    env = _pod_env(settings, tenant, PROFILE, model="claude-haiku-5")

    assert env.get("MODEL") == PINNED_MODEL
    assert "claude-haiku-5" not in env.values()

    unpinned = _pod_env(scheduler_settings(), tenant, PROFILE, model="claude-haiku-5")
    assert "MODEL" not in unpinned and "claude-haiku-5" not in unpinned.values()


def test_one_lookup_serves_both_backends():
    settings = scheduler_settings(worker_models={PROFILE: f" {PINNED_MODEL} "})
    assert profile_model(settings, RUNNER_PROFILES[PROFILE]) == PINNED_MODEL
    assert profile_model(settings, RUNNER_PROFILES["browser"]) is None


def _block(name: str) -> str:
    text = LOCALS.read_text()
    match = re.search(rf"^  {name} = \{{\n(.*?)^  \}}", text, re.M | re.S)
    assert match, f"{name} not found in terraform/infra/locals.tf"
    return match.group(1)


def test_terraform_pins_claude_codes_model_and_no_canary():
    """WORKER_MODELS is written from `local.runner_models`, the only way MODEL
    reaches a GKE pod."""
    models = dict(re.findall(r'^\s*"([a-z0-9-]+)"\s*=\s*"([^"]+)"', _block("runner_models"), re.M))
    assert models.get(PROFILE) == PINNED_MODEL, models
    assert CANARY not in models, models


def test_terraform_mirrors_claude_code_on_gke_and_drops_the_canary():
    profiles = _block("runner_profiles")
    entry = re.search(r'^    "claude-code" = \{\n(.*?)^    \}', profiles, re.M | re.S)
    assert entry, "claude-code is not mirrored in local.runner_profiles"
    assert re.search(r'^\s*backend\s*=\s*"GKE_AUTOPILOT"', entry.group(1), re.M)
    assert f'"{CANARY}"' not in profiles


def test_terraform_keeps_the_claude_code_cloud_run_jobs_as_the_rollback():
    """Owner decision 2026-10-08: the Cloud Run Jobs stay until 2026-10-15.

    The Job matrix filters on the backend, so a GKE profile gets no Job unless
    it is named as a fallback -- and the fallback carries the date it goes.
    """
    text = LOCALS.read_text()
    fallback = re.search(r"^  cloud_run_fallback_profiles = \[([^\]]*)\]", text, re.M)
    assert fallback, "cloud_run_fallback_profiles is missing: the switch would destroy the claude-code Jobs"
    assert f'"{PROFILE}"' in fallback.group(1)
    assert "kept until 2026-10-15" in text.lower()
    assert re.search(
        r'if\s*\(profile\.backend == "CLOUD_RUN_JOB" \|\| contains\(local\.cloud_run_fallback_profiles, profile_name\)\)',
        text,
    ), "the Job matrix does not admit the fallback profiles"
