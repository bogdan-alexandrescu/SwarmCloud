"""The dispatcher turns residential egress on for a live-browser pod, and for
nothing else (docs/browser-egress.md §10.3; lane EGRESS-DISPATCH, part of #1030).

What the gate (lane EGRESS-GATE) reads, the dispatcher must set, and only when
the scheduler's own configuration -- BROWSER_EGRESS_ENABLED, the task's tenant
in BROWSER_EGRESS_TENANTS, its profile in BROWSER_EGRESS_PROFILES, rendered by
terraform/infra/egress.tf -- says so:

  * on the `display` container, `SWARM_BROWSER_EGRESS=proxy` and
    `SWARM_EGRESS_BYPASS`, the bypass hosts egress.tf gives swarm-egress;
  * on the pod, the label `swarm-egress: proxy`, which
    kubernetes/network-policies/allow-egress-proxy.yaml selects;
  * with any of the three gates off, the manifest is the one this dispatcher
    renders today, byte for byte (the golden is a render under the default
    settings, which carry no egress configuration at all);
  * nothing a caller writes on a task turns it on (invariant 10).

MUTATION: drop the tenant check from `browser_egress_on` and
`test_any_gate_off_is_byte_identical[tenant_off]` fails; label every pod and
`test_other_profiles_are_untouched` fails; read the switch from
`task.metadata` and `test_a_caller_field_cannot_turn_it_on` fails.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend

from scheduler.dispatch import (
    BROWSER_EGRESS_BYPASS_ENV,
    BROWSER_EGRESS_ENV,
    EGRESS_POD_LABEL,
    GkeJobDispatcher,
    GkeTarget,
    browser_egress_on,
)
from scheduler.settings import SchedulerSettings

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import NOW, make_lease, make_task

PROFILE = "claude-code-browser"
BYPASS = ("github.com", ".github.com", ".githubusercontent.com", "swarm.example.com",
          ".corp.example.com")

#: The configuration that switches tenant `eng` on for the browser profile.
ON: dict[str, Any] = dict(
    live_browser_sidecar=True,
    browser_egress_enabled=True,
    browser_egress_tenants=("eng",),
    browser_egress_profiles=(PROFILE,),
    browser_egress_bypass=BYPASS,
)


def _tenant(tenant_id: str = "eng") -> Tenant:
    return Tenant(
        tenant_id=tenant_id,
        kind="group",
        principal=f"{tenant_id}@example.com",
        created_at=NOW,
        service_account=f"swarm-agent-worker-{tenant_id}@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/{tenant_id}",
        namespace=f"swarm-tenant-{tenant_id}",
    )


def _job(settings: SchedulerSettings, *, profile_name: str = PROFILE,
         tenant_id: str = "eng", **task_fields: Any) -> dict[str, Any]:
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object())
    task = dataclasses.replace(make_task(profile_name), tenant_id=tenant_id, **task_fields)
    return dispatcher._manifest(task=task, lease=make_lease(task),
                                profile=RUNNER_PROFILES[profile_name],
                                tenant=_tenant(tenant_id))


def _today(*, profile_name: str = PROFILE, tenant_id: str = "eng",
           live: bool = True) -> str:
    """The golden: today's render, under settings with no egress configuration."""
    return _bytes(_job(scheduler_settings(live_browser_sidecar=live),
                       profile_name=profile_name, tenant_id=tenant_id))


def _bytes(job: dict[str, Any]) -> str:
    return json.dumps(job, sort_keys=True)


def _display(job: dict[str, Any]) -> dict[str, Any]:
    (display,) = [c for c in job["spec"]["template"]["spec"].get("initContainers", [])
                  if c["name"] == "display"]
    return display


def _env(container: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e["value"] for e in container["env"]}


# --- the names the other side reads --------------------------------------------


