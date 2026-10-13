"""GitHub permissions health: `GET /v1/forge/permissions` (#1086, PERM-1).

What is held here, every case offline against a fake GitHub transport:

  * `missing` is each requirement of `REQUIRED` a grant does not meet, read
    by access rank (admin covers write covers read);
  * `pending_update` is true exactly when the App requests a permission an
    installation has not accepted -- the 2026-10-11 ai-studio state -- and
    the org's review link is GitHub's update form for that installation;
  * the App's `settings_url` reaches a platform admin and never a member,
    including a member served from the cache an admin's read filled;
  * a token-backed repository's capabilities are true, false or null from
    two read-only GETs, and null is "not measured", never a refusal;
  * one org's installation erroring is that org's `error`, and the other
    orgs and their repositories still answer;
  * the answer is cached per tenant for a minute; `refresh=true` reads again.

Every token-shaped value is built at runtime, never written as a literal.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import forgeapp, forgeperms
from swarm_api.deps import build_context
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.forgeperms import REQUIRED, ForgePermissions, Registered, missing, pending
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.repositories import repo_id_for
from swarm_api.waker import NullWaker

from .conftest import api_settings, auth_header, seed_tenant

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
API = "https://api.github.com"

#: Everything REQUIRED asks for, at exactly its access.
FULL = {req.permission: req.access for req in REQUIRED}


def _jwt() -> str:
    return "jwt." + secrets.token_hex(12)


def _classic() -> str:
    return "ghp" + "_" + secrets.token_hex(18)


def _fine_grained() -> str:
    return "github" + "_pat_" + secrets.token_hex(20)


class FakeGitHub:
    """api.github.com as `forgeapp.HttpSend` sees it: a path answers a
    (status, body) pair, or raises when its answer is an exception."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.calls: list[tuple[str, str, str]] = []

    def on(self, path: str, status: int | Exception, body: Any = None) -> None:
        self.routes[path] = status if isinstance(status, Exception) else (status, body)

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None,
                 timeout: float) -> forgeapp.HttpAnswer:
        assert method == "GET", "the permissions read makes GETs only"
        assert url.startswith(API + "/")
        path = url[len(API):]
        self.calls.append((method, path, headers.get("Authorization", "")))
        answer = self.routes.get(path)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return forgeapp.HttpAnswer(404, {}, b'{"message": "Not Found"}')
        status, data = answer
        return forgeapp.HttpAnswer(status, {}, json.dumps(data).encode("utf-8"))

    def paths(self) -> list[str]:
        return [path for _, path, _ in self.calls]


def _app(github: FakeGitHub, permissions: dict[str, str] = FULL, *,
         owner_type: str = "Organization") -> None:
    github.on("/app", 200, {"id": 12345, "slug": "swarmcloud-saga",
                            "owner": {"login": "sagaxyz", "type": owner_type},
                            "permissions": {"metadata": "read", **permissions}})


def _installed(github: FakeGitHub, owner: str, repo: str, inst: int, *,
               account_type: str = "Organization") -> None:
    github.on(f"/repos/{owner}/{repo}/installation", 200,
              {"id": inst, "account": {"login": owner, "type": account_type}})


def _installation(github: FakeGitHub, inst: int, granted: dict[str, str]) -> None:
    github.on(f"/app/installations/{inst}", 200,
              {"id": inst, "permissions": {"metadata": "read", **granted}})


def _service(github: FakeGitHub, clock: list[float] | None = None,
             jwt: str | None = None) -> ForgePermissions:
    value = jwt or _jwt()
    held = clock if clock is not None else [0.0]
    return ForgePermissions(app_jwt=lambda: value, app_slug="swarmcloud-saga", send=github,
                            clock=lambda: held[0])


def _repo(owner: str, repo: str, tenant: str = "eng") -> Registered:
    return Registered(repo_id=repo_id_for(tenant, owner, repo), owner=owner, repo=repo,
                      default_branch="main")


