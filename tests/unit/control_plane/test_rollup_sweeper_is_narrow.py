"""The rollup sweeper reaches ONE route: POST /v1/admin/workflows/rollup.

WHY THIS EXISTS
---------------
D17, owner's chosen shape. `POST /v1/admin/workflows/rollup` converges the
STORED `Workflow.state` of workflows nobody reads (docs/workflows.md, "Workflow
state"), and nothing called it on a schedule, so a workflow nobody listed kept
a stale stored state for ever. terraform/modules/scheduler/jobs.tf now calls it
once per registered tenant, with an OIDC token for a dedicated
`swarm-rollup-sweeper` account, and swarm-api lets that account
(`ApiSettings.rollup_sweeper_users`, env ROLLUP_SWEEPER_USERS) through on that
one route and on nothing else (`auth.ROLLUP_SWEEPER_ROUTES`).

It is NOT an admin. Admin is one boolean that opens every /v1/admin route,
including `PUT /v1/admin/tenants/{id}/limits`, which can disable any tenant. A
Cloud Scheduler job's identity holding that would make a scheduler
misconfiguration a tenant outage. And it is not a TENANT MEMBER either: it is
refused on every authenticated route, admin or not, so a leaked sweeper token
lists no task and submits no work.

WHAT IS SWEPT. Every route in every router module whose dependency tree
authenticates the caller (`current_auth`), collected the same way
test_continuation_scope_is_narrow.py collects them, so a route added tomorrow
is in these cases the day it exists.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import ROLLUP_SWEEPER_ROUTES, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import AppContext, build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import api_settings, auth_header, seed_task, seed_tenant
from .test_continuation_scope_is_narrow import SWEPT, _ids, _send
from .test_workflow_state_rollup import make_workflow, seed_workflow

#: The identity terraform/modules/scheduler/jobs.tf gives the rollup jobs. A
#: service-account address, outside the allowed domain on purpose: it is
#: admitted by being on ROLLUP_SWEEPER_USERS, not through ALLOWED_USERS.
SWEEPER = "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
SWEEPER_HEADERS = {"Authorization": "Bearer token-sweeper"}

#: THE OWNER'S DECISION as a closed set, deliberately a literal: derived from
#: ROLLUP_SWEEPER_ROUTES, widening that set would widen what a stolen sweeper
#: token reaches and every test here would still pass.
#: The issue-run tick (#454, owner decision "Advancing runs: swarm-api, on a
#: Cloud Scheduler tick") was added to it on purpose: it moves only runs
#: already in the named tenant, as their own creators.
#: The repository index poll (docs/repo-index.md §3.3, lane RI4) was added for
#: the same reason: it reads only the named tenant's registrations and
#: submits an index run as the registration's creator, never as the sweeper.
#: The merge wake (docs/merge-step.md "Revised 2026-10-06" §1, lane MS2) was
#: added on the owner's 2026-10-06 plan: it reads only the named tenant's
#: CI_PENDING parks, with that tenant's token, and its one write is the wake
#: marker on them -- it submits nothing and moves no task.
#: The GitHub user-token refresh sweep (docs/onboarding.md §3.4 item 6, owner
#: decision D2 on #780, lane OB3) was added for the swarm-forge-refresh job
#: OB2 built: it refreshes due connections into their own users' slots, and
#: submits nothing and moves no task.
#: The personal-workspace dispatch sweep (docs/workspaces.md §2.2, lane W7
#: of #847) is called by swarm-tick every 5 minutes: it publishes again the
#: opaque id of a workspace an admin already approved, and approves nothing.
#: The `task_finished` push (#748, filed by the owner 2026-10-06) was added
#: so a MERGE verdict's pull request is opened without a worker: it acts only
#: on the finished task's own tenant's gated integrator, with that tenant's
#: token, and moves only a step it claimed (`swarm_api.verdictpublish`).
DECIDED = frozenset({
    ("POST", "/v1/admin/workflows/rollup"),
    ("POST", "/v1/admin/runs/advance"),
    ("POST", "/v1/admin/repositories/poll"),
    ("POST", "/v1/admin/merges/wake"),
    ("POST", "/v1/admin/forge/refresh"),
    ("POST", "/v1/admin/workspaces/sweep"),
    ("POST", "/v1/admin/tasks/finished"),
    # NOT POST /v1/admin/issues/sweep: the issue sweeper's job and route were
    # removed on 2026-10-12 (Schedules S9, second change), replaced by the
    # tenant's `issue-sweep` schedule, which the schedule tick fires.
    # The stranded-PR sweep (part of #295, jobs.tf `stranded_pr_sweep`): it
    # reads only the named tenant's pull requests and writes only its
    # `stranded_prs` document; a redrive from this identity is refused
    # (test_stranded_prs.py), so it can submit nothing.
    ("POST", "/v1/admin/stranded-prs/sweep"),
    # The workspace secrets rotation sweep (#1347, owner decision Q5;
    # jobs.tf `secrets_rotation_sweep`): it disables previous versions whose
    # 24-hour grace has passed and never destroys one. It reads no value,
    # grants nothing, submits nothing, and takes no argument: what it may
    # disable is fixed by the records' own `disable_after`.
    ("POST", "/v1/admin/workspace-secrets/rotation-sweep"),
    # The GitHub push hint (docs/design/graph-service.md §6.2; owner decision
    # 2026-10-16 on #1384, lane GS-HOOKS): the public receiver
    # swarm-github-hooks RUNS AS this account and forwards a verified push's
    # repository name here. The route reads no payload sha, takes no tenant,
    # and only polls now what the five-minute poll would poll anyway, each
    # registration with its own workspace's token. The owner accepted that a
    # public-facing service then holds every route in this set.
    ("POST", "/v1/admin/repositories/push"),
    # The per-run MCP credential sweep (docs/design/registered-mcp-servers.md
    # §4.4, owner decision Q2 of #1433; jobs.tf, lane MCP-IAM): it deletes the
    # `-mcpc-` secret of a credential whose task ended or whose expiry passed,
    # and marks the row. It takes no argument, reads no value, submits
    # nothing and grants nothing; what it may destroy is fixed by each row.
    ("POST", "/v1/admin/mcp-credentials/sweep"),
})

REFUSED = [r for r in SWEPT if r not in DECIDED]


def _context(db, tokens, group_map, objects, *, verified: Any = True,
             **extra: Any) -> AppContext:
    tokens = dict(tokens)
    tokens["token-sweeper"] = {
        "email": SWEEPER,
        "email_verified": verified,
        "sub": "sub-sweeper",
    }
    return build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,), **extra),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )


@pytest.fixture
def sweeper_context(db, tokens, group_map, objects) -> AppContext:
    seed_tenant(db, "eng")
    return _context(db, tokens, group_map, objects)


@pytest.fixture
def sweeper_client(sweeper_context) -> TestClient:
    return TestClient(create_app(sweeper_context), raise_server_exceptions=False)


def test_the_allow_list_is_the_decided_one() -> None:
    assert set(ROLLUP_SWEEPER_ROUTES) == set(DECIDED)


def test_the_sweep_is_not_empty_and_contains_the_decided_route() -> None:
    assert set(DECIDED) <= set(SWEPT), "the rollup route was not collected by the sweep"
    assert len(REFUSED) > 20, f"the refused sweep is implausibly small: {len(REFUSED)}"
    assert ("PUT", "/v1/admin/tenants/{tenant_id}/limits") in REFUSED
    assert ("POST", "/v1/tasks") in REFUSED


def test_the_rotation_sweep_is_the_only_workspace_secrets_route_the_sweeper_reaches() -> None:
    """#1347 added ONE route. Every other workspace-secrets route -- the
    writes, the grants, the form links, the listing -- stays refused to this
    identity, so a leaked sweeper token can disable a version only once its
    grace has passed, and can read, add, grant or delete nothing."""
    def is_secrets_route(route: tuple[str, str]) -> bool:
        path = route[1]
        return path.startswith("/v1/workspace/secrets") or "/workspace-secrets/" in path

    reached = {r for r in ROLLUP_SWEEPER_ROUTES if is_secrets_route(r)}
    assert reached == {("POST", "/v1/admin/workspace-secrets/rotation-sweep")}
    refused = [r for r in REFUSED if is_secrets_route(r)]
    assert len(refused) >= 5, f"the workspace-secrets routes were not swept: {refused}"
    assert ("PUT", "/v1/workspace/secrets/{name}/value") in refused
    assert ("DELETE", "/v1/workspace/secrets/{name}") in refused


class _RecordingWriter:
    """The rotation sweep's Secret Manager writer, recording what it is
    asked; nothing here may create, add or destroy."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def create_secret(self, tenant_id: str, secret_id: str) -> None:
        raise AssertionError("the rotation sweep never creates a secret")

    def add_version(self, secret_id: str, payload: bytes) -> str:
        raise AssertionError("the rotation sweep never adds a version")

    def disable_version(self, version: str) -> None:
        self.calls.append(("disable_version", version))

    def destroy_versions(self, secret_id: str) -> int:
        raise AssertionError("the rotation sweep never destroys")


