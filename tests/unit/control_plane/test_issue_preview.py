"""`GET /v1/issues/preview?issue=<ref>`: the issue, read with the TENANT's forge token.

The owner picked mock-up 1A on 2026-10-02, accepting that swarm-api becomes a
second reader of `swarm-tenant-<tenant>-git`. What that acceptance costs is
held here:

  * the token is read for the caller's tenant and no other;
  * it goes to api.github.com in one header and nowhere else -- never a
    response, a log record or the text of an exception, on every path,
    failures included;
  * the body served is masked by the API's own redaction and bounded;
  * every way the read can fail has its own code.

No credentials, no network: the secret reader is a fake, and the forge is the
real client over a fake transport, so its status mapping and host pin are the
shipped code.
"""

from __future__ import annotations

import json
import logging
import secrets

import pytest
from fastapi.testclient import TestClient

from swarm_api import forge
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.validation import parse_issue_ref
from swarm_api.waker import NullWaker

from .conftest import api_settings, auth_header

#: Built at runtime: nothing token-shaped is a literal in this file.
TOKEN = "ghp_" + secrets.token_hex(18)

ISSUE = {
    "number": 42,
    "title": "Widgets cannot be sorted",
    "body": "Steps: open the list.\n",
    "state": "open",
    "comments": 3,
    "labels": [{"name": "bug"}, {"name": "ui"}],
    "html_url": "https://github.com/saga-xyz/widgets/issues/42",
}


class FakeTokens:
    def __init__(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens
        self.asked: list[str] = []

    def token_for(self, tenant) -> str:
        secret_id = tenant.secret_name(forge.GIT_PROVIDER)
        self.asked.append(secret_id)
        if secret_id not in self.tokens:
            raise forge.NoForgeCredential(
                f"tenant {tenant.tenant_id!r} has no forge credential ({secret_id})"
            )
        return self.tokens[secret_id]


class FakeForge:
    """The transport under the real `GitHubIssues`: records, answers as told."""

    def __init__(self, status: int = 200, body=None, raises: Exception | None = None) -> None:
        self.status = status
        self.body = ISSUE if body is None else body
        self.raises = raises
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float):
        self.calls.append((url, dict(headers)))
        if self.raises is not None:
            raise self.raises
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return self.status, raw


def _client(db, tokens, group_map, objects, *, forge_tokens, transport) -> TestClient:
    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=forge_tokens,
        forge=forge.GitHubIssues(send=transport),
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


@pytest.fixture
def make(db, tokens, group_map, objects):
    def build(transport=None, token_map=None):
        tokens_reader = FakeTokens(
            {"swarm-tenant-eng-git": TOKEN} if token_map is None else token_map
        )
        transport = transport or FakeForge()
        return (
            _client(db, tokens, group_map, objects, forge_tokens=tokens_reader, transport=transport),
            tokens_reader,
            transport,
        )
    return build


def _preview(client, ref="saga-xyz/widgets#42", user="alice"):
    return client.get("/v1/issues/preview", params={"issue": ref}, headers=auth_header(user))


def _assert_no_token(response, caplog, *extra: str) -> None:
    assert TOKEN not in response.text
    assert TOKEN not in caplog.text
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
        if record.exc_info:
            assert TOKEN not in logging.Formatter().formatException(record.exc_info)
    for text in extra:
        assert TOKEN not in text


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------

def test_the_preview_serves_the_issue_read_with_the_tenants_token(make, caplog):
    caplog.set_level(logging.DEBUG)
    client, reader, transport = make()
    response = _preview(client)
    assert response.status_code == 200, response.text
    issue = response.json()["issue"]
    assert issue["title"] == "Widgets cannot be sorted"
    assert issue["body"] == "Steps: open the list.\n"
    assert issue["body_truncated"] is False
    assert issue["labels"] == ["bug", "ui"]
    assert issue["state"] == "open"
    assert issue["comments"] == 3
    assert issue["url"] == "https://github.com/saga-xyz/widgets/issues/42"
    assert issue["ref"] == "saga-xyz/widgets#42"
    # The caller's tenant's secret, and only that one.
    assert reader.asked == ["swarm-tenant-eng-git"]
    # One request, to the pinned host, with the token in its header.
    [(url, headers)] = transport.calls
    assert url == "https://api.github.com/repos/saga-xyz/widgets/issues/42"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    _assert_no_token(response, caplog)


def test_the_preview_says_auto_merge_is_available_and_what_a_run_gets_by_default(make, db):
    # The submit form draws the auto-merge switch from this: available since
    # contract request 47 enabled the merge step, defaulting to the
    # platform's `merge_by_default` (owner decisions 2026-10-04, #295).
    client, _, _ = make()
    response = _preview(client)
    assert response.status_code == 200, response.text
    merge = response.json()["auto_merge"]
    assert merge["available"] is True
    assert merge["reason"] is None
    assert merge["default"] is False
    db.docs["control/settings"] = {"merge_by_default": True}
    assert _preview(client).json()["auto_merge"]["default"] is True


