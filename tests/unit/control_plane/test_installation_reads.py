"""Background GitHub reads go out as the App's installation, not as a person (#1483, QUOTA-INSTALL).

Measured 2026-10-17 00:00-02:10 UTC: every call SwarmCloud made for the eng
and u-bogdan workspaces went out as user 5000393, through the App USER token
both workspaces connect with. The merge wake's CI reads alone cost ~3,450
calls an hour and the stranded-PR sweep ~200-400; the person's 5,000/h ran
out, push tests read as missing permissions and ten lane attempts failed.

What these tests hold:

  1. `forge_installation.InstallationTokens` mints with the App JWT, for the
     tenant's OWN enabled installation of the owner, scoped to the one
     repository and to read permissions, and holds the token until five
     minutes before it expires.
  2. An owner with no installation (a person's fallback token, `method:
     pat`), or another tenant's installation, mints nothing.
  3. A mint that fails falls back, is logged ONCE with its type only, and
     no token or JWT reaches a log line or an exception's text.
  4. Each switched read -- the merge wake's CI read, the issue run's CI read,
     the stranded-PR sweep, the repository index's head poll -- carries the
     installation token; the fallback owner's carries the tenant's token; and
     every write stays on the tenant's token.
  5. forgeperms never needed the switch: an installed repository is read with
     the App JWT and the tenant token is not even read for it.
  6. The setting defaults ON; off, no source is wired.

A fake transport records the Authorization of every call. Every token is
built at runtime. No credentials, no network, no emulator.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from swarm_api import forge, forge_installation, forgeapp, forgewrite
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.forge_installation import (
    FAILURE_HOLD, READ_PERMISSIONS, REFRESH_BEFORE, InstallationTokens,
)
from swarm_api.forgeperms import ForgePermissions, Registered
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.repoindex import read_head_if_changed
from swarm_api.settings import ApiSettings
from swarm_api.waker import NullWaker
from swarm_common.models import Tenant

from . import forge_fakes
from .conftest import api_settings, seed_tenant
from .test_issue_run_ci import SHA_A, _read as _issue_read, _to_checking
from .test_merge_wake import CHECK, HEAD, NUMBER, SWEEPER, SWEEPER_HEADERS, QueueGitHub, parked_merge
from .test_stranded_prs import GitHub as StrandedGitHub, _opener, _register

INSTALLATION = 169162403
T0 = datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc)


def _value() -> str:
    return secrets.token_hex(20)


class Mints:
    """`POST /app/installations/{id}/access_tokens` as `forgeapp.HttpSend` sees it."""

    def __init__(self, *, status: int = 201, raises: Exception | None = None,
                 lifetime: timedelta = timedelta(hours=1), now: datetime = T0) -> None:
        self.status = status
        self.raises = raises
        self.lifetime = lifetime
        self.now = now
        #: (method, url, Authorization, decoded body), copied at call time.
        self.calls: list[tuple[str, str, str, dict[str, Any]]] = []
        self.issued: list[str] = []

    def __call__(self, method, url, headers, body, timeout) -> forgeapp.HttpAnswer:
        self.calls.append((method, url, headers.get("Authorization", ""),
                           json.loads(body or b"{}")))
        if self.raises is not None:
            raise self.raises
        if self.status != 201:
            return forgeapp.HttpAnswer(self.status, {}, b'{"message": "refused"}')
        token = _value()
        self.issued.append(token)
        expires = (self.now + self.lifetime).isoformat().replace("+00:00", "Z")
        return forgeapp.HttpAnswer(201, {}, json.dumps(
            {"token": token, "expires_at": expires}).encode("utf-8"))


class Clock:
    def __init__(self) -> None:
        self.at = T0.timestamp()

    def __call__(self) -> float:
        return self.at


def _enable(db, tenant_id: str, owner: str, *, installation: int | None = INSTALLATION,
            method: str | None = None, user: str = "alice@example.com") -> None:
    """The `forge_orgs` document access.py writes for an enabled owner."""
    doc: dict[str, Any] = {
        "tenant_id": tenant_id, "user": user, "user_hash": forgeapp.user_hash(user),
        "owner": owner.lower(), "owner_login": owner, "installation_id": installation,
        "install_state": "installed" if method is None else "token",
    }
    if method is not None:
        doc["method"] = method
    db.collection(forge_installation.ORGS).document(
        f"{tenant_id}__{forgeapp.user_hash(user)}__{owner.lower()}").set(doc)


class Lines(logging.Handler):
    """Every line `forge_installation` logs, formatted, whatever create_app
    did to the root logger's handlers."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def lines():
    handler = Lines()
    logger = logging.getLogger(forge_installation.__name__)
    logger.addHandler(handler)
    level, logger.level = logger.level, logging.DEBUG
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def _source(db, mints: Mints, clock: Clock | None = None, jwt: str | None = None):
    value = jwt or _value()
    return InstallationTokens(db, app_jwt=lambda: value, send=mints,
                              clock=clock or Clock()), value


