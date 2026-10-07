"""A task whose submitter's GitHub connection failed parks before any lease (#780, OB6).

docs/onboarding.md §3.3 step 2. swarm-api's refresh sweep keeps each
person's user slot `git-u-<hex>` fresh, and marks the connection
`refresh_failed` when it cannot. A worker started for a task that runs on that
slot would hold a capacity slot to clone with a token GitHub refuses. So
`credentials.credential_for`, asked by admission with the task, says no, the
task parks CREDENTIAL_MISSING with nothing reserved, and the credential sweep
returns it to READY once the connection is `active` again.

Which task runs on a user slot is decided the "Without it" way (§3.3) until
the task carries its credential: the signed `submitted_by` holds a grant for
the task's repository. These tests seed the documents in OB3's shape
(`swarm_api.forgeapp`) and §3.1's grant id, and the last ones hold the
scheduler's restated id recipes to swarm-api's own functions.
"""

from __future__ import annotations

import pytest

from swarm_api import forgeapp, repositories, validation
from swarm_common.profiles import RUNNER_PROFILES

from scheduler import credentials
from scheduler.credentials import (
    AccountPool,
    ForgeAnswer,
    ForgeConnections,
    credential_for,
    needs_user_slot,
)
from scheduler.store import SchedulerStore

from .conftest import scheduler_settings, seed_pool, seed_task, seed_tenant

TENANT = "eng"
OTHER = "research"
PERSON = "Ada@Example.com"
REPOSITORY = "Saga/Widgets"
URL = f"https://github.com/{REPOSITORY}.git"


def world(db) -> None:
    seed_pool(db, "global", hard_limit=10)
    seed_tenant(db, TENANT)
    seed_tenant(db, OTHER)


def task(db, task_id: str, *, repository_url: str | None = URL, submitted_by: str = PERSON,
         tenant_id: str = TENANT, **fields) -> None:
    seed_task(db, task_id=task_id, tenant_id=tenant_id, **fields)
    db.docs[f"tasks/{task_id}"]["repository_url"] = repository_url
    db.docs[f"tasks/{task_id}"]["submitted_by"] = submitted_by


def grant(db, *, tenant_id: str = TENANT, user: str = PERSON, repository: str = REPOSITORY,
          stored_tenant: str | None = None) -> None:
    owner, repo = repository.split("/")
    repo_id = repositories.repo_id_for(tenant_id, owner, repo)
    hashed = forgeapp.user_hash(user)
    db.docs[f"forge_grants/{tenant_id}__{hashed}__{repo_id}"] = {
        "tenant_id": stored_tenant or tenant_id,
        "user_hash": hashed,
        "repo_id": repo_id,
        "repository": repository,
        "owner": owner,
        "mode": "write",
    }


def connection(db, state: str, *, tenant_id: str = TENANT, user: str = PERSON,
               stored_tenant: str | None = None) -> str:
    """A connection document as `ForgeApp._exchange` writes it, then given `state`."""
    conn_id = forgeapp.connection_id_for(tenant_id, user.strip().lower())
    db.docs[f"forge_connections/{conn_id}"] = {
        "connection_id": conn_id,
        "tenant_id": stored_tenant or tenant_id,
        "user": user.strip().lower(),
        "user_hash": forgeapp.user_hash(user),
        "forge": "github",
        "method": forgeapp.METHOD_APP_USER,
        "state": state,
        "failure": None,
    }
    return conn_id


def parked_detail(db, task_id: str) -> dict:
    prefix = f"tasks/{task_id}/events/"
    events = [doc for path, doc in sorted(db.docs.items())
              if path.startswith(prefix) and doc.get("type") == "parked"]
    assert events, f"{task_id} recorded no parked event"
    return events[-1]["detail"]


def leases(db) -> list[str]:
    return [path for path in db.docs if path.startswith("leases/")]


