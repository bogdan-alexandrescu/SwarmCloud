"""Whom GitHub calls admin of a repository, asked live with their own token
(SSC-REPO-SCOPE, part of #2158).

Design: docs/design/secrets-scopes.md §2.5 and §3.6, owner decisions Q2 and Q6
of its §11. A repository's secrets for agents may be set by a workspace admin,
a platform admin, or a member GitHub calls ADMIN of that repository, checked
at each write with that person's OWN GitHub user token
(`swarm_api.github_repo_admin`), never from a stored role and never with the
App's installation token. Every failure to ask is a refusal.

The attackers these tests stand in for:

  * A MEMBER WITH WRITE ON GITHUB, not admin: refused, and nothing is stored.
  * A MEMBER WITH NO GITHUB CONNECTION, or an expired one: refused with a
    reason, not let through on the workspace's own forge token.
  * GITHUB FAILING (5xx, a transport error, a 404): refused, never guessed.
  * A CODE PATH THAT ASKS WITH THE WRONG TOKEN: the fake GitHub records the
    Authorization of every request, and the installation (tenant) token is a
    value it must never see.

Offline: FakeFirestore, a fake GitHub transport under the real
`forge.GitHubIssues`, a fake connection store, and the workspace-secrets
Secret Manager stand-in. Every token is built at runtime.
"""

from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from swarm_api.access import AccessRefused, NotConnected
from swarm_api.auth import TENANT_HEADER, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.forge import GitHubIssues
from swarm_api.forgeapp import Caller
from swarm_api.github_repo_admin import (
    ADMIN,
    NOT_ADMIN,
    NOT_CONNECTED,
    NOT_VISIBLE,
    TOKEN_EXPIRED,
    UNAVAILABLE,
    check,
    require_repo_admin,
)
from swarm_api.errors import ApiError
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.repositories import SCOPE_USER, HeldCredential, repo_id_for
from swarm_api.waker import NullWaker
from swarm_api.workspace_secrets import SECRETS_COLLECTION

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, api_settings, auth_header, seed_tenant
from .test_workspace_secrets import FakeSecretManager, fake_value

DAVE = "dave@example.com"
REPO_ID = repo_id_for("eng", "acme", "widgets")
BASE = f"/v1/repositories/{REPO_ID}/secrets"


def runtime_token() -> str:
    return secrets.token_hex(20)


class FakeGitHub:
    """`GET /repos/{owner}/{repo}` as GitHub answers it, recording every
    request's URL and Authorization header."""

    def __init__(self, *, status: int = 200, admin: bool = True, push: bool = True,
                 fail: Exception | None = None) -> None:
        self.status = status
        self.admin = admin
        self.push = push
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float):
        self.calls.append((url, headers.get("Authorization", "")))
        if self.fail is not None:
            raise self.fail
        body = {"full_name": "acme/widgets", "default_branch": "main", "private": True,
                "permissions": {"admin": self.admin, "push": self.push, "pull": True}}
        raw = json.dumps(body).encode() if self.status == 200 else b'{"message": "nope"}'
        return self.status, raw, {}


class FakeAccess:
    """`AccessService.user_token`: the person's own connection, or its failure."""

    def __init__(self, *, token: str | None = None, error: Exception | None = None,
                 scope: str = SCOPE_USER) -> None:
        self.token = token if token is not None else runtime_token()
        self.error = error
        self.scope = scope
        self.callers: list[Caller] = []

    @contextmanager
    def user_token(self, caller: Caller) -> Iterator[HeldCredential]:
        self.callers.append(caller)
        if self.error is not None:
            raise self.error
        yield HeldCredential(self.scope, "swarm-tenant-eng-git-u-0123456789abcdef", self.token,
                             login="dave-gh")


class InstallationTokens:
    """The tenant's forge token (an App installation token, in production).
    Its value is one the fake GitHub must never be shown."""

    def __init__(self) -> None:
        self.value = runtime_token()
        self.reads = 0

    def token_for(self, tenant: Any, *, person: str | None = None) -> str:
        self.reads += 1
        return self.value


CALLER = Caller(email=DAVE, tenant_id="eng")


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------


