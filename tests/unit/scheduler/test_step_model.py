"""A step runs its model class's pin, on both backends, from one place (lane
MODEL-SCHED of docs/design/model-per-step.md; part of #1435).

The scheduler reads WORKER_CLASS_MODELS (terraform's `local.runner_models`,
keyed by class) and holds it to the frozen `MODEL_CLASSES` when it STARTS:
exactly the four classes, each pinned to an ID its class `admits`. At
dispatch, `step_model` resolves the step's class with the frozen
`step_model_class` and `worker_env` carries that class's pin as `MODEL`, so a
GKE pod and a Cloud Run execution of the same step run the same model. A step
naming no class runs its profile's default (`opus`, R3). The Cloud Run Job's
own `MODEL` stays the default class's pin, overridden per execution.

MUTATIONS: drop the key check in `check_worker_class_models` and the missing
and extra cases fail; drop the `admits` check and the wrong-class case fails;
drop the `MODEL` line from `worker_env` and every render case fails; read
`profile.default_model_class` instead of the step's class there and the
haiku renders fail; leave the GKE manifest's `profile_model` append
unconditional and the GKE haiku render carries two MODELs.
"""

from __future__ import annotations

import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.conftest import PROJECT, scheduler_settings  # noqa: E402
from control_plane.test_dispatch_manifests import FakeBatchApi, FakeJobsClient  # noqa: E402

from swarm_common.models import Lease, Task, Tenant  # noqa: E402
from swarm_common.profiles import MODEL_CLASSES, RUNNER_PROFILES  # noqa: E402
from swarm_common.states import TaskState  # noqa: E402

from scheduler.dispatch import (  # noqa: E402
    CloudRunJobDispatcher,
    DispatchError,
    GkeJobDispatcher,
    GkeTarget,
    step_model,
    worker_env,
)
from scheduler.settings import (  # noqa: E402
    SchedulerSettings,
    check_worker_class_models,
    parse_worker_class_models,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)

#: terraform/infra/locals.tf `runner_models`, as WORKER_CLASS_MODELS carries
#: it. Test input: every assertion below compares a render to THIS map.
CLASS_MODELS = {
    "opus": "claude-opus-5-5",
    "fable": "claude-fable-5-1",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}

#: Catalogue profiles that run no model class (the design's §4.1 table).
CLASSLESS = sorted(
    name for name, p in RUNNER_PROFILES.items()
    if not p.model_classes and not p.worker_action
)


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


def _task(profile_name: str = "claude-code", *, model_class: str | None = None,
          model: str | None = None, input: dict | None = None,
          metadata: dict | None = None) -> Task:
    profile = RUNNER_PROFILES[profile_name]
    return Task(
        id="task_abc123",
        tenant_id="u-alice",
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.LEASED,
        runner_profile=profile_name,
        resource_class=profile.resource_class,
        input=dict(input or {}),
        submitted_by="alice@saga.xyz",
        provider=profile.provider,
        model=model,
        model_class=model_class,
        metadata=dict(metadata or {}),
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
        dispatch_deadline=NOW + timedelta(minutes=8),
        expires_at=NOW + timedelta(minutes=2),
    )


def _settings(**overrides):
    base = {"worker_class_models": dict(CLASS_MODELS)}
    base.update(overrides)
    return scheduler_settings(**base)


# -- at start ----------------------------------------------------------------


def test_the_terraform_class_map_is_accepted():
    check_worker_class_models(dict(CLASS_MODELS))
    assert _settings().worker_class_models == CLASS_MODELS


def test_a_class_pinned_to_another_class_s_id_refuses_to_start():
    """`haiku = "claude-opus-5-5"` would bill every haiku step as Opus."""
    wrong = dict(CLASS_MODELS, haiku=CLASS_MODELS["opus"])
    with pytest.raises(ValueError, match="haiku"):
        _settings(worker_class_models=wrong)


def test_a_pin_from_an_older_generation_refuses_to_start():
    with pytest.raises(ValueError, match="sonnet"):
        _settings(worker_class_models=dict(CLASS_MODELS, sonnet="claude-sonnet-4-6"))


