"""A run keeps what the issue read at submission said (#454; lane U9 item 4).

The run page's "Read from the issue" card printed title, labels, body and
comments as "not served by the run", although the console had previewed them
seconds before the run was created. The run now stores, at submission, the
same read the preview serves -- `forge.preview`, so the same masking and the
same body bound -- and serves it as `issue_read`:

  * title, labels, state, comment count, the canonical URL, and the body
    truncated to the preview's length and masked as the preview masks it;
  * read with the caller's TENANT's forge token, which is never stored,
    served or logged;
  * a read that fails does not refuse the run (the planner reads the issue
    itself, on the worker): the run is created with `issue_read: null` and
    `issue_read_error` saying why;
  * a run created before this change serves both as null, and the console
    keeps its dash and reason for exactly that case.

No credentials, no network: the token reader and the transport are fakes, and
the forge client over them is the shipped one.
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
from swarm_api.waker import NullWaker

from .conftest import api_settings, auth_header

#: Built at runtime: nothing token-shaped is a literal in this file.
TOKEN = "ghp_" + secrets.token_hex(18)
REF = "saga-xyz/widgets#42"

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
    def __init__(self, status: int = 200, body=None) -> None:
        self.status = status
        self.body = ISSUE if body is None else body
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float):
        self.calls.append(url)
        return self.status, json.dumps(self.body).encode()


@pytest.fixture
def make(db, tokens, group_map, objects):
    def build(transport=None, token_map=None):
        reader = FakeTokens({"swarm-tenant-eng-git": TOKEN} if token_map is None else token_map)
        transport = transport or FakeForge()
        ctx = build_context(
            settings=api_settings(),
            db=db,
            verifier=StaticTokenVerifier(tokens),
            groups=StaticGroups(group_map),
            credentials=InMemoryCredentials(),
            waker=NullWaker(),
            metrics=ApiMetrics(),
            objects=objects,
            forge_tokens=reader,
            forge=forge.GitHubIssues(send=transport),
        )
        return TestClient(create_app(ctx), raise_server_exceptions=False), reader, transport
    return build


def _create(client, user="alice"):
    return client.post("/v1/runs", headers=auth_header(user), json={"issue": REF})


def _stored(db, run_id: str) -> dict:
    return db.docs[f"issue_runs/{run_id}"]


def test_a_run_stores_and_serves_what_the_issue_said_at_submission(make, db, caplog):
    caplog.set_level(logging.DEBUG)
    client, reader, transport = make()
    created = _create(client)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    read = run["issue_read"]
    assert read["title"] == "Widgets cannot be sorted"
    assert read["labels"] == ["bug", "ui"]
    assert read["state"] == "open"
    assert read["comments"] == 3
    assert read["url"] == "https://github.com/saga-xyz/widgets/issues/42"
    assert read["body"] == "Steps: open the list.\n"
    assert read["body_truncated"] is False
    assert read["body_redacted"] is False
    assert read["read_at"]
    assert run["issue_read_error"] is None
    # The caller's tenant's secret, one read of the issue, at the pinned host.
    assert reader.asked == ["swarm-tenant-eng-git"]
    assert transport.calls == ["https://api.github.com/repos/saga-xyz/widgets/issues/42"]
    # It is on the run document, and every later read serves it.
    assert _stored(db, run["id"])["issue_read"]["title"] == "Widgets cannot be sorted"
    again = client.get(f"/v1/runs/{run['id']}", headers=auth_header("alice")).json()["run"]
    assert again["issue_read"] == read
    listed = client.get("/v1/runs", headers=auth_header("alice")).json()["runs"]
    assert listed[0]["issue_read"]["title"] == "Widgets cannot be sorted"
    # The token: not stored, not served, not logged.
    assert TOKEN not in json.dumps(_stored(db, run["id"]), default=str)
    assert TOKEN not in created.text and TOKEN not in caplog.text


def test_the_stored_body_is_masked_and_bounded_as_the_preview_is(make, db):
    leaked = "ghp_" + secrets.token_hex(18)
    body = {
        **ISSUE,
        "title": f"see {leaked}",
        "body": f"repro: export GITHUB_TOKEN={leaked}\n" + "x" * forge.MAX_PREVIEW_BODY_CHARS,
    }
    client, _, _ = make(FakeForge(body=body))
    created = _create(client)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    read = run["issue_read"]
    assert leaked not in created.text
    assert leaked not in json.dumps(_stored(db, run["id"]), default=str)
    assert read["body_redacted"] is True
    assert read["body_truncated"] is True
    assert len(read["body"]) <= forge.MAX_PREVIEW_BODY_CHARS + 1


def test_the_tenants_own_token_pasted_into_the_issue_is_masked_in_the_stored_read(make, db):
    client, _, _ = make(FakeForge(body={**ISSUE, "body": f"someone pasted {TOKEN} here"}))
    created = _create(client)
    assert created.status_code == 201, created.text
    assert TOKEN not in created.text
    assert TOKEN not in json.dumps(_stored(db, created.json()["run"]["id"]), default=str)


@pytest.mark.parametrize(
    ("transport", "token_map", "code"),
    [
        (None, {}, "no_forge_credential"),
        (FakeForge(status=404), None, "not_found"),
        (FakeForge(status=403), None, "no_access"),
        (FakeForge(status=502), None, "read_failed"),
    ],
)
def test_a_failed_read_creates_the_run_and_says_why(make, db, transport, token_map, code):
    client, _, _ = make(transport, token_map)
    created = _create(client)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    assert run["state"] == "PLANNING"
    assert run["issue_read"] is None
    assert run["issue_read_error"]["code"] == code
    assert run["issue_read_error"]["message"]
    assert TOKEN not in created.text


def test_a_run_created_before_runs_kept_the_read_serves_none(make, db):
    client, _, _ = make()
    run = _create(client).json()["run"]
    doc = _stored(db, run["id"])
    del doc["issue_read"]
    del doc["issue_read_error"]
    again = client.get(f"/v1/runs/{run['id']}", headers=auth_header("alice"))
    assert again.status_code == 200, again.text
    served = again.json()["run"]
    assert served["issue_read"] is None
    assert served["issue_read_error"] is None
