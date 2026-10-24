"""A pool account serves only the model classes its plan can run (owner decision R5).

Owner decision R5, 2026-10-17, on PR 1509: a pool account whose plan cannot
serve a class (Fable, say) must never be given a step of that class. So:

  * AN ACCOUNT LISTS THE CLASSES IT SERVES. Empty -- every account written
    before this field, and every account nobody has narrowed -- serves every
    class, so nothing changes until an admin narrows one.
  * THE BROKER ASSIGNS A STEP ONLY TO AN ACCOUNT THAT SERVES ITS CLASS, on an
    assign and on a swap. The class is the TASK's, read from `tasks/{id}` in
    the caller's own tenant and resolved by the frozen `step_model_class`; a
    class nobody can read is served only by an account that serves every
    class, never by a narrowed one.
  * A STEP WHOSE CLASS NO ACCOUNT SERVES WAITS. Admission parks it
    CREDENTIAL_MISSING with `account_pool: no_account_serves_class` and the
    class, before any lease: it holds nothing (invariant 1), nothing is
    reserved, so the all-or-nothing reservation is never half-made
    (invariant 2). The credential sweep promotes it once an account serves it.

Unit tests: the fake Firestore, no credentials, no emulator.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from quota_broker.accounts import Account, accounts_serving, choose
from quota_broker.accountstore import AccountStore, MalformedAccountError
from quota_broker.main import create_app
from swarm_common.profiles import RUNNER_PROFILES

from .conftest import measure, scheduler_settings, seed_pool, seed_task, seed_tenant

ENG = "eng"
RESEARCH = "research"
BROKER_URL = "https://swarm-quota-broker.example.run.app"


class _Identity:
    def __init__(self) -> None:
        self.tenant: str | None = ENG
        self.platform = False

    def resolve(self, authorization):  # noqa: ANN001 - the broker's duck type
        return (None, True) if self.platform else (self.tenant, False)


@pytest.fixture()
def accounts(broker) -> AccountStore:
    return AccountStore(broker.db)


@pytest.fixture()
def client(broker, accounts):
    identity = _Identity()
    app = create_app(broker, identity=identity, account_store=accounts)
    c = TestClient(app, raise_server_exceptions=False)
    c.identity = identity
    return c


def _task(db, task_id: str, *, tenant_id: str = ENG, model_class: str | None = None,
          profile: str = "claude-code", **fields) -> None:
    seed_task(
        db,
        task_id=task_id,
        tenant_id=tenant_id,
        runner_profile=profile,
        resource_class=RUNNER_PROFILES[profile].resource_class,
        provider=RUNNER_PROFILES[profile].provider,
        **fields,
    )
    db.docs[f"tasks/{task_id}"]["model_class"] = model_class


def _narrow(client, account_id: str, classes: list[str]):
    return client.put(
        f"/v1/accounts/{account_id}/model-classes", json={"model_classes": classes}
    )


def _assign(client, task_id: str | None = None, **body):
    if task_id is not None:
        body = {"task_id": task_id, "attempt_id": f"att_{task_id}", **body}
    return client.post("/v1/accounts/assign", json={"provider": "anthropic", **body})


# --------------------------------------------------------------------------
# The account record
# --------------------------------------------------------------------------


def _account(**fields) -> Account:
    return Account(account_id=f"{ENG}:one", owner_tenant=ENG, label="one", **fields)


def test_an_account_with_no_list_serves_every_class():
    account = _account()
    assert account.model_classes == ()
    assert all(account.serves_class(c) for c in ("opus", "fable", "sonnet", "haiku"))
    # A class nobody could read is served too: this account serves them all.
    assert account.serves_class(None)


def test_an_account_listing_only_opus_serves_opus_and_nothing_else():
    account = _account(model_classes=("opus",))
    assert account.serves_class("opus")
    assert not account.serves_class("fable")
    assert not account.serves_class("haiku")
    # Not known is not opus: a narrowed account takes only a class it names.
    assert not account.serves_class(None)


def test_the_list_round_trips_through_firestore_and_is_absent_on_old_documents():
    account = _account(model_classes=("opus", "sonnet"))
    stored = account.to_firestore()
    assert stored["model_classes"] == ["opus", "sonnet"]
    assert Account.from_firestore(stored).model_classes == ("opus", "sonnet")

    old = dict(stored)
    del old["model_classes"]
    assert Account.from_firestore(old).model_classes == ()


def test_a_malformed_list_takes_the_account_out_of_the_pool_rather_than_widening_it(
    broker, accounts
):
    """A string `model_classes` iterated would be letters; read as "every
    class" it would hand a Fable step to the very account it was meant to
    refuse. Malformed is unreadable, exactly as a malformed `lend_to` is."""
    accounts.register(ENG, "bad")
    broker.db.document(f"accounts/{ENG}:bad").update({"model_classes": "opus"})
    with pytest.raises(MalformedAccountError):
        accounts.get(f"{ENG}:bad")
    assert accounts.list() == []


def test_choose_skips_an_account_that_does_not_serve_the_class():
    now = datetime.now(timezone.utc)
    opus_only = _account(model_classes=("opus",))
    every = Account(account_id=f"{ENG}:two", owner_tenant=ENG, label="two")

    assert choose([opus_only], ENG, now, model_class="fable") is None
    assert choose([opus_only, every], ENG, now, model_class="fable") is every
    assert choose([opus_only], ENG, now, model_class="opus") is opus_only
    assert accounts_serving([opus_only], ENG, "anthropic", model_class="fable") == []
    assert accounts_serving([opus_only], ENG, "anthropic") == [opus_only]


def test_changing_who_an_account_is_lent_to_keeps_the_classes_it_serves(client, accounts):
    """Lending is changed by re-registering, which writes the whole document.
    Dropping the list there would silently widen an opus-only account back to
    every class -- and hand it the next Fable step."""
    accounts.register(ENG, "team")
    assert _narrow(client, f"{ENG}:team", ["opus"]).status_code == 200

    r = client.put(f"/v1/accounts/{ENG}:team/lending", json={"lend_to": [RESEARCH]})

    assert r.status_code == 200, r.text
    assert accounts.get(f"{ENG}:team").model_classes == ("opus",)
    assert r.json()["account"]["model_classes"] == ["opus"]


# --------------------------------------------------------------------------
# The admin route
# --------------------------------------------------------------------------


def test_the_owner_narrows_and_widens_the_classes_an_account_serves(client, accounts):
    accounts.register(ENG, "team")

    narrowed = _narrow(client, f"{ENG}:team", ["sonnet", "opus"])
    assert narrowed.status_code == 200, narrowed.text
    # Stored in the vocabulary's order, whatever order it was sent in.
    assert narrowed.json()["account"]["model_classes"] == ["opus", "sonnet"]
    assert accounts.get(f"{ENG}:team").model_classes == ("opus", "sonnet")

    listed = client.get("/v1/accounts", params={"tenant_id": ENG}).json()["accounts"]
    assert listed[0]["model_classes"] == ["opus", "sonnet"]

    widened = _narrow(client, f"{ENG}:team", [])
    assert widened.status_code == 200
    assert widened.json()["account"]["model_classes"] == []
    assert accounts.get(f"{ENG}:team").model_classes == ()


@pytest.mark.parametrize("classes", [["gpt"], ["opus", "opus"], ["claude-opus-5-5"]])
def test_a_class_outside_the_frozen_vocabulary_is_refused(client, accounts, classes):
    accounts.register(ENG, "team")
    r = _narrow(client, f"{ENG}:team", classes)
    assert r.status_code == 422, r.text
    assert accounts.get(f"{ENG}:team").model_classes == ()


def test_only_the_owner_may_narrow_an_account(client, accounts):
    accounts.register(RESEARCH, "shared", lend_to=[ENG])
    client.identity.tenant = ENG  # the borrower
    r = _narrow(client, f"{RESEARCH}:shared", ["opus"])
    assert r.status_code in (401, 403), r.text
    assert accounts.get(f"{RESEARCH}:shared").model_classes == ()


# --------------------------------------------------------------------------
# Assignment
# --------------------------------------------------------------------------


def test_an_account_listing_only_opus_is_never_assigned_a_fable_step(
    client, accounts, broker
):
    accounts.register(ENG, "opus-only")
    _narrow(client, f"{ENG}:opus-only", ["opus"])
    _task(broker.db, "task_fable", model_class="fable")

    r = _assign(client, "task_fable")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account_id"] is None
    # The pool is not how a Fable step of this tenant runs: the worker falls
    # back to the tenant's own key, and a keyless tenant's admission parks it.
    assert body["reason"] == "no_accounts_registered"
    assert body["model_class"] == "fable"
    assert broker.db.docs[f"accounts/{ENG}:opus-only"]["holds"] == []


def test_a_fable_step_goes_to_the_account_that_serves_fable(client, accounts, broker):
    # `a-opus` sorts first by label, so only the class filter can skip it.
    accounts.register(ENG, "a-opus")
    accounts.register(ENG, "b-every")
    _narrow(client, f"{ENG}:a-opus", ["opus"])
    _task(broker.db, "task_fable", model_class="fable")
    _task(broker.db, "task_default")

    assert _assign(client, "task_fable").json()["account_id"] == f"{ENG}:b-every"
    # No class on the task is the profile's default, `opus`: the opus-only
    # account serves it.
    assert _assign(client, "task_default").json()["account_id"] == f"{ENG}:a-opus"


def test_an_account_with_no_list_is_assigned_every_class(client, accounts, broker):
    accounts.register(ENG, "every")
    measure(accounts, f"{ENG}:every")  # one agent until measured (#2222)
    for cls in ("opus", "fable", "sonnet", "haiku"):
        _task(broker.db, f"task_{cls}", model_class=cls)
        assert _assign(client, f"task_{cls}").json()["account_id"] == f"{ENG}:every"


@pytest.mark.parametrize("which", ["no task id", "unknown task", "another tenant's task"])
def test_a_step_whose_class_cannot_be_read_never_gets_a_narrowed_account(
    client, accounts, broker, which
):
    """An older worker sends no task id; a task id is a claim. Neither may land a
    step of an unknown class on an account that serves only some classes. An
    account that serves every class still takes it, so nothing that worked
    before stops working."""
    accounts.register(ENG, "a-opus")
    _narrow(client, f"{ENG}:a-opus", ["opus"])
    _task(broker.db, "task_theirs", tenant_id=RESEARCH, model_class="opus")
    task_id = {"no task id": None, "unknown task": "task_nowhere",
               "another tenant's task": "task_theirs"}[which]

    assert _assign(client, task_id).json()["account_id"] is None

    accounts.register(ENG, "b-every")
    assert _assign(client, task_id).json()["account_id"] == f"{ENG}:b-every"


def test_a_swap_moves_a_fable_step_only_to_an_account_that_serves_fable(
    client, accounts, broker
):
    accounts.register(ENG, "a-every")
    accounts.register(ENG, "b-opus")
    _narrow(client, f"{ENG}:b-opus", ["opus"])
    _task(broker.db, "task_fable", model_class="fable")
    held = _assign(client, "task_fable").json()
    assert held["account_id"] == f"{ENG}:a-every"

    swap = {
        "account_id": held["account_id"],
        "assignment_id": held["assignment_id"],
        "task_id": "task_fable",
        "attempt_id": "att_task_fable",
        "reason": "exhausted",
    }
    r = client.post("/v1/accounts/swap", json=swap)

    assert r.status_code == 200, r.text
    assert r.json()["swapped"] is False
    assert broker.db.docs[f"accounts/{ENG}:b-opus"]["holds"] == []

    accounts.register(ENG, "c-every")
    moved = client.post("/v1/accounts/swap", json=swap).json()
    assert moved["swapped"] is True
    assert moved["account_id"] == f"{ENG}:c-every"


# --------------------------------------------------------------------------
# Admission: a class no account serves waits, holding nothing
# --------------------------------------------------------------------------


def _world(db, *, tenant_key: bool = False) -> None:
    seed_pool(db, "global", hard_limit=10)
    seed_tenant(db, ENG, credentials=("anthropic",) if tenant_key else ())


def _pools(db) -> dict[str, int]:
    return {p: d["active"] for p, d in db.docs.items() if p.startswith("pools/")}


def _leases(db) -> list[str]:
    return [p for p in db.docs if p.startswith("leases/")]


def _parked_events(db, task_id: str) -> list[dict]:
    prefix = f"tasks/{task_id}/events/"
    return [
        doc for path, doc in sorted(db.docs.items())
        if path.startswith(prefix) and doc.get("type") == "parked"
    ]


def _pool_settings():
    return scheduler_settings(quota_broker_url=BROKER_URL)


def test_a_step_whose_class_no_account_serves_parks_and_holds_nothing(
    db, make_scheduler, dispatcher
):
    _world(db)
    store = AccountStore(db)
    store.register(ENG, "opus-only")
    db.document(f"accounts/{ENG}:opus-only").update({"model_classes": ["opus"]})
    _task(db, "task_fable", model_class="fable")
    before = _pools(db)

    report = make_scheduler(settings=_pool_settings()).drain()

    doc = db.docs["tasks/task_fable"]
    assert (doc["state"], doc["park_reason"]) == ("PARKED", "CREDENTIAL_MISSING")
    assert report.leased == 0
    assert dispatcher.dispatched == []
    # Invariant 1 and 2: no lease, and every pool exactly as it was -- nothing
    # was reserved, so nothing was half-reserved either.
    assert _leases(db) == []
    assert _pools(db) == before
    detail = _parked_events(db, "task_fable")[-1]["detail"]
    assert detail["account_pool"] == "no_account_serves_class"
    assert detail["model_class"] == "fable"


def test_the_same_tenants_opus_step_runs_on_that_account(db, make_scheduler, dispatcher):
    _world(db)
    AccountStore(db).register(ENG, "opus-only")
    db.document(f"accounts/{ENG}:opus-only").update({"model_classes": ["opus"]})
    measure(AccountStore(db), f"{ENG}:opus-only")  # one agent until measured (#2222)
    _task(db, "task_default")
    _task(db, "task_opus", model_class="opus")

    make_scheduler(settings=_pool_settings()).drain()

    assert sorted(d["task_id"] for d in dispatcher.dispatched) == ["task_default", "task_opus"]
    # And the reservation is the usual one: both leased, the pool counted once each.
    assert db.docs["pools/global"]["active"] == 2


def test_an_account_with_no_list_admits_a_fable_step(db, make_scheduler, dispatcher):
    _world(db)
    AccountStore(db).register(ENG, "every")
    _task(db, "task_fable", model_class="fable")

    make_scheduler(settings=_pool_settings()).drain()

    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_fable"]


def test_a_tenant_with_its_own_key_runs_a_class_no_account_serves_on_the_key(
    db, make_scheduler, dispatcher
):
    """A metered key serves every class. The broker answers
    `no_accounts_registered` for the class, so the worker falls back to it."""
    _world(db, tenant_key=True)
    AccountStore(db).register(ENG, "opus-only")
    db.document(f"accounts/{ENG}:opus-only").update({"model_classes": ["opus"]})
    _task(db, "task_fable", model_class="fable")

    make_scheduler(settings=_pool_settings()).drain()

    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_fable"]


def test_the_parked_step_runs_once_an_admin_lets_an_account_serve_its_class(
    db, make_scheduler, dispatcher
):
    _world(db)
    AccountStore(db).register(ENG, "opus-only")
    db.document(f"accounts/{ENG}:opus-only").update({"model_classes": ["opus"]})
    _task(db, "task_fable", model_class="fable")
    scheduler = make_scheduler(settings=_pool_settings())

    scheduler.drain()
    assert db.docs["tasks/task_fable"]["state"] == "PARKED"
    assert scheduler.drain().promoted_credentials == 0

    db.document(f"accounts/{ENG}:opus-only").update({"model_classes": ["opus", "fable"]})
    second = scheduler.drain()

    assert second.promoted_credentials == 1
    scheduler.drain()
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_fable"]


def test_the_schedulers_decoder_reads_the_class_a_step_asked_for(db):
    """Without it every task the scheduler read resolved to its profile's
    default class, and the class check above saw `opus` for a Fable step."""
    from scheduler.codec import task_from_dict

    _task(db, "task_fable", model_class="fable")
    _task(db, "task_default")
    _task(db, "task_odd")
    db.docs["tasks/task_odd"]["model_class"] = ["fable"]

    assert task_from_dict(db.docs["tasks/task_fable"]).model_class == "fable"
    assert task_from_dict(db.docs["tasks/task_default"]).model_class is None
    assert task_from_dict(db.docs["tasks/task_odd"]).model_class is None