def test_the_names_are_the_ones_the_gate_and_the_policy_read():
    # livegate/egress.py EGRESS_ENV / BYPASS_ENV, and allow-egress-proxy.yaml's
    # podSelector. Spelled out here so a rename on this side alone fails.
    assert BROWSER_EGRESS_ENV == ("SWARM_BROWSER_EGRESS", "proxy")
    assert BROWSER_EGRESS_BYPASS_ENV == "SWARM_EGRESS_BYPASS"
    assert EGRESS_POD_LABEL == ("swarm-egress", "proxy")


def test_the_default_settings_carry_no_egress():
    settings = scheduler_settings()
    assert settings.browser_egress_enabled is False
    assert settings.browser_egress_tenants == ()
    assert settings.browser_egress_bypass == ()
    today = _today()
    assert "SWARM_BROWSER_EGRESS" not in today
    assert "SWARM_EGRESS_BYPASS" not in today
    assert '"swarm-egress"' not in today


# --- on ----------------------------------------------------------------------------


def test_egress_on_sets_the_env_and_the_label():
    job = _job(scheduler_settings(**ON))
    labels = job["spec"]["template"]["metadata"]["labels"]
    assert labels["swarm-egress"] == "proxy"
    env = _env(_display(job))
    assert env["SWARM_BROWSER_EGRESS"] == "proxy"
    assert env["SWARM_EGRESS_BYPASS"] == ",".join(BYPASS)
    # The worker container is not where Chromium runs: it gets neither.
    (worker,) = [c for c in job["spec"]["template"]["spec"]["containers"]
                 if c["name"] == "worker"]
    assert "SWARM_BROWSER_EGRESS" not in _env(worker)
    assert "SWARM_EGRESS_BYPASS" not in _env(worker)


def test_egress_on_adds_exactly_the_env_and_the_label():
    on = _job(scheduler_settings(**ON))
    labels = on["spec"]["template"]["metadata"]["labels"]
    del labels["swarm-egress"]
    display = _display(on)
    display["env"] = [e for e in display["env"]
                      if e["name"] not in ("SWARM_BROWSER_EGRESS", "SWARM_EGRESS_BYPASS")]
    assert _bytes(on) == _today()


def test_an_empty_bypass_list_still_names_the_variable():
    # The gate adds GitHub itself; an empty value is "nothing extra", never a
    # missing switch.
    job = _job(scheduler_settings(**{**ON, "browser_egress_bypass": ()}))
    env = _env(_display(job))
    assert env["SWARM_BROWSER_EGRESS"] == "proxy"
    assert env["SWARM_EGRESS_BYPASS"] == ""


# --- off: byte-identical to today --------------------------------------------------


@pytest.mark.parametrize("off", [
    pytest.param({"browser_egress_enabled": False}, id="platform_off"),
    pytest.param({"browser_egress_tenants": ("research",)}, id="tenant_off"),
    pytest.param({"browser_egress_tenants": ()}, id="no_tenants"),
    pytest.param({"browser_egress_profiles": ("claude-code",)}, id="profile_off"),
    pytest.param({"browser_egress_profiles": ()}, id="no_profiles"),
])
def test_any_gate_off_is_byte_identical(off):
    settings = scheduler_settings(**{**ON, **off})
    assert _bytes(_job(settings)) == _today()


def test_a_tenant_whose_name_contains_a_listed_one_is_off():
    # Membership, never a substring: "eng" listed does not switch "eng-2" on.
    settings = scheduler_settings(**ON)
    assert _bytes(_job(settings, tenant_id="eng-2")) == _today(tenant_id="eng-2")


def test_the_sidecar_switch_off_means_no_egress():
    # No display container, no Chromium to proxy: the label would open a path
    # nothing uses, so the pod is today's.
    settings = scheduler_settings(**{**ON, "live_browser_sidecar": False})
    assert _bytes(_job(settings)) == _today(live=False)


def _gke_profiles() -> list[str]:
    return sorted(n for n, p in RUNNER_PROFILES.items()
                  if resolve_backend(p) is Backend.GKE_AUTOPILOT)


