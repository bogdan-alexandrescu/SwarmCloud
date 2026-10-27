"""The MCP attach, the worker's half, and the relay in the lifecycle
(MCP-WORKER, part of #1433).

Design: docs/design/registered-mcp-servers.md §4.6 and §4.7. A real worker
runs the `claude-code` profile with a small "agent" script in place of the
catalogue's runner (the substitution test_secretrelease.py makes), against a
fake of swarm-api's routes that VERIFIES the attach's signature with
swarm-api's own code (`swarm_api.childkey.verify_proof` over
`attempts_mcp.attach_message`), so a worker that signed the wrong bytes, or
under the wrong purpose, fails here as it would against the real route. The
agent calls a tool through the relay; the fake MCP server is
`test_mcp_relay.FakeMcp`.

What each case pins:

  * the server sees the credential, the agent never does: not in its
    environment, argv, the relay file or any file under the workspace, during
    or after the attempt, nor in a log line or a Firestore document;
  * the attach is asked once, signed, `{generation}` and nothing else, and
    only when the input names a server (dark otherwise);
  * a 409 attach exits fenced without the agent;
  * a withheld server fails the attempt before the agent: finally for
    `credential_expired`, retryably for `unreadable`;
  * the lifecycle's fence flag reaches the relay;
  * each credential is registered for redaction before it is validated, and
    a server the worker's own rules refuse is withheld.

Every fake value is built at runtime (CLAUDE.md: nothing added may look like a
credential).
"""

from __future__ import annotations

import functools
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import lifecycle
from agent_worker import mcp_attach as attach_mod
from agent_worker import mcp_relay as relay_mod
from agent_worker.errors import ExitCode, FencedError
from swarm_api import childkey as api_childkey
from swarm_api.routes import attempts_mcp as api_attach
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import TaskState

from test_mcp_relay import PUBLIC, FakeMcp, fake_credential
from worker_seeds import seed_attempt, seed_tenant

NONCE = "a1b2c3d4-test-nonce-not-a-secret"


AGENT = r'''
import json, os, sys, pathlib, urllib.request, urllib.error
plan = json.load(open(sys.argv[1]))
relay_path = os.environ.get("SWARM_MCP_RELAY")
seen = {"env": dict(os.environ), "argv": sys.argv, "relay_path": relay_path, "answers": []}
if relay_path:
    relay = json.loads(open(relay_path).read())
    seen["relay"] = relay
    for server in relay["servers"]:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "search", "arguments": {"q": "why"}}}).encode()
        request = urllib.request.Request(server["url"], data=body, method="POST")
        request.add_header("Authorization", "Bearer " + relay["token"])
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                seen["answers"].append([response.status, response.read().decode()])
        except urllib.error.HTTPError as exc:
            seen["answers"].append([exc.code, exc.read().decode()])
work = pathlib.Path(os.environ["SWARM_WORK_DIR"])
seen["work"] = [p.read_text(errors="replace") for p in work.rglob("*") if p.is_file()]
with open(plan["record"], "a") as handle:
    handle.write(json.dumps(seen) + "\n")
pathlib.Path(os.environ["SWARM_RESULT"]).write_text(json.dumps(
    {"status": "succeeded", "summary": "", "output": {}, "error": None, "metrics": {},
     "artifacts": []}))
'''


def _server(name: str = "kb", credential: str | None = None, **extra: Any) -> dict[str, Any]:
    item = {"name": name, "url": f"https://{name}.example.com/mcp", "revision": 3,
            "description": "the decisions graph",
            "auth": {"header": "Authorization", "scheme": "Bearer", "credential": credential},
            "allowed_tools": ["search"],
            "limits": {"max_request_bytes": 1048576, "max_response_bytes": 4194304,
                       "calls_per_minute": 60, "call_timeout_seconds": 60}}
    item.update(extra)
    return item


