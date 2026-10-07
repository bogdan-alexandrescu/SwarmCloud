"""The git token probe (docs/git-tokens.md §3.4, §5; lane GT2a).

What is held here:

  * each of §5.1's eight capability readers maps the forge's answers to
    `ok` / `missing` / `unknown`, a 403 and a network error included;
  * a probe is GETs only, to api.github.com and github.com only, at most
    eight per token x repository;
  * the expiry header is parsed, and a passed expiry makes the record
    `expired`;
  * `last verified` moves only on a COMPLETE probe; a partial one keeps the
    previous rows and says why the last attempt failed;
  * the probe runs on registration (and so on rotation, which re-registers)
    and on `POST /v1/git-tokens/{id}/verify`, optionally for one repository,
    and the list and the record carry its summaries;
  * another tenant's token is a 404, and nothing is read or sent for it;
  * the token appears in no response and no log line, even when the forge
    echoes it and a transport logs it.

The forge is a fake transport; nothing reaches a network. Every
token-shaped value is built at runtime.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from swarm_api import forge
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.gittokens import (
    CAPABILITIES,
    CHECKS_COLLECTION,
    COLLECTION,
    MAX_GETS_PER_REPOSITORY,
    parse_expiry,
    probe_token,
    repo_id_for,
    token_id_for,
)
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.waker import NullWaker

from .conftest import PROJECT, api_settings, auth_header, seed_tenant

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

OWNER_REPO = "saga-xyz/widgets"
REPO = repo_id_for("eng", OWNER_REPO)


def classic_value() -> str:
    return "ghp" + "_" + secrets.token_hex(18)


def fine_grained_value() -> str:
    return "github" + "_pat_" + secrets.token_hex(30)


def app_value() -> str:
    return "ghs" + "_" + secrets.token_hex(18)


def repo_body(*, push: bool = True, triage: bool = True, private: bool = True) -> dict:
    return {
        "full_name": OWNER_REPO,
        "default_branch": "main",
        "private": private,
        "permissions": {"admin": False, "maintain": False, "push": push,
                        "triage": triage or push, "pull": True},
    }


def key_of(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path
    if parsed.hostname == "github.com" and path.endswith("/info/refs"):
        return "receive_pack"
    if path == "/user":
        return "user"
    if path == "/user/orgs":
        return "orgs"
    if path.endswith("/check-runs"):
        return "checks"
    if "/rules/branches/" in path:
        return "rules"
    if path.endswith("/issues"):
        return "issues"
    if path.endswith("/actions/workflows"):
        return "workflows"
    if re.fullmatch(r"/repos/[^/]+/[^/]+", path):
        return "repo"
    return "other"


class ProbeForge:
    """api.github.com and github.com as the probe sees them, in memory.

    `status` maps a read (`repo`, `receive_pack`, `checks`, `rules`, `issues`,
    `workflows`, `user`, `orgs`) to the status it answers; `raises` maps one
    to an exception the transport raises; `body` overrides one's JSON.
    `orgs` is the logins `GET /user/orgs` lists, paged as GitHub pages.
    """

    def __init__(self, *, scopes: str | None = "repo, workflow",
                 expiry: str | None = "2026-12-01 00:00:00 UTC",
                 repo: dict | None = None,
                 status: dict[str, int] | None = None,
                 raises: dict[str, Exception] | None = None,
                 body: dict[str, Any] | None = None,
                 headers: dict[str, dict[str, str]] | None = None,
                 orgs: list[str] | None = None) -> None:
        self.scopes = scopes
        self.orgs = orgs or []
        self.expiry = expiry
        self.repo = repo or repo_body()
        self.status = status or {}
        self.raises = raises or {}
        self.body = body or {}
        self.headers = headers or {}
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        #: Called with (url, headers) before each answer.
        self.on_call: Any = None

    def keys(self) -> list[str]:
        return [key_of(url) for _, url, _ in self.calls]

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> forge.ProbeResponse:
        self.calls.append(("GET", url, dict(headers)))
        if self.on_call is not None:
            self.on_call(url, headers)
        key = key_of(url)
        if key in self.raises:
            raise self.raises[key]
        answer_headers = {"x-ratelimit-remaining": "4999"}
        if self.scopes is not None:
            answer_headers["x-oauth-scopes"] = self.scopes
        if self.expiry is not None:
            answer_headers["github-authentication-token-expiration"] = self.expiry
        answer_headers.update(self.headers.get(key, {}))
        status = self.status.get(key, 200)
        if key in self.body:
            payload: Any = self.body[key]
        elif status != 200:
            payload = {"message": "refused"}
        elif key == "orgs":
            query = parse_qs(urlparse(url).query)
            page = int(query.get("page", ["1"])[0])
            per_page = int(query.get("per_page", ["30"])[0])
            payload = [{"login": login, "id": 1000 + i, "url": f"https://api.github.com/orgs/{login}"}
                       for i, login in enumerate(self.orgs)][(page - 1) * per_page: page * per_page]
        else:
            payload = {
                "user": {"login": "swarm-bot"},
                "repo": self.repo,
                "receive_pack": None,
                "checks": {"total_count": 0, "check_runs": []},
                "rules": [],
                "issues": [],
                "workflows": {"total_count": 0, "workflows": []},
            }.get(key, {})
        raw = b"001f# service=git-receive-pack\n" if payload is None else json.dumps(payload).encode()
        return forge.ProbeResponse(status=status, headers=answer_headers, body=raw)


def run(value: str, fake: ProbeForge, *, now: datetime = T0):
    return probe_token(value, [(REPO, OWNER_REPO)], send=fake, now=now)


def checks(result) -> dict[str, Any]:
    (pair,) = result.pairs
    return pair.checks


def states(result) -> dict[str, str | None]:
    return {cap: (c.state if c is not None else None) for cap, c in checks(result).items()}


# -- the shape of a probe ----------------------------------------------------


def test_a_healthy_classic_token_is_ok_everywhere_in_at_most_eight_gets() -> None:
    fake = ProbeForge()
    result = run(classic_value(), fake)
    assert result.complete and result.error is None
    assert set(checks(result)) == set(CAPABILITIES) and len(CAPABILITIES) == 8
    assert states(result) == {cap: "ok" for cap in CAPABILITIES}
    assert result.kind == "classic_pat" and result.forge_login == "swarm-bot"
    assert len(fake.calls) <= MAX_GETS_PER_REPOSITORY == 8
    for method, url, _ in fake.calls:
        assert method == "GET"
        assert forge.may_receive_forge_token(url), url
    # Every row says why.
    assert all(c.reason for c in checks(result).values())


def test_the_host_rule() -> None:
    assert forge.may_receive_forge_token("https://api.github.com/user")
    assert forge.may_receive_forge_token("https://github.com/o/r.git/info/refs")
    for url in ("http://api.github.com/user", "https://api.github.com:8443/user",
                "https://evil.example/user", "https://u@github.com/o/r",
                "https://github.com.evil.example/x"):
        assert not forge.may_receive_forge_token(url), url
    with pytest.raises(forge.ProbeHostRefused):
        forge.urllib_probe_send("https://evil.example/user", {}, 1.0)


# -- clone / read -------------------------------------------------------------


def test_clone_reader() -> None:
    assert checks(run(classic_value(), ProbeForge()))["clone"].state == "ok"
    gone = run(classic_value(), ProbeForge(status={"repo": 404}))
    assert checks(gone)["clone"].state == "missing"
    assert "not visible" in checks(gone)["clone"].reason
    # Nothing past clone can be done in a repository the token cannot see.
    assert set(states(gone).values()) == {"missing"}
    refused = run(classic_value(), ProbeForge(status={"repo": 403}))
    assert checks(refused)["clone"].state == "missing"
    assert "403" in checks(refused)["clone"].evidence
    down = run(classic_value(), ProbeForge(raises={"repo": OSError("connection reset")}))
    assert not down.complete
    assert states(down)["clone"] is None  # not measured: the previous answer stands
    assert "OSError" in down.pairs[0].error


def test_a_rate_limited_or_failing_forge_is_not_an_answer() -> None:
    limited = ProbeForge(status={"repo": 403},
                         headers={"repo": {"x-ratelimit-remaining": "0"}})
    assert not run(classic_value(), limited).complete
    for status in (429, 500, 503):
        result = run(classic_value(), ProbeForge(status={"repo": status}))
        assert not result.complete, status
        assert states(result)["clone"] is None


# -- push ----------------------------------------------------------------------


def test_push_reader() -> None:
    ok = checks(run(fine_grained_value(), ProbeForge(scopes=None)))["push"]
    assert ok.state == "ok" and "receive-pack" in ok.reason
    refused = checks(run(fine_grained_value(), ProbeForge(scopes=None,
                                                          status={"receive_pack": 403})))
    assert refused["push"].state == "missing"
    role = checks(run(classic_value(), ProbeForge(repo=repo_body(push=False))))
    assert role["push"].state == "missing" and "role" in role["push"].reason
    scope = checks(run(classic_value(), ProbeForge(scopes="read:org")))
    assert scope["push"].state == "missing" and "repo scope" in scope["push"].reason
    # A public repository is pushed with public_repo.
    public = checks(run(classic_value(), ProbeForge(scopes="public_repo",
                                                    repo=repo_body(private=False))))
    assert public["push"].state == "ok"
    down = run(classic_value(), ProbeForge(raises={"receive_pack": TimeoutError()}))
    assert not down.complete and states(down)["push"] is None
    assert states(down)["clone"] == "ok"


def test_the_push_read_is_basic_auth_to_github_com_only() -> None:
    fake = ProbeForge()
    run(classic_value(), fake)
    (url, headers) = next((u, h) for _, u, h in fake.calls if key_of(u) == "receive_pack")
    assert urlparse(url).hostname == "github.com"
    assert url.endswith("/saga-xyz/widgets.git/info/refs?service=git-receive-pack")
    assert headers["Authorization"].startswith("Basic ")


# -- open pull requests ---------------------------------------------------------


def test_open_pull_requests_reader() -> None:
    assert checks(run(classic_value(), ProbeForge()))["open_pull_requests"].state == "ok"
    fine = checks(run(fine_grained_value(), ProbeForge(scopes=None)))["open_pull_requests"]
    assert fine.state == "unknown" and "fine-grained" in fine.reason
    app = checks(run(app_value(), ProbeForge(scopes=None)))["open_pull_requests"]
    assert app.state == "unknown"
    no_push = checks(run(classic_value(), ProbeForge(status={"receive_pack": 403})))
    assert no_push["open_pull_requests"].state == "missing"
    down = run(classic_value(), ProbeForge(raises={"receive_pack": OSError()}))
    assert states(down)["open_pull_requests"] is None


# -- read checks ------------------------------------------------------------------


def test_read_checks_reader() -> None:
    fake = ProbeForge()
    assert checks(run(classic_value(), fake))["read_checks"].state == "ok"
    assert any(urlparse(u).path.endswith("/commits/main/check-runs") for _, u, _ in fake.calls)
    refused = checks(run(classic_value(), ProbeForge(status={"checks": 403})))["read_checks"]
    assert refused.state == "missing" and "403" in refused.evidence
    down = run(classic_value(), ProbeForge(raises={"checks": OSError()}))
    assert not down.complete and states(down)["read_checks"] is None
    assert states(down)["read_issues"] == "ok"


# -- merge ----------------------------------------------------------------------------


def test_merge_reader() -> None:
    assert checks(run(classic_value(), ProbeForge()))["merge"].state == "ok"
    restricted = checks(run(classic_value(), ProbeForge(
        body={"rules": [{"type": "update", "ruleset_id": 7}]})))["merge"]
    assert restricted.state == "unknown" and "bypass" in restricted.reason
    queued = checks(run(classic_value(), ProbeForge(
        body={"rules": [{"type": "merge_queue", "ruleset_id": 8}]})))["merge"]
    assert queued.state == "missing" and "merge queue" in queued.reason
    unreadable = checks(run(classic_value(), ProbeForge(status={"rules": 403})))["merge"]
    assert unreadable.state == "unknown" and "403" in unreadable.evidence
    no_push = checks(run(classic_value(), ProbeForge(repo=repo_body(push=False))))["merge"]
    assert no_push.state == "missing"
    down = run(classic_value(), ProbeForge(raises={"rules": OSError()}))
    assert states(down)["merge"] is None and not down.complete


# -- close issues --------------------------------------------------------------------


def test_close_issues_reader() -> None:
    triage = checks(run(classic_value(), ProbeForge(repo=repo_body(push=False, triage=True))))
    assert triage["close_issues"].state == "ok"
    pull = checks(run(classic_value(), ProbeForge(repo=repo_body(push=False, triage=False))))
    assert pull["close_issues"].state == "missing"
    fine = checks(run(fine_grained_value(), ProbeForge(scopes=None)))["close_issues"]
    assert fine.state == "unknown" and "fine-grained" in fine.reason
    fine_pull = checks(run(fine_grained_value(), ProbeForge(
        scopes=None, repo=repo_body(push=False, triage=False))))["close_issues"]
    assert fine_pull.state == "missing"
    refused = checks(run(classic_value(), ProbeForge(status={"repo": 403})))["close_issues"]
    assert refused.state == "missing"


# -- read issues ---------------------------------------------------------------------


def test_read_issues_reader() -> None:
    assert checks(run(classic_value(), ProbeForge()))["read_issues"].state == "ok"
    disabled = checks(run(classic_value(), ProbeForge(status={"issues": 410})))["read_issues"]
    assert disabled.state == "missing" and "issues are disabled" in disabled.reason
    refused = checks(run(classic_value(), ProbeForge(status={"issues": 403})))["read_issues"]
    assert refused.state == "missing"
    down = run(classic_value(), ProbeForge(raises={"issues": OSError()}))
    assert states(down)["read_issues"] is None and not down.complete


# -- workflow_dispatch ------------------------------------------------------------------


def test_workflow_dispatch_reader() -> None:
    classic = ProbeForge()
    assert checks(run(classic_value(), classic))["workflow_dispatch"].state == "ok"
    # Decided from the scope: the workflows list is not read for a classic token.
    assert "workflows" not in classic.keys()
    fine = checks(run(fine_grained_value(), ProbeForge(scopes=None)))["workflow_dispatch"]
    assert fine.state == "unknown"
    fine_403 = checks(run(fine_grained_value(), ProbeForge(scopes=None,
                                                           status={"workflows": 403})))
    assert fine_403["workflow_dispatch"].state == "missing"
    no_scope = checks(run(classic_value(), ProbeForge(scopes="public_repo")))
    assert no_scope["workflow_dispatch"].state == "missing"
    down = run(fine_grained_value(), ProbeForge(scopes=None, raises={"workflows": OSError()}))
    assert states(down)["workflow_dispatch"] is None and not down.complete


# -- expiry (§3.4) ------------------------------------------------------------------------


def test_parse_expiry() -> None:
    assert parse_expiry("2026-11-01 00:00:00 UTC") == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert parse_expiry("2026-11-01 00:00:00 -0800") == datetime(2026, 11, 1, 8, tzinfo=timezone.utc)
    assert parse_expiry("2026-11-01 00:00:00 +0000") == datetime(2026, 11, 1, tzinfo=timezone.utc)
    for bad in (None, "", "soon", "2026-13-01 00:00:00 UTC"):
        assert parse_expiry(bad) is None, bad


def test_the_probe_reads_expiry_and_rate_headers() -> None:
    result = run(classic_value(), ProbeForge(expiry="2026-10-20 00:00:00 UTC"))
    assert result.expiry_read and result.expires_at == datetime(2026, 10, 20, tzinfo=timezone.utc)
    assert result.rate_remaining == 4999
    none = run(classic_value(), ProbeForge(expiry=None))
    assert none.expiry_read and none.expires_at is None


# -- the routes -------------------------------------------------------------------------------


class Slots:
    """Secret Manager's git-token slots for any tenant, in memory."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.versions: dict[str, str] = {}
        self.asked: list[str] = []
        self.raises: Exception | None = None

    def put(self, secret_id: str, value: str, version: str = "1") -> None:
        self.values[secret_id] = value
        self.versions[secret_id] = version

    def token_for(self, tenant) -> str:
        return self.read_slot(tenant, forge.GIT_PROVIDER).value

    def read_slot(self, tenant, provider: str) -> forge.SlotValue:
        secret_id = tenant.secret_name(provider)
        self.asked.append(secret_id)
        if self.raises is not None:
            raise self.raises
        if secret_id not in self.values:
            raise forge.NoForgeCredential(f"no value stored in {secret_id}")
        return forge.SlotValue(self.values[secret_id], self.versions[secret_id])


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def slots() -> Slots:
    return Slots()


