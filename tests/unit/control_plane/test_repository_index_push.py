"""A push to a registered repository polls it now: `POST /v1/admin/repositories/push`.

docs/design/graph-service.md §6.2, lane GS-PUSHAPI (part of #1384). The
public receiver `swarm-github-hooks` (lane GS-HOOKS) verifies GitHub's
signature, dedups the delivery and forwards `{forge, full_name}` and nothing
else. What is held here:

  * a push maps to EVERY registration of that repository, in every workspace
    that registered it, matched on the lower-cased `owner/repo`; each is
    polled with its own workspace's credential and queued as its own creator,
    in its own tenant (decision 3, invariant 9);
  * the push runs the poll's own `RepoIndex._poll_one`, so the push and the
    poll can never decide differently: the throttle, `paused` and the
    in-flight rule hold for a push exactly as for a tick;
  * nothing from the payload but the name is accepted, so a commit sha a
    payload carries is never read, trusted or stored: the head is read from
    GitHub with the workspace's token;
  * `webhook` is "poll and push": a registration on `poll` or `off` is not
    read for a push;
  * only the identity the route admits may call it, and nobody else -- not
    a member, not an admin.

No credentials, no network: GitHub is a fake transport under the real client.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api import forge, repoindex
from swarm_api.auth import AuthContext, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import admin_auth, build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker
from swarm_common.identity import Principal

from .conftest import api_settings, auth_header, seed_tenant
from .repo_fakes import TenantTokens, repo_entry
from .repo_index_fakes import (
    REPOSITORY,
    HeadPolls,
    IndexGitHub,
    etag_of,
    finish_index_task,
    fixture_index,
    sha,
)

ONE, TWO, THREE = sha("one"), sha("two"), sha("three")
OTHER = "example-org/gadgets"
PUSH_URL = "/v1/admin/repositories/push"

#: The identity the route admits, as `poll_repositories` admits the
#: scheduler's: the rollup sweeper (`AuthContext.is_rollup_sweeper`).
SWEEPER = "swarm-rollup-sweeper@example-project.iam.gserviceaccount.com"


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta: float) -> None:
        self.at = self.at + timedelta(**delta)


def _caller(email: str, *, sweeper: bool = False, admin: bool = False) -> AuthContext:
    return AuthContext(
        principal=Principal(email=email, subject=f"sub-{email}",
                            domain=email.rsplit("@", 1)[-1], groups=()),
        tenant_id="u-caller", is_admin=admin, tenant_principal=email,
        is_rollup_sweeper=sweeper,
    )


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def github():
    return IndexGitHub(
        heads={"main": ONE},
        repos={
            REPOSITORY: repo_entry(REPOSITORY, default_branch="main"),
            OTHER: repo_entry(OTHER, default_branch="main"),
        },
    )


@pytest.fixture
def polls(github):
    return HeadPolls(github)


@pytest.fixture
def secrets_reader():
    return TenantTokens()


@pytest.fixture
def app(db, tokens, group_map, objects, github, polls, secrets_reader, clock):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    ctx = build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)),
        db=db,
        verifier=StaticTokenVerifier(dict(tokens)),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=secrets_reader,
        forge=forge.GitHubIssues(send=github, probe_send=polls),
        now=clock,
    )
    return create_app(ctx)


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def as_caller(app):
    """Pass the admin gate as `context`, so the route's own check is what is tested.

    Admitting the hooks identity through `auth.require_admin` is an
    `auth.py` change outside this lane (questions.json, the PR body); the
    route body decides the rest, and that is what these tests hold."""

    def _set(context: AuthContext) -> None:
        app.dependency_overrides[admin_auth] = lambda: context

    yield _set
    app.dependency_overrides.pop(admin_auth, None)


@pytest.fixture
def sweeper(as_caller):
    as_caller(_caller(SWEEPER, sweeper=True))


def _register(client, repository=REPOSITORY, user="alice", on_change="webhook") -> str:
    created = client.post(
        "/v1/repositories",
        json={"repository": repository, "index": {"on_change": on_change}},
        headers=auth_header(user),
    )
    assert created.status_code == 201, created.text
    return created.json()["repository"]["repo_id"]


def _push(client, full_name=REPOSITORY, **extra):
    return client.post(PUSH_URL, json={"forge": "github", "full_name": full_name, **extra})


def _tasks(db, tenant_id=None) -> list[dict]:
    return [doc for path, doc in db.docs.items()
            if path.startswith("tasks/") and path.count("/") == 1
            and (tenant_id is None or doc.get("tenant_id") == tenant_id)]


def _index(db, repo_id) -> dict:
    return db.docs[f"repositories/{repo_id}"]["index"]


def _indexed(db, repo_id, commit, at) -> None:
    _index(db, repo_id).update(current_sha=commit, last_indexed_at=at, head_sha=commit,
                               head_read_at=at, etag=etag_of(commit), etag_sha=commit)


# --------------------------------------------------------------------------
# the mapping: every workspace that registered the repository
# --------------------------------------------------------------------------

def test_a_push_polls_the_repository_in_every_workspace_that_registered_it(
    client, db, polls, secrets_reader, sweeper
):
    eng = _register(client, user="alice")
    research = _register(client, user="bob")
    assert eng != research  # one registration per workspace (repo_id_for)
    secrets_reader.asked.clear()

    response = _push(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report"]["registrations"] == 2
    assert body["report"]["submitted"] == 2 and body["failures"] == []
    # Each workspace's head read with its OWN credential.
    assert sorted(secrets_reader.asked) == ["swarm-tenant-eng-git", "swarm-tenant-research-git"]
    assert len(polls.calls) == 2
    # Each run in its own tenant, as its own registration's creator.
    [eng_task] = _tasks(db, "eng")
    [research_task] = _tasks(db, "research")
    assert eng_task["submitted_by"] == "alice@example.com"
    assert research_task["submitted_by"] == "bob@example.com"
    assert eng_task["metadata"]["repo_index"] == eng
    assert research_task["metadata"]["repo_index"] == research
    assert _index(db, eng)["in_flight_task_id"] == eng_task["id"]
    assert _index(db, research)["in_flight_task_id"] == research_task["id"]


def test_the_name_is_matched_without_regard_to_case(client, db, sweeper):
    repo_id = _register(client)
    response = _push(client, "Example-Org/Widgets")
    assert response.status_code == 200, response.text
    assert response.json()["report"]["registrations"] == 1
    [task] = _tasks(db)
    assert task["metadata"]["repo_index"] == repo_id


def test_another_repository_is_never_touched(client, db, polls, secrets_reader, sweeper):
    widgets = _register(client)
    gadgets = _register(client, OTHER, user="bob")
    secrets_reader.asked.clear()

    body = _push(client).json()

    assert body["report"]["registrations"] == 1
    assert [url for url, _h, _s in polls.calls] == [
        "https://api.github.com/repos/example-org/widgets/commits/main"
    ]
    assert secrets_reader.asked == ["swarm-tenant-eng-git"]
    assert [t["metadata"]["repo_index"] for t in _tasks(db)] == [widgets]
    untouched = _index(db, gadgets)
    assert untouched.get("head_read_at") is None and untouched.get("last_poll") is None


def test_a_push_for_a_repository_nobody_registered_does_nothing(client, db, polls, sweeper):
    _register(client)
    response = _push(client, "someone/else")
    assert response.status_code == 200, response.text
    assert response.json()["report"]["registrations"] == 0
    assert polls.calls == [] and _tasks(db) == []


@pytest.mark.parametrize("on_change", ["poll", "off"])
def test_only_a_webhook_registration_is_read_for_a_push(
    client, db, polls, sweeper, on_change
):
    """`poll` means poll only and `off` neither (§6.2): a push is not theirs."""
    repo_id = _register(client, on_change=on_change)

    body = _push(client).json()

    assert body["report"]["registrations"] == 1
    assert body["report"]["submitted"] == 0
    assert body["report"]["held"] == {f"on_change_{on_change}": 1}
    assert polls.calls == [] and _tasks(db) == []
    assert _index(db, repo_id).get("head_read_at") is None


# --------------------------------------------------------------------------
# the poll's own decision path, reused
# --------------------------------------------------------------------------

def test_a_push_runs_the_polls_own_poll_one(client, db, monkeypatch, sweeper):
    eng = _register(client, user="alice")
    research = _register(client, user="bob")
    seen: list[tuple[str, str]] = []
    real = repoindex.RepoIndex._poll_one

    def spy(self, tenant_id, record, tenant, owner_auth, report):
        seen.append((tenant_id, record["repo_id"]))
        assert tenant.tenant_id == tenant_id
        return real(self, tenant_id, record, tenant, owner_auth, report)

    monkeypatch.setattr(repoindex.RepoIndex, "_poll_one", spy)

    assert _push(client).status_code == 200

    assert sorted(seen) == sorted([("eng", eng), ("research", research)])
    # And `_poll_one` stored the tick as the poll does.
    assert _index(db, eng)["last_poll"]["outcome"] == "queued"


def test_the_minimum_change_interval_holds_a_push_back(
    client, db, objects, github, clock, sweeper
):
    repo_id = _register(client)
    _push(client)
    [task] = _tasks(db)
    finish_index_task(db, objects, task["id"], fixture_index(ONE))
    clock.advance(minutes=10)
    github.heads["main"] = TWO

    early = _push(client).json()["report"]
    assert early["submitted"] == 0 and early["held"] == {"throttled": 1}
    assert _index(db, repo_id)["head_sha"] == TWO  # read all the same

    clock.advance(minutes=21)
    later = _push(client).json()["report"]
    assert later["submitted"] == 1
    assert sorted(t["repository_ref"] for t in _tasks(db)) == sorted([ONE, TWO])


def test_a_push_never_duplicates_a_run_in_flight(client, db, github, sweeper, clock):
    repo_id = _register(client)
    assert _push(client).json()["report"]["submitted"] == 1
    clock.advance(minutes=1)
    again = _push(client).json()["report"]
    assert again["submitted"] == 0
    assert len(_tasks(db)) == 1
    assert _index(db, repo_id)["last_poll"]["reason"] in ("current", "in_flight", "throttled")


def test_a_paused_registration_is_not_read_for_a_push(client, db, polls, sweeper):
    repo_id = _register(client)
    client.patch(f"/v1/repositories/{repo_id}", json={"paused": True},
                 headers=auth_header("alice"))
    report = _push(client).json()["report"]
    assert report["skipped"] == 1 and report["submitted"] == 0
    assert polls.calls == [] and _tasks(db) == []


def test_one_registrations_failure_does_not_stop_the_others(client, db, sweeper):
    eng = _register(client, user="alice")
    research = _register(client, user="bob")
    # carol is in no tenant group: the creator as they would read once removed.
    db.docs[f"repositories/{eng}"]["created_by"] = "carol@example.com"

    body = _push(client).json()

    assert body["failures"] == [{"repo_id": eng, "code": "owner_not_member"}]
    assert body["report"]["submitted"] == 1
    [task] = _tasks(db)
    assert task["metadata"]["repo_index"] == research


# --------------------------------------------------------------------------
# the payload's sha is never trusted or stored
# --------------------------------------------------------------------------

def test_a_payload_sha_is_refused_and_nothing_is_read(client, db, polls, sweeper):
    _register(client)
    response = _push(client, after=THREE)
    assert response.status_code == 422, response.text
    assert polls.calls == [] and _tasks(db) == []
    assert THREE not in repr(db.docs)


def test_the_head_stored_is_the_one_read_with_the_workspaces_token(
    client, db, github, polls, clock, sweeper
):
    """A valid signature over a stale payload can at most cause one extra
    head read (§6.2): what is stored and indexed is GitHub's answer."""
    repo_id = _register(client)
    _indexed(db, repo_id, ONE, clock() - timedelta(hours=2))
    github.heads["main"] = TWO

    _push(client)

    assert _index(db, repo_id)["head_sha"] == TWO
    [task] = _tasks(db)
    assert task["repository_ref"] == TWO
    assert polls.calls[-1][0] == "https://api.github.com/repos/example-org/widgets/commits/main"