class FakeRoutes:
    """swarm-api's child-key, release and attach routes, verifying with its code."""

    def __init__(self, servers: list[dict[str, Any]] | None = None,
                 withheld: list[dict[str, str]] | None = None) -> None:
        self.servers = servers or []
        self.withheld = withheld or []
        self.calls: list[dict[str, Any]] = []
        self.public_key: str | None = None
        self.fail: children_mod.ChildApiError | None = None

    def call(self, method, path, *, body=None, headers=None):
        headers = dict(headers or {})
        self.calls.append({"method": method, "path": path, "body": body, "headers": headers})
        if path.endswith("/child-key"):
            self.public_key = json.loads(body).get("public_key")
            return 201, {"registered": "key"}
        if path.endswith("/secrets:release"):
            return 200, {"secrets": [], "withheld": []}
        if path.endswith("/mcp:attach"):
            proof = headers.get(children_mod.PROOF_HEADER)
            stamp = headers.get(children_mod.TIMESTAMP_HEADER) or ""
            message = api_attach.attach_message(method, path, body, stamp)
            if not (self.public_key and proof
                    and api_childkey.verify_proof(self.public_key, proof, message)):
                raise children_mod.ChildApiError(403, "mcp_attach_unproven", "",
                                                 retryable=False)
            if self.fail is not None:
                raise self.fail
            return 200, {"servers": self.servers, "withheld": self.withheld}
        return 200, {}

    def attaches(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["path"].endswith("/mcp:attach")]


class _ApiKeySecrets:
    def access(self, secret_name: str, version: str = "latest") -> str:
        return json.dumps({"ANTHROPIC_API_KEY": "fake-" + "q" * 32 + secret_name[-4:]})


@pytest.fixture
def agent(tmp_path, monkeypatch):
    script = tmp_path / "agent.py"
    script.write_text(AGENT)
    plan = tmp_path / "plan.json"
    record = tmp_path / "record.jsonl"
    plan.write_text(json.dumps({"record": str(record)}))
    monkeypatch.setattr(lifecycle, "_runner_argv",
                        lambda cfg: [sys.executable, str(script), str(plan)])

    def records() -> list[dict[str, Any]]:
        return [json.loads(line) for line in record.read_text().splitlines()] \
            if record.exists() else []

    return records


@pytest.fixture
def upstream():
    fake = FakeMcp()
    yield fake
    fake.stop()


def _worker(db, worker_factory, routes, upstream=None, *,
            task_input: dict[str, Any] | None = None):
    seed_attempt(db, runner_profile="claude-code",
                 task_input=task_input if task_input is not None
                 else {"prompt": "ask the graph", "mcp_servers": ["kb"]})
    seed_tenant(db, credentials=["anthropic"])
    worker, _, _ = worker_factory(
        runner_profile="claude-code", child_api=routes, child_nonce=NONCE,
        swarm_api_url="https://swarm-api.example", secret_client=_ApiKeySecrets(),
    )
    if upstream is not None:
        worker.mcp._relay_factory = functools.partial(
            relay_mod.Relay, resolver=lambda host: [PUBLIC], connector=upstream.connector)
    return worker


def _files_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and not p.is_symlink()]


# --------------------------------------------------------------------------
# restatements held to swarm-api's
# --------------------------------------------------------------------------


def test_the_workers_purpose_and_route_are_swarm_apis():
    assert attach_mod.ATTACH_PURPOSE == api_attach.ATTACH_PURPOSE
    cfg = type("C", (), {"attempt_id": "att_1", "generation": 1})()
    route = attach_mod.McpAttachment(cfg, children=None, logger=None).route()
    assert route == api_attach.ATTACH_ROUTE.replace("{attempt_id}", "att_1")
    assert attach_mod.MAX_SERVERS == api_attach.MAX_SERVERS


def test_the_requested_names_are_read_as_swarm_api_reads_them():
    task = {"input": {"mcp_servers": ["kb", "kb", "Bad Name", 7, "crm", "a", "b", "c"]}}
    assert attach_mod.requested_servers(task["input"]) == api_attach.signed_server_names(task)
    assert attach_mod.requested_servers(None) == []
    assert attach_mod.requested_servers({"mcp_servers": "kb"}) == []


# --------------------------------------------------------------------------
# §4.7: the server sees the credential, the agent never does
# --------------------------------------------------------------------------