def _read(service: ForgePermissions, repos: list[Registered], *, token: str | None = None,
          is_admin: bool = True, refresh: bool = False) -> dict[str, Any]:
    def held() -> str:
        if token is None:
            raise AssertionError("no token-backed repository in this test")
        return token

    return service.read("eng", repositories=lambda: list(repos), token=held, now=T0,
                        is_admin=is_admin, refresh=refresh)


def _row(answer: dict[str, Any], repo: str) -> dict[str, Any]:
    return next(row for row in answer["repositories"] if row["repo"] == repo)


def _org(answer: dict[str, Any], org: str) -> dict[str, Any]:
    return next(entry for entry in answer["orgs"] if entry["org"] == org)


# -- missing ----------------------------------------------------------------------


def test_missing_is_each_requirement_the_grant_does_not_meet():
    granted = {k: v for k, v in FULL.items() if k != "statuses"}
    gaps = missing(granted)
    assert [(g["permission"], g["access"], g["granted"]) for g in gaps] == [
        ("statuses", "read", None)]
    assert gaps[0]["needed_for"] == "merges can't see CI, so they fail"
    assert set(gaps[0]["capabilities"]) == {"merge", "ci"}


def test_missing_reads_access_by_rank():
    # write covers read and admin covers write; read does not cover write.
    granted = {**FULL, "statuses": "write", "contents": "admin", "issues": "read"}
    assert [(g["permission"], g["granted"]) for g in missing(granted)] == [("issues", "read")]
    assert missing(FULL) == []
    assert {g["permission"] for g in missing({})} == {req.permission for req in REQUIRED}
    assert {g["permission"] for g in missing(None)} == {req.permission for req in REQUIRED}


def test_required_is_served_from_the_one_table():
    github = FakeGitHub()
    _app(github)
    answer = _read(_service(github), [])
    assert answer["required"] == [req.to_api() for req in REQUIRED]
    statuses = next(r for r in answer["required"] if r["permission"] == "statuses")
    assert statuses["access"] == "read"


def test_the_apps_own_gap_is_missing_on_the_app():
    github = FakeGitHub()
    without = {k: v for k, v in FULL.items() if k != "statuses"}
    _app(github, without)
    app = _read(_service(github), [])["app"]
    assert app["configured"] is True and app["error"] is None
    assert [g["permission"] for g in app["missing"]] == ["statuses"]


# -- pending_update and the org card --------------------------------------------


def test_pending_update_when_the_app_requests_what_the_org_has_not_accepted():
    github = FakeGitHub()
    _app(github)  # the App now requests statuses: read
    _installed(github, "sagaxyz", "ai-studio", 101)
    _installation(github, 101, {k: v for k, v in FULL.items() if k != "statuses"})
    _installed(github, "example-org", "api", 202)
    _installation(github, 202, FULL)
    answer = _read(_service(github), [_repo("sagaxyz", "ai-studio"), _repo("example-org", "api")])

    saga = _org(answer, "sagaxyz")
    assert saga["pending_update"] is True
    assert saga["pending"] == [{"permission": "statuses", "requested": "read", "granted": None}]
    assert [g["permission"] for g in saga["missing"]] == ["statuses"]
    assert saga["review_url"] == (
        "https://github.com/organizations/sagaxyz/settings/installations/101/permissions/update")
    assert saga["error"] is None

    other = _org(answer, "example-org")
    assert other["pending_update"] is False and other["missing"] == []

    row = _row(answer, "sagaxyz/ai-studio")
    assert row["credential"] == "app" and row["installation_id"] == 101
    assert row["capabilities"] == {"push": True, "pull_request": True, "merge": False,
                                   "ci": False, "issues": True, "actions": True}
    assert [g["permission"] for g in row["blocked_by"]] == ["statuses"]
    assert _row(answer, "example-org/api")["capabilities"] == {
        cap: True for cap in forgeperms.CAPABILITIES}


