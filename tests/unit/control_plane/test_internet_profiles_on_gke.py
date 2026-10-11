"""generic, codex and merge run on GKE Autopilot (contract requests 64-66).

Owner decision of 2026-10-10, option A of #939
(docs/incidents/2026-10-09-egress-open-delay.md): a new Cloud Run instance's
internet path opens a median 20.2 s after start (n=19) against GKE's 1.17 s
(n=148) through the same NAT. indexer went first, alone, as the canary
(request 63); these are the profiles that reach the internet and followed it.
post-verdict and claude-code-review stay on Cloud Run, each designed to run as
an account of its own that a GKE pod cannot be, and mock reaches no internet.

What would make the move wrong:

  * a profile still resolving to Cloud Run, or the Terraform mirror
    disagreeing with the catalogue (tests/terraform/catalogue.tftest.hcl
    compares them);
  * the GKE pod not running the worker lifecycle as the tenant's own KSA, or
    carrying a mounted credential: merge reads the tenant's `-git` token at
    merge time, as the worker account the KSA is bound to (request 47), and
    must still find no secret in its environment;
  * Terraform destroying the `swarm-job-<tenant>-<profile>` Cloud Run Jobs that
    are the rollback, because its Job loop filters on the backend.

MUTATION: drop "merge" from `cloud_run_fallback_profiles` in
terraform/infra/locals.tf, and
`test_terraform_keeps_their_cloud_run_jobs_as_the_rollback[merge]` fails; set
generic's backend back to CLOUD_RUN_JOB in profiles.py, and
`test_each_resolves_to_gke_autopilot[generic]` fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend

from scheduler.dispatch import BackendRouter, GkeJobDispatcher, GkeTarget

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import NOW, FakeBatchApi, make_lease, make_task

REPO = Path(__file__).resolve().parents[3]
MOVED = {"generic": 64, "codex": 65, "merge": 66}
STAYING = ("mock", "post-verdict", "claude-code-review")
LOCALS = REPO / "terraform/infra/locals.tf"
REQUESTS = REPO / "docs/contract-change-requests.md"


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


def _pod_spec(profile: str, tenant: Tenant) -> tuple[dict, GkeJobDispatcher]:
    api = FakeBatchApi()
    dispatcher = GkeJobDispatcher(scheduler_settings(), target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api)
    task = make_task(profile)
    dispatcher.dispatch(task=task, lease=make_lease(task), profile=RUNNER_PROFILES[profile], tenant=tenant)
    return api.created[0][1]["spec"]["template"]["spec"], dispatcher


@pytest.mark.parametrize("profile", sorted(MOVED))
def test_each_resolves_to_gke_autopilot(profile):
    assert RUNNER_PROFILES[profile].backend is Backend.GKE_AUTOPILOT
    assert resolve_backend(RUNNER_PROFILES[profile]) is Backend.GKE_AUTOPILOT


@pytest.mark.parametrize("profile", STAYING)
def test_the_profiles_that_cannot_or_need_not_move_stay_on_cloud_run(profile):
    assert resolve_backend(RUNNER_PROFILES[profile]) is Backend.CLOUD_RUN_JOB


@pytest.mark.parametrize("profile", sorted(MOVED))
def test_the_router_sends_each_to_the_gke_dispatcher(profile):
    settings = scheduler_settings()
    gke = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=FakeBatchApi())
    router = BackendRouter(cloud_run=None, gke=gke, settings=settings)
    assert router.for_backend(resolve_backend(RUNNER_PROFILES[profile])) is gke


@pytest.mark.parametrize("profile", sorted(MOVED))
def test_the_pod_runs_the_lifecycle_as_the_tenants_ksa_with_no_credential(profile, tenant):
    spec, dispatcher = _pod_spec(profile, tenant)
    worker = next(c for c in spec["containers"] if c["name"] == "worker")
    env = {e["name"]: e.get("value") for e in worker["env"]}

    assert "command" not in worker and "args" not in worker, "the image ENTRYPOINT is the lifecycle"
    assert env["RUNNER_PROFILE"] == profile
    assert env["TENANT_ID"] == tenant.tenant_id
    assert spec["serviceAccountName"] == dispatcher.ksa_for(tenant)
    assert spec["automountServiceAccountToken"] is False
    assert not any("valueFrom" in e for e in worker["env"]), "no credential is projected into a GKE pod"
    assert "MODEL" not in env, f"{profile} names no model"


def test_terraform_mirrors_each_on_gke():
    text = LOCALS.read_text(encoding="utf-8")
    for profile in MOVED:
        entry = re.search(rf'^    "{profile}" = \{{\n(.*?)^    \}}', text, re.M | re.S)
        assert entry, f"{profile} is not mirrored in local.runner_profiles"
        assert re.search(r'^\s*backend\s*=\s*"GKE_AUTOPILOT"', entry.group(1), re.M), entry.group(1)


@pytest.mark.parametrize("profile", sorted(MOVED))
def test_terraform_keeps_their_cloud_run_jobs_as_the_rollback(profile):
    """The rollback is one line in profiles.py only while the Jobs exist."""
    text = LOCALS.read_text(encoding="utf-8")
    fallback = re.search(r"^  cloud_run_fallback_profiles = \[([^\]]*)\]", text, re.M)
    assert fallback, "cloud_run_fallback_profiles is missing: the switch would destroy the Jobs"
    assert f'"{profile}"' in fallback.group(1)


@pytest.mark.parametrize("profile,number", sorted(MOVED.items()))
def test_each_request_records_the_owners_acceptance_and_the_rollback(profile, number):
    text = REQUESTS.read_text(encoding="utf-8")
    start = text.index(f"## {number}. ")
    end = text.find("\n## ", start + 1)
    section = text[start:end if end != -1 else len(text)]
    # scripts/lib/check-frozen-contract.sh passes a diff under
    # apps/common/swarm_common/ only beside an added line holding this phrase.
    assert "accepted by the owner" in section.lower()
    assert "#939" in section
    assert f"`{profile}`" in section or f'"{profile}"' in section
    assert "CLOUD_RUN_JOB" in section, "the rollback (backend back to CLOUD_RUN_JOB) must be recorded"
