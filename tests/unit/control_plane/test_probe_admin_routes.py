"""A scoped probe admin is admin ONLY on the routes the roles and join
acceptance probes measure (owner decision 2026-11-20, "roles + join only";
docs/design/workspace-roles.md section 8).

`roles.PROBE_ADMIN_ROUTES` is the list. `RoleStore.resolve` answers `admin`
for a probe admin only when its caller passes `probe_admin=True`, and only
those routes do; every other admin gate gets its ordinary role, capped at
`member`, and refuses it 403 `probe_admin_not_permitted`.

Two halves:

* THE ENUMERATION: the list is exactly the owner's; every entry is a route
  the app serves; every literal `probe_admin=True` is in an endpoint on the
  list, and every other `probe_admin=` only forwards its own parameter; every
  route that declares `people_scope` (which answers a probe admin from its
  entry) is on the list; and `probe_admin_tenants` is read only where the
  list's routes need it. A NEW admin route cannot admit a probe admin without
  failing one of these.
* THE BEHAVIOUR: as `probe`, listed for eng, every workspace-admin write a
  workspace admin makes -- a secret, an MCP server, the tenant provider key,
  a git-token slot, the merge, code-intelligence and judgment settings, a
  private plugin install, a repository's issue flag, the workspace's name,
  removing a member -- is refused `probe_admin_not_permitted`, with the role
  switches OFF (so a switch cannot let it through), and nothing is written;
  while the listed routes still work for it.

Every fake value is built at runtime.
"""

from __future__ import annotations

import ast
import secrets
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import swarm_api
from swarm_api import codeintel, refusals, roles
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.mcp_servers import FLAG as MCP_FLAG
from swarm_api.metrics import ApiMetrics
from swarm_api.routes.people import people_scope
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, NoForgeTokens, api_settings, seed_tenant
from .test_account_api import FakePool, account
from .test_child_tasks_api import KEY
from .test_roles_gates_admin import SETTINGS, admin_writes, h, stored
from .test_roles_store import Clock
from .test_workspace_secrets import FakeSecretManager, RecordingFirestore

PACKAGE = Path(swarm_api.__file__).resolve().parent
PROBE = "probe@example.com"
GROUPS = {
    PROBE: (ENG_GROUP,),
    "alice@example.com": (ENG_GROUP,),
    "bob@example.com": (ENG_GROUP,),
    "ops@example.com": (ADMIN_GROUP,),
}
SWITCHES = (refusals.SWITCHES["role_admin_required"].env,
            refusals.SWITCHES["merge_settings_owner_required"].env,
            refusals.SWITCHES[codeintel.OwnerRequired.code].env)

#: What the owner allowed, written out again so a change to the list is a
#: change to this test too.
OWNER_ALLOWED = frozenset({
    ("GET", "/v1/workspace/roles"),
    ("PUT", "/v1/workspace/roles/person"),
    ("DELETE", "/v1/workspace/roles/person"),
    ("PUT", "/v1/workspace/roles/default"),
    ("GET", "/v1/workspace/members"),
    ("GET", "/v1/workspace/join-link"),
    ("PUT", "/v1/workspace/join-link"),
    ("GET", "/v1/admin/people"),
    ("POST", "/v1/admin/join-requests/{ref}/approve"),
    ("POST", "/v1/admin/join-requests/{ref}/deny"),
})
#: The functions that may read `probe_admin_tenants` (module, function).
TENANTS_READERS = frozenset({
    ("swarm_api.roles", "probe_admin_scope"),
    ("swarm_api.routes.people", "administered_workspaces"),
    ("swarm_api.routes.people", "people_scope"),
    ("swarm_api.routes.people", "join_scope"),
})


@pytest.fixture
def db() -> RecordingFirestore:
    db = RecordingFirestore()
    seed_tenant(db, "eng", credentials=("git",))
    return db


@pytest.fixture
def sm() -> FakeSecretManager:
    return FakeSecretManager()


@pytest.fixture
def credentials() -> InMemoryCredentials:
    return InMemoryCredentials()