def test_pending_is_unknown_when_the_app_could_not_be_read():
    github = FakeGitHub()
    github.on("/app", 502, {})
    _installed(github, "sagaxyz", "ai-studio", 101)
    _installation(github, 101, FULL)
    answer = _read(_service(github), [_repo("sagaxyz", "ai-studio")])
    assert "502" in answer["app"]["error"]
    org = _org(answer, "sagaxyz")
    # The grant was read, so `missing` stands; what the App requests was not.
    assert org["missing"] == [] and org["pending_update"] is None


def test_pending_ignores_what_the_installation_already_exceeds():
    assert pending({"statuses": "read"}, {"statuses": "write"}) == []
    assert pending({"issues": "write"}, {"issues": "read"}) == [
        {"permission": "issues", "requested": "write", "granted": "read"}]


def test_a_user_installation_reviews_on_the_account_form():
    github = FakeGitHub()
    _app(github)
    _installed(github, "alice-gh", "dotfiles", 303, account_type="User")
    _installation(github, 303, FULL)
    org = _org(_read(_service(github), [_repo("alice-gh", "dotfiles")]), "alice-gh")
    assert org["review_url"] == "https://github.com/settings/installations/303/permissions/update"


def test_one_installation_is_read_once_for_all_its_repositories():
    github = FakeGitHub()
    _app(github)
    for name in ("a", "b", "c"):
        _installed(github, "sagaxyz", name, 101)
    _installation(github, 101, FULL)
    answer = _read(_service(github), [_repo("sagaxyz", n) for n in ("a", "b", "c")])
    assert len(answer["orgs"]) == 1
    assert github.paths().count("/app/installations/101") == 1


# -- one org erroring -------------------------------------------------------------


@pytest.mark.parametrize("failure", [502, 403, OSError("connection reset")])
def test_one_org_erroring_leaves_the_others_answering(failure):
    github = FakeGitHub()
    _app(github)
    _installed(github, "sagaxyz", "ai-studio", 101)
    github.on("/app/installations/101", failure, {"message": "nope"})
    _installed(github, "example-org", "api", 202)
    _installation(github, 202, FULL)
    answer = _read(_service(github), [_repo("sagaxyz", "ai-studio"), _repo("example-org", "api")])

    broken = _org(answer, "sagaxyz")
    assert broken["error"] and broken["granted"] is None and broken["missing"] is None
    # The link to fix it is still there: it needs no read.
    assert broken["review_url"].endswith("/installations/101/permissions/update")
    row = _row(answer, "sagaxyz/ai-studio")
    assert row["credential"] == "app" and row["error"]
    assert row["capabilities"] == {cap: None for cap in forgeperms.CAPABILITIES}

    fine = _org(answer, "example-org")
    assert fine["error"] is None and fine["missing"] == []
    assert _row(answer, "example-org/api")["capabilities"]["merge"] is True


def test_an_installation_lookup_github_did_not_answer_is_that_repositorys_error():
    github = FakeGitHub()
    _app(github)
    github.on("/repos/sagaxyz/ai-studio/installation", 503, {})
    _installed(github, "example-org", "api", 202)
    _installation(github, 202, FULL)
    answer = _read(_service(github), [_repo("sagaxyz", "ai-studio"), _repo("example-org", "api")])
    row = _row(answer, "sagaxyz/ai-studio")
    assert row["credential"] is None and "503" in row["error"]
    assert row["capabilities"] == {cap: None for cap in forgeperms.CAPABILITIES}
    assert _row(answer, "example-org/api")["capabilities"]["push"] is True


def test_an_unconfigured_app_is_the_apps_error_and_repositories_use_the_token():
    github = FakeGitHub()

    def refuse() -> str:
        raise forgeapp.AppNotConfigured("GITHUB_APP_ID is not set in swarm-api's environment")

    token = _classic()
    service = ForgePermissions(app_jwt=refuse, app_slug="", send=github)
    github.on("/repos/example-org/api", 200, {"default_branch": "main",
                                              "permissions": {"push": True, "triage": True}})
    github.on("/repos/example-org/api/commits/main/status", 200, {"state": "success"})
    answer = _read(service, [_repo("example-org", "api")], token=token)
    assert answer["app"]["configured"] is False and "GITHUB_APP_ID" in answer["app"]["error"]
    assert answer["orgs"] == []
    assert _row(answer, "example-org/api")["credential"] == "token"


