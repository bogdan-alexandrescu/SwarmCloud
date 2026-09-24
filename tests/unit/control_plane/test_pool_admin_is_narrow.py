"""A pool admin reaches ONE admin route. Every other admin route stays admin-only.

WHY THIS EXISTS
---------------
Owner decision, 2026-09-24, replacing the one taken earlier the same day.

The first decision made `swarm-verify` -- the identity the in-VPC verification
gate runs as -- a full platform admin in dev, so race-test could narrow
`runner:mock` through `PUT /v1/admin/limits/runner/mock` instead of a raw
Firestore write. Review then showed what "admin" means here: it is one boolean,
and it opens every /v1/admin route. `PUT /v1/admin/tenants/{id}/limits` can
disable any tenant; `POST /v1/admin/workflows/rollup` rewrites any tenant's
workflow state. The owner's answer: swarm-verify must NOT be a full admin.

So swarm-api has a second, narrower list, `admin_pool_users` (ADMIN_POOL_USERS).
A caller on it may call exactly the routes in an explicit ALLOW-LIST of
(method, route template) -- today one, the runner ceiling -- and nothing else.

WHY AN ALLOW-LIST AND NOT A DENY-LIST. A deny-list ("everything except the
tenant writes") is correct on the day it is written and wrong the day somebody
adds an admin route, because the new route is allowed until someone remembers
to deny it. The failure is silent: nothing breaks, the gate just holds more than
anyone decided. With an allow-list a new admin route is admin-only unless it is
deliberately added. `test_a_new_admin_route_is_admin_only_by_default` below is
the one that tells the two shapes apart; every other test here would pass for
a complete deny-list too.

WHAT IS SWEPT. The routes are collected from every router module in
`swarm_api.routes`, and a route counts as admin-gated when `admin_auth` appears
anywhere in its dependency tree. So a new admin route -- in admin.py or in any
other router -- is in these parametrised cases the moment it exists, with no
list here to update. The sweep has a floor derived from the source (every
`Depends(admin_auth)` in the routers), so an empty sweep cannot pass by silence.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import APIRouter, Depends
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import swarm_api.routes as routes_package
from swarm_api.auth import AuthContext, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import admin_auth, build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import api_settings, seed_pool, seed_tenant

#: The gate's identity in these tests. A service-account address on purpose: it
#: is outside the allowed domain (saga.xyz) and is admitted through
#: ALLOWED_USERS, which is exactly how the real swarm-verify reaches swarm-api.
GATE = "gate@verify-project.iam.gserviceaccount.com"
#: A full admin, through the admin group in conftest's group map.
ROOT = "root@saga.xyz"

#: THE OWNER'S DECISION, 2026-09-24, as a closed set. This is deliberately a
#: literal and not read from the implementation: if it were derived from
#: `POOL_ADMIN_ROUTES`, adding a route to that list would widen what the gate
#: can do and every test here would still pass. Widening it is a new decision;
#: this line is where it gets recorded.
#:
#: Only the PUT. race-test reads the pool back from Firestore, not through the
#: API (scripts/race-test.sh: "OBSERVATIONS still read Firestore directly"), so
#: no admin GET is needed and none is granted.
DECIDED = frozenset({("PUT", "/v1/admin/limits/runner/{runner_profile}")})

#: Plausible values for the path parameters the admin routes take, so a full
#: admin's request reaches the handler rather than a 404 on a made-up name. A
#: parameter not listed gets "x", which is still enough for the gate to decide.
PATH_VALUES = {
    "runner_profile": "mock",
    "provider": "anthropic",
    "tenant_id": "eng",
    "resource_class": "standard",
    "backend": "CLOUD_RUN_JOB",
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


def _all_routes() -> list[APIRoute]:
    found: list[APIRoute] = []
    for module in _router_modules():
        router = getattr(module, "router", None)
        if router is None:
            continue
        found.extend(r for r in router.routes if isinstance(r, APIRoute))
    return found


def _is_admin_gated(route: APIRoute) -> bool:
    return any(call is admin_auth for call in _calls(route.dependant))


def _admin_routes() -> list[tuple[str, str]]:
    """Every (method, route template) whose dependency tree includes admin_auth."""
    return sorted(
        (method, route.path)
        for route in _all_routes()
        if _is_admin_gated(route)
        for method in route.methods
    )


def _fill(path: str) -> str:
    return re.sub(
        r"\{([^}:]+)(?::[^}]*)?\}", lambda m: PATH_VALUES.get(m.group(1), "x"), path
    )


ADMIN_ROUTES = _admin_routes()
NOT_DECIDED = [r for r in ADMIN_ROUTES if r not in DECIDED]
OTHER_WRITES = [r for r in NOT_DECIDED if r[0] != "GET"]
READS = [r for r in NOT_DECIDED if r[0] == "GET"]


def _ids(routes: list[tuple[str, str]]) -> list[str]:
    return [f"{method} {path}" for method, path in routes]


def test_the_sweep_found_every_admin_route_the_source_declares() -> None:
    """A parametrised sweep over nothing passes everything.

    The floor is DERIVED, not a number: every `Depends(admin_auth)` written in
    a router module is one admin-gated route, so the sweep must find exactly as
    many routes as the source declares. If the dependency walk above stopped
    recognising the gate, this is what goes red.
    """
    declared = sum(
        len(re.findall(r"Depends\(\s*admin_auth\s*\)", Path(m.__file__).read_text()))
        for m in _router_modules()
    )
    gated = [r for r in _all_routes() if _is_admin_gated(r)]
    assert declared > 0, "no router declares Depends(admin_auth); the sweep read nothing"
    assert len(gated) == declared, (
        f"the source declares {declared} admin-gated route(s) and the sweep found "
        f"{len(gated)}: {sorted(r.path for r in gated)}"
    )
    # And the parametrised cases below actually have cases in them.
    assert OTHER_WRITES, "no admin write route besides the decided one was collected"
    assert READS, "no admin read route was collected"


def test_every_route_under_v1_admin_is_admin_gated() -> None:
    """The allow-list only means anything if the admin surface is behind the gate.

    A route under /v1/admin that forgot `Depends(admin_auth)` would not be
    "admin-only by default" -- it would be open to every authenticated caller,
    and no pool-admin test would notice, because the sweep only sees gated
    routes.
    """
    ungated = sorted(
        f"{sorted(r.methods)} {r.path}"
        for r in _all_routes()
        if r.path.startswith("/v1/admin/") and not _is_admin_gated(r)
    )
    assert not ungated, f"these /v1/admin routes are not behind admin_auth: {ungated}"


# ---------------------------------------------------------------------------
# Fixtures: the real app, with the gate on ADMIN_POOL_USERS
# ---------------------------------------------------------------------------

@pytest.fixture
def gate_context(db, tokens, group_map, objects):
    tokens = dict(tokens)
    tokens["token-gate"] = {
        "email": GATE,
        "email_verified": True,
        "sub": f"sub-{GATE}",
    }
    return build_context(
        # The gate is admitted as a caller through allowed_users, and given the
        # narrow capability through admin_pool_users -- the same two lists the
        # deployment sets for swarm-verify. It is NOT in admin_users.
        settings=api_settings(allowed_users=(GATE,), admin_pool_users=(GATE,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )


@pytest.fixture
def gate_client(gate_context) -> TestClient:
    return TestClient(create_app(gate_context), raise_server_exceptions=False)


GATE_HEADERS = {"Authorization": "Bearer token-gate"}
ROOT_HEADERS = {"Authorization": "Bearer token-root"}


def _seed_everything(db) -> None:
    """Enough state that a full admin's request reaches each handler."""
    seed_tenant(db, "eng")
    for name in (
        "global",
        "runner:mock",
        "provider:anthropic",
        "provider:anthropic:tenant:eng",
        "backend:CLOUD_RUN_JOB",
        "resource:standard",
    ):
        seed_pool(db, name, hard_limit=20, active=2)


