"""Every platform-level route refuses a workspace admin, a member and a viewer.

WHY THIS EXISTS
---------------
BGD, 2026-10-23 (#2157): Admin holds every platform-level secret and setting,
is shown only to admins, and only admins may change anything there -- through
the console, the API, MCP and the plugin. The console hides the section; the
API is what actually holds the line, because MCP and the plugin call it with
the caller's own token. So this file is about the API, with one look at the
MCP client to show it adds no way round it.

`test_pool_admin_is_narrow.py` already holds the STRUCTURE: every route under
/v1/admin declares `admin_auth`. That is a statement about the dependency tree,
and it is true of a route whose gate lets the wrong person through just as
well. This file holds the BEHAVIOUR: a request built here, from each of the
people who are NOT platform admins but are the most likely to be mistaken for
one, is refused with 403 on every route of the platform surface, and the
refusal writes nothing.

THE PEOPLE
----------
* `wsadmin` -- the admin of the group workspace `eng`, by a signed grant. The
  most important case: Admin › Workspaces you admin is drawn for them, so the
  console calls Admin addresses on their behalf, and a gate that read "admin"
  off the workspace role would open every platform page to them.
* `alice` -- a member of `eng`, no grant: the default role.
* `viewer` -- in `eng`, granted `viewer`.
* `carol` -- in no group: the admin of nothing but her own personal workspace.

Each is checked first through `GET /v1/tenants/me`, so a refusal below cannot
be explained by the person being someone other than the test says.

WHAT IS SWEPT
-------------
The platform surface is the union of:

* every operation the app PUBLISHES (its OpenAPI document) whose path is
  /v1/admin or under it, or that carries the `admin` tag -- so a route added
  under the prefix with no gate at all is in the sweep the moment it exists;
* every route of a `swarm_api.routes` router whose dependency tree contains
  `admin_auth` -- which reaches the admin-gated routes outside the prefix
  (`/metrics`, `/v1/runs/owner-signatures`, the two QA-target admin actions)
  and any route that is left out of the schema.

Plus `INLINE_PLATFORM_WRITES`: the platform-level fields of an otherwise
workspace-level route, which are gated inside the handler (`_platform_admin`
in routes/repositories.py) and so cannot be found by walking dependencies.

The sweep has a floor (`test_the_sweep_is_not_empty`) and a control
(`test_the_sweep_catches_an_unguarded_admin_route`): the same two functions,
pointed at an app with one deliberately unguarded /v1/admin route, must name
it. A sweep that saw nothing, or a check that refused nothing, would pass every
other case here by silence.

MUTATION: declare `PUT /v1/admin/settings` with `current_auth` instead of
`admin_auth`, and every person's case goes red on it (422: the body reached
validation); make `_platform_admin` in routes/repositories.py skip its
`require_admin`, and wsadmin's goes red on all five inline writes.
"""

from __future__ import annotations

import copy
import importlib
import pkgutil
import re
import secrets
from pathlib import Path
from typing import Any, Iterable, Iterator

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import swarm_api.routes as routes_package
from swarm_api import roles
from swarm_api.auth import TENANT_HEADER, AuthContext, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import AppContext, admin_auth, build_context, current_auth, get_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, NoForgeTokens, api_settings, seed_pool, seed_tenant
from .fakes import FakeFirestore

REPO = Path(__file__).resolve().parents[3]
MCP_SOURCE = REPO / "apps" / "swarm-mcp" / "swarm_mcp"

ADMIN_PREFIX = "/v1/admin"
ADMIN_TAG = "admin"

GROUPS: dict[str, tuple[str, ...]] = {
    "root@example.com": (ADMIN_GROUP, ENG_GROUP),
    "wsadmin@example.com": (ENG_GROUP,),
    "alice@example.com": (ENG_GROUP,),
    "viewer@example.com": (ENG_GROUP,),
    "carol@example.com": (),
}

#: (person, the workspace their requests select, the role they hold there).
#: carol selects nothing: her default is her own personal workspace.
NON_ADMINS: list[tuple[str, str | None, str]] = [
    ("wsadmin", "eng", roles.ADMIN),
    ("alice", "eng", roles.MEMBER),
    ("viewer", "eng", roles.VIEWER),
    ("carol", None, roles.ADMIN),
]

