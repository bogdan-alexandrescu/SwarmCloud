"""Handing an agent an account, and getting it back.

These two routes are the pool's hot path and the only ones a WORKER calls.
What they have to get right:

  * THE TENANT IS THE CALLER'S, NEVER THE BODY'S. `Account.may_serve` is how
    invariant 9 reaches the pool -- an account is usable by its owner and by
    the tenants that owner named, and by nobody else. A tenant taken from the
    request would make that a formality.
  * NOT FINDING AN ACCOUNT IS A 200. `accounts.choose` returns None when every
    account is spent, and the caller's correct response is to park -- free, and
    it resumes by itself. An error would cost the task one of three attempts
    for a condition nobody caused, and would be indistinguishable at the caller
    from the broker being down, which wants the opposite response.
  * A DEPLOYMENT WITH NO ACCOUNTS MUST BE UNCHANGED. "No account registered"
    and "no account free" are therefore different reasons, because the caller
    does different things: fall back to the per-tenant secret, or park.
  * A RELEASE PROVES IT HELD SOMETHING. `may_serve` says which accounts a
    caller could ever be assigned; it is not evidence that it holds one now.
    Releasing names the assignment id it was issued, so a tenant an account is
    lent to cannot drive the owner's counter to zero with calls it never
    earned -- and a duplicate release takes nobody else's slot.
  * AN ABANDONED ASSIGNMENT EXPIRES. A worker that is SIGKILLed never releases
    and `apps/reconciler/` has no account code, so the count is kept as holds
    with deadlines and the quota sweep prunes them. A bare counter could only
    ever drift upward, and it is what `choose()` spreads load on.
  * NO KEY MATERIAL CROSSES THE WIRE. The response names a secret; the worker
    reads it under its own service account.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quota_broker.accounts import (
    DEFAULT_HOLD_TTL,
    AccountState,
    WindowReading,
    holds_from_firestore,
)
from quota_broker.accountstore import AccountStore

PROJECT = "test-project"
ENG = "eng"
RESEARCH = "research"


class _Identity:
    """A verified worker ID token, as `WorkerIdentity.resolve` would return it.

    Mutable so one test can act as several callers, which is the only way to
    prove that the tenant comes from the identity: the same request body has to
    produce different accounts for different callers.
    """

    def __init__(self) -> None:
        self.tenant: str | None = ENG
        self.platform = False

    def resolve(self, authorization):  # noqa: ANN001 - the broker's duck type
        return (None, True) if self.platform else (self.tenant, False)

    def as_tenant(self, tenant_id: str) -> None:
        self.tenant, self.platform = tenant_id, False

    def as_platform(self) -> None:
        self.tenant, self.platform = None, True


@pytest.fixture()
def accounts(broker) -> AccountStore:
    return AccountStore(broker.db)


@pytest.fixture()
def client(broker, accounts):
    from fastapi.testclient import TestClient

    from quota_broker.main import create_app

    identity = _Identity()
    app = create_app(broker, identity=identity, account_store=accounts)
    c = TestClient(app, raise_server_exceptions=False)
    c.identity = identity
    return c


def _assign(client, provider: str = "anthropic", **body):
    return client.post("/v1/accounts/assign", json={"provider": provider, **body})


def _release(client, account_id: str, assignment_id: str, **body):
    return client.post(
        f"/v1/accounts/{account_id}/release",
        json={"assignment_id": assignment_id, **body},
    )


def _assign_and_release(client, account_id: str, **body):
    """One full cycle, as a worker performs it."""
    assignment_id = _assign(client).json()["assignment_id"]
    return _release(client, account_id, assignment_id, **body)


def _expire_hold(db, account_id: str, index: int) -> None:
    """Backdate one hold's deadline: a worker that was killed without releasing."""
    doc = db.docs[f"accounts/{account_id}"]
    holds = [dict(h) for h in doc["holds"]]
    holds[index]["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.document(f"accounts/{account_id}").update({"holds": holds})


def _observe(accounts: AccountStore, account_id: str, utilization: float, *,
             hours: int = 4, age_minutes: int = 0) -> None:
    """Record a rate-limit reading, the way a worker reports one."""
    now = datetime.now(timezone.utc)
    accounts.record_reading(
        account_id,
        {"five_hour": WindowReading(
            utilization=utilization, resets_at=now + timedelta(hours=hours))},
        observed_at=now - timedelta(minutes=age_minutes),
    )


# -- the tenant is the caller's -------------------------------------------


def test_the_tenant_comes_from_the_caller_and_not_from_the_body(client, accounts):
    """The same request, from two callers, yields two different accounts.

    This is invariant 9 at the pool. A body that could name a tenant would let
    any worker be handed any tenant's subscription credential.
    """
    accounts.register(ENG, "eng-one")
    accounts.register(RESEARCH, "research-one")

    client.identity.as_tenant(ENG)
    assert _assign(client).json()["account_id"] == f"{ENG}:eng-one"

    client.identity.as_tenant(RESEARCH)
    assert _assign(client).json()["account_id"] == f"{RESEARCH}:research-one"


def test_a_tenant_field_in_the_body_is_refused_outright(client, accounts):
    """Not ignored -- refused. A field the server silently drops is one a
    caller will keep sending, convinced it works."""
    accounts.register(ENG, "eng-one")
    r = client.post(
        "/v1/accounts/assign",
        json={"provider": "anthropic", "owner_tenant": RESEARCH},
    )
    assert r.status_code == 422


def test_an_account_another_tenant_owns_and_has_not_lent_is_invisible(client, accounts):
    accounts.register(RESEARCH, "research-one")
    client.identity.as_tenant(ENG)

    body = _assign(client).json()
    assert body["account_id"] is None
    assert body["reason"] == "no_accounts_registered"


def test_a_lent_account_may_be_assigned_to_the_borrower(client, accounts):
    """Isolation is the default; lending is a decision with a name on it."""
    accounts.register(RESEARCH, "shared", lend_to=[ENG])
    client.identity.as_tenant(ENG)

    assert _assign(client).json()["account_id"] == f"{RESEARCH}:shared"


def test_a_platform_caller_has_no_tenant_and_is_refused(client, accounts):
    """"Not configured" must mean refuse. The platform is not a tenant any
    account was lent to, so there is nothing `may_serve` could check -- and
    assigning anyway would hand an arbitrary tenant's credential to whatever
    ran the sweep tick."""
    accounts.register(ENG, "eng-one")
    client.identity.as_platform()

    r = _assign(client)
    assert r.status_code == 403
    assert r.json()["code"] == "forbidden"


# -- choosing -------------------------------------------------------------


def test_the_tenants_own_account_is_spent_before_a_borrowed_one(client, accounts):
    """A loan is a courtesy and should be spent last, even when the borrowed
    account has more room."""
    accounts.register(ENG, "mine")
    accounts.register(RESEARCH, "theirs", lend_to=[ENG])
    _observe(accounts, f"{ENG}:mine", 0.60)        # 40% left
    _observe(accounts, f"{RESEARCH}:theirs", 0.01)  # 99% left

    client.identity.as_tenant(ENG)
    assert _assign(client).json()["account_id"] == f"{ENG}:mine"


def test_the_account_with_the_most_headroom_wins(client, accounts):
    accounts.register(ENG, "tired")
    accounts.register(ENG, "fresh")
    _observe(accounts, f"{ENG}:tired", 0.80)
    _observe(accounts, f"{ENG}:fresh", 0.10)

    assert _assign(client).json()["account_id"] == f"{ENG}:fresh"


def test_equal_headroom_goes_to_whichever_has_fewest_agents_on_it(client, accounts):
    """Spread load rather than stack it: two agents on one subscription is the
    problem the pool exists to remove."""
    accounts.register(ENG, "aaa")
    accounts.register(ENG, "bbb")
    # `aaa` sorts first by label, so only the assignment count can move it.
    _assign(client)
    assert _assign(client).json()["account_id"] == f"{ENG}:bbb"


def test_a_paused_account_is_not_given_to_a_new_agent(client, accounts):
    """PAUSED means "no new work", not "get off".

    The reason is `pool_paused` and not `no_account_available`, because the two
    are different waits: one is a person, the other is a clock. Reporting a
    paused pool as "spent" sends whoever reads the park detail looking for a
    quota window that was never the problem.
    """
    accounts.register(ENG, "only")
    accounts.set_state(f"{ENG}:only", AccountState.PAUSED, "operator paused it")

    body = _assign(client).json()
    assert body["account_id"] is None
    assert body["reason"] == "pool_paused"
    assert body["next_reset_at"] is None


def test_an_account_for_another_provider_is_not_offered(client, accounts):
    accounts.register(ENG, "openai-one", provider="openai")
    client.identity.as_tenant(ENG)

    assert _assign(client, provider="anthropic").json()["reason"] == "no_accounts_registered"
    assert _assign(client, provider="openai").json()["account_id"] == f"{ENG}:openai-one"


def test_an_unknown_provider_is_refused_rather_than_silently_empty(client, accounts):
    """422 and not an empty answer: "no accounts for provider 'anthropc'" reads
    as an empty pool and sends an operator to register one."""
    accounts.register(ENG, "eng-one")
    r = _assign(client, provider="anthropc")
    assert r.status_code == 422
    assert r.json()["code"] == "validation_failed"


# -- nothing available is a 200 -------------------------------------------


def test_no_account_registered_is_two_hundred_with_a_reason(client):
    """This is the backwards-compatibility answer. Every deployment that has
    never registered an account gets it, and its worker keeps using the
    per-tenant secret exactly as before."""
    r = _assign(client)
    assert r.status_code == 200
    assert r.json()["account_id"] is None
    assert r.json()["reason"] == "no_accounts_registered"
    assert r.json()["secret"] is None


def test_every_account_spent_is_two_hundred_with_a_different_reason(client, accounts):
    """Different from "none registered" because the caller does something
    different: park and come back, rather than fall back to a tenant secret."""
    accounts.register(ENG, "spent")
    _observe(accounts, f"{ENG}:spent", 0.99)

    body = _assign(client).json()
    assert body["account_id"] is None
    assert body["reason"] == "no_account_available"


def test_a_spent_pool_says_when_it_clears_so_the_caller_need_not_poll(client, accounts):
    """`resetsAt` is reported by the provider, so a drained account can be
    scheduled back at a known instant instead of probed."""
    accounts.register(ENG, "spent")
    _observe(accounts, f"{ENG}:spent", 0.99, hours=3)

    body = _assign(client).json()
    assert body["next_reset_at"], "the caller has to be able to park until a known time"
    resets = datetime.fromisoformat(body["next_reset_at"])
    assert timedelta(hours=2) < resets - datetime.now(timezone.utc) < timedelta(hours=4)


def test_a_pool_waiting_on_a_person_names_no_reset_time(client, accounts):
    """A paused account is waiting on a human, not a clock. Inventing a retry
    time for it would wake the task up to the same answer."""
    accounts.register(ENG, "only")
    accounts.set_state(f"{ENG}:only", AccountState.PAUSED, "")

    assert _assign(client).json()["next_reset_at"] is None


def test_a_stale_reading_is_not_reported_as_an_exhausted_pool(client, accounts):
    """The band this whole distinction exists for.

    `choose()` rejects on `headroom()`, which HALVES a reading older than
    `stale_after`. An account at 0.80 utilisation -- 0.20 remaining, above the
    0.15 floor -- observed 45 minutes ago is halved to 0.10 and rejected. The
    route used to compute "when does it clear" from the RAW remaining with no
    staleness at all, find no blocking window, and answer `no_account_available`
    with `next_reset_at: null` -- which the worker reads as "waiting on a
    person" and parks on for a quarter of an hour, when in fact nothing says
    the account is spent and the broker's own usage poll refreshes the reading
    on its next sweep, minutes away.
    """
    accounts.register(ENG, "quiet")
    _observe(accounts, f"{ENG}:quiet", 0.80, hours=4, age_minutes=45)

    body = _assign(client).json()
    assert body["account_id"] is None, "the halved reading is below the floor"
    assert body["reason"] == "no_recent_reading"
    assert body["next_reset_at"] is None, "nothing is waiting on a window"


def test_a_fresh_reading_in_the_same_band_is_assigned(client, accounts):
    """The other half of the previous test: 0.20 remaining is ABOVE the floor,
    so an account observed just now is handed over. Only the age changed."""
    accounts.register(ENG, "quiet")
    _observe(accounts, f"{ENG}:quiet", 0.80, hours=4, age_minutes=0)

    assert _assign(client).json()["account_id"] == f"{ENG}:quiet"


# -- what an assignment returns -------------------------------------------


def test_the_response_names_the_secret_and_carries_no_key_material(client, accounts):
    """The NAME. The worker reads the value itself, under its own service
    account -- so this body is no more sensitive than the listing an operator
    already reads, and a log line carrying it leaks nothing."""
    accounts.register(ENG, "personal")
    body = _assign(client).json()

    assert body["secret"] == f"swarm-account-{ENG}--personal"
    blob = client.post("/v1/accounts/assign", json={"provider": "anthropic"}).text
    for forbidden in ("accessToken", "refreshToken", "access_token", "-refresh"):
        assert forbidden not in blob, forbidden


def test_the_assignment_carries_the_account_in_the_usual_shape(client, accounts):
    accounts.register(ENG, "personal")
    account = _assign(client).json()["account"]

    for column in ("account_id", "owner_tenant", "label", "state", "assigned",
                   "windows", "observed_at", "stale", "lend_to", "reason"):
        assert column in account
    assert account["stale"] is True, "nothing observed is not the same as zero"


def test_assigning_counts_the_agent_onto_the_account(client, accounts):
    accounts.register(ENG, "personal")

    assert _assign(client).json()["account"]["assigned"] == 1
    assert _assign(client).json()["account"]["assigned"] == 2
    assert accounts.get(f"{ENG}:personal").assigned == 2


# -- release --------------------------------------------------------------


def test_releasing_gives_the_slot_back(client, accounts):
    accounts.register(ENG, "personal")

    r = _assign_and_release(client, f"{ENG}:personal")
    assert r.status_code == 200
    assert r.json()["assigned"] == 0
    assert r.json()["reason"] == ""
    assert accounts.get(f"{ENG}:personal").assigned == 0


def test_releasing_twice_gives_back_one_slot_and_not_two(client, accounts):
    """It genuinely happens -- a worker releases on its exit path and the call
    is retried after a lost response. The second one must be a no-op rather
    than a decrement, or one agent's exit would give back another's slot."""
    accounts.register(ENG, "personal")
    first = _assign(client).json()["assignment_id"]
    second = _assign(client).json()["assignment_id"]
    assert accounts.get(f"{ENG}:personal").assigned == 2

    for _ in range(3):
        r = _release(client, f"{ENG}:personal", first)

    assert r.json()["reason"] == "not_held", "the repeats found nothing to give back"
    assert accounts.get(f"{ENG}:personal").assigned == 1, "the other agent is still on it"

    _release(client, f"{ENG}:personal", second)
    assert accounts.get(f"{ENG}:personal").assigned == 0


def test_a_release_must_name_the_assignment_it_is_giving_back(client, accounts):
    """`may_serve` says which accounts a caller COULD be assigned; it is not
    evidence that it holds one. Without the id this route decremented on
    nothing else, so any tenant an account was lent to could drive the owner's
    counter down as often as it liked."""
    accounts.register(ENG, "personal")
    _assign(client)

    r = client.post(f"/v1/accounts/{ENG}:personal/release", json={})
    assert r.status_code == 422
    assert accounts.get(f"{ENG}:personal").assigned == 1


def test_a_borrower_cannot_release_an_assignment_it_never_held(client, accounts):
    """The counter is what `choose()` spreads load on and what an operator
    reads as "agents on this account". A borrower that could drive it to zero
    could make an account with five live agents sort as idle and take a sixth.
    """
    accounts.register(RESEARCH, "shared", lend_to=[ENG])
    client.identity.as_tenant(RESEARCH)
    owners_assignment = _assign(client).json()["assignment_id"]

    # The borrower may reach the route -- it may_serve this account -- and
    # guesses the owner's assignment id.
    client.identity.as_tenant(ENG)
    r = _release(client, f"{RESEARCH}:shared", owners_assignment)

    assert r.status_code == 200
    assert r.json()["reason"] == "not_held"
    assert accounts.get(f"{RESEARCH}:shared").assigned == 1, "the owner still holds it"


def test_the_borrower_releases_a_borrowed_account_not_its_owner(client, accounts):
    """Authorised by `may_serve`, not by ownership. Requiring the owner would
    leave every borrowed assignment counted forever."""
    accounts.register(RESEARCH, "shared", lend_to=[ENG])
    client.identity.as_tenant(ENG)

    r = _assign_and_release(client, f"{RESEARCH}:shared")
    assert r.status_code == 200
    assert r.json()["reason"] == ""
    assert accounts.get(f"{RESEARCH}:shared").assigned == 0


def test_a_tenant_that_could_never_hold_the_account_may_not_release_it(client, accounts):
    """Refused at the route, before the assignment id is even looked at."""
    accounts.register(RESEARCH, "private")
    accounts.register(ENG, "mine")
    client.identity.as_tenant(ENG)
    assignment_id = _assign(client).json()["assignment_id"]

    r = _release(client, f"{RESEARCH}:private", assignment_id)
    assert r.status_code == 403
    assert r.json()["code"] == "forbidden"


def test_releasing_an_account_an_operator_removed_is_not_an_error(client, accounts):
    """An operator may remove an account while an agent is still on it. Making
    the worker's exit path fail for that would turn a deliberate act into a
    failed attempt."""
    accounts.register(ENG, "personal")
    assignment_id = _assign(client).json()["assignment_id"]
    accounts.remove(f"{ENG}:personal")

    r = _release(client, f"{ENG}:personal", assignment_id)
    assert r.status_code == 200
    assert r.json()["reason"] == "account_removed"


def test_the_count_is_moved_in_a_transaction(client, accounts, db):
    """`assigned` is what `choose()` uses to spread load and several processes
    move it at once; a lost update quietly pushes work onto the others."""
    accounts.register(ENG, "personal")
    before = db.transactions_committed

    _assign_and_release(client, f"{ENG}:personal")

    assert db.transactions_committed >= before + 2


# -- an abandoned assignment expires --------------------------------------


def test_an_assignment_carries_a_deadline(client, accounts, db):
    """The whole of the backstop, in one field. A worker that is SIGKILLed,
    OOM-killed or preempted never releases, and `apps/reconciler/` has no
    account code -- so without a deadline on the hold the count could only ever
    climb, and it is `choose()`'s load-spreading tiebreak."""
    accounts.register(ENG, "personal")
    _assign(client)

    holds = holds_from_firestore(db.docs[f"accounts/{ENG}:personal"]["holds"])
    assert len(holds) == 1
    assert holds[0].tenant_id == ENG
    ttl = holds[0].expires_at - datetime.now(timezone.utc)
    assert timedelta(hours=2) < ttl <= DEFAULT_HOLD_TTL, (
        "longer than the longest runner timeout, so it cannot expire under a "
        "live agent, and short enough that a killed worker is not forever"
    )


def test_the_sweep_reclaims_an_assignment_whose_worker_never_released_it(
    client, accounts, db
):
    """The claim the old comments made about the reconciler, made true here.

    The sweep runs on the same five-minute tick as everything else in this
    service, and it is the only thing in the repository that gives back a hold
    nobody released.
    """
    accounts.register(ENG, "personal")
    _assign(client)
    _assign(client)
    assert accounts.get(f"{ENG}:personal").assigned == 2

    # One worker is killed outright: its hold is still on the document and its
    # deadline has passed.
    _expire_hold(db, f"{ENG}:personal", 0)

    client.identity.as_platform()
    body = client.post("/v1/quota/sweep").json()

    assert body["holds"]["reclaimed"] == 1
    assert accounts.get(f"{ENG}:personal").assigned == 1, "the live agent is untouched"


def test_an_expired_hold_is_dropped_by_the_next_assignment_too(client, accounts, db):
    """Not only on the sweep. An account being assigned is an account whose
    document is already being written, so correcting the count there costs
    nothing and closes the window between a worker's death and the next tick."""
    accounts.register(ENG, "personal")
    _assign(client)
    _expire_hold(db, f"{ENG}:personal", 0)

    assert _assign(client).json()["account"]["assigned"] == 1, "not 2"


def test_the_sweep_says_so_when_a_registered_pool_has_never_been_reached(
    client, accounts, caplog
):
    """THE ALARM FOR THE DEFECT THAT HAS NO OTHER SYMPTOM.

    A worker with no QUOTA_BROKER_URL never calls. A worker whose service
    account is missing from this service's `run.invoker` list is rejected by
    Cloud Run before the application runs. Neither produces an error here,
    neither is logged, and the listing shows a healthy idle pool while every
    agent runs on the one shared per-tenant subscription.

    Only the broker can tell that apart from a deployment that simply has not
    adopted the pool, because only the broker knows both halves: accounts are
    registered, and nothing has ever asked for one.
    """
    accounts.register(ENG, "one")
    accounts.register(ENG, "two")
    client.identity.as_platform()

    with caplog.at_level("WARNING", logger="quota_broker.main"):
        body = client.post("/v1/quota/sweep").json()

    assert body["holds"] == {
        "pruned": 0, "reclaimed": 0, "registered": 2, "never_assigned": 2,
    }
    assert any("run.invoker" in r.getMessage() for r in caplog.records), (
        "the warning has to name what an operator must actually fix"
    )


def test_a_deployment_that_has_not_adopted_the_pool_is_not_warned_at(
    client, caplog
):
    """No accounts is not a fault. Warning about it would train everyone to
    ignore the one warning that matters."""
    client.identity.as_platform()

    with caplog.at_level("WARNING", logger="quota_broker.main"):
        body = client.post("/v1/quota/sweep").json()

    assert body["holds"]["registered"] == 0
    assert not any("run.invoker" in r.getMessage() for r in caplog.records)


def test_one_assignment_is_enough_to_stop_the_alarm(client, accounts, caplog):
    """It is "nothing has EVER been assigned", not "nothing is assigned now".
    An idle pool at 3am is normal; a pool nothing has ever reached is not."""
    accounts.register(ENG, "one")
    _assign_and_release(client, f"{ENG}:one")
    client.identity.as_platform()

    with caplog.at_level("WARNING", logger="quota_broker.main"):
        body = client.post("/v1/quota/sweep").json()

    assert body["holds"]["never_assigned"] == 0
    assert not any("run.invoker" in r.getMessage() for r in caplog.records)


# -- an account whose secret the worker cannot read -----------------------


def test_an_excluded_account_is_not_offered_again(client, accounts):
    """`choose()` is deterministic, so a worker handed an account it cannot
    read would be handed the same one on its next ask, and on the retry after
    that, until the task ran out of attempts."""
    accounts.register(ENG, "first")
    accounts.register(ENG, "second")

    first = _assign(client).json()["account_id"]
    second = _assign(client, exclude=[first]).json()["account_id"]

    assert second != first
    assert second in (f"{ENG}:first", f"{ENG}:second")


def test_excluding_everything_is_a_wait_and_not_a_fallback(client, accounts):
    """`no_accounts_registered` is the ONE answer that means "use your tenant
    secret". A tenant whose pool is broken must not quietly go back to sharing
    one subscription -- that is the contention the pool exists to remove."""
    accounts.register(ENG, "only")

    body = _assign(client, exclude=[f"{ENG}:only"]).json()
    assert body["reason"] == "no_account_available"


def test_an_unreadable_report_stops_the_next_agent_walking_into_it(client, accounts):
    """Across attempts, not just within one. `exclude` dies with the worker;
    this is what stops the NEXT task being handed the same wall."""
    accounts.register(ENG, "fresh")
    accounts.register(ENG, "working")

    first = _assign(client).json()
    _release(
        client,
        first["account_id"],
        first["assignment_id"],
        unusable="NotFound: no version of the secret yet",
    )

    assert _assign(client).json()["account_id"] != first["account_id"]


def test_one_tenants_unreadable_report_does_not_take_the_account_away_from_anyone_else(
    client, accounts
):
    """A borrower that was never granted secretAccessor on a lent secret has
    learned nothing about the OWNER. A global mark would be a griefing tool:
    one tenant could pause another's account by claiming it cannot read it."""
    accounts.register(RESEARCH, "shared", lend_to=[ENG])

    client.identity.as_tenant(ENG)
    borrowed = _assign(client).json()
    _release(
        client,
        borrowed["account_id"],
        borrowed["assignment_id"],
        unusable="PermissionDenied",
    )
    assert _assign(client).json()["reason"] == "no_account_available"

    client.identity.as_tenant(RESEARCH)
    assert _assign(client).json()["account_id"] == f"{RESEARCH}:shared"


def test_the_listing_shows_who_could_not_read_an_account(client, accounts):
    """An account nobody can read is not an account nobody wants, and on the
    listing the two looked identical: full headroom, zero agents, and every
    task for that tenant quietly failing."""
    accounts.register(ENG, "fresh")
    first = _assign(client).json()
    _release(client, first["account_id"], first["assignment_id"], unusable="NotFound")

    client.identity.as_platform()
    listed = client.get("/v1/accounts").json()["accounts"]
    assert [a["unreadable_by"] for a in listed if a["account_id"] == f"{ENG}:fresh"] == [
        [ENG]
    ]


# -- the existing routes are untouched ------------------------------------


def test_assign_did_not_shadow_the_account_id_routes(client, accounts):
    """`/v1/accounts/assign` and `/v1/accounts/{id}/...` differ in shape, and a
    literal segment that swallowed an id would be found here first."""
    accounts.register(ENG, "assign")     # an account whose LABEL is "assign"
    r = client.put(f"/v1/accounts/{ENG}:assign/lending", json={"lend_to": [RESEARCH]})
    assert r.status_code == 200
    assert r.json()["account"]["lend_to"] == [RESEARCH]
