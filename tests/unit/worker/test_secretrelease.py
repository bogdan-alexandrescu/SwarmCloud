"""The workspace secrets release, the worker's half (SEC-RELEASE, part of #1347).

Design: docs/design/workspace-secrets.md §3.3, §4.1, §4.2 and §4.5. A real
worker runs the `claude-code` profile with a small "agent" script in place of
the catalogue's runner (the substitution test_child_tasks_worker.py makes),
against a fake of swarm-api's routes that VERIFIES the release's signature
with swarm-api's own code (`swarm_api.childkey.verify_proof` over
`attempts_secrets.release_message`), so a worker that signed the wrong bytes,
or under the wrong purpose, fails here as it would against the real route.

What each case pins:

  * every released value is a 0600 file under private/secrets/, none under
    work/ (which is checkpointed), and none in the runner's environment or
    argv: the runner sees only SWARM_SECRETS_MANIFEST, a manifest of names and
    paths with no value;
  * every value is registered for redaction BEFORE the runner starts, and is
    in no captured log line and no Firestore document;
  * private/secrets/ is gone once the attempt ends;
  * the release is asked once, signed, with `{generation}` and nothing else;
  * a 409 stands the worker down; swarm-api unreachable parks a required slot
    CREDENTIAL_MISSING and starts an optional-only attempt without it;
  * an item whose slot this attempt does not declare is never written.

Every fake value is built at runtime (CLAUDE.md: nothing added may look like a
credential).
"""

from __future__ import annotations

import dataclasses
import json
import secrets
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import lifecycle
from agent_worker import secretrelease as release_mod
from agent_worker.errors import ExitCode
from swarm_api import childkey as api_childkey
from swarm_api.routes import attempts_secrets as api_release
from swarm_common.profiles import RUNNER_PROFILES, SecretSlot
from swarm_common.states import ParkReason, TaskState

from worker_seeds import TENANT, seed_attempt, seed_tenant

NONCE = "a1b2c3d4-test-nonce-not-a-secret"


def fake_value(fill: str = "w") -> str:
    """Shaped like an API key, built at runtime so no literal looks like one."""
    return "sk" + "-" + fill * 36 + secrets.token_hex(4)


AGENT = r'''
import json, os, sys, stat, pathlib
plan = json.load(open(sys.argv[1]))
manifest_path = os.environ.get("SWARM_SECRETS_MANIFEST")
seen = {"env": dict(os.environ), "argv": sys.argv, "manifest_path": manifest_path,
        "files": [], "work": []}
if manifest_path:
    seen["manifest"] = json.loads(open(manifest_path).read())
    seen["manifest_mode"] = stat.S_IMODE(os.stat(manifest_path).st_mode)
    for entry in seen["manifest"]["secrets"]:
        p = entry["path"]
        seen["files"].append({"path": p, "mode": stat.S_IMODE(os.stat(p).st_mode),
                              "value": open(p).read()})
work = pathlib.Path(os.environ["SWARM_WORK_DIR"])
seen["work"] = [p.read_text(errors="replace") for p in work.rglob("*") if p.is_file()]
with open(plan["record"], "a") as handle:
    handle.write(json.dumps(seen) + "\n")
pathlib.Path(os.environ["SWARM_RESULT"]).write_text(json.dumps(
    {"status": "succeeded", "summary": "", "output": {}, "error": None, "metrics": {},
     "artifacts": []}))
'''


class FakeRoutes:
    """swarm-api's child-key and release routes, verifying with swarm-api's code."""

    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = items or []
        self.calls: list[dict[str, Any]] = []
        self.public_key: str | None = None
        self.fail: children_mod.ChildApiError | None = None
        self.withheld: list[dict[str, Any]] = []

    def call(self, method, path, *, body=None, headers=None):
        headers = dict(headers or {})
        self.calls.append({"method": method, "path": path, "body": body, "headers": headers})
        if path.endswith("/child-key"):
            self.public_key = json.loads(body).get("public_key")
            return 201, {"registered": "key"}
        if path.endswith("/secrets:release"):
            proof = headers.get(children_mod.PROOF_HEADER)
            stamp = headers.get(children_mod.TIMESTAMP_HEADER) or ""
            message = api_release.release_message(method, path, body, stamp)
            if not (self.public_key and proof
                    and api_childkey.verify_proof(self.public_key, proof, message)):
                raise children_mod.ChildApiError(403, "secrets_release_unproven", "",
                                                 retryable=False)
            if self.fail is not None:
                raise self.fail
            return 200, {"secrets": self.items, "withheld": self.withheld}
        return 200, {}

    def releases(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["path"].endswith("/secrets:release")]