@pytest.fixture
def fake() -> ProbeForge:
    return ProbeForge()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def probe_client(db, tokens, group_map, slots, fake, clock) -> TestClient:
    context = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        forge_tokens=slots,
        forge=forge.GitHubIssues(probe_send=fake),
        now=clock,
    )
    seed_tenant(db, "eng", credentials=("git", "anthropic"))
    seed_tenant(db, "research", credentials=("git",))
    return TestClient(create_app(context), raise_server_exceptions=False)


def register_repo(client: TestClient) -> dict:
    response = client.post("/v1/git-tokens", json={"scope": "repository", "repository": OWNER_REPO},
                           headers=auth_header("root"))
    assert response.status_code in (200, 201), response.text
    return response.json()


def test_registration_runs_the_probe(probe_client, slots, fake, db) -> None:
    value = classic_value()
    repo_secret = f"swarm-tenant-eng-git-r-{REPO.removeprefix('repo_')}"
    slots.put(repo_secret, value)
    body = register_repo(probe_client)
    token = body["token"]
    assert token["state"] == "active"
    assert token["verified_at"] == T0.isoformat()
    assert token["kind"] == "classic_pat" and token["forge_login"] == "swarm-bot"
    assert token["last4"] == value[-4:]
    assert token["expires_at"] == "2026-12-01T00:00:00+00:00"
    (row,) = token["probe"]["repositories"]
    assert row["repo_id"] == REPO and row["repository"] == OWNER_REPO
    assert {cap: c["state"] for cap, c in row["capabilities"].items()} == {
        cap: "ok" for cap in CAPABILITIES}
    assert row["verified_at"] == T0.isoformat()
    assert f"{CHECKS_COLLECTION}/{token['token_id']}_{REPO}" in db.docs
    assert slots.asked == [repo_secret]


