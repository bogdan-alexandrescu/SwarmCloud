"""A continuation-scoped account reaches the routes in ONE allow-list, and
reads only what it submitted.

WHY THIS EXISTS
---------------
Contract request 30, decision 2 (2026-09-29). A service account a tenant lists
in terraform resolves to that tenant -- `eng`'s GSA, secrets, prefix and
namespace -- but its RIGHTS within the tenant are not a human member's: it may
submit a `continues_task` workflow and read what IT submitted, and nothing
else.

The first draft of that decision named routes to gate, built by reading
`tasks.py` and `workflows.py` and stopping. Re-review found eight account
routes, the credential route, `/v1/attempts`, `/v1/outcomes` and three
checkpoint-content routes that list never looked at. So the check is
DEFAULT-DENY, in `current_auth` itself, against `auth.CONTINUATION_ROUTES`; and
this file does not trust anyone to have thought of a route either. It walks
every route in every router the app includes, the same shape as
test_pool_admin_is_narrow.py.

TWO ASSERTIONS PER ROUTE.
  * Reachability: every authenticated route NOT in the decided set 403s the
    listed caller, naming the scope; every route IN it does not.
  * Ownership: every decided route that names a `{task_id}` or `{workflow_id}`
    is driven at one that a DIFFERENT `eng` member submitted, and must answer
    exactly what a missing task answers. That catches a `submitted_by`
    thread-through missed in any service layer, on the day a route is added.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from typing import Any, Iterator

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import swarm_api.routes as routes_package
from swarm_common.identity import TenantMember

from swarm_api.auth import CONTINUATION_ROUTES, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import AppContext, build_context, current_auth
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ENG_GROUP, PROJECT, api_settings, auth_header, seed_task, seed_tenant

FIXER = f"swarm-ci-fix@{PROJECT}.iam.gserviceaccount.com"
FIXER_UID = "104857600000000000001"
FIXER_HEADERS = {"Authorization": "Bearer token-fixer"}
ALICE = auth_header("alice")

#: THE OWNER'S DECISION, 2026-09-29, as a closed set. A literal, not read from
#: the implementation: if it were derived from CONTINUATION_ROUTES, widening
#: that set would widen what a stolen fixer token reaches and every test here
#: would still pass. Widening it is a new decision; this is where it is written.
DECIDED = frozenset({
    ("POST", "/v1/workflows"),
    ("GET", "/v1/tasks"),
    ("GET", "/v1/tasks/{task_id}"),
    ("GET", "/v1/tasks/{task_id}/events"),
    ("GET", "/v1/tasks/{task_id}/attempts"),
    ("GET", "/v1/tasks/{task_id}/artifacts"),
    ("GET", "/v1/tasks/{task_id}/artifacts/content"),
    ("GET", "/v1/tasks/{task_id}/artifacts/raw"),
    ("GET", "/v1/tasks/{task_id}/checkpoints"),
    ("GET", "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/files"),
    ("GET", "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/files/{path:path}"),
    ("GET", "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/content"),
    ("GET", "/v1/tasks/{task_id}/logs"),
    ("GET", "/v1/tasks/{task_id}/transcript"),
    ("GET", "/v1/tasks/{task_id}/answer"),
    ("GET", "/v1/tasks/{task_id}/input"),
    ("GET", "/v1/workflows"),
    ("GET", "/v1/workflows/{workflow_id}"),
    ("GET", "/v1/resource-classes"),
    ("GET", "/v1/runtimes"),
})

#: Named by re-review as missed by the first draft; each must be in the refused
#: sweep, so a router rename cannot quietly take one out of it.
NAMED_REFUSED = [
    ("GET", "/v1/accounts"),
    ("POST", "/v1/accounts"),
    ("POST", "/v1/accounts/authorize"),
    ("POST", "/v1/accounts/exchange"),
    ("POST", "/v1/accounts/{account_id}/refresh"),
    ("PUT", "/v1/accounts/{account_id}/lending"),
    ("PUT", "/v1/accounts/{account_id}/state"),
    ("DELETE", "/v1/accounts/{account_id}"),
    ("POST", "/v1/tenants/me/credentials"),
    ("GET", "/v1/tenants/me"),
    ("GET", "/v1/attempts"),
    ("GET", "/v1/outcomes"),
    ("GET", "/v1/leases"),
    ("GET", "/v1/stats"),
    ("GET", "/v1/capacity"),
    ("GET", "/v1/providers"),
    ("POST", "/v1/tasks"),
    ("POST", "/v1/tasks/batch"),
    ("POST", "/v1/tasks/{task_id}/cancel"),
    ("POST", "/v1/workflows/{workflow_id}/cancel"),
]

PATH_VALUES = {
    "runner_profile": "mock",
    "provider": "anthropic",
    "tenant_id": "eng",
    "resource_class": "standard",
    "backend": "CLOUD_RUN_JOB",
    "checkpoint_id": "ckpt-00001",
    "path": "notes.txt",
}

#: Required query parameters of decided routes, so an ownership request reaches
#: the handler instead of a 422 on a missing parameter.
QUERY = {
    "/v1/tasks/{task_id}/artifacts/content": {"name": "stdout.log"},
    "/v1/tasks/{task_id}/artifacts/raw": {"name": "stdout.log"},
}


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

def _router_modules() -> list[Any]:
    names = sorted(m.name for m in pkgutil.iter_modules(routes_package.__path__))
    return [importlib.import_module(f"swarm_api.routes.{name}") for name in names]


def _calls(dependant: Any) -> Iterator[Any]:
    yield dependant.call
    for sub in dependant.dependencies:
        yield from _calls(sub)


def _iter_api_routes(routes: Any) -> Iterator[APIRoute]:
    """Every `APIRoute` under `routes`, however deeply FastAPI nests it.

    A newer FastAPI resolves `include_router` lazily: `app.routes` holds a
    `_IncludedRouter` wrapper around the ROUTER OBJECT passed to
    `include_router` (its `original_router` attribute), not a flat copy of
    that router's `APIRoute`s. Walking `app.routes` and filtering
    `isinstance(route, APIRoute)` therefore found NOTHING -- confirmed by
    reproducing the wrapping with a minimal FastAPI app and inspecting
    `type(r).__name__` for each entry in `.routes`. Recursing through
    `original_router.routes` (or a plain `.routes`, for a `Mount` or whatever
    shape a future FastAPI version nests routers in) reaches the SAME
    `APIRoute` objects a live request resolves to `request.scope["route"]` --
    also verified directly, with a `TestClient` request whose handler read
    `request.scope["route"]` back and compared it by identity.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif (router := getattr(route, "original_router", None)) is not None:
            yield from _iter_api_routes(router.routes)
        elif (sub_routes := getattr(route, "routes", None)) is not None:
            yield from _iter_api_routes(sub_routes)