@pytest.fixture
def client(db, sm, credentials, monkeypatch) -> TestClient:
    monkeypatch.setenv(MCP_FLAG, "on")
    for switch in SWITCHES:
        monkeypatch.delenv(switch, raising=False)
    tokens = {f"token-{e.split('@')[0]}": {"email": e, "email_verified": True,
                                           "sub": f"sub-{e}", "hd": "example.com"}
              for e in GROUPS}
    ctx = build_context(
        settings=api_settings(settings_key=secrets.token_hex(32), child_key=KEY,
                              console_url="https://console.example.test",
                              probe_admin_users=((PROBE, ("eng",)),)),
        db=db, verifier=StaticTokenVerifier(tokens), groups=StaticGroups(GROUPS),
        credentials=credentials, waker=NullWaker(), metrics=ApiMetrics(),
        objects=None, forge_tokens=NoForgeTokens(), now=Clock(),
    )
    app = create_app(ctx)
    app.state.workspace_secret_writer = sm
    app.state.account_pool = FakePool(account("eng", "team-max"))
    return TestClient(app, raise_server_exceptions=False)


# -- the enumeration -------------------------------------------------------------


def _walk(routes: Any) -> Iterator[APIRoute]:
    """Every `APIRoute`, however FastAPI nests an included router (see
    test_continuation_scope_is_narrow._iter_api_routes for why)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif (router := getattr(route, "original_router", None)) is not None:
            yield from _walk(router.routes)
        elif (sub_routes := getattr(route, "routes", None)) is not None:
            yield from _walk(sub_routes)


def _routes(client: TestClient) -> list[APIRoute]:
    routes = list(_walk(client.app.routes))
    assert len(routes) > 100, "the route walk found almost nothing"
    return routes


def _endpoints(client: TestClient) -> dict[tuple[str, str], set[tuple[str, str]]]:
    """(module, function) -> the (method, path) pairs it serves."""
    out: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for route in _routes(client):
        key = (route.endpoint.__module__, route.endpoint.__name__)
        out.setdefault(key, set()).update((m, route.path) for m in route.methods)
    return out


def _calls() -> list[tuple[str, ast.FunctionDef, ast.Call]]:
    """Every call in swarm_api, with its module and its innermost function."""
    found: list[tuple[str, ast.FunctionDef, ast.Call]] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        module = "swarm_api." + ".".join(path.relative_to(PACKAGE).with_suffix("").parts)
        module = module.removesuffix(".__init__")
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def visit(node: ast.AST, fn: ast.FunctionDef | None) -> None:
            for child in ast.iter_child_nodes(node):
                inner = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
                if isinstance(child, ast.Call) and inner is not None:
                    found.append((module, inner, child))
                visit(child, inner)

        visit(tree, None)
    return found


def _name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def test_the_list_is_exactly_what_the_owner_allowed():
    assert roles.PROBE_ADMIN_ROUTES == OWNER_ALLOWED
    assert ("DELETE", "/v1/workspace/members/person") not in roles.PROBE_ADMIN_ROUTES


def test_every_listed_route_is_served(client):
    served = {(m, r.path) for r in _routes(client) for m in r.methods}
    assert roles.PROBE_ADMIN_ROUTES <= served, roles.PROBE_ADMIN_ROUTES - served


def test_only_listed_endpoints_opt_in_and_helpers_only_forward(client):
    endpoints = _endpoints(client)
    opted: set[tuple[str, str]] = set()
    bad: list[str] = []
    for module, fn, call in _calls():
        if _name(call) in ("Resolution", "replace"):
            # The marker itself, built in `RoleStore.resolve`.
            continue
        for kw in call.keywords:
            if kw.arg != "probe_admin":
                continue
            where = f"{module}.{fn.name} -> {_name(call)}"
            if isinstance(kw.value, ast.Constant):
                if kw.value.value is not True:
                    continue
                served = endpoints.get((module, fn.name))
                if not served:
                    bad.append(f"{where}: probe_admin=True outside an endpoint")
                elif not served <= roles.PROBE_ADMIN_ROUTES:
                    bad.append(f"{where}: serves {sorted(served - roles.PROBE_ADMIN_ROUTES)}")
                else:
                    opted |= served
            else:
                params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
                if not (isinstance(kw.value, ast.Name) and kw.value.id == "probe_admin"
                        and "probe_admin" in params):
                    bad.append(f"{where}: probe_admin= must forward the caller's own parameter")
    assert not bad, "\n".join(bad)
    people = {(m, r.path) for r in _routes(client) for m in r.methods if _declares_people_scope(r)}
    # Every listed route opts in by one of the two ways, and nothing else does.
    assert opted | people == roles.PROBE_ADMIN_ROUTES


def _declares_people_scope(route: APIRoute) -> bool:
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is people_scope:
            return True
        stack.extend(dep.dependencies)
    return False


def test_every_route_that_declares_people_scope_is_listed(client):
    declared = {(m, r.path) for r in _routes(client) for m in r.methods
                if _declares_people_scope(r)}
    assert declared, "people_scope is declared by no route: the enumeration reads nothing"
    assert declared <= roles.PROBE_ADMIN_ROUTES, declared - roles.PROBE_ADMIN_ROUTES


def test_probe_admin_tenants_is_read_only_where_the_list_needs_it():
    readers = {(module, fn.name) for module, fn, call in _calls()
               if _name(call) == "probe_admin_tenants"}
    assert readers, "probe_admin_tenants is called nowhere: the enumeration reads nothing"
    assert readers <= TENANTS_READERS, readers - TENANTS_READERS


# -- the behaviour -----------------------------------------------------------------


def _everything(db, sm, credentials) -> dict[str, Any]:
    """What a refused write must leave alone: every document but a person's
    own sighting, and every Secret Manager and provider-key call."""
    docs = {p: d for p, d in db.docs.items() if not p.startswith("people/")}
    return {"docs": docs, **stored(db, sm, credentials)}


def _refused(response) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "probe_admin_not_permitted", response.text


@pytest.mark.parametrize("kind", ["secret", "mcp", "tenant_key", "git_token"])
def test_secrets_keys_servers_and_git_slots_refuse_it(client, db, sm, credentials, kind):
    before = _everything(db, sm, credentials)
    _refused(admin_writes(client, "probe")[kind])
    assert _everything(db, sm, credentials) == before


@pytest.mark.parametrize("kind", sorted(SETTINGS))
def test_each_workspace_setting_refuses_it_with_the_switches_off(client, db, sm, credentials,
                                                                 kind):
    route, body = SETTINGS[kind]
    before = _everything(db, sm, credentials)
    _refused(client.put(route.format("eng"), headers=h("probe"), json=body))
    assert _everything(db, sm, credentials) == before


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/v1/workspaces/eng/plugin-catalogue/installs",
     {"source": {"kind": "github", "repository": "example-org/plug", "ref": "main"}}),
    ("PATCH", "/v1/repositories/repo-1", {"agents_may_file_issues": True}),
    ("PUT", "/v1/workspaces/eng/name", {"display_name": "Renamed"}),
    ("DELETE", "/v1/workspace/members/person", {"email": "bob@example.com"}),
])
def test_the_other_admin_routes_refuse_it(client, db, sm, credentials, method, path, body):
    before = _everything(db, sm, credentials)
    _refused(client.request(method, path, headers=h("probe"), json=body))
    assert _everything(db, sm, credentials) == before


def test_a_member_is_not_given_the_probe_admins_code(client):
    """The code is the probe admin's alone: a plain member meets the gate's
    own answer, as before."""
    response = client.put("/v1/workspaces/eng/name", headers=h("bob"),
                          json={"display_name": "Renamed"})
    assert response.status_code == 403
    assert response.json()["code"] != "probe_admin_not_permitted"


def test_the_listed_routes_still_work_for_it(client):
    roles_read = client.get("/v1/workspace/roles", headers=h("probe"))
    assert roles_read.status_code == 200, roles_read.text
    assert (roles_read.json()["me"]["role"], roles_read.json()["can_manage"]) == ("admin", True)
    for response in (
        client.put("/v1/workspace/roles/person", headers=h("probe"),
                   json={"email": "bob@example.com", "role": "viewer"}),
        client.request("DELETE", "/v1/workspace/roles/person", headers=h("probe"),
                       json={"email": "bob@example.com"}),
        client.put("/v1/workspace/roles/default", headers=h("probe"),
                   json={"role": "viewer", "reason": "probe"}),
        client.get("/v1/workspace/members", headers=h("probe")),
        client.get("/v1/workspace/join-link", headers=h("probe")),
        client.get("/v1/admin/people", headers=h("probe", None)),
    ):
        assert response.status_code == 200, response.text
    audit = client.get("/v1/workspace/roles", headers=h("probe")).json()["audit"]
    mine = [e for e in audit if e["by"] == PROBE]
    assert mine and all(e["detail"]["via"] == roles.VIA_PROBE_ADMIN for e in mine), mine
