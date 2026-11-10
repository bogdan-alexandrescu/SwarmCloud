"""The worker wires a managed plugin's secrets, the TypeSafe key and the Jev filters into the loader.

docs/design/managed-plugins.md §4.4 and §6.2. A real worker runs the
`claude-code` profile with a small "agent" script in place of the runner
(test_secretrelease.py's substitution) against a fake of swarm-api's routes
that verifies the release's signature with swarm-api's own code, and loads
the step's signed managed plugins from a filesystem object store:

  * the lifecycle hands `plugins.start` all four inputs: the plugin secrets
    the release answered, the workspace TypeSafe key for a binding, the
    profile's secret names and the repository's hard-stop paths, the inputs
    `qualitygates.prepare` gets;
  * a keyless step writes no adapter, so the built-in gates keep running;
  * a released plugin secret reaches only its own plugin's launcher, and
    never the agent's environment, argv or work/, a log line or a Firestore
    document (the record included);
  * a step whose profile declares no slot still asks the release when its
    signed set names a plugin secret, and asks nothing when it names none.

Every fake value is built at runtime (CLAUDE.md: nothing added may look like a
credential).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_worker import lifecycle, pluginload
from agent_worker import secretrelease as release_mod
from agent_worker.errors import ExitCode
from agent_worker.objectstore import LocalObjectStore
from agent_worker.runners import claude_code
from swarm_common.profiles import RUNNER_PROFILES
from test_pluginload import _entry, _plugin_files, _tar
from test_secretrelease import NONCE, FakeRoutes, _ApiKeySecrets, agent  # noqa: F401  (the fixture)
from worker_seeds import seed_attempt, seed_tenant

REPO = Path(__file__).resolve().parents[3]
LAUNCHER = REPO / "images" / "agent-runtime-base" / "swarm-plugin-exec"
BELAY = "p-" + "a" * 12
ENFORCE = "p-" + "d" * 12
STOPS = ["deploy/secrets/", "infra.env"]


def fake_value(fill: str) -> str:
    return "plg-" + fill * 8 + secrets.token_hex(12)


class PluginRoutes(FakeRoutes):
    """The release route, answering the signed set's plugin secrets too
    (`attempts_secrets.PluginItem.answer`'s shape)."""

    def __init__(self, plugin_items: list[dict[str, Any]]) -> None:
        super().__init__([])
        self.plugin_items = plugin_items

    def call(self, method, path, *, body=None, headers=None):
        status, answer = super().call(method, path, body=body, headers=headers)
        if path.endswith("/secrets:release"):
            answer = {**answer, "plugin_secrets": self.plugin_items}
        return status, answer


def _plugin_item(install_id: str, variable: str, value: str) -> dict[str, Any]:
    return {"name": f"swarm-tenant-eng-plg-{install_id}-token", "install_id": install_id,
            "field": "token", "variable": variable, "target": "env", "kind": "plugin_secret",
            "consumer": f"plugin:{install_id}", "value": value}


@pytest.fixture
def store(tmp_path, monkeypatch) -> LocalObjectStore:
    found = LocalObjectStore(tmp_path / "bucket")
    monkeypatch.setattr(pluginload, "default_store", lambda environ: found)
    return found


@pytest.fixture
def kept(monkeypatch):
    """The workspace is kept, so private/ can be read after the run."""
    monkeypatch.setattr(lifecycle.workspace_mod, "destroy", lambda ws: None)


@pytest.fixture
def started(monkeypatch):
    """Every `plugins.start` call's keywords, the real start still run."""
    calls: list[dict[str, Any]] = []
    real = lifecycle.plugins_mod.start

    def spy(task, **kwargs):
        # A copy of what was released: the lifecycle empties its own after.
        calls.append({**kwargs, "released": dict(kwargs.get("released") or {})})
        return real(task, **kwargs)

    monkeypatch.setattr(lifecycle.plugins_mod, "start", spy)
    return calls


@pytest.fixture
def hard_stops(monkeypatch):
    asked: list[tuple[Any, ...]] = []

    def read(db, tenant_id, repository_url, *, call_options=None):
        asked.append((tenant_id, repository_url))
        return list(STOPS)

    monkeypatch.setattr(lifecycle.qualitygates_mod, "hard_stop_paths", read)
    return asked


def _workspace_key(monkeypatch, key: str | None) -> list[str]:
    asked: list[str] = []

    def resolve(*, db, client, tenant_id, logger, call_options=None):
        asked.append(tenant_id)
        if key is not None:
            logger.register_secret(key)
        return key

    monkeypatch.setattr(lifecycle.jevkey_mod, "resolve_workspace", resolve)
    return asked


def _seed(db, store: LocalObjectStore, plugins: dict[str, dict[str, Any]], *,
          profile: str = "claude-code") -> None:
    block = {}
    for install_id, spec in plugins.items():
        data = _tar(_plugin_files(spec["name"]))
        entry = _entry(install_id, spec["name"], data, secrets=spec.get("secrets", {}))
        store.upload_bytes(entry["object"], data)
        block[install_id] = entry
    seed_attempt(db, runner_profile=profile, task_input={"prompt": "check it"})
    seed_tenant(db, credentials=["anthropic"])
    doc = db.doc("tasks/task_1")
    doc.setdefault("metadata", {}).setdefault("dispatch", {})["plugins"] = block


def _worker(worker_factory, routes, *, profile: str = "claude-code"):
    worker, _, _ = worker_factory(
        runner_profile=profile, child_api=routes, child_nonce=NONCE,
        swarm_api_url="https://swarm-api.example", secret_client=_ApiKeySecrets(),
    )
    return worker


def _private(seen: dict[str, Any]) -> Path:
    return Path(seen["env"][pluginload.ENV_DIR_ENV]).parent


def _env_doc(private: Path, install_id: str) -> dict[str, Any]:
    return json.loads((private / pluginload.ENV_DIR / f"{install_id}.json").read_text())


# --------------------------------------------------------------------------
# all four inputs
# --------------------------------------------------------------------------


def test_the_lifecycle_passes_the_secrets_the_key_and_both_filters_to_the_loader(
        db, worker_factory, store, agent, kept, started, hard_stops, monkeypatch):
    token, key = fake_value("t"), "ts-" + secrets.token_hex(16)
    _workspace_key(monkeypatch, key)
    _seed(db, store, {
        BELAY: {"name": "jev-belay", "secrets": {"BELAY_TOKEN": {"field": "token", "secret": "plugin"}}},
        ENFORCE: {"name": "jev-enforce", "secrets": {
            "TYPESAFE_API_KEY": {"field": "key", "binding": "provider_key:typesafe"}}},
    })
    routes = PluginRoutes([_plugin_item(BELAY, "BELAY_TOKEN", token)])
    worker = _worker(worker_factory, routes)
    rc = worker.run()
    assert rc == ExitCode.OK, db.doc("tasks/task_1").get("last_error")

    [call] = started
    assert call["released"] == {(BELAY, "BELAY_TOKEN"): token}
    assert call["provider_key"] == worker._plugin_provider_key
    assert call["secret_env_names"] == tuple(RUNNER_PROFILES["claude-code"].secrets)
    assert tuple(call["hard_stop_paths"]) == tuple(STOPS) and hard_stops
    # The released secret reached the loader (and was dropped from the
    # release's heap once it had been handed over).
    assert worker.secret_release.result.plugins == {}

    [seen] = agent()
    private = _private(seen)
    belay = _env_doc(private, BELAY)
    assert Path(belay["secrets"]["BELAY_TOKEN"]).read_text() == token
    enforce = _env_doc(private, ENFORCE)
    adapter = enforce["adapter"]
    assert Path(adapter["key_file"]).read_text() == key
    assert adapter["secret_env_names"] == sorted(set(RUNNER_PROFILES["claude-code"].secrets))
    assert adapter["hard_stop_paths"] == STOPS
    load = json.loads((private / pluginload.PLUGINS_DIR / pluginload.LOAD_FILE).read_text())
    assert {p["name"]: p["adapter"] for p in load["plugins"]} == {"jev-belay": False, "jev-enforce": True}
    # The managed jev-enforce has its key: its built-in gate is replaced.
    assert claude_code.replaced_gates(load["plugins"])


def test_the_provider_key_is_the_forwarders_shim_when_it_is_on_and_never_another_provider(
        db, worker_factory, monkeypatch, tmp_path):
    asked = _workspace_key(monkeypatch, "ts-" + secrets.token_hex(16))
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db)
    worker = _worker(worker_factory, PluginRoutes([]))
    assert worker._plugin_provider_key("anthropic") is None and asked == []
    shim = tmp_path / "shim.json"
    shim.write_text(json.dumps({"shim_url": "http://127.0.0.1:1/", "token": secrets.token_hex(8)}))
    monkeypatch.setattr(worker.typesafe, "on", True)
    monkeypatch.setattr(type(worker.typesafe), "shim_file", property(lambda self: shim))
    assert worker._plugin_provider_key("typesafe") == shim.read_text() and asked == []
    monkeypatch.setattr(type(worker.typesafe), "shim_file", property(lambda self: None))
    assert worker._plugin_provider_key("typesafe") is None and asked == []