@pytest.mark.parametrize("missing", sorted(MODEL_CLASSES))
def test_a_missing_class_refuses_to_start(missing):
    partial = {k: v for k, v in CLASS_MODELS.items() if k != missing}
    with pytest.raises(ValueError, match=missing):
        _settings(worker_class_models=partial)


def test_an_extra_key_refuses_to_start():
    with pytest.raises(ValueError, match="opus-fast"):
        _settings(worker_class_models=dict(CLASS_MODELS, **{"opus-fast": "claude-opus-5-5"}))


def test_a_profile_keyed_map_in_the_class_variable_refuses_to_start():
    """WORKER_MODELS's shape in WORKER_CLASS_MODELS: every key is wrong."""
    with pytest.raises(ValueError, match="WORKER_CLASS_MODELS"):
        _settings(worker_class_models={"claude-code": "claude-opus-5-5"})


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '["claude-opus-5-5"]',
        '{"opus": "claude-opus-5-5"}',
        '{"opus": "claude-opus-5-5", "fable": "claude-fable-5-1", '
        '"sonnet": "claude-sonnet-5-5", "haiku": "claude-opus-5-5"}',
    ],
)
def test_the_environment_variable_refuses_the_same_maps(raw):
    with pytest.raises(ValueError, match="WORKER_CLASS_MODELS"):
        parse_worker_class_models(raw)


def test_the_environment_variable_reaches_the_settings(monkeypatch):
    import json

    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.setenv("WORKER_CLASS_MODELS", json.dumps(CLASS_MODELS))
    assert SchedulerSettings.from_env().worker_class_models == CLASS_MODELS
    monkeypatch.delenv("WORKER_CLASS_MODELS")
    assert SchedulerSettings.from_env().worker_class_models == {}


# -- the render, on both backends ----------------------------------------------


def _gke_models(settings, tenant: Tenant, task: Task) -> list[str]:
    api = FakeBatchApi()
    GkeJobDispatcher(
        settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api
    ).dispatch(
        task=task, lease=_lease(task), profile=RUNNER_PROFILES[task.runner_profile],
        tenant=tenant,
    )
    container = api.created[0][1]["spec"]["template"]["spec"]["containers"][0]
    assert container["name"] == "worker"
    return [e["value"] for e in container["env"] if e["name"] == "MODEL"]


def _cloud_run(settings, tenant: Tenant, task: Task) -> tuple[list[str], list[str]]:
    """(the execution override's MODELs, the Job's own MODELs)."""
    client = FakeJobsClient()
    CloudRunJobDispatcher(settings, client=client).dispatch(
        task=task, lease=_lease(task), profile=RUNNER_PROFILES[task.runner_profile],
        tenant=tenant,
    )
    (run,) = client.runs
    (override,) = run["overrides"].container_overrides
    execution = [e.value for e in override.env if e.name == "MODEL"]
    job = client.created[0]["job"]
    on_job = [e.value for e in job.template.template.containers[0].env if e.name == "MODEL"]
    return execution, on_job


@pytest.mark.parametrize("profile_name", ["claude-code", "claude-code-browser", "indexer"])
def test_a_haiku_step_renders_the_haiku_pin_on_gke(tenant, profile_name):
    task = _task(profile_name, model_class="haiku")
    assert _gke_models(_settings(), tenant, task) == [CLASS_MODELS["haiku"]]


@pytest.mark.parametrize("profile_name", ["claude-code", "claude-code-browser", "indexer"])
def test_a_classless_step_renders_the_opus_pin_on_gke(tenant, profile_name):
    task = _task(profile_name)
    assert _gke_models(_settings(), tenant, task) == [CLASS_MODELS["opus"]]


def test_a_haiku_step_renders_the_haiku_pin_on_cloud_run(tenant):
    """The execution override carries the step's pin; the Job keeps opus."""
    execution, on_job = _cloud_run(_settings(), tenant, _task(model_class="haiku"))
    assert execution == [CLASS_MODELS["haiku"]]
    assert on_job == [CLASS_MODELS["opus"]], "the Job's own MODEL is the default class's"


def test_a_classless_step_renders_the_opus_pin_on_cloud_run(tenant):
    execution, on_job = _cloud_run(_settings(), tenant, _task())
    assert execution == [CLASS_MODELS["opus"]]
    assert on_job == [CLASS_MODELS["opus"]]