def _authenticated_routes() -> list[tuple[str, str]]:
    """Every (method, template) whose dependency tree authenticates the caller."""
    found = []
    for module in _router_modules():
        router = getattr(module, "router", None)
        if router is None:
            continue
        for route in router.routes:
            if isinstance(route, APIRoute) and any(
                call is current_auth for call in _calls(route.dependant)
            ):
                found.extend((method, route.path) for method in route.methods)
    return sorted(set(found))


def _fill(path: str, **values: str) -> str:
    merged = {**PATH_VALUES, **values}
    return re.sub(
        r"\{([^}:]+)(?::[^}]*)?\}", lambda m: merged.get(m.group(1), "x"), path
    )


SWEPT = _authenticated_routes()
REFUSED = [r for r in SWEPT if r not in DECIDED]
ALLOWED = [r for r in SWEPT if r in DECIDED]
OWNED = [r for r in ALLOWED if "{task_id}" in r[1] or "{workflow_id}" in r[1]]


def _ids(routes: list[tuple[str, str]]) -> list[str]:
    return [f"{method} {path}" for method, path in routes]


def _send(client: TestClient, method: str, path: str, headers: dict[str, str], **values: str):
    url = _fill(path, **values)
    params = QUERY.get(path)
    if method in ("GET", "DELETE"):
        return client.request(method, url, headers=headers, params=params)
    return client.request(method, url, headers=headers, params=params, json={})


# ---------------------------------------------------------------------------
# Fixtures: the real app, with swarm-ci-fix listed under eng
# ---------------------------------------------------------------------------

def _context(db, tokens, group_map, objects) -> AppContext:
    tokens = dict(tokens)
    tokens["token-fixer"] = {"email": FIXER, "email_verified": True, "sub": FIXER_UID}
    return build_context(
        settings=api_settings(
            tenant_service_accounts=(
                TenantMember(email=FIXER, kind="group", principal=ENG_GROUP, uid=FIXER_UID),
            )
        ),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )


@pytest.fixture
def listed_context(db, tokens, group_map, objects) -> AppContext:
    seed_tenant(db, "eng")
    return _context(db, tokens, group_map, objects)


@pytest.fixture
def fixer_client(listed_context) -> TestClient:
    return TestClient(create_app(listed_context), raise_server_exceptions=False)


def _scope_refusal(response) -> bool:
    return response.status_code == 403 and "continuation-scoped" in response.text


# ---------------------------------------------------------------------------
# The set itself
# ---------------------------------------------------------------------------

def test_the_allow_list_is_the_decided_one():
    assert set(CONTINUATION_ROUTES) == set(DECIDED)


def test_the_sweep_is_not_empty_and_covers_the_named_routes():
    assert len(SWEPT) > len(DECIDED), "the sweep found fewer routes than the allow-list"
    missing = [r for r in NAMED_REFUSED if r not in REFUSED]
    assert not missing, f"named routes absent from the refused sweep: {missing}"
    assert set(ALLOWED) == set(DECIDED), (
        f"allow-listed routes no router declares: {sorted(set(DECIDED) - set(ALLOWED))}"
    )
    assert OWNED, "no task- or workflow-scoped route was collected"


def test_every_allow_listed_template_is_a_published_url(listed_context):
    """`current_auth` compares the DECLARING router's template (see the comment
    on `auth.POOL_ADMIN_ROUTES`); main.py includes every router bare, which is
    the only reason these read like URLs. A prefix added later fails here.

    Compared against `route.path` -- the exact string `current_auth` reads off
    `request.scope["route"].path` at request time -- NOT `openapi()["paths"]`.
    FastAPI's OpenAPI document renders a path-converter route
    (`{path:path}`, the checkpoint-files route) as `{path}`: it strips the
    convertor for the spec. `route.path` keeps it. Comparing against the
    OpenAPI form here would fail this test on the one convertor route in the
    app even though `current_auth`'s own check (`deps.py`) and
    CONTINUATION_ROUTES already agree with each other -- and, worse, "fixing"
    CONTINUATION_ROUTES to match the stripped OpenAPI form would make
    `current_auth` refuse that route to a continuation-scoped caller, since
    its `request.scope["route"].path` still carries the convertor. Verified
    directly: `starlette.routing.Route("...{path:path}").path ==
    "...{path:path}"` while `.path_format` (what FastAPI's OpenAPI generator
    uses) strips it to `"...{path}"`.

    Walked with `_iter_api_routes` rather than filtering `app.routes` directly
    for `isinstance(route, APIRoute)` -- that filter alone found nothing on
    this FastAPI version (`assert ... in set()`, "the published set was not
    built"): `include_router` is resolved lazily into an internal wrapper
    around the included router, so the real `APIRoute`s are nested under it,
    not flat in `app.routes`. See `_iter_api_routes`'s docstring.
    """
    published = {
        (method, route.path)
        for route in _iter_api_routes(create_app(listed_context).routes)
        for method in route.methods
    }
    assert ("GET", "/v1/tasks/{task_id}") in published, "the published set was not built"
    assert not sorted(set(CONTINUATION_ROUTES) - published)


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", REFUSED, ids=_ids(REFUSED))
def test_a_route_outside_the_allow_list_is_refused_to_the_listed_account(
    fixer_client, method, path
):
    response = _send(fixer_client, method, path, FIXER_HEADERS)
    assert _scope_refusal(response), (
        f"{method} {path} answered {response.status_code} {response.text[:200]}"
    )


@pytest.mark.parametrize("method,path", ALLOWED, ids=_ids(ALLOWED))
def test_a_route_in_the_allow_list_is_not_refused_for_scope(fixer_client, method, path):
    response = _send(fixer_client, method, path, FIXER_HEADERS)
    assert not _scope_refusal(response), response.text[:200]


@pytest.mark.parametrize("method,path", NAMED_REFUSED, ids=_ids(NAMED_REFUSED))
def test_an_ordinary_member_is_not_refused_for_scope(fixer_client, method, path):
    response = _send(fixer_client, method, path, ALICE)
    assert not _scope_refusal(response), response.text[:200]