# --------------------------------------------------------------------------
# 1-3. the source
# --------------------------------------------------------------------------

def test_the_mint_is_the_apps_scoped_to_one_repository_and_read_only(db):
    _enable(db, "eng", "bogdan-alexandrescu")
    mints = Mints()
    source, jwt = _source(db, mints)

    token = source.token_for_repo("eng", "Bogdan-Alexandrescu", "product-oracle")

    assert token == mints.issued[0]
    ((method, url, auth, body),) = mints.calls
    assert method == "POST"
    assert url == f"https://api.github.com/app/installations/{INSTALLATION}/access_tokens"
    assert auth == f"Bearer {jwt}"
    assert body == {"repositories": ["product-oracle"], "permissions": dict(READ_PERMISSIONS)}
    assert set(READ_PERMISSIONS.values()) == {"read"}


def test_a_token_is_held_until_five_minutes_before_it_expires(db):
    _enable(db, "eng", "bogdan-alexandrescu")
    mints, clock = Mints(), Clock()
    source, _ = _source(db, mints, clock)

    first = source.token_for_repo("eng", "bogdan-alexandrescu", "product-oracle")
    clock.at += 3600 - REFRESH_BEFORE - 1
    assert source.token_for_repo("eng", "bogdan-alexandrescu", "product-oracle") == first
    assert len(mints.calls) == 1

    clock.at += 1
    mints.now = T0 + timedelta(seconds=3600 - REFRESH_BEFORE)
    second = source.token_for_repo("eng", "bogdan-alexandrescu", "product-oracle")
    assert second != first and len(mints.calls) == 2


def test_each_repository_gets_its_own_token(db):
    _enable(db, "eng", "bogdan-alexandrescu")
    mints = Mints()
    source, _ = _source(db, mints)
    a = source.token_for_repo("eng", "bogdan-alexandrescu", "product-oracle")
    b = source.token_for_repo("eng", "bogdan-alexandrescu", "claudeswitch")
    assert a != b
    assert [c[3]["repositories"] for c in mints.calls] == [["product-oracle"], ["claudeswitch"]]


@pytest.mark.parametrize("case", ["pat", "no_document", "other_tenant", "requested"])
def test_an_owner_without_this_tenants_installation_mints_nothing(db, case):
    if case == "pat":
        _enable(db, "eng", "sagaxyz", installation=None, method="pat")
    elif case == "other_tenant":
        _enable(db, "research", "sagaxyz")
    elif case == "requested":
        _enable(db, "eng", "sagaxyz")
        doc = next(iter(v for k, v in db.docs.items() if k.startswith("forge_orgs/")))
        doc["install_state"] = "requested"
    mints = Mints()
    source, _ = _source(db, mints)

    assert source.token_for_repo("eng", "sagaxyz", "ai-studio") is None
    assert mints.calls == []


