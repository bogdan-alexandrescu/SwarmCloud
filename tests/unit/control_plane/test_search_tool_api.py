"""The search tool's API half (docs/search-tool.md; lane SEARCH-TOOL, part of #1030).

Drives the real route, `POST /v1/attempts/{attempt_id}/search`, over the
in-memory Firestore, with a worker identity the static verifier maps to the
tenant's worker service account and an attempt key registered exactly as a
worker registers one (`test_child_tasks_api`). The provider is a fake
transport under the real `BraveSearch`, and the key a fake source, so nothing
reaches the network or Secret Manager.

Every key is built at runtime (CLAUDE.md: nothing added may look like a
credential).

What each case pins, from the owner's brief:

* the flag is OFF by default, and off the route is a 404 that writes nothing,
  and the key registration tells the worker `search_tool: false`;
* an unproven request is 403, one answer for every cause, and writes nothing;
* a stale generation is 409 `search_fenced` with a `fenced` audit row;
* only a live-browser task is answered;
* the tenant's daily cap is 429 `search_daily_cap`, counted across attempts;
* each provider failure maps to its own code and status, never 409;
* the key is in no answer, no audit row and no log line;
* a person reads a task's search calls under the task's own scope.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from swarm_api import search as search_mod
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.search import BraveSearch, KeyUnavailable, ProviderError, SearchTool
from swarm_api.waker import NullWaker

from .conftest import api_settings, auth_header, seed_tenant
from .test_child_tasks_api import (  # noqa: F401  (worker_tokens is a fixture)
    KEY,
    OTHER_WORKER_TOKEN,
    WORKER_TOKEN,
    Attempt,
    ready_parent,
    register,
    seed_parent,
    worker_tokens,
)

BROWSER = "claude-code-browser"
AUDIT = "search_audit"


def make_key() -> str:
    # Runtime-built: no line of this file holds a credential-shaped literal.
    return "BSA" + secrets.token_hex(16)


class Brave:
    """Brave's web search endpoint, faked: records each call, answers `reply`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.status = 200
        self.body: bytes | None = None
        self.raises: Exception | None = None

    def results(self, n: int) -> bytes:
        return json.dumps({"web": {"results": [
            {"title": f"<strong>Result</strong> {i} &amp; more",
             "url": f"https://example.com/{i}",
             "description": f"snippet <strong>{i}</strong>"}
            for i in range(n)
        ]}}).encode()

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        if self.raises is not None:
            raise self.raises
        return self.status, self.body if self.body is not None else self.results(20)


class Key:
    def __init__(self) -> None:
        self.value = make_key()
        self.reads = 0
        self.fail: str | None = None

    def read(self) -> str:
        self.reads += 1
        if self.fail:
            raise KeyUnavailable(self.fail)
        return self.value


@pytest.fixture
def brave() -> Brave:
    return Brave()


@pytest.fixture
def key() -> Key:
    return Key()


