"""A task whose submitter's GitHub connection failed parks before any lease (#780, OB6).

docs/onboarding.md §3.3 step 2. swarm-api's refresh sweep keeps each
person's user slot `git-u-<hex>` fresh, and marks the connection
`refresh_failed` when it cannot. A worker started for a task that runs on that
slot would hold a capacity slot to clone with a token GitHub refuses. So
`credentials.credential_for`, asked by admission with the task, says no, the
task parks CREDENTIAL_MISSING with nothing reserved, and the credential sweep
returns it to READY once the connection is `active` again.

Which task runs on a user slot is the task's signed `forge_credential`
(contract request 54; lane OB5, owner decision 2026-10-07): a `git-u-<hex>`
suffix asks about the submitter's connection, and `git`, None or a repository
token is the tenant's fallback and asks nothing. The scheduler reads no grant:
the worker re-reads it before cloning and before each push (§3.3 step 4).
These tests seed the documents in OB3's shape (`swarm_api.forgeapp`), and the
last ones hold the scheduler's restated recipes to swarm-api's own functions.
"""

from __future__ import annotations

import pytest

from swarm_api import forgeapp, gittokens, repositories, validation
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


#: "The submitter's own user slot", as swarm-api resolves a granted person's task.
SLOT = object()


def slot(user: str = PERSON) -> str:
    return gittokens.provider_suffix(gittokens.Scope.USER, user=user)


def task(db, task_id: str, *, repository_url: str | None = URL, submitted_by: str = PERSON,
         tenant_id: str = TENANT, forge_credential: object = SLOT, **fields) -> None:
    seed_task(db, task_id=task_id, tenant_id=tenant_id, **fields)
    db.docs[f"tasks/{task_id}"]["repository_url"] = repository_url
    db.docs[f"tasks/{task_id}"]["submitted_by"] = submitted_by
    db.docs[f"tasks/{task_id}"]["forge_credential"] = (
        slot(submitted_by) if forge_credential is SLOT else forge_credential
    )


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
    "why, forge_credential",
    [
        ("'git': the tenant token, the fallback (a service submission)", "git"),
        ("None: a task written before contract request 54", None),
        ("a repository token is not a person's", "git-r-" + "0" * 16),
    ],
)
def test_a_task_that_needs_no_user_slot_ignores_the_connection(
    db, make_scheduler, dispatcher, why, forge_credential
):
    world(db)
    grant(db)
    connection(db, forgeapp.REFRESH_FAILED)
    task(db, "task_tenant_token", forge_credential=forge_credential)

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


def test_the_scheduler_reads_no_grant(db, make_scheduler, dispatcher, monkeypatch):
    """The signed slot decides; the grant is the worker's to re-read (§3.3 step 4)."""
    world(db)
    connection(db, forgeapp.ACTIVE)
    task(db, "task_no_grant_read")
    reads: list[str] = []
    real = db.collection

    def collection(path: str):
        reads.append(path)
        return real(path)

    monkeypatch.setattr(db, "collection", collection)
    make_scheduler().drain()

    assert db.docs["tasks/task_no_grant_read"]["state"] == "DISPATCHED"
    assert credentials.GRANTS not in reads
    assert credentials.CONNECTIONS in reads


def test_a_slot_that_is_not_the_submitters_parks(db, make_scheduler, dispatcher):
    """swarm-api names a person's task by their own slot; another's has no
    connection this task may rely on, whatever state that person's is in."""
    world(db)
    connection(db, forgeapp.ACTIVE)
    connection(db, forgeapp.ACTIVE, user="bob@example.com")
    task(db, "task_other_slot", forge_credential=slot("bob@example.com"))

    make_scheduler().drain()

    assert db.docs["tasks/task_other_slot"]["state"] == "PARKED"
    assert parked_detail(db, "task_other_slot")["forge_connection"] == "connection_missing"


def test_a_task_with_no_repository_reads_no_forge_document(db, make_scheduler, monkeypatch):
    world(db)
    task(db, "task_plain", repository_url=None, forge_credential=None)
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


def test_a_disconnected_persons_task_stays_parked(db, make_scheduler, dispatcher):
    """The task was signed for the person's slot, so disconnecting does not
    move it onto the tenant token: it waits, costing nothing, for a reconnect."""
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

    assert db.docs["tasks/task_disconnect"]["state"] == "PARKED"
    assert db.docs["pools/global"]["active"] == 0
    assert leases(db) == []


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


@pytest.mark.parametrize(
    "forge_credential, needs",
    [(SLOT, True), ("git", False), (None, False), ("git-r-" + "a" * 16, False),
     # Not a shape `Task` takes: decoded as None, never a drain that fails.
     ("git-u-not-hex", False), ("swarm-tenant-research-git", False)],
)
def test_needs_user_slot_reads_the_signed_forge_credential(db, forge_credential, needs):
    world(db)
    task(db, "task_y", forge_credential=forge_credential)
    loaded = SchedulerStore(db).get_task("task_y")

    assert needs_user_slot(loaded, ForgeConnections(db)) is needs


@pytest.mark.parametrize("state", [forgeapp.REFRESH_FAILED, forgeapp.ACTIVE])
def test_credential_for_parks_a_user_slot_on_refresh_failed_and_never_git(db, state):
    """The brief's rule, asked of `credential_for` itself: a user slot whose
    connection is `refresh_failed` is not runnable; `git` never asks."""
    world(db)
    connection(db, state)
    task(db, "task_user")
    task(db, "task_git", forge_credential="git")
    store = SchedulerStore(db)
    tenant = store.get_tenant(TENANT)
    pool = AccountPool(broker_url="", read_accounts=list)

    user = credential_for(RUNNER_PROFILES["mock"], tenant, pool,
                          task=store.get_task("task_user"), forge=ForgeConnections(db))
    git = credential_for(RUNNER_PROFILES["mock"], tenant, pool,
                         task=store.get_task("task_git"), forge=ForgeConnections(db))

    assert user.runnable is (state == forgeapp.ACTIVE)
    if state == forgeapp.REFRESH_FAILED:
        assert user.forge is ForgeAnswer.REFRESH_FAILED
        assert user.park_detail()["forge_connection"] == "refresh_failed"
    assert git.forge is ForgeAnswer.NO_USER_SLOT and git.runnable


# --------------------------------------------------------------------------
# Parity: the restated recipes are swarm-api's
# --------------------------------------------------------------------------


@pytest.mark.parametrize("email", ["ada@example.com", "  Ada@Example.COM ", "x+y@saga.xyz"])
def test_the_user_and_connection_ids_are_swarm_apis(email):
    assert credentials.user_hash(email) == forgeapp.user_hash(email)
    assert credentials.connection_id_for(TENANT, email) == forgeapp.connection_id_for(
        TENANT, email.strip().lower()
    )


@pytest.mark.parametrize("email", ["ada@example.com", "  Ada@Example.COM "])
def test_the_user_slot_hash_is_swarm_apis(db, email):
    world(db)
    task(db, "task_z", submitted_by=email)
    loaded = SchedulerStore(db).get_task("task_z")
    assert loaded.forge_credential == gittokens.provider_suffix(gittokens.Scope.USER, user=email)
    assert credentials.user_slot_hash(loaded) == forgeapp.user_hash(email)


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
