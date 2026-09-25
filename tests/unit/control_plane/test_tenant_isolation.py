"""The tenant boundary.

`alice@saga.xyz` is in `eng@saga.xyz` -> tenant `eng`.
`bob@saga.xyz`   is in `research@saga.xyz` -> tenant `research`.
`carol@saga.xyz` is in no registered group -> personal tenant `u-carol`.

The property under test is not "the route filters by tenant" -- it is that there
is NO request bob can construct that returns alice's task. Reading another
tenant's id must be indistinguishable from reading an id that does not exist,
because a different status code is itself an enumeration oracle.
"""

from __future__ import annotations

from .conftest import auth_header


def submit(client, user: str, **overrides):
    body = {"runner_profile": "mock", "input": {"prompt": f"hello from {user}"}}
    body.update(overrides)
    response = client.post("/v1/tasks", headers=auth_header(user), json=body)
    assert response.status_code == 201, response.text
    return response.json()["task"]


# -- tenant resolution ----------------------------------------------------

def test_group_membership_decides_the_tenant(client):
    assert submit(client, "alice")["tenant_id"] == "eng"
    assert submit(client, "bob")["tenant_id"] == "research"


def test_user_in_no_registered_group_gets_a_personal_tenant(client):
    assert submit(client, "carol")["tenant_id"] == "u-carol"


# -- the required case: A cannot read B's tasks ---------------------------

def test_tenant_cannot_read_another_tenants_task_by_id(client):
    alice_task = submit(client, "alice")

    seen_by_owner = client.get(f"/v1/tasks/{alice_task['id']}", headers=auth_header("alice"))
    assert seen_by_owner.status_code == 200

    stolen = client.get(f"/v1/tasks/{alice_task['id']}", headers=auth_header("bob"))
    assert stolen.status_code == 404

    # Indistinguishable from an id that never existed: no oracle.
    missing = client.get("/v1/tasks/task_does_not_exist", headers=auth_header("bob"))
    assert missing.status_code == 404
    assert stolen.json()["code"] == missing.json()["code"]


def test_list_never_returns_another_tenants_tasks(client):
    alice_ids = {submit(client, "alice")["id"] for _ in range(3)}
    bob_ids = {submit(client, "bob")["id"] for _ in range(2)}

    bob_list = client.get("/v1/tasks", headers=auth_header("bob")).json()
    returned = {task["id"] for task in bob_list["tasks"]}
    assert returned == bob_ids
    assert not (returned & alice_ids)
    assert all(task["tenant_id"] == "research" for task in bob_list["tasks"])


def test_events_and_artifacts_are_tenant_scoped(client):
    alice_task = submit(client, "alice")

    assert client.get(
        f"/v1/tasks/{alice_task['id']}/events", headers=auth_header("alice")
    ).status_code == 200

    for path in ("events", "artifacts"):
        response = client.get(
            f"/v1/tasks/{alice_task['id']}/{path}", headers=auth_header("bob")
        )
        assert response.status_code == 404, path


def test_cancel_of_another_tenants_task_is_refused(client, db):
    alice_task = submit(client, "alice")

    response = client.post(
        f"/v1/tasks/{alice_task['id']}/cancel", headers=auth_header("bob")
    )
    assert response.status_code == 404
    # And it really did not touch the document.
    assert db.docs[f"tasks/{alice_task['id']}"]["cancel_requested"] is False
    assert db.docs[f"tasks/{alice_task['id']}"]["state"] == "READY"


def test_workflow_reads_are_tenant_scoped(client):
    created = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={"steps": [{"step_id": "only", "runner_profile": "mock"}]},
    )
    workflow_id = created.json()["workflow"]["workflow_id"]

    assert client.get(
        f"/v1/workflows/{workflow_id}", headers=auth_header("alice")
    ).status_code == 200
    assert client.get(
        f"/v1/workflows/{workflow_id}", headers=auth_header("bob")
    ).status_code == 404
    assert client.post(
        f"/v1/workflows/{workflow_id}/cancel", headers=auth_header("bob")
    ).status_code == 404

    bob_workflows = client.get("/v1/workflows", headers=auth_header("bob")).json()
    assert bob_workflows["workflows"] == []