def test_other_profiles_are_untouched():
    others = [n for n in _gke_profiles() if n != PROFILE]
    assert others, "expected at least one other GKE profile to hold unchanged"
    for name in others:
        # Listed, too: only a live-browser profile has a browser to proxy.
        settings = scheduler_settings(**{**ON, "browser_egress_profiles": (PROFILE, name)})
        assert _bytes(_job(settings, profile_name=name)) == _today(profile_name=name), name


# --- invariant 10 --------------------------------------------------------------------


_CALLER = dict(
    input={"browser_egress": True, "egress": "proxy", "SWARM_BROWSER_EGRESS": "proxy",
           "labels": {"swarm-egress": "proxy"}},
    metadata={"browser_egress": True, "egress": "proxy", "swarm-egress": "proxy",
              "SWARM_EGRESS_BYPASS": "evil.example"},
)


@pytest.mark.parametrize("off", [
    pytest.param({"browser_egress_enabled": False}, id="platform_off"),
    pytest.param({"browser_egress_tenants": ()}, id="tenant_off"),
    pytest.param({"browser_egress_profiles": ()}, id="profile_off"),
])
def test_a_caller_field_cannot_turn_it_on(off):
    settings = scheduler_settings(**{**ON, **off})
    shaped = _job(settings, **_CALLER)
    assert shaped["spec"]["template"]["metadata"]["labels"].get("swarm-egress") is None
    assert "SWARM_BROWSER_EGRESS" not in _env(_display(shaped))
    assert _bytes(shaped) == _bytes(_job(settings))


def test_a_caller_field_cannot_change_what_egress_on_sets():
    settings = scheduler_settings(**ON)
    plain = _job(settings)
    shaped = _job(settings, **_CALLER)
    assert _env(_display(shaped)) == _env(_display(plain))
    assert (shaped["spec"]["template"]["metadata"]["labels"]
            == plain["spec"]["template"]["metadata"]["labels"])


def test_browser_egress_on_reads_configuration_only():
    profile = RUNNER_PROFILES[PROFILE]
    on = scheduler_settings(**ON)
    assert browser_egress_on(on, tenant_id="eng", profile=profile)
    assert not browser_egress_on(on, tenant_id="research", profile=profile)
    assert not browser_egress_on(on, tenant_id="eng", profile=RUNNER_PROFILES["claude-code"])
    assert not browser_egress_on(scheduler_settings(**{**ON, "browser_egress_enabled": False}),
                                 tenant_id="eng", profile=profile)


# --- configuration --------------------------------------------------------------------


def test_from_env_reads_the_names_egress_tf_renders(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.setenv("BROWSER_EGRESS_ENABLED", "true")
    monkeypatch.setenv("BROWSER_EGRESS_TENANTS", "eng, research")
    monkeypatch.setenv("BROWSER_EGRESS_PROFILES", "claude-code-browser")
    monkeypatch.setenv("BROWSER_EGRESS_BYPASS", "github.com,.github.com, swarm.example.com")
    settings = SchedulerSettings.from_env()
    assert settings.browser_egress_enabled is True
    assert settings.browser_egress_tenants == ("eng", "research")
    assert settings.browser_egress_profiles == ("claude-code-browser",)
    assert settings.browser_egress_bypass == ("github.com", ".github.com", "swarm.example.com")


def test_from_env_defaults_off(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("REGION", "us-central1")
    for name in ("BROWSER_EGRESS_ENABLED", "BROWSER_EGRESS_TENANTS",
                 "BROWSER_EGRESS_PROFILES", "BROWSER_EGRESS_BYPASS"):
        monkeypatch.delenv(name, raising=False)
    settings = SchedulerSettings.from_env()
    assert settings.browser_egress_enabled is False
    assert settings.browser_egress_tenants == ()
    # The same default swarm-api reads (swarm_api.settings), so the two agree
    # on a deployment that sets only the tenants.
    assert settings.browser_egress_profiles == ("claude-code-browser",)
    assert settings.browser_egress_bypass == ()
