"""A Cloud Run Job the scheduler creates is the Job terraform creates.

THE DEFECT THIS PINS (gap audit D7, 2026-10-01). Terraform's Jobs
(terraform/modules/cloud_run_jobs/main.tf) have a MEMORY-medium tmpfs workspace
on launch stage GA, as CONTRACT.md's "Correction (workspace storage)" says. The
Jobs `CloudRunJobDispatcher._build_job` creates -- on every 404, so for every
self-service `u-*` tenant, every pool-served tenant and every non-default
resource class -- still asked for `MEDIUM_UNSPECIFIED`, the Preview
disk-backed ephemeral volume, on launch stage BETA: the one Cloud Run feature
the platform had decided not to use, and the one that disables live migration.

The parity half reads the terraform module's TEXT, because there is no plan to
read offline. It fails when either side moves alone: a terraform edit to the
medium, the launch stage, the workspace formula or its default fraction, or a
scheduler edit to any of them.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from swarm_common.config import Settings
from swarm_common.models import Tenant
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES

from scheduler.dispatch import (
    WORKSPACE_MEMORY_FRACTION,
    CloudRunJobDispatcher,
    workspace_size_gib,
)
from scheduler.settings import SchedulerSettings

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "terraform" / "modules" / "cloud_run_jobs"
INFRA_MAIN = ROOT / "terraform" / "infra" / "main.tf"
PROJECT = "saga-agents-staging"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

#: The formula terraform sizes the workspace with, as written in main.tf.
#: Held verbatim: a change to it there must be a change to
#: `workspace_size_gib` here, and this string is what makes that a red test
#: rather than a quiet divergence.
TERRAFORM_WORKSPACE_FORMULA = (
    "max(1, min(rc.disk_gib, floor(rc.memory_gib * var.workspace_memory_fraction)))"
)


def _settings() -> SchedulerSettings:
    return SchedulerSettings(
        core=Settings(project_id=PROJECT, artifact_bucket=f"{PROJECT}-swarm-artifacts"),
        project_id=PROJECT,
        region="us-central1",
        artifact_registry_host=f"us-central1-docker.pkg.dev/{PROJECT}/swarm-images",
        worker_image_tag="test",
    )


def _tenant() -> Tenant:
    return Tenant(
        tenant_id="u-alice",
        kind="user",
        principal="alice@saga.xyz",
        created_at=NOW,
        service_account=f"swarm-agent-worker-u-alice@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/u-alice",
        namespace="swarm-tenant-u-alice",
    )


def _job(resource_class: str | None = None):
    dispatcher = CloudRunJobDispatcher(_settings(), client=object())
    return dispatcher._build_job(RUNNER_PROFILES["mock"], _tenant(), resource_class)


def _job_resource_block(text: str) -> str:
    start = text.index('resource "google_cloud_run_v2_job" "this"')
    return text[start:]


def _terraform_job_fields() -> dict[str, str]:
    """launch_stage, the workspace volume's medium and size_limit, from main.tf."""
    block = _job_resource_block((MODULE / "main.tf").read_text())
    launch = re.search(r'^\s*launch_stage\s*=\s*"([A-Z_]+)"', block, re.M)
    volume = re.search(
        r'volumes\s*\{\s*name\s*=\s*"workspace"\s*empty_dir\s*\{(?P<body>.*?)\n\s*\}', block,
        re.S
    )
    assert launch, "no launch_stage on google_cloud_run_v2_job.this"
    assert volume, "no `workspace` empty_dir volume on google_cloud_run_v2_job.this"
    body = re.sub(r"#[^\n]*", "", volume.group("body"))
    medium = re.search(r'medium\s*=\s*"([A-Z_]+)"', body)
    size = re.search(r'size_limit\s*=\s*"([^"]+)"', body)
    assert medium and size, f"the workspace empty_dir names no medium or size_limit: {body!r}"
    return {"launch_stage": launch.group(1), "medium": medium.group(1),
            "size_limit": size.group(1)}


