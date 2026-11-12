"""dev's TypeSafe forwarder serves the `eng` workspace (step 3 of 3,
terraform/environments/dev/dev.tfvars `typesafe_forwarder`; the owner chose on
2026-11-02 to build every improvement proposal in CLD's Jev report, the
forwarder one among them; part of #2688).

The forwarder was served in dev from 2026-10-16 with `enabled = false` and no
tenants, so every Engineering pod on the TypeSafe backend still wrote
`private/jev-typesafe.key`. These tests read dev.tfvars itself, turn it into the
scheduler's two variables exactly as terraform/infra/typesafe_fwd.tf's
`scheduler_typesafe_fwd_env` does, read them through `SchedulerSettings
.from_env` (the scheduler's real reader), dispatch an Engineering attempt on
the TypeSafe backend, and hand the rendered worker environment to the worker's
own predicate and `jevkey.prepare`. So a red run says which link is missing:
the tfvars, the label, the switch or the shim.

Both attempts the forwarder serves are covered: a live-browser pod
(`claude-code-browser`) and a claude-code step whose signed workflow switch
says `jev_judgments: on`. A tenant not listed keeps its key file, and so does
eng on an internal backend: listing eng changes nothing for a workspace that
never chose TypeSafe.

MUTATION: set `enabled = false`, drop "eng" from `tenants`, or turn `serve`
off in dev.tfvars, and the labelled cases fail.

No credentials, no network, no emulator: the secret store and Firestore are
objects that fail the test if `prepare` touches them.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest

from agent_worker import jevkey, typesafe_shim
from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES

from scheduler.dispatch import GkeJobDispatcher, GkeTarget
from scheduler.settings import SchedulerSettings

from .conftest import PROJECT
from .test_dispatch_manifests import NOW, make_lease, make_task

REPO = Path(__file__).resolve().parents[3]
DEV_TFVARS = REPO / "terraform" / "environments" / "dev" / "dev.tfvars.example"
RUNBOOK = REPO / "docs" / "runbooks" / "typesafe-forwarder.md"

LABEL = ("swarm-typesafe", "inject")
SWITCH = (typesafe_shim.SWITCH_ENV, typesafe_shim.SWITCH_ON)


# --- dev.tfvars, read as terraform reads it ----------------------------------


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _bool(text: str, name: str) -> bool:
    found = re.findall(rf"^{name}\s*=\s*(true|false)\s*$", _strip_comments(text), re.M)
    assert len(found) == 1, f"dev.tfvars sets {name} {len(found)} times"
    return found[0] == "true"


def _forwarder_block(text: str) -> str:
    found = re.findall(r"^typesafe_forwarder\s*=\s*\{(.*?)^\}", _strip_comments(text), re.M | re.S)
    assert len(found) == 1, f"dev.tfvars sets typesafe_forwarder {len(found)} times"
    return found[0]


def _attr_bool(block: str, name: str) -> bool:
    # terraform/infra/typesafe_fwd.tf: every attribute defaults to false.
    found = re.findall(rf"^\s*{name}\s*=\s*(true|false)\s*$", block, re.M)
    assert len(found) <= 1
    return bool(found) and found[0] == "true"


def _attr_tenants(block: str) -> list[str]:
    found = re.findall(r"^\s*tenants\s*=\s*\[(.*?)\]", block, re.M | re.S)
    assert len(found) <= 1
    return re.findall(r'"([^"]+)"', found[0]) if found else []


def dev_scheduler_env() -> dict[str, str]:
    """`local.scheduler_typesafe_fwd_env` for dev: TYPESAFE_FORWARDER_ENABLED is
    "true" only when the forwarder is created (enable_typesafe_forwarder with
    enable_gke_autopilot), served and enabled; the tenants are joined by ","."""
    text = DEV_TFVARS.read_text(encoding="utf-8")
    block = _forwarder_block(text)
    serving = (_bool(text, "enable_typesafe_forwarder") and _bool(text, "enable_gke_autopilot")
               and _attr_bool(block, "serve"))
    on = serving and _attr_bool(block, "enabled")
    return {
        "TYPESAFE_FORWARDER_ENABLED": "true" if on else "false",
        "TYPESAFE_FORWARDER_TENANTS": ",".join(_attr_tenants(block)),
    }


# --- the scheduler, configured as dev configures it ---------------------------


@pytest.fixture
def dev_settings(monkeypatch) -> SchedulerSettings:
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("REGION", "us-central1")
    # dev's display rollout switch is on (enable_live_browser = true): the
    # live-browser case needs it; the jev_judgments case does not.
    monkeypatch.setenv("LIVE_BROWSER_SIDECAR", "true")
    for name, value in dev_scheduler_env().items():
        monkeypatch.setenv(name, value)
    return SchedulerSettings.from_env()


def _tenant(tenant_id: str) -> Tenant:
    return Tenant(
        tenant_id=tenant_id, kind="group", principal=f"{tenant_id}@example.com", created_at=NOW,
        service_account=f"swarm-agent-worker-{tenant_id}@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/{tenant_id}",
        namespace=f"swarm-tenant-{tenant_id}",
    )


def _pod(settings: SchedulerSettings, tenant_id: str, *, profile: str, backend: str = "typesafe",
         dispatch: dict[str, Any] | None = None) -> dict[str, Any]:
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object(), judgment=lambda _tenant_id: backend)
    task = make_task(profile)
    if dispatch is not None:
        task = dataclasses.replace(task, metadata={"dispatch": dispatch})
    job = dispatcher._manifest(task=task, lease=make_lease(task),
                               profile=RUNNER_PROFILES[profile], tenant=_tenant(tenant_id))
    return job["spec"]["template"]


def _worker_environ(template: dict[str, Any]) -> dict[str, str]:
    (worker,) = [c for c in template["spec"]["containers"] if c["name"] == "worker"]
    return {e["name"]: e.get("value", "") for e in worker["env"] if "name" in e}


# --- the worker, given that pod's environment ---------------------------------


class _Untouchable:
    """Firestore and Secret Manager: with the forwarder on, nothing reads them."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"jevkey.prepare read the key store ({name}) with the forwarder on")


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.lines.append((event, fields))

    warning = info