# --------------------------------------------------------------------------
# a keyless step
# --------------------------------------------------------------------------


def test_a_keyless_step_writes_no_adapter_and_keeps_the_built_in_gates(
        db, worker_factory, store, agent, kept, started, hard_stops, monkeypatch):
    asked = _workspace_key(monkeypatch, None)
    _seed(db, store, {ENFORCE: {"name": "jev-enforce", "secrets": {
        "TYPESAFE_API_KEY": {"field": "key", "binding": "provider_key:typesafe"}}}})
    worker = _worker(worker_factory, PluginRoutes([]))
    assert worker.run() == ExitCode.OK, db.doc("tasks/task_1").get("last_error")
    assert asked == ["eng"]
    [seen] = agent()
    private = _private(seen)
    assert _env_doc(private, ENFORCE)["adapter"] is None
    assert not (private / pluginload.SECRETS_DIR / ENFORCE / "typesafe.key").exists()
    load = json.loads((private / pluginload.PLUGINS_DIR / pluginload.LOAD_FILE).read_text())
    assert [p["adapter"] for p in load["plugins"]] == [False]
    assert claude_code.replaced_gates(load["plugins"]) == set()
    # Counted, never named, in the record.
    summary = db.doc("tasks/task_1").get("result_summary") or {}
    assert "TYPESAFE_API_KEY" not in json.dumps(summary, default=str)


