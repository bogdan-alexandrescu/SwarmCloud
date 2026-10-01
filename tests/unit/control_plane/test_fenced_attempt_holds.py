"""A fenced attempt's account holds are given back at once, by the platform (#380).

A worker that crashes, or is fenced, never runs its own release, so its hold
kept counting until `DEFAULT_HOLD_TTL` (3 h) lapsed and `prune_holds` noticed --
and `choose()` handed out that much less of the account meanwhile. Holds are
stamped with `task_id` and `attempt_id` (#412, #413), so once the reconciler
has fenced an attempt the broker can release exactly that attempt's holds.

What these pin, on the broker's route `POST /v1/holds/release-attempt`:

  * fencing attempt N releases exactly N's holds -- on every account it holds,
    lent ones included -- and no other attempt's, no other task's with the same
    attempt id, and no unstamped hold;
  * each released hold's record is closed as `released`, in the same
    transaction, as every other change to `holds` is;
  * a repeat finds nothing and writes nothing (idempotent);
  * a worker's token is refused, as on the broker's other platform routes, and
    an empty id is refused before it could match an unstamped hold.

The reconciler's half -- that it asks only after its fence has committed -- is
tests/unit/worker/test_reconciler_releases_fenced_holds.py.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from quota_broker.accounts import HOLD_LOG_COLLECTION, holds_from_firestore
from quota_broker.accountstore import AccountStore
from quota_broker.main import create_app
from quota_broker.service import QuotaBroker
from quota_broker.settings import BrokerSettings

from .conftest import core_settings
from .fakes import FakeFirestore

ENG = "eng"
RESEARCH = "research"
PERSONAL = f"{ENG}:personal"
SECOND = f"{ENG}:second"
ROUTE = "/v1/holds/release-attempt"


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
    store.register(ENG, "personal", lend_to=(RESEARCH,))
    store.register(ENG, "second", lend_to=(RESEARCH,))
    identity = _Identity()
    c = TestClient(
        create_app(broker, identity=identity, account_store=store),
        raise_server_exceptions=False,
    )
    c.identity = identity
    return c


def _assign(client, tenant: str, *, exclude: tuple[str, ...] = (), **stamp: str) -> dict[str, Any]:
    client.identity.tenant, client.identity.platform = tenant, False
    body: dict[str, Any] = {"provider": "anthropic", **stamp}
    if exclude:
        body["exclude"] = list(exclude)
    response = client.post("/v1/accounts/assign", json=body)
    assert response.status_code == 200, response.text
    assert response.json().get("account_id"), response.json()
    return response.json()


def _holds(db, account_id: str) -> list[tuple[str, str]]:
    """(task_id, attempt_id) of each live hold; "" for an unstamped one."""
    doc = db.docs[f"accounts/{account_id}"]
    holds = holds_from_firestore(doc["holds"])
    assert doc["assigned"] == len(holds), "`assigned` drifted from `holds`"
    return sorted((h.task_id or "", h.attempt_id or "") for h in holds)


def _release(client, *, as_platform: bool = True, **body: Any):
    client.identity.platform = as_platform
    client.identity.tenant = None if as_platform else ENG
    return client.post(ROUTE, json=body)


@pytest.fixture()
def held(client, db):
    """Attempt N on both accounts (one was unusable, so it took the other),
    attempt M on one, the same attempt id under another task, a borrower's
    hold for N, and a hold from before stamping."""
    n1 = _assign(client, ENG, exclude=(SECOND,), task_id="task-n", attempt_id="att-n")
    n2 = _assign(client, ENG, exclude=(PERSONAL,), task_id="task-n", attempt_id="att-n")
    _assign(client, ENG, exclude=(SECOND,), task_id="task-m", attempt_id="att-m")
    _assign(client, ENG, exclude=(PERSONAL,), task_id="task-other", attempt_id="att-n")
    lent = _assign(
        client, RESEARCH, exclude=(SECOND,), task_id="task-n", attempt_id="att-n"
    )
    _assign(client, ENG, exclude=(SECOND,))                 # unstamped
    return [n1["assignment_id"], n2["assignment_id"], lent["assignment_id"]]


def test_fencing_attempt_n_releases_exactly_ns_holds(client, db, held):
    response = _release(client, task_id="task-n", attempt_id="att-n")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["released"] == 3
    assert sorted(body["accounts"]) == sorted([PERSONAL, SECOND])
    assert _holds(db, PERSONAL) == [("", ""), ("task-m", "att-m")]
    assert _holds(db, SECOND) == [("task-other", "att-n")]
    for assignment_id in held:
        record = db.docs[f"{HOLD_LOG_COLLECTION}/{assignment_id}"]
        assert record["end"] == "released"
        assert record["released_at"] is not None


def test_releasing_again_changes_nothing(client, db, held):
    assert _release(client, task_id="task-n", attempt_id="att-n").json()["released"] == 3
    before = {k: dict(v) for k, v in db.docs.items()}

    again = _release(client, task_id="task-n", attempt_id="att-n")

    assert again.status_code == 200
    assert again.json()["released"] == 0
    assert db.docs == before, "a repeat release wrote something"


def test_an_attempt_with_no_holds_releases_nothing(client, db, held):
    before = {k: dict(v) for k, v in db.docs.items()}
    response = _release(client, task_id="task-z", attempt_id="att-z")
    assert response.json()["released"] == 0
    assert db.docs == before


def test_a_worker_token_is_refused(client, db, held):
    before = {k: dict(v) for k, v in db.docs.items()}

    refused = _release(client, as_platform=False, task_id="task-n", attempt_id="att-n")

    assert refused.status_code == 403
    assert refused.json()["code"] == "forbidden"
    assert db.docs == before


@pytest.mark.parametrize(
    "body",
    [
        {"task_id": "task-n", "attempt_id": ""},
        {"task_id": "", "attempt_id": "att-n"},
        {"attempt_id": "att-n"},
    ],
)
def test_an_empty_or_missing_id_is_refused_before_it_can_match_an_unstamped_hold(
    client, db, held, body
):
    before = {k: dict(v) for k, v in db.docs.items()}
    assert _release(client, **body).status_code == 422
    assert db.docs == before
