"""GitHub Actions secrets, written write-only (SSC-ACTIONS-API, part of #2158).

Design: docs/design/secrets-scopes.md §3 and §6.2, with BGD's answers in its
§11: the EXISTING App mints (Q3), SwarmCloud never creates, edits or deletes
an environment (Q4), swarm-api seals with PyNaCl (Q5), and the writer is a
workspace manager whom GitHub calls admin of the repository (Q6).

The attackers these tests stand in for:

  * A MEMBER GITHUB CALLS ADMIN BUT WHO DOES NOT MANAGE THE WORKSPACE, and a
    MANAGER GITHUB DOES NOT CALL ADMIN: each refused, and GitHub's secret
    endpoints are never called.
  * A CODE PATH THAT ASKS THE APP FOR MORE: every mint is recorded, and each
    must name one repository and one permission, never `administration`.
  * A LEAK: the value, its ciphertext and every token are built at runtime
    and searched for in every response, log line and Firestore document.
  * A WRITE INTO AN ENVIRONMENT ANY BRANCH CAN DEPLOY, refused unless the
    caller types its name.

Offline: FakeFirestore, the real `InstallationTokens` over a fake GitHub
transport (which holds a real Curve25519 key pair, so it can open what was
sealed), and the admin check's fakes. Every token is built at runtime.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote, urlparse

import pytest
from fastapi.testclient import TestClient
from nacl.public import PrivateKey, SealedBox

from swarm_api.actions_secrets import (
    ENVIRONMENT_MISSING,
    KEY_ROTATED,
    LINKS_COLLECTION,
    NOT_MAIN_ONLY,
    RULE_ANY,
    RULE_CUSTOM,
    RULE_MAIN_ONLY,
    RULE_PROTECTED,
    WRITES_COLLECTION,
    ActionsSecrets,
)
from swarm_api.auth import TENANT_HEADER, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.forge import GitHubIssues
from swarm_api.forge_installation import (
    ACTIONS_PERMISSIONS,
    InstallationTokens,
)
from swarm_api.forgeapp import HttpAnswer
from swarm_api.forgeperms import ACTIONS_SECRETS_REQUIREMENTS, REQUIRED, review_url
from swarm_api.github_repo_admin import COPY, NOT_ADMIN, UNAVAILABLE
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.repositories import repo_id_for
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, api_settings, auth_header, seed_tenant
from .test_github_repo_admin import FakeAccess, FakeGitHub

ROOT = "root@example.com"
DAVE = "dave@example.com"
REPO_ID = repo_id_for("eng", "acme", "widgets")
BASE = f"/v1/repositories/{REPO_ID}/actions"
INSTALLATION = 4242


def runtime_value() -> str:
    """Shaped like a private key's body, built at runtime."""
    return "pk" + "-" + secrets.token_hex(24)