@pytest.mark.parametrize("failure", ["refused", "unreachable", "jwt"])
def test_a_failed_mint_falls_back_is_logged_once_and_leaks_nothing(db, caplog, failure):
    _enable(db, "eng", "bogdan-alexandrescu")
    jwt = _value()
    mints = Mints(status=422) if failure == "refused" else Mints(
        raises=OSError(f"connection reset while sending {jwt}") if failure == "unreachable"
        else None)
    clock = Clock()

    def signed() -> str:
        if failure == "jwt":
            raise forgeapp.AppNotConfigured(f"the key {jwt} is unusable")
        return jwt

    source = InstallationTokens(db, app_jwt=signed, send=mints, clock=clock)
    caplog.set_level(logging.DEBUG)

    assert source.token_for_repo("eng", "bogdan-alexandrescu", "product-oracle") is None
    clock.at += FAILURE_HOLD
    assert source.token_for_repo("eng", "bogdan-alexandrescu", "product-oracle") is None

    lines = [r for r in caplog.records if "not minted" in r.getMessage()]
    assert len(lines) == 1, [r.getMessage() for r in caplog.records]
    assert jwt not in caplog.text
    assert "connection reset" not in caplog.text and "unusable" not in caplog.text
    expected = {"refused": "HTTP 422", "unreachable": "OSError",
                "jwt": "AppNotConfigured"}[failure]
    assert expected in lines[0].getMessage()


def test_a_failed_repository_is_not_asked_again_inside_the_hold(db):
    _enable(db, "eng", "bogdan-alexandrescu")
    mints, clock = Mints(status=403), Clock()
    source, _ = _source(db, mints, clock)
    source.token_for_repo("eng", "bogdan-alexandrescu", "product-oracle")
    clock.at += FAILURE_HOLD - 1
    source.token_for_repo("eng", "bogdan-alexandrescu", "product-oracle")
    assert len(mints.calls) == 1
    clock.at += 1
    mints.status = 201
    assert source.token_for_repo("eng", "bogdan-alexandrescu", "product-oracle")
    assert len(mints.calls) == 2


def test_a_minted_token_never_reaches_a_log_line(db, caplog):
    _enable(db, "eng", "bogdan-alexandrescu")
    mints = Mints()
    source, jwt = _source(db, mints)
    caplog.set_level(logging.DEBUG)
    token = source.token_for_repo("eng", "bogdan-alexandrescu", "product-oracle")
    assert token and token not in caplog.text and jwt not in caplog.text
    assert token not in repr(source._held)


# --------------------------------------------------------------------------
# 4a. the merge wake's CI read
# --------------------------------------------------------------------------

class FixedSource:
    """An installation for `owners` only; records what was asked."""

    def __init__(self, *owners: str) -> None:
        self.owners = {o.lower() for o in owners}
        self.value = _value()
        self.asked: list[tuple[str, str, str]] = []

    def token_for_repo(self, tenant_id: str, owner: str, repo: str) -> str | None:
        self.asked.append((tenant_id, owner, repo))
        return self.value if owner.lower() in self.owners else None


def _auths(calls, *, method: str = "GET") -> set[str]:
    return {headers.get("Authorization", "") for m, _, headers, _ in calls if m == method}


def _wake_client(db, tokens, group_map, objects, github, tenant_tokens, source,
                 now=lambda: datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)) -> TestClient:
    seed_tenant(db, "eng")
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    context = build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)), db=db,
        verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, now=now, forge_tokens=tenant_tokens,
        forge_writer=forgewrite.GitHubWriter(send=github, locate=github.locate,
                                             fetch=github.fetch),
        forge_installations=source,
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


@pytest.fixture
def wake_github() -> QueueGitHub:
    gh = QueueGitHub()
    gh.open_pull(NUMBER, HEAD, ref="swarm/task_int")
    gh.require(CHECK)
    gh.check(HEAD, CHECK, None, status="in_progress")
    return gh