#: Plausible path values, so a refusal is the gate's and not a 404 on a name.
PATH_VALUES = {
    "runner_profile": "mock",
    "provider": "anthropic",
    "tenant_id": "eng",
    "workspace_id": "eng",
    "resource_class": "standard",
    "backend": "CLOUD_RUN_JOB",
    "email": "alice@example.com",
}

#: The platform-level fields of workspace routes, gated in the handler. Each
#: is (method, path, body); the path's repo id need not exist, because the
#: gate runs before the record is read.
INLINE_PLATFORM_WRITES: list[tuple[str, str, dict[str, Any]]] = [
    ("POST", "/v1/repositories", {"repository": "example-org/tool", "platform": True}),
    ("POST", "/v1/repositories", {"repository": "example-org/tool", "merge_policy": "off"}),
    ("PATCH", "/v1/repositories/repo_0123456789abcdef", {"platform": True}),
    ("PATCH", "/v1/repositories/repo_0123456789abcdef", {"merge_policy": "inherit"}),
    ("PATCH", "/v1/repositories/repo_0123456789abcdef", {"readable_by_tasks": True}),
]


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

def _routers() -> list[APIRouter]:
    names = sorted(m.name for m in pkgutil.iter_modules(routes_package.__path__))
    found = []
    for name in names:
        router = getattr(importlib.import_module(f"swarm_api.routes.{name}"), "router", None)
        if router is not None:
            found.append(router)
    return found


def _calls(dependant: Any) -> Iterator[Any]:
    yield dependant.call
    for sub in dependant.dependencies:
        yield from _calls(sub)


def platform_surface(app: FastAPI, routers: Iterable[APIRouter]) -> list[tuple[str, str]]:
    """(METHOD, path template) of every platform-level route: published under
    /v1/admin or tagged `admin`, or declaring `admin_auth` anywhere."""
    found: set[tuple[str, str]] = set()
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            under = path == ADMIN_PREFIX or path.startswith(ADMIN_PREFIX + "/")
            if under or ADMIN_TAG in (operation.get("tags") or []):
                found.add((method.upper(), path))
    for router in routers:
        for route in router.routes:
            if isinstance(route, APIRoute) and any(c is admin_auth for c in _calls(route.dependant)):
                found.update((method, route.path) for method in route.methods)
    return sorted(found)


def _fill(path: str) -> str:
    return re.sub(r"\{([^}:]+)(?::[^}]*)?\}", lambda m: PATH_VALUES.get(m.group(1), "x"), path)