def test_the_agent_reaches_the_server_through_the_relay_and_never_holds_the_credential(
        db, worker_factory, agent, upstream, log_stream, monkeypatch):
    # The workspace is KEPT, so what is checked below is what the attempt left.
    kept: list[Any] = []
    monkeypatch.setattr(lifecycle.workspace_mod, "destroy", lambda ws: kept.append(ws))
    credential = fake_credential()
    routes = FakeRoutes([_server(credential=credential)])
    worker = _worker(db, worker_factory, routes, upstream)
    rc = worker.run()
    assert rc == ExitCode.OK, db.doc("tasks/task_1").get("last_error")

    [seen] = agent()
    # The server saw the credential, as the registered header.
    [request] = upstream.requests
    assert request["headers"].get("Authorization") == f"Bearer {credential}"
    assert json.loads(request["body"])["params"]["name"] == "search"
    [[status, answer]] = seen["answers"]
    assert status == 200 and credential not in answer

    # The runner was handed the relay file's path, under private/, 0600.
    relay_path = Path(seen["relay_path"])
    assert relay_path.name == attach_mod.RELAY_FILE and relay_path.parent.name == "private"
    relay = seen["relay"]
    assert [s["name"] for s in relay["servers"]] == ["kb"]
    assert relay["servers"][0]["url"].startswith("http://127.0.0.1:")
    assert relay["servers"][0]["description"] == "the decisions graph"
    # Never the credential: not in its environment, argv, the relay file or work/.
    assert credential not in json.dumps(seen["env"])
    assert all(credential not in a for a in seen["argv"])
    assert credential not in json.dumps(relay)
    assert all(credential not in text for text in seen["work"])

    # After the attempt: the relay file is gone and no file under the
    # workspace holds the value.
    [ws] = kept
    assert not relay_path.exists()
    for path in _files_under(Path(ws.root)):
        assert credential.encode() not in path.read_bytes(), path
    # In no log line and no Firestore document.
    assert credential not in log_stream.getvalue()
    assert credential not in json.dumps(db.documents, default=str)
    # And the worker holds no relay any more.
    assert worker.mcp.relay is None and worker.mcp.result.servers == []


def test_the_attach_is_asked_once_signed_with_the_generation_and_nothing_else(
        db, worker_factory, agent, upstream):
    routes = FakeRoutes([_server(credential=fake_credential())])
    worker = _worker(db, worker_factory, routes, upstream)
    assert worker.run() == ExitCode.OK
    [call] = routes.attaches()
    assert json.loads(call["body"]) == {"generation": 1}
    assert call["path"] == "/v1/attempts/att_1/mcp:attach"
    paths = [c["path"] for c in routes.calls]
    assert paths.index("/v1/attempts/att_1/child-key") < paths.index(call["path"])


def test_a_task_that_names_no_server_makes_no_call_and_gets_no_relay(
        db, worker_factory, agent):
    routes = FakeRoutes([_server(credential=fake_credential())])
    worker = _worker(db, worker_factory, routes, task_input={"prompt": "no servers"})
    assert worker.run() == ExitCode.OK
    assert routes.attaches() == []
    [seen] = agent()
    assert seen["relay_path"] is None and attach_mod.RELAY_ENV not in seen["env"]


# --------------------------------------------------------------------------
# §4.7 steps 1-2: when it cannot attach
# --------------------------------------------------------------------------


def test_a_409_attach_exits_fenced_without_the_agent(db, worker_factory, agent, upstream):
    routes = FakeRoutes([_server(credential=fake_credential())])
    routes.fail = children_mod.ChildApiError(409, "mcp_attach_fenced", "", retryable=False)
    worker = _worker(db, worker_factory, routes, upstream)
    assert worker.run() == ExitCode.GENERATION_FENCED
    assert agent() == []
    assert upstream.requests == []


def test_a_withheld_server_fails_the_attempt_before_the_agent(
        db, worker_factory, agent, upstream):
    """A task that is no workflow's: nothing can re-store its credential."""
    routes = FakeRoutes([], withheld=[{"name": "kb", "reason": "credential_expired"}])
    worker = _worker(db, worker_factory, routes, upstream)
    assert worker.run() == ExitCode.FAILED
    assert agent() == []
    task = db.doc("tasks/task_1")
    assert task.get("state") == TaskState.FAILED.value
    assert "mcp_server_withheld" in task.get("last_error")
    assert "kb:credential_expired" in task.get("last_error")