def test_the_merge_wake_reads_ci_with_the_installation_token(
        db, tokens, group_map, objects, wake_github):
    tenant_tokens = forge_fakes.AnyTenantTokens()
    source = FixedSource("example-org")
    client = _wake_client(db, tokens, group_map, objects, wake_github, tenant_tokens, source)
    parked_merge(db)

    body = client.post("/v1/admin/merges/wake?tenant_id=eng", headers=SWEEPER_HEADERS).json()

    assert body["report"]["waiting"] == 1, body
    assert ("eng", "example-org", "widgets") in source.asked
    assert _auths(wake_github.calls) == {f"Bearer {source.value}"}


def test_the_merge_wake_keeps_the_tenant_token_for_an_owner_with_no_installation(
        db, tokens, group_map, objects, wake_github):
    tenant_tokens = forge_fakes.AnyTenantTokens()
    source = FixedSource("someone-else")
    client = _wake_client(db, tokens, group_map, objects, wake_github, tenant_tokens, source)
    parked_merge(db)

    client.post("/v1/admin/merges/wake?tenant_id=eng", headers=SWEEPER_HEADERS)

    (user,) = tenant_tokens.issued.values()
    assert _auths(wake_github.calls) == {f"Bearer {user}"}


def test_the_merge_wake_falls_back_when_the_mint_fails_and_logs_no_token(
        db, tokens, group_map, objects, wake_github, lines):
    """The real source, over a mint GitHub refuses: the read still happens,
    with the tenant's token, and one line says why -- with no value in it."""
    _enable(db, "eng", "example-org")
    mints = Mints(status=422)
    jwt = _value()
    source = InstallationTokens(db, app_jwt=lambda: jwt, send=mints, clock=Clock())
    tenant_tokens = forge_fakes.AnyTenantTokens()
    client = _wake_client(db, tokens, group_map, objects, wake_github, tenant_tokens, source)
    parked_merge(db)

    body = client.post("/v1/admin/merges/wake?tenant_id=eng", headers=SWEEPER_HEADERS).json()

    assert body["report"]["waiting"] == 1, body
    assert len(mints.calls) == 1
    (user,) = tenant_tokens.issued.values()
    assert _auths(wake_github.calls) == {f"Bearer {user}"}
    out = lines.text
    assert len([line for line in lines.lines if "not minted" in line]) == 1, out
    assert "HTTP 422" in out
    assert user not in out and jwt not in out


def test_the_merge_wake_reads_with_a_real_minted_token(
        db, tokens, group_map, objects, wake_github, lines):
    _enable(db, "eng", "example-org")
    mints = Mints()
    source = InstallationTokens(db, app_jwt=_value, send=mints, clock=Clock())
    client = _wake_client(db, tokens, group_map, objects, wake_github,
                          forge_fakes.AnyTenantTokens(), source)
    parked_merge(db)

    client.post("/v1/admin/merges/wake?tenant_id=eng", headers=SWEEPER_HEADERS)

    (minted,) = mints.issued
    assert _auths(wake_github.calls) == {f"Bearer {minted}"}
    assert minted not in lines.text


# --------------------------------------------------------------------------
# 4b. the issue run's CI read
# --------------------------------------------------------------------------

class IssueCiSource(FixedSource):
    pass


@pytest.fixture
def issue_source() -> IssueCiSource:
    return IssueCiSource("example-org")


@pytest.fixture
def api_context(db, tokens, group_map, objects, forge_tokens, writes, clock, request):
    # test_issue_run_ci's context, with an installation source when a test asks.
    source = request.getfixturevalue("issue_source") \
        if "issue_source" in request.fixturenames else None
    return build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(),
        waker=NullWaker(), metrics=ApiMetrics(), objects=objects, now=clock,
        forge_tokens=forge_tokens,
        forge=forge.GitHubIssues(send=forge_fakes.GitHub(issues=[forge_fakes.issue(42)])),
        forge_writer=forgewrite.GitHubWriter(send=writes, locate=writes.locate,
                                             fetch=writes.fetch),
        forge_installations=source,
    )