def test_filters_cannot_widen_the_scope(client):
    """A query parameter must never be able to reach across the boundary."""
    alice_task = submit(client, "alice")

    for query in (
        "?workflow_id=any",
        "?runner_profile=mock",
        "?state=READY",
    ):
        listed = client.get(f"/v1/tasks{query}", headers=auth_header("bob")).json()
        assert all(t["tenant_id"] == "research" for t in listed["tasks"])
        assert alice_task["id"] not in {t["id"] for t in listed["tasks"]}


def test_capacity_hides_other_tenants_pools(client, db):
    from .conftest import seed_pool

    seed_pool(db, "global", hard_limit=100)
    seed_pool(db, "tenant:eng", hard_limit=20, active=7)
    seed_pool(db, "tenant:research", hard_limit=20, active=1)
    seed_pool(db, "provider:anthropic:tenant:eng", hard_limit=10, active=3)

    capacity = client.get("/v1/capacity", headers=auth_header("bob")).json()
    names = {pool["name"] for pool in capacity["pools"]}
    assert "global" in names
    assert "tenant:research" in names
    # Another tenant's live usage is not bob's business.
    assert "tenant:eng" not in names
    assert "provider:anthropic:tenant:eng" not in names


def test_admin_sees_every_pool(client, db):
    from .conftest import seed_pool

    seed_pool(db, "tenant:eng", hard_limit=20, active=7)
    seed_pool(db, "tenant:research", hard_limit=20)

    capacity = client.get("/v1/capacity", headers=auth_header("root")).json()
    names = {pool["name"] for pool in capacity["pools"]}
    assert {"tenant:eng", "tenant:research"} <= names


def test_stats_counts_only_the_callers_tenant(client):
    for _ in range(3):
        submit(client, "alice")
    submit(client, "bob")

    bob_stats = client.get("/v1/stats", headers=auth_header("bob")).json()
    assert bob_stats["tenant_id"] == "research"
    assert bob_stats["tasks_by_state"]["READY"] == 1
    # A non-admin gets no platform-wide view at all.
    assert "platform_tasks_by_state" not in bob_stats

    root_stats = client.get("/v1/stats", headers=auth_header("root")).json()
    assert root_stats["platform_tasks_by_state"]["READY"] == 4


# -- authentication -------------------------------------------------------

def test_missing_and_bad_tokens_are_rejected(client):
    assert client.get("/v1/tasks").status_code == 401
    assert client.get(
        "/v1/tasks", headers={"Authorization": "Bearer not-a-real-token"}
    ).status_code == 401
    assert client.get(
        "/v1/tasks", headers={"Authorization": "token-alice"}
    ).status_code == 401


def test_error_bodies_never_echo_the_token(client):
    secret = "Bearer super-secret-token-value"
    response = client.get("/v1/tasks", headers={"Authorization": secret})
    assert response.status_code == 401
    assert "super-secret-token-value" not in response.text


def test_admin_surface_requires_an_admin_group(client):
    assert client.post(
        "/v1/admin/dispatch/pause", headers=auth_header("alice"), json={}
    ).status_code == 403
    assert client.post(
        "/v1/admin/dispatch/pause", headers=auth_header("root"), json={}
    ).status_code == 200


# -- tenant ids collide; principals must not -------------------------------