def test_the_sweeper_runs_the_rotation_sweep(db, sweeper_context) -> None:
    """What swarm-workspace-secrets-rotation-sweep does each hour, through
    the real app: a version past its grace is disabled, by the sweeper."""
    from datetime import timedelta

    from swarm_api.workspace_secrets import ROTATION_GRACE, SECRETS_COLLECTION

    now = sweeper_context.now()
    secret_id = "swarm-tenant-eng-wsec-" + "0" * 16
    db.docs[f"{SECRETS_COLLECTION}/{secret_id}"] = {
        "secret_id": secret_id, "tenant_id": "eng", "name": "typesafe-key", "state": "active",
        "current_version": f"projects/test/secrets/{secret_id}/versions/2",
        "versions": [
            {"version": f"projects/test/secrets/{secret_id}/versions/1", "state": "enabled",
             "disable_after": now - ROTATION_GRACE - timedelta(minutes=1)},
            {"version": f"projects/test/secrets/{secret_id}/versions/2", "state": "enabled",
             "disable_after": None},
        ],
    }
    app = create_app(sweeper_context)
    writer = _RecordingWriter()
    app.state.workspace_secret_writer = writer
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/v1/admin/workspace-secrets/rotation-sweep", headers=SWEEPER_HEADERS)

    assert response.status_code == 200, response.text
    assert response.json()["disabled"] == 1, response.text
    assert writer.calls == [("disable_version", f"projects/test/secrets/{secret_id}/versions/1")]
    states = [v["state"] for v in db.docs[f"{SECRETS_COLLECTION}/{secret_id}"]["versions"]]
    assert states == ["disabled", "enabled"]


