"""`GET /v1/forge/permissions`: which GitHub permissions SwarmCloud needs,
which the App and each connected org's installation hold, and what each
registered repository can do (#1086, lane PERM-1).

What is held here, offline against forge fakes:

  * an org that has not accepted a permission the App requests has a gap
    whose fix is that installation's permission-update review page; a fully
    granted org has none;
  * a permission the App itself does not request is an App gap, fixed on the
    App's permissions settings page -- a URL sent to an admin only, and
    absent from anyone else's response;
  * each gap says in plain words what breaks (statuses: "merges can't see
    CI, so they fail", the 2026-10-11 ai-studio failure);
  * an installation-backed repository takes its org's gaps; a token-backed
    one reports what the token probe measured;
  * `refresh=true` reads GitHub again, so an accepted permission turns the
    card and the dots green; without it a short cache answers;
  * GitHub unreachable, the key missing or refused, the App not configured:
    `unknown` with a fixed reason, never ok, never a 500, never GitHub's body;
  * no JWT, key or token reaches the response or a log line;
  * the tenant comes from the caller's token, never the query.

Every key and token is built at runtime.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import forgeapp
from swarm_api.access import AccessService, org_id_for
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.forgeperms import REQUIRED, ForgePermissions, gaps
from swarm_api.gittokens import (
    CAPABILITIES,
    CHECKS_COLLECTION,
    COLLECTION as TOKENS_COLLECTION,
    Scope,
    TokenState,
    record_for_slot,
)
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.repositories import repo_id_for
from swarm_api.waker import NullWaker

from .conftest import NoForgeTokens, api_settings, auth_header, seed_tenant
from .forge_fakes import AppGitHub, AppKey, app_key

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
ROUTE = "/v1/forge/permissions"

#: Everything SwarmCloud asks for (docs/runbooks/github-app.md D8).
FULL = {"metadata": "read", "contents": "write", "pull_requests": "write", "checks": "read",
        "statuses": "read", "issues": "write"}
WITHOUT_STATUSES = {k: v for k, v in FULL.items() if k != "statuses"}
STATUSES_BREAK = "merges can't see CI, so they fail"


class Ticker:
    """A monotonic clock a test moves by hand."""

    def __init__(self) -> None:
        self.at = 1000.0

    def __call__(self) -> float:
        return self.at


@pytest.fixture(scope="module")
def key() -> tuple[str, Any]:
    return app_key()


@pytest.fixture
def ticker() -> Ticker:
    return Ticker()


@pytest.fixture
def github(key) -> AppGitHub:
    return AppGitHub(key[1], requested=FULL)


@pytest.fixture
def app_secret(key) -> AppKey:
    return AppKey(key[0])


def build(db, tokens, group_map, objects, github, app_secret, ticker, *,
          app_id: str = "12345", slug: str = "swarmcloud") -> TestClient:
    seed_tenant(db, "eng", credentials=("git",))
    seed_tenant(db, "research", credentials=("git",))
    ctx = build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(), waker=NullWaker(),
        metrics=ApiMetrics(), objects=objects, forge_tokens=NoForgeTokens(), now=lambda: T0,
    )
    service = forgeapp.ForgeApp(
        db, config=forgeapp.AppConfig(client_id="Iv23" + "abcdefgh12", app_id=app_id, slug=slug),
        secrets=app_secret, slots=object(), send=github, now=lambda: T0)
    app = create_app(ctx, forge_app=service,
                     access_service=AccessService(db, service, send=github, now=lambda: T0))
    app.state.forge_permissions = ForgePermissions(db, service, now=lambda: T0, clock=ticker)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def api(db, tokens, group_map, objects, github, app_secret, ticker) -> TestClient:
    return build(db, tokens, group_map, objects, github, app_secret, ticker)


def enable_org(db, owner: str, installation_id: int, *, tenant: str = "eng",
               email: str = "alice@saga.xyz", owner_type: str = "Organization",
               selection: str = "all", method: str | None = None) -> None:
    """The `forge_orgs` document `AccessService.enable` writes."""
    doc = {
        "tenant_id": tenant, "user": email, "user_hash": forgeapp.user_hash(email),
        "owner": owner.lower(), "owner_login": owner, "owner_type": owner_type,
        "installation_id": None if method else installation_id,
        "repository_selection": selection, "install_state": "installed",
        "enabled_at": T0, "checked_at": T0,
    }
    if method:
        doc["method"] = method
    db.docs[f"{forgeapp.ORGS}/{org_id_for(tenant, email, owner)}"] = doc


def register(db, repository: str, *, tenant: str = "eng") -> str:
    owner, repo = repository.split("/")
    repo_id = repo_id_for(tenant, owner, repo)
    db.docs[f"repositories/{repo_id}"] = {
        "repo_id": repo_id, "tenant_id": tenant, "forge": "github", "owner": owner,
        "repo": repo, "default_branch": "main", "created_at": T0, "updated_at": T0,
    }
    return repo_id


def tenant_token(db, *, tenant: str = "eng") -> str:
    record = record_for_slot(tenant, Scope.TENANT, registered_by="swarm-api",
                             now=T0 - timedelta(days=9))
    record.kind = "classic_pat"
    record.state = TokenState.ACTIVE
    record.verified_at = T0 - timedelta(hours=2)
    db.docs[f"{TOKENS_COLLECTION}/{record.token_id}"] = record.to_firestore()
    return record.token_id


def probed(db, token_id: str, repo_id: str, repository: str, *, tenant: str = "eng",
           **cells: str) -> None:
    caps = {cap: {"state": "ok", "reason": "measured", "evidence": "",
                  "verified_at": T0 - timedelta(hours=1)} for cap in CAPABILITIES}
    for cap, state in cells.items():
        caps[cap] = {"state": state, "reason": "measured", "evidence": "",
                     "verified_at": T0 - timedelta(hours=1)}
    db.docs[f"{CHECKS_COLLECTION}/{token_id}_{repo_id}"] = {
        "token_id": token_id, "tenant_id": tenant, "repo_id": repo_id,
        "repository": repository, "capabilities": caps,
        "verified_at": T0 - timedelta(hours=1), "attempted_at": T0 - timedelta(hours=1),
        "complete": True, "error": None,
    }


def read(api: TestClient, user: str = "alice", **params: Any) -> dict[str, Any]:
    answer = api.get(ROUTE, params=params, headers=auth_header(user))
    assert answer.status_code == 200, answer.text
    return answer.json()


def org(body: dict[str, Any], owner: str) -> dict[str, Any]:
    (card,) = [c for c in body["orgs"] if c["owner"].lower() == owner.lower()]
    return card


def repo(body: dict[str, Any], repository: str) -> dict[str, Any]:
    (row,) = [r for r in body["repositories"] if r["repository"] == repository]
    return row


# -- org cards -------------------------------------------------------------------------


def test_an_org_that_has_not_accepted_a_requested_permission_has_a_gap_with_its_review_url(
        api, db, github):
    # The App asks for statuses: read; saga-xyz's installation does not hold it.
    github.install(77, "saga-xyz", WITHOUT_STATUSES)
    enable_org(db, "saga-xyz", 77)
    github.install(91, "alice-gh", WITHOUT_STATUSES, kind="User")
    enable_org(db, "alice-gh", 91, owner_type="User")
    body = read(api)
    card = org(body, "saga-xyz")
    assert card["status"] == "missing" and card["installation_id"] == 77
    (gap,) = card["gaps"]
    assert gap["permission"] == "statuses" and gap["level"] == "read" and gap["held"] is None
    assert gap["fix"] == "accept"
    assert gap["fix_url"] == ("https://github.com/organizations/saga-xyz/settings/"
                              "installations/77/permissions/update")
    # A user account's installation is reviewed on the user's settings.
    (user_gap,) = org(body, "alice-gh")["gaps"]
    assert user_gap["fix_url"] == "https://github.com/settings/installations/91/permissions/update"
    # The App asks for everything, so its own card is green.
    assert body["app"]["status"] == "ok" and body["app"]["gaps"] == []
    # Every call was a read as the App.
    assert {m for m, _, _ in github.calls} == {"GET"}


def test_a_fully_granted_org_has_no_gaps(api, db, github):
    github.install(77, "saga-xyz", {**FULL, "contents": "write", "actions": "read"})
    enable_org(db, "saga-xyz", 77)
    card = org(read(api), "saga-xyz")
    assert card["status"] == "ok" and card["gaps"] == [] and card["reason"] is None
    # The pure comparison: write covers read, admin covers write, absent is a gap.
    assert gaps(REQUIRED, FULL) == []
    assert gaps(REQUIRED, {**FULL, "checks": "write", "contents": "admin"}) == []
    assert [g["permission"] for g in gaps(REQUIRED, {**FULL, "contents": "read"})] == ["contents"]
    assert [g["permission"] for g in gaps(REQUIRED, {})] == [n["permission"] for n in REQUIRED]


def test_a_permission_the_app_does_not_request_is_an_app_gap(api, db, github):
    github.requested = dict(WITHOUT_STATUSES)
    github.install(77, "saga-xyz", WITHOUT_STATUSES)
    enable_org(db, "saga-xyz", 77)
    body = read(api, "root")
    (app_gap,) = body["app"]["gaps"]
    assert body["app"]["status"] == "missing"
    assert app_gap["permission"] == "statuses" and app_gap["fix"] == "app"
    assert app_gap["fix_url"] == "https://github.com/organizations/saga/settings/apps/swarmcloud/permissions"
    # On the org's card it is the App's to fix: no org can accept what is not asked for.
    (org_gap,) = org(body, "saga-xyz")["gaps"]
    assert org_gap["fix"] == "app" and org_gap["fix_url"] == app_gap["fix_url"]
    assert "installations/77/permissions/update" not in json.dumps(body)
    # An App owned by a user account is changed on that user's settings.
    github.owner_type = "User"
    again = read(api, "root", refresh="true")
    assert again["app"]["gaps"][0]["fix_url"] == "https://github.com/settings/apps/swarmcloud/permissions"


def test_the_app_settings_url_is_sent_only_to_an_admin(api, db, github):
    github.requested = dict(WITHOUT_STATUSES)
    github.install(77, "saga-xyz", WITHOUT_STATUSES)
    enable_org(db, "saga-xyz", 77)
    admin = read(api, "root")
    assert "settings/apps/swarmcloud/permissions" in json.dumps(admin)
    assert admin["app"]["permissions_url"].endswith("/settings/apps/swarmcloud/permissions")
    # A member sees the same gaps -- from the cache, too -- and no App link at all.
    member = read(api, "alice")
    assert member["cached"] is True
    text = json.dumps(member)
    assert "settings/apps" not in text
    assert "permissions_url" not in member["app"]
    assert all("fix_url" not in g for g in member["app"]["gaps"])
    assert [g["permission"] for g in member["app"]["gaps"]] == ["statuses"]
    assert all("fix_url" not in g for g in org(member, "saga-xyz")["gaps"])
    # And the admin's next cached answer still carries it: stripping is per caller.
    assert "settings/apps/swarmcloud/permissions" in json.dumps(read(api, "root"))


def test_each_gap_names_what_breaks_in_plain_words(api, db, github):
    github.install(77, "saga-xyz", {"metadata": "read"})
    enable_org(db, "saga-xyz", 77)
    card = org(read(api), "saga-xyz")
    by_name = {g["permission"]: g for g in card["gaps"]}
    assert set(by_name) == {"contents", "pull_requests", "checks", "statuses", "issues"}
    assert by_name["statuses"]["breaks"] == STATUSES_BREAK
    assert by_name["checks"]["breaks"] == "merges can't wait for checks"
    assert by_name["contents"]["breaks"].startswith("steps can't push their branch")
    assert by_name["pull_requests"]["breaks"] == "runs can't open or update their pull request"
    assert by_name["issues"]["breaks"] == "issue runs can't comment on their issue"
    for need in REQUIRED:
        # A consequence in words, not an identifier: no snake_case, no colon level.
        assert need["breaks"] and need["needed_for"]
        assert "_" not in need["breaks"] and ":" not in need["breaks"]
    assert {n["permission"]: n["level"] for n in REQUIRED}["statuses"] == "read"


# -- repositories ------------------------------------------------------------------------


def test_repositories_of_an_org_with_a_gap_are_missing_and_name_it(api, db, github):
    github.install(77, "saga-xyz", WITHOUT_STATUSES)
    enable_org(db, "saga-xyz", 77)
    widgets = register(db, "saga-xyz/widgets")
    register(db, "saga-xyz/payments")
    body = read(api)
    for name in ("saga-xyz/widgets", "saga-xyz/payments"):
        row = repo(body, name)
        assert row["source"] == "app" and row["status"] == "missing"
        assert row["problems"] == [{"permission": "statuses", "level": "read",
                                    "breaks": STATUSES_BREAK}]
    assert widgets in org(body, "saga-xyz")["repositories"]
    # Under an installation limited to selected repositories, one that is not
    # among them is missing even when every permission is held.
    github.installations.clear()
    github.install(77, "saga-xyz", FULL, selection="selected", repositories=("saga-xyz/widgets",))
    again = read(api, refresh="true")
    assert repo(again, "saga-xyz/widgets")["status"] == "ok"
    payments = repo(again, "saga-xyz/payments")
    assert payments["status"] == "missing"
    assert payments["problems"][0]["permission"] == "repository_access"
    assert "does not include this repository" in payments["problems"][0]["breaks"]
    assert "/repos/saga-xyz/payments/installation" in github.paths()


def test_a_token_backed_repository_reports_its_token_probe(api, db, github):
    token_id = tenant_token(db)
    lacking = register(db, "octo-org/widget-shop")
    probed(db, token_id, lacking, "octo-org/widget-shop", read_statuses="missing")
    healthy = register(db, "acme/widgets")
    probed(db, token_id, healthy, "acme/widgets")
    register(db, "acme/never-probed")
    fine = register(db, "acme/fine-grained")
    probed(db, token_id, fine, "acme/fine-grained", open_pull_requests="unknown")
    body = read(api)
    row = repo(body, "octo-org/widget-shop")
    assert row["source"] == "token" and row["status"] == "missing"
    assert row["token_id"] == token_id
    assert row["problems"] == [{"permission": "statuses", "level": "read",
                                "breaks": STATUSES_BREAK}]
    assert repo(body, "acme/widgets")["status"] == "ok"
    never = repo(body, "acme/never-probed")
    assert never["status"] == "unknown" and never["reason"] == "not_probed"
    # A row the probe could not measure is unknown, never ok.
    grey = repo(body, "acme/fine-grained")
    assert grey["status"] == "unknown" and grey["unread"] == ["pull_requests"]
    # A probe stored before read_statuses existed has no such row: unknown, never ok.
    old = db.docs[f"{CHECKS_COLLECTION}/{token_id}_{healthy}"]
    del old["capabilities"]["read_statuses"]
    stale = repo(read(api, refresh="true"), "acme/widgets")
    assert stale["status"] == "unknown" and "statuses" in stale["unread"]
    # No org card is made for an owner reached only by a token.
    assert body["orgs"] == []


# -- freshness -----------------------------------------------------------------------------


def test_refresh_true_reads_github_again_after_acceptance(api, db, github, ticker):
    item = github.install(77, "saga-xyz", WITHOUT_STATUSES)
    enable_org(db, "saga-xyz", 77)
    register(db, "saga-xyz/widgets")
    first = read(api)
    assert org(first, "saga-xyz")["status"] == "missing" and first["cached"] is False
    reads = len(github.calls)
    # The org accepts on GitHub.
    item["permissions"]["statuses"] = "read"
    # Coming back to the tab inside the cache window: the cached answer.
    ticker.at += 30
    cached = read(api)
    assert cached["cached"] is True and len(github.calls) == reads
    assert org(cached, "saga-xyz")["status"] == "missing"
    # Re-check: GitHub again, and the card and the repository are green.
    fresh = read(api, refresh="true")
    assert fresh["cached"] is False and len(github.calls) > reads
    assert org(fresh, "saga-xyz")["status"] == "ok" and org(fresh, "saga-xyz")["gaps"] == []
    assert repo(fresh, "saga-xyz/widgets")["status"] == "ok"
    # Past the cache window a plain read goes to GitHub too.
    item["permissions"].pop("checks")
    ticker.at += 61
    late = read(api)
    assert late["cached"] is False and org(late, "saga-xyz")["status"] == "missing"


# -- unknown is never ok ---------------------------------------------------------------------


def _all_unknown(body: dict[str, Any], reason: str) -> None:
    assert body["app"]["status"] == "unknown" and body["app"]["reason"] == reason
    assert body["orgs"] and all(c["status"] == "unknown" and c["reason"] == reason
                                for c in body["orgs"])
    for row in body["repositories"]:
        if row["source"] == "app":
            assert row["status"] == "unknown" and row["reason"] == reason


def test_github_unreachable_is_unknown_never_ok_and_never_echoes_a_body(
        api, db, github, app_secret, key):
    github.install(77, "saga-xyz", FULL)
    enable_org(db, "saga-xyz", 77)
    register(db, "saga-xyz/widgets")
    github.down = True
    body = read(api, refresh="true")
    _all_unknown(body, "github_unreachable")
    assert github.echo not in json.dumps(body)
    # GitHub answers, but not to this key: refused, never a body.
    github.down = False
    github.public_key = app_key()[1]
    refused = api.get(ROUTE, params={"refresh": "true"}, headers=auth_header("alice"))
    assert refused.status_code == 200
    _all_unknown(refused.json(), "key_rejected")
    assert github.echo not in refused.text and "JSON web token" not in refused.text
    # No key stored: nothing is sent.
    github.public_key = key[1]
    app_secret.pem = None
    sent = len(github.calls)
    _all_unknown(read(api, refresh="true"), "private_key_missing")
    assert len(github.calls) == sent
    app_secret.pem = "not a key"
    _all_unknown(read(api, refresh="true"), "private_key_malformed")
    assert len(github.calls) == sent


def test_the_app_not_configured_is_unknown(db, tokens, group_map, objects, github, app_secret,
                                           ticker):
    api = build(db, tokens, group_map, objects, github, app_secret, ticker, app_id="", slug="")
    github.install(77, "saga-xyz", FULL)
    enable_org(db, "saga-xyz", 77)
    body = read(api)
    _all_unknown(body, "app_id_not_configured")
    assert github.calls == []


def test_no_token_or_jwt_appears_in_the_response_or_logs(api, db, github, key, caplog):
    caplog.set_level(logging.DEBUG)
    transport = logging.getLogger("tests.forgeperms.transport")
    original = github.__call__

    def logging_send(method, url, headers, body, timeout):
        # A transport that logs what it sends: the redaction literal must mask it.
        transport.warning("sending %s %s headers=%s", method, url, headers)
        return original(method, url, headers, body, timeout)

    api.app.state.forge_app._github._send = logging_send
    github.install(77, "saga-xyz", WITHOUT_STATUSES)
    enable_org(db, "saga-xyz", 77)
    register(db, "saga-xyz/widgets")
    token_id = tenant_token(db)
    shop = register(db, "octo-org/widget-shop")
    probed(db, token_id, shop, "octo-org/widget-shop")
    texts = [api.get(ROUTE, headers=auth_header(u)).text for u in ("alice", "root")]
    assert github.jwts, "the App was asked nothing"
    logs = caplog.text
    pem = key[0]
    values = list(github.jwts) + [pem] + [line for line in pem.splitlines()
                                          if line and not line.startswith("-----")]
    for value in values:
        for text in texts + [logs]:
            assert value not in text
    for text in texts:
        assert not re.search(r"gh[pousr]_[A-Za-z0-9]{8,}|github_pat_|eyJ[A-Za-z0-9_-]{8,}", text)
        assert github.echo not in text
        assert "Bearer" not in text and "Authorization" not in text
    # The masked header did reach the log: the transport line was written, masked.
    assert "sending GET https://api.github.com/app " in logs


def test_the_route_takes_its_tenant_from_the_token(api, db, github):
    github.install(77, "saga-xyz", FULL)
    github.install(88, "research-org", WITHOUT_STATUSES)
    enable_org(db, "saga-xyz", 77)
    enable_org(db, "research-org", 88, tenant="research", email="bob@saga.xyz")
    register(db, "saga-xyz/widgets")
    register(db, "research-org/lab", tenant="research")
    mine = read(api, "alice", tenant_id="research", refresh="true")
    assert mine["tenant_id"] == "eng"
    assert [c["owner"] for c in mine["orgs"]] == ["saga-xyz"]
    assert [r["repository"] for r in mine["repositories"]] == ["saga-xyz/widgets"]
    theirs = read(api, "bob")
    assert theirs["tenant_id"] == "research"
    assert [c["owner"] for c in theirs["orgs"]] == ["research-org"]
    assert [r["repository"] for r in theirs["repositories"]] == ["research-org/lab"]
    # Another tenant's cached answer is never served: each tenant has its own.
    assert read(api, "alice")["orgs"][0]["owner"] == "saga-xyz"
    assert api.get(ROUTE).status_code == 401