def _send(client: TestClient, method: str, path: str, headers: dict[str, str]):
    if method == "GET":
        return client.request(method, _fill(path), headers=headers)
    return client.request(method, _fill(path), headers=headers, json={})


# ---------------------------------------------------------------------------
# What the gate CAN do
# ---------------------------------------------------------------------------

def test_a_pool_admin_can_narrow_and_restore_a_runner_ceiling(gate_client, db) -> None:
    """What race-test does: narrow runner:mock to one slot, then put it back."""
    seed_pool(db, "runner:mock", hard_limit=20, active=2)

    narrowed = gate_client.put(
        "/v1/admin/limits/runner/mock", headers=GATE_HEADERS, json={"limit": 1}
    )
    assert narrowed.status_code == 200, narrowed.text
    doc = db.docs["pools/runner:mock"]
    assert doc["hard_limit"] == 1
    # Attributed to the gate, from the verified token -- the half of the
    # decision that makes a narrowed pool distinguishable from an operator's.
    assert doc.get("admin_changed_by") == GATE
    assert doc["active"] == 2, "the ceiling route moved active"

    restored = gate_client.put(
        "/v1/admin/limits/runner/mock", headers=GATE_HEADERS, json={"limit": 20}
    )
    assert restored.status_code == 200, restored.text
    assert db.docs["pools/runner:mock"]["hard_limit"] == 20


def test_the_pool_admin_is_not_reported_as_an_admin(gate_client) -> None:
    """`is_admin` drives the web UI's operator screens and two cross-tenant
    fields on /v1/stats and /v1/capacity (service.py). The narrow capability
    must not switch any of that on."""
    me = gate_client.get("/v1/tenants/me", headers=GATE_HEADERS)
    assert me.status_code == 200, me.text
    assert me.json()["principal"]["is_admin"] is False


# ---------------------------------------------------------------------------
# What the gate CANNOT do
# ---------------------------------------------------------------------------