def test_rotation_is_seen_by_the_next_probe(probe_client, slots, clock) -> None:
    repo_secret = f"swarm-tenant-eng-git-r-{REPO.removeprefix('repo_')}"
    slots.put(repo_secret, classic_value(), version="1")
    token_id = register_repo(probe_client)["token"]["token_id"]
    rotated = classic_value()
    slots.put(repo_secret, rotated, version="2")
    clock.now = T0 + timedelta(hours=1)
    # Re-registering the slot after `create-secrets.sh --disable-previous` is
    # the console's "rotate": it probes again.
    token = register_repo(probe_client)["token"]
    assert token["token_id"] == token_id
    assert token["rotated_at"] == clock.now.isoformat()
    assert token["last4"] == rotated[-4:]


def test_a_slot_with_no_value_yet_stays_unverified(probe_client, fake) -> None:
    token = register_repo(probe_client)["token"]
    assert token["state"] == "unverified" and token["verified_at"] is None
    assert "no value stored" in token["probe"]["error"]
    assert "create-secrets.sh" in token["probe"]["error"]
    assert fake.calls == []


def test_verify_one_repository_and_all_known(probe_client, slots, fake, clock) -> None:
    slots.put("swarm-tenant-eng-git", classic_value())
    default_id = token_id_for("eng", "tenant", "")
    probe_client.get("/v1/git-tokens", headers=auth_header("alice"))
    # No repository named yet: the token-level read only (login, expiry).
    first = probe_client.post(f"/v1/git-tokens/{default_id}/verify", headers=auth_header("alice"))
    assert first.status_code == 200, first.text
    assert first.json()["token"]["probe"]["repositories"] == []
    assert first.json()["token"]["verified_at"] == T0.isoformat()
    assert fake.keys() == ["user", "orgs"]
    # Scoped to one repository: that pair is probed and remembered.
    clock.now = T0 + timedelta(minutes=5)
    scoped = probe_client.post(f"/v1/git-tokens/{default_id}/verify",
                               json={"repository": OWNER_REPO}, headers=auth_header("alice"))
    assert scoped.status_code == 200, scoped.text
    (row,) = scoped.json()["token"]["probe"]["repositories"]
    assert row["repository"] == OWNER_REPO and row["verified_at"] == clock.now.isoformat()
    # Then an unscoped verify probes every repository the token is known to cover.
    fake.calls.clear()
    clock.now = T0 + timedelta(minutes=10)
    again = probe_client.post(f"/v1/git-tokens/{default_id}/verify", headers=auth_header("alice"))
    assert "repo" in fake.keys()
    assert again.json()["token"]["verified_at"] == clock.now.isoformat()