@pytest.mark.parametrize("reason, code", [
    ("credential_expired", "mcp_credential_expired"),
    ("credential_missing", "mcp_credential_missing"),
])
def test_a_workflow_steps_lapsed_credential_parks_for_its_caller_before_the_agent(
        db, worker_factory, agent, upstream, reason, code):
    """BGD 2026-10-26 (registered-mcp-servers.md §4.10): a workflow step whose
    per-run credential lapsed between admission and the attach waits for its
    caller to re-store it, PARKED CREDENTIAL_MISSING, its lease released,
    with the detail a caller watches for. The agent never starts."""
    routes = FakeRoutes([], withheld=[{"name": "kb", "reason": reason}])
    worker = _worker(db, worker_factory, routes, upstream)
    db.doc("tasks/task_1")["workflow_id"] = "wf_oracle"
    assert worker.run() == ExitCode.PARKED
    assert agent() == []
    task = db.doc("tasks/task_1")
    assert (task.get("state"), task.get("park_reason")) == (
        TaskState.PARKED.value, "CREDENTIAL_MISSING")
    assert task.get("current_lease_id") is None
    [block] = task.get("blocked_by")
    assert {k: block[k] for k in ("reason", "credential", "code", "server", "expired_at")} == {
        "reason": "CREDENTIAL_MISSING", "credential": "mcp", "code": code, "server": "kb",
        "expired_at": None}
    assert block["mcp_servers"] == [{"name": "kb", "code": code, "why": reason}]


def test_a_lapsed_credential_beside_another_withheld_server_still_fails():
    """Only a withholding a re-store can fix parks: one server withheld for
    another reason means the attempt cannot start whatever is re-stored."""
    mixed = attach_mod.Attached(withheld=[("kb", "credential_expired"), ("crm", "disabled")])
    assert mixed.lapsed_credentials() == []
    lapsed = attach_mod.Attached(withheld=[("kb", "credential_expired")])
    assert lapsed.lapsed_credentials() == [("kb", "credential_expired")]
    assert attach_mod.Attached(withheld=[("kb", "credential_expired")],
                               unavailable="down").lapsed_credentials() == []


def test_the_credential_reasons_are_swarm_apis():
    from swarm_api import mcp_credentials as api_credentials

    assert attach_mod.CREDENTIAL_EXPIRED == api_credentials.CREDENTIAL_EXPIRED
    assert attach_mod.CREDENTIAL_MISSING == api_credentials.CREDENTIAL_MISSING


def test_an_unreadable_server_fails_the_attempt_retryably(db, worker_factory, agent, upstream):
    routes = FakeRoutes([], withheld=[{"name": "kb", "reason": "unreadable"}])
    worker = _worker(db, worker_factory, routes, upstream)
    assert worker.run() == ExitCode.FAILED
    assert agent() == []
    assert db.doc("tasks/task_1").get("state") == TaskState.READY.value


def test_a_server_the_answer_left_out_is_withheld(db, worker_factory, agent, upstream):
    routes = FakeRoutes([])
    worker = _worker(db, worker_factory, routes, upstream)
    assert worker.run() == ExitCode.FAILED
    assert agent() == []
    assert "kb:not_answered" in db.doc("tasks/task_1").get("last_error")


def test_swarm_api_unavailable_fails_before_the_agent(db, worker_factory, agent, upstream):
    routes = FakeRoutes([_server(credential=fake_credential())])
    routes.fail = children_mod.ChildApiError(503, "mcp_unavailable", "", retryable=True)
    worker = _worker(db, worker_factory, routes, upstream)
    assert worker.run() == ExitCode.FAILED
    assert agent() == []
    task = db.doc("tasks/task_1")
    assert task.get("state") == TaskState.READY.value
    assert "mcp_attach_unavailable" in task.get("last_error")


def test_no_attempt_key_fails_before_the_agent_and_asks_nothing(db, worker_factory, agent):
    routes = FakeRoutes([_server(credential=fake_credential())])
    seed_attempt(db, runner_profile="claude-code",
                 task_input={"prompt": "p", "mcp_servers": ["kb"]})
    seed_tenant(db, credentials=["anthropic"])
    worker, _, _ = worker_factory(runner_profile="claude-code", child_api=routes,
                                  swarm_api_url="https://swarm-api.example",
                                  secret_client=_ApiKeySecrets())
    assert worker.run() == ExitCode.FAILED
    assert agent() == [] and routes.attaches() == []
    assert db.doc("tasks/task_1").get("state") == TaskState.FAILED.value


# --------------------------------------------------------------------------
# the fence flag
# --------------------------------------------------------------------------


def test_the_lifecycles_fence_reaches_the_relay(db, worker_factory, upstream, tmp_path):
    routes = FakeRoutes([_server(credential=fake_credential())])
    worker = _worker(db, worker_factory, routes, upstream)
    worker.mcp = _attachment(_server(credential=fake_credential()), upstream=upstream)
    worker.mcp.attach(profile=RUNNER_PROFILES["claude-code"], inputs={"mcp_servers": ["kb"]})
    worker.mcp.start(tmp_path)
    relay = worker.mcp.relay
    assert relay is not None and not relay.fenced
    worker._exit_fenced(FencedError(1, 2, "superseded"))
    assert relay.fenced
    worker.mcp.close()
    assert not (tmp_path / attach_mod.RELAY_FILE).exists()