# --------------------------------------------------------------------------
# a released plugin secret reaches its plugin alone
# --------------------------------------------------------------------------

#: The plugin's command: which variables it got and a digest of each token.
CHILD = """
import hashlib, json, os
print(json.dumps({"names": sorted(os.environ),
                  "digests": {n: hashlib.sha256(os.environ[n].encode()).hexdigest()
                              for n in ("BELAY_TOKEN", "ENFORCE_TOKEN") if n in os.environ}}))
"""


def _launch(env_dir: Path, install_id: str) -> dict[str, Any]:
    environ = {"PATH": "/usr/bin:/bin", "HOME": str(env_dir.parent), pluginload.ENV_DIR_ENV: str(env_dir)}
    done = subprocess.run([sys.executable, "-I", str(LAUNCHER), install_id, "--", sys.executable, "-c", CHILD],
                          env=environ, input="{}", capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.splitlines()[0])


def test_a_released_plugin_secret_reaches_only_its_plugins_launcher(
        db, worker_factory, store, agent, kept, hard_stops, log_stream, monkeypatch):
    _workspace_key(monkeypatch, None)
    belay_token, enforce_token = fake_value("b"), fake_value("e")
    _seed(db, store, {
        BELAY: {"name": "jev-belay", "secrets": {"BELAY_TOKEN": {"field": "token", "secret": "plugin"}}},
        ENFORCE: {"name": "jev-enforce", "secrets": {"ENFORCE_TOKEN": {"field": "token", "secret": "plugin"}}},
    })
    routes = PluginRoutes([_plugin_item(BELAY, "BELAY_TOKEN", belay_token),
                           _plugin_item(ENFORCE, "ENFORCE_TOKEN", enforce_token)])
    worker = _worker(worker_factory, routes)
    assert worker.run() == ExitCode.OK, db.doc("tasks/task_1").get("last_error")
    values = (belay_token, enforce_token)
    # Registered for redaction.
    assert set(values) <= set(worker.log._secrets)

    [seen] = agent()
    # Never in the agent's environment, argv or work/.
    for value in values:
        assert all(value not in v for v in seen["env"].values())
        assert all(value not in a for a in seen["argv"])
        assert all(value not in text for text in seen["work"])
        assert value not in json.dumps(seen.get("manifest") or {})
    assert "BELAY_TOKEN" not in seen["env"] and "ENFORCE_TOKEN" not in seen["env"]
    # Never in a log line or a Firestore document, the record included.
    for value in values:
        assert value not in log_stream.getvalue()
        assert value not in json.dumps(db.documents, default=str)

    # Each plugin's launcher hands its command its own secret, and drops the other's.
    env_dir = Path(seen["env"][pluginload.ENV_DIR_ENV])
    belay, enforce = _launch(env_dir, BELAY), _launch(env_dir, ENFORCE)
    assert belay["digests"] == {"BELAY_TOKEN": hashlib.sha256(belay_token.encode()).hexdigest()}
    assert enforce["digests"] == {"ENFORCE_TOKEN": hashlib.sha256(enforce_token.encode()).hexdigest()}
    assert "ENFORCE_TOKEN" not in belay["names"] and "BELAY_TOKEN" not in enforce["names"]