def _app(db, worker_tokens, group_map, objects, brave, key, **settings):  # noqa: F811
    from swarm_api.routes import search as routes
    from swarm_api.routes.children import _worker_verifier

    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    ctx = build_context(
        settings=api_settings(**{"child_key": KEY, **settings}),
        db=db,
        verifier=StaticTokenVerifier(worker_tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )
    application = create_app(ctx)

    def service() -> SearchTool:
        return SearchTool.from_context(ctx, verifier=_worker_verifier(ctx),
                                       provider=BraveSearch(send=brave), key=key)

    application.dependency_overrides[routes.search_tool] = service
    return TestClient(application, raise_server_exceptions=False)


@pytest.fixture
def client(db, worker_tokens, group_map, objects, brave, key) -> TestClient:  # noqa: F811
    return _app(db, worker_tokens, group_map, objects, brave, key,
                search_tool_enabled=True, search_daily_queries_per_tenant=3)


@pytest.fixture
def off_client(db, worker_tokens, group_map, objects, brave, key) -> TestClient:  # noqa: F811
    return _app(db, worker_tokens, group_map, objects, brave, key)


def running(client, db, attempt: Attempt | None = None, *, profile: str = BROWSER) -> Attempt:
    attempt = attempt or Attempt()
    ready_parent(client, db, attempt, runner_profile=profile)
    return attempt


def call(client, attempt: Attempt, query: str = "python release notes", *,
         count: int | None = 5, token: str = WORKER_TOKEN, sign: bool = True,
         extra: dict[str, Any] | None = None, request_id: str = "req-1",
         path_attempt: str | None = None, tamper: bool = False) -> Any:
    """The worker's call, shaped exactly as `agent_worker.searchtools.SearchTools.call` sends it."""
    t = attempt.tuple
    fields: dict[str, Any] = {
        "tenant_id": t.tenant_id, "task_id": t.task_id, "attempt_id": t.attempt_id,
        "lease_id": t.lease_id, "generation": t.generation, "request_id": request_id,
        "query": query, **(extra or {}),
    }
    if count is not None:
        fields["count"] = count
    body = json.dumps(fields, sort_keys=True).encode("utf-8")
    path = f"/v1/attempts/{path_attempt or t.attempt_id}/search"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if sign:
        headers.update(attempt.sign("POST", path, body))
    if tamper:
        body = body.replace(b"req-1", b"req-2")
    return client.post(path, content=body, headers=headers)


def audit_rows(db) -> list[dict[str, Any]]:
    return [doc for path, doc in sorted(db.docs.items(), reverse=True)
            if path.startswith(f"{AUDIT}/")]


# --------------------------------------------------------------------------
# the flag
# --------------------------------------------------------------------------


def test_the_flag_is_off_by_default():
    assert api_settings().search_tool_enabled is False


def _env(monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.setenv("ENVIRONMENT", "dev")


def test_from_env_reads_the_flag_off_unless_set(monkeypatch):
    from swarm_api.settings import ApiSettings

    _env(monkeypatch)
    for name in ("SEARCH_TOOL_ENABLED", "SEARCH_PROVIDER", "SEARCH_API_KEY_SECRET",
                 "SEARCH_DAILY_QUERIES_PER_TENANT"):
        monkeypatch.delenv(name, raising=False)
    settings = ApiSettings.from_env()
    assert settings.search_tool_enabled is False
    assert settings.search_provider == "brave"
    assert settings.search_api_key_secret == "swarm-search-api-key"
    monkeypatch.setenv("SEARCH_TOOL_ENABLED", "true")
    assert ApiSettings.from_env().search_tool_enabled is True


def test_an_unknown_provider_is_refused_at_start(monkeypatch):
    from swarm_api.settings import ApiSettings

    _env(monkeypatch)
    ApiSettings.from_env()  # the control: it starts with the default provider
    monkeypatch.setenv("SEARCH_PROVIDER", "altavista")
    with pytest.raises(ValueError, match="SEARCH_PROVIDER"):
        ApiSettings.from_env()


def test_off_the_route_is_a_404_that_reads_and_writes_nothing(off_client, db, brave, key):
    attempt = running(off_client, db)
    before = dict(db.docs)
    response = call(off_client, attempt)
    assert response.status_code == 404, response.text
    assert brave.calls == [] and key.reads == 0
    assert db.docs == before


def test_off_the_registration_tells_the_worker_no_search_tool(off_client, db):
    attempt = Attempt()
    seed_parent(db, attempt, state="STARTING", runner_profile=BROWSER)
    response = register(off_client, db, attempt)
    assert response.status_code == 201, response.text
    assert response.json()["search_tool"] is False


def test_on_the_registration_tells_a_keyed_worker_it_has_the_search_tool(client, db):
    attempt = Attempt()
    seed_parent(db, attempt, state="STARTING", runner_profile=BROWSER)
    response = register(client, db, attempt)
    assert response.status_code == 201, response.text
    assert response.json()["search_tool"] is True


# --------------------------------------------------------------------------
# who, and the fence
# --------------------------------------------------------------------------


def test_a_proven_live_browser_attempt_gets_results(client, db, brave, key):
    attempt = running(client, db)
    response = call(client, attempt, "python 3.13 release notes", count=3)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "brave" and body["count"] == 3
    assert body["results"][0] == {"title": "Result 0 & more", "url": "https://example.com/0",
                                  "snippet": "snippet 0"}
    assert len(body["results"]) == 3
    # The provider was asked for exactly this query and count, with the key in
    # its header, and nowhere in the URL.
    (method, url, headers), = brave.calls
    assert method == "GET"
    asked = parse_qs(urlparse(url).query)
    assert asked == {"q": ["python 3.13 release notes"], "count": ["3"]}
    assert headers["X-Subscription-Token"] == key.value
    assert key.value not in url
    row, = audit_rows(db)
    assert row["result"] == "ok" and row["results"] == 3 and row["provider"] == "brave"
    assert row["query"] == "python 3.13 release notes" and row["tool"] == "search"


@pytest.mark.parametrize("case", ["no_token", "other_tenant", "person", "unsigned",
                                  "tampered", "other_attempt_in_path"])
def test_an_unproven_request_is_one_403_and_writes_nothing(client, db, brave, key, case):
    attempt = running(client, db)
    before = {k for k in db.docs}
    kwargs: dict[str, Any] = {
        "no_token": {"token": ""},
        "other_tenant": {"token": OTHER_WORKER_TOKEN},
        "person": {"token": auth_header("alice")["Authorization"].split()[1]},
        "unsigned": {"sign": False},
        "tampered": {"tamper": True},
        "other_attempt_in_path": {"path_attempt": "att_someone_else"},
    }[case]
    response = call(client, attempt, **kwargs)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "search_unproven"
    assert response.json()["message"] == search_mod.UNPROVEN_MESSAGE
    assert brave.calls == [] and key.reads == 0
    assert {k for k in db.docs} == before


def test_a_stale_generation_is_fenced_and_audited(client, db, brave):
    attempt = running(client, db)
    db.docs[f"tasks/{attempt.tuple.task_id}"]["current_generation"] = 2
    response = call(client, attempt)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "search_fenced"
    assert brave.calls == []
    row, = audit_rows(db)
    assert row["result"] == "fenced" and row["code"] == "search_fenced"


def test_a_cancel_requested_task_is_fenced(client, db, brave):
    attempt = running(client, db)
    db.docs[f"tasks/{attempt.tuple.task_id}"]["cancel_requested"] = True
    assert call(client, attempt).status_code == 409
    assert brave.calls == []


def test_a_task_that_is_not_a_live_browser_task_is_not_answered(client, db, brave):
    attempt = running(client, db, profile="claude-code")
    response = call(client, attempt)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "search_not_offered"
    assert brave.calls == []
    assert audit_rows(db)[0]["code"] == "search_not_offered"


@pytest.mark.parametrize("extra", [{"provider": "google"}, {"url": "https://x.example"},
                                   {"key": "k"}])
def test_the_caller_names_no_provider_url_or_key(client, db, brave, extra):
    attempt = running(client, db)
    response = call(client, attempt, extra=extra)
    assert response.status_code == 422
    assert brave.calls == []


@pytest.mark.parametrize("count", [0, 11, 50])
def test_count_is_one_to_ten(client, db, brave, count):
    attempt = running(client, db)
    assert call(client, attempt, count=count).status_code == 422
    assert brave.calls == []


def test_count_defaults_to_five(client, db, brave):
    attempt = running(client, db)
    response = call(client, attempt, count=None)
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 5


# --------------------------------------------------------------------------
# the cap
# --------------------------------------------------------------------------


def test_the_tenants_daily_cap_counts_every_attempt_and_refuses_429(client, db, brave):
    first = running(client, db)
    for i in range(2):
        assert call(client, first, request_id=f"req-{i}").status_code == 200
    second = running(client, db, Attempt(task="task_second", attempt="att_second",
                                         lease="lease_second"))
    assert call(client, second, request_id="req-2").status_code == 200
    response = call(client, second, request_id="req-3")
    assert response.status_code == 429, response.text
    assert response.json()["code"] == "search_daily_cap"
    assert len(brave.calls) == 3
    assert [r["code"] for r in audit_rows(db)].count("search_daily_cap") == 1
    quota = [doc for path, doc in db.docs.items() if path.startswith("search_quota/")]
    assert [q["queries"] for q in quota] == [3]


def test_another_tenants_queries_do_not_count(client, db, brave):
    eng = running(client, db)
    for i in range(3):
        assert call(client, eng, request_id=f"req-{i}").status_code == 200
    db.docs["search_quota/research__seed"] = {"tenant_id": "research", "queries": 99}
    assert call(client, eng, request_id="req-9").status_code == 429


def test_a_failed_provider_call_still_spends_one_query(client, db, brave):
    attempt = running(client, db)
    brave.status = 503
    for i in range(3):
        assert call(client, attempt, request_id=f"req-{i}").status_code == 503
    assert call(client, attempt, request_id="req-9").status_code == 429


# --------------------------------------------------------------------------
# provider errors, and the key
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status,body,raises,code,http", [
    (401, b"", None, "search_provider_refused", 502),
    (403, b"", None, "search_provider_refused", 502),
    (429, b"", None, "search_provider_busy", 429),
    (422, b"", None, "search_query_rejected", 422),
    (500, b"", None, "search_provider_unavailable", 503),
    (200, b"not json", None, "search_provider_failed", 502),
    (200, b'{"web": {"results": "nope"}}', None, "search_provider_failed", 502),
    (200, b"", ProviderError("unavailable"), "search_provider_unavailable", 503),
])
def test_provider_errors_map_to_their_own_codes_never_409(client, db, brave, key, caplog,
                                                          status, body, raises, code, http):
    attempt = running(client, db)
    brave.status, brave.body, brave.raises = status, body, raises
    with caplog.at_level(logging.DEBUG):
        response = call(client, attempt)
    assert response.status_code == http != 409, response.text
    assert response.json()["code"] == code
    row, = audit_rows(db)
    assert row["code"] == code
    assert row["result"] == ("failed" if http >= 500 else "refused")
    assert key.value not in response.text
    assert key.value not in json.dumps(row, default=str)
    assert key.value not in caplog.text


