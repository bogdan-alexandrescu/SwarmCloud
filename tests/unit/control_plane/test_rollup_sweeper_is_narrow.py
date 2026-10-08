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
DECIDED = frozenset({
    ("POST", "/v1/admin/workflows/rollup"),
    ("POST", "/v1/admin/runs/advance"),
    ("POST", "/v1/admin/repositories/poll"),
    ("POST", "/v1/admin/merges/wake"),
    ("POST", "/v1/admin/forge/refresh"),
    ("POST", "/v1/admin/workspaces/sweep"),
})

REFUSED = [r for r in SWEPT if r not in DECIDED]


def _context(db, tokens, group_map, objects, *, verified: Any = True) -> AppContext:
    tokens = dict(tokens)
    tokens["token-sweeper"] = {
        "email": SWEEPER,
        "email_verified": verified,
        "sub": "sub-sweeper",
    }
    return build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)),
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
