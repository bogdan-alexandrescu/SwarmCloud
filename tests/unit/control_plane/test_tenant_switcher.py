"""The tenant switcher: `X-Swarm-Tenant` SELECTS among verified memberships.

Owner decision 2026-10-01. Invariant 9 (per-tenant isolation) is the risk: the
header may only pick one of the registered tenant groups Cloud Identity has
already confirmed the caller is in. It never grants a tenant, it never changes
admin status, and a header naming a tenant the caller is not verified in is a
403 raised before any route body runs -- nothing written, nothing read.

`dave` is in eng AND research (eng first in the admin order); `alice` is in eng
only; `carol` is in neither; `root` is an admin who is also in eng.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import (
    TENANT_HEADER,
    TENANT_QUERY,
    TENANT_QUERY_ROUTES,
    Authenticator,
    StaticTokenVerifier,
)
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.errors import Forbidden
from swarm_api.groups import GroupLookupError, StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import (
    ADMIN_GROUP,
    ENG_GROUP,
    PROJECT,
    RESEARCH_GROUP,
    api_settings,
    auth_header,
    seed_task,
    seed_tenant,
)
from .fakes import FakeFirestore
from .test_checkpoint_content import WORKSPACE, put_checkpoint, tar_gz
from .test_checkpoint_content import seed as seed_checkpoint_task


class CountingFirestore(FakeFirestore):
    """Counts every reference handed out, so "nothing was read" is measurable."""

    def __init__(self) -> None:
        super().__init__()
        self.touched = 0

    def collection(self, path: str):  # type: ignore[override]
        self.touched += 1
        return super().collection(path)

    def document(self, path: str):  # type: ignore[override]
        self.touched += 1
        return super().document(path)

    def get_all(self, references, field_paths=None, transaction=None):  # type: ignore[override]
        self.touched += 1
        return super().get_all(references, field_paths, transaction)

    def transaction(self, **kwargs: Any):  # type: ignore[override]
        self.touched += 1
        return super().transaction(**kwargs)

    def batch(self):  # type: ignore[override]
        self.touched += 1
        return super().batch()


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    return {
        "alice@saga.xyz": (ENG_GROUP,),
        "bob@saga.xyz": (RESEARCH_GROUP,),
        "carol@saga.xyz": (),
        "dave@saga.xyz": (ENG_GROUP, RESEARCH_GROUP),
        "root@saga.xyz": (ADMIN_GROUP, ENG_GROUP),
    }


@pytest.fixture
def db() -> CountingFirestore:
    return CountingFirestore()


def as_(user: str, tenant: str | None = None) -> dict[str, str]:
    headers = auth_header(user)
    if tenant is not None:
        headers[TENANT_HEADER] = tenant
    return headers


def submit(client: TestClient, user: str, tenant: str | None = None) -> Any:
    body = {"runner_profile": "mock", "input": {"prompt": f"hello from {user}"}}
    return client.post("/v1/tasks", headers=as_(user, tenant), json=body)


def test_the_header_name_is_the_documented_one():
    assert TENANT_HEADER == "X-Swarm-Tenant"


# -- member of A and B: the header picks B -----------------------------------

def test_a_member_of_two_tenants_submits_into_the_one_the_header_names(client):
    default = submit(client, "dave")
    assert default.status_code == 201, default.text
    assert default.json()["task"]["tenant_id"] == "eng"

    chosen = submit(client, "dave", "research")
    assert chosen.status_code == 201, chosen.text
    assert chosen.json()["task"]["tenant_id"] == "research"


def test_the_header_scopes_reads_to_the_chosen_tenant(client):
    in_eng = submit(client, "dave").json()["task"]["id"]
    in_research = submit(client, "dave", "research").json()["task"]["id"]

    listed_default = {t["id"] for t in client.get("/v1/tasks", headers=as_("dave")).json()["tasks"]}
    listed_research = {
        t["id"] for t in client.get("/v1/tasks", headers=as_("dave", "research")).json()["tasks"]
    }
    assert listed_default == {in_eng}
    assert listed_research == {in_research}

    # A task of the OTHER tenant reads as absent under the header, exactly as
    # another tenant's task always has.
    assert client.get(f"/v1/tasks/{in_eng}", headers=as_("dave", "research")).status_code == 404
    assert client.get(f"/v1/tasks/{in_research}", headers=as_("dave", "research")).status_code == 200
    assert client.get(f"/v1/tasks/{in_research}", headers=as_("dave")).status_code == 404


def test_me_reports_the_chosen_tenant_and_its_principal(client):
    me = client.get("/v1/tenants/me", headers=as_("dave", "research"))
    assert me.status_code == 200, me.text
    tenant = me.json()["tenant"]
    assert tenant["tenant_id"] == "research"
    assert tenant["principal"] == RESEARCH_GROUP


def test_bob_in_research_sees_what_dave_submitted_there(client):
    """The chosen tenant is the REAL tenant, shared with its other members."""
    task_id = submit(client, "dave", "research").json()["task"]["id"]
    assert client.get(f"/v1/tasks/{task_id}", headers=as_("bob")).status_code == 200
    assert client.get(f"/v1/tasks/{task_id}", headers=as_("alice")).status_code == 404


# -- member of A only: header B is 403, store untouched ----------------------

@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/tasks"),
    ("GET", "/v1/tasks"),
    ("GET", "/v1/tenants/me"),
    ("GET", "/v1/tenants/mine"),
])
def test_a_tenant_the_caller_is_not_in_is_403_with_nothing_read_or_written(client, db, method, path):
    submit(client, "bob")  # research exists, so there is something to steal
    before = dict(db.docs)
    touched = db.touched

    body = {"runner_profile": "mock", "input": {"prompt": "x"}} if method == "POST" else None
    response = client.request(method, path, headers=as_("alice", "research"), json=body)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "tenant_not_member"
    assert db.docs == before
    assert db.touched == touched


def test_the_refusal_is_the_same_for_a_tenant_that_does_not_exist(client):
    """No oracle: "not yours" and "no such tenant" are one answer."""
    submit(client, "bob")
    real = client.get("/v1/tasks", headers=as_("alice", "research"))
    made_up = client.get("/v1/tasks", headers=as_("alice", "nonesuch"))
    assert real.status_code == made_up.status_code == 403
    assert real.json() == made_up.json()


def test_a_personal_tenant_id_is_not_selectable(client):
    """`u-<name>` is nobody's membership, including carol's own."""
    assert submit(client, "alice", "u-carol").status_code == 403
    assert submit(client, "carol", "u-carol").status_code == 403


def test_the_header_cannot_name_an_admin_group(client):
    """An admin group is a membership, not a tenant group: never selectable."""
    assert submit(client, "root", "swarm-admins").status_code == 403


def test_the_header_is_matched_exactly(client):
    assert submit(client, "dave", "Research").status_code == 403
    assert submit(client, "dave", "research@saga.xyz").status_code == 403


# -- no header: byte-for-byte today ------------------------------------------

def test_no_header_is_todays_first_match(client):
    for user, expected in (("alice", "eng"), ("bob", "research"), ("carol", "u-carol"),
                           ("dave", "eng"), ("root", "eng")):
        response = submit(client, user)
        assert response.status_code == 201, response.text
        assert response.json()["task"]["tenant_id"] == expected, user


def test_an_empty_header_is_absent(client):
    response = submit(client, "dave", "")
    assert response.status_code == 201, response.text
    assert response.json()["task"]["tenant_id"] == "eng"


def _authenticator(group_map: dict[str, tuple[str, ...]]) -> Authenticator:
    claims = {
        f"t-{email.split('@')[0]}": {"email": email, "email_verified": True, "sub": f"s-{email}"}
        for email in group_map
    }
    return Authenticator(api_settings(), StaticTokenVerifier(claims), StaticGroups(group_map))


def test_no_header_context_is_identical_to_the_one_without_the_parameter(group_map):
    auth = _authenticator(group_map)
    for user in ("alice", "bob", "carol", "dave", "root"):
        legacy = auth.authenticate(f"Bearer t-{user}")
        explicit = auth.authenticate(f"Bearer t-{user}", None, tenant=None)
        assert legacy == explicit, user


# -- principal and id agree under the header ---------------------------------

def test_principal_and_tenant_id_describe_the_same_tenant(group_map):
    auth = _authenticator(group_map)
    chosen = auth.authenticate("Bearer t-dave", None, tenant="research")
    assert chosen.tenant_id == "research"
    assert chosen.tenant_principal == RESEARCH_GROUP

    default = auth.authenticate("Bearer t-dave", None, tenant="eng")
    assert default.tenant_id == "eng"
    assert default.tenant_principal == ENG_GROUP
    # Selecting the default tenant explicitly is the same context as not
    # selecting at all.
    assert default == auth.authenticate("Bearer t-dave")


def test_the_header_does_not_change_admin_status(group_map):
    auth = _authenticator({**group_map, "root@saga.xyz": (ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP)})
    root = auth.authenticate("Bearer t-root", None, tenant="research")
    assert root.is_admin is True
    assert root.tenant_id == "research"
    dave = auth.authenticate("Bearer t-dave", None, tenant="research")
    assert dave.is_admin is False
    # The principal's groups are what Cloud Identity confirmed, unchanged.
    assert dave.principal.groups == (ENG_GROUP, RESEARCH_GROUP)


def test_a_refused_header_raises_forbidden_not_a_context(group_map):
    auth = _authenticator(group_map)
    with pytest.raises(Forbidden):
        auth.authenticate("Bearer t-alice", None, tenant="research")


class _FlakyResearch:
    """Research's lookup fails; eng answers. `groups_for`'s rule then swallows
    the failure (it is below a confirmed match), so research is UNVERIFIED --
    and an unverified membership must never be selectable."""

    def groups_for(self, member_email: str, candidates: tuple[str, ...]) -> tuple[str, ...]:
        if RESEARCH_GROUP in candidates and ENG_GROUP not in candidates:
            raise GroupLookupError("research did not answer")
        return tuple(g for g in candidates if g == ENG_GROUP)


def test_an_unverified_membership_is_not_selectable():
    claims = {"t-dave": {"email": "dave@saga.xyz", "email_verified": True, "sub": "s"}}
    auth = Authenticator(api_settings(), StaticTokenVerifier(claims), _FlakyResearch())
    assert auth.authenticate("Bearer t-dave").tenant_id == "eng"
    with pytest.raises(Forbidden):
        auth.authenticate("Bearer t-dave", None, tenant="research")


# -- GET /v1/tenants/mine -----------------------------------------------------

def test_mine_lists_exactly_the_verified_memberships_in_admin_order(client):
    def mine(user: str) -> list[dict[str, str]]:
        response = client.get("/v1/tenants/mine", headers=as_(user))
        assert response.status_code == 200, response.text
        return response.json()

    assert mine("dave") == [
        {"tenant_id": "eng", "display_name": ENG_GROUP},
        {"tenant_id": "research", "display_name": RESEARCH_GROUP},
    ]
    assert mine("alice") == [{"tenant_id": "eng", "display_name": ENG_GROUP}]
    assert mine("bob") == [{"tenant_id": "research", "display_name": RESEARCH_GROUP}]
    # The admin group is not a tenant group, so it is never listed.
    assert mine("root") == [{"tenant_id": "eng", "display_name": ENG_GROUP}]
    # A personal tenant is not a membership; the list is empty, not an error.
    assert mine("carol") == []


def test_mine_follows_the_admin_order_not_the_directorys(db, tokens):
    settings = api_settings(tenant_groups=(RESEARCH_GROUP, ENG_GROUP))
    ctx = build_context(
        settings=settings, db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups({"dave@saga.xyz": (ENG_GROUP, RESEARCH_GROUP)}),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(), objects=None,
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    listed = client.get("/v1/tenants/mine", headers=as_("dave")).json()
    assert [t["tenant_id"] for t in listed] == ["research", "eng"]
    # And the default is the first of them, as resolve_tenant says.
    assert submit(client, "dave").json()["task"]["tenant_id"] == "research"


def test_mine_reads_and_writes_nothing(client, db):
    touched = db.touched
    before = dict(db.docs)
    assert client.get("/v1/tenants/mine", headers=as_("dave")).status_code == 200
    assert db.touched == touched
    assert db.docs == before


def test_every_listed_tenant_is_selectable_and_nothing_else_is(client):
    for user in ("alice", "bob", "dave", "root"):
        listed = [t["tenant_id"] for t in client.get("/v1/tenants/mine", headers=as_(user)).json()]
        for tenant in ("eng", "research"):
            status = client.get("/v1/tasks", headers=as_(user, tenant)).status_code
            assert (status == 200) == (tenant in listed), (user, tenant, status)


# -- ?tenant= on the routes a browser fetches by itself ------------------------
#
# An <img src>, a download href and an "open full" tab carry no custom header,
# so the switcher's choice rides as a query parameter there -- and is validated
# by the very same `_select_tenant`, so it too selects and never grants.

def an_artifact_in(db, objects, tenant: str, task: str) -> None:
    seed_tenant(db, tenant)
    seed_task(db, task_id=task, tenant_id=tenant, state="SUCCEEDED")
    key = f"tenants/{tenant}/tasks/{task}/attempts/att_1/artifacts/out.txt"
    objects.put(key, b"hello\n")
    db.docs[f"tasks/{task}"]["result_summary"] = {
        "artifacts": [{"name": "out.txt", "bytes": 6, "uri": f"gs://swarm-artifacts-{PROJECT}/{key}"}],
        "artifact_bytes": 6,
        "logs": {},
    }


def raw_url(task: str, **params: str) -> str:
    query = "&".join(f"{k}={v}" for k, v in {"name": "out.txt", **params}.items())
    return f"/v1/tasks/{task}/artifacts/raw?{query}"


def test_the_query_parameter_is_accepted_on_the_browser_fetched_routes_only():
    # The raw artifact (an <img src>, a download, an "open full" tab) and the
    # checkpoint archive (a plain download link, so the browser can stream a
    # large file to disk) -- the two routes the console hands the browser to
    # fetch by itself, with no header.
    assert TENANT_QUERY == "tenant"
    assert TENANT_QUERY_ROUTES == frozenset({
        ("GET", "/v1/tasks/{task_id}/artifacts/raw"),
        ("GET", "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/content"),
    })


def test_a_chosen_tenants_artifact_is_served_with_the_query_parameter(client, db, objects):
    an_artifact_in(db, objects, "research", "task_r")
    chosen = client.get(raw_url("task_r", tenant="research"), headers=as_("dave"))
    assert chosen.status_code == 200, chosen.text
    assert chosen.content == b"hello\n"
    # The control: without the selection it is the DEFAULT tenant's (eng), and
    # the research task is not there -- which is what the console used to hit.
    assert client.get(raw_url("task_r"), headers=as_("dave")).status_code == 404


def test_the_query_parameter_cannot_grant_a_tenant(client, db, objects):
    an_artifact_in(db, objects, "research", "task_r")
    before = dict(db.docs)
    touched = db.touched
    refused = client.get(raw_url("task_r", tenant="research"), headers=as_("alice"))
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "tenant_not_member"
    assert db.docs == before
    assert db.touched == touched


def test_the_query_parameter_is_ignored_on_every_other_route(client):
    task_id = submit(client, "dave", "research").json()["task"]["id"]
    # On a JSON route the header is the only selector: ?tenant= changes nothing.
    assert client.get(f"/v1/tasks/{task_id}?tenant=research", headers=as_("dave")).status_code == 404
    assert client.get("/v1/tasks?tenant=research", headers=as_("alice")).status_code == 200


def test_header_and_query_naming_different_tenants_is_refused(client, db, objects):
    an_artifact_in(db, objects, "research", "task_r")
    both = client.get(raw_url("task_r", tenant="research"), headers=as_("dave", "eng"))
    assert both.status_code == 422, both.text
    agreeing = client.get(raw_url("task_r", tenant="research"), headers=as_("dave", "research"))
    assert agreeing.status_code == 200, agreeing.text
    # Identity is still checked first: no credential is a 401, not a 422.
    anonymous = client.get(raw_url("task_r", tenant="research"), headers={TENANT_HEADER: "eng"})
    assert anonymous.status_code == 401


def a_checkpoint_in(db, objects, tenant: str, task: str) -> bytes:
    seed_checkpoint_task(db, tenant=tenant, task=task)
    archive = tar_gz(WORKSPACE)
    put_checkpoint(objects, tenant=tenant, task=task, archive=archive, file_count=4)
    return archive


def checkpoint_url(task: str, **params: str) -> str:
    query = "&".join(f"{k}={v}" for k, v in {"attempt_id": "att_1", **params}.items())
    return f"/v1/tasks/{task}/checkpoints/ckpt-00001/content?{query}"


def test_a_chosen_tenants_checkpoint_downloads_with_the_query_parameter(client, db, objects):
    archive = a_checkpoint_in(db, objects, "research", "task_r")
    chosen = client.get(checkpoint_url("task_r", tenant="research"), headers=as_("dave"))
    assert chosen.status_code == 200, chosen.text
    assert chosen.content == archive
    # The control: the download link carries no header, so without the query
    # parameter it is the DEFAULT tenant's (eng), where the research task is
    # not -- the 404 a console switched to research used to get.
    assert client.get(checkpoint_url("task_r"), headers=as_("dave")).status_code == 404


def test_the_query_parameter_cannot_grant_a_checkpoint(client, db, objects):
    a_checkpoint_in(db, objects, "research", "task_r")
    before = dict(db.docs)
    touched = db.touched
    refused = client.get(checkpoint_url("task_r", tenant="research"), headers=as_("alice"))
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "tenant_not_member"
    assert db.docs == before
    assert db.touched == touched
    # Agreeing header and query are one selection; disagreeing ones are refused.
    assert client.get(
        checkpoint_url("task_r", tenant="research"), headers=as_("dave", "eng")
    ).status_code == 422