def _terraform_workspace_formula() -> str:
    text = (MODULE / "main.tf").read_text()
    match = re.search(r"workspace_gib\s*=\s*\{.*?name\s*=>\s*(?P<expr>[^\n]+)\n", text, re.S)
    assert match, "local.workspace_gib is not a `for name, rc in ... : name => <expr>` map"
    return " ".join(match.group("expr").split())


def _terraform_default_fraction() -> float:
    text = (MODULE / "variables.tf").read_text()
    match = re.search(
        r'variable\s+"workspace_memory_fraction"\s*\{.*?^\s*default\s*=\s*([0-9.]+)', text,
        re.S | re.M,
    )
    assert match, "variable workspace_memory_fraction carries no numeric default"
    return float(match.group(1))


def _terraform_fraction() -> float:
    """The fraction terraform/infra actually passes, or the module default."""
    text = INFRA_MAIN.read_text()
    start = text.index('module "cloud_run_jobs"')
    block = text[start:text.index("\n}\n", start)]
    override = re.search(r"^\s*workspace_memory_fraction\s*=\s*([^\n#]+)", block, re.M)
    if override is None:
        return _terraform_default_fraction()
    value = override.group(1).strip()
    assert re.fullmatch(r"[0-9.]+", value), (
        f"terraform/infra passes workspace_memory_fraction = {value!r}, which this test "
        "cannot evaluate offline; the scheduler's WORKSPACE_MEMORY_FRACTION must be "
        "held to it some other way before this test can stand"
    )
    return float(value)


# -- the scheduler's own Job ----------------------------------------------------

@pytest.mark.parametrize("resource_class", sorted(RESOURCE_CLASSES))
def test_the_scheduler_job_workspace_is_memory_on_ga(resource_class):
    from google.api import launch_stage_pb2
    from google.cloud import run_v2

    job = _job(resource_class)
    volume = job.template.template.volumes[0]
    assert volume.name == "workspace"
    assert volume.empty_dir.medium == run_v2.EmptyDirVolumeSource.Medium.MEMORY, (
        "MEDIUM_UNSPECIFIED is Cloud Run's Preview disk-backed ephemeral volume, "
        "which this platform does not use (CONTRACT.md, Correction (workspace storage))"
    )
    assert job.launch_stage == launch_stage_pb2.LaunchStage.GA
    assert job.launch_stage != launch_stage_pb2.LaunchStage.BETA


@pytest.mark.parametrize("resource_class", sorted(RESOURCE_CLASSES))
def test_the_workspace_never_reaches_the_memory_limit(resource_class):
    """A tmpfs workspace is charged against the container's memory; at the full
    limit a full workspace OOM-kills the agent (a SIGKILL: no checkpoint)."""
    rc = RESOURCE_CLASSES[resource_class]
    limit = _job(resource_class).template.template.volumes[0].empty_dir.size_limit
    assert int(limit.removesuffix("Gi")) < rc.memory_gib


# -- the scheduler's Job against terraform's ------------------------------------

def test_terraform_still_declares_what_this_test_compares_against():
    fields = _terraform_job_fields()
    assert fields["launch_stage"] == "GA"
    assert fields["medium"] == "MEMORY"
    assert fields["size_limit"] == "${local.workspace_gib[each.value.resource_class]}Gi"
    assert _terraform_workspace_formula() == TERRAFORM_WORKSPACE_FORMULA, (
        "terraform's workspace formula changed; change scheduler.dispatch."
        "workspace_size_gib to match, then this string"
    )


def test_the_scheduler_uses_terraforms_workspace_fraction():
    assert WORKSPACE_MEMORY_FRACTION == _terraform_fraction()


