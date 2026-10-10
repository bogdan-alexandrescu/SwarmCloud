"""indexer runs on GKE Autopilot, the canary for #939 (contract request 62).

Owner decision of 2026-10-10, option A of
docs/incidents/2026-10-09-egress-open-delay.md: a new Cloud Run instance's
internet path opens a median 20.2 s after start (n=19) against GKE's 1.17 s
(n=148) through the same NAT, so the profiles on Cloud Run Jobs move to
Autopilot -- indexer first, alone, and the others after it is measured.

What would make the canary wrong:

  * indexer still resolving to Cloud Run, or the Terraform mirror disagreeing
    with the catalogue (tests/terraform/catalogue.tftest.hcl compares them);
  * the GKE pod lacking what an index run reads: the indexer image (not the
    base, which has no `swarm-repo-index`), claude-code's pinned model, and
    the TENANT_ID and ARTIFACT_BUCKET that `indexrun.phase_env` hands the
    graph writer as its target;
  * Terraform destroying the `swarm-job-<tenant>-indexer` Cloud Run Jobs that
    are the rollback, because its Job loop filters on the backend.

MUTATION: drop "indexer" from `cloud_run_fallback_profiles` in
terraform/infra/locals.tf, and
`test_terraform_keeps_the_indexer_cloud_run_jobs_as_the_rollback` fails; set
indexer's backend back to CLOUD_RUN_JOB in profiles.py, and
`test_indexer_resolves_to_gke_autopilot` fails.
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
PROFILE = "indexer"
PINNED_MODEL = "claude-opus-5-5"
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


def _worker(settings, tenant: Tenant) -> dict:
    api = FakeBatchApi()
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api)
    task = make_task(PROFILE)
    dispatcher.dispatch(task=task, lease=make_lease(task), profile=RUNNER_PROFILES[PROFILE], tenant=tenant)
    containers = api.created[0][1]["spec"]["template"]["spec"]["containers"]
    return next(c for c in containers if c["name"] == "worker")


def test_indexer_resolves_to_gke_autopilot():
    assert RUNNER_PROFILES[PROFILE].backend is Backend.GKE_AUTOPILOT
    assert resolve_backend(RUNNER_PROFILES[PROFILE]) is Backend.GKE_AUTOPILOT


def test_the_router_sends_indexer_to_the_gke_dispatcher():
    settings = scheduler_settings()
    gke = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=FakeBatchApi())
    router = BackendRouter(cloud_run=None, gke=gke, settings=settings)
    assert router.for_backend(resolve_backend(RUNNER_PROFILES[PROFILE])) is gke


def test_the_indexer_pod_carries_what_an_index_run_reads(tenant):
    image = RUNNER_PROFILES[PROFILE].image
    settings = scheduler_settings(worker_models={PROFILE: PINNED_MODEL})
    digest = f"{settings.artifact_registry_host}/{image}@sha256:{'c' * 64}"
    settings = scheduler_settings(worker_models={PROFILE: PINNED_MODEL}, worker_image_refs={image: digest})

    worker = _worker(settings, tenant)
    env = {e["name"]: e["value"] for e in worker["env"]}

    assert image == "agent-runtime-indexer"
    assert worker["image"] == digest, "the pod must run the indexer image's pinned digest, not the base"
    assert env["RUNNER_PROFILE"] == PROFILE
    assert env.get("MODEL") == PINNED_MODEL
    # `indexrun.phase_env` hands both to `swarm-repo-graph` as its target.
    assert env["TENANT_ID"] == tenant.tenant_id
    assert env["ARTIFACT_BUCKET"] == settings.core.artifact_bucket


def test_terraform_mirrors_indexer_on_gke():
    text = LOCALS.read_text(encoding="utf-8")
    entry = re.search(r'^    "indexer" = \{\n(.*?)^    \}', text, re.M | re.S)
    assert entry, "indexer is not mirrored in local.runner_profiles"
    assert re.search(r'^\s*backend\s*=\s*"GKE_AUTOPILOT"', entry.group(1), re.M), entry.group(1)


def test_terraform_keeps_the_indexer_cloud_run_jobs_as_the_rollback():
    """The rollback is one line in profiles.py only while the Jobs exist."""
    text = LOCALS.read_text(encoding="utf-8")
    fallback = re.search(r"^  cloud_run_fallback_profiles = \[([^\]]*)\]", text, re.M)
    assert fallback, "cloud_run_fallback_profiles is missing: the switch would destroy the indexer Jobs"
    assert f'"{PROFILE}"' in fallback.group(1)


def test_request_62_records_the_owners_acceptance_and_the_rollback():
    text = REQUESTS.read_text(encoding="utf-8")
    start = text.index("## 62. ")
    end = text.find("\n## 63. ", start)
    section = text[start:end if end != -1 else len(text)]
    # scripts/lib/check-frozen-contract.sh passes a diff under
    # apps/common/swarm_common/ only beside an added line holding this phrase.
    assert "accepted by the owner" in section.lower()
    assert "#939" in section
    assert "CLOUD_RUN_JOB" in section, "the rollback (backend back to CLOUD_RUN_JOB) must be recorded"
