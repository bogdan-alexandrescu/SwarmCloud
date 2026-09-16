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
