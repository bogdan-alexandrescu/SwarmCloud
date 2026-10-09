"""D5: a person's fallback GitHub token for ONE owner (#780; docs/onboarding.md
§1.3, §2.3, §3.1, §3.2 row `POST /v1/onboarding/github/token`, §6 D5).

An org whose admin will not install the SwarmCloud GitHub App is reached with
a personal access token the person stores for that owner. What is held here,
every case offline against forge fakes:

  * the route stores the value ONCE, as a version of the person's per-owner
    slot `swarm-tenant-<t>-git-u-<16 hex of sha256(email|owner)>` -- a name
    `swarm_common.models.FORGE_CREDENTIAL` accepts and that falls under
    terraform/bootstrap/forge_user_slots.tf's `-git-u-` prefix, so neither the
    frozen contract nor IAM changes -- and answers the org record, never the
    token; no response, log record or Firestore document holds the value;
  * the body names `owner` and `token` and nothing else: a tenant or a user
    in it is a 422, and the tenant and the person are the verified caller's;
  * the probe's refusals carry §2.3's code and copy -- SSO_NOT_AUTHORISED,
    CLASSIC_PAT_BLOCKED, FINE_GRAINED_PAT_PENDING -- and a refused token
    leaves no enabled version behind;
  * a PAT-enabled owner's repositories, grants and verification are read with
    that token, never the App's user token, and a person's task on one of its
    repositories is signed with the per-owner slot, which the scheduler and the
    worker recognise as the submitter's own;
  * removing the owner (or disconnecting) disables every version of the slot,
    revokes its record and deletes the owner's grants, so a later submission
    is refused or falls back exactly as REPOSITORY_GRANTS_ENFORCED says.

Every token-shaped value is built at runtime, never written as a literal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from agent_worker import secrets as worker_secrets
from scheduler import credentials as sched
from swarm_api import access, forge, forgeapp, gittokens, repositories
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.onboarding import recovery_copy
from swarm_api.waker import NullWaker
from swarm_common.models import FORGE_CREDENTIAL

from .conftest import api_settings, auth_header, seed_grant, seed_tenant
from .repo_fakes import GitHubRepos, TenantTokens, repo_entry
from .spec_signer import LocalSpecSigner
from .test_access_api import LOGIN, ORG, AccessGitHub, _connect, _enable
from .test_forgeapp import CLIENT_ID, Clock, FakeAppSecret, FakeSlots

ALICE = "alice@saga.xyz"
BOB = "bob@saga.xyz"
#: The org that will not install the App: reached through a token.
PAT_ORG = "saga-xyz"
REPO = f"{PAT_ORG}/widgets"
REPO_URL = f"https://github.com/{REPO}.git"
TOKEN_ROUTE = "/v1/onboarding/github/token"


def _expected_suffix(email: str, owner: str) -> str:
    """The recipe, written out independently of the code under test."""
    digest = hashlib.sha256(f"{email.lower()}|{owner.lower()}".encode()).hexdigest()
    return "git-u-" + digest[:16]


def _rid(repository: str, tenant: str = "eng") -> str:
    owner, repo = repository.split("/")
    return repositories.repo_id_for(tenant, owner, repo)


class PatGitHub(AccessGitHub):
    """AccessGitHub plus what GitHub answers a personal access token: the
    account, its orgs, an org's repositories and one repository. `refuse`
    maps an owner to the answer every read under it gets instead."""

    def __init__(self) -> None:
        super().__init__()
        self.pats: dict[str, str] = {}
        self.org_repos: dict[str, list[dict[str, Any]]] = {
            PAT_ORG: [repo_entry(REPO), repo_entry(f"{PAT_ORG}/gadgets", push=False)],
        }
        self.refuse: dict[str, forgeapp.HttpAnswer] = {}
        #: `owner/repo` -> a status every read of that repository answers.
        self.repo_status: dict[str, int] = {}

    def pat(self, kind: str = "classic", login: str = LOGIN) -> str:
        value = ("ghp" + "_" + secrets.token_hex(18) if kind == "classic"
                 else "github" + "_pat_" + secrets.token_hex(30))
        self.pats[value] = login
        self.issued.append(value)
        return value

    def __call__(self, method, url, headers, body, timeout):  # noqa: ANN001
        parsed = urlparse(url)
        token = self._bearer(headers)
        if parsed.netloc == "api.github.com" and token is not None \
                and token.startswith(("ghp" + "_", "github" + "_pat_")) and token not in self.pats:
            self.calls.append({"method": method, "url": url, "headers": dict(headers),
                               "body": None})
            return forgeapp.HttpAnswer(401, {}, b'{"message": "Bad credentials"}')
        if parsed.netloc != "api.github.com" or token not in self.pats:
            return super().__call__(method, url, headers, body, timeout)
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": None})
        login = self.pats[token]
        path = parsed.path
        parts = path.strip("/").split("/")
        scopes = {"x-oauth-scopes": "repo, read:org"} if token.startswith("ghp" + "_") else {}
        if path == "/user":
            return forgeapp.HttpAnswer(200, scopes, json.dumps({"login": login, "id": 7}).encode())
        if path == "/user/orgs":
            return forgeapp.HttpAnswer(200, scopes, json.dumps([{"login": PAT_ORG}]).encode())
        # GitHub reads logins case-insensitively.
        owner = parts[1].lower() if len(parts) >= 2 and parts[0] in ("orgs", "repos") else None
        if owner is not None and owner in self.refuse:
            return self.refuse[owner]
        if len(parts) == 3 and parts[0] == "orgs" and parts[2] == "repos":
            if owner not in self.org_repos:
                return forgeapp.HttpAnswer(404, {}, b'{"message": "Not Found"}')
            return forgeapp.HttpAnswer(200, scopes, json.dumps(self.org_repos[owner]).encode())
        if len(parts) == 3 and parts[0] == "repos":
            full = f"{parts[1]}/{parts[2]}"
            if full in self.repo_status:
                return forgeapp.HttpAnswer(self.repo_status[full], {},
                                           b'{"message": "Not Found"}')
            for entry in self.org_repos.get(owner, []):
                if entry["full_name"].lower() == full.lower():
                    return forgeapp.HttpAnswer(200, scopes, json.dumps(entry).encode())
        return forgeapp.HttpAnswer(404, {}, b'{"message": "Not Found"}')

    def bearers(self, path_prefix: str) -> set[str]:
        return {c["headers"].get("Authorization", "")[len("Bearer "):]
                for c in self.calls
                if urlparse(c["url"]).netloc == "api.github.com"
                and urlparse(c["url"]).path.startswith(path_prefix)}


def _sso(owner: str) -> forgeapp.HttpAnswer:
    return forgeapp.HttpAnswer(
        403, {"x-github-sso": f"required; url=https://github.com/orgs/{owner}/sso?"
                              "authorization_request=" + secrets.token_hex(8)},
        b'{"message": "Resource protected by organization SAML enforcement."}')


def _classic_blocked(owner: str) -> forgeapp.HttpAnswer:
    return forgeapp.HttpAnswer(403, {}, json.dumps({
        "message": f"`{owner}` forbids access via a personal access token (classic). Please "
                   "use a GitHub App, OAuth App, or a personal access token with fine-grained "
                   "permissions."}).encode())


# -- fixtures ----------------------------------------------------------------------


@pytest.fixture
def github() -> PatGitHub:
    return PatGitHub()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def slots() -> FakeSlots:
    return FakeSlots()


@pytest.fixture
def make_api(db, tokens, group_map, objects, clock, github, slots):
    seed_tenant(db, "eng", credentials=("git",))
    seed_tenant(db, "research", credentials=("git",))
    listing = [repo_entry(f"{ORG}/repo-{i:04d}") for i in range(3)]
    issues = GitHubRepos(repos={e["full_name"]: e for e in listing}, listing=listing)

    def make(*, enforced: bool = True) -> TestClient:
        ctx = build_context(
            settings=api_settings(repository_grants_enforced=enforced), db=db,
            verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
            credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
            objects=objects, forge_tokens=TenantTokens(), forge=forge.GitHubIssues(send=issues),
            now=clock, signer=LocalSpecSigner(),
        )
        app = forgeapp.ForgeApp(
            db, config=forgeapp.AppConfig(client_id=CLIENT_ID, app_id="12345",
                                          slug="swarmcloud"),
            secrets=FakeAppSecret("s" + "x" * 39), slots=slots, send=github, now=clock)
        service = access.AccessService(db, app, send=github, now=clock)
        return TestClient(create_app(ctx, forge_app=app, access_service=service),
                          raise_server_exceptions=False)

    return make


@pytest.fixture
def api(make_api) -> TestClient:
    return make_api()


def _store(api: TestClient, token: str, owner: str = PAT_ORG, user: str = "alice"):
    return api.post(TOKEN_ROUTE, json={"owner": owner, "token": token},
                    headers=auth_header(user))


def _slot(email: str = ALICE, owner: str = PAT_ORG) -> str:
    return gittokens.owner_suffix(email, owner)


def _org_doc(db, email: str = ALICE, owner: str = PAT_ORG, tenant: str = "eng"):
    return db.docs.get(f"forge_orgs/{access.org_id_for(tenant, email, owner)}")


def _no_value_anywhere(db, github: PatGitHub, texts: list[str]) -> None:
    dumped = json.dumps(db.dump(), default=str)
    for value in github.pats:
        assert value not in dumped, "a token reached Firestore"
        for text in texts:
            assert value not in text, "a token reached a response or a log record"


def _task(repository_url: str = REPO_URL) -> dict[str, Any]:
    return {"runner_profile": "mock", "input": {"prompt": "hello"},
            "repository_url": repository_url}


def _tasks(db) -> list[dict[str, Any]]:
    return [doc for key, doc in sorted(db.docs.items())
            if key.startswith("tasks/") and key.count("/") == 1]


# -- the suffix --------------------------------------------------------------------


def test_the_owner_slot_suffix_is_git_u_hex_of_email_and_owner():
    suffix = gittokens.owner_suffix("Alice@Saga.xyz ", "Saga-XYZ")
    assert suffix == _expected_suffix(ALICE, PAT_ORG)
    assert FORGE_CREDENTIAL.fullmatch(suffix), "the frozen contract must accept it"
    # Distinct from the person's App slot, and from the same person's slot
    # for another owner: one token per owner.
    assert suffix != gittokens.provider_suffix(gittokens.Scope.USER, user=ALICE)
    assert suffix != gittokens.owner_suffix(ALICE, "another-org")
    # Under the bootstrap's runtime-created user-slot prefix (no IAM change).
    assert gittokens.secret_name_for("eng", suffix).startswith("swarm-tenant-eng-git-u-")


# -- storing -----------------------------------------------------------------------


def test_the_route_stores_one_version_and_answers_the_org_never_the_token(
        api, github, db, slots, caplog):
    caplog.set_level(logging.DEBUG)
    token = github.pat("classic")
    answer = _store(api, token)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    suffix = _expected_suffix(ALICE, PAT_ORG)
    assert slots.versions("eng", suffix) == 1
    assert slots.latest("eng", suffix) == token
    assert slots.secrets[f"swarm-tenant-eng-{suffix}"]["labels"]["managed-by"] == "swarm-api"
    org = body["org"]
    assert org["owner"] == PAT_ORG and org["enabled"] is True and org["method"] == "pat"
    assert body["token"]["secret_name"] == f"swarm-tenant-eng-{suffix}"
    assert body["token"]["kind"] == "classic_pat"
    assert body["token"]["forge_login"] == LOGIN
    doc = _org_doc(db)
    assert doc["method"] == "pat" and doc["tenant_id"] == "eng" and doc["user"] == ALICE
    assert doc["provider_suffix"] == suffix
    record = db.docs[f"git_tokens/{doc['token_id']}"]
    assert record["scope"] == "user" and record["user"] == ALICE
    assert record["provider_suffix"] == suffix and record["kind"] == "classic_pat"
    assert record["state"] == "active" and record["forge_login"] == LOGIN
    # The token was read with, never the App's: no App connection exists.
    assert github.bearers("/orgs/") == {token}
    _no_value_anywhere(db, github, [answer.text, caplog.text])


def test_a_fine_grained_token_is_recorded_as_one(api, github, db):
    token = github.pat("fine")
    answer = _store(api, token)
    assert answer.status_code == 200, answer.text
    assert db.docs[f"git_tokens/{_org_doc(db)['token_id']}"]["kind"] == "fine_grained_pat"


def test_a_second_token_for_the_owner_is_a_new_version_of_the_same_slot(api, github, slots):
    assert _store(api, github.pat()).status_code == 200
    second = github.pat()
    assert _store(api, second, owner="Saga-XYZ").status_code == 200
    assert slots.versions("eng", _slot()) == 2
    assert slots.latest("eng", _slot()) == second


@pytest.mark.parametrize("extra", [{"tenant_id": "research"}, {"user": BOB},
                                   {"scope": "tenant"}, {"provider": "git"}])
def test_the_body_refuses_tenant_user_and_any_unnamed_field(api, github, db, slots, extra):
    token = github.pat()
    answer = api.post(TOKEN_ROUTE, json={"owner": PAT_ORG, "token": token, **extra},
                      headers=auth_header("alice"))
    assert answer.status_code == 422, answer.text
    assert token not in answer.text
    assert slots.secrets == {} and not db.dump("forge_orgs/")
    assert github.calls == []


@pytest.mark.parametrize("body", [{"token": "ghp" + "_" + "x" * 36},
                                  {"owner": PAT_ORG},
                                  {"owner": "not/an owner", "token": "ghp" + "_" + "x" * 36}])
def test_the_body_needs_an_owner_and_a_token(api, github, body):
    answer = api.post(TOKEN_ROUTE, json=body, headers=auth_header("alice"))
    assert answer.status_code == 422, answer.text
    assert github.calls == []


def test_a_value_that_is_not_a_personal_access_token_is_refused(api, github, slots):
    for value in ("ghu" + "_" + secrets.token_hex(18), "ghs" + "_" + secrets.token_hex(18),
                  secrets.token_hex(20)):
        answer = _store(api, value)
        assert answer.status_code == 422, answer.text
        assert value not in answer.text
    assert slots.secrets == {} and github.calls == []


def test_another_tenants_caller_cannot_read_or_disable_the_slot(api, github, db, slots):
    assert _store(api, github.pat()).status_code == 200
    for answer in (api.get(f"/v1/access/orgs/{PAT_ORG}/repositories",
                           headers=auth_header("bob")),
                   api.delete(f"/v1/access/orgs/{PAT_ORG}", headers=auth_header("bob"))):
        assert answer.status_code == 404, answer.text
    assert slots.latest("eng", _slot()) is not None
    assert _org_doc(db)["method"] == "pat"
    # Bob's own token for the same owner is his slot, in his tenant.
    bobs = github.pat()
    assert _store(api, bobs, user="bob").status_code == 200
    assert slots.latest("research", _slot(BOB)) == bobs
    assert slots.latest("eng", _slot()) != bobs


# -- refusals (§2.3) ---------------------------------------------------------------


def _refused(answer, code: str, **fill: str) -> None:
    assert answer.status_code == 403, answer.text
    detail = answer.json()["detail"]
    assert detail["failure_code"] == code
    assert detail["recovery"] == recovery_copy(code, **fill)


def _left_disabled(db, slots, github) -> None:
    assert slots.latest("eng", _slot()) is None, "a refused token is left enabled"
    assert _org_doc(db) is None


def test_a_token_sso_has_not_authorised_is_refused_with_sso_copy(api, github, db, slots):
    github.refuse[PAT_ORG] = _sso(PAT_ORG)
    answer = _store(api, github.pat("classic"))
    _refused(answer, "SSO_NOT_AUTHORISED", owner=PAT_ORG,
             url=f"https://github.com/orgs/{PAT_ORG}/sso")
    assert "authorization_request" not in answer.text
    _left_disabled(db, slots, github)


def test_a_classic_token_the_org_blocks_is_refused_with_classic_copy(api, github, db, slots):
    github.refuse[PAT_ORG] = _classic_blocked(PAT_ORG)
    _refused(_store(api, github.pat("classic")), "CLASSIC_PAT_BLOCKED", owner=PAT_ORG)
    _left_disabled(db, slots, github)


def test_a_pending_fine_grained_token_is_refused_with_pending_copy(api, github, db, slots):
    # The person already chose a repository there; GitHub shows a fine-grained
    # token awaiting the org's approval only public repositories, so the
    # private one answers 404.
    seed_grant(db, "eng", ALICE, REPO)
    github.repo_status[REPO] = 404
    _refused(_store(api, github.pat("fine")), "FINE_GRAINED_PAT_PENDING", owner=PAT_ORG)
    _left_disabled(db, slots, github)


def test_a_fine_grained_token_that_reaches_nothing_in_the_org_is_pending(api, github, db, slots):
    github.org_repos[PAT_ORG] = []
    _refused(_store(api, github.pat("fine")), "FINE_GRAINED_PAT_PENDING", owner=PAT_ORG)
    _left_disabled(db, slots, github)


def test_a_token_that_cannot_read_the_owner_is_refused(api, github, db, slots):
    answer = _store(api, github.pat("classic"), owner="nobody-org")
    assert answer.status_code == 422, answer.text
    assert "nobody-org" in answer.json()["message"]
    assert slots.latest("eng", _slot(owner="nobody-org")) is None
    assert _org_doc(db, owner="nobody-org") is None


def test_a_token_github_rejects_is_refused_and_nothing_stored(api, github, db, slots):
    token = "ghp" + "_" + secrets.token_hex(18)   # not one GitHub issued
    github.issued.append(token)
    answer = _store(api, token)
    assert answer.status_code == 422, answer.text
    assert token not in answer.text
    assert slots.secrets == {} and _org_doc(db) is None


def test_a_refused_token_leaves_the_owners_working_token_in_place(api, github, slots):
    good = github.pat("classic")
    assert _store(api, good).status_code == 200
    github.refuse[PAT_ORG] = _sso(PAT_ORG)
    assert _store(api, github.pat("classic")).status_code == 403
    assert slots.latest("eng", _slot()) == good
    assert slots.versions("eng", _slot()) == 1


# -- reading and running with the token --------------------------------------------


def test_a_pat_owner_is_read_granted_and_verified_with_its_token(api, github, db):
    _connect(api, github)              # the App connection exists too ...
    _enable(api)                       # ... for ORG, which the App reaches
    token = github.pat("classic")
    assert _store(api, token).status_code == 200
    app_tokens = set(github.access_tokens())

    listing = api.get(f"/v1/access/orgs/{PAT_ORG}/repositories", headers=auth_header("alice"))
    assert listing.status_code == 200, listing.text
    assert {r["repository"] for r in listing.json()["repositories"]} == {
        REPO, f"{PAT_ORG}/gadgets"}

    granted = api.put(f"/v1/access/grants/{_rid(REPO)}",
                      json={"repository": REPO, "mode": "write"}, headers=auth_header("alice"))
    assert granted.status_code == 200, granted.text
    verified = api.post(f"/v1/access/grants/{_rid(REPO)}/verify", json={},
                        headers=auth_header("alice"))
    assert verified.status_code == 200, verified.text
    assert verified.json()["grant"]["checks"]["clone"]["state"] == "ok"

    # Every read under the PAT owner carried the PAT, none the App's token.
    used = github.bearers(f"/orgs/{PAT_ORG}") | github.bearers(f"/repos/{PAT_ORG}")
    assert used == {token}
    assert not used & app_tokens

    owners = api.get("/v1/access/orgs", headers=auth_header("alice"))
    assert owners.status_code == 200, owners.text
    row = {r["owner"].lower(): r for r in owners.json()["owners"]}[PAT_ORG]
    assert row["enabled"] is True and row["method"] == "pat"


def test_a_persons_task_on_a_pat_owner_runs_with_the_owner_slot(api, github, db):
    _connect(api, github)
    assert _store(api, github.pat("classic")).status_code == 200
    seed_grant(db, "eng", ALICE, REPO)
    seed_grant(db, "eng", ALICE, f"{ORG}/repo-0001")
    for url in (REPO_URL, f"https://github.com/{ORG}/repo-0001.git"):
        answer = api.post("/v1/tasks", json=_task(url), headers=auth_header("alice"))
        assert answer.status_code == 201, answer.text
    by_url = {doc["repository_url"]: doc for doc in _tasks(db)}
    pat_task = by_url[REPO_URL]
    assert pat_task["forge_credential"] == _slot()
    assert pat_task["forge_access"] == "write"
    # The App-reached owner keeps the person's App slot.
    assert by_url[f"https://github.com/{ORG}/repo-0001.git"]["forge_credential"] == \
        gittokens.provider_suffix(gittokens.Scope.USER, user=ALICE)

    # The scheduler admits it as the submitter's own slot ...
    forge_view = sched.ForgeConnections(db)
    task = SimpleNamespace(tenant_id="eng", submitted_by=ALICE, repository_url=REPO_URL,
                           forge_credential=_slot())
    assert sched.needs_user_slot(task)
    assert sched.forge_answer(task, forge_view) is sched.ForgeAnswer.ACTIVE
    # ... and the worker finds the person's grant behind it.
    assert worker_secrets.grant_refusal(
        db, tenant_id="eng", suffix=_slot(), repository_url=REPO_URL, write=True,
        submitted_by=ALICE) is None


def test_another_persons_owner_slot_is_not_the_submitters(api, github, db):
    """A task signed with someone else's per-owner slot is not admitted as
    theirs, and the worker finds no grant behind it (invariant 9, D7)."""
    seed_grant(db, "eng", ALICE, REPO)
    assert _store(api, github.pat("classic")).status_code == 200
    forge_view = sched.ForgeConnections(db)
    task = SimpleNamespace(tenant_id="eng", submitted_by="carol@saga.xyz",
                           repository_url=REPO_URL, forge_credential=_slot())
    assert sched.forge_answer(task, forge_view) is sched.ForgeAnswer.CONNECTION_MISSING
    assert worker_secrets.grant_refusal(
        db, tenant_id="eng", suffix=_slot(), repository_url=REPO_URL, write=False,
        submitted_by="carol@saga.xyz") is not None
    # Alice's slot for the owner, on a repository of ANOTHER owner, is not hers either.
    other = f"https://github.com/{ORG}/repo-0001.git"
    seed_grant(db, "eng", ALICE, f"{ORG}/repo-0001")
    task = SimpleNamespace(tenant_id="eng", submitted_by=ALICE, repository_url=other,
                           forge_credential=_slot())
    assert sched.forge_answer(task, forge_view) is sched.ForgeAnswer.CONNECTION_MISSING
    assert worker_secrets.grant_refusal(
        db, tenant_id="eng", suffix=_slot(), repository_url=other, write=False,
        submitted_by=ALICE) is not None


# -- removal -----------------------------------------------------------------------


@pytest.mark.parametrize("enforced", [True, False])
def test_removing_the_owner_disables_every_version_and_its_grants(
        make_api, github, db, slots, enforced):
    api = make_api(enforced=enforced)
    assert _store(api, github.pat()).status_code == 200
    assert _store(api, github.pat()).status_code == 200
    assert slots.versions("eng", _slot()) == 2
    granted = api.put(f"/v1/access/grants/{_rid(REPO)}",
                      json={"repository": REPO, "mode": "write"}, headers=auth_header("alice"))
    assert granted.status_code == 200, granted.text
    token_id = _org_doc(db)["token_id"]

    removed = api.delete(f"/v1/access/orgs/{PAT_ORG}", headers=auth_header("alice"))
    assert removed.status_code == 200, removed.text
    assert removed.json()["grants_deleted"] == 1
    secret = slots.secrets[f"swarm-tenant-eng-{_slot()}"]
    assert [v["enabled"] for v in secret["versions"]] == [False, False]
    assert db.docs[f"git_tokens/{token_id}"]["state"] == "revoked"
    assert _org_doc(db) is None
    assert not [d for d in db.dump("forge_grants/").values() if d.get("owner") == PAT_ORG]

    # A task signed with the slot before the removal is no longer admitted ...
    task = SimpleNamespace(tenant_id="eng", submitted_by=ALICE, repository_url=REPO_URL,
                           forge_credential=_slot())
    assert sched.forge_answer(task, sched.ForgeConnections(db)) is not sched.ForgeAnswer.ACTIVE
    assert worker_secrets.grant_refusal(
        db, tenant_id="eng", suffix=_slot(), repository_url=REPO_URL, write=False,
        submitted_by=ALICE) is not None
    # ... and a new submission is refused, or runs with the tenant token, as
    # REPOSITORY_GRANTS_ENFORCED says.
    answer = api.post("/v1/tasks", json=_task(), headers=auth_header("alice"))
    if enforced:
        assert answer.status_code == 403, answer.text
        assert answer.json()["code"] == "REPOSITORY_NOT_GRANTED"
    else:
        assert answer.status_code == 201, answer.text
        assert _tasks(db)[-1]["forge_credential"] == "git"


def test_disconnecting_github_revokes_every_owner_token(api, github, db, slots):
    _connect(api, github)
    assert _store(api, github.pat()).status_code == 200
    token_id = _org_doc(db)["token_id"]
    answer = api.delete("/v1/onboarding/github", headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    assert slots.latest("eng", _slot()) is None
    assert db.docs[f"git_tokens/{token_id}"]["state"] == "revoked"
    assert _org_doc(db) is None
    assert answer.json()["owner_tokens_revoked"] == [PAT_ORG]


def test_a_person_with_only_owner_tokens_can_disconnect(api, github, db, slots):
    assert _store(api, github.pat()).status_code == 200
    seed_grant(db, "eng", ALICE, REPO)
    answer = api.delete("/v1/onboarding/github", headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    assert answer.json()["owner_tokens_revoked"] == [PAT_ORG]
    assert answer.json()["grants_deleted"] == 1
    assert slots.latest("eng", _slot()) is None and _org_doc(db) is None