def test_a_provider_that_echoes_the_key_never_reaches_the_answer(client, db, brave, key, caplog):
    attempt = running(client, db)
    brave.status = 401
    brave.body = json.dumps({"error": f"bad token {key.value}"}).encode()
    with caplog.at_level(logging.DEBUG):
        response = call(client, attempt)
    assert response.status_code == 502
    assert key.value not in response.text and key.value not in caplog.text
    assert key.value not in json.dumps(audit_rows(db), default=str)


def test_no_key_is_503_search_unconfigured_and_names_the_script(client, db, brave, key):
    attempt = running(client, db)
    key.fail = "swarm-search-api-key has no enabled version"
    response = call(client, attempt)
    assert response.status_code == 503
    assert response.json()["code"] == "search_unconfigured"
    assert "create-secrets.sh" in response.json()["message"]
    assert brave.calls == []


def test_a_success_answer_and_row_hold_no_key(client, db, brave, key, caplog):
    attempt = running(client, db)
    with caplog.at_level(logging.DEBUG):
        response = call(client, attempt)
    assert response.status_code == 200
    assert key.value not in response.text and key.value not in caplog.text
    assert key.value not in json.dumps(audit_rows(db), default=str)


def test_brave_drops_non_web_urls_and_strips_markup():
    raw = json.dumps({"web": {"results": [
        {"title": "a", "url": "javascript:alert(1)", "description": "x"},
        {"title": "b", "url": "ftp://example.com/f", "description": "x"},
        {"title": "<b>c</b>\n", "url": "https://example.com/c", "description": "&lt;d&gt;"},
    ]}}).encode()
    provider = BraveSearch(send=lambda *a: (200, raw))
    assert [r.as_dict() for r in provider.search("q", 10, key="k")] == [
        {"title": "c", "url": "https://example.com/c", "snippet": "<d>"}]


def test_brave_with_no_web_block_answers_no_results():
    provider = BraveSearch(send=lambda *a: (200, b'{"query": {"original": "q"}}'))
    assert provider.search("q", 5, key="k") == []


# --------------------------------------------------------------------------
# the audit's read
# --------------------------------------------------------------------------


def test_a_tenant_member_reads_the_tasks_search_calls(client, db):
    attempt = running(client, db)
    call(client, attempt, "first", request_id="req-1")
    call(client, attempt, "second", request_id="req-2")
    response = client.get(f"/v1/tasks/{attempt.tuple.task_id}/search-calls",
                          headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 2
    assert [c["query"] for c in body["calls"]] == ["second", "first"]


def test_another_tenants_member_cannot_read_them(client, db):
    attempt = running(client, db)
    call(client, attempt)
    response = client.get(f"/v1/tasks/{attempt.tuple.task_id}/search-calls",
                          headers=auth_header("bob"))
    assert response.status_code == 404