# -- token probes -------------------------------------------------------------------


def _token_repo(github: FakeGitHub, *, role: dict[str, bool] | None, status: int | Exception,
                repo_status: int = 200) -> None:
    github.on("/repos/example-org/api/installation", 404, {"message": "Not Found"})
    github.on("/repos/example-org/api", repo_status,
              {"default_branch": "trunk", "permissions": role or {}})
    github.on("/repos/example-org/api/commits/trunk/status", status, {"state": "success"})


def test_a_classic_token_that_may_push_and_read_ci_is_true_but_actions_is_unmeasured():
    github = FakeGitHub()
    _app(github)
    _token_repo(github, role={"push": True, "triage": True, "pull": True}, status=200)
    token = _classic()
    answer = _read(_service(github), [_repo("example-org", "api")], token=token)
    row = _row(answer, "example-org/api")
    assert row["credential"] == "token" and row["error"] is None
    assert row["capabilities"] == {"push": True, "pull_request": True, "merge": True,
                                   "ci": True, "issues": True, "actions": None}
    assert row["blocked_by"] == []
    # The probe reads the default branch GitHub names, not the registration's.
    assert "/repos/example-org/api/commits/trunk/status" in github.paths()
    # The token went to the repository reads only; the JWT to the App's.
    sent = {path: auth for _, path, auth in github.calls}
    assert sent["/repos/example-org/api"] == f"Bearer {token}"
    assert token not in sent["/app"] and token not in sent["/repos/example-org/api/installation"]


def test_a_read_only_token_is_false_where_measured():
    github = FakeGitHub()
    _app(github)
    _token_repo(github, role={"pull": True}, status=403)
    row = _row(_read(_service(github), [_repo("example-org", "api")], token=_classic()),
               "example-org/api")
    assert row["capabilities"] == {"push": False, "pull_request": False, "merge": False,
                                   "ci": False, "issues": False, "actions": None}
    blocked = {cap for gap in row["blocked_by"] for cap in gap["capabilities"]}
    assert blocked == {"push", "pull_request", "merge", "ci", "issues"}
    assert all(gap["permission"] is None for gap in row["blocked_by"])


def test_a_fine_grained_token_is_null_where_no_read_can_tell():
    github = FakeGitHub()
    _app(github)
    _token_repo(github, role={"push": True, "triage": True}, status=200)
    row = _row(_read(_service(github), [_repo("example-org", "api")], token=_fine_grained()),
               "example-org/api")
    assert row["capabilities"] == {"push": True, "pull_request": None, "merge": None,
                                   "ci": True, "issues": None, "actions": None}
    assert row["blocked_by"] == []


@pytest.mark.parametrize("status", [404, 502, OSError("reset")])
def test_a_status_read_that_did_not_refuse_is_null_not_false(status):
    github = FakeGitHub()
    _app(github)
    _token_repo(github, role={"push": True, "triage": True}, status=status)
    row = _row(_read(_service(github), [_repo("example-org", "api")], token=_classic()),
               "example-org/api")
    assert row["capabilities"]["ci"] is None
    assert row["capabilities"]["merge"] is None
    assert row["capabilities"]["push"] is True


def test_a_repository_the_token_cannot_see_is_false_everywhere():
    github = FakeGitHub()
    _app(github)
    _token_repo(github, role=None, status=200, repo_status=404)
    row = _row(_read(_service(github), [_repo("example-org", "api")], token=_classic()),
               "example-org/api")
    assert row["capabilities"] == {cap: False for cap in forgeperms.CAPABILITIES}
    assert "HTTP 404" in row["blocked_by"][0]["needed_for"]


