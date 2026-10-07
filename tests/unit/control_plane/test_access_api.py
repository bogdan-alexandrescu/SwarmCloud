"""The access API: owners, repositories paged and searched, grants, verify,
and register/readable resolving the caller's own GitHub slot
(docs/onboarding.md §2.2, §2.4, §3.1-§3.2; #780, lane OB4).

What is held here, every case offline against forge fakes:

  * every GitHub read is made with the caller's OWN token, minted by a
    refresh of their connection (swarm-api never reads the base slot), and
    both new values are stored in the slots, as the sweep stores them;
  * owners list installations, the person's own account and orgs without
    an installation; only an installed owner can be enabled;
  * an owner's repositories are served one page at a time, searched here,
    each saying registered, granted and mode;
  * a grant reads the repository once, refuses write where GitHub says no
    push or the repository is archived, names SSO and a missing
    installation with §2.3's copy, and registers the repository on first
    grant; removing the last grant unregisters only what a grant registered;
  * verify reads clone, push and pull request, a read grant needs no push,
    and a read that did not come back is `unknown`, never `missing`;
  * another member's or another tenant's grant is the same 404 as none;
  * bodies forbid unnamed fields; no response, document or log line holds
    a token value.

Every token-shaped value is built at runtime, never written as a literal.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from swarm_api import access, forge, forgeapp, repositories
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.onboarding import recovery_copy
from swarm_api.waker import NullWaker

from .conftest import api_settings, auth_header, seed_tenant
from .repo_fakes import GitHubRepos, TenantTokens, repo_entry
from .test_forgeapp import (
    CLIENT_ID,
    T0,
    Clock,
    FakeAppSecret,
    FakeGitHub,
    FakeSlots,
    _json,
    _suffix,
)

ORG = "example-org"
LOGIN = "alice-gh"


def _installation(inst_id: int, login: str, kind: str = "Organization",
                  pull_requests: str = "write") -> dict[str, Any]:
    return {"id": inst_id, "account": {"login": login, "type": kind},
            "repository_selection": "selected",
            "permissions": {"contents": "write", "pull_requests": pull_requests}}


class AccessGitHub(FakeGitHub):
    """FakeGitHub plus the reads the access API makes as the person. A read
    is answered only for an access token GitHub minted and has not seen
    replaced, so a test proves the person's own fresh token was used."""

    def __init__(self) -> None:
        super().__init__()
        self.login = LOGIN
        self.installs: list[dict[str, Any]] = [_installation(11, ORG),
                                               _installation(12, LOGIN, "User")]
        self.user_orgs: list[str] = [ORG, "other-org"]
        self.repos: dict[int, list[dict[str, Any]]] = {
            11: [repo_entry(f"{ORG}/repo-{i:04d}") for i in range(250)],
            12: [repo_entry(f"{LOGIN}/dotfiles")],
        }
        self.sso: set[str] = set()
        self.down: set[str] = set()
        self.no_push_git: set[str] = set()

    def access_tokens(self) -> list[str]:
        return [v for v in self.issued if v.startswith("ghu" + "_")]

    def _bearer(self, headers: dict[str, str]) -> str | None:
        value = headers.get("Authorization", "")
        return value[len("Bearer "):] if value.startswith("Bearer ") else None

    def __call__(self, method, url, headers, body, timeout):  # noqa: ANN001
        parsed = urlparse(url)
        if parsed.netloc == "github.com" and parsed.path.endswith("/info/refs"):
            self.calls.append({"method": method, "url": url, "headers": dict(headers),
                               "body": None})
            full = parsed.path[1:-len(".git/info/refs")]
            if "git" in self.down:
                return forgeapp.HttpAnswer(502, {}, b"")
            service = parse_qs(parsed.query)["service"][0]
            if service == "git-receive-pack" and full in self.no_push_git:
                return forgeapp.HttpAnswer(403, {}, b"")
            return forgeapp.HttpAnswer(200, {}, b"001e# service=" + service.encode())
        if parsed.netloc != "api.github.com" or parsed.path in ("/user",) \
                or parsed.path.startswith("/applications/"):
            return super().__call__(method, url, headers, body, timeout)
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": None})
        token = self._bearer(headers)
        assert token is not None and token in self.access_tokens(), \
            "an access read must carry a token GitHub minted for this person"
        query = parse_qs(parsed.query)
        page = int(query.get("page", ["1"])[0])
        per = int(query.get("per_page", ["30"])[0])
        path = parsed.path
        for prefix in self.down:
            if path.startswith(prefix):
                return forgeapp.HttpAnswer(502, {}, b"")
        if path == "/user/installations":
            chunk = self.installs[(page - 1) * per: page * per]
            return _json(200, {"total_count": len(self.installs), "installations": chunk})
        if path == "/user/orgs":
            chunk = [{"login": o} for o in self.user_orgs][(page - 1) * per: page * per]
            return forgeapp.HttpAnswer(200, {}, json.dumps(chunk).encode())
        if path.startswith("/user/installations/") and path.endswith("/repositories"):
            inst = int(path.split("/")[3])
            owner = next((i["account"]["login"] for i in self.installs if i["id"] == inst), None)
            if owner is None:
                return _json(404, {"message": "Not Found"})
            if owner in self.sso:
                return self._sso(owner)
            listing = self.repos.get(inst, [])
            return _json(200, {"total_count": len(listing),
                               "repositories": listing[(page - 1) * per: page * per]})
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "repos":
            if parts[1] in self.sso:
                return self._sso(parts[1])
            for listing in self.repos.values():
                for entry in listing:
                    if entry["full_name"].lower() == f"{parts[1]}/{parts[2]}".lower():
                        return _json(200, entry)
            return _json(404, {"message": "Not Found"})
        return _json(404, {})

    @staticmethod
    def _sso(owner: str) -> forgeapp.HttpAnswer:
        return forgeapp.HttpAnswer(
            403, {"x-github-sso": f"required; url=https://github.com/orgs/{owner}/sso?"
                                  "authorization_request=abc"}, b'{"message": "SSO"}')


