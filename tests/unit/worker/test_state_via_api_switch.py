"""SWARM_STATE_VIA_API: the per-tenant switch, and what the entrypoint builds on it.

Lane ISO-WORKER-CORE (docs/design/tenant-settings-integrity.md §3.2
"Migration", §4.6, §6; owner decision Q7: a per-tenant switch, dev first).

  * The switch DEFAULTS OFF: an unset variable is a tenant nobody switched,
    and its worker builds `ControlPlane` over a Firestore client, as before.
  * With it on, NO `firestore.Client` is built: the control plane is
    `RemoteControlPlane`, and the `db` every other module is handed refuses
    by name, so a read that has not moved to the routes cannot reach
    Firestore through it.
  * On without an API to call or a nonce to open with is a worker that
    cannot start (78), and says which variable is missing, never its value.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

import agent_worker.__main__ as entrypoint
from agent_worker.config import WorkerConfig
from agent_worker.control import ControlPlane, RemoteControlPlane
from agent_worker.errors import ConfigError, ControlPlaneError, ExitCode
from swarm_common.config import Settings

from fakes import FakeFirestore

NONCE = "nonce-" + secrets.token_hex(8)


@pytest.fixture
def worker_env(tmp_path: Path, monkeypatch) -> None:
    for name in ("QUOTA_BROKER_URL", "SWARM_STATE_VIA_API", "SWARM_API_URL",
                 "SWARM_CHILD_NONCE", "DISPATCH_TOPIC"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "TASK_ID": "task_1", "ATTEMPT_ID": "att_1", "LEASE_ID": "lease_1",
        "TENANT_ID": "eng", "GENERATION": "1", "RUNNER_PROFILE": "mock",
        "PROJECT_ID": "swarm-test", "LOCAL_ARTIFACT_ROOT": str(tmp_path / "gcs"),
        "WORKSPACE_ROOT": str(tmp_path / "ws"), "DISABLE_CLOUD_MONITORING": "1",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def clients(monkeypatch) -> list[str]:
    """Every Firestore client the entrypoint builds, by either door."""
    from google.cloud import firestore

    built: list[str] = []

    def client(*_args, **_kwargs):
        built.append("firestore.Client")
        return FakeFirestore()

    monkeypatch.setattr(firestore, "Client", client)
    original = entrypoint._firestore_client

    def via_entrypoint(settings):
        built.append("_firestore_client")
        return original(settings)

    monkeypatch.setattr(entrypoint, "_firestore_client", via_entrypoint)
    return built


def _config() -> tuple[WorkerConfig, Settings]:
    settings = Settings.from_env()
    return WorkerConfig.from_env(settings), settings


def test_the_switch_defaults_off(worker_env):
    config, _settings = _config()
    assert config.state_via_api is False


@pytest.mark.parametrize(("raw", "on"), [("1", True), ("on", True), ("true", True),
                                         ("0", False), ("off", False), ("false", False)])
def test_the_switch_reads_as_an_operator_writes_it(worker_env, monkeypatch, raw, on):
    monkeypatch.setenv("SWARM_STATE_VIA_API", raw)
    monkeypatch.setenv("SWARM_API_URL", "https://swarm-api.test")
    monkeypatch.setenv("SWARM_CHILD_NONCE", NONCE)
    config, _settings = _config()
    assert config.state_via_api is on


@pytest.mark.parametrize(("url", "nonce", "missing"), [
    ("", NONCE, "SWARM_API_URL"), ("https://swarm-api.test", "", "SWARM_CHILD_NONCE"),
], ids=["no-api-url", "no-nonce"])
def test_switched_without_an_api_or_a_nonce_cannot_start(
    worker_env, monkeypatch, url, nonce, missing
):
    monkeypatch.setenv("SWARM_STATE_VIA_API", "1")
    monkeypatch.setenv("SWARM_API_URL", url)
    monkeypatch.setenv("SWARM_CHILD_NONCE", nonce)

    with pytest.raises(ConfigError) as caught:
        _config()

    assert missing in str(caught.value) and NONCE not in str(caught.value)
    assert caught.value.exit_code == ExitCode.CONFIG


def test_off_builds_the_direct_control_plane_over_firestore(worker_env, clients):
    config, settings = _config()

    worker = entrypoint.build_worker(config, settings)

    assert type(worker.control) is ControlPlane
    assert clients == ["_firestore_client", "firestore.Client"]


def test_on_builds_no_firestore_client_at_all(worker_env, monkeypatch, clients):
    monkeypatch.setenv("SWARM_STATE_VIA_API", "1")
    monkeypatch.setenv("SWARM_API_URL", "https://swarm-api.test")
    monkeypatch.setenv("SWARM_CHILD_NONCE", NONCE)
    config, settings = _config()

    worker = entrypoint.build_worker(config, settings)

    assert clients == []
    assert isinstance(worker.control, RemoteControlPlane)
    assert not worker.control.opened  # nothing is called until the lifecycle's gate
    assert worker.db is entrypoint.NO_DIRECT_FIRESTORE
    with pytest.raises(ControlPlaneError):
        worker.db.collection("workspace_settings")
    # The child path signs with the key state/open registers, over one transport.
    assert worker.children._api is worker.control._calls._api
    assert NONCE not in repr(worker.db) and NONCE not in repr(config)