@pytest.mark.parametrize("model_class", sorted(MODEL_CLASSES))
def test_every_class_renders_its_own_pin(tenant, model_class):
    task = _task(model_class=model_class)
    env = worker_env(task=task, lease=_lease(task), tenant=tenant, settings=_settings())
    assert env["MODEL"] == CLASS_MODELS[model_class]


def test_the_job_s_model_follows_the_class_map_over_worker_models(tenant):
    """With both maps set, the Job's MODEL is the class map's default pin."""
    settings = _settings(worker_models={"claude-code": "claude-opus-5"})
    _, on_job = _cloud_run(settings, tenant, _task())
    assert on_job == [CLASS_MODELS["opus"]]


# -- refusals at dispatch --------------------------------------------------------


def test_a_class_the_profile_does_not_accept_refuses_on_cloud_run_before_any_call(tenant):
    """claude-code-review accepts opus only. Refused, not defaulted, and the
    Job is never touched."""
    client = FakeJobsClient()
    task = _task("claude-code-review", model_class="haiku")
    with pytest.raises(DispatchError) as raised:
        CloudRunJobDispatcher(_settings(), client=client).dispatch(
            task=task, lease=_lease(task), profile=RUNNER_PROFILES["claude-code-review"],
            tenant=tenant,
        )
    assert raised.value.code == "model_not_pinned"
    assert client.calls == []


def test_a_class_with_no_pin_refuses_rather_than_running_the_cli_default(tenant):
    """No class map (a scheduler configured before #1435): a step naming a
    class is refused, never run on the profile's or the CLI's default."""
    settings = scheduler_settings(worker_models={"claude-code": CLASS_MODELS["opus"]})
    task = _task(model_class="sonnet")
    with pytest.raises(DispatchError) as raised:
        step_model(settings, RUNNER_PROFILES["claude-code"], task)
    assert raised.value.code == "model_not_pinned"
    with pytest.raises(DispatchError):
        _gke_models(settings, tenant, task)


def test_without_a_class_map_a_classless_step_runs_as_before(tenant):
    """The one release in which terraform still derives WORKER_MODELS: a
    scheduler without the class map renders claude-code exactly as #226 did
    -- on the Job and the pod, never in the per-execution environment."""
    settings = scheduler_settings(worker_models={"claude-code": CLASS_MODELS["opus"]})
    task = _task()
    env = worker_env(task=task, lease=_lease(task), tenant=tenant, settings=settings)
    assert "MODEL" not in env
    assert _gke_models(settings, tenant, task) == [CLASS_MODELS["opus"]]
    execution, on_job = _cloud_run(settings, tenant, task)
    assert execution == [] and on_job == [CLASS_MODELS["opus"]]


@pytest.mark.parametrize("profile_name", CLASSLESS)
def test_a_classless_profile_gets_no_model_on_either_backend(tenant, profile_name):
    task = _task(profile_name)
    env = worker_env(task=task, lease=_lease(task), tenant=tenant, settings=_settings())
    assert "MODEL" not in env
    assert set(CLASS_MODELS.values()).isdisjoint(env.values())


@pytest.mark.parametrize("profile_name", CLASSLESS)
def test_a_class_on_a_classless_profile_refuses(tenant, profile_name):
    task = _task(profile_name, model_class="haiku")
    with pytest.raises(DispatchError) as raised:
        worker_env(task=task, lease=_lease(task), tenant=tenant, settings=_settings())
    assert raised.value.code == "model_not_pinned"


def test_the_resolved_pin_is_the_settings_value_and_not_a_restatement(tenant):
    """A pin this test has never seen still comes through: the value is read
    from the scheduler's own map, not from a table in the dispatcher."""
    minor = 6 + secrets.randbelow(90)
    pins = dict(CLASS_MODELS, haiku=f"claude-haiku-5-{minor}")
    task = _task(model_class="haiku")
    env = worker_env(task=task, lease=_lease(task), tenant=tenant,
                     settings=_settings(worker_class_models=pins))
    assert env["MODEL"] == pins["haiku"]