def collision_client(db, second_group: str, member: str):
    """An app whose registered tenant groups both slug to the tenant id `eng`."""
    from fastapi.testclient import TestClient

    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.deps import build_context
    from swarm_api.groups import StaticGroups
    from swarm_api.main import create_app
    from swarm_api.waker import NullWaker

    from .conftest import ENG_GROUP, api_settings, core_settings

    domains = tuple(sorted({"saga.xyz", second_group.split("@", 1)[1]}))
    tokens = {
        "token-alice": {"email": "alice@saga.xyz", "email_verified": True, "sub": "s1"},
        "token-mallory": {"email": member, "email_verified": True, "sub": "s2"},
    }
    ctx = build_context(
        settings=api_settings(
            core=core_settings(allowed_domains=domains),
            tenant_groups=(ENG_GROUP, second_group),
        ),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups({"alice@saga.xyz": (ENG_GROUP,), member: (second_group,)}),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def test_a_second_group_cannot_inherit_the_first_groups_tenant(db):
    """`eng@saga.xyz` and `eng@partner.com` both slug to tenant `eng`.

    The frozen `tenant_id_for_group` keeps the LOCAL PART only, and
    ALLOWED_DOMAINS is explicitly a comma-separated list, so this needs no
    unusual group naming -- just a second permitted domain. Without the principal
    check the second group silently inherits the first's service account, its
    Secret Manager secret, its GCS prefix and its namespace, and every member of
    it can list the first group's tasks and overwrite its provider key.
    """
    client = collision_client(db, "eng@partner.com", "mallory@partner.com")

    first = client.post(
        "/v1/tasks", headers={"Authorization": "Bearer token-alice"},
        json={"runner_profile": "mock"},
    )
    assert first.status_code == 201
    assert first.json()["task"]["tenant_id"] == "eng"
    assert db.docs["tenants/eng"]["principal"] == "eng@saga.xyz"

    stolen = client.post(
        "/v1/tasks", headers={"Authorization": "Bearer token-mallory"},
        json={"runner_profile": "mock"},
    )
    assert stolen.status_code == 409, stolen.text
    body = stolen.json()
    assert body["code"] == "conflict"
    assert body["detail"]["registered_principal"] == "eng@saga.xyz"
    assert body["detail"]["requested_principal"] == "eng@partner.com"

    # Nothing of theirs was written, and the tenant still belongs to the first.
    assert db.docs["tenants/eng"]["principal"] == "eng@saga.xyz"
    task_docs = [p for p in db.docs if p.startswith("tasks/") and p.count("/") == 1]
    assert task_docs == [f"tasks/{first.json()['task']['id']}"]


def test_a_punctuation_variant_of_a_group_name_collides_the_same_way(db):
    """`eng.team@`, `eng_team@` and `Eng-Team@` all slug to `eng-team`; the
    domain is not the only way in."""
    client = collision_client(db, "eng@saga.xyz.example", "mallory@saga.xyz.example")
    ok = client.post(
        "/v1/tasks", headers={"Authorization": "Bearer token-alice"},
        json={"runner_profile": "mock"},
    )
    assert ok.status_code == 201
    refused = client.post(
        "/v1/tasks", headers={"Authorization": "Bearer token-mallory"},
        json={"runner_profile": "mock"},
    )
    assert refused.status_code == 409


def test_every_member_of_one_group_shares_its_tenant(client, db):
    """The check is on the TENANT's principal, not the caller's, so two members
    of the same group are not mistaken for a collision."""
    from .conftest import ADMIN_GROUP  # noqa: F401  (root is in eng as well)

    assert submit(client, "alice")["tenant_id"] == "eng"
    assert submit(client, "root")["tenant_id"] == "eng"
    assert db.docs["tenants/eng"]["principal"] == "eng@saga.xyz"


def test_a_personal_tenant_records_the_user_as_its_principal(client, db):
    assert submit(client, "carol")["tenant_id"] == "u-carol"
    assert db.docs["tenants/u-carol"]["principal"] == "carol@saga.xyz"
    assert db.docs["tenants/u-carol"]["kind"] == "user"


# -- the collision must be refused on READS, not only on submits -----------
#
# Until 2026-09-21 the principal check ran only inside `ensure_tenant`, which
# the submit paths reach through `SubmissionService.tenant_for`. Every read and
# the cancel route filtered Firestore by the raw `auth.tenant_id` string
# instead, so the second principal was refused when submitting and served when
# listing, reading, cancelling, counting and asking for capacity. That is the
# wrong way round: the cheapest thing to do with somebody else's tenant id is
# read it, and cancelling their work is a WRITE that arrived through a read
# path. `deps.tenant_scope` -> `SubmissionService.scope_for` ->
# `Store.assert_tenant_scope` is what closes it.


def test_the_colliding_principal_cannot_read_or_cancel_through_any_route(db):
    client = collision_client(db, "eng@partner.com", "mallory@partner.com")
    mallory = {"Authorization": "Bearer token-mallory"}

    created = client.post(
        "/v1/tasks", headers={"Authorization": "Bearer token-alice"},
        json={"runner_profile": "mock"},
    )
    assert created.status_code == 201
    task_id = created.json()["task"]["id"]

    workflow = client.post(
        "/v1/workflows",
        headers={"Authorization": "Bearer token-alice"},
        json={"steps": [{"step_id": "only", "runner_profile": "mock"}]},
    )
    assert workflow.status_code == 201
    workflow_id = workflow.json()["workflow"]["workflow_id"]

    reads = (
        ("GET", "/v1/tasks"),
        ("GET", f"/v1/tasks/{task_id}"),
        ("GET", f"/v1/tasks/{task_id}/events"),
        ("GET", f"/v1/tasks/{task_id}/attempts"),
        ("GET", f"/v1/tasks/{task_id}/artifacts"),
        # The inspection routes. They read OBJECTS rather than documents, so
        # forgetting `tenant_scope` on one of them would not merely show
        # another tenant's metadata -- it would hand over their agent's stdout
        # and their checkpoint archives. This list is the guard that catches a
        # new route added without the dependency, so a new route belongs in it.
        ("GET", f"/v1/tasks/{task_id}/checkpoints"),
        ("GET", f"/v1/tasks/{task_id}/logs"),
        # The Artifacts tab's reads (#184): the agent's raw bytes, its
        # transcript, its answer, and the CPU readings off its events.
        ("GET", f"/v1/tasks/{task_id}/logs?stream=agent_stdout"),
        ("GET", f"/v1/tasks/{task_id}/artifacts/raw?name=x"),
        ("GET", f"/v1/tasks/{task_id}/transcript"),
        ("GET", f"/v1/tasks/{task_id}/answer"),
        ("GET", f"/v1/tasks/{task_id}/attempts?include=usage"),
        # Every attempt of the tenant, across tasks: the spend of every run
        # the colliding principal did not start.
        ("GET", "/v1/attempts"),
        ("GET", "/v1/workflows"),
        ("GET", f"/v1/workflows/{workflow_id}"),
        ("GET", "/v1/stats"),
        ("GET", "/v1/capacity"),
        ("GET", "/v1/providers"),
        ("GET", "/v1/tenants/me"),
    )
    for method, path in reads:
        response = client.request(method, path, headers=mallory)
        assert response.status_code == 409, f"{method} {path} -> {response.status_code}"
        assert response.json()["code"] == "conflict", path

    # The two cancel routes are writes that were reachable through the read
    # boundary, so they are asserted separately and against the document.
    for path in (f"/v1/tasks/{task_id}/cancel", f"/v1/workflows/{workflow_id}/cancel"):
        assert client.post(path, headers=mallory).status_code == 409, path
    assert db.docs[f"tasks/{task_id}"]["cancel_requested"] is False
    assert db.docs[f"tasks/{task_id}"]["state"] == "READY"

    # And the owner is unaffected by the guard.
    owner = {"Authorization": "Bearer token-alice"}
    assert client.get("/v1/tasks", headers=owner).status_code == 200
    assert client.get(f"/v1/tasks/{task_id}", headers=owner).status_code == 200
    assert client.get("/v1/stats", headers=owner).status_code == 200


def test_a_group_named_like_the_personal_prefix_collides_with_a_users_tenant(db):
    """`u-eng@saga.xyz` as a registered GROUP derives the same id as the
    PERSONAL tenant of `eng@saga.xyz`.

    The frozen `tenant_id_for_user` prefixes `u-` and documents that this
    "cannot collide with a group" -- but the group path adds no prefix at all,
    so a group whose local part already begins `u-` lands on exactly that id.
    No second domain and no punctuation variant is needed; one plausible group
    name is enough. The fix for the id itself belongs in the frozen module, so
    what is asserted here is the behaviour this service can guarantee without
    it: the two principals are never served as one tenant.
    """
    from fastapi.testclient import TestClient

    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.deps import build_context
    from swarm_api.groups import StaticGroups
    from swarm_api.main import create_app
    from swarm_api.waker import NullWaker

    from .conftest import api_settings

    group = "u-eng@saga.xyz"
    ctx = build_context(
        settings=api_settings(tenant_groups=(group,)),
        db=db,
        verifier=StaticTokenVerifier({
            "token-member": {"email": "dana@saga.xyz", "email_verified": True, "sub": "s1"},
            # In NO registered group, so she falls back to a personal tenant --
            # and her local part is `eng`, so that tenant is `u-eng` too.
            "token-eng": {"email": "eng@saga.xyz", "email_verified": True, "sub": "s2"},
        }),
        groups=StaticGroups({"dana@saga.xyz": (group,), "eng@saga.xyz": ()}),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)

    owned = client.post(
        "/v1/tasks", headers={"Authorization": "Bearer token-member"},
        json={"runner_profile": "mock"},
    )
    assert owned.status_code == 201
    assert owned.json()["task"]["tenant_id"] == "u-eng"
    assert db.docs["tenants/u-eng"]["principal"] == "u-eng@saga.xyz"

    intruder = {"Authorization": "Bearer token-eng"}
    # Same derived id, different principal: refused on the write path...
    assert client.post(
        "/v1/tasks", headers=intruder, json={"runner_profile": "mock"}
    ).status_code == 409
    # ...and on the read paths, which is what was missing.
    assert client.get("/v1/tasks", headers=intruder).status_code == 409
    assert client.get(
        f"/v1/tasks/{owned.json()['task']['id']}", headers=intruder
    ).status_code == 409


def test_the_guard_creates_no_tenant_document_on_a_read(db):
    """A GET must not write. `scope_for` checks; `tenant_for` is what creates."""
    from fastapi.testclient import TestClient

    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.deps import build_context
    from swarm_api.groups import StaticGroups
    from swarm_api.main import create_app
    from swarm_api.waker import NullWaker

    from .conftest import ENG_GROUP, api_settings

    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier({
            "token-alice": {"email": "alice@saga.xyz", "email_verified": True, "sub": "s1"},
        }),
        groups=StaticGroups({"alice@saga.xyz": (ENG_GROUP,)}),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)

    assert client.get(
        "/v1/tasks", headers={"Authorization": "Bearer token-alice"}
    ).status_code == 200
    assert "tenants/eng" not in db.docs