def test_a_repository_read_github_did_not_answer_is_null_everywhere():
    github = FakeGitHub()
    _app(github)
    _token_repo(github, role=None, status=200, repo_status=502)
    row = _row(_read(_service(github), [_repo("example-org", "api")], token=_classic()),
               "example-org/api")
    assert row["capabilities"] == {cap: None for cap in forgeperms.CAPABILITIES}
    assert row["blocked_by"] == [] and "502" in row["error"]


def test_an_unreadable_tenant_token_is_each_token_rows_error():
    from swarm_api.forge import NoForgeCredential

    github = FakeGitHub()
    _app(github)
    _token_repo(github, role={"push": True}, status=200)
    calls = []

    def token() -> str:
        calls.append(1)
        raise NoForgeCredential("tenant 'eng' has no forge credential")

    answer = _service(github).read("eng", repositories=lambda: [_repo("example-org", "api")],
                                   token=token, now=T0, is_admin=False)
    row = _row(answer, "example-org/api")
    assert row["credential"] == "token" and "no forge credential" in row["error"]
    assert row["capabilities"] == {cap: None for cap in forgeperms.CAPABILITIES}
    assert calls == [1]


# -- admin-only settings_url ----------------------------------------------------------


def test_settings_url_is_served_to_an_admin_and_never_to_a_member():
    github = FakeGitHub()
    _app(github)
    clock = [0.0]
    service = _service(github, clock)
    admin = _read(service, [], is_admin=True)
    assert admin["app"]["settings_url"] == (
        "https://github.com/organizations/sagaxyz/settings/apps/swarmcloud-saga/permissions")
    # Served from the cache the admin's read filled: still stripped.
    member = _read(service, [], is_admin=False)
    assert member["cached"] is True and member["app"]["settings_url"] is None
    # And the admin's next read still has it: the cache was not mutated.
    assert _read(service, [], is_admin=True)["app"]["settings_url"]


def test_a_user_owned_app_settings_url_is_the_accounts_form():
    github = FakeGitHub()
    _app(github, owner_type="User")
    app = _read(_service(github), [], is_admin=True)["app"]
    assert app["settings_url"] == "https://github.com/settings/apps/swarmcloud-saga/permissions"


# -- the cache ----------------------------------------------------------------------


def test_the_answer_is_cached_for_a_minute_and_refresh_reads_again():
    github = FakeGitHub()
    _app(github)
    _installed(github, "sagaxyz", "ai-studio", 101)
    _installation(github, 101, {k: v for k, v in FULL.items() if k != "statuses"})
    clock = [100.0]
    service = _service(github, clock)
    repos = [_repo("sagaxyz", "ai-studio")]

    first = _read(service, repos)
    assert first["cached"] is False and _org(first, "sagaxyz")["pending_update"] is True
    reads = len(github.calls)

    # The org accepts on GitHub; within the minute the cache still answers.
    _installation(github, 101, FULL)
    clock[0] = 159.0
    again = _read(service, repos)
    assert again["cached"] is True and len(github.calls) == reads
    assert _org(again, "sagaxyz")["pending_update"] is True

    # refresh=true reads again, at once.
    fresh = _read(service, repos, refresh=True)
    assert fresh["cached"] is False and len(github.calls) > reads
    assert _org(fresh, "sagaxyz")["pending_update"] is False

    # And past the minute the cache expires on its own.
    reads = len(github.calls)
    clock[0] = 159.0 + forgeperms.CACHE_SECONDS
    assert _read(service, repos)["cached"] is False and len(github.calls) > reads


def test_the_cache_is_per_tenant():
    github = FakeGitHub()
    _app(github)
    service = _service(github)
    service.read("eng", repositories=lambda: [], token=lambda: "", now=T0, is_admin=False)
    other = service.read("research", repositories=lambda: [], token=lambda: "", now=T0,
                         is_admin=False)
    assert other["cached"] is False and other["tenant_id"] == "research"


# -- the route ----------------------------------------------------------------------


class FakeTokens:
    def __init__(self, value: str) -> None:
        self.value = value
        self.reads: list[str] = []

    def token_for(self, tenant: Any, *, person=None) -> str:
        self.reads.append(tenant.tenant_id)
        return self.value