class FakeActionsGitHub:
    """api.github.com for one installation of acme/widgets: the mint, the
    public keys, the secrets, and the environments (which it never lets
    anyone write). Records every request."""

    def __init__(self, *, granted: dict[str, str] | None = None) -> None:
        self.granted = granted if granted is not None else {
            "metadata": "read", "contents": "write", "actions": "read",
            "secrets": "write", "environments": "write"}
        self.private = PrivateKey.generate()
        self.key_id = "kid-" + secrets.token_hex(4)
        self.rotate_on_put = 0
        self.calls: list[tuple[str, str, str]] = []
        self.mints: list[dict[str, Any]] = []
        self.tokens: list[str] = []
        self.jwt = secrets.token_hex(24)
        self.secrets: dict[tuple[str | None, str], bytes] = {}
        self.ciphertexts: list[str] = []
        self.environments: dict[str, dict[str, Any]] = {}

    def app_jwt(self) -> str:
        return self.jwt

    def environment(self, name: str, *, policy: dict[str, bool] | None,
                    branches: list[dict[str, str]] = ()) -> None:
        self.environments[name] = {"policy": policy, "branches": list(branches)}

    def _rotate(self) -> None:
        self.private = PrivateKey.generate()
        self.key_id = "kid-" + secrets.token_hex(4)

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None,
                 timeout: float) -> HttpAnswer:
        path = urlparse(url).path
        query = urlparse(url).query
        self.calls.append((method, path + (f"?{query}" if query else ""),
                           headers.get("Authorization", "")))
        if path == f"/app/installations/{INSTALLATION}/access_tokens":
            asked = json.loads(body or b"{}")
            self.mints.append(asked)
            if any(self.granted.get(p) not in (a, "write") for p, a in
                   asked.get("permissions", {}).items()):
                return _answer(422, {"message": "The permissions requested are not granted "
                                                "to this installation."})
            token = secrets.token_hex(20)
            self.tokens.append(token)
            expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            return _answer(201, {"token": token, "expires_at": expires})
        if headers.get("Authorization") not in {f"Bearer {t}" for t in self.tokens}:
            return _answer(401, {"message": "Bad credentials"})
        parts = [unquote(p) for p in path.split("/")]
        assert parts[1:4] == ["repos", "acme", "widgets"], path
        rest = parts[4:]
        env: str | None = None
        if rest[:1] == ["environments"]:
            if len(rest) == 1:
                return _answer(200, {"total_count": len(self.environments), "environments": [
                    {"name": n} for n in self.environments]})
            env = rest[1]
            if env not in self.environments:
                return _answer(404, {"message": "Not Found"})
            spec = self.environments[env]
            if len(rest) == 2 and method == "GET":
                return _answer(200, {"name": env, "deployment_branch_policy": spec["policy"]})
            if rest[2:] == ["deployment-branch-policies"] and method == "GET":
                return _answer(200, {"total_count": len(spec["branches"]),
                                     "branch_policies": spec["branches"]})
            assert rest[2] == "secrets", f"an environment write: {method} {path}"
            rest = ["actions", *rest[2:]]
        assert rest[:2] == ["actions", "secrets"], path
        if rest[2:] == ["public-key"]:
            return _answer(200, {"key_id": self.key_id, "key": base64.b64encode(
                bytes(self.private.public_key)).decode()})
        if len(rest) == 2:
            return _answer(200, {"total_count": 1, "secrets": [
                {"name": n, "created_at": "2026-10-24T00:00:00Z",
                 "updated_at": "2026-10-24T00:00:00Z"}
                for (e, n) in self.secrets if e == env]})
        name = rest[2]
        if method == "DELETE":
            if self.secrets.pop((env, name), None) is None:
                return _answer(404, {"message": "Not Found"})
            return _answer(204, None)
        assert method == "PUT", path
        data = json.loads(body or b"{}")
        assert set(data) == {"encrypted_value", "key_id"}
        if self.rotate_on_put:
            self.rotate_on_put -= 1
            self._rotate()
        if data["key_id"] != self.key_id:
            return _answer(422, {"message": "Bad request - key_id"})
        self.ciphertexts.append(data["encrypted_value"])
        plain = SealedBox(self.private).decrypt(base64.b64decode(data["encrypted_value"]))
        created = (env, name) not in self.secrets
        self.secrets[(env, name)] = plain
        return _answer(201 if created else 204, None)

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.calls if m in ("PUT", "POST", "PATCH", "DELETE")
                and "/access_tokens" not in p]


def _answer(status: int, body: Any) -> HttpAnswer:
    return HttpAnswer(status, {}, json.dumps(body).encode() if body is not None else b"")


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    """dave a member of eng, bob of research, root a platform admin (a manager)."""
    return {DAVE: (ENG_GROUP,), "bob@example.com": (RESEARCH_GROUP,),
            ROOT: (ADMIN_GROUP, ENG_GROUP)}