@pytest.mark.parametrize("resource_class", sorted(RESOURCE_CLASSES))
def test_the_scheduler_job_has_terraforms_volume_and_launch_stage(resource_class):
    from google.api import launch_stage_pb2
    from google.cloud import run_v2

    fields = _terraform_job_fields()
    rc = RESOURCE_CLASSES[resource_class]
    fraction = _terraform_fraction()
    # TERRAFORM_WORKSPACE_FORMULA, evaluated as terraform evaluates it.
    terraform_gib = max(1, min(rc.disk_gib, math.floor(rc.memory_gib * fraction)))
    expected_size = fields["size_limit"].replace(
        "${local.workspace_gib[each.value.resource_class]}", str(terraform_gib)
    )

    job = _job(resource_class)
    volume = job.template.template.volumes[0]
    assert run_v2.EmptyDirVolumeSource.Medium(volume.empty_dir.medium).name == fields["medium"]
    assert volume.empty_dir.size_limit == expected_size
    assert launch_stage_pb2.LaunchStage.Name(job.launch_stage) == fields["launch_stage"]
    assert workspace_size_gib(rc) == terraform_gib


# -- jobs the scheduler made before this change ---------------------------------

class _JobsClient:
    def __init__(self, job) -> None:
        self.job = job
        self.updated: list = []

    def get_job(self, request):
        return self.job

    def update_job(self, request):
        self.updated.append(request.job)

        class _Op:
            def result(self, timeout=None):
                return None

        return _Op()


def _stale_preview_job(dispatcher: CloudRunJobDispatcher):
    """A scheduler-made Job as `_build_job` wrote it before D7: same image,
    same environment, Preview disk on BETA."""
    from google.api import launch_stage_pb2
    from google.cloud import run_v2

    job = dispatcher._build_job(RUNNER_PROFILES["mock"], _tenant())
    job.launch_stage = launch_stage_pb2.LaunchStage.BETA
    job.template.template.volumes[0].empty_dir.medium = (
        run_v2.EmptyDirVolumeSource.Medium.MEDIUM_UNSPECIFIED
    )
    job.template.template.volumes[0].empty_dir.size_limit = "4Gi"
    return job


def test_a_scheduler_job_left_on_the_preview_disk_is_rebuilt():
    """Changing `_build_job` alone moves only Jobs created from now on. A Job
    created before it, at today's image, would otherwise keep the Preview disk
    until the next image change happened to rebuild it."""
    from google.api import launch_stage_pb2
    from google.cloud import run_v2

    dispatcher = CloudRunJobDispatcher(_settings(), client=object())
    client = _JobsClient(_stale_preview_job(dispatcher))
    dispatcher._client = client

    dispatcher.ensure_job(RUNNER_PROFILES["mock"], _tenant())

    assert len(client.updated) == 1, "the stale Job was not rewritten"
    rebuilt = client.updated[0]
    assert rebuilt.launch_stage == launch_stage_pb2.LaunchStage.GA
    assert (rebuilt.template.template.volumes[0].empty_dir.medium
            == run_v2.EmptyDirVolumeSource.Medium.MEMORY)


@pytest.mark.parametrize("resource_class", sorted(RESOURCE_CLASSES))
def test_a_current_scheduler_job_is_not_rewritten(resource_class):
    """The drift check compares against what `_build_job` writes without
    building a Job; this is what holds the two to one answer, per class."""
    dispatcher = CloudRunJobDispatcher(_settings(), client=object())
    client = _JobsClient(
        dispatcher._build_job(RUNNER_PROFILES["mock"], _tenant(), resource_class)
    )
    dispatcher._client = client

    dispatcher.ensure_job(RUNNER_PROFILES["mock"], _tenant(), resource_class)

    assert client.updated == []


def test_a_job_reading_back_an_unspecified_launch_stage_is_not_rewritten():
    """A GA Job created without the field reads back LAUNCH_STAGE_UNSPECIFIED.
    That is not the Preview volume's mark, so it must not rewrite the Job on
    every scheduler start; a BETA one (above) still is."""
    from google.api import launch_stage_pb2

    dispatcher = CloudRunJobDispatcher(_settings(), client=object())
    job = dispatcher._build_job(RUNNER_PROFILES["mock"], _tenant())
    job.launch_stage = launch_stage_pb2.LaunchStage.LAUNCH_STAGE_UNSPECIFIED
    client = _JobsClient(job)
    dispatcher._client = client

    dispatcher.ensure_job(RUNNER_PROFILES["mock"], _tenant())

    assert client.updated == []