class FakeAppSecret:
    """The App's key, a PEM built at runtime."""

    def __init__(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = key.private_bytes(serialization.Encoding.PEM,
                                     serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()).decode("ascii")

    def private_key(self) -> str:
        return self.pem

    def client_secret(self) -> str:  # pragma: no cover - not read by this route
        raise forgeapp.AppNotConfigured("not used here")


def _seed_repository(db, tenant: str, owner: str, repo: str) -> None:
    repo_id = repo_id_for(tenant, owner, repo)
    db.collection("repositories").document(repo_id).set({
        "repo_id": repo_id, "tenant_id": tenant, "forge": "github", "owner": owner,
        "repo": repo, "default_branch": "main"})


@pytest.fixture
def route(db, tokens, group_map, objects):
    github = FakeGitHub()
    forge_tokens = FakeTokens(_classic())
    seed_tenant(db, "eng", credentials=("git",))
    ctx = build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(), waker=NullWaker(),
        metrics=ApiMetrics(), objects=objects, forge_tokens=forge_tokens,
        now=lambda: T0)
    service = forgeapp.ForgeApp(
        db, config=forgeapp.AppConfig(client_id="Iv23" + "abcdefgh12", app_id="12345",
                                      slug="swarmcloud-saga"),
        secrets=FakeAppSecret(), slots=None, send=github, now=lambda: T0)
    client = TestClient(create_app(ctx, forge_app=service), raise_server_exceptions=False)
    return client, github, forge_tokens


def test_the_route_answers_the_callers_tenant_with_the_apps_jwt(route, db):
    client, github, forge_tokens = route
    _app(github, {k: v for k, v in FULL.items() if k != "statuses"})
    _seed_repository(db, "eng", "sagaxyz", "ai-studio")
    _seed_repository(db, "eng", "example-org", "api")
    _seed_repository(db, "research", "other-org", "secret")
    _installed(github, "sagaxyz", "ai-studio", 101)
    _installation(github, 101, FULL)
    _token_repo(github, role={"push": True, "triage": True}, status=200)
    github.on("/repos/example-org/api/commits/main/status", 200, {})

    member = client.get("/v1/forge/permissions", headers=auth_header("alice"))
    assert member.status_code == 200, member.text
    body = member.json()
    assert body["tenant_id"] == "eng"
    assert {row["repo"] for row in body["repositories"]} == {"sagaxyz/ai-studio",
                                                            "example-org/api"}
    assert body["app"]["settings_url"] is None
    assert [g["permission"] for g in body["app"]["missing"]] == ["statuses"]
    # The org accepted more than the App now asks for: nothing pending.
    assert _org(body, "sagaxyz")["pending_update"] is False
    assert forge_tokens.reads == ["eng"]
    # The App's calls carried a signed JWT (three dot-separated parts).
    app_auth = next(auth for _, path, auth in github.calls if path == "/app")
    assert app_auth.startswith("Bearer ") and app_auth.count(".") == 2
    # Nothing in the answer is the token or the JWT.
    assert forge_tokens.value not in member.text and app_auth[7:] not in member.text

    admin = client.get("/v1/forge/permissions", headers=auth_header("root"))
    assert admin.json()["cached"] is True
    assert admin.json()["app"]["settings_url"].endswith("/apps/swarmcloud-saga/permissions")

    calls = len(github.calls)
    fresh = client.get("/v1/forge/permissions?refresh=true", headers=auth_header("alice"))
    assert fresh.json()["cached"] is False and len(github.calls) > calls


# -- the docs ---------------------------------------------------------------------


def test_the_onboarding_doc_lists_every_requirement_at_its_access():
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[3] / "docs" / "onboarding.md").read_text("utf-8")
    section = doc[doc.index("### 2.6 GitHub permissions health"):]
    section = section[:section.index("\n---")]
    rows = {line.split("|")[1].strip().strip("`"): line.split("|")[2].strip()
            for line in section.splitlines() if line.startswith("| `")}
    assert rows == {req.permission: req.access for req in REQUIRED}