def _headers(person: str, tenant: str | None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer token-{person}"}
    if tenant is not None:
        headers[TENANT_HEADER] = tenant
    return headers


def not_refused(client: TestClient, requests: Iterable[tuple[str, str, Any]],
                headers: dict[str, str]) -> list[str]:
    """Every request that did NOT answer 403, as "METHOD path -> status"."""
    missed = []
    for method, path, body in requests:
        url = _fill(path)
        if method == "GET":
            response = client.request(method, url, headers=headers)
        else:
            response = client.request(method, url, headers=headers, json=body)
        if response.status_code != 403:
            missed.append(f"{method} {path} -> {response.status_code}")
    return missed


def _as_requests(surface: list[tuple[str, str]]) -> list[tuple[str, str, Any]]:
    return [(method, path, {}) for method, path in surface]


# ---------------------------------------------------------------------------
# Fixtures: the real app, eng with one workspace admin and one viewer
# ---------------------------------------------------------------------------

def _context(db: FakeFirestore) -> AppContext:
    tokens = {
        f"token-{email.split('@')[0]}": {
            "email": email, "email_verified": True, "sub": f"sub-{email}", "hd": "example.com",
        }
        for email in GROUPS
    }
    return build_context(
        # A settings key so the role grants below are signed and honoured; a
        # grant key so no admin route answers 503 for a missing one before its
        # gate could. Both built at runtime: nothing credential-shaped is
        # committed. The burst is raised because one person walks every route.
        settings=api_settings(settings_key=secrets.token_hex(32),
                              workspace_grant_key=secrets.token_hex(32),
                              rate_limit_burst=10_000),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(GROUPS),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=None,
        forge_tokens=NoForgeTokens(),
    )


def _grant(ctx: AppContext, tenant_id: str, email: str, role: str) -> None:
    store = ctx.roles
    assert store is not None, "the context has no roles store: no grant could be written"
    store.write(tenant_id=tenant_id, subject=email, role=role, by="root@example.com",
                via="platform_admin", reason="", previous=store.newest_valid(tenant_id, email, fresh=True),
                action=roles.AUDIT_GRANTED, detail={})


@pytest.fixture
def world():
    db = FakeFirestore()
    seed_tenant(db, "eng")
    for name in ("global", "runner:mock", "provider:anthropic", "provider:anthropic:tenant:eng",
                 "backend:CLOUD_RUN_JOB", "resource:standard"):
        seed_pool(db, name, hard_limit=20, active=2)
    ctx = _context(db)
    _grant(ctx, "eng", "wsadmin@example.com", roles.ADMIN)
    _grant(ctx, "eng", "viewer@example.com", roles.VIEWER)
    app = create_app(ctx)
    return db, ctx, app, TestClient(app, raise_server_exceptions=False)


SURFACE = platform_surface(create_app(_context(FakeFirestore())), _routers())


# ---------------------------------------------------------------------------
# The floor and the control
# ---------------------------------------------------------------------------

def test_the_sweep_is_not_empty() -> None:
    """A walk over nothing refuses nothing and passes. The floor is derived
    from the source: every `Depends(admin_auth)` a router declares is one
    route the sweep must hold, and the admin router's own writes are in it."""
    declared = sum(
        len(re.findall(r"Depends\(\s*admin_auth\s*\)", Path(m.__file__).read_text()))
        for m in (importlib.import_module(f"swarm_api.routes.{i.name}")
                  for i in pkgutil.iter_modules(routes_package.__path__))
    )
    assert declared > 0
    assert len(SURFACE) >= declared, (len(SURFACE), declared)
    for expected in [("PUT", "/v1/admin/settings"), ("PUT", "/v1/admin/limits/global"),
                     ("GET", "/v1/admin/platform-secrets"), ("PUT", "/v1/admin/runner-sizes"),
                     ("PUT", "/v1/admin/admins/{email}"), ("POST", "/v1/admin/workspaces")]:
        assert expected in SURFACE, f"{expected} is missing from the sweep"
    assert [r for r in SURFACE if r[0] != "GET"], "no platform write was swept"


def _unguarded_fixture_router(db: FakeFirestore) -> APIRouter:
    """One guarded and one deliberately UNGUARDED route under /v1/admin. The
    unguarded one writes, so a pass through it is visible in the database."""
    router = APIRouter(prefix=ADMIN_PREFIX, tags=[ADMIN_TAG])

    @router.put("/fixture/guarded")
    def guarded(auth: AuthContext = Depends(admin_auth)) -> dict:
        return {"ok": True}

    @router.put("/fixture/unguarded")
    def unguarded(auth: AuthContext = Depends(current_auth),
                  ctx: AppContext = Depends(get_context)) -> dict:
        ctx.db.document("platform_settings/fixture").set({"by": auth.email})
        return {"ok": True}

    return router


def test_the_sweep_catches_an_unguarded_admin_route(world) -> None:
    """The control: the same `platform_surface` and `not_refused` this file
    runs over the real app find a route left without a gate, and do not
    flag the guarded one beside it."""
    db, ctx, app, client = world
    app.include_router(_unguarded_fixture_router(db))
    app.openapi_schema = None  # built again, with the fixture routes in it
    surface = platform_surface(app, _routers())
    fixture = [r for r in surface if "/fixture/" in r[1]]
    assert fixture == [("PUT", "/v1/admin/fixture/guarded"),
                       ("PUT", "/v1/admin/fixture/unguarded")], fixture

    missed = not_refused(client, _as_requests(fixture), _headers("wsadmin", "eng"))
    assert missed == ["PUT /v1/admin/fixture/unguarded -> 200"], missed
    assert "platform_settings/fixture" in db.docs, "the unguarded route did not run"


# ---------------------------------------------------------------------------
# Who the people are
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("person,tenant,role", NON_ADMINS, ids=[p for p, _, _ in NON_ADMINS])
def test_each_person_is_who_the_sweep_says(world, person, tenant, role) -> None:
    _, _, _, client = world
    me = client.get("/v1/tenants/me", headers=_headers(person, tenant))
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["principal"]["is_admin"] is False
    assert body["principal"]["is_admin_unresolved"] is False
    assert body["role"]["name"] == role, body.get("role")

    root = client.get("/v1/tenants/me", headers=_headers("root", "eng")).json()
    assert root["principal"]["is_admin"] is True, "the platform admin is not one: the 403s prove nothing"
    assert client.get("/v1/admin/tenants", headers=_headers("root", "eng")).status_code == 200


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("person,tenant,role", NON_ADMINS, ids=[p for p, _, _ in NON_ADMINS])
def test_every_platform_route_refuses_a_non_admin_and_writes_nothing(world, person, tenant, role) -> None:
    db, _, _, client = world
    headers = _headers(person, tenant)
    # One ordinary read first, so anything a first request writes for any
    # caller (a person's first-seen record) is in the "before" picture.
    assert client.get("/v1/tenants/me", headers=headers).status_code == 200
    before = copy.deepcopy(db.docs)

    missed = not_refused(client, _as_requests(SURFACE) + INLINE_PLATFORM_WRITES, headers)

    assert not missed, (
        f"{person} ({role} of {tenant or 'their personal workspace'}) was not refused by "
        f"{len(missed)} platform route(s): {missed}"
    )
    changed = sorted(k for k in set(before) | set(db.docs) if before.get(k) != db.docs.get(k))
    assert not changed, f"a refused request still wrote: {changed}"


# ---------------------------------------------------------------------------
# MCP and the plugin: the caller's own token, to routes this sweep holds
# ---------------------------------------------------------------------------

def _mcp_sources() -> list[Path]:
    files = sorted(MCP_SOURCE.glob("*.py"))
    assert files, f"no MCP source under {MCP_SOURCE}"
    return files


def test_every_admin_path_the_mcp_names_is_on_the_swept_surface() -> None:
    """MCP and the plugin (which calls the MCP server and the sc CLI) reach
    Admin only through these paths. Each must be a route this file proves
    refuses a non-admin; a path to an admin route the sweep does not see would
    be one nobody has checked."""
    named = set()
    for source in _mcp_sources():
        for match in re.finditer(r"/v1/admin[A-Za-z0-9_/:{}.-]*", source.read_text()):
            named.add(match.group(0).rstrip("/."))
    assert named, "the MCP source names no /v1/admin path; the scan read nothing"
    swept = {path for _, path in SURFACE}
    missing = sorted(p for p in named if p not in swept and p != ADMIN_PREFIX)
    assert not missing, f"the MCP calls admin paths the sweep does not hold: {missing}"


def test_the_mcp_sends_the_callers_identity_and_nothing_that_could_raise_it() -> None:
    """No key that acts as an admin for a non-admin: every header the MCP
    client sets is the caller's own bearer token, the workspace it selects
    (which the API checks against the caller), or content negotiation."""
    # `Accept-Encoding` is content negotiation (API-GZIP, #1822): it asks for a
    # gzip body and can raise nothing.
    allowed = {"Authorization", "Accept", "Accept-Encoding", "Content-Type", "Content-Length",
               "TENANT_HEADER"}
    seen: dict[str, list[str]] = {}
    for source in _mcp_sources():
        for match in re.finditer(r"add_header\(\s*(?:\"([^\"]+)\"|([A-Za-z_.]+))", source.read_text()):
            name = match.group(1) or match.group(2).rsplit(".", 1)[-1]
            seen.setdefault(name, []).append(source.name)
    assert "Authorization" in seen, "the scan found no Authorization header; it read nothing"
    extra = {name: files for name, files in seen.items() if name not in allowed}
    assert not extra, f"the MCP sets headers beyond the caller's identity: {extra}"
