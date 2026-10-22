"""A Cloud Run Job the scheduler creates gets terraform's Direct VPC egress (#1893).

THE DEFECT THIS PINS (2026-10-21). Terraform's worker Jobs
(terraform/modules/cloud_run_jobs/main.tf `vpc_access`) leave through the swarm
network: egress ALL_TRAFFIC, one interface on the swarm network and subnetwork,
tagged `swarm-worker`. The Jobs `CloudRunJobDispatcher._build_job` creates --
every self-service `u-<email>` tenant's -- had no `vpc_access` at all. A worker
switched to state-through-swarm-api then reached swarm-api from the internet,
and swarm-api's `internal-and-cloud-load-balancing` ingress answered 404: the
2026-10-21 isolation cutover a1 rollback (PR 1892).

The fix ships WIRED AND OFF (`tenant_job_vpc_egress`, terraform/infra
tenant_job_vpc.tf): off, the Job is built exactly as before; on, it carries the
block from the scheduler's settings, and `_refresh_image` brings a Job this
dispatcher made earlier up to it -- never a terraform-made one.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from swarm_common.config import Settings
from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES

from scheduler.dispatch import CloudRunJobDispatcher
from scheduler.settings import SchedulerSettings

ROOT = Path(__file__).resolve().parents[3]
JOBS_MODULE = ROOT / "terraform" / "modules" / "cloud_run_jobs" / "main.tf"
PROJECT = "saga-agents-staging"
NOW = datetime(2026, 10, 21, 23, 0, tzinfo=timezone.utc)
NETWORK = f"projects/{PROJECT}/global/networks/swarm-vpc"
SUBNETWORK = f"projects/{PROJECT}/regions/us-central1/subnetworks/swarm-subnet-us-central1"
TAG = "swarm-worker"


def _settings(*, vpc: bool) -> SchedulerSettings:
    extra = {}
    if vpc:
        extra = dict(
            tenant_job_vpc_egress=True,
            tenant_job_network=NETWORK,
            tenant_job_subnetwork=SUBNETWORK,
            tenant_job_network_tags=(TAG,),
        )
    return SchedulerSettings(
        core=Settings(project_id=PROJECT, artifact_bucket=f"{PROJECT}-swarm-artifacts"),
        project_id=PROJECT,
        region="us-central1",
        artifact_registry_host=f"us-central1-docker.pkg.dev/{PROJECT}/swarm-images",
        worker_image_tag="test",
        **extra,
    )


def _tenant() -> Tenant:
    return Tenant(
        tenant_id="u-alice",
        kind="user",
        principal="alice@example.com",
        created_at=NOW,
        service_account=f"swarm-agent-worker-u-alice@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/u-alice",
        namespace="swarm-tenant-u-alice",
    )


def _build(*, vpc: bool):
    dispatcher = CloudRunJobDispatcher(_settings(vpc=vpc), client=object())
    return dispatcher._build_job(RUNNER_PROFILES["mock"], _tenant(), None)


def _has_vpc_access(job) -> bool:
    from google.cloud import run_v2

    return run_v2.TaskTemplate.pb(job.template.template).HasField("vpc_access")


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


def _ensure(dispatcher: CloudRunJobDispatcher, existing) -> _JobsClient:
    client = _JobsClient(existing)
    dispatcher._client = client
    dispatcher.ensure_job(RUNNER_PROFILES["mock"], _tenant())
    return client


# -- the builder ------------------------------------------------------------------

def test_switch_off_builds_the_job_with_no_vpc_access():
    # MUTATION: set vpc_access unconditionally in `_build_job` and this fails:
    # merging the switch off would move every scheduler-made Job onto a subnet
    # the scheduler holds no grant for.
    assert not _has_vpc_access(_build(vpc=False))


def test_switch_on_builds_terraforms_vpc_access_from_settings():
    from google.cloud import run_v2

    vpc = _build(vpc=True).template.template.vpc_access
    # MUTATION: PRIVATE_RANGES_ONLY (or no egress) and provider calls leave
    # outside the swarm Cloud NAT, without the allow-listed addresses.
    assert vpc.egress == run_v2.VpcAccess.VpcEgress.ALL_TRAFFIC
    # MUTATION: a second interface, or none, and the Job is not terraform's.
    assert len(vpc.network_interfaces) == 1
    nic = vpc.network_interfaces[0]
    # MUTATION: read the network from anything but settings and this fails.
    assert nic.network == NETWORK
    # MUTATION: read the subnetwork from anything but settings and this fails.
    assert nic.subnetwork == SUBNETWORK
    # MUTATION: drop the tags and the worker-ingress deny rule cannot name it.
    assert list(nic.tags) == [TAG]


def test_terraforms_jobs_still_say_all_traffic():
    """The builder mirrors terraform's egress value; this fails if terraform's
    module moves off ALL_TRAFFIC alone."""
    text = JOBS_MODULE.read_text()
    block = text[text.index("vpc_access {"):]
    # MUTATION: change terraform's egress and this fails until the builder follows.
    assert re.search(r'egress\s*=\s*"ALL_TRAFFIC"', block[:2000])


# -- the refresh -------------------------------------------------------------------

def test_a_scheduler_made_job_missing_the_block_is_refreshed_onto_it():
    from google.cloud import run_v2

    stale = _build(vpc=False)  # what this dispatcher made before the switch
    client = _ensure(CloudRunJobDispatcher(_settings(vpc=True), client=object()), stale)

    # MUTATION: drop the VPC check from `_refresh_image` and swarm-job-u-bogdan-*
    # keep reaching swarm-api from the internet until deleted by hand.
    assert len(client.updated) == 1
    vpc = client.updated[0].template.template.vpc_access
    assert vpc.egress == run_v2.VpcAccess.VpcEgress.ALL_TRAFFIC
    assert vpc.network_interfaces[0].subnetwork == SUBNETWORK


def test_a_terraform_made_job_is_never_refreshed():
    terraform_job = _build(vpc=False)
    terraform_job.labels["managed-by"] = "swarm-terraform"
    client = _ensure(CloudRunJobDispatcher(_settings(vpc=True), client=object()), terraform_job)

    # MUTATION: drop the managed-by check and the scheduler rewrites terraform's Jobs.
    assert client.updated == []


def test_a_job_already_on_the_template_is_not_rewritten():
    current = _build(vpc=True)
    client = _ensure(CloudRunJobDispatcher(_settings(vpc=True), client=object()), current)

    # MUTATION: report drift whenever the switch is on and every Job is
    # rewritten once per scheduler process.
    assert client.updated == []


def test_a_job_reading_back_short_names_is_not_rewritten():
    """Cloud Run may read the network back by name rather than full resource
    name. That is the same subnetwork and must not rewrite the Job on every start."""
    current = _build(vpc=True)
    nic = current.template.template.vpc_access.network_interfaces[0]
    nic.network = "swarm-vpc"
    nic.subnetwork = "swarm-subnet-us-central1"
    client = _ensure(CloudRunJobDispatcher(_settings(vpc=True), client=object()), current)

    # MUTATION: compare full strings and this Job is rewritten every process.
    assert client.updated == []


def test_a_job_on_another_subnet_is_moved_to_the_swarm_one():
    wrong = _build(vpc=True)
    wrong.template.template.vpc_access.network_interfaces[0].subnetwork = (
        f"projects/{PROJECT}/regions/us-central1/subnetworks/agents-staging-subnet"
    )
    client = _ensure(CloudRunJobDispatcher(_settings(vpc=True), client=object()), wrong)

    # MUTATION: check only that a block exists, and this Job stays on the wrong subnet.
    assert len(client.updated) == 1
    assert client.updated[0].template.template.vpc_access.network_interfaces[0].subnetwork == SUBNETWORK


def test_switch_off_does_not_refresh_for_vpc():
    client = _ensure(CloudRunJobDispatcher(_settings(vpc=False), client=object()), _build(vpc=False))

    # MUTATION: treat a missing block as drift with the switch off and every
    # scheduler-made Job is rewritten for nothing.
    assert client.updated == []


# -- the settings ------------------------------------------------------------------

def test_settings_read_the_switch_and_the_names_from_the_environment(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("TENANT_JOB_VPC_EGRESS", "true")
    monkeypatch.setenv("TENANT_JOB_NETWORK", NETWORK)
    monkeypatch.setenv("TENANT_JOB_SUBNETWORK", SUBNETWORK)
    monkeypatch.setenv("TENANT_JOB_NETWORK_TAGS", TAG)
    settings = SchedulerSettings.from_env()

    # MUTATION: rename any of the four environment variables and this fails.
    assert settings.tenant_job_vpc_egress is True
    assert settings.tenant_job_network == NETWORK
    assert settings.tenant_job_subnetwork == SUBNETWORK
    assert settings.tenant_job_network_tags == (TAG,)


def test_settings_default_the_switch_off(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    for name in ("TENANT_JOB_VPC_EGRESS", "TENANT_JOB_NETWORK",
                 "TENANT_JOB_SUBNETWORK", "TENANT_JOB_NETWORK_TAGS"):
        monkeypatch.delenv(name, raising=False)

    # MUTATION: default the switch on and an unconfigured scheduler builds VPC Jobs.
    assert SchedulerSettings.from_env().tenant_job_vpc_egress is False


@pytest.mark.parametrize("missing", ["tenant_job_network", "tenant_job_subnetwork",
                                     "tenant_job_network_tags"])
def test_switch_on_without_a_name_is_refused_at_start(missing):
    kwargs = dict(
        tenant_job_vpc_egress=True,
        tenant_job_network=NETWORK,
        tenant_job_subnetwork=SUBNETWORK,
        tenant_job_network_tags=(TAG,),
    )
    kwargs[missing] = () if missing == "tenant_job_network_tags" else ""
    # MUTATION: drop the check and the Job is created on an empty subnetwork,
    # refused by Cloud Run at every dispatch rather than once at start.
    with pytest.raises(ValueError, match="TENANT_JOB_VPC_EGRESS"):
        SchedulerSettings(
            core=Settings(project_id=PROJECT, artifact_bucket=f"{PROJECT}-swarm-artifacts"),
            project_id=PROJECT,
            **kwargs,
        )