def test_the_sweeper_runs_the_mcp_credential_sweep(db, tokens, group_map, objects) -> None:
    """What the 10-minute MCP credential sweep job does, through the real app:
    a terminal task's per-run credential is deleted and its row marked, by
    the sweeper (design registered-mcp-servers.md §4.4)."""
    from datetime import timedelta

    from swarm_api.mcp_credentials import COLLECTION, RowKeys

    from .test_child_tasks_api import KEY

    seed_tenant(db, "eng")
    sweeper_context = _context(db, tokens, group_map, objects, child_key=KEY)
    now = sweeper_context.now()
    secret_id = "swarm-tenant-eng-mcpc-" + "0" * 16
    seed_task(db, task_id="t_done", tenant_id="eng", state="SUCCEEDED")
    row = {
        "owner_id": "t_done", "owner_kind": "task", "tenant_id": "eng",
        "server_id": "mcp_0000000000000000", "name": "kb", "secret_id": secret_id,
        "version": f"projects/test/secrets/{secret_id}/versions/1",
        "expires_at": (now + timedelta(hours=1)).replace(microsecond=0), "state": "live",
    }
    row["attestation"] = RowKeys(KEY).sign(row)
    db.docs[f"{COLLECTION}/t_done~mcp_0000000000000000"] = row
    deleted: list[str] = []

    class _Writer:
        def create_secret(self, *args: Any) -> None:
            raise AssertionError("the sweep never creates a secret")

        def add_version(self, *args: Any) -> str:
            raise AssertionError("the sweep never adds a version")

        def delete_secret(self, secret: str) -> None:
            deleted.append(secret)

    app = create_app(sweeper_context)
    app.state.mcp_credential_writer = _Writer()
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/v1/admin/mcp-credentials/sweep", headers=SWEEPER_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["destroyed"] == 1
    assert deleted == [secret_id]
    assert db.docs[f"{COLLECTION}/t_done~mcp_0000000000000000"]["state"] == "destroyed"


def test_the_mcp_sweep_is_the_only_mcp_route_the_sweeper_reaches() -> None:
    """MCP-API added ONE route for this identity. Registering, changing or
    reading a server stays refused to it."""
    reached = {r for r in ROLLUP_SWEEPER_ROUTES if "mcp" in r[1]}
    assert reached == {("POST", "/v1/admin/mcp-credentials/sweep")}
    refused = [r for r in REFUSED if r[1].startswith("/v1/mcp-servers")]
    assert len(refused) >= 4, f"the MCP server routes were not swept: {refused}"


