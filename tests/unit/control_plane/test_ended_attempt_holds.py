"""An attempt that ended without its worker releasing gives its holds back on the next sweep (#380).

The reconciler releases a fenced attempt's holds at its own fence
(tests/unit/control_plane/test_fenced_attempt_holds.py and
tests/unit/worker/test_reconciler_releases_fenced_holds.py). That covers only
the fences the reconciler commits. An attempt can also end with nothing calling
the broker at all: fenced by a pass whose broker call failed, superseded by a
generation somebody else bumped, or a worker that recorded its end and was
killed before its `finally` reached `_release_account`. Each of those holds kept
counting until `DEFAULT_HOLD_TTL` (3 h).

The quota sweep now reads, for every stamped live hold, the task and attempt
documents it names, and releases the hold when the record says the attempt is
over:

  * the task's `current_generation` is past the attempt's `generation` -- the
    attempt is fenced, the same evidence the reconciler acts on; or
  * the attempt has `completed_at`, longer ago than `ENDED_ATTEMPT_GRACE` --
    the worker's own release normally lands inside that window.

What the record cannot prove ended is left to the TTL: a live attempt, one
that completed inside the grace, an unstamped hold, and a hold naming a task or
attempt document that does not exist.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quota_broker.accounts import HOLD_LOG_COLLECTION, holds_from_firestore
from quota_broker.accountstore import AccountStore
from quota_broker.main import ENDED_ATTEMPT_GRACE, create_app
from quota_broker.service import QuotaBroker
from quota_broker.settings import BrokerSettings

from .conftest import core_settings
from .fakes import FakeFirestore

ENG = "eng"
PERSONAL = f"{ENG}:personal"


class _Identity:
    def __init__(self) -> None:
        self.tenant: str | None = ENG
        self.platform = False

    def resolve(self, authorization):  # noqa: ANN001 - the broker's duck type
        return (None, True) if self.platform else (self.tenant, False)


@pytest.fixture()
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture()
def client(db):
    broker = QuotaBroker(db, settings=BrokerSettings(core=core_settings(), default_hard_max=50))
    store = AccountStore(broker.db)
    store.register(ENG, "personal")
    identity = _Identity()
    c = TestClient(
        create_app(broker, identity=identity, account_store=store),
        raise_server_exceptions=False,
    )
    c.identity = identity
    return c


def _assign(client, **stamp: str) -> str:
    client.identity.tenant, client.identity.platform = ENG, False
    response = client.post("/v1/accounts/assign", json={"provider": "anthropic", **stamp})
    assert response.status_code == 200, response.text
    assert response.json().get("account_id") == PERSONAL, response.json()
    return response.json()["assignment_id"]


def _seed_attempt(
    db: FakeFirestore,
    task_id: str,
    attempt_id: str,
    *,
    generation: int,
    current_generation: int,
    completed_at: datetime | None = None,
) -> None:
    db.docs[f"tasks/{task_id}"] = {
        "id": task_id, "tenant_id": ENG, "state": "RUNNING",
        "current_generation": current_generation,
    }
    db.docs[f"attempts/{attempt_id}"] = {
        "attempt_id": attempt_id, "task_id": task_id, "tenant_id": ENG,
        "generation": generation, "completed_at": completed_at,
    }


def _sweep(client) -> dict[str, Any]:
    client.identity.platform, client.identity.tenant = True, None
    response = client.post("/v1/quota/sweep")
    assert response.status_code == 200, response.text
    return response.json()


def _held(db) -> list[str]:
    """attempt_id of each live hold on the account; "" for an unstamped one."""
    doc = db.docs[f"accounts/{PERSONAL}"]
    holds = holds_from_firestore(doc["holds"])
    assert doc["assigned"] == len(holds), "`assigned` drifted from `holds`"
    return sorted(h.attempt_id or "" for h in holds)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_a_superseded_attempts_hold_is_released_and_the_live_ones_are_not(client, db):
    fenced = _assign(client, task_id="task-f", attempt_id="att-f")
    _assign(client, task_id="task-l", attempt_id="att-l")
    _seed_attempt(db, "task-f", "att-f", generation=3, current_generation=4)
    # The control: same shape, generation still current, nothing completed.
    _seed_attempt(db, "task-l", "att-l", generation=3, current_generation=3)
    assert _held(db) == ["att-f", "att-l"]

    body = _sweep(client)

    assert _held(db) == ["att-l"]
    assert body["ended_holds"]["released"] == 1
    record = db.docs[f"{HOLD_LOG_COLLECTION}/{fenced}"]
    assert record["end"] == "released"
    assert record["released_at"] is not None


def test_a_completed_attempt_is_released_only_after_the_grace(client, db):
    _assign(client, task_id="task-old", attempt_id="att-old")
    _assign(client, task_id="task-new", attempt_id="att-new")
    now = _now()
    _seed_attempt(
        db, "task-old", "att-old", generation=1, current_generation=1,
        completed_at=now - ENDED_ATTEMPT_GRACE - timedelta(seconds=30),
    )
    # Its worker's own release may still be on the way: left alone.
    _seed_attempt(
        db, "task-new", "att-new", generation=1, current_generation=1,
        completed_at=now - timedelta(seconds=5),
    )

    _sweep(client)

    assert _held(db) == ["att-new"]


def test_what_the_record_cannot_prove_ended_is_left_to_the_ttl(client, db):
    _assign(client)                                         # unstamped
    _assign(client, task_id="task-gone", attempt_id="att-gone")  # no documents
    _assign(client, task_id="task-x", attempt_id="att-x")
    # An attempt document that names a different task proves nothing about
    # the hold that claims it.
    _seed_attempt(db, "task-y", "att-x", generation=1, current_generation=2)
    db.docs["tasks/task-x"] = {"id": "task-x", "tenant_id": ENG, "current_generation": 2}

    body = _sweep(client)

    assert _held(db) == ["", "att-gone", "att-x"]
    assert body["ended_holds"]["released"] == 0


def test_a_second_sweep_writes_nothing_to_the_account(client, db):
    _assign(client, task_id="task-f", attempt_id="att-f")
    _seed_attempt(db, "task-f", "att-f", generation=3, current_generation=4)
    assert _sweep(client)["ended_holds"]["released"] == 1
    before = dict(db.docs[f"accounts/{PERSONAL}"])

    assert _sweep(client)["ended_holds"]["released"] == 0
    assert db.docs[f"accounts/{PERSONAL}"] == before