# -- fixtures ----------------------------------------------------------------------


@pytest.fixture
def github() -> AccessGitHub:
    return AccessGitHub()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def slots() -> FakeSlots:
    return FakeSlots()


@pytest.fixture
def tenant_tokens() -> TenantTokens:
    return TenantTokens()


@pytest.fixture
def issues_transport() -> GitHubRepos:
    listing = [repo_entry(f"{ORG}/repo-{i:04d}") for i in range(3)]
    return GitHubRepos(repos={e["full_name"]: e for e in listing}, listing=listing)


@pytest.fixture
def api(db, tokens, group_map, objects, clock, github, slots, tenant_tokens,
        issues_transport) -> TestClient:
    seed_tenant(db, "eng", credentials=("git",))
    seed_tenant(db, "research", credentials=("git",))
    ctx = build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(), waker=NullWaker(),
        metrics=ApiMetrics(), objects=objects, forge_tokens=tenant_tokens,
        forge=forge.GitHubIssues(send=issues_transport), now=clock,
    )
    app = forgeapp.ForgeApp(
        db, config=forgeapp.AppConfig(client_id=CLIENT_ID, app_id="12345", slug="swarmcloud"),
        secrets=FakeAppSecret("s" + "x" * 39), slots=slots, send=github, now=clock)
    service = access.AccessService(db, app, send=github, now=clock)
    return TestClient(create_app(ctx, forge_app=app, access_service=service),
                      raise_server_exceptions=False)


def _connect(api: TestClient, github: AccessGitHub, user: str = "alice") -> None:
    answer = api.post("/v1/onboarding/github/authorize", json={"surface": "console"},
                      headers=auth_header(user))
    assert answer.status_code == 200, answer.text
    state = parse_qs(urlparse(answer.json()["authorize_url"]).query)["state"][0]
    answer = api.post("/v1/onboarding/github/exchange",
                      json={"state": state, "code": github.code()}, headers=auth_header(user))
    assert answer.status_code == 200, answer.text