def test_a_pool_admin_cannot_disable_a_tenant(gate_client, db) -> None:
    """The route that decided this: with full admin the gate could set
    `enabled=false` on any tenant, which stops every route for that tenant."""
    seed_tenant(db, "eng")
    before = db.dump()

    response = gate_client.put(
        "/v1/admin/tenants/eng/limits", headers=GATE_HEADERS, json={"enabled": False}
    )
    assert response.status_code == 403, response.text
    assert db.dump() == before, "a refused tenant write still changed Firestore"
    assert db.docs["tenants/eng"]["enabled"] is True


@pytest.mark.parametrize(("method", "path"), OTHER_WRITES, ids=_ids(OTHER_WRITES))
def test_a_pool_admin_is_refused_every_other_admin_write(
    gate_client, db, method: str, path: str
) -> None:
    _seed_everything(db)
    before = db.dump()

    response = _send(gate_client, method, path, GATE_HEADERS)

    assert response.status_code == 403, (
        f"{method} {path} answered {response.status_code} to a pool admin, which "
        f"may call {sorted(DECIDED)} and nothing else: {response.text}"
    )
    assert db.dump() == before, f"a refused {method} {path} still changed Firestore"


@pytest.mark.parametrize(("method", "path"), READS, ids=_ids(READS))
def test_a_pool_admin_is_refused_every_admin_read(
    gate_client, db, method: str, path: str
) -> None:
    """Not granted because not needed: race-test observes through Firestore."""
    _seed_everything(db)

    response = _send(gate_client, method, path, GATE_HEADERS)

    assert response.status_code == 403, (
        f"{method} {path} answered {response.status_code} to a pool admin: "
        f"{response.text}"
    )


def test_a_new_admin_route_is_admin_only_by_default(gate_context, db) -> None:
    """THE test that separates an allow-list from a deny-list.

    A route added tomorrow -- shaped as much like the granted one as possible,
    a PUT under /v1/admin/limits/ -- must refuse the pool admin without anyone
    having remembered to deny it. A complete deny-list passes every other test
    in this file and fails this one; so does any prefix or pattern match on
    "/v1/admin/limits/".
    """
    extra = APIRouter(prefix="/v1/admin")

    @extra.put("/limits/added-later/{name}")
    def added_later(name: str, auth: AuthContext = Depends(admin_auth)) -> dict:
        return {"name": name, "by": auth.email}

    app = create_app(gate_context)
    app.include_router(extra)
    client = TestClient(app, raise_server_exceptions=False)

    refused = client.put("/v1/admin/limits/added-later/mock", headers=GATE_HEADERS)
    assert refused.status_code == 403, (
        f"an admin route nobody allow-listed answered {refused.status_code} to a "
        f"pool admin: {refused.text}"
    )
    # Control: the route itself works, so the 403 above is the gate's answer.
    allowed = client.put("/v1/admin/limits/added-later/mock", headers=ROOT_HEADERS)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["by"] == ROOT


def test_the_allow_list_is_the_decided_one_and_names_real_routes() -> None:
    """Two ways the allow-list can be wrong without any request failing.

    Wider than decided: the gate holds a route nobody decided to give it.
    Stale: an entry names a route that was renamed or removed, so the real
    route became admin-only and race-test is back to a 403 -- the sweep above
    would not notice, because it only ever expects refusals.
    """
    from swarm_api.auth import POOL_ADMIN_ROUTES

    assert set(POOL_ADMIN_ROUTES) == set(DECIDED), (
        f"the pool-admin allow-list is {sorted(POOL_ADMIN_ROUTES)}; the owner "
        f"decided {sorted(DECIDED)} on 2026-09-24. Widening it is a new decision."
    )
    missing = sorted(set(POOL_ADMIN_ROUTES) - set(ADMIN_ROUTES))
    assert not missing, (
        f"the allow-list names {missing}, which no router serves behind admin_auth"
    )


# ---------------------------------------------------------------------------
# A full admin is unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES, ids=_ids(ADMIN_ROUTES))
def test_a_full_admin_still_passes_the_gate_everywhere(
    gate_client, db, method: str, path: str
) -> None:
    """Every admin-gated route, including the decided one, still admits a full
    admin. 403 and 503 are the only answers the gate gives; anything else
    (200, or a 422 on the empty body sent here) was decided past it."""
    _seed_everything(db)

    response = _send(gate_client, method, path, ROOT_HEADERS)

    assert response.status_code not in (401, 403, 503), (
        f"{method} {path} refused a full admin with {response.status_code}: "
        f"{response.text}"
    )


def test_a_full_admin_can_still_disable_a_tenant(gate_client, db) -> None:
    """The route the gate lost is not lost to operators."""
    seed_tenant(db, "eng")

    response = gate_client.put(
        "/v1/admin/tenants/eng/limits", headers=ROOT_HEADERS, json={"enabled": False}
    )
    assert response.status_code == 200, response.text
    assert db.docs["tenants/eng"]["enabled"] is False