@pytest.fixture
def clock():
    from .test_issue_run_ci import Clock as IssueClock

    return IssueClock()


@pytest.fixture
def writes():
    fake = forge_fakes.GitHubWrites()
    fake.require("unit")
    return fake


@pytest.fixture
def forge_tokens():
    return forge_fakes.AnyTenantTokens()


def _ci_auths(writes, sha: str) -> set[str]:
    """The CI read's GETs: the base branch's rules, the check runs and the
    statuses at the head. (The keyword block's sync reads the pull request
    before it PATCHes it: part of a write, so the tenant's token.)"""
    return {headers.get("Authorization", "") for m, url, headers, _ in writes.calls
            if m == "GET" and (f"/commits/{sha}/" in urlparse(url).path
                               or "/rules/branches/" in urlparse(url).path)}


def test_the_issue_runs_ci_read_uses_the_installation_token_and_writes_stay_the_tenants(
        client, db, objects, writes, clock, forge_tokens, issue_source):
    running = _to_checking(client, db, objects, writes, clock)
    writes.calls.clear()
    writes.check(SHA_A, "unit", None, status="in_progress")

    run = _issue_read(client, clock, running["id"])

    assert run["state"] == "CHECKING"
    assert ("eng", "example-org", "widgets") in issue_source.asked
    assert _ci_auths(writes, SHA_A) == {f"Bearer {issue_source.value}"}
    user = forge_tokens.issued[next(iter(forge_tokens.issued))]
    for method, _, headers, _ in writes.calls:
        if method != "GET":
            assert headers.get("Authorization") == f"Bearer {user}"


def test_the_issue_runs_ci_read_keeps_the_tenant_token_without_a_source(
        client, db, objects, writes, clock, forge_tokens):
    running = _to_checking(client, db, objects, writes, clock)
    writes.calls.clear()
    writes.check(SHA_A, "unit", None, status="in_progress")

    _issue_read(client, clock, running["id"])

    user = forge_tokens.issued[next(iter(forge_tokens.issued))]
    assert _ci_auths(writes, SHA_A) == {f"Bearer {user}"}


# --------------------------------------------------------------------------
# 4c. the stranded-PR sweep
# --------------------------------------------------------------------------

def _sweep_client(db, tokens, group_map, objects, github, tenant_tokens, source) -> TestClient:
    seed_tenant(db, "eng")
    _register(db, "eng", "example-org", "widgets")
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    context = build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)), db=db,
        verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, forge_tokens=tenant_tokens,
        forge_writer=forgewrite.GitHubWriter(send=github), forge_installations=source,
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


@pytest.mark.parametrize("installed", [True, False])
def test_the_stranded_pr_sweep_reads_with_the_installation_token(
        db, tokens, group_map, objects, installed):
    github = StrandedGitHub()
    sha = github.add(7)
    github.green(sha)
    _opener(db, 7)
    tenant_tokens = forge_fakes.AnyTenantTokens()
    source = FixedSource("example-org" if installed else "someone-else")
    client = _sweep_client(db, tokens, group_map, objects, github, tenant_tokens, source)

    response = client.post("/v1/admin/stranded-prs/sweep?tenant_id=eng",
                           headers=SWEEPER_HEADERS)

    assert response.status_code == 200, response.text
    assert len(github.calls) >= 3, github.calls
    (user,) = tenant_tokens.issued.values()
    expected = source.value if installed else user
    assert github.tokens_sent_for("example-org/widgets") == {f"Bearer {expected}"}


# --------------------------------------------------------------------------
# 4d. the repository index's head poll
# --------------------------------------------------------------------------