def test_the_previews_default_is_the_repositorys_merge_policy_first(make, db):
    """WF-MERGE-API: the form's default is what POST /v1/runs would record --
    the registered repository's `merge_policy`, else the platform default."""
    from swarm_api import repositories

    client, _, _ = make()
    repo_id = repositories.repo_id_for("eng", "saga-xyz", "widgets")
    db.docs["control/settings"] = {"merge_by_default": True}
    db.docs[f"repositories/{repo_id}"] = {
        "repo_id": repo_id, "tenant_id": "eng", "owner": "saga-xyz", "repo": "widgets",
        "merge_policy": "off",
    }
    assert _preview(client).json()["auto_merge"]["default"] is False
    db.docs["control/settings"] = {"merge_by_default": False}
    db.docs[f"repositories/{repo_id}"]["merge_policy"] = "on_merge_verdict"
    assert _preview(client).json()["auto_merge"]["default"] is True


def test_an_issue_url_previews_the_same_issue(make):
    client, _, transport = make()
    response = _preview(client, "https://github.com/saga-xyz/widgets/issues/42")
    assert response.status_code == 200
    assert transport.calls[0][0].endswith("/repos/saga-xyz/widgets/issues/42")


def test_the_body_is_masked_by_the_apis_redaction(make, caplog):
    leaked = "ghp_" + secrets.token_hex(18)
    body = {**ISSUE, "body": f"repro: export GITHUB_TOKEN={leaked}\nthen run it", "title": f"see {leaked}"}
    client, _, _ = make(FakeForge(body=body))
    response = _preview(client)
    assert response.status_code == 200
    issue = response.json()["issue"]
    assert leaked not in response.text
    assert issue["body_redacted"] is True
    assert "then run it" in issue["body"]
    assert leaked not in issue["title"]


def test_the_tenants_own_token_in_an_issue_body_is_masked_too(make, caplog):
    body = {**ISSUE, "body": f"someone pasted {TOKEN} here"}
    client, _, _ = make(FakeForge(body=body))
    response = _preview(client)
    assert response.status_code == 200
    _assert_no_token(response, caplog)


def test_a_long_body_is_truncated_and_says_so(make):
    body = {**ISSUE, "body": "x" * (forge.MAX_PREVIEW_BODY_CHARS + 500)}
    client, _, _ = make(FakeForge(body=body))
    issue = _preview(client).json()["issue"]
    assert issue["body_truncated"] is True
    assert len(issue["body"]) <= forge.MAX_PREVIEW_BODY_CHARS + 1


def test_a_closed_issue_is_served_with_state_closed(make):
    client, _, _ = make(FakeForge(body={**ISSUE, "state": "closed"}))
    response = _preview(client)
    assert response.status_code == 200
    assert response.json()["issue"]["state"] == "closed"


def test_an_issue_with_no_body_serves_an_empty_one(make):
    client, _, _ = make(FakeForge(body={**ISSUE, "body": None}))
    assert _preview(client).json()["issue"]["body"] == ""


# --------------------------------------------------------------------------
# every failure, its own code, and never the token
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,code,http",
    [
        (404, "not_found", 404),
        (410, "not_found", 404),
        (403, "no_access", 403),
        (401, "no_access", 403),
        (500, "read_failed", 502),
        (302, "read_failed", 502),
    ],
)
def test_each_forge_answer_has_its_own_code(make, caplog, status, code, http):
    caplog.set_level(logging.DEBUG)
    client, _, _ = make(FakeForge(status=status, body={"message": "nope"}))
    response = _preview(client)
    assert response.status_code == http, response.text
    assert response.json()["code"] == code
    _assert_no_token(response, caplog)


def test_not_found_is_worded_not_found_or_not_visible(make):
    client, _, _ = make(FakeForge(status=404, body={"message": "Not Found"}))
    assert "not found or not visible" in _preview(client).json()["message"]


def test_a_pull_request_from_the_forge_is_is_pull_request(make, caplog):
    body = {**ISSUE, "pull_request": {"url": "https://api.github.com/repos/saga-xyz/widgets/pulls/42"}}
    client, _, _ = make(FakeForge(body=body))
    response = _preview(client)
    assert response.status_code == 422
    assert response.json()["code"] == "is_pull_request"
    _assert_no_token(response, caplog)


def test_a_pull_request_url_is_is_pull_request_without_reading_anything(make):
    client, reader, transport = make()
    response = _preview(client, "https://github.com/saga-xyz/widgets/pull/42")
    assert response.status_code == 422
    assert response.json()["code"] == "is_pull_request"
    assert reader.asked == [] and transport.calls == []


def test_a_tenant_with_no_git_secret_is_no_forge_credential(make, caplog):
    client, reader, transport = make(token_map={})
    response = _preview(client)
    assert response.json()["code"] == "no_forge_credential"
    assert response.status_code == 409
    assert transport.calls == [], "nothing is sent to the forge without a credential"
    _assert_no_token(response, caplog)