@pytest.mark.parametrize("body", [
    {"forge": "gitlab", "full_name": REPOSITORY},
    {"full_name": REPOSITORY},
    {"forge": "github"},
    {"forge": "github", "full_name": "no-slash"},
    {"forge": "github", "full_name": "a/b/c"},
    {"forge": "github", "full_name": "../widgets"},
    {"forge": "github", "full_name": REPOSITORY, "tenant_id": "eng"},
    {"forge": "github", "full_name": REPOSITORY, "ref": "refs/heads/main"},
])
def test_a_body_beyond_the_name_is_refused(client, db, polls, sweeper, body):
    _register(client)
    response = client.post(PUSH_URL, json=body)
    assert response.status_code == 422, (body, response.text)
    assert polls.calls == [] and _tasks(db) == []


# --------------------------------------------------------------------------
# who may call it
# --------------------------------------------------------------------------

@pytest.mark.parametrize("user", ["alice", "root"])
def test_a_person_may_not_push(client, db, user):
    _register(client)
    response = client.post(PUSH_URL, json={"forge": "github", "full_name": REPOSITORY},
                           headers=auth_header(user))
    assert response.status_code == 403, response.text
    assert _tasks(db) == []


def test_an_unauthenticated_push_is_refused(client, db):
    _register(client)
    response = _push(client)
    assert response.status_code == 401, response.text
    assert _tasks(db) == []


def test_the_route_itself_admits_only_its_identity(client, db, as_caller, polls):
    """Past the admin gate, an admin is still not the push's identity."""
    _register(client)
    as_caller(_caller("root@example.com", admin=True))
    response = _push(client)
    assert response.status_code == 403, response.text
    assert polls.calls == [] and _tasks(db) == []


def test_the_route_is_behind_the_admin_gate():
    """`test_every_route_under_v1_admin_is_admin_gated`'s rule, for this route."""
    from swarm_api.routes import admin

    [route] = [r for r in admin.router.routes
               if getattr(r, "path", None) == "/v1/admin/repositories/push"]

    def calls(dependant):
        yield dependant.call
        for sub in dependant.dependencies:
            yield from calls(sub)

    assert admin_auth in set(calls(route.dependant))
    assert route.methods == {"POST"}