def test_verify_refuses_a_repository_the_slot_does_not_cover(probe_client, slots, fake) -> None:
    slots.put(f"swarm-tenant-eng-git-r-{REPO.removeprefix('repo_')}", classic_value())
    token_id = register_repo(probe_client)["token"]["token_id"]
    fake.calls.clear()
    response = probe_client.post(f"/v1/git-tokens/{token_id}/verify",
                                 json={"repository": "saga-xyz/other"}, headers=auth_header("root"))
    assert response.status_code == 422, response.text
    assert fake.calls == []
    for body in ({"repository": "not a repo"}, {"value": "x"}, {"repo_id": "repo_zz"}):
        assert probe_client.post(f"/v1/git-tokens/{token_id}/verify", json=body,
                                 headers=auth_header("root")).status_code == 422


def test_a_partial_probe_does_not_move_last_verified(probe_client, slots, fake, clock) -> None:
    slots.put(f"swarm-tenant-eng-git-r-{REPO.removeprefix('repo_')}", classic_value())
    token_id = register_repo(probe_client)["token"]["token_id"]
    clock.now = T0 + timedelta(days=3)
    fake.raises["checks"] = OSError("connection reset")
    fake.status["issues"] = 410
    response = probe_client.post(f"/v1/git-tokens/{token_id}/verify", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    assert token["verified_at"] == T0.isoformat()
    assert token["probe"]["attempted_at"] == clock.now.isoformat()
    assert token["probe"]["complete"] is False
    assert "OSError" in token["probe"]["error"]
    (row,) = token["probe"]["repositories"]
    assert row["verified_at"] == T0.isoformat() and row["complete"] is False
    # The row that could not be read keeps its previous answer...
    assert row["capabilities"]["read_checks"]["state"] == "ok"
    assert row["capabilities"]["read_checks"]["verified_at"] == T0.isoformat()
    # ...and one that was read takes the new one.
    assert row["capabilities"]["read_issues"]["state"] == "missing"
    # The secret unreadable is a probe that did not run at all.
    slots.raises = forge.IssueReadFailed("swarm-api may not read the slot")
    clock.now = T0 + timedelta(days=4)
    blocked = probe_client.post(f"/v1/git-tokens/{token_id}/verify", headers=auth_header("alice"))
    assert blocked.status_code == 200
    assert blocked.json()["token"]["verified_at"] == T0.isoformat()
    assert blocked.json()["token"]["probe"]["complete"] is False


def test_an_expired_token_becomes_expired(probe_client, slots, fake) -> None:
    slots.put(f"swarm-tenant-eng-git-r-{REPO.removeprefix('repo_')}", classic_value())
    fake.expiry = "2026-10-01 00:00:00 UTC"
    token = register_repo(probe_client)["token"]
    assert token["state"] == "expired"
    assert token["expires_at"] == "2026-10-01T00:00:00+00:00"


def test_verify_is_tenant_scoped(probe_client, slots, fake) -> None:
    slots.put("swarm-tenant-eng-git", classic_value())
    probe_client.get("/v1/git-tokens", headers=auth_header("alice"))
    eng_default = token_id_for("eng", "tenant", "")
    missing_id = token_id_for("research", "repository", REPO)
    theirs = probe_client.post(f"/v1/git-tokens/{eng_default}/verify", headers=auth_header("bob"))
    missing = probe_client.post(f"/v1/git-tokens/{missing_id}/verify", headers=auth_header("bob"))
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json()["message"].replace(eng_default, "X") == (
        missing.json()["message"].replace(missing_id, "X"))
    # Nothing was read and nothing was sent for it.
    assert slots.asked == [] and fake.calls == []
    # And bob's list never shows eng's probe.
    listed = probe_client.get("/v1/git-tokens", headers=auth_header("bob")).json()["tokens"]
    assert all(t["tenant_id"] == "research" for t in listed)


def test_a_user_slot_is_verified_by_its_user_or_an_admin(probe_client, slots) -> None:
    mine = probe_client.post("/v1/git-tokens", json={"scope": "user"}, headers=auth_header("alice"))
    token_id = mine.json()["token"]["token_id"]
    other = probe_client.post("/v1/git-tokens", json={"scope": "user"}, headers=auth_header("root"))
    other_id = other.json()["token"]["token_id"]
    assert probe_client.post(f"/v1/git-tokens/{token_id}/verify",
                             headers=auth_header("alice")).status_code == 200
    assert probe_client.post(f"/v1/git-tokens/{other_id}/verify",
                             headers=auth_header("alice")).status_code == 403
    assert probe_client.post(f"/v1/git-tokens/{token_id}/verify",
                             headers=auth_header("root")).status_code == 200


def test_a_revoked_token_is_not_probed(probe_client, slots, fake) -> None:
    slots.put("swarm-tenant-eng-git", classic_value())
    probe_client.get("/v1/git-tokens", headers=auth_header("alice"))
    default_id = token_id_for("eng", "tenant", "")
    assert probe_client.delete(f"/v1/git-tokens/{default_id}",
                               headers=auth_header("root")).status_code == 200
    response = probe_client.post(f"/v1/git-tokens/{default_id}/verify", headers=auth_header("root"))
    assert response.status_code == 409
    assert slots.asked == [] and fake.calls == []


def test_list_and_record_carry_the_probe_summaries(probe_client, slots) -> None:
    slots.put(f"swarm-tenant-eng-git-r-{REPO.removeprefix('repo_')}", classic_value())
    token_id = register_repo(probe_client)["token"]["token_id"]
    listed = probe_client.get("/v1/git-tokens", headers=auth_header("alice")).json()["tokens"]
    by_id = {t["token_id"]: t for t in listed}
    one = probe_client.get(f"/v1/git-tokens/{token_id}", headers=auth_header("alice")).json()["token"]
    for token in (by_id[token_id], one):
        (row,) = token["probe"]["repositories"]
        assert row["summary"] == {"ok": 8, "missing": 0, "unknown": 0}
        assert set(row["capabilities"]) == set(CAPABILITIES)
        for cell in row["capabilities"].values():
            assert set(cell) >= {"state", "reason", "evidence", "verified_at"}
    # The default, never probed, says so rather than inventing rows.
    default = by_id[token_id_for("eng", "tenant", "")]
    assert default["probe"]["repositories"] == [] and default["probe"]["attempted_at"] is None


def test_the_secret_appears_in_no_response_and_no_log(probe_client, slots, fake, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    value = classic_value()
    slots.put(f"swarm-tenant-eng-git-r-{REPO.removeprefix('repo_')}", value)
    chatty = logging.getLogger("tests.chatty_transport")

    def careless(url: str, headers: dict[str, str]) -> None:
        # A careless library between us and the forge, logging what it sends.
        chatty.debug("sending %s with %s", url, headers.get("Authorization"))
        chatty.debug("raw token in message: " + value)

    fake.on_call = careless
    fake.status["checks"] = 403
    fake.body["checks"] = {"message": f"Bad credentials for {value}"}
    fake.raises["issues"] = OSError(f"connect failed with Authorization: token {value}")

    texts = [register_repo(probe_client)]
    token_id = texts[0]["token"]["token_id"]
    texts.append(probe_client.post(f"/v1/git-tokens/{token_id}/verify",
                                   headers=auth_header("alice")).json())
    texts.append(probe_client.get("/v1/git-tokens", headers=auth_header("alice")).json())
    texts.append(probe_client.get(f"/v1/git-tokens/{token_id}",
                                  headers=auth_header("alice")).json())
    assert any(rec.name == "tests.chatty_transport" for rec in caplog.records)
    for text in texts:
        dumped = json.dumps(text)
        assert value not in dumped
        assert value[4:] not in dumped
    for record in caplog.records:
        message = record.getMessage()
        assert value not in message and value[4:] not in message, message
    # The forge's own message reached the evidence, masked.
    row = texts[1]["token"]["probe"]["repositories"][0]
    assert "Bad credentials" in row["capabilities"]["read_checks"]["evidence"]
    assert row["capabilities"]["read_checks"]["state"] == "missing"
    # The transport's exception is named by its type only.
    assert "OSError" in texts[1]["token"]["probe"]["error"]


def test_the_slot_value_never_prints() -> None:
    value = classic_value()
    assert value not in repr(forge.SlotValue(value, "3"))


def test_records_hold_no_value(probe_client, slots, db) -> None:
    value = classic_value()
    slots.put(f"swarm-tenant-eng-git-r-{REPO.removeprefix('repo_')}", value)
    register_repo(probe_client)
    for path, doc in db.docs.items():
        if path.startswith((COLLECTION + "/", CHECKS_COLLECTION + "/")):
            assert value not in json.dumps(doc, default=str)