def _shim_on(environ: dict[str, str]) -> bool:
    """Whether the worker serves the loopback shim for this pod: the lifecycle's
    own `TypesafeInject`, on GKE, given the pod's worker environment."""
    return typesafe_shim.TypesafeInject(None, children=None, logger=_Log(), environ=environ,
                                        backend=typesafe_shim.GKE_BACKEND).on


def _worker_key_env(environ: dict[str, str], private: Path) -> dict[str, str]:
    """What the worker hands jev-browser for this pod: `jevkey.prepare` with the
    shim's switch and, when on, the shim file the shim writes."""
    inject = _shim_on(environ)
    shim_file = private / typesafe_shim.SHIM_FILE_NAME
    if inject:
        shim_file.write_text("shim", encoding="utf-8")
    return jevkey.prepare(
        endpoint="127.0.0.1:9222", db=_Untouchable(), client=_Untouchable(), tenant_id="eng",
        submitted_by="u-owner", private_dir=private, logger=_Log(), backend="typesafe",
        inject=inject, shim_file=shim_file if inject else None)


ENGINEERING = [
    pytest.param({"profile": "claude-code-browser"}, id="live-browser"),
    pytest.param({"profile": "claude-code", "dispatch": {"jev_judgments": "on"}},
                 id="jev-judgments-switched-on"),
]


def test_dev_serves_and_enables_the_forwarder_for_eng():
    env = dev_scheduler_env()
    assert env["TYPESAFE_FORWARDER_ENABLED"] == "true"
    assert env["TYPESAFE_FORWARDER_TENANTS"].split(",") == ["eng"]


@pytest.mark.parametrize("attempt", ENGINEERING)
def test_an_engineering_typesafe_attempt_is_labelled_and_switched(dev_settings, attempt):
    template = _pod(dev_settings, "eng", **attempt)
    assert template["metadata"]["labels"].get(LABEL[0]) == LABEL[1]
    assert _worker_environ(template).get(SWITCH[0]) == SWITCH[1]


@pytest.mark.parametrize("attempt", ENGINEERING)
def test_an_engineering_typesafe_attempt_runs_the_shim(dev_settings, attempt):
    """The judge (`judge.attempt.setup`) and the quality gates take the shim
    file whenever the worker serves one; this is the switch that serves it."""
    assert _shim_on(_worker_environ(_pod(dev_settings, "eng", **attempt)))


def test_an_engineering_live_browser_gets_the_shim_not_a_key_file(dev_settings, tmp_path):
    environ = _worker_environ(_pod(dev_settings, "eng", profile="claude-code-browser"))
    env = _worker_key_env(environ, tmp_path)
    assert env == {jevkey.KEY_FILE_ENV: str(tmp_path / typesafe_shim.SHIM_FILE_NAME)}
    assert not (tmp_path / jevkey.KEY_FILE_NAME).exists()


@pytest.mark.parametrize("attempt", ENGINEERING)
@pytest.mark.parametrize("tenant_id, backend", [
    pytest.param("research", "typesafe", id="a-tenant-not-listed"),
    pytest.param("eng", "internal-jev", id="eng-on-internal-jev"),
    pytest.param("eng", "internal-laya", id="eng-on-internal-laya"),
])
def test_everyone_else_is_left_as_they_were(dev_settings, attempt, tenant_id, backend):
    template = _pod(dev_settings, tenant_id, backend=backend, **attempt)
    assert LABEL[0] not in template["metadata"]["labels"]
    assert SWITCH[0] not in _worker_environ(template)


def test_the_runbook_shows_one_engineering_call_through_the_forwarder():
    """The deployed probe: the forwarder's own `typesafe_forwarded` line for
    tenant eng, read from swarm-system, and the pod's label beside it."""
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "typesafe_forwarded" in text
    assert '"tenant_id": "eng"' in text
    assert "deployment/swarm-typesafe-fwd" in text
    assert "swarm-typesafe=inject" in text
