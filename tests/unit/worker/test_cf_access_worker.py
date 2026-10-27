"""A Cloudflare Access service token, the worker's half (CF-ACCESS-TOKEN, part of #2442).

A `cf-access` item of the workspace-secrets release is held in the worker's
heap and handed to the live gate; it is never a file under `private/`, never
in the manifest, never in the runner's environment, argv or `work/`, and it
is registered for redaction before the runner exists. These tests drive the
real worker (`lifecycle.Worker.run`) and the real release against swarm-api's
proof check, as test_secretrelease.py does, and the real `cf_access.hand_over`
against a recording gate.

The frozen catalogue declares the slot on `claude-code-browser` alone
(contract request 117, docs/contract-change-requests.md), and these tests
read it from there: no fixture adds it. The fake token is built at run time,
never written as one literal.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest

from agent_worker import cf_access, lifecycle
from agent_worker import children as children_mod
from agent_worker import secretrelease as release_mod
from agent_worker.errors import ExitCode
from swarm_common.profiles import RUNNER_PROFILES

from test_secretrelease import (  # noqa: F401
    NONCE, FakeRoutes, _ApiKeySecrets, _item, _Log, _worker, agent, fake_value, spawned)
from worker_seeds import seed_attempt, seed_tenant

HOSTS = ["staging-admin.example.dev", "admin.example.com"]


def _token() -> tuple[str, str, str]:
    client_id, client_secret = secrets.token_hex(16) + ".access", secrets.token_hex(32)
    return client_id, client_secret, json.dumps({"client_id": client_id, "client_secret": client_secret})


def _cf_item(value: str, hosts: list[str] | None = None, name: str = "platform-admin") -> dict[str, Any]:
    item = _item(name, value, slot="cf-access", kind="oauth_client")
    item["hosts"] = list(HOSTS if hosts is None else hosts)
    return item


@pytest.fixture
def handed(monkeypatch):
    """What `hand_over` was given, copied before it empties the list."""
    seen: list[list[dict[str, Any]]] = []
    original = cf_access.hand_over

    def recording(gate, items, log, *, protected):
        seen.append([dict(i) for i in items])
        return original(gate, items, log, protected=protected)

    monkeypatch.setattr(cf_access, "hand_over", recording)
    return seen


def _browser_worker(db, worker_factory, routes):
    """test_secretrelease's `_worker`, on the one profile that declares the slot.

    This unit run starts no live gate; the caller lets the agent start without
    one (`gate_missing` False) so the release and the hand-over are reached."""
    seed_attempt(db, runner_profile="claude-code-browser", task_input={"prompt": "use the key"})
    seed_tenant(db, credentials=["anthropic"])
    worker, _, _ = worker_factory(
        runner_profile="claude-code-browser", child_api=routes, child_nonce=NONCE,
        swarm_api_url="https://swarm-api.example", secret_client=_ApiKeySecrets(),
    )
    return worker


class Gate:
    def __init__(self, refuse: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.refuse = refuse

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((op, json.loads(json.dumps(args))))
        if self.refuse:
            raise RuntimeError("refused:hosts")
        return {"cf_access": "held"}


# --------------------------------------------------------------------------
# The whole worker: the agent never holds the values
# --------------------------------------------------------------------------


def test_a_cf_access_item_never_reaches_the_agents_environment_files_or_work(
        db, worker_factory, agent, spawned, log_stream, monkeypatch, handed):  # noqa: F811
    monkeypatch.setattr(lifecycle.workspace_mod, "destroy", lambda ws: None)
    monkeypatch.setattr(lifecycle.handoff_mod.HandoffPath, "gate_missing", property(lambda self: False))
    client_id, client_secret, value = _token()
    other = fake_value("f")
    routes = FakeRoutes([_cf_item(value), _item("brave-key", other)])
    worker = _browser_worker(db, worker_factory, routes)
    recording, at_spawn = spawned
    recording.logger = worker.log
    assert worker.run() == ExitCode.OK, db.doc("tasks/task_1").get("last_error")

    [seen] = agent()
    # The ordinary item is a file, as before; the cf-access one is not.
    assert [e["name"] for e in seen["manifest"]["secrets"]] == ["brave-key"]
    assert [f["value"] for f in seen["files"]] == [other]
    private = Path(seen["manifest_path"]).parent
    assert not (private / "cf-access").exists(), "no directory for the gate slot"
    everything = json.dumps(seen)
    for needle in (value, client_id, client_secret):
        assert needle not in everything, "not in the environment, argv, manifest, files or work/"
        assert needle not in log_stream.getvalue()
        assert needle not in json.dumps(db.documents, default=str)
    # Every field registered for redaction before the runner existed.
    assert at_spawn and {value, client_id, client_secret} <= at_spawn[-1]
    # Handed over (to no gate: this unit run starts no live browser), then dropped.
    assert handed == [[{"name": "platform-admin", "slot": "cf-access", "hosts": HOSTS, "value": value}]]
    assert worker.secret_release.result is not None and worker.secret_release.result.gate == []


def test_a_workspace_with_no_cf_access_grant_hands_nothing(
        db, worker_factory, agent, handed):  # noqa: F811
    routes = FakeRoutes([_item("brave-key", fake_value())])
    worker = _worker(db, worker_factory, routes)
    assert worker.run() == ExitCode.OK
    assert handed == [[]]
    assert cf_access.hand_over(Gate(), [], _Log(), protected=True) == cf_access.NONE


# --------------------------------------------------------------------------
# The release, alone
# --------------------------------------------------------------------------


def _release(items: list[dict[str, Any]], tmp_path: Path, log: _Log | None = None,
             profile: str = "claude-code-browser") -> release_mod.Released:
    key = children_mod.AttemptKey()
    routes = FakeRoutes(items)
    routes.public_key = key.public
    children = type("P", (), {"_key": key, "_api": routes, "keyed": True})()
    cfg = type("C", (), {"attempt_id": "att_1", "generation": 1})()
    return release_mod.SecretRelease(cfg, children=children, logger=log or _Log()).release(
        profile=RUNNER_PROFILES[profile], inputs={}, private_dir=tmp_path)


def test_the_browser_profiles_attempts_take_the_cf_access_slot():
    # MUTATION: drop `cf-access` from claude-code-browser's secret_slots and
    # the worker refuses every item in it.
    slots = release_mod.consumer_slots(RUNNER_PROFILES["claude-code-browser"], {})
    assert slots["cf-access"].kinds == ("oauth_client",)
    assert set(release_mod.GATE_SLOTS) <= set(slots)


@pytest.mark.parametrize("name", sorted(
    n for n, p in RUNNER_PROFILES.items() if p.secret_slots and n != "claude-code-browser"))
def test_no_other_profile_is_released_a_cf_access_item(tmp_path, name):
    assert "cf-access" not in release_mod.consumer_slots(RUNNER_PROFILES[name], {})
    _, _, value = _token()
    released = _release([_cf_item(value)], tmp_path, profile=name)
    assert released.gate == [] and released.delivered == [] and released.env == {}


def test_the_release_holds_a_gate_item_in_the_heap_and_writes_no_file(tmp_path):
    client_id, client_secret, value = _token()
    log = _Log()
    released = _release([_cf_item(value)], tmp_path, log)
    assert released.gate == [{"name": "platform-admin", "slot": "cf-access", "hosts": HOSTS, "value": value}]
    assert released.delivered == [] and released.env == {}
    assert not (tmp_path / "secrets").exists()
    assert {value, client_id, client_secret} <= log._secrets
    assert value not in repr(released)


@pytest.mark.parametrize("hosts", [[], None, "admin.example.com", [""], [7]])
def test_a_gate_item_without_a_host_list_is_refused(tmp_path, hosts):
    _, _, value = _token()
    item = _cf_item(value)
    if hosts is None:
        item.pop("hosts")
    else:
        item["hosts"] = hosts
    released = _release([item], tmp_path)
    assert released.gate == [] and released.delivered == []


# --------------------------------------------------------------------------
# The hand-over
# --------------------------------------------------------------------------


def test_the_gate_is_handed_each_token_with_its_hosts_and_the_list_is_emptied():
    client_id, client_secret, value = _token()
    items = [{"name": "platform-admin", "slot": "cf-access", "hosts": HOSTS, "value": value}]
    gate, log = Gate(), _Log()
    assert cf_access.hand_over(gate, items, log, protected=True) == cf_access.HANDED
    assert gate.calls == [("cf_access", {"tokens": [
        {"hosts": HOSTS, "client_id": client_id, "client_secret": client_secret}]})]
    assert items == []
    assert value not in json.dumps(log.lines) and client_secret not in json.dumps(log.lines)


@pytest.mark.parametrize("case, expected", [
    ("no_gate", cf_access.NO_GATE),
    ("unprotected", cf_access.UNPROTECTED),
    ("malformed", cf_access.MALFORMED),
    ("refused", cf_access.GATE_REFUSED),
])
def test_a_token_that_cannot_be_handed_over_is_dropped_and_never_logged(case, expected):
    client_id, client_secret, value = _token()
    if case == "malformed":
        value = json.dumps({"username": client_id, "password": client_secret})
    items = [{"name": "platform-admin", "slot": "cf-access", "hosts": HOSTS, "value": value}]
    gate, log = Gate(refuse=case == "refused"), _Log()
    outcome = cf_access.hand_over(None if case == "no_gate" else gate, items, log,
                                  protected=case != "unprotected")
    assert outcome == expected and items == []
    if case != "refused":
        assert gate.calls == []
    assert client_secret not in json.dumps(log.lines) and client_id not in json.dumps(log.lines)


def test_the_gate_slots_are_swarm_apis():
    from swarm_api import workspace_secrets

    assert release_mod.GATE_SLOTS == workspace_secrets.HOST_SCOPED_SLOTS
    assert release_mod.MAX_GATE_HOSTS == workspace_secrets.MAX_SLOT_HOSTS
    assert cf_access.CTL_OP == "cf_access"