@pytest.fixture
def world(db, tokens, group_map, objects):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    db.docs[f"repositories/{REPO_ID}"] = {
        "repo_id": REPO_ID, "tenant_id": "eng", "forge": "github", "owner": "acme",
        "repo": "widgets", "repository_url": "https://github.com/acme/widgets",
        "default_branch": "main"}
    db.docs["forge_orgs/eng-acme"] = {
        "tenant_id": "eng", "owner": "acme", "owner_type": "Organization", "method": "app",
        "install_state": "installed", "installation_id": INSTALLATION}

    def make(github: FakeActionsGitHub, *, admin: bool = True,
             access: FakeAccess | None = None) -> TestClient:
        ctx = build_context(
            settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
            groups=StaticGroups(group_map), credentials=InMemoryCredentials(),
            waker=NullWaker(), metrics=ApiMetrics(), objects=objects,
            forge=GitHubIssues(send=FakeGitHub(admin=admin)), forge_installations=None)
        app = create_app(ctx)
        app.state.access = access or FakeAccess()
        mint = InstallationTokens(db, app_jwt=github.app_jwt, send=github)
        app.state.actions_secrets = ActionsSecrets(db, mint=mint, send=github, now=ctx.now,
                                                   app_slug="swarmcloud")
        return TestClient(app, raise_server_exceptions=False)

    return make


def as_(who: str, tenant: str = "eng") -> dict[str, str]:
    return {**auth_header(who), TENANT_HEADER: tenant}


def put(client: TestClient, value: str, name: str = "MERGE_APP_PRIVATE_KEY", who: str = "root",
        **body: Any):
    return client.put(f"{BASE}/secrets/{name}", headers=as_(who), json={"value": value, **body})


def everything(db, caplog, *responses) -> str:
    return "\n".join([caplog.text, repr(db.docs), *(r.text for r in responses)])


# --------------------------------------------------------------------------
# the write, sealed, and nothing kept
# --------------------------------------------------------------------------


def test_a_repository_secret_is_sealed_to_githubs_key_and_put(world, db):
    github = FakeActionsGitHub()
    value = runtime_value()
    response = put(world(github), value)
    assert response.status_code == 200, response.text
    assert github.secrets == {(None, "MERGE_APP_PRIVATE_KEY"): value.encode()}
    assert response.json()["created"] is True
    [record] = [d for p, d in db.docs.items() if p.startswith(f"{WRITES_COLLECTION}/")]
    assert set(record) == {"repo_id", "tenant_id", "repository", "environment", "name",
                           "set_by", "set_at", "github_status", "state"}
    assert (record["name"], record["set_by"], record["state"]) == \
        ("MERGE_APP_PRIVATE_KEY", ROOT, "set")
    audit = [d for p, d in db.docs.items() if p.startswith("admin_audit/")]
    assert [a["action"] for a in audit] == ["actions_secret_set"]


def test_no_response_log_line_or_document_carries_a_value_a_ciphertext_or_a_token(
        world, db, caplog):
    caplog.set_level(logging.DEBUG)
    github = FakeActionsGitHub()
    github.environment("repo-secrets", policy={"protected_branches": False,
                                               "custom_branch_policies": True},
                       branches=[{"name": "main", "type": "branch"}])
    client = world(github)
    value, other = runtime_value(), runtime_value()
    first = put(client, value)
    second = put(client, other, environment="repo-secrets")
    listed = client.get(f"{BASE}/secrets", headers=as_("dave"))
    assert (first.status_code, second.status_code, listed.status_code) == (200, 200, 200)
    seen = everything(db, caplog, first, second, listed)
    assert github.ciphertexts and github.tokens
    for leaked in (value, other, *github.ciphertexts, *github.tokens, github.jwt):
        assert leaked not in seen


def test_an_environment_secret_goes_to_that_environment(world, db):
    github = FakeActionsGitHub()
    github.environment("repo-secrets", policy={"protected_branches": False,
                                               "custom_branch_policies": True},
                       branches=[{"name": "main", "type": "branch"}])
    value = runtime_value()
    response = put(world(github), value, environment="repo-secrets")
    assert response.status_code == 200, response.text
    assert response.json()["rule"] == RULE_MAIN_ONLY
    assert github.secrets == {("repo-secrets", "MERGE_APP_PRIVATE_KEY"): value.encode()}