class _ApiKeySecrets:
    """The tenant's provider key, as a JSON secret, built at runtime."""

    def access(self, secret_name: str, version: str = "latest") -> str:
        return json.dumps({"ANTHROPIC_API_KEY": "fake-" + "q" * 32 + secret_name[-4:]})


@pytest.fixture
def agent(tmp_path, monkeypatch):
    script = tmp_path / "agent.py"
    script.write_text(AGENT)
    plan = tmp_path / "plan.json"
    record = tmp_path / "record.jsonl"
    plan.write_text(json.dumps({"record": str(record)}))
    monkeypatch.setattr(lifecycle, "_runner_argv", lambda cfg: [sys.executable, str(script), str(plan)])

    def records() -> list[dict[str, Any]]:
        return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []

    return records


@pytest.fixture
def spawned(monkeypatch):
    """What the worker's logger had registered at the moment each child was spawned."""
    seen: list[set[str]] = []
    original = lifecycle.ChildProcess

    class Recording(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen.append(set(Recording.logger._secrets) if Recording.logger else set())
            super().__init__(*args, **kwargs)

    Recording.logger = None
    monkeypatch.setattr(lifecycle, "ChildProcess", Recording)
    return Recording, seen


def _worker(db, worker_factory, routes, **kwargs):
    seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "use the key"})
    seed_tenant(db, credentials=["anthropic"])
    worker, cfg, _ = worker_factory(
        runner_profile="claude-code", child_api=routes, child_nonce=NONCE,
        swarm_api_url="https://swarm-api.example", secret_client=_ApiKeySecrets(), **kwargs,
    )
    return worker


def _item(name: str, value: str, *, slot: str = "files", kind: str = "api_key") -> dict[str, Any]:
    return {"name": name, "slot": slot, "kind": kind,
            "version": f"projects/p/secrets/swarm-tenant-{TENANT}-wsec-{'a' * 16}/versions/1",
            "value": value}


# --------------------------------------------------------------------------
# restatements held to swarm-api's
# --------------------------------------------------------------------------


def test_the_workers_purpose_is_swarm_apis():
    assert release_mod.RELEASE_PURPOSE == api_release.RELEASE_PURPOSE
    route = release_mod.SecretRelease(
        type("C", (), {"attempt_id": "att_1", "generation": 1})(), children=None, logger=None
    ).route()
    assert route == api_release.RELEASE_ROUTE.replace("{attempt_id}", "att_1")


# --------------------------------------------------------------------------
# §4.1: files under private/, the manifest's path only
# --------------------------------------------------------------------------


def test_every_value_is_a_0600_file_under_private_and_nowhere_in_the_runner(
        db, worker_factory, agent, spawned, log_stream, monkeypatch):
    # The workspace is KEPT, so the removal below is the release's own and not
    # the workspace teardown's.
    monkeypatch.setattr(lifecycle.workspace_mod, "destroy", lambda ws: None)
    value, other = fake_value("a"), fake_value("b")
    routes = FakeRoutes([_item("brave-key", value), _item("openai-key", other)])
    worker = _worker(db, worker_factory, routes)
    recording, at_spawn = spawned
    recording.logger = worker.log
    rc = worker.run()
    assert rc == ExitCode.OK, db.doc("tasks/task_1").get("last_error")

    [seen] = agent()
    private = Path(seen["manifest_path"]).parent.parent
    assert private.name == "private" and private.is_dir()
    # The runner sees exactly ONE variable for them: the manifest's path.
    assert seen["manifest_path"] and seen["manifest_path"].endswith("/private/secrets/manifest.json")
    assert seen["manifest_mode"] == 0o600
    manifest = seen["manifest"]["secrets"]
    assert sorted(e["name"] for e in manifest) == ["brave-key", "openai-key"]
    assert all(set(e) == {"name", "slot", "kind", "path"} for e in manifest)
    assert value not in json.dumps(seen["manifest"]) and other not in json.dumps(seen["manifest"])
    for entry in seen["files"]:
        assert "/private/secrets/files/" in entry["path"] and "/work/" not in entry["path"]
        assert entry["mode"] == 0o600
    assert {f["value"] for f in seen["files"]} == {value, other}
    # Never in the runner's environment or argv, never in work/.
    assert all(value not in v and other not in v for v in seen["env"].values())
    assert all(value not in a and other not in a for a in seen["argv"])
    assert all(value not in text and other not in text for text in seen["work"])

    # Registered for redaction BEFORE the runner was spawned.
    assert at_spawn and {value, other} <= at_spawn[-1]
    # In no captured log line and no Firestore document.
    assert value not in log_stream.getvalue() and other not in log_stream.getvalue()
    assert value not in json.dumps(db.documents, default=str)
    # Gone once the attempt ended, whatever happens to the workspace.
    assert not (private / "secrets").exists()