def test_a_disabled_tenant_can_still_read_and_cancel_its_own_work(client, db):
    """The read guard is the COLLISION check only, deliberately not `enabled`.

    Disabling a tenant stops it starting work -- submission 403s through
    `tenant_for`. A tenant that has been stopped still has to see what it has
    running and be able to cancel it, so the read path must not inherit that
    refusal.
    """
    task_id = submit(client, "alice")["id"]
    db.docs["tenants/eng"]["enabled"] = False

    assert client.post(
        "/v1/tasks", headers=auth_header("alice"), json={"runner_profile": "mock"}
    ).status_code == 403
    assert client.get("/v1/tasks", headers=auth_header("alice")).status_code == 200
    assert client.get(
        f"/v1/tasks/{task_id}", headers=auth_header("alice")
    ).status_code == 200
    assert client.post(
        f"/v1/tasks/{task_id}/cancel", headers=auth_header("alice")
    ).status_code == 200


def test_no_task_or_workflow_route_reads_the_raw_tenant_id():
    """The boundary is the dependency, so a new route must not bypass it.

    A source check, because the property is about code that does not exist yet:
    the failure this guards against is somebody adding `GET /v1/tasks/{id}/logs`
    next month with `auth.tenant_id` in the store call, which no request-level
    test can reach. It reads the AST rather than the text so that prose about
    `auth.tenant_id` -- which these two modules deliberately contain -- is not
    mistaken for a use of it.

    Only the two route modules. `service.py` uses `ctx.tenant_id` legitimately
    inside `scope_for` (it IS the guard) and on paths already behind
    `tenant_for`, so a blanket rule there would have to grow a whitelist, and a
    whitelist is the thing that quietly readmits what it was meant to exclude.
    `/v1/stats` and `/v1/capacity` are covered by the request-level test above.
    """
    import ast
    import pathlib

    import swarm_api

    root = pathlib.Path(swarm_api.__file__).parent
    offenders = []
    for name in ("routes/tasks.py", "routes/workflows.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"), filename=name)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "tenant_id"
                and isinstance(node.value, ast.Name)
                and node.value.id == "auth"
            ):
                offenders.append(f"{name}:{node.lineno}")
    assert not offenders, (
        "take the tenant from the tenant_scope dependency, not from auth: two "
        "verified principals can derive the same tenant_id, and only the "
        "dependency checks which of them the tenant document belongs to. "
        + ", ".join(offenders)
    )