def test_a_stale_key_is_resealed_once_and_a_second_rotation_is_409(world, db):
    github = FakeActionsGitHub()
    github.rotate_on_put = 1
    client = world(github)
    value = runtime_value()
    assert put(client, value).status_code == 200
    assert github.secrets[(None, "MERGE_APP_PRIVATE_KEY")] == value.encode()
    github.rotate_on_put = 2
    response = put(client, runtime_value())
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == KEY_ROTATED
    assert github.secrets[(None, "MERGE_APP_PRIVATE_KEY")] == value.encode()


@pytest.mark.parametrize("name", ["GITHUB_TOKEN", "github_x", "1KEY", "A-B", "A B"])
def test_a_name_github_refuses_is_422_before_anything_is_asked(world, name):
    github = FakeActionsGitHub()
    response = put(world(github), runtime_value(), name=name)
    assert response.status_code == 422, response.text
    assert github.calls == []


@pytest.mark.parametrize("environment", ["..", ".", "a/b", "x\ty", "x\x7fy"])
def test_an_environment_name_that_could_change_the_path_is_422(world, environment):
    github = FakeActionsGitHub()
    response = put(world(github), runtime_value(), environment=environment)
    assert response.status_code == 422, response.text
    assert github.calls == []


def test_the_value_is_not_the_name_and_not_empty(world):
    github = FakeActionsGitHub()
    client = world(github)
    assert put(client, "MERGE_APP_PRIVATE_KEY").status_code == 422
    assert put(client, "").status_code == 422
    assert github.secrets == {}


# --------------------------------------------------------------------------
# who (Q6)
# --------------------------------------------------------------------------


def test_a_manager_github_does_not_call_admin_is_refused(world, db):
    github = FakeActionsGitHub()
    response = put(world(github, admin=False), runtime_value())
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["reason"] == NOT_ADMIN
    assert github.calls == [] and github.secrets == {}


def test_a_github_admin_who_does_not_manage_the_workspace_is_refused(world, db):
    github = FakeActionsGitHub()
    response = put(world(github, admin=True), runtime_value(), who="dave")
    assert response.status_code == 403, response.text
    assert github.calls == [] and github.secrets == {}


def test_another_workspaces_repository_is_404(world):
    github = FakeActionsGitHub()
    client = world(github)
    response = client.put(f"{BASE}/secrets/X_KEY", headers=as_("bob", "research"),
                          json={"value": runtime_value()})
    assert response.status_code == 404, response.text
    assert github.calls == []


# --------------------------------------------------------------------------
# the mint (Q3)
# --------------------------------------------------------------------------


def test_each_mint_names_one_repository_and_one_permission_never_administration(world):
    github = FakeActionsGitHub()
    github.environment("repo-secrets", policy={"protected_branches": False,
                                               "custom_branch_policies": True},
                       branches=[{"name": "main", "type": "branch"}])
    client = world(github)
    assert put(client, runtime_value()).status_code == 200
    assert put(client, runtime_value(), environment="repo-secrets").status_code == 200
    assert client.get(f"{BASE}/secrets", headers=as_("dave")).status_code == 200
    assert github.mints
    allowed = [dict(p) for p in ACTIONS_PERMISSIONS.values()]
    for mint in github.mints:
        assert mint["repositories"] == ["widgets"]
        assert len(mint["permissions"]) == 1 and mint["permissions"] in allowed
    asked = {p: a for m in github.mints for p, a in m["permissions"].items()}
    assert set(asked) <= {"secrets", "environments", "actions"}
    assert "administration" not in asked
    # A repository write asks Secrets: write and nothing else.
    assert {"repositories": ["widgets"], "permissions": {"secrets": "write"}} in github.mints