# --------------------------------------------------------------------------
# defence in depth
# --------------------------------------------------------------------------


class _Log:
    def __init__(self) -> None:
        self.secrets: list[str] = []

    def register_secret(self, value: str) -> None:
        self.secrets.append(value)

    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    warning = info


def _attachment(*servers: dict[str, Any], upstream: Any = None,
                log: Any = None) -> attach_mod.McpAttachment:
    """An attachment whose route answers `servers`, signing nothing."""
    routes = type("R", (), {})()
    routes.call = lambda *a, **k: (200, {"servers": list(servers), "withheld": []})
    children = type("K", (), {"keyed": True, "_key": object(), "_api": routes})()
    cfg = type("C", (), {"attempt_id": "att_1", "generation": 2, "task_id": "task_1"})()
    factory = relay_mod.Relay if upstream is None else functools.partial(
        relay_mod.Relay, resolver=lambda host: [PUBLIC], connector=upstream.connector)
    return attach_mod.McpAttachment(cfg, children=children, logger=log or _Log(),
                                    relay_factory=factory)


@pytest.fixture(autouse=True)
def _unsigned(monkeypatch):
    """The stand-in attachments sign nothing; the worker runs verify for real."""
    real = attach_mod.sign_headers
    monkeypatch.setattr(attach_mod, "sign_headers", lambda key, *a, **k: {}
                        if type(key) is object else real(key, *a, **k))


@pytest.mark.parametrize("change", [
    {"url": "http://kb.example.com/mcp"},
    {"url": "https://10.0.0.1/mcp"},
    {"url": "https://user@kb.example.com/mcp"},
    {"url": "https://kb.example.com:8443/mcp"},
    {"url": "https://kb.example.com/mcp?x=1"},
    {"url": "https://localhost/mcp"},
    {"auth": {"header": "Cookie", "scheme": "Bearer", "credential": "c" * 20}},
    {"auth": {"header": "Authorization", "scheme": "Token", "credential": "c" * 20}},
    {"auth": {"header": "Authorization", "scheme": "Bearer", "credential": "a\nb" * 5}},
    {"allowed_tools": ["ok", "not ok"]},
    {"name": "other"},
])
def test_a_server_the_workers_rules_refuse_is_not_attached(change):
    item = {**_server(credential="v" * 24), **change}
    server, why = attach_mod.server_from_answer(item, ["kb"])
    assert server is None and why


def test_a_server_the_rules_accept_is_attached_whole():
    credential = fake_credential()
    server, why = attach_mod.server_from_answer(
        _server(credential=credential, url="https://KB.Example.com:443/v1/mcp"), ["kb"])
    assert why == "" and server is not None
    assert (server.host, server.path, server.credential) == ("kb.example.com", "/v1/mcp",
                                                             credential)
    assert server.allowed_tools == frozenset({"search"}) and server.revision == 3


def test_every_credential_is_registered_before_it_is_validated():
    good, bad = fake_credential(), fake_credential()
    log = _Log()
    attachment = _attachment(_server(credential=good),
                             _server(name="crm", credential=bad, url="http://crm.example.com"),
                             log=log)
    result = attachment.attach(profile=RUNNER_PROFILES["claude-code"],
                               inputs={"mcp_servers": ["kb", "crm"]})
    assert good in log.secrets and bad in log.secrets
    assert [s.name for s in result.servers] == ["kb"]
    assert result.withheld == [("crm", "refused_by_worker_url")]
    assert result.failure() == (attach_mod.WITHHELD, "crm:refused_by_worker_url", False)


def test_the_relay_file_is_private_and_holds_no_credential(tmp_path):
    credential = fake_credential()
    attachment = _attachment(_server(credential=credential))
    attachment.attach(profile=RUNNER_PROFILES["claude-code"], inputs={"mcp_servers": ["kb"]})
    path = attachment.start(tmp_path)
    try:
        assert path is not None and stat.S_IMODE(path.stat().st_mode) == 0o600
        assert credential not in path.read_text()
        assert attachment.env == {attach_mod.RELAY_ENV: str(path)}
    finally:
        attachment.close()
    assert not path.exists() and attachment.env == {}