def test_the_release_is_asked_once_signed_with_the_generation_and_nothing_else(
        db, worker_factory, agent):
    routes = FakeRoutes([_item("brave-key", fake_value())])
    worker = _worker(db, worker_factory, routes)
    assert worker.run() == ExitCode.OK
    [call] = routes.releases()
    assert json.loads(call["body"]) == {"generation": 1}
    assert call["path"] == "/v1/attempts/att_1/secrets:release"
    # The key was registered first, while STARTING, by the same worker.
    paths = [c["path"] for c in routes.calls]
    assert paths.index("/v1/attempts/att_1/child-key") < paths.index(call["path"])


def test_a_profile_with_no_slot_makes_no_call(db, worker_factory):
    routes = FakeRoutes([_item("brave-key", fake_value())])
    seed_attempt(db, runner_profile="mock")
    seed_tenant(db)
    worker, _, _ = worker_factory(child_api=routes, child_nonce=NONCE,
                                  swarm_api_url="https://swarm-api.example")
    assert worker.run() == ExitCode.OK
    assert routes.releases() == []


# --------------------------------------------------------------------------
# §3.4: defence in depth
# --------------------------------------------------------------------------


def test_an_item_in_a_slot_this_attempt_does_not_declare_is_never_written(
        db, worker_factory, agent, log_stream):
    stray, kept = fake_value("s"), fake_value("k")
    routes = FakeRoutes([
        _item("typesafe-key", stray, slot="typesafe"),     # claude-code launches no jev
        _item("../escape", stray),
        _item("brave-key", kept),
        _item("brave-key", stray),                          # twice
        _item("forge-ish", stray, kind="forge_token"),
    ])
    worker = _worker(db, worker_factory, routes)
    assert worker.run() == ExitCode.OK
    [seen] = agent()
    assert [f["value"] for f in seen["files"]] == [kept]
    # Refused values were in this heap, so they are redacted too.
    assert stray not in log_stream.getvalue()


# --------------------------------------------------------------------------
# §4.2 and invariant 5: when it cannot ask
# --------------------------------------------------------------------------


def test_a_409_stands_the_worker_down_and_the_runner_never_starts(db, worker_factory, agent):
    routes = FakeRoutes([_item("brave-key", fake_value())])
    routes.fail = children_mod.ChildApiError(409, "secrets_release_fenced", "", retryable=False)
    worker = _worker(db, worker_factory, routes)
    rc = worker.run()
    assert rc == ExitCode.GENERATION_FENCED
    assert agent() == []


def test_unreachable_with_optional_slots_only_starts_without_them(db, worker_factory, agent):
    routes = FakeRoutes([_item("brave-key", fake_value())])
    routes.fail = children_mod.ChildApiError(503, "secrets_release_unavailable", "",
                                             retryable=True)
    worker = _worker(db, worker_factory, routes)
    assert worker.run() == ExitCode.OK
    [seen] = agent()
    assert seen["manifest_path"] is None and "SWARM_SECRETS_MANIFEST" not in seen["env"]


def _required(monkeypatch) -> None:
    """A required slot: the catalogue declares none today (request 80 makes every
    slot optional), so the profile's slots are replaced for this test only."""
    slot = SecretSlot(kinds=("api_key",), means="a key the agent cannot run without",
                      required=True)
    original = release_mod.consumer_slots

    def with_required(profile, inputs):
        out = dict(original(profile, inputs))
        if profile.name == "claude-code":
            out["files"] = slot
        return out

    monkeypatch.setattr(release_mod, "consumer_slots", with_required)


