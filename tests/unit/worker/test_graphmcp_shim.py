"""The pod's shim in front of the shared graph service (docs/design/graph-service.md
§4.3-§4.4, §10; lane GS-SHIM, part of #1384).

`python -m agent_worker.graphmcp.shim --pin <pin.json> --ticket-file <file>
--workdir <checkout>` is registered as `swarm-graph`. Held here, offline (the
service is a fake transport or a loopback HTTP server; no credential, no
cluster):

  * `initialize`, `ping` and `tools/list` are answered locally: the service
    down never empties the agent's tool list;
  * a `tools/call` is forwarded with the ticket, and the service's answer
    (computed under the empty observation) comes back `tools.remark`ed with
    the pod's own observation -- byte for byte the local server's page;
  * the ticket file is re-read only when it changes, and once more on a 401;
  * every failure §10 assigns to the shim is told in §10's words, freshness
    first, `isError: true`, never an empty list that reads as an answer;
  * the ticket never goes to stderr, an answer, a proxy or a redirect.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest
import repo_index_fixtures as fx
from test_graphmcp_source import write_hand_snapshot

from agent_worker.graphmcp import shim, tools
from agent_worker.graphmcp.freshness import CHANGED, DIRTY, Freshness, Observation
from agent_worker.graphmcp.server import GraphServer
from agent_worker.graphmcp.snapshot import Snapshot


def fake_ticket(exp: float | None = None) -> str:
    """Ticket-shaped, built at runtime: `<b64url(json)>.<b64url(mac)>`."""
    payload = {"tenant_id": "eng", "exp": int(exp if exp is not None else time.time() + 600),
               "nonce": secrets.token_hex(8)}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    return b64(raw) + "." + b64(secrets.token_bytes(32))


class FixedFreshness:
    """`Freshness` with a fixed observation; counts the git calls it stands for."""

    def __init__(self, obs: Observation) -> None:
        self.obs = obs
        self.observed = 0

    def observe(self) -> Observation:
        self.observed += 1
        return self.obs


@pytest.fixture(scope="module")
def snap(tmp_path_factory) -> Snapshot:
    return Snapshot(write_hand_snapshot(tmp_path_factory.mktemp("shim") / "snap"))


def observation(snap: Snapshot) -> Observation:
    paths = sorted({str(r["path"]) for key in snap.keys("files")
                    for r in snap.shard("files", key).rows if r.get("path")})
    assert len(paths) >= 2, paths
    dirty, changed = {paths[0]}, {paths[-1]}
    block = {"index_sha": snap.commit_sha, "base_sha": "b" * 40, "behind_by": 2,
             "changed_since_index": len(changed), "dirty": len(dirty)}
    return Observation(block=block, changed=frozenset(changed), dirty=frozenset(dirty))


class Service:
    """A fake transport: the service's answer computed with GS-CORE's own code
    under the empty observation, or a scripted status."""

    def __init__(self, snap: Snapshot | None, script: list[Any] | None = None) -> None:
        self.snap = snap
        self.script = list(script or [])
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str],
                 timeout: float) -> tuple[int, bytes]:
        message = json.loads(body)
        self.calls.append({"url": url, "message": message, "headers": dict(headers),
                           "timeout": timeout})
        step = self.script.pop(0) if self.script else 200
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step(url, body, headers, timeout)
        if isinstance(step, tuple):
            return step
        if step != 200:
            return step, b"{}"
        assert self.snap is not None
        params = message["params"]
        text, failed = tools.call(self.snap, Observation.empty(self.snap.commit_sha),
                                  params["name"], params.get("arguments"))
        return 200, json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {
            "content": [{"type": "text", "text": text}], "isError": failed}}).encode()


def make_shim(tmp_path: Path, transport: Callable[..., tuple[int, bytes]], *,
              obs: Observation | None = None, ticket: str | None = None,
              index_sha: str = "a" * 40, now: float | None = None,
              sleeps: list[float] | None = None) -> tuple[shim.ShimServer, Path]:
    ticket_file = tmp_path / "private" / "graph-ticket"
    ticket_file.parent.mkdir(parents=True, exist_ok=True)
    if ticket is not None:
        ticket_file.write_text(ticket + "\n")
    block = {"index_sha": index_sha}
    fresh = FixedFreshness(obs or Observation(block=block, changed=frozenset(),
                                              dirty=frozenset()))
    recorded = sleeps if sleeps is not None else []
    server = shim.ShimServer(fresh, ticket=shim.TicketFile(ticket_file), index_sha=index_sha,
                             url=shim.DEFAULT_URL, transport=transport,
                             sleep=recorded.append,
                             clock=(lambda: now) if now is not None else time.time)
    return server, ticket_file


def ask(server: shim.ShimServer, name: str = "search", arguments: dict | None = None,
        ident: Any = 7) -> dict:
    response = server.handle({"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                              "params": {"name": name,
                                         "arguments": arguments or {"query": "load"}}})
    assert response is not None and response["id"] == ident
    return response


def text_of(response: dict) -> tuple[dict, bool]:
    result = response["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def never(*_a: Any, **_k: Any) -> tuple[int, bytes]:
    raise AssertionError("the shim called the service for a local method")


# --- local methods ------------------------------------------------------------

def test_initialize_ping_and_tools_list_are_answered_locally(tmp_path):
    server, _ = make_shim(tmp_path, never)
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["serverInfo"]["name"] == "swarm-graph"
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert server.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}
    listed = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    # The shim's own package's list: the eight, call_graph and index.
    assert listed["result"]["tools"] == tools.TOOLS
    assert [t["name"] for t in listed["result"]["tools"]] == list(tools.TOOL_NAMES)
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_survives_a_missing_ticket_and_a_dead_service(tmp_path):
    server, ticket_file = make_shim(tmp_path, never)
    assert not ticket_file.exists()
    listed = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert len(listed["result"]["tools"]) == len(tools.TOOLS)


def test_a_call_with_no_tool_name_is_invalid_params_without_a_request(tmp_path):
    server, _ = make_shim(tmp_path, never, ticket=fake_ticket())
    response = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                              "params": {"arguments": {}}})
    assert response["error"]["code"] == -32602


# --- forward and remark -------------------------------------------------------

QUESTIONS = [("search", {"query": "load"}), ("index", {"layer": "files"}),
             ("index", {"layer": "symbols"}), ("context", {"symbol": "nowhere#gone"}),
             ("nope", {})]


@pytest.mark.parametrize("name,arguments", QUESTIONS)
def test_the_shims_answer_is_the_local_servers_answer(tmp_path, snap, name, arguments):
    obs = observation(snap)
    service = Service(snap)
    server, _ = make_shim(tmp_path, service, obs=obs, ticket=fake_ticket(),
                          index_sha=snap.commit_sha)
    local = GraphServer(snap, FixedFreshness(obs)).handle(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}})
    remote = ask(server, name, arguments)
    assert remote == local
    assert len(service.calls) == 1


def test_every_page_through_the_shim_carries_the_pods_marks(tmp_path, snap, monkeypatch):
    monkeypatch.setattr(tools, "ANSWER_CAP", CAP)
    obs = observation(snap)
    server, _ = make_shim(tmp_path, Service(snap), obs=obs, ticket=fake_ticket(),
                          index_sha=snap.commit_sha)
    local = GraphServer(snap, FixedFreshness(obs))
    marks: set[str] = set()
    questions = pages = 0
    for layer in ("files", "symbols"):
        for key in snap.keys(layer):
            questions += 1
            cursor = None
            while True:
                arguments = {"layer": layer, "key": key,
                             **({"cursor": cursor} if cursor else {})}
                mine = ask(server, "index", arguments)
                theirs = local.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                       "params": {"name": "index", "arguments": arguments}})
                assert mine == theirs
                answer, failed = text_of(mine)
                assert not failed, answer
                assert next(iter(answer)) == "freshness"
                assert answer["freshness"] == obs.block
                marks |= {row.get("stale") for row in answer["rows"] if row.get("stale")}
                pages += 1
                cursor = answer["next_cursor"]
                if cursor is None:
                    break
    assert pages > questions, "the cap is small enough that some answer pages"
    assert marks == {DIRTY, CHANGED}


#: Small enough that a shard of the hand snapshot pages, large enough for its head.
CAP = 520


def test_the_request_carries_the_ticket_and_only_name_and_arguments(tmp_path, snap):
    ticket = fake_ticket()
    service = Service(snap)
    server, _ = make_shim(tmp_path, service, ticket=ticket, index_sha=snap.commit_sha)
    server.handle({"jsonrpc": "2.0", "id": "q1", "method": "tools/call",
                   "params": {"name": "search", "arguments": {"query": "x"},
                              "_meta": {"progressToken": 3}}})
    (call,) = service.calls
    assert call["headers"]["Authorization"] == "Bearer " + ticket
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["url"] == shim.DEFAULT_URL
    assert call["timeout"] == shim.CALL_TIMEOUT_SECONDS == 10.0
    assert call["message"] == {"jsonrpc": "2.0", "id": "q1", "method": "tools/call",
                               "params": {"name": "search", "arguments": {"query": "x"}}}


def test_the_observation_is_taken_once_per_answer(tmp_path, snap):
    server, _ = make_shim(tmp_path, Service(snap), ticket=fake_ticket(),
                          index_sha=snap.commit_sha)
    for _ in range(3):
        ask(server)
    assert server.freshness.observed == 3


def test_a_json_rpc_error_from_the_service_is_passed_through(tmp_path):
    body = json.dumps({"jsonrpc": "2.0", "id": 7,
                       "error": {"code": -32602, "message": "params must be an object"}})
    server, _ = make_shim(tmp_path, Service(None, [(200, body.encode())]),
                          ticket=fake_ticket())
    response = ask(server)
    assert response["error"] == {"code": -32602, "message": "params must be an object"}


# --- the ticket file ----------------------------------------------------------

def test_the_ticket_file_is_re_read_only_when_it_changes(tmp_path, snap):
    first = fake_ticket()
    service = Service(snap)
    server, ticket_file = make_shim(tmp_path, service, ticket=first, index_sha=snap.commit_sha)
    ask(server)
    ask(server)
    assert server.ticket.reads == 1
    second = fake_ticket()
    staged = ticket_file.with_name("graph-ticket.tmp")
    staged.write_text(second)
    os.replace(staged, ticket_file)  # the worker's write-then-rename
    ask(server)
    assert server.ticket.reads == 2
    assert [c["headers"]["Authorization"] for c in service.calls] == \
        ["Bearer " + first] * 2 + ["Bearer " + second]


def test_a_401_re_reads_the_ticket_once_and_retries(tmp_path, snap):
    first, renewed = fake_ticket(), fake_ticket()
    holder: dict[str, Path] = {}

    def renewal_race(url, body, headers, timeout):
        # The worker renews between the shim's read and the service's check,
        # keeping the size and mtime, so only a forced re-read sees it.
        path = holder["file"]
        stat = path.stat()
        path.write_text(renewed + "\n")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        return 401, b'{"error": "ticket_refused"}'

    service = Service(snap, [renewal_race, 200])
    server, ticket_file = make_shim(tmp_path, service, ticket=first,
                                    index_sha=snap.commit_sha)
    holder["file"] = ticket_file
    answer, failed = text_of(ask(server))
    assert not failed and "rows" in answer
    assert [c["headers"]["Authorization"] for c in service.calls] == \
        ["Bearer " + first, "Bearer " + renewed]


@pytest.mark.parametrize("expired", [True, False])
def test_a_persistent_401_says_the_ticket_was_refused(tmp_path, expired):
    now = 1_800_000_000.0
    ticket = fake_ticket(exp=now - 5 if expired else now + 300)
    service = Service(None, [401, 401])
    server, _ = make_shim(tmp_path, service, ticket=ticket, now=now)
    answer, failed = text_of(ask(server))
    why = "expired" if expired else "invalid"
    assert failed
    assert answer["error"] == shim.TICKET_REFUSED.format(why=why)
    assert answer["error"] == (f"the graph service refused this attempt's ticket ({why}); "
                               "no answer was computed.")
    assert len(service.calls) == 2


def test_the_services_401_reason_wins_over_the_local_reading(tmp_path):
    body = json.dumps({"error": "ticket_refused", "reason": "expired"}).encode()
    server, _ = make_shim(tmp_path, Service(None, [(401, body), (401, body)]),
                          ticket=fake_ticket())
    answer, _ = text_of(ask(server))
    assert answer["error"] == shim.TICKET_REFUSED.format(why="expired")


def test_no_ticket_file_is_refused_without_a_request(tmp_path):
    server, _ = make_shim(tmp_path, never)
    answer, failed = text_of(ask(server))
    assert failed
    assert answer["error"] == shim.TICKET_REFUSED.format(why="missing")


@pytest.mark.parametrize("content", ["", "two words", "line\r\nX-Injected: 1",
                                     "x" * (shim.MAX_TICKET_BYTES + 1)])
def test_a_ticket_that_cannot_be_a_header_is_never_sent(tmp_path, content):
    server, ticket_file = make_shim(tmp_path, never)
    ticket_file.write_text(content)
    answer, failed = text_of(ask(server))
    assert failed and "refused this attempt's ticket" in answer["error"]


# --- §10, every message the shim gives ----------------------------------------

def assert_told(response: dict, message: str, obs_block: dict | None = None) -> dict:
    answer, failed = text_of(response)
    assert failed, "a failure is a failed tool call"
    assert next(iter(answer)) == "freshness", "freshness first, on errors too"
    if obs_block is not None:
        assert answer["freshness"] == obs_block
    assert answer["error"] == message
    assert "rows" not in answer and "total" not in answer, "never an empty list"
    return answer


def test_service_down_mid_attempt_retries_once_after_a_second(tmp_path):
    sleeps: list[float] = []
    service = Service(None, [shim.ServiceUnreachable("connection refused"),
                             shim.ServiceUnreachable("connection refused")])
    server, _ = make_shim(tmp_path, service, ticket=fake_ticket(), sleeps=sleeps)
    assert_told(ask(server), shim.UNREACHABLE.format(cause="connection refused"))
    assert shim.UNREACHABLE.format(cause="timed out") == (
        "the graph service is unreachable (timed out); no answer was computed. "
        "Retry later, or read the code directly.")
    assert len(service.calls) == 2
    assert sleeps == [1.0]


def test_a_retry_that_reaches_the_service_answers_normally(tmp_path, snap):
    service = Service(snap, [shim.ServiceUnreachable("timed out"), 200])
    server, _ = make_shim(tmp_path, service, ticket=fake_ticket(), index_sha=snap.commit_sha)
    answer, failed = text_of(ask(server))
    assert not failed and "rows" in answer


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_5xx_is_unreachable_after_one_retry(tmp_path, status):
    sleeps: list[float] = []
    body = json.dumps({"error": "store_unavailable"}).encode()
    service = Service(None, [(status, body), (status, body)])
    server, _ = make_shim(tmp_path, service, ticket=fake_ticket(), sleeps=sleeps)
    assert_told(ask(server), shim.UNREACHABLE.format(cause=f"HTTP {status}"))
    assert len(service.calls) == 2 and sleeps == [1.0]


def test_service_disabled_is_told_without_a_retry(tmp_path):
    sleeps: list[float] = []
    service = Service(None, [(503, b'{"error": "disabled"}')])
    server, _ = make_shim(tmp_path, service, ticket=fake_ticket(), sleeps=sleeps)
    assert_told(ask(server), shim.DISABLED)
    assert shim.DISABLED == ("the graph service is switched off by the platform; "
                             "no answer was computed.")
    assert len(service.calls) == 1 and sleeps == []


def test_index_missing_names_the_pinned_commit(tmp_path):
    sha = "c0ffee" + "1" * 34
    server, _ = make_shim(tmp_path, Service(None, [404]), ticket=fake_ticket(), index_sha=sha)
    assert_told(ask(server), f"the index of {sha[:12]} is no longer stored (pruned or "
                             "missing); no answer was computed.")


def test_a_manifest_that_does_not_match_the_pin_is_refused(tmp_path):
    sha = "d" * 40
    server, _ = make_shim(tmp_path, Service(None, [409]), ticket=fake_ticket(), index_sha=sha)
    assert_told(ask(server), f"the stored index of {sha[:12]} does not match its promoted "
                             "digest; refused.")


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"jsonrpc": "2.0", "id": 7}',
                                  b'{"jsonrpc":"2.0","id":7,"result":{"content":[]}}'])
def test_an_unreadable_answer_is_never_served(tmp_path, body):
    server, _ = make_shim(tmp_path, Service(None, [(200, body), (200, body)]),
                          ticket=fake_ticket())
    assert_told(ask(server), shim.UNREACHABLE.format(cause=shim.UNREADABLE))


def test_a_cold_cache_is_not_the_agents_concern(tmp_path, snap):
    # §10: a cold cache is slower, never different -- the shim sees a 200 and
    # serves it; nothing about the cache reaches the answer.
    service = Service(snap)
    server, _ = make_shim(tmp_path, service, ticket=fake_ticket(), index_sha=snap.commit_sha)
    answer, _ = text_of(ask(server))
    assert "cache" not in answer


def test_a_failure_carries_the_pods_freshness_block(tmp_path):
    obs = Observation(block={"index_sha": "e" * 40, "base_sha": "f" * 40, "behind_by": 0,
                             "changed_since_index": 0, "dirty": 3},
                      changed=frozenset(), dirty=frozenset({"a.py"}))
    server, _ = make_shim(tmp_path, Service(None, [404]), obs=obs, ticket=fake_ticket(),
                          index_sha="e" * 40)
    assert_told(ask(server), shim.INDEX_MISSING.format(sha12="e" * 12), obs.block)


def test_git_failing_in_the_shim_says_dirty_unknown(tmp_path, snap):
    # No checkout git can read: never zero, always `unknown` (freshness.Observation).
    nowhere = tmp_path / "not-a-repo"
    nowhere.mkdir()
    fresh = Freshness.establish(snap.commit_sha, workdir=nowhere,
                                record={"index_sha": snap.commit_sha, "changed": []})
    server = shim.ShimServer(fresh, ticket=shim.TicketFile(_ticket_in(tmp_path)),
                             index_sha=snap.commit_sha, url=shim.DEFAULT_URL,
                             transport=Service(snap), sleep=lambda _s: None)
    answer, _ = text_of(ask(server))
    assert answer["freshness"]["dirty"] == "unknown"


def _ticket_in(tmp_path: Path) -> Path:
    path = tmp_path / "private" / "graph-ticket"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fake_ticket())
    return path


# --- the ticket stays secret --------------------------------------------------

def test_the_ticket_never_reaches_stderr_or_an_answer(tmp_path, capsys):
    ticket = fake_ticket()
    scripts = [[shim.ServiceUnreachable("refused"), shim.ServiceUnreachable("refused")],
               [401, 401], [(503, b'{"error":"disabled"}')], [404], [409], [418]]
    texts = []
    for i, script in enumerate(scripts):
        server, _ = make_shim(tmp_path / str(i), Service(None, script), ticket=ticket)
        texts.append(json.dumps(server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "search", "arguments": {"query": "q"}}})))
    captured = capsys.readouterr()
    for text in [*texts, captured.err, captured.out]:
        assert ticket not in text
        assert ticket.split(".")[0] not in text


# --- the HTTP transport, over loopback ----------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []
    status = 200
    location: str | None = None

    def do_POST(self) -> None:  # noqa: N802 (http.server's name)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        type(self).seen.append({"path": self.path, "headers": dict(self.headers),
                                "body": json.loads(body)})
        self.send_response(type(self).status)
        if type(self).location:
            self.send_header("Location", type(self).location)
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": "{}"}], "isError": False}}).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 (a followed 303 arrives as a GET)
        type(self).seen.append({"path": self.path, "headers": dict(self.headers), "body": None})
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture()
def loopback():
    _Handler.seen = []
    _Handler.status = 200
    _Handler.location = None
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_http_transport_posts_the_ticket_and_ignores_any_proxy(loopback, monkeypatch):
    # The pod's egress proxy must never see a graph ticket: the service is in-cluster.
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    ticket = fake_ticket()
    status, body = shim.http_transport(loopback, b'{"jsonrpc":"2.0","id":1}',
                                       shim.request_headers(ticket), 2.0)
    assert status == 200 and json.loads(body)["result"]["isError"] is False
    (seen,) = _Handler.seen
    assert seen["path"] == "/mcp"
    assert seen["headers"]["Authorization"] == "Bearer " + ticket


@pytest.mark.parametrize("status", [401, 404, 409, 503])
def test_the_http_transport_returns_error_statuses(loopback, status):
    _Handler.status = status
    got, _ = shim.http_transport(loopback, b"{}", shim.request_headers(fake_ticket()), 2.0)
    assert got == status


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_the_http_transport_never_follows_a_redirect(loopback, status):
    _Handler.status = status
    _Handler.location = loopback.replace("/mcp", "/elsewhere")
    with pytest.raises(shim.ServiceUnreachable):
        shim.http_transport(loopback, b"{}", shim.request_headers(fake_ticket()), 2.0)
    assert [s["path"] for s in _Handler.seen] == ["/mcp"]


def test_the_http_transport_says_unreachable_when_nothing_listens():
    with pytest.raises(shim.ServiceUnreachable):
        shim.http_transport("http://127.0.0.1:9/mcp", b"{}",
                            shim.request_headers(fake_ticket()), 1.0)


# --- main: the argv §4.2 registers --------------------------------------------

def _git(repo: Path, *args: str) -> str:
    env = fx.git_env(repo.parent / "home")
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def test_main_refuses_to_start_without_a_readable_pin(tmp_path, capsys):
    ticket = _ticket_in(tmp_path)
    assert shim.main(["--pin", str(tmp_path / "absent.json"),
                      "--ticket-file", str(ticket)]) == 2
    bad = tmp_path / "pin.json"
    bad.write_text(json.dumps({"index_sha": "not-a-sha"}))
    assert shim.main(["--pin", str(bad), "--ticket-file", str(ticket)]) == 2
    assert "pin" in capsys.readouterr().err


def test_main_serves_from_the_pin_and_the_checkout(tmp_path, snap, monkeypatch):
    repo = fx.build_repo(tmp_path / "repo", {"src/a.py": "x = 1\n", "src/b.py": "y = 2\n"})
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "a.py").write_text("x = 3\n")
    pin = tmp_path / "work" / ".swarm-context" / "pin.json"
    pin.parent.mkdir(parents=True)
    pin.write_text(json.dumps({"index_sha": snap.commit_sha, "manifest_digest": "sha256:" + "0" * 64,
                               "base_sha": head, "behind_by": 1, "basis": "staged",
                               "changed": ["src/b.py"]}))
    service = Service(snap)
    monkeypatch.setattr(shim, "http_transport", service)
    monkeypatch.setenv(shim.URL_ENV, "http://swarm-graph.example.internal:8080/mcp")
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "search", "arguments": {"query": "load"}}}]
    stdin = tmp_path / "in.jsonl"
    stdin.write_text("".join(json.dumps(r) + "\n" for r in requests))
    stdout = tmp_path / "out.jsonl"
    with stdin.open("rb") as fin, stdout.open("wb") as fout:
        monkeypatch.setattr(shim.sys, "stdin", type("I", (), {"buffer": fin})())
        monkeypatch.setattr(shim.sys, "stdout", type("O", (), {"buffer": fout})())
        assert shim.main(["--pin", str(pin), "--ticket-file", str(_ticket_in(tmp_path)),
                          "--workdir", str(repo)]) == 0
    listed, called = [json.loads(line) for line in stdout.read_text().splitlines()]
    assert listed["result"]["tools"] == tools.TOOLS
    block = json.loads(called["result"]["content"][0]["text"])["freshness"]
    assert block == {"index_sha": snap.commit_sha, "base_sha": head, "behind_by": 1,
                     "changed_since_index": 1, "dirty": 1}
    assert service.calls[0]["url"] == "http://swarm-graph.example.internal:8080/mcp"


def test_service_url_defaults_and_refuses_another_scheme(monkeypatch):
    monkeypatch.delenv(shim.URL_ENV, raising=False)
    assert shim.service_url() == shim.DEFAULT_URL == \
        "http://swarm-graph.swarm-system.svc.cluster.local:8080/mcp"
    monkeypatch.setenv(shim.URL_ENV, "file:///etc/passwd")
    with pytest.raises(ValueError):
        shim.service_url()
