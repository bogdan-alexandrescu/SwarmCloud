"""The scheduler hands every worker the console origin and the PR-links switch.

Owner decision, 2026-10-01: a pull request a worker opens carries the
workflow and agent console links ONLY when the platform switch is on, and it
is OFF BY DEFAULT. The two values are the scheduler's own platform settings
(SWARM_CONSOLE_URL, SWARM_PR_CONSOLE_LINKS on the swarm-scheduler service),
never anything a caller sent (invariant 10).

WHAT IS PINNED
  * on, with an origin: `worker_env` carries both, the switch spelled "true";
  * off, or no origin: each name is OMITTED, not set empty or "false" -- a
    Cloud Run execution override MERGES into the Job's environment, so a
    value here would override whatever terraform baked into the Job;
  * a settings object without the fields at all (an older SchedulerSettings,
    a test double) still renders, with neither name;
  * `SchedulerSettings.from_env` reads both, default off and empty.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from swarm_common.models import Tenant

from scheduler.dispatch import worker_env
from scheduler.settings import SchedulerSettings

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import NOW, make_lease, make_task

CONSOLE = "https://swarm.example.test"


def _tenant() -> Tenant:
    return Tenant(
        tenant_id="eng",
        kind="group",
        principal="eng@saga.xyz",
        created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def _env(settings) -> dict[str, str]:
    task = make_task()
    return worker_env(task=task, lease=make_lease(task), tenant=_tenant(), settings=settings)


def test_worker_env_carries_the_console_link_settings_when_on():
    env = _env(scheduler_settings(console_url=CONSOLE, pr_console_links=True))
    assert env["SWARM_CONSOLE_URL"] == CONSOLE
    assert env["SWARM_PR_CONSOLE_LINKS"] == "true"


def test_worker_env_omits_the_console_link_switch_when_off():
    env = _env(scheduler_settings(console_url=CONSOLE, pr_console_links=False))
    assert env["SWARM_CONSOLE_URL"] == CONSOLE
    assert "SWARM_PR_CONSOLE_LINKS" not in env


def test_worker_env_omits_an_empty_console_url_rather_than_setting_it_empty():
    env = _env(scheduler_settings(console_url="  ", pr_console_links=True))
    assert "SWARM_CONSOLE_URL" not in env
    assert env["SWARM_PR_CONSOLE_LINKS"] == "true"


def test_worker_env_console_link_defaults_omit_both_names():
    env = _env(scheduler_settings())
    assert "SWARM_CONSOLE_URL" not in env
    assert "SWARM_PR_CONSOLE_LINKS" not in env


def test_worker_env_console_links_tolerate_settings_without_the_fields():
    base = scheduler_settings()
    bare = SimpleNamespace(project_id=base.project_id, region=base.region, core=base.core)
    env = _env(bare)
    assert "SWARM_CONSOLE_URL" not in env
    assert "SWARM_PR_CONSOLE_LINKS" not in env


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, False), ("", False), ("false", False), ("true", True), ("TRUE", True), ("1", True)],
)
def test_scheduler_settings_read_the_console_link_switch(monkeypatch, raw, expected):
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("REGION", "us-central1")
    if raw is None:
        monkeypatch.delenv("SWARM_PR_CONSOLE_LINKS", raising=False)
    else:
        monkeypatch.setenv("SWARM_PR_CONSOLE_LINKS", raw)
    monkeypatch.setenv("SWARM_CONSOLE_URL", f" {CONSOLE} ")
    settings = SchedulerSettings.from_env()
    assert settings.pr_console_links is expected
    assert settings.console_url == CONSOLE


def test_scheduler_settings_console_link_defaults_are_off_and_empty(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.delenv("SWARM_PR_CONSOLE_LINKS", raising=False)
    monkeypatch.delenv("SWARM_CONSOLE_URL", raising=False)
    settings = SchedulerSettings.from_env()
    assert settings.pr_console_links is False
    assert settings.console_url == ""