def test_the_permission_table_asks_no_more_than_the_writes_need():
    asked = {(p, a) for perms in ACTIONS_PERMISSIONS.values() for p, a in perms.items()}
    assert asked <= {("secrets", "write"), ("secrets", "read"), ("environments", "write"),
                     ("environments", "read"), ("actions", "read")}
    assert all(len(perms) == 1 for perms in ACTIONS_PERMISSIONS.values())
    # What the runbook asks BGD to grant is exactly the writes' permissions,
    # and the rule read needs nothing beyond what REQUIRED already asks.
    assert {(r.permission, r.access) for r in ACTIONS_SECRETS_REQUIREMENTS} == \
        {("secrets", "write"), ("environments", "write")}
    assert ("actions", "read") in {(r.permission, r.access) for r in REQUIRED}


def test_an_installation_without_secrets_granted_is_refused_with_its_review_link(world, db):
    github = FakeActionsGitHub(granted={"metadata": "read", "actions": "read"})
    response = put(world(github), runtime_value())
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "secrets_not_granted"
    assert detail["review_url"] == review_url("acme", INSTALLATION, "Organization")
    assert github.writes() == [] and github.secrets == {}
    assert not [p for p in db.docs if p.startswith(f"{WRITES_COLLECTION}/")]


def test_an_owner_with_no_installation_is_refused_with_the_install_link(world, db):
    del db.docs["forge_orgs/eng-acme"]
    github = FakeActionsGitHub()
    response = put(world(github), runtime_value())
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["install_url"] == \
        "https://github.com/apps/swarmcloud/installations/new"
    assert github.calls == []


# --------------------------------------------------------------------------
# environments (Q4): read, never written
# --------------------------------------------------------------------------


def test_no_route_creates_edits_or_deletes_an_environment(world):
    """Every route the app mounts, read from its OpenAPI document: under a
    repository's `actions/environments`, GET only."""
    client = world(FakeActionsGitHub())
    paths = client.get("/openapi.json").json()["paths"]
    routes = {p: sorted(m.upper() for m in ops) for p, ops in paths.items()
              if p.startswith("/v1/repositories/{repo_id}/actions")}
    assert "/v1/repositories/{repo_id}/actions/secrets/{name}" in routes
    for path, methods in routes.items():
        if "/environments" in path:
            assert methods == ["GET"], (path, methods)
    assert "/v1/repositories/{repo_id}/actions/environments" not in routes


def test_no_github_call_writes_an_environment(world):
    github = FakeActionsGitHub()
    github.environment("open", policy=None)
    client = world(github)
    put(client, runtime_value(), environment="open", confirm_environment="open")
    put(client, runtime_value(), environment="missing")
    client.request("DELETE", f"{BASE}/secrets/MERGE_APP_PRIVATE_KEY", headers=as_("root"),
                   json={"confirm_name": "MERGE_APP_PRIVATE_KEY", "environment": "open"})
    for method, path in github.writes():
        assert "/secrets/" in path, (method, path)