def test_an_admin_is_admin_and_asked_with_their_own_token():
    github, access = FakeGitHub(admin=True), FakeAccess()
    answer = check(access, GitHubIssues(send=github), CALLER, "acme", "widgets")
    assert answer.admin and answer.reason == ADMIN and answer.login == "dave-gh"
    assert access.callers == [CALLER]
    assert github.calls == [("https://api.github.com/repos/acme/widgets",
                             f"Bearer {access.token}")]


@pytest.mark.parametrize("github, reason", [
    (FakeGitHub(admin=False, push=True), NOT_ADMIN),
    (FakeGitHub(status=404), NOT_VISIBLE),
    (FakeGitHub(status=401), TOKEN_EXPIRED),
    (FakeGitHub(status=502), UNAVAILABLE),
    (FakeGitHub(status=503), UNAVAILABLE),
    (FakeGitHub(fail=OSError("connection reset")), UNAVAILABLE),
])
def test_every_answer_but_admin_is_a_no_with_its_reason(github, reason):
    access = FakeAccess()
    answer = check(access, GitHubIssues(send=github), CALLER, "acme", "widgets")
    assert not answer.admin and answer.reason == reason and answer.message
    assert access.token not in answer.message


@pytest.mark.parametrize("error, reason", [
    (NotConnected(), NOT_CONNECTED),
    (AccessRefused("REFRESH_FAILED", "your GitHub authorisation has expired"), TOKEN_EXPIRED),
    (RuntimeError("slot unreadable"), UNAVAILABLE),
])
def test_no_usable_connection_is_a_no_and_github_is_never_asked(error, reason):
    github = FakeGitHub(admin=True)
    answer = check(FakeAccess(error=error), GitHubIssues(send=github), CALLER, "acme", "widgets")
    assert (answer.admin, answer.reason) == (False, reason)
    assert github.calls == []


def test_a_credential_that_is_not_the_persons_own_is_never_sent():
    """Only a user credential answers for a person: a tenant-scoped one (the
    installation's reach) is refused before GitHub is asked."""
    github = FakeGitHub(admin=True)
    answer = check(FakeAccess(scope="tenant"), GitHubIssues(send=github), CALLER, "acme",
                   "widgets")
    assert not answer.admin and github.calls == []


def test_require_raises_forbidden_while_its_switch_is_off_and_its_own_code_on(monkeypatch):
    github = FakeGitHub(admin=False)
    monkeypatch.delenv("REFUSAL_GITHUB_ADMIN_REQUIRED", raising=False)
    with pytest.raises(ApiError) as off:
        require_repo_admin(FakeAccess(), GitHubIssues(send=github), CALLER, "acme", "widgets")
    assert (off.value.status_code, off.value.code) == (403, "forbidden")
    assert off.value.detail == {"reason": NOT_ADMIN, "repository": "acme/widgets"}
    monkeypatch.setenv("REFUSAL_GITHUB_ADMIN_REQUIRED", "on")
    with pytest.raises(ApiError) as on:
        require_repo_admin(FakeAccess(), GitHubIssues(send=github), CALLER, "acme", "widgets")
    assert (on.value.status_code, on.value.code) == (403, "github_admin_required")


# --------------------------------------------------------------------------
# through the routes (Q2)
# --------------------------------------------------------------------------


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    """dave a member of eng, bob of research, root an admin."""
    return {
        DAVE: (ENG_GROUP,),
        "bob@example.com": (RESEARCH_GROUP,),
        "root@example.com": (ADMIN_GROUP, ENG_GROUP),
    }


@pytest.fixture
def world(db, tokens, group_map, objects):
    """eng with acme/widgets registered. Answers a client factory taking the
    fake GitHub and, optionally, the fake connection store (None: the real
    AccessService, under which nobody has connected GitHub)."""
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    db.docs[f"repositories/{REPO_ID}"] = {
        "repo_id": REPO_ID, "tenant_id": "eng", "forge": "github", "owner": "acme",
        "repo": "widgets", "repository_url": "https://github.com/acme/widgets"}
    sm = FakeSecretManager()
    installation = InstallationTokens()

    def make(github: FakeGitHub, access: FakeAccess | None = None) -> TestClient:
        ctx = build_context(
            settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
            groups=StaticGroups(group_map), credentials=InMemoryCredentials(),
            waker=NullWaker(), metrics=ApiMetrics(), objects=objects,
            forge_tokens=installation, forge=GitHubIssues(send=github),
        )
        app = create_app(ctx)
        app.state.workspace_secret_writer = sm
        if access is not None:
            app.state.access = access
        return TestClient(app, raise_server_exceptions=False)

    make.sm = sm  # type: ignore[attr-defined]
    make.installation = installation  # type: ignore[attr-defined]
    return make


