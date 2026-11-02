"""`GET /v1/admin/setup/github-app/permissions`: the GitHub App and its
installation, read as the App by swarm-api for `swarm upgrade`, so the
installer never reads a secret's value (lane DAYONE-UPGRADE-APP-VIA-API,
part of #2688).

The answer names the App's slug, owner and permissions and the installation's
id, account and permissions, and nothing else: no token, key, JWT or secret
field (invariant 10). With no App yet, or a key that cannot sign yet, it
answers a state, never a 500. A platform admin's route only.
"""

from __future__ import annotations

import json
from typing import Any

from swarm_api.errors import ApiError
from swarm_api.forgeapp import AppNotConfigured, HttpAnswer
from swarm_api.setup_github import GitHubSetupApi

from .conftest import auth_header
from .test_setup_routes import (  # noqa: F401 - fixtures
    ENV,
    ORG,
    db,
    directory,
    fake_secrets,
    github,
    group_map,
    pool,
    setup_app,
    setup_client,
    tokens,
)

ROUTE = "/v1/admin/setup/github-app/permissions"
GITHUB_APP_DOC = f"swarm-install/{ENV}/console/github_app"

APP = {"id": 4242, "slug": "swarmcloud-test", "node_id": "A_1", "client_id": "Iv1.example",
       "owner": {"login": ORG, "type": "Organization"},
       "permissions": {"contents": "write", "metadata": "read", "statuses": "read"}}


class AppGitHub:
    """GitHub's `GET /app`, `/app/installations` and `/app/installations/{id}`,
    every request recorded with its headers."""

    def __init__(self, installations: list[dict[str, Any]], status: int = 200) -> None:
        self.installations = installations
        self.status = status
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def __call__(self, method, url, headers, body, timeout) -> HttpAnswer:
        self.requests.append((method, url, dict(headers)))
        if self.status != 200:
            return HttpAnswer(self.status, {}, b'{"message": "Bad credentials"}')
        path = url.removeprefix("https://api.github.com")
        if path == "/app":
            return HttpAnswer(200, {}, json.dumps(APP).encode())
        if path == "/app/installations":
            return HttpAnswer(200, {}, json.dumps(self.installations).encode())
        for found in self.installations:
            if path == f"/app/installations/{found['id']}":
                # GitHub's installation answer also carries URLs and tokens' URLs: never repeated.
                return HttpAnswer(200, {}, json.dumps({**found, "access_tokens_url": url + "/access_tokens",
                                                       "repository_selection": "all"}).encode())
        return HttpAnswer(404, {}, b"{}")


def _use(setup_app, send: AppGitHub) -> AppGitHub:
    setup_app.state.setup_github_api = GitHubSetupApi(send=send)
    return send


def test_the_answer_is_the_app_and_its_installation_and_nothing_else(setup_app, setup_client, db) -> None:
    db.docs[GITHUB_APP_DOC] = {"id": 4242, "slug": "swarmcloud-test", "installed_on": ORG}
    send = _use(setup_app, AppGitHub([
        {"id": 7, "account": {"login": "someone-else", "type": "User"}, "permissions": {}},
        {"id": 9001, "account": {"login": ORG.upper(), "type": "Organization"},
         "permissions": {"contents": "write", "metadata": "read"}}]))
    answer = setup_client.get(ROUTE, headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body == {
        "state": "read",
        "app": {"slug": "swarmcloud-test", "owner": ORG, "owner_is_org": True,
                "permissions": {"contents": "write", "metadata": "read", "statuses": "read"}},
        "installation": {"id": 9001, "account": ORG.upper(), "account_is_org": True,
                         "permissions": {"contents": "write", "metadata": "read"}},
    }
    # Exactly these keys: no token, key, JWT, client id or URL rides along.
    assert set(body) == {"state", "app", "installation"}
    assert set(body["app"]) == {"slug", "owner", "owner_is_org", "permissions"}
    assert set(body["installation"]) == {"id", "account", "account_is_org", "permissions"}
    assert [(m, u.removeprefix("https://api.github.com")) for m, u, _ in send.requests] == [
        ("GET", "/app"), ("GET", "/app/installations"), ("GET", "/app/installations/9001")]
    for _, _, headers in send.requests:
        scheme, jwt = headers["Authorization"].split(" ", 1)
        assert scheme == "Bearer" and jwt.count(".") == 2, "signed as the App"
        assert jwt not in answer.text


def test_without_installed_on_the_org_then_the_only_installation(setup_app, setup_client, db) -> None:
    db.docs[GITHUB_APP_DOC] = {"id": 4242, "slug": "swarmcloud-test"}
    _use(setup_app, AppGitHub([{"id": 5, "account": {"login": "a-person", "type": "User"},
                                "permissions": {"contents": "read"}}]))
    body = setup_client.get(ROUTE, headers=auth_header("root")).json()
    assert body["installation"] == {"id": 5, "account": "a-person", "account_is_org": False,
                                    "permissions": {"contents": "read"}}

    _use(setup_app, AppGitHub([{"id": 5, "account": {"login": "a-person", "type": "User"}},
                               {"id": 6, "account": {"login": ORG, "type": "Organization"},
                                "permissions": {"issues": "write"}}]))
    body = setup_client.get(ROUTE, headers=auth_header("root")).json()
    assert body["installation"]["id"] == 6, "SETUP_GITHUB_ORG's installation"

    _use(setup_app, AppGitHub([]))
    body = setup_client.get(ROUTE, headers=auth_header("root")).json()
    assert body["state"] == "read" and body["app"]["slug"] == "swarmcloud-test" and body["installation"] is None


def test_no_app_yet_is_a_state_not_an_error(setup_app, setup_client, db) -> None:
    send = _use(setup_app, AppGitHub([]))
    answer = setup_client.get(ROUTE, headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"state": "no_app", "app": None, "installation": None}
    assert send.requests == [], "GitHub is not asked"


def test_a_key_that_cannot_sign_yet_is_a_state_not_an_error(setup_app, setup_client, db, fake_secrets,
                                                            monkeypatch) -> None:
    db.docs[GITHUB_APP_DOC] = {"id": 4242, "slug": "swarmcloud-test"}
    send = _use(setup_app, AppGitHub([]))

    def missing() -> str:
        raise AppNotConfigured("swarm-github-app-private-key has no enabled version")

    monkeypatch.setattr(fake_secrets, "private_key", missing)
    answer = setup_client.get(ROUTE, headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"state": "key_unavailable", "app": None, "installation": None}
    assert send.requests == []


def test_a_github_refusal_is_said_with_its_status_only(setup_app, setup_client, db) -> None:
    db.docs[GITHUB_APP_DOC] = {"id": 4242, "slug": "swarmcloud-test"}
    _use(setup_app, AppGitHub([], status=401))
    answer = setup_client.get(ROUTE, headers=auth_header("root"))
    assert answer.status_code != 500 and answer.status_code >= 400, answer.text
    assert "GitHub answered 401" in answer.text and "Bad credentials" not in answer.text
    assert issubclass(AppNotConfigured, ApiError)


def test_platform_admin_only(setup_app, setup_client, db) -> None:
    db.docs[GITHUB_APP_DOC] = {"id": 4242, "slug": "swarmcloud-test"}
    send = _use(setup_app, AppGitHub([]))
    for user in ("wadmin", "alice", "carol", "deployer"):
        assert setup_client.get(ROUTE, headers=auth_header(user)).status_code == 403, user
    assert send.requests == []