# --------------------------------------------------------------------------
# Admission
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state, answer",
    [
        (forgeapp.REFRESH_FAILED, "refresh_failed"),
        (forgeapp.REVOKED, "revoked"),
        ("something_new", "unrecognised"),
        (None, "connection_missing"),
    ],
)
def test_a_task_on_a_failed_connection_parks_before_any_lease(
    db, make_scheduler, dispatcher, state, answer
):
    world(db)
    grant(db)
    if state is not None:
        connection(db, state)
    task(db, "task_dead_token")

    report = make_scheduler().drain()

    doc = db.docs["tasks/task_dead_token"]
    assert doc["state"] == "PARKED", doc
    assert doc["park_reason"] == "CREDENTIAL_MISSING"
    assert parked_detail(db, "task_dead_token")["forge_connection"] == answer
    assert report.parked == 1
    # Parked BEFORE a lease (invariant 1): nothing reserved in any pool, no
    # lease, no worker -- so no half-made reservation either (invariant 2),
    # and nothing for concurrency to count (invariant 3).
    assert db.docs["pools/global"]["active"] == 0
    assert leases(db) == []
    assert dispatcher.dispatched == []


def test_an_active_connection_with_a_grant_is_admitted(db, make_scheduler, dispatcher):
    world(db)
    grant(db)
    connection(db, forgeapp.ACTIVE)
    task(db, "task_live")

    make_scheduler().drain()

    assert db.docs["tasks/task_live"]["state"] == "DISPATCHED"
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_live"]


@pytest.mark.parametrize(
    "why, repository_url, with_grant",
    [
        ("no grant: the task runs on the tenant token, as before onboarding", URL, False),
        ("no repository at all", None, True),
        ("a repository on another forge", "https://gitlab.com/saga/widgets.git", True),
    ],
)
def test_a_task_that_needs_no_user_slot_ignores_the_connection(
    db, make_scheduler, dispatcher, why, repository_url, with_grant
):
    world(db)
    if with_grant:
        grant(db)
    connection(db, forgeapp.REFRESH_FAILED)
    task(db, "task_tenant_token", repository_url=repository_url)

    make_scheduler().drain()

    assert db.docs["tasks/task_tenant_token"]["state"] == "DISPATCHED", why


def test_another_tenants_connection_does_not_stand_in(db, make_scheduler, dispatcher):
    """Invariant 9: the same person's active connection in another tenant, or a
    document at this tenant's id that says it belongs to another, is not theirs here."""
    world(db)
    grant(db)
    connection(db, forgeapp.ACTIVE, tenant_id=OTHER)
    task(db, "task_cross")

    make_scheduler().drain()
    assert db.docs["tasks/task_cross"]["state"] == "PARKED"
    assert parked_detail(db, "task_cross")["forge_connection"] == "connection_missing"

    connection(db, forgeapp.ACTIVE, stored_tenant=OTHER)
    db.docs["tasks/task_cross"].update(state="READY", park_reason=None)
    make_scheduler().drain()
    assert db.docs["tasks/task_cross"]["state"] == "PARKED"


def test_a_grant_stored_for_another_tenant_is_no_grant(db, make_scheduler, dispatcher):
    world(db)
    grant(db, stored_tenant=OTHER)
    connection(db, forgeapp.REFRESH_FAILED)
    task(db, "task_foreign_grant")

    make_scheduler().drain()

    assert db.docs["tasks/task_foreign_grant"]["state"] == "DISPATCHED"


def test_a_task_with_no_repository_reads_no_forge_document(db, make_scheduler, monkeypatch):
    world(db)
    task(db, "task_plain", repository_url=None)
    reads: list[str] = []
    real = db.collection

    def collection(path: str):
        if path in (credentials.GRANTS, credentials.CONNECTIONS):
            reads.append(path)
        return real(path)

    monkeypatch.setattr(db, "collection", collection)
    make_scheduler().drain()

    assert db.docs["tasks/task_plain"]["state"] == "DISPATCHED"
    assert reads == []


# --------------------------------------------------------------------------
# The credential sweep
# --------------------------------------------------------------------------


def test_the_sweep_returns_the_park_once_the_connection_is_active(
    db, make_scheduler, dispatcher
):
    world(db)
    grant(db)
    conn_id = connection(db, forgeapp.REFRESH_FAILED)
    task(db, "task_reconnect")
    scheduler = make_scheduler()

    scheduler.drain()
    assert db.docs["tasks/task_reconnect"]["state"] == "PARKED"

    still = scheduler.drain()
    assert still.promoted_credentials == 0
    assert db.docs["tasks/task_reconnect"]["state"] == "PARKED"
    assert db.docs["pools/global"]["active"] == 0

    # The person reconnects: the exchange rewrites the document `active`. The
    # next drain reads it afresh (`ForgeConnections.forget`).
    db.docs[f"forge_connections/{conn_id}"]["state"] = forgeapp.ACTIVE
    report = scheduler.drain()

    assert report.promoted_credentials == 1
    assert db.docs["tasks/task_reconnect"]["state"] == "DISPATCHED"
    promoted = [doc for path, doc in sorted(db.docs.items())
                if path.startswith("tasks/task_reconnect/events/")
                and doc.get("type") == "ready"]
    assert promoted and promoted[-1]["detail"]["forge_connection"] == "active"