def as_(who: str, tenant: str = "eng") -> dict[str, str]:
    return {**auth_header(who), TENANT_HEADER: tenant}


def create(client: TestClient, who: str, value: str, name: str = "widgets-db"):
    return client.post(BASE, headers=as_(who),
                       json={"name": name, "provider": "other", "kind": "api_key",
                             "value": value})


def stored(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith(f"{SECRETS_COLLECTION}/")]


def test_a_member_github_calls_admin_may_set_the_repositorys_secret(world, db):
    github, access = FakeGitHub(admin=True), FakeAccess()
    client = world(github, access)
    value = fake_value()
    response = create(client, "dave", value)
    assert response.status_code == 201, response.text
    assert response.json()["authority"] == "github_repo_admin"
    assert response.json()["secret"]["repo_id"] == REPO_ID
    assert value not in response.text
    [record] = stored(db)
    assert record["scope"] == "repository" and record["created_by"] == DAVE
    audit = [d for p, d in db.docs.items() if p.startswith("admin_audit/")]
    assert [a["detail"]["authority"] for a in audit] == ["github_repo_admin"]


def test_a_member_github_calls_write_may_not(world, db):
    github = FakeGitHub(admin=False, push=True)
    client = world(github, FakeAccess())
    response = create(client, "dave", fake_value())
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["reason"] == NOT_ADMIN
    assert stored(db) == [] and world.sm.payloads == {}  # type: ignore[attr-defined]


def test_a_member_with_no_forge_connection_is_refused(world, db):
    """The real AccessService: dave never connected GitHub. Refused with the
    reason, and the workspace's own forge token is not asked in his place."""
    github = FakeGitHub(admin=True)
    client = world(github)
    response = create(client, "dave", fake_value())
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["reason"] == NOT_CONNECTED
    assert github.calls == [] and stored(db) == []
    assert world.installation.reads == 0  # type: ignore[attr-defined]


def test_a_github_5xx_is_refused(world, db):
    client = world(FakeGitHub(status=502), FakeAccess())
    response = create(client, "dave", fake_value())
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["reason"] == UNAVAILABLE
    assert stored(db) == []


def test_the_admin_check_uses_the_persons_token_never_the_installation_token(world, db):
    github, access = FakeGitHub(admin=True), FakeAccess()
    client = world(github, access)
    assert create(client, "dave", fake_value()).status_code == 201
    assert github.calls, "GitHub was never asked"
    seen = {authorization for _url, authorization in github.calls}
    assert seen == {f"Bearer {access.token}"}
    installation = world.installation.value  # type: ignore[attr-defined]
    assert all(installation not in authorization for authorization in seen)
    assert world.installation.reads == 0  # type: ignore[attr-defined]
    assert access.callers == [Caller(email=DAVE, tenant_id="eng")]


def test_github_is_asked_again_at_every_write(world, db):
    """Live, never remembered: an admin demoted on GitHub is refused the next write."""
    github, access = FakeGitHub(admin=True), FakeAccess()
    client = world(github, access)
    assert create(client, "dave", fake_value()).status_code == 201
    github.admin = False
    response = client.put(f"{BASE}/widgets-db/value", headers=as_("dave"),
                          json={"value": fake_value("z")})
    assert response.status_code == 403, response.text
    assert len(github.calls) == 2


def test_a_workspace_admin_needs_no_github_answer(world, db):
    github = FakeGitHub(admin=False)
    client = world(github, FakeAccess())
    response = create(client, "root", fake_value())
    assert response.status_code == 201, response.text
    assert response.json()["authority"] == "workspace_admin"
    assert github.calls == []