class ProbeGitHub:
    def __init__(self) -> None:
        self.auths: list[str] = []

    def __call__(self, url, headers, timeout) -> forge.ProbeResponse:
        self.auths.append(headers.get("Authorization", ""))
        return forge.ProbeResponse(status=200, headers={"etag": '"abc"'},
                                   body=("c" * 40).encode())


@pytest.mark.parametrize("installed", [True, False])
def test_the_index_head_poll_reads_with_the_installation_token(installed):
    probe = ProbeGitHub()
    tenant_tokens = forge_fakes.AnyTenantTokens()
    source = FixedSource("example-org" if installed else "someone-else")
    tenant = Tenant(tenant_id="eng", kind="group", principal="eng@example.com", created_at=T0)
    record = {"owner": "example-org", "repo": "widgets", "default_branch": "main",
              "repo_id": "repo_x"}

    read = read_head_if_changed(record, tenant, etag=None, known_sha=None,
                                tokens=tenant_tokens, forge=forge.GitHubIssues(probe_send=probe),
                                installations=source)

    assert read.sha == "c" * 40
    if installed:
        assert probe.auths == [f"Bearer {source.value}"]
        assert tenant_tokens.asked == [], "the tenant's token is not even read"
    else:
        (user,) = tenant_tokens.issued.values()
        assert probe.auths == [f"Bearer {user}"]


# --------------------------------------------------------------------------
# 5. forgeperms: the App JWT, never the person, for an installed repository
# --------------------------------------------------------------------------

def test_forgeperms_reads_an_installed_repository_without_the_tenant_token():
    calls: list[tuple[str, str]] = []
    jwt = _value()

    def github(method, url, headers, body, timeout) -> forgeapp.HttpAnswer:
        path = url[len("https://api.github.com"):]
        calls.append((path, headers.get("Authorization", "")))
        if path == "/app":
            return forgeapp.HttpAnswer(200, {}, json.dumps({
                "id": 1, "slug": "swarmcloud-saga", "permissions": {"metadata": "read"},
                "owner": {"login": "sagaxyz", "type": "Organization"}}).encode())
        if path == "/repos/bogdan-alexandrescu/product-oracle/installation":
            return forgeapp.HttpAnswer(200, {}, json.dumps({
                "id": INSTALLATION,
                "account": {"login": "bogdan-alexandrescu", "type": "User"}}).encode())
        if path == f"/app/installations/{INSTALLATION}":
            return forgeapp.HttpAnswer(200, {}, json.dumps(
                {"id": INSTALLATION, "permissions": {"metadata": "read"}}).encode())
        return forgeapp.HttpAnswer(404, {}, b"{}")

    def token() -> str:
        raise AssertionError("an installed repository read the tenant's token")

    service = ForgePermissions(app_jwt=lambda: jwt, app_slug="swarmcloud-saga", send=github)
    answer = service.compute("eng", [Registered(repo_id="r", owner="bogdan-alexandrescu",
                                                repo="product-oracle")], token=token, now=T0)

    assert answer["repositories"][0]["credential"] == "app"
    assert {auth for _, auth in calls} == {f"Bearer {jwt}"}


# --------------------------------------------------------------------------
# 6. the setting
# --------------------------------------------------------------------------

def test_the_setting_defaults_on_and_turns_off(monkeypatch):
    assert ApiSettings.__dataclass_fields__["forge_installation_reads"].default is True
    monkeypatch.setenv("FORGE_INSTALLATION_READS", "false")
    from swarm_api import settings as settings_module

    assert settings_module._bool("FORGE_INSTALLATION_READS", True) is False


@pytest.mark.parametrize("on", [True, False])
def test_build_context_wires_a_source_only_when_the_setting_is_on(db, tokens, group_map,
                                                                  objects, on):
    context = build_context(
        settings=api_settings(forge_installation_reads=on), db=db,
        verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, forge_tokens=forge_fakes.AnyTenantTokens(),
    )
    assert isinstance(context.forge_installations, InstallationTokens) is on
    if not on:
        assert context.forge_installations is None