def test_the_sweep_returns_the_park_when_the_person_disconnects(
    db, make_scheduler, dispatcher
):
    """Disconnect deletes the person's grants, so the task needs no user slot."""
    world(db)
    grant(db)
    conn_id = connection(db, forgeapp.REFRESH_FAILED)
    task(db, "task_disconnect")
    scheduler = make_scheduler()
    scheduler.drain()
    assert db.docs["tasks/task_disconnect"]["state"] == "PARKED"

    db.docs[f"forge_connections/{conn_id}"]["state"] = forgeapp.REVOKED
    for path in [p for p in db.docs if p.startswith("forge_grants/")]:
        del db.docs[path]
    scheduler.drain()

    assert db.docs["tasks/task_disconnect"]["state"] == "DISPATCHED"


# --------------------------------------------------------------------------
# The rule itself
# --------------------------------------------------------------------------


def test_without_a_task_the_connection_is_not_asked(db):
    """The Cloud Run Job's secret mount asks without a task: nothing about the
    forge is decided after the lease."""
    world(db)
    grant(db)
    connection(db, forgeapp.REFRESH_FAILED)
    task(db, "task_x")
    store = SchedulerStore(db)
    tenant = store.get_tenant(TENANT)
    pool = AccountPool(broker_url="", read_accounts=list)

    answer = credential_for(RUNNER_PROFILES["mock"], tenant, pool)
    assert answer.forge is ForgeAnswer.NOT_ASKED and answer.runnable
    assert "forge_connection" not in answer.park_detail()

    loaded = store.get_task("task_x")
    asked = credential_for(RUNNER_PROFILES["mock"], tenant, pool, task=loaded,
                           forge=ForgeConnections(db))
    assert asked.forge is ForgeAnswer.REFRESH_FAILED and not asked.runnable


def test_needs_user_slot_is_the_grant_of_the_signed_submitter(db):
    world(db)
    grant(db, user="someone.else@example.com")
    task(db, "task_y")
    loaded = SchedulerStore(db).get_task("task_y")

    assert not needs_user_slot(loaded, ForgeConnections(db))
    grant(db)
    assert needs_user_slot(loaded, ForgeConnections(db))


# --------------------------------------------------------------------------
# Parity: the restated recipes are swarm-api's
# --------------------------------------------------------------------------


@pytest.mark.parametrize("email", ["ada@example.com", "  Ada@Example.COM ", "x+y@saga.xyz"])
def test_the_user_and_connection_ids_are_swarm_apis(email):
    assert credentials.user_hash(email) == forgeapp.user_hash(email)
    assert credentials.connection_id_for(TENANT, email) == forgeapp.connection_id_for(
        TENANT, email.strip().lower()
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Saga/Widgets",
        "https://github.com/saga/widgets.git",
        "https://www.github.com/saga/widgets/",
        "git@github.com:Saga/Widgets.git",
        "https://github.com:8443/saga/widgets",
        "https://github.com/saga",
        "https://github.com/saga/widgets/tree/main",
        "https://gitlab.com/saga/widgets",
        "not a url",
        "",
        None,
    ],
)
def test_the_repository_reading_and_repo_id_are_swarm_apis(url):
    named = credentials.github_repository(url)
    assert named == validation.merge_repository(url)
    if named is not None:
        assert credentials.repo_id_for(TENANT, *named) == repositories.repo_id_for(
            TENANT, *named
        )


def test_the_collections_and_states_are_ob3s():
    assert credentials.CONNECTIONS == forgeapp.CONNECTIONS
    assert credentials.GRANTS == forgeapp.GRANTS
    assert {forgeapp.ACTIVE, forgeapp.REFRESH_FAILED, forgeapp.REVOKED} == set(
        credentials._STATES
    )
