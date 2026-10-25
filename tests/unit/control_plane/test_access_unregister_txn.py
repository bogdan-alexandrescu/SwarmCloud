"""SSC-UNREGISTER-TXN (#2158): the last access grant's registration goes the
way `DELETE /v1/repositories/{repo_id}` removes one.

secrets-scopes.md §2.1: "A removed registration disables every grant on its
secrets in the transaction that removes it." `AccessService._unregister_if_last`
removes a registration a grant made when the last grant on it goes (a revoke,
or disabling the owner of every grant), and it used to delete the registration
alone: the grants on the repository's secrets stayed enabled, and a later
registration of the same repository (the same `repo_id`) revived them. It now
removes it through `WorkspaceSecrets.delete_repository`, one transaction.

When that transaction fails, the access grant is already gone and nothing else
changes: the registration and its secrets' grants stay as they were, the answer
says `unregistered: false`, and a warning names the tenant, the repository and
the reason.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import access, forge, forgeapp, workspace_secrets
from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.auth import TENANT_HEADER, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.grant_attest import GrantKeys
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker
from swarm_api.workspace_secrets import AUDIT_REPOSITORY_GRANTS_DISABLED, GRANTS_COLLECTION

from .conftest import api_settings, auth_header, seed_tenant
from .repo_fakes import GitHubRepos, TenantTokens
from .test_access_api import ORG, AccessGitHub, _connect, _enable, _grant, _rid
from .test_forgeapp import CLIENT_ID, Clock, FakeAppSecret, FakeSlots
from .test_workspace_secrets import FakeSecretManager, RecordingFirestore, RecordingTransaction, fake_value

ROOT = {**auth_header("root"), TENANT_HEADER: "eng"}
GRANTED = f"{ORG}/repo-0001"
SECOND = f"{ORG}/repo-0002"


@pytest.fixture
def db() -> RecordingFirestore:
    return RecordingFirestore()


@pytest.fixture
def grant_key() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture
def github() -> AccessGitHub:
    return AccessGitHub()


@pytest.fixture
def sm() -> FakeSecretManager:
    return FakeSecretManager()


@pytest.fixture
def api(db, tokens, group_map, objects, github, sm, grant_key) -> TestClient:
    clock = Clock()
    seed_tenant(db, "eng", credentials=("git",))
    seed_tenant(db, "research", credentials=("git",))
    ctx = build_context(
        settings=api_settings(workspace_grant_key=grant_key), db=db,
        verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, forge_tokens=TenantTokens(),
        forge=forge.GitHubIssues(send=GitHubRepos(repos={}, listing=[])), now=clock,
    )
    forge_app = forgeapp.ForgeApp(
        db, config=forgeapp.AppConfig(client_id=CLIENT_ID, app_id="12345", slug="swarmcloud"),
        secrets=FakeAppSecret("s" + "x" * 39), slots=FakeSlots(), send=github, now=clock)
    service = access.AccessService(db, forge_app, send=github, now=clock)
    app = create_app(ctx, forge_app=forge_app, access_service=service)
    app.state.workspace_secret_writer = sm
    return TestClient(app, raise_server_exceptions=False)


def _secret_granted(api: TestClient, repository: str, name: str = "x-key",
                    consumers: tuple[str, ...] = ("profile:claude-code",)) -> None:
    """A repository secret on `repository`'s registration, granted to each of
    `consumers` by a manager, through the routes that write them."""
    base = f"/v1/repositories/{_rid(repository)}/secrets"
    created = api.post(base, headers=ROOT, json={
        "name": name, "provider": "other", "kind": "api_key", "value": fake_value()})
    assert created.status_code == 201, created.text
    for consumer in consumers:
        granted = api.put(f"{base}/{name}/grants/{consumer}", headers=ROOT,
                          json={"enabled": True})
        assert granted.status_code == 200, granted.text


def _secret_grants(db: RecordingFirestore, repository: str) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items()
            if p.startswith(f"{GRANTS_COLLECTION}/") and d.get("repo_id") == _rid(repository)]


def _granted_with_secrets(api: TestClient, github: AccessGitHub, db: RecordingFirestore,
                          repository: str = GRANTED) -> None:
    assert _grant(api, repository).status_code == 200
    assert db.docs[f"repositories/{_rid(repository)}"]["registered_via"] == "grant"
    _secret_granted(api, repository)
    _secret_granted(api, repository, name="y-key",
                    consumers=("profile:claude-code", "profile:codex"))
    assert len(_secret_grants(db, repository)) == 3
    assert all(g["enabled"] is True for g in _secret_grants(db, repository))


def _revoke(api: TestClient, repository: str, user: str = "alice"):
    return api.delete(f"/v1/access/grants/{_rid(repository)}", headers=auth_header(user))


# -- the registration goes with its secrets' grants, in one commit -----------


def test_revoking_the_last_grant_disables_the_secrets_grants_with_the_registration(
        api, github, db, grant_key):
    _connect(api, github)
    _enable(api)
    _granted_with_secrets(api, github, db)
    before = len(db.commits)
    gone = _revoke(api, GRANTED)
    assert gone.status_code == 200, gone.text
    assert gone.json()["unregistered"] is True and gone.json()["revoked"] is True
    assert f"repositories/{_rid(GRANTED)}" not in db.docs
    grants = _secret_grants(db, GRANTED)
    assert len(grants) == 3 and all(g["enabled"] is False for g in grants), grants
    assert all(GrantKeys(grant_key).check(g) == "valid" for g in grants)
    assert all(g["changed_by"] == "alice@example.com" for g in grants)
    # ONE commit removed the registration and disabled every grant.
    [commit] = [c for c in db.commits[before:] if f"repositories/{_rid(GRANTED)}" in c]
    assert {p for p in db.docs if p.startswith(f"{GRANTS_COLLECTION}/")
            and db.docs[p].get("repo_id") == _rid(GRANTED)} <= set(commit)
    rows = [d for p, d in db.docs.items() if p.startswith(f"{AUDIT_COLLECTION}/")
            and d.get("action") == AUDIT_REPOSITORY_GRANTS_DISABLED]
    assert len(rows) == 2
    assert all(r["detail"]["authority"] == "access_unregister" for r in rows)
    assert all(r["by"] == "alice@example.com" for r in rows)


def test_a_re_registration_after_the_revoke_revives_no_grant(api, github, db):
    _connect(api, github)
    _enable(api)
    _granted_with_secrets(api, github, db)
    assert _revoke(api, GRANTED).json()["unregistered"] is True
    assert _grant(api, GRANTED).status_code == 200  # the same repo_id again
    assert f"repositories/{_rid(GRANTED)}" in db.docs
    assert all(g["enabled"] is False for g in _secret_grants(db, GRANTED))


def test_disabling_an_owner_does_the_same_for_each_repository_it_held_last(api, github, db):
    _connect(api, github)
    _enable(api)
    _granted_with_secrets(api, github, db)
    _granted_with_secrets(api, github, db, repository=SECOND)
    answer = api.delete(f"/v1/access/orgs/{ORG}", headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    assert answer.json()["grants_deleted"] == 2
    assert sorted(answer.json()["unregistered"]) == sorted([_rid(GRANTED), _rid(SECOND)])
    for repository in (GRANTED, SECOND):
        assert f"repositories/{_rid(repository)}" not in db.docs
        grants = _secret_grants(db, repository)
        assert len(grants) == 3 and all(g["enabled"] is False for g in grants), grants


# -- what stays ------------------------------------------------------------------


def test_another_members_grant_keeps_the_registration_and_its_secrets_grants(
        api, github, db):
    _connect(api, github)
    _connect(api, github, user="root")
    _enable(api)
    _enable(api, user="root")
    _granted_with_secrets(api, github, db)
    assert _grant(api, GRANTED, user="root").status_code == 200
    gone = _revoke(api, GRANTED)
    assert gone.status_code == 200 and gone.json()["unregistered"] is False
    assert f"repositories/{_rid(GRANTED)}" in db.docs
    assert all(g["enabled"] is True for g in _secret_grants(db, GRANTED))


def test_a_registration_made_another_way_keeps_itself_and_its_secrets_grants(
        api, github, db):
    _connect(api, github)
    _enable(api)
    rid = _rid(GRANTED)
    db.docs[f"repositories/{rid}"] = {
        "repo_id": rid, "tenant_id": "eng", "forge": "github", "owner": ORG,
        "repo": "repo-0001", "repository_url": f"https://github.com/{GRANTED}"}
    assert _grant(api, GRANTED, mode="read").status_code == 200
    assert "registered_via" not in db.docs[f"repositories/{rid}"]
    _secret_granted(api, GRANTED)
    gone = _revoke(api, GRANTED)
    assert gone.status_code == 200 and gone.json()["unregistered"] is False
    assert f"repositories/{rid}" in db.docs
    [grant] = _secret_grants(db, GRANTED)
    assert grant["enabled"] is True


# -- the transaction failing ---------------------------------------------------


class _FailingRemoval(RecordingTransaction):
    """A commit that removes a registration fails, unapplied."""

    def _commit(self) -> list[Any]:
        if any(op == "delete" and ref.path.startswith("repositories/")
               for op, ref, _ in self._buffer):
            self._buffer = []
            raise RuntimeError("firestore unavailable")
        return super()._commit()


def _too_many_writes(db, monkeypatch) -> None:
    # The real over-500-writes refusal (a 409), reached with fewer secrets.
    monkeypatch.setattr(workspace_secrets, "MAX_TRANSACTION_WRITES", 2)


def _commit_fails(db, monkeypatch) -> None:
    monkeypatch.setattr(db, "transaction", lambda **kw: _FailingRemoval(db))


@pytest.mark.parametrize("failure,reason", [(_too_many_writes, "Conflict"),
                                            (_commit_fails, "RuntimeError")])
def test_a_failed_removal_keeps_the_registration_and_its_grants_and_says_so(
        api, github, db, monkeypatch, caplog, failure, reason):
    _connect(api, github)
    _enable(api)
    _granted_with_secrets(api, github, db)
    before = {p: dict(d) for p, d in db.docs.items() if p.startswith(f"{GRANTS_COLLECTION}/")}
    failure(db, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="swarm_api.access"):
        gone = _revoke(api, GRANTED)
    # The access grant is revoked; the registration was not removed, and said so.
    assert gone.status_code == 200, gone.text
    assert gone.json() == {"repo_id": _rid(GRANTED), "revoked": True, "unregistered": False,
                           "tenant_id": "eng"}
    assert not [p for p in db.docs if p.startswith(f"{forgeapp.GRANTS}/")]
    assert db.docs[f"repositories/{_rid(GRANTED)}"]["registered_via"] == "grant"
    assert {p: d for p, d in db.docs.items() if p.startswith(f"{GRANTS_COLLECTION}/")} == before
    assert not [d for p, d in db.docs.items() if p.startswith(f"{AUDIT_COLLECTION}/")
                and d.get("action") == AUDIT_REPOSITORY_GRANTS_DISABLED]
    [line] = [r.getMessage() for r in caplog.records
              if r.levelno == logging.WARNING and "access unregister failed" in r.getMessage()]
    assert f"tenant=eng repo_id={_rid(GRANTED)} reason={reason}" in line
    # The registration left is retried by the next grant's revoke.
    monkeypatch.undo()
    assert _grant(api, GRANTED).status_code == 200
    assert _revoke(api, GRANTED).json()["unregistered"] is True
    assert f"repositories/{_rid(GRANTED)}" not in db.docs
    assert all(g["enabled"] is False for g in _secret_grants(db, GRANTED))


def test_a_failed_removal_on_an_owners_disable_still_answers_what_was_done(
        api, github, db, monkeypatch, caplog):
    _connect(api, github)
    _enable(api)
    _granted_with_secrets(api, github, db)
    _commit_fails(db, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="swarm_api.access"):
        answer = api.delete(f"/v1/access/orgs/{ORG}", headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    assert answer.json()["grants_deleted"] == 1 and answer.json()["unregistered"] == []
    assert f"repositories/{_rid(GRANTED)}" in db.docs
    assert all(g["enabled"] is True for g in _secret_grants(db, GRANTED))
    assert f"repo_id={_rid(GRANTED)} reason=RuntimeError" in caplog.text