@pytest.mark.parametrize("path", ["/v1/resource-classes", "/v1/runtimes"])
def test_the_static_catalogue_is_served_to_the_listed_account(fixer_client, path):
    assert fixer_client.get(path, headers=FIXER_HEADERS).status_code == 200


# ---------------------------------------------------------------------------
# POST /v1/workflows: the route is open, a non-continuation request is not
# ---------------------------------------------------------------------------

WORKFLOW = {
    "steps": [
        {"step_id": "fix", "runner_profile": "mock", "input": {"prompt": "fix"}, "depends_on": []}
    ]
}


def test_a_workflow_without_continues_task_is_refused_to_the_listed_account(fixer_client):
    refused = fixer_client.post("/v1/workflows", headers=FIXER_HEADERS, json=WORKFLOW)
    assert refused.status_code == 403, refused.text
    assert "continues_task" in refused.text
    accepted = fixer_client.post("/v1/workflows", headers=ALICE, json=WORKFLOW)
    assert accepted.status_code == 201, accepted.text


# ---------------------------------------------------------------------------
# continues_task itself, for the listed account (could not be written in
# #343: `resolve_continuation` did not yet pass `Store.get_task` its now-
# required `submitted_by`, so any of these raised TypeError before route
# refusal ever ran).
# ---------------------------------------------------------------------------

CONTINUATION_REPO = "https://github.com/saga-xyz/example"