def _enable(api: TestClient, owner: str = ORG, user: str = "alice") -> dict[str, Any]:
    answer = api.post("/v1/access/orgs", json={"owner": owner}, headers=auth_header(user))
    assert answer.status_code == 200, answer.text
    return answer.json()


def _rid(repository: str, tenant: str = "eng") -> str:
    owner, repo = repository.split("/")
    return repositories.repo_id_for(tenant, owner, repo)


def _grant(api: TestClient, repository: str, mode: str = "write", user: str = "alice"):
    return api.put(f"/v1/access/grants/{_rid(repository)}",
                   json={"repository": repository, "mode": mode}, headers=auth_header(user))


def _no_value_anywhere(db, github: AccessGitHub, texts: list[str]) -> None:
    dumped = json.dumps(db.dump(), default=str)
    for value in github.issued:
        assert value not in dumped, "a forge value reached Firestore"
        for text in texts:
            assert value not in text, "a forge value reached a response or a log line"


# -- not connected -------------------------------------------------------------------


def test_without_a_connection_the_access_reads_say_connect_first(api, github):
    answer = api.get("/v1/access/orgs", headers=auth_header("alice"))
    assert answer.status_code == 409
    assert answer.json()["code"] == "github_not_connected"
    overview = api.get("/v1/access", headers=auth_header("alice")).json()
    assert overview["connection"] is None and overview["orgs"] == [] and overview["grants"] == []
    assert github.calls == []


# -- owners --------------------------------------------------------------------------