# --------------------------------------------------------------------------
# when the release is asked
# --------------------------------------------------------------------------


def test_a_profile_with_no_slot_asks_the_release_for_its_plugins_secrets(
        db, worker_factory, store, kept, started, hard_stops, monkeypatch):
    _workspace_key(monkeypatch, None)
    token = fake_value("m")
    _seed(db, store, {BELAY: {"name": "jev-belay", "secrets": {
        "BELAY_TOKEN": {"field": "token", "secret": "plugin"}}}}, profile="mock")
    routes = PluginRoutes([_plugin_item(BELAY, "BELAY_TOKEN", token)])
    worker = _worker(worker_factory, routes, profile="mock")
    assert worker.run() == ExitCode.OK, db.doc("tasks/task_1").get("last_error")
    assert len(routes.releases()) == 1
    [call] = started
    assert call["secret_env_names"] == tuple(RUNNER_PROFILES["mock"].secrets or ())
    assert token in worker.log._secrets


def test_a_set_with_no_released_secret_asks_nothing_for_a_profile_with_no_slot(
        db, worker_factory, store, kept, hard_stops, monkeypatch):
    _workspace_key(monkeypatch, None)
    _seed(db, store, {ENFORCE: {"name": "jev-enforce", "secrets": {
        "TYPESAFE_API_KEY": {"field": "key", "binding": "provider_key:typesafe"}}}}, profile="mock")
    routes = PluginRoutes([])
    worker = _worker(worker_factory, routes, profile="mock")
    assert worker.run() == ExitCode.OK, db.doc("tasks/task_1").get("last_error")
    assert routes.releases() == []


# --------------------------------------------------------------------------
# the release client's half
# --------------------------------------------------------------------------


class _Log:
    def __init__(self) -> None:
        self._secrets: list[str] = []
        self.warnings: list[dict[str, Any]] = []

    def register_secret(self, value: str | None) -> None:
        if value:
            self._secrets.append(value)

    def warning(self, message: str, **fields: Any) -> None:
        self.warnings.append({"message": message, **fields})


def test_plugin_items_are_kept_by_install_and_variable_and_every_value_registered():
    log = _Log()
    release = release_mod.SecretRelease(type("C", (), {"attempt_id": "att_1", "generation": 1})(),
                                        children=None, logger=log)
    good, stray, dup = fake_value("g"), fake_value("s"), fake_value("d")
    kept = release._accept_plugins([
        _plugin_item(BELAY, "BELAY_TOKEN", good),
        {**_plugin_item("../x", "BELAY_TOKEN", stray)},
        {**_plugin_item(BELAY, "BELAY_TOKEN", dup)},
        {**_plugin_item(ENFORCE, "lower", stray)},
        {**_plugin_item(ENFORCE, "RULES", ""), "kind": "plugin_file"},
        {**_plugin_item(ENFORCE, "OTHER", stray), "kind": "api_key"},
        "not an item",
    ])
    assert kept == {(BELAY, "BELAY_TOKEN"): good, (ENFORCE, "RULES"): ""}
    assert {good, stray, dup} <= set(log._secrets)
    assert sorted(w["reason"] for w in log.warnings) == ["duplicate", "install_id", "kind", "variable"]
    assert all(good not in json.dumps(w) and stray not in json.dumps(w) for w in log.warnings)