def _submit_direct_pr_task(fixer_client, headers: dict[str, str], **extra: Any) -> str:
    body = {
        "runner_profile": "mock",
        "strategy": "direct-pr",
        "repository_url": CONTINUATION_REPO,
        **extra,
    }
    response = fixer_client.post("/v1/tasks", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()["task"]["id"]


def _fix_workflow(continues: str) -> dict:
    return {
        "steps": [{"step_id": "fix", "runner_profile": "mock", "input": {"prompt": "fix"}}],
        "strategy": "direct-pr",
        "continues_task": continues,
    }


def test_the_listed_account_can_continue_a_task_another_member_submitted(fixer_client):
    """The whole point of the scope: ci-fix continues ALICE's red pull request,
    never one of its own -- so this must not be narrowed by `submitted_by`."""
    original = _submit_direct_pr_task(fixer_client, ALICE)
    response = fixer_client.post(
        "/v1/workflows", headers=FIXER_HEADERS, json=_fix_workflow(original)
    )
    assert response.status_code == 201, response.text
    assert response.json()["dispatch"]["continues_task"] == original


def test_the_listed_account_cannot_submit_a_merge_only_continuation(fixer_client):
    """A merge is a wider power than the push request 30 accepted for the CI
    fixer: a workflow of one `merge` step continuing a pull request is a
    member's only (contract request 47; the issue run's merge)."""
    original = _submit_direct_pr_task(fixer_client, ALICE)
    merge_only = {
        "steps": [{"step_id": "merge", "runner_profile": "merge"}],
        "strategy": "direct-pr",
        "continues_task": original,
    }
    refused = fixer_client.post("/v1/workflows", headers=FIXER_HEADERS, json=merge_only)
    assert refused.status_code == 422, refused.text
    assert "does not merge" in refused.json()["message"]
    accepted = fixer_client.post("/v1/workflows", headers=ALICE, json=merge_only)
    assert accepted.status_code == 201, accepted.text


def _submit_integrate_workflow(fixer_client, headers: dict[str, str]) -> dict[str, str]:
    response = fixer_client.post(
        "/v1/workflows",
        headers=headers,
        json={
            "steps": [
                {"step_id": "build", "runner_profile": "mock"},
                {"step_id": "publish", "runner_profile": "mock", "depends_on": ["build"]},
            ],
            "strategy": "integrate",
            "repository_url": CONTINUATION_REPO,
        },
    )
    assert response.status_code == 201, response.text
    return {s["step_id"]: s["task_id"] for s in response.json()["workflow"]["steps"]}


def test_the_listed_account_can_continue_an_integrate_workflows_integrator(fixer_client, db):
    """Owner decision 2026-10-06: every SwarmCloud lane pull request is opened
    by an `integrate` workflow's integrator, so a fixer that reached
    `direct-pr` tasks only fixed none of them (PR #740's refusal). The fix
    pushes to the integrator's own `swarm/<task>` branch -- the pull
    request's -- because the continuation's root IS the integrator."""
    integrator = _submit_integrate_workflow(fixer_client, ALICE)["publish"]
    response = fixer_client.post(
        "/v1/workflows", headers=FIXER_HEADERS, json=_fix_workflow(integrator)
    )
    assert response.status_code == 201, response.text
    assert response.json()["dispatch"]["continues_task"] == integrator
    fix_task_id = response.json()["workflow"]["steps"][0]["task_id"]
    assert db.docs[f"tasks/{fix_task_id}"]["metadata"]["dispatch"]["continues"] == integrator
    assert db.docs[f"tasks/{fix_task_id}"]["submitted_by"] == FIXER


def test_a_continuation_of_an_integrator_continues_the_integrators_branch(fixer_client, db):
    """A member's fix round on the integrator, continued by the fixer, still
    pushes to the integrator's branch: a continuation of a continuation
    resolves to its root, exactly as it does for a `direct-pr` task."""
    integrator = _submit_integrate_workflow(fixer_client, ALICE)["publish"]
    members = fixer_client.post("/v1/workflows", headers=ALICE, json=_fix_workflow(integrator))
    assert members.status_code == 201, members.text
    members_fix = members.json()["workflow"]["steps"][0]["task_id"]
    through = fixer_client.post(
        "/v1/workflows", headers=FIXER_HEADERS, json=_fix_workflow(members_fix)
    )
    assert through.status_code == 201, through.text
    fix_task_id = through.json()["workflow"]["steps"][0]["task_id"]
    assert db.docs[f"tasks/{fix_task_id}"]["metadata"]["dispatch"]["continues"] == integrator


def test_the_listed_account_cannot_continue_an_integrate_workflows_other_step(fixer_client):
    """A contributor's branch is merged by the integrator and has no pull
    request of its own: a fix pushed there reaches nobody. Refused with the
    code a member's attempt gets, by the integrator's recorded role -- never
    by anything the caller sends."""
    contributor = _submit_integrate_workflow(fixer_client, ALICE)["build"]
    for headers in (FIXER_HEADERS, ALICE):
        refused = fixer_client.post(
            "/v1/workflows", headers=headers, json=_fix_workflow(contributor)
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["code"] == "invalid_dispatch"
        assert "opens no pull request of its own" in refused.json()["message"]


def test_the_listed_account_cannot_continue_another_tenants_integrator(fixer_client):
    """The integrator reach is the caller's tenant only: `bob` is `research`,
    and his integrator reads exactly like a task that does not exist."""
    theirs = _submit_integrate_workflow(fixer_client, auth_header("bob"))["publish"]
    response = fixer_client.post(
        "/v1/workflows", headers=FIXER_HEADERS, json=_fix_workflow(theirs)
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_dispatch"
    assert response.json()["message"] == f"continues_task {theirs!r} is not a task in your tenant."


def test_the_listed_accounts_integrator_fix_is_capped_and_stamped_for_the_post_back(
    fixer_client, db
):
    """#728's per-pull-request cap and pending post-back stamp hold for an
    integrator continuation as for a `direct-pr` one: both key on the root."""
    integrator = _submit_integrate_workflow(fixer_client, ALICE)["publish"]

    def red_run(attempt: int) -> dict:
        return {
            **_fix_workflow(integrator),
            "metadata": {"ci_fix": {
                "pull_request": 740, "run_id": 1, "head_sha": "a" * 40,
                "attempt": attempt, "max_attempts": 2,
            }},
        }

    for attempt in (1, 2):
        response = fixer_client.post("/v1/workflows", headers=FIXER_HEADERS, json=red_run(attempt))
        assert response.status_code == 201, response.text
        fix_task_id = response.json()["workflow"]["steps"][0]["task_id"]
        assert db.docs[f"tasks/{fix_task_id}"]["metadata"]["ci_fix"]["postback"] == "pending"
    third = fixer_client.post("/v1/workflows", headers=FIXER_HEADERS, json=red_run(3))
    assert third.status_code == 422, third.text
    assert third.json()["code"] == "invalid_dispatch"
    assert "the cap is 2" in third.json()["message"]


def test_the_continued_tasks_submitted_by_is_the_listed_account_not_the_original(
    fixer_client, db
):
    """The fix task's `submitted_by` is the caller (contract request 30,
    decision 2): copying it from the continued task would lose the listed
    account's own sight of what it submitted, on every later read route that
    filters by `submitted_by`."""
    original = _submit_direct_pr_task(fixer_client, ALICE)
    response = fixer_client.post(
        "/v1/workflows", headers=FIXER_HEADERS, json=_fix_workflow(original)
    )
    assert response.status_code == 201, response.text
    fix_task_id = response.json()["workflow"]["steps"][0]["task_id"]
    assert db.docs[f"tasks/{fix_task_id}"]["submitted_by"] == FIXER

    # And the listed account can now read its own continuation back, unlike
    # alice's original task (test_another_members_task_reads_as_missing...).
    mine = fixer_client.get(f"/v1/tasks/{fix_task_id}", headers=FIXER_HEADERS)
    assert mine.status_code == 200, mine.text


def test_a_task_from_another_tenant_still_cannot_be_continued_by_the_listed_account(
    fixer_client,
):
    """The tenant check in `resolve_continuation` is unaffected by the
    unfiltered `submitted_by=None`: only the CALLER'S TENANT's tasks are
    reachable at all."""
    bob = auth_header("bob")
    theirs = _submit_direct_pr_task(fixer_client, bob)
    response = fixer_client.post(
        "/v1/workflows", headers=FIXER_HEADERS, json=_fix_workflow(theirs)
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_dispatch"
    assert response.json()["message"] == f"continues_task {theirs!r} is not a task in your tenant."


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------

@pytest.fixture
def alices(fixer_client) -> dict[str, str]:
    """One task and one workflow a DIFFERENT eng member submitted."""
    task = fixer_client.post(
        "/v1/tasks", headers=ALICE, json={"runner_profile": "mock", "input": {"prompt": "a"}}
    )
    assert task.status_code == 201, task.text
    workflow = fixer_client.post("/v1/workflows", headers=ALICE, json=WORKFLOW)
    assert workflow.status_code == 201, workflow.text
    return {
        "task_id": task.json()["task"]["id"],
        "workflow_id": workflow.json()["workflow"]["workflow_id"],
    }


@pytest.mark.parametrize("method,path", OWNED, ids=_ids(OWNED))
def test_another_members_task_reads_as_missing_to_the_listed_account(
    fixer_client, alices, method, path
):
    ids = alices
    kind, wanted = (
        ("workflow", ids["workflow_id"]) if "{workflow_id}" in path else ("task", ids["task_id"])
    )
    missing = f"{kind} {wanted!r} not found"
    as_fixer = _send(fixer_client, method, path, FIXER_HEADERS, **ids)
    assert as_fixer.status_code == 404, as_fixer.text[:300]
    assert missing in as_fixer.text, as_fixer.text[:300]
    # And the same request by its submitter finds it: the 404 above is the
    # filter, not a route that 404s for everyone.
    as_alice = _send(fixer_client, method, path, ALICE, **ids)
    assert missing not in as_alice.text, as_alice.text[:300]


def _seed_own(db, task_id: str) -> None:
    doc = seed_task(db, task_id=task_id, tenant_id="eng")
    doc["submitted_by"] = FIXER


def test_the_listed_account_reads_its_own_task(fixer_client, db, alices):
    _seed_own(db, "task_mine")
    response = fixer_client.get("/v1/tasks/task_mine", headers=FIXER_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["task"]["submitted_by"] == FIXER
    files = fixer_client.get(
        "/v1/tasks/task_mine/checkpoints/ckpt-00001/files", headers=FIXER_HEADERS
    )
    assert "task 'task_mine' not found" not in files.text


def test_the_task_list_holds_only_what_the_listed_account_submitted(fixer_client, db, alices):
    _seed_own(db, "task_mine")
    mine = fixer_client.get("/v1/tasks", headers=FIXER_HEADERS)
    assert mine.status_code == 200, mine.text
    assert [t["id"] for t in mine.json()["tasks"]] == ["task_mine"]
    everyone = fixer_client.get("/v1/tasks", headers=ALICE)
    assert {"task_mine", alices["task_id"]} <= {t["id"] for t in everyone.json()["tasks"]}


def test_the_workflow_list_holds_only_what_the_listed_account_submitted(fixer_client, alices):
    mine = fixer_client.get("/v1/workflows", headers=FIXER_HEADERS)
    assert mine.status_code == 200, mine.text
    assert mine.json()["workflows"] == []
    everyone = fixer_client.get("/v1/workflows", headers=ALICE)
    assert alices["workflow_id"] in {w["workflow_id"] for w in everyone.json()["workflows"]}