def test_owners_are_read_with_the_persons_freshly_refreshed_token(api, github, db, slots,
                                                                 caplog):
    caplog.set_level(logging.DEBUG)
    _connect(api, github)
    minted_before = len(github.access_tokens())
    answer = api.get("/v1/access/orgs", headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    # One refresh: a new access token, stored in the slot the worker reads,
    # and the refresh token's twin replaced.
    assert len(github.access_tokens()) == minted_before + 1
    newest = github.access_tokens()[-1]
    assert slots.latest("eng", _suffix()) == newest
    assert github.to("https://api.github.com/user/installations?per_page=100&page=1")[0][
        "headers"]["Authorization"] == f"Bearer {newest}"
    conn = db.docs[f"forge_connections/{forgeapp.connection_id_for('eng', 'alice@saga.xyz')}"]
    assert conn["refreshed_at"] == T0 and conn["refresh_lease"] is None
    owners = {row["owner"]: row for row in body["owners"]}
    assert owners[LOGIN]["owner_type"] == "User"
    assert owners[LOGIN]["install_state"] == "installed"
    assert owners[ORG]["install_state"] == "installed" and owners[ORG]["installation_id"] == 11
    assert owners["other-org"]["install_state"] == "not_installed"
    assert owners["other-org"]["install_url"] == \
        "https://github.com/apps/swarmcloud/installations/new"
    assert not any(row["enabled"] for row in body["owners"])
    _no_value_anywhere(db, github, [answer.text, caplog.text])


def test_only_an_installed_owner_is_enabled(api, github, db):
    _connect(api, github)
    refused = api.post("/v1/access/orgs", json={"owner": "other-org"},
                       headers=auth_header("alice"))
    assert refused.status_code == 409
    assert refused.json()["code"] == "not_installed"
    assert refused.json()["detail"]["failure_code"] == "REPO_NOT_INSTALLED"
    assert not db.dump("forge_orgs/")
    org = _enable(api)["org"]
    assert org["owner"] == ORG and org["installation_id"] == 11 and org["enabled"] is True
    doc = db.docs[f"forge_orgs/{access.org_id_for('eng', 'alice@saga.xyz', ORG)}"]
    assert doc["tenant_id"] == "eng" and doc["user"] == "alice@saga.xyz"
    overview = api.get("/v1/access", headers=auth_header("alice")).json()
    assert [o["owner"] for o in overview["orgs"]] == [ORG]


def test_bodies_forbid_unnamed_fields(api, github):
    _connect(api, github)
    for path, body in (("/v1/access/orgs", {"owner": ORG, "tenant_id": "research"}),
                       ("/v1/access/orgs", {"owner": ORG, "token": "ghu" + "_" + "x" * 36})):
        answer = api.post(path, json=body, headers=auth_header("alice"))
        assert answer.status_code == 422, answer.text
    repository = f"{ORG}/repo-0001"
    answer = api.put(f"/v1/access/grants/{_rid(repository)}",
                     json={"repository": repository, "mode": "read", "user": "root@saga.xyz"},
                     headers=auth_header("alice"))
    assert answer.status_code == 422
    answer = api.post(f"/v1/access/grants/{_rid(repository)}/verify",
                      json={"checks": ["clone"], "secret_name": "x"},
                      headers=auth_header("alice"))
    assert answer.status_code == 422


# -- repositories --------------------------------------------------------------------


def test_an_owners_repositories_page_through_the_installation(api, github):
    _connect(api, github)
    _enable(api)
    first = api.get(f"/v1/access/orgs/{ORG}/repositories", headers=auth_header("alice"))
    assert first.status_code == 200, first.text
    body = first.json()
    assert len(body["repositories"]) == 100 and body["next_page"] == 2
    assert body["total_count"] == 250 and body["capped"] is False
    row = body["repositories"][0]
    assert row["repository"] == f"{ORG}/repo-0000"
    assert row["repo_id"] == _rid(f"{ORG}/repo-0000")
    assert (row["registered"], row["granted"], row["mode"], row["can_push"]) == \
        (False, False, None, True)
    last = api.get(f"/v1/access/orgs/{ORG}/repositories", params={"page": 3},
                   headers=auth_header("alice")).json()
    assert len(last["repositories"]) == 50 and last["next_page"] is None


def test_a_search_reads_every_page_and_serves_only_matches(api, github):
    _connect(api, github)
    _enable(api)
    body = api.get(f"/v1/access/orgs/{ORG}/repositories", params={"q": "REPO-02"},
                   headers=auth_header("alice")).json()
    names = [r["repo"] for r in body["repositories"]]
    assert names == [f"repo-{i:04d}" for i in range(200, 250)]
    assert body["next_page"] is None and body["q"] == "repo-02"


def test_a_disabled_owner_has_no_repository_list(api, github):
    _connect(api, github)
    answer = api.get(f"/v1/access/orgs/{ORG}/repositories", headers=auth_header("alice"))
    assert answer.status_code == 404


def test_an_sso_refusal_names_the_org_and_its_sso_page(api, github, db):
    _connect(api, github)
    _enable(api)
    github.sso.add(ORG)
    answer = api.get(f"/v1/access/orgs/{ORG}/repositories", headers=auth_header("alice"))
    assert answer.status_code == 403
    detail = answer.json()["detail"]
    assert detail["failure_code"] == "SSO_NOT_AUTHORISED"
    assert detail["url"] == f"https://github.com/orgs/{ORG}/sso"
    assert detail["recovery"] == recovery_copy("SSO_NOT_AUTHORISED", owner=ORG,
                                               url=f"https://github.com/orgs/{ORG}/sso")
    assert db.docs[f"forge_orgs/{access.org_id_for('eng', 'alice@saga.xyz', ORG)}"]["sso"] \
        == "required"


# -- grants --------------------------------------------------------------------------


def test_a_write_grant_registers_the_repository_as_the_person(api, github, db):
    _connect(api, github)
    _enable(api)
    repository = f"{ORG}/repo-0007"
    answer = _grant(api, repository)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["grant"]["mode"] == "write" and body["registered"] is True
    registration = db.docs[f"repositories/{_rid(repository)}"]
    assert registration["access"]["token_scope"] == "user"
    assert registration["access"]["secret_name"] == f"swarm-tenant-eng-{_suffix()}"
    assert registration["registered_via"] == "grant"
    listed = api.get(f"/v1/access/orgs/{ORG}/repositories", headers=auth_header("alice")).json()
    row = next(r for r in listed["repositories"] if r["repository"] == repository)
    assert (row["registered"], row["granted"], row["mode"]) == (True, True, "write")


def test_a_grant_whose_id_is_not_the_repositorys_is_refused(api, github):
    _connect(api, github)
    _enable(api)
    answer = api.put(f"/v1/access/grants/{_rid(f'{ORG}/repo-0001')}",
                     json={"repository": f"{ORG}/repo-0002", "mode": "read"},
                     headers=auth_header("alice"))
    assert answer.status_code == 422


def test_write_is_refused_where_github_says_no_push_or_archived(api, github, db):
    _connect(api, github)
    _enable(api)
    github.repos[11][3]["permissions"]["push"] = False
    github.repos[11][4]["archived"] = True
    no_push = _grant(api, f"{ORG}/repo-0003")
    assert no_push.status_code == 422
    assert no_push.json()["detail"]["failure_code"] == "PERMISSION_MISSING"
    archived = _grant(api, f"{ORG}/repo-0004")
    assert archived.json()["detail"]["failure_code"] == "REPO_ARCHIVED"
    assert _grant(api, f"{ORG}/repo-0003", mode="read").status_code == 200
    assert not db.dump(f"forge_grants/eng__{forgeapp.user_hash('alice@saga.xyz')}__"
                       f"{_rid(f'{ORG}/repo-0004')}")


def test_a_repository_the_installation_does_not_cover_names_repo_not_installed(api, github):
    _connect(api, github)
    _enable(api)
    answer = _grant(api, f"{ORG}/not-installed", mode="read")
    assert answer.status_code == 403
    detail = answer.json()["detail"]
    assert detail["failure_code"] == "REPO_NOT_INSTALLED"
    assert detail["url"] == "https://github.com/settings/installations/11"


def test_removing_the_last_grant_unregisters_only_what_a_grant_registered(api, github, db):
    _connect(api, github)
    _enable(api)
    granted = f"{ORG}/repo-0001"
    assert _grant(api, granted).status_code == 200
    # Registered before, by the tenant path: kept when its grant goes.
    kept = f"{ORG}/repo-0002"
    db.docs[f"repositories/{_rid(kept)}"] = {"repo_id": _rid(kept), "tenant_id": "eng"}
    assert _grant(api, kept, mode="read").status_code == 200
    gone = api.delete(f"/v1/access/grants/{_rid(granted)}", headers=auth_header("alice"))
    assert gone.status_code == 200 and gone.json()["unregistered"] is True
    assert f"repositories/{_rid(granted)}" not in db.docs
    stays = api.delete(f"/v1/access/grants/{_rid(kept)}", headers=auth_header("alice"))
    assert stays.json()["unregistered"] is False
    assert f"repositories/{_rid(kept)}" in db.docs


def test_another_members_grant_keeps_the_registration(api, github, db):
    _connect(api, github)
    _connect(api, github, user="root")
    _enable(api)
    _enable(api, user="root")
    repository = f"{ORG}/repo-0001"
    assert _grant(api, repository).status_code == 200
    assert _grant(api, repository, user="root").status_code == 200
    gone = api.delete(f"/v1/access/grants/{_rid(repository)}", headers=auth_header("alice"))
    assert gone.json()["unregistered"] is False
    assert f"repositories/{_rid(repository)}" in db.docs


def test_disabling_an_owner_deletes_its_grants(api, github, db):
    _connect(api, github)
    _enable(api)
    _enable(api, owner=LOGIN)
    assert _grant(api, f"{ORG}/repo-0001").status_code == 200
    assert _grant(api, f"{LOGIN}/dotfiles").status_code == 200
    answer = api.delete(f"/v1/access/orgs/{ORG}", headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    assert answer.json()["grants_deleted"] == 1
    assert answer.json()["installation_settings_url"] == \
        "https://github.com/settings/installations/11"
    overview = api.get("/v1/access", headers=auth_header("alice")).json()
    assert [g["repository"] for g in overview["grants"]] == [f"{LOGIN}/dotfiles"]
    assert [o["owner"] for o in overview["orgs"]] == [LOGIN.lower()]


# -- isolation -----------------------------------------------------------------------


def test_another_members_or_tenants_grant_is_the_same_404(api, github):
    _connect(api, github)
    _enable(api)
    repository = f"{ORG}/repo-0001"
    assert _grant(api, repository).status_code == 200
    for user in ("root", "bob"):
        _connect(api, github, user=user)
        assert api.delete(f"/v1/access/grants/{_rid(repository)}",
                          headers=auth_header(user)).status_code == 404
        assert api.post(f"/v1/access/grants/{_rid(repository)}/verify",
                        headers=auth_header(user)).status_code == 404
        assert api.get("/v1/access", headers=auth_header(user)).json()["grants"] == []


def test_the_members_view_is_an_admins_and_holds_no_value(api, github, db):
    _connect(api, github)
    _enable(api)
    assert _grant(api, f"{ORG}/repo-0001").status_code == 200
    assert api.get("/v1/access/members", headers=auth_header("alice")).status_code == 403
    answer = api.get("/v1/access/members", headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    members = {m["user"]: m for m in answer.json()["members"]}
    assert members["alice@saga.xyz"]["connection"]["state"] == "active"
    assert [g["repository"] for g in members["alice@saga.xyz"]["grants"]] == [f"{ORG}/repo-0001"]
    _no_value_anywhere(db, github, [answer.text])


# -- verify --------------------------------------------------------------------------


def test_verify_a_write_grant_reads_clone_push_and_pull_request(api, github, db):
    _connect(api, github)
    _enable(api)
    repository = f"{ORG}/repo-0001"
    assert _grant(api, repository).status_code == 200
    answer = api.post(f"/v1/access/grants/{_rid(repository)}/verify",
                      headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["passed"] is True and body["failures"] == []
    assert {k: v["state"] for k, v in body["grant"]["checks"].items()} == \
        {"clone": "ok", "push": "ok", "pull_request": "ok"}
    git = [c["url"] for c in github.calls if c["url"].startswith("https://github.com/")]
    assert any("service=git-upload-pack" in u for u in git)
    assert any("service=git-receive-pack" in u for u in git)
    assert all(c["method"] == "GET" for c in github.calls
               if not c["url"].startswith(forgeapp.TOKEN_URL))


def test_a_read_grant_needs_no_push(api, github):
    _connect(api, github)
    _enable(api)
    repository = f"{ORG}/repo-0001"
    assert _grant(api, repository, mode="read").status_code == 200
    body = api.post(f"/v1/access/grants/{_rid(repository)}/verify",
                    headers=auth_header("alice")).json()
    assert body["passed"] is True
    assert body["grant"]["checks"]["push"]["state"] == "not_required"
    assert not any("git-receive-pack" in c["url"] for c in github.calls)


def test_a_refused_push_is_permission_missing(api, github):
    _connect(api, github)
    _enable(api)
    repository = f"{ORG}/repo-0001"
    assert _grant(api, repository).status_code == 200
    github.no_push_git.add(repository)
    body = api.post(f"/v1/access/grants/{_rid(repository)}/verify",
                    json={"checks": ["push"]}, headers=auth_header("alice")).json()
    assert body["passed"] is False
    assert body["grant"]["checks"]["push"]["state"] == "missing"
    assert body["failures"][0]["code"] == "PERMISSION_MISSING"
    assert body["failures"][0]["copy"] == recovery_copy("PERMISSION_MISSING", repo=repository,
                                                        login=LOGIN)


def test_a_check_github_did_not_answer_is_unknown_not_missing(api, github):
    _connect(api, github)
    _enable(api)
    repository = f"{ORG}/repo-0001"
    assert _grant(api, repository).status_code == 200
    github.down.add("git")
    body = api.post(f"/v1/access/grants/{_rid(repository)}/verify",
                    headers=auth_header("alice")).json()
    states = {k: v["state"] for k, v in body["grant"]["checks"].items()}
    assert states == {"clone": "unknown", "push": "unknown", "pull_request": "unknown"}
    assert {f["code"] for f in body["failures"]} == {"FORGE_UNREACHABLE"}


# -- the connection's state ----------------------------------------------------------


def test_a_failed_connection_is_refused_with_its_recovery_copy(api, github, db):
    _connect(api, github)
    conn_path = f"forge_connections/{forgeapp.connection_id_for('eng', 'alice@saga.xyz')}"
    db.docs[conn_path]["state"] = forgeapp.REFRESH_FAILED
    answer = api.get("/v1/access/orgs", headers=auth_header("alice"))
    assert answer.status_code == 409
    assert answer.json()["detail"]["recovery"] == recovery_copy("REFRESH_FAILED", login=LOGIN)
    readable = api.get("/v1/repositories/readable", headers=auth_header("alice"))
    assert readable.status_code == 409


def test_a_refresh_github_refuses_marks_the_connection_failed(api, github, db):
    _connect(api, github)
    github.token_endpoint = "refuse"
    answer = api.get("/v1/access/orgs", headers=auth_header("alice"))
    assert answer.status_code == 409
    assert answer.json()["detail"]["failure_code"] == "REFRESH_FAILED"
    conn = db.docs[f"forge_connections/{forgeapp.connection_id_for('eng', 'alice@saga.xyz')}"]
    assert conn["state"] == forgeapp.REFRESH_FAILED and conn["refresh_lease"] is None


def test_a_refresh_running_elsewhere_is_a_409_not_a_second_spend(api, github, db):
    _connect(api, github)
    conn_path = f"forge_connections/{forgeapp.connection_id_for('eng', 'alice@saga.xyz')}"
    db.docs[conn_path]["refresh_lease"] = {"holder": "sweep", "until": T0 + timedelta(minutes=1)}
    refreshes = len(github.to(forgeapp.TOKEN_URL))
    answer = api.get("/v1/access/orgs", headers=auth_header("alice"))
    assert answer.status_code == 409 and answer.json()["code"] == "conflict"
    assert len(github.to(forgeapp.TOKEN_URL)) == refreshes


# -- register and readable resolve the caller's slot ----------------------------------


def test_readable_uses_the_callers_own_connection(api, github, issues_transport,
                                                  tenant_tokens):
    _connect(api, github)
    answer = api.get("/v1/repositories/readable", headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["token_scope"] == "user"
    assert body["secret_name"] == f"swarm-tenant-eng-{_suffix()}"
    assert issues_transport.calls[-1][1]["Authorization"].endswith(github.access_tokens()[-1])
    assert tenant_tokens.asked == []
    assert github.access_tokens()[-1] not in answer.text


def test_readable_without_a_connection_is_the_tenant_token_as_before(api, tenant_tokens):
    body = api.get("/v1/repositories/readable", headers=auth_header("alice")).json()
    assert body["token_scope"] == "tenant" and body["secret_name"] == "swarm-tenant-eng-git"
    assert tenant_tokens.asked == ["swarm-tenant-eng-git"]


def test_register_reads_as_the_caller_and_names_their_slot(api, github, db):
    _connect(api, github)
    answer = api.post("/v1/repositories", json={"repository": f"{ORG}/repo-0001"},
                      headers=auth_header("alice"))
    assert answer.status_code == 201, answer.text
    record = db.docs[f"repositories/{_rid(f'{ORG}/repo-0001')}"]
    assert record["access"]["token_scope"] == "user"
    assert record["access"]["secret_name"] == f"swarm-tenant-eng-{_suffix()}"
    assert "registered_via" not in record
    refused = api.post("/v1/repositories", json={"repository": f"{ORG}/unseen"},
                       headers=auth_header("alice"))
    assert refused.status_code == 403
    assert refused.json()["detail"]["cause"] == "REPO_NOT_INSTALLED"
    assert refused.json()["detail"]["token_scope"] == "user"