@pytest.mark.parametrize("policy, branches, rule", [
    ({"protected_branches": False, "custom_branch_policies": True},
     [{"name": "main", "type": "branch"}], RULE_MAIN_ONLY),
    (None, [], RULE_ANY),
    ({"protected_branches": True, "custom_branch_policies": False}, [], RULE_PROTECTED),
    ({"protected_branches": False, "custom_branch_policies": True},
     [{"name": "main", "type": "branch"}, {"name": "release/*", "type": "branch"}], RULE_CUSTOM),
    ({"protected_branches": False, "custom_branch_policies": True},
     [{"name": "*", "type": "branch"}], RULE_CUSTOM),
    ({"protected_branches": False, "custom_branch_policies": True},
     [{"name": "main", "type": "tag"}], RULE_CUSTOM),
])
def test_check_rule_tells_main_only_from_any_branch(world, policy, branches, rule):
    github = FakeActionsGitHub()
    github.environment("repo-secrets", policy=policy, branches=branches)
    response = world(github).get(f"{BASE}/environments/repo-secrets/rule", headers=as_("dave"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["exists"], body["rule"], body["main_only"]) == \
        (True, rule, rule == RULE_MAIN_ONLY)
    assert github.writes() == []


def test_a_missing_environment_is_answered_with_githubs_create_page(world):
    github = FakeActionsGitHub()
    client = world(github)
    rule = client.get(f"{BASE}/environments/repo-secrets/rule", headers=as_("dave"))
    assert rule.status_code == 200, rule.text
    assert rule.json()["exists"] is False
    assert rule.json()["create_url"] == \
        "https://github.com/acme/widgets/settings/environments/new"
    write = put(client, runtime_value(), environment="repo-secrets")
    assert write.status_code == 409, write.text
    assert write.json()["detail"]["reason"] == ENVIRONMENT_MISSING
    assert github.writes() == []


def test_an_any_branch_write_without_the_typed_name_is_refused(world, db):
    github = FakeActionsGitHub()
    github.environment("open", policy=None)
    client = world(github)
    value = runtime_value()
    refused = put(client, value, environment="open")
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["reason"] == NOT_MAIN_ONLY
    mistyped = put(client, value, environment="open", confirm_environment="Open")
    assert mistyped.status_code == 409
    assert github.secrets == {}
    typed = put(client, value, environment="open", confirm_environment="open")
    assert typed.status_code == 200, typed.text
    assert github.secrets == {("open", "MERGE_APP_PRIVATE_KEY"): value.encode()}


# --------------------------------------------------------------------------
# the one-time link (Q8)
# --------------------------------------------------------------------------


def test_a_link_is_consumed_once(world, db):
    github = FakeActionsGitHub()
    client = world(github)
    minted = client.post(f"{BASE}/links", headers=as_("root"),
                         json={"name": "MERGE_APP_PRIVATE_KEY"})
    assert minted.status_code == 201, minted.text
    token = minted.json()["path"].rsplit("&t=", 1)[1]
    assert token not in repr(db.docs)
    [link] = [d for p, d in db.docs.items() if p.startswith(f"{LINKS_COLLECTION}/")]
    assert link["email"] == ROOT and link["repo_id"] == REPO_ID
    first = put(client, runtime_value(), link_token=token)
    assert first.status_code == 200, first.text
    assert first.json()["via_link"] is True
    again = put(client, runtime_value(), link_token=token)
    assert again.status_code == 403, again.text
    assert token not in again.text


def test_a_link_for_another_secret_is_refused(world):
    github = FakeActionsGitHub()
    client = world(github)
    minted = client.post(f"{BASE}/links", headers=as_("root"), json={"name": "OTHER_KEY"})
    token = minted.json()["path"].rsplit("&t=", 1)[1]
    response = put(client, runtime_value(), link_token=token)
    assert response.status_code == 403, response.text
    assert github.secrets == {}


# --------------------------------------------------------------------------
# list and delete
# --------------------------------------------------------------------------


def test_the_list_says_which_secrets_swarmcloud_set(world, db):
    github = FakeActionsGitHub()
    github.secrets[(None, "SET_ON_GITHUB")] = b"x"
    client = world(github)
    assert put(client, runtime_value()).status_code == 200
    listed = client.get(f"{BASE}/secrets", headers=as_("dave"))
    assert listed.status_code == 200, listed.text
    rows = {r["name"]: r for r in listed.json()["repository_secrets"]}
    assert rows["MERGE_APP_PRIVATE_KEY"]["set_by_swarmcloud"] is True
    assert rows["MERGE_APP_PRIVATE_KEY"]["set_by"] == ROOT
    assert rows["SET_ON_GITHUB"]["set_by_swarmcloud"] is False


def test_a_delete_needs_the_typed_name(world, db):
    github = FakeActionsGitHub()
    client = world(github)
    assert put(client, runtime_value()).status_code == 200
    wrong = client.request("DELETE", f"{BASE}/secrets/MERGE_APP_PRIVATE_KEY",
                           headers=as_("root"), json={"confirm_name": "MERGE_APP"})
    assert wrong.status_code == 422 and github.secrets
    gone = client.request("DELETE", f"{BASE}/secrets/MERGE_APP_PRIVATE_KEY",
                          headers=as_("root"), json={"confirm_name": "MERGE_APP_PRIVATE_KEY"})
    assert gone.status_code == 200, gone.text
    assert github.secrets == {}
    [record] = [d for p, d in db.docs.items() if p.startswith(f"{WRITES_COLLECTION}/")]
    assert record["state"] == "deleted"


# --------------------------------------------------------------------------
# the list says who may write, names its repository, links GitHub's create page
# (SSC-ACTIONS-API-2, part of #2157)
# --------------------------------------------------------------------------


def listing(client: TestClient, who: str = "root"):
    response = client.get(f"{BASE}/secrets", headers=as_(who))
    assert response.status_code == 200, response.text
    return response.json()


def test_the_list_serves_can_write_for_a_manager_github_calls_admin(world):
    access = FakeAccess()
    body = listing(world(FakeActionsGitHub(), admin=True, access=access))
    assert body["can_write"] is True
    assert body["cannot_write_because"] is None
    assert datetime.fromisoformat(body["checked_at"]).tzinfo is not None
    assert len(access.callers) == 1, "GitHub is asked, live, with the caller's own token"


def test_the_list_says_why_a_manager_github_does_not_call_admin_cannot_write(world):
    body = listing(world(FakeActionsGitHub(), admin=False))
    assert body["can_write"] is False
    assert body["cannot_write_because"] == COPY[NOT_ADMIN]
    assert body["checked_at"] is not None


def test_the_list_says_why_a_member_who_is_not_a_manager_cannot_write(world):
    access = FakeAccess()
    body = listing(world(FakeActionsGitHub(), admin=True, access=access), who="dave")
    assert body["can_write"] is False
    assert "only its admins" in body["cannot_write_because"]
    # The manager rule fails first, so GitHub is not asked about this person.
    assert body["checked_at"] is None and access.callers == []


def test_a_failing_github_check_is_can_write_false_and_the_read_still_succeeds(world):
    github = FakeActionsGitHub()
    github.secrets[(None, "SET_ON_GITHUB")] = b"x"
    body = listing(world(github, access=FakeAccess(error=RuntimeError("github is down"))))
    assert body["can_write"] is False
    assert body["cannot_write_because"] == COPY[UNAVAILABLE]
    assert [r["name"] for r in body["repository_secrets"]] == ["SET_ON_GITHUB"]


def test_the_write_routes_never_read_can_write_from_the_caller(world):
    github = FakeActionsGitHub()
    response = put(world(github, admin=False), runtime_value(), can_write=True)
    assert response.status_code == 422, response.text
    refused = put(world(github, admin=False), runtime_value())
    assert refused.status_code == 403, refused.text
    assert github.secrets == {}


def test_repository_is_owner_repo_and_its_secrets_are_repository_secrets(world):
    github = FakeActionsGitHub()
    github.secrets[(None, "SET_ON_GITHUB")] = b"x"
    body = listing(world(github), who="dave")
    assert body["repository"] == "acme/widgets"
    assert [r["name"] for r in body["repository_secrets"]] == ["SET_ON_GITHUB"]


def test_the_list_serves_githubs_create_page_on_a_repository_with_no_environment(world):
    body = listing(world(FakeActionsGitHub()), who="dave")
    assert body["environments"] == []
    assert body["create_url"] == "https://github.com/acme/widgets/settings/environments/new"


# --------------------------------------------------------------------------
# the runbook says what the code asks for
# --------------------------------------------------------------------------


def test_the_runbook_names_every_permission_the_writes_need_and_not_administration():
    from pathlib import Path

    from swarm_api.actions_secrets import RUNBOOK

    path, anchor = RUNBOOK.split("#")
    text = (Path(__file__).resolve().parents[3] / path).read_text(encoding="utf-8")
    heading = "## Writing GitHub Actions secrets"
    assert anchor == "writing-github-actions-secrets" and heading in text
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    for requirement in ACTIONS_SECRETS_REQUIREMENTS:
        assert f"**{requirement.permission.capitalize()}** to **Read and write**" in section
    assert "Leave **Administration** at **No access**" in section