def test_the_sweeper_reaches_the_push_route(db, sweeper_client) -> None:
    """GS-HOOKS: the receiver calls POST /v1/admin/repositories/push as the
    sweeper, through the real app's route allow-list (the push route's own
    tests override the caller and so never met that list). A push for a
    repository nobody registered polls nothing and answers 200."""
    response = sweeper_client.post(
        "/v1/admin/repositories/push",
        json={"forge": "github", "full_name": "nobody-registered/this-repository"},
        headers=SWEEPER_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert "rollup sweeper may not call" not in response.text


def test_the_push_route_is_still_the_sweepers_alone(db, sweeper_client) -> None:
    """The control: naming the route in the allow-list opened it to the
    sweeper, not to a member, whose push is refused by the route itself."""
    response = sweeper_client.post(
        "/v1/admin/repositories/push",
        json={"forge": "github", "full_name": "nobody-registered/this-repository"},
        headers=auth_header("alice"),
    )
    assert response.status_code == 403, response.text


def test_the_push_route_is_the_only_repository_route_the_sweeper_gained() -> None:
    """GS-HOOKS added ONE route. Registering, editing, deleting or indexing a
    repository stays refused to this identity, so the public receiver that
    holds it can poll now and nothing else on repositories."""
    reached = {r for r in ROLLUP_SWEEPER_ROUTES if "/repositories" in r[1]}
    assert reached == {("POST", "/v1/admin/repositories/poll"), ("POST", "/v1/admin/repositories/push")}
    refused = [r for r in REFUSED if "/repositories" in r[1]]
    assert len(refused) >= 3, f"the repository routes were not swept: {refused}"


def test_the_sweeper_converges_a_workflow_nobody_looked_at(db, sweeper_client) -> None:
    """What the Cloud Scheduler job does, end to end through the real app."""
    seed_task(db, task_id="t1", tenant_id="eng", state="SUCCEEDED")
    seed_workflow(db, make_workflow(steps=[("a", "t1")]))

    response = sweeper_client.post(
        "/v1/admin/workflows/rollup?tenant_id=eng", headers=SWEEPER_HEADERS
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_id"] == "eng"
    assert body["report"]["written"] == 1
    assert db.docs["workflows/wf_test"]["state"] == "SUCCEEDED"


@pytest.mark.parametrize("method,path", REFUSED, ids=_ids(REFUSED))
def test_every_other_authenticated_route_is_refused_to_the_sweeper(
    sweeper_client, method, path
) -> None:
    response = _send(sweeper_client, method, path, SWEEPER_HEADERS)
    assert response.status_code == 403, (
        f"{method} {path} answered {response.status_code} {response.text[:200]}"
    )
    assert "rollup sweeper" in response.text, response.text[:200]


def test_the_sweeper_is_neither_an_admin_nor_a_tenant_member(sweeper_context) -> None:
    ctx = sweeper_context.authenticator.authenticate("Bearer token-sweeper")
    assert ctx.is_rollup_sweeper is True
    assert ctx.is_admin is False
    assert ctx.is_pool_admin is False
    assert ctx.member_scope == ""
    assert ctx.tenant_choices == ()


@pytest.mark.parametrize("verified", [False, None])
def test_a_sweeper_token_without_an_explicitly_verified_email_is_refused(
    db, tokens, group_map, objects, verified
) -> None:
    """The sweeper skips the domain check and both Cloud Identity passes, so
    `email_verified: true` is the only outside confirmation left -- the same
    rule the listed-service-account path applies."""
    seed_tenant(db, "eng")
    client = TestClient(
        create_app(_context(db, tokens, group_map, objects, verified=verified)),
        raise_server_exceptions=False,
    )
    response = client.post("/v1/admin/workflows/rollup?tenant_id=eng", headers=SWEEPER_HEADERS)
    assert response.status_code == 401, response.text


def test_an_ordinary_member_still_cannot_call_the_rollup_route(db, sweeper_client) -> None:
    """The capability is per identity: adding it opened the route to nobody else."""
    response = sweeper_client.post(
        "/v1/admin/workflows/rollup?tenant_id=eng", headers=auth_header("alice")
    )
    assert response.status_code == 403


def test_a_full_admin_still_can(db, sweeper_client) -> None:
    response = sweeper_client.post(
        "/v1/admin/workflows/rollup?tenant_id=eng", headers=auth_header("root")
    )
    assert response.status_code == 200, response.text


def test_an_address_that_is_both_a_sweeper_and_an_admin_is_refused_at_startup(monkeypatch) -> None:
    """Two roles on one address make which one applies depend on code order."""
    from swarm_api.settings import ApiSettings

    for name, value in {
        "ENVIRONMENT": "dev",
        "PROJECT_ID": "saga-agents-staging",
        "ROLLUP_SWEEPER_USERS": SWEEPER,
        "ADMIN_USERS": SWEEPER,
    }.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="ROLLUP_SWEEPER_USERS"):
        ApiSettings.from_env()


def test_rollup_sweeper_users_is_read_from_the_environment(monkeypatch) -> None:
    from swarm_api.settings import ApiSettings

    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("PROJECT_ID", "saga-agents-staging")
    monkeypatch.setenv("ROLLUP_SWEEPER_USERS", f" {SWEEPER} ")
    assert ApiSettings.from_env().rollup_sweeper_users == (SWEEPER,)
