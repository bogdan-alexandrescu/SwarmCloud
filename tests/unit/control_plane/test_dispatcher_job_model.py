"""A Job the scheduler creates runs the same model as a Job Terraform creates (#226).

Owner decision of 2026-09-26: the `claude-code` profile's Cloud Run Job sets
`MODEL=claude-opus-5-5`, in Terraform, so every step runs that model rather
than the CLI's default. Terraform's Jobs are only some of the Jobs that run:
`CloudRunJobDispatcher.ensure_job` CREATES one whenever `get_job` 404s -- for
every self-service `u-<email>` tenant Terraform does not list, and for a
resource class other than the profile's own. Those Jobs carried no `MODEL`, so
their agents would have run the CLI's default model while Terraform's ran the
pinned one.

So the value is stated ONCE, in `terraform/infra/locals.tf` (`runner_models`),
and reaches the scheduler as `WORKER_MODELS`, the way the image digests reach it
as `WORKER_IMAGE_REFS`. These tests hold the scheduler's half:

  * `WORKER_MODELS` parses to {profile: model}, and a value that could not be a
    model map refuses to start rather than dispatching without it;
  * a Job built for claude-code carries `MODEL`, and one for a profile with no
    model carries none;
  * a Job the scheduler created BEFORE this, with no `MODEL`, is rebuilt once,
    before its next execution; a Terraform Job is never touched;
  * the per-execution environment still carries no `MODEL`: a task's own
    `model` is attribution, and test_model_flag_is_attribution_only.py is the
    deliberate brake on wiring it through.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.models import Lease, Task, Tenant
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import TaskState

from scheduler.dispatch import CloudRunJobDispatcher, job_id_for, worker_env
from scheduler.settings import SchedulerSettings, parse_worker_models

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import FakeJobsClient

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
PINNED_MODEL = "claude-opus-5-5"


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="u-alice",
        kind="user",
        principal="alice@saga.xyz",
        created_at=NOW,
        service_account=f"swarm-agent-worker-u-alice@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/u-alice",
        namespace="swarm-tenant-u-alice",
    )


def _task(profile_name: str, *, model: str | None = None) -> Task:
    profile = RUNNER_PROFILES[profile_name]
    return Task(
        id="task_abc123",
        tenant_id="u-alice",
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.LEASED,
        runner_profile=profile_name,
        resource_class=profile.resource_class,
        input={},
        submitted_by="alice@saga.xyz",
        provider=profile.provider,
        model=model,
        timeout_seconds=1800,
    )


def _lease(task: Task) -> Lease:
    return Lease(
        lease_id="lease_1",
        task_id=task.id,
        attempt_id="att_1",
        tenant_id=task.tenant_id,
        generation=3,
        pools=["global"],
        units=1,
        state=TaskState.LEASED,
        created_at=NOW,
        dispatch_deadline=NOW + timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=2),
    )


def _plain_env(job) -> dict[str, str]:
    return {
        e.name: e.value
        for e in job.template.template.containers[0].env
        if not e.value_source.secret_key_ref.secret
    }


# -- the setting -------------------------------------------------------------

def test_worker_models_parses_to_a_profile_map(monkeypatch):
    assert parse_worker_models("") == {}
    assert parse_worker_models('{"claude-code": "claude-opus-5-5"}') == {
        "claude-code": PINNED_MODEL
    }
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.setenv("WORKER_MODELS", '{"claude-code": "claude-opus-5-5"}')
    assert SchedulerSettings.from_env().worker_models == {"claude-code": PINNED_MODEL}
    monkeypatch.delenv("WORKER_MODELS")
    assert SchedulerSettings.from_env().worker_models == {}


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '["claude-code"]',
        '{"no-such-profile": "claude-opus-5-5"}',
        '{"claude-code": ""}',
        '{"claude-code": 5}',
    ],
)
def test_a_worker_models_value_that_is_not_a_model_map_refuses_to_start(raw):
    with pytest.raises(ValueError, match="WORKER_MODELS"):
        parse_worker_models(raw)


def test_a_settings_object_built_directly_is_checked_too():
    with pytest.raises(ValueError, match="WORKER_MODELS"):
        scheduler_settings(worker_models={"no-such-profile": PINNED_MODEL})


# -- the Job the scheduler creates -------------------------------------------

def test_a_claude_code_job_the_scheduler_creates_carries_the_pinned_model(tenant):
    settings = scheduler_settings(worker_models={"claude-code": PINNED_MODEL})
    dispatcher = CloudRunJobDispatcher(settings, client=FakeJobsClient())

    job = dispatcher._build_job(RUNNER_PROFILES["claude-code"], tenant)

    assert _plain_env(job).get("MODEL") == PINNED_MODEL


def test_a_profile_with_no_model_gets_no_model_variable(tenant):
    settings = scheduler_settings(worker_models={"claude-code": PINNED_MODEL})
    dispatcher = CloudRunJobDispatcher(settings, client=FakeJobsClient())

    for name in ("mock", "generic"):
        assert "MODEL" not in _plain_env(dispatcher._build_job(RUNNER_PROFILES[name], tenant))


def test_a_new_job_is_created_with_the_model(tenant):
    settings = scheduler_settings(worker_models={"claude-code": PINNED_MODEL})
    client = FakeJobsClient()
    task = _task("claude-code")

    CloudRunJobDispatcher(settings, client=client).dispatch(
        task=task, lease=_lease(task), profile=RUNNER_PROFILES["claude-code"], tenant=tenant
    )

    assert len(client.created) == 1
    assert _plain_env(client.created[0]["job"]).get("MODEL") == PINNED_MODEL


def _scheduler_job_from_before(tenant: Tenant):
    """A Job this dispatcher created before `WORKER_MODELS` existed."""
    builder = CloudRunJobDispatcher(scheduler_settings(), client=object())
    job = builder._build_job(RUNNER_PROFILES["claude-code"], tenant)
    job.name = builder.job_name(job_id_for(tenant.tenant_id, "claude-code"))
    assert "MODEL" not in _plain_env(job)
    assert job.labels["managed-by"] == "swarm-scheduler"
    return job


def test_a_scheduler_job_without_the_model_is_rebuilt_once_before_it_runs(tenant):
    seeded = _scheduler_job_from_before(tenant)
    client = FakeJobsClient(jobs={seeded.name: seeded})
    settings = scheduler_settings(worker_models={"claude-code": PINNED_MODEL})
    dispatcher = CloudRunJobDispatcher(settings, client=client)
    task = _task("claude-code")

    dispatcher.dispatch(
        task=task, lease=_lease(task), profile=RUNNER_PROFILES["claude-code"], tenant=tenant
    )

    assert client.calls == ["get_job", "update_job", "run_job"], client.calls
    assert _plain_env(client.jobs[seeded.name]).get("MODEL") == PINNED_MODEL
    # Once per job per process, not on every dispatch.
    dispatcher.dispatch(
        task=task, lease=_lease(task), profile=RUNNER_PROFILES["claude-code"], tenant=tenant
    )
    assert client.calls.count("update_job") == 1


def test_a_scheduler_job_already_at_the_model_is_not_rewritten(tenant):
    """The common case once the first dispatch has corrected it costs no write."""
    settings = scheduler_settings(worker_models={"claude-code": PINNED_MODEL})
    builder = CloudRunJobDispatcher(settings, client=object())
    current = builder._build_job(RUNNER_PROFILES["claude-code"], tenant)
    current.name = builder.job_name(job_id_for(tenant.tenant_id, "claude-code"))
    client = FakeJobsClient(jobs={current.name: current})
    task = _task("claude-code")

    CloudRunJobDispatcher(settings, client=client).dispatch(
        task=task, lease=_lease(task), profile=RUNNER_PROFILES["claude-code"], tenant=tenant
    )

    assert client.calls == ["get_job", "run_job"], client.calls


def test_a_terraform_job_is_never_rewritten_for_the_model(tenant):
    """Terraform's Jobs are Terraform's: the next apply sets their MODEL."""
    seeded = _scheduler_job_from_before(tenant)
    seeded.labels["managed-by"] = "swarm-terraform"
    client = FakeJobsClient(jobs={seeded.name: seeded})
    settings = scheduler_settings(worker_models={"claude-code": PINNED_MODEL})
    task = _task("claude-code")

    CloudRunJobDispatcher(settings, client=client).dispatch(
        task=task, lease=_lease(task), profile=RUNNER_PROFILES["claude-code"], tenant=tenant
    )

    assert "update_job" not in client.calls, client.calls


def test_the_execution_environment_still_carries_no_model(tenant):
    """The model is on the JOB, never in the per-execution environment a task
    shapes. A task's own `model` field names a model and still selects none."""
    settings = scheduler_settings(worker_models={"claude-code": PINNED_MODEL})
    task = _task("claude-code", model="claude-haiku-5")

    env = worker_env(task=task, lease=_lease(task), tenant=tenant, settings=settings)

    assert "MODEL" not in env
    assert "claude-haiku-5" not in env.values()