@pytest.mark.parametrize("how", ["unreachable", "no_grant"])
def test_a_required_slot_with_nothing_in_it_parks_credential_missing(
        db, worker_factory, agent, monkeypatch, how):
    _required(monkeypatch)
    routes = FakeRoutes([])
    if how == "unreachable":
        routes.fail = children_mod.ChildApiError(None, "api_unavailable", "", retryable=True)
    worker = _worker(db, worker_factory, routes)
    assert worker.run() == ExitCode.PARKED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.CREDENTIAL_MISSING.value
    assert agent() == []


def test_a_required_slot_that_was_released_starts(db, worker_factory, agent, monkeypatch):
    _required(monkeypatch)
    routes = FakeRoutes([_item("brave-key", fake_value())])
    worker = _worker(db, worker_factory, routes)
    assert worker.run() == ExitCode.OK
    assert len(agent()) == 1


def test_no_attempt_key_is_unreachable_and_asks_nothing(db, worker_factory, agent):
    routes = FakeRoutes([_item("brave-key", fake_value())])
    seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "x"})
    seed_tenant(db, credentials=["anthropic"])
    worker, _, _ = worker_factory(runner_profile="claude-code", child_api=routes,
                                  secret_client=_ApiKeySecrets())
    assert worker.run() == ExitCode.OK
    assert routes.releases() == []
    assert worker.secret_release.result.unreachable == release_mod.NO_KEY


# --------------------------------------------------------------------------
# the module on its own
# --------------------------------------------------------------------------


class _Log:
    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self.lines: list[tuple[str, dict[str, Any]]] = []

    def register_secret(self, value: str | None) -> None:
        if value and len(value) >= 8:
            self._secrets.add(value)

    def info(self, message: str, **fields: Any) -> None:
        self.lines.append((message, fields))

    warning = info


def test_a_structured_value_registers_each_of_its_fields(tmp_path):
    password = "pw-" + secrets.token_hex(8)
    value = json.dumps({"username": "robot-" + secrets.token_hex(3), "password": password})
    key = children_mod.AttemptKey()
    routes = FakeRoutes([_item("site-login", value, kind="basic_auth")])
    routes.public_key = key.public
    children = type("P", (), {"_key": key, "_api": routes, "keyed": True})()
    log = _Log()
    cfg = type("C", (), {"attempt_id": "att_1", "generation": 3})()
    released = release_mod.SecretRelease(cfg, children=children, logger=log).release(
        profile=RUNNER_PROFILES["claude-code"], inputs={}, private_dir=tmp_path)
    assert released.delivered == [("site-login", "files")]
    assert {value, password} <= log._secrets
    assert json.loads(routes.releases()[0]["body"]) == {"generation": 3}
    path = tmp_path / "secrets" / "files" / "site-login"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "secrets").stat().st_mode) == 0o700
    release_mod.remove(tmp_path)
    assert not (tmp_path / "secrets").exists()


def test_a_planted_symlink_is_replaced_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    root = tmp_path / "private"
    root.mkdir()
    (root / "secrets").mkdir()
    (root / "secrets" / "files").mkdir()
    (root / "secrets" / "files" / "brave-key").symlink_to(outside)
    key = children_mod.AttemptKey()
    routes = FakeRoutes([_item("brave-key", fake_value())])
    routes.public_key = key.public
    children = type("P", (), {"_key": key, "_api": routes, "keyed": True})()
    cfg = type("C", (), {"attempt_id": "att_1", "generation": 1})()
    release_mod.SecretRelease(cfg, children=children, logger=_Log()).release(
        profile=RUNNER_PROFILES["claude-code"], inputs={}, private_dir=root)
    assert outside.read_text() == "untouched"
    assert not (root / "secrets" / "files" / "brave-key").is_symlink()


def test_the_browser_tool_decides_the_tool_slots():
    profile = RUNNER_PROFILES["claude-code-browser"]
    assert "typesafe" in release_mod.consumer_slots(profile, {"browser_tool": "jev"})
    assert "typesafe" not in release_mod.consumer_slots(profile, {"browser_tool": "playwright"})
    assert release_mod.wants_release(RUNNER_PROFILES["mock"], {}) is False
    merge = RUNNER_PROFILES["merge"]
    assert release_mod.wants_release(merge, {}) is False
    assert dataclasses.is_dataclass(profile)
