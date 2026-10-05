"""The scheduler hands every worker its wake topic, so the worker can ring it (#636).

The worker publishes a `task_finished` wake when it ends a task
(`agent_worker.finishwake`); the topic it publishes on is the scheduler's own
DISPATCH_TOPIC setting, passed through `worker_env` like the broker URL --
a platform endpoint, never anything a caller sent (invariant 10). Omitted, not
set empty, when the scheduler has none: a Cloud Run execution override MERGES
into the Job's environment, and an empty value would override one terraform
had baked in.
"""

from __future__ import annotations

from types import SimpleNamespace

from scheduler.dispatch import gke_worker_env, worker_env

from .conftest import scheduler_settings
from .test_dispatch_manifests import make_lease, make_task
from .test_worker_env_console_links import _tenant


def _env(settings, render=worker_env) -> dict[str, str]:
    task = make_task()
    return render(task=task, lease=make_lease(task), tenant=_tenant(), settings=settings)


def test_worker_env_carries_the_wake_topic():
    env = _env(scheduler_settings(dispatch_topic="swarm-scheduler-wake"))
    assert env["DISPATCH_TOPIC"] == "swarm-scheduler-wake"


def test_the_gke_worker_gets_the_wake_topic_too():
    env = _env(scheduler_settings(dispatch_topic="swarm-scheduler-wake"), gke_worker_env)
    assert env["DISPATCH_TOPIC"] == "swarm-scheduler-wake"


def test_worker_env_omits_an_empty_wake_topic():
    assert "DISPATCH_TOPIC" not in _env(scheduler_settings(dispatch_topic="  "))
    assert "DISPATCH_TOPIC" not in _env(scheduler_settings())


def test_worker_env_tolerates_settings_without_the_field():
    base = scheduler_settings()
    bare = SimpleNamespace(project_id=base.project_id, region=base.region, core=base.core)
    assert "DISPATCH_TOPIC" not in _env(bare)