def test_another_tenant_reads_its_own_secret_and_never_engs(make):
    client, reader, transport = make()
    response = _preview(client, user="bob")
    assert response.json()["code"] == "no_forge_credential"
    assert reader.asked == ["swarm-tenant-research-git"]
    assert transport.calls == []


def test_a_transport_failure_carrying_the_token_is_read_failed_without_it(make, caplog):
    caplog.set_level(logging.DEBUG)
    transport = FakeForge(raises=OSError(f"connection reset while sending Bearer {TOKEN}"))
    client, _, _ = make(transport)
    response = _preview(client)
    assert response.status_code == 502
    assert response.json()["code"] == "read_failed"
    _assert_no_token(response, caplog)


def test_an_unparseable_answer_is_read_failed(make, caplog):
    client, _, _ = make(FakeForge(body=b"<html>"))
    response = _preview(client)
    assert response.json()["code"] == "read_failed"
    _assert_no_token(response, caplog)


def test_a_malformed_reference_is_a_422(make):
    client, reader, _ = make()
    response = _preview(client, "not-a-ref")
    assert response.status_code == 422
    assert reader.asked == []


def test_no_exception_the_client_raises_carries_the_token():
    """The client's own errors, outside any route: the text of every one."""
    ref = parse_issue_ref("saga-xyz/widgets#42")
    cases = [
        FakeForge(status=404, body={"message": TOKEN}),
        FakeForge(status=403, body={"message": TOKEN}),
        FakeForge(status=500, body={"message": TOKEN}),
        FakeForge(body={**ISSUE, "pull_request": {}}),
        FakeForge(body=TOKEN.encode()),
        FakeForge(raises=RuntimeError(TOKEN)),
    ]
    for transport in cases:
        with pytest.raises(forge.ForgeReadError) as raised:
            forge.GitHubIssues(send=transport).fetch(ref, TOKEN)
        assert TOKEN not in str(raised.value)
        assert TOKEN not in json.dumps(raised.value.to_payload())
        # Nothing chains back to the text that held it, not even a traceback.
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None or raised.value.__suppress_context__


# --------------------------------------------------------------------------
# the production pieces: pinned host, no redirects, the secret path
# --------------------------------------------------------------------------

def test_the_client_refuses_to_send_the_token_to_any_other_host():
    with pytest.raises(forge.IssueReadFailed):
        forge._urllib_send("https://example.com/repos/a/b/issues/1", {"Authorization": "x"}, 1.0)


def test_the_production_opener_never_follows_a_redirect():
    import urllib.request

    handler = forge._NoRedirects()
    request = urllib.request.Request("https://api.github.com/repos/a/b/issues/1")
    assert handler.redirect_request(request, None, 301, "Moved", {}, "https://evil.example/") is None


class _Payload:
    def __init__(self, data: bytes) -> None:
        self.data = data


class _Version:
    def __init__(self, data: bytes) -> None:
        self.payload = _Payload(data)


class FakeSecretClient:
    def __init__(self, data: bytes | None = None, error: Exception | None = None) -> None:
        self.data, self.error, self.names = data, error, []

    def access_secret_version(self, request):
        self.names.append(request["name"])
        if self.error is not None:
            raise self.error
        return _Version(self.data)


def _tenant(tenant_id="eng"):
    from datetime import datetime, timezone

    from swarm_common.models import Tenant

    return Tenant(tenant_id=tenant_id, kind="group", principal=f"{tenant_id}@saga.xyz",
                  created_at=datetime.now(timezone.utc))


def test_the_secret_reader_reads_the_tenants_git_secret_latest_version():
    client = FakeSecretClient(data=(TOKEN + "\n").encode())
    reader = forge.SecretManagerForgeTokens("saga-agents-staging", client=client)
    assert reader.token_for(_tenant()) == TOKEN
    assert client.names == ["projects/saga-agents-staging/secrets/swarm-tenant-eng-git/versions/latest"]


def test_a_missing_secret_is_no_forge_credential():
    from google.api_core import exceptions as gexc

    reader = forge.SecretManagerForgeTokens(
        "saga-agents-staging", client=FakeSecretClient(error=gexc.NotFound("no such secret"))
    )
    with pytest.raises(forge.NoForgeCredential) as raised:
        reader.token_for(_tenant())
    assert raised.value.code == "no_forge_credential"


def test_an_empty_secret_is_no_forge_credential():
    reader = forge.SecretManagerForgeTokens("p", client=FakeSecretClient(data=b"  \n"))
    with pytest.raises(forge.NoForgeCredential):
        reader.token_for(_tenant())


def test_a_secret_manager_refusal_is_read_failed_and_names_no_payload():
    from google.api_core import exceptions as gexc

    reader = forge.SecretManagerForgeTokens(
        "p", client=FakeSecretClient(error=gexc.PermissionDenied(f"denied {TOKEN}"))
    )
    with pytest.raises(forge.IssueReadFailed) as raised:
        reader.token_for(_tenant())
    assert TOKEN not in str(raised.value)
