"""A duplicate is decided by the provider's own identity, never by a resemblance (#3046).

On the night of 24 to 25 October 2026 (01:00-04:45Z) the pool withheld
eng:aieng-claude1 and eng:saga-team -- and eng:ai-eng-3 and eng:devops-main --
as `same_subscription_as: ai-eng-2` while ai-eng-2 read 97% of its week and 46
steps waited. They were distinct seats of one Team organisation, and the rule
then in force (POOL-DUP-DETECT, #2222) keyed a subscription on the
organisation alone. #2282 added the seat, but kept a fallback: two accounts on
one organisation whose seat is not recorded yet were paired by IDENTICAL
READINGS, and the newer was withheld. A usage reading is not an identity.

BGD, 2026-11-02:

  * a duplicate is decided only by an identity the provider reports for the
    subscription and that is stable -- the organisation AND the seat, both
    recorded, both equal -- never by a shared usage reading, a lent tenant or
    a similar label;
  * an account whose identity is unknown is NOT a duplicate, and a stored
    mark on one does not withhold it;
  * a withheld account carries the reason and the matching identity's KIND
    (never its value) in the park's `accounts` block and on Capacity >
    Accounts.

Unit tests: the fake Firestore. Every organisation id and login is built at
runtime.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from quota_broker.accounts import (
    DUPLICATE_MATCHED_ON,
    Account,
    WindowReading,
    account_id_for,
    choose,
    duplicates,
    identical_reading_pairs,
)
from quota_broker.accountstore import AccountStore
from quota_broker.main import account_to_api, acquire_hold
from quota_broker.oauth import Organisation, organisation_identity, seat_identity
from scheduler.loop import pool_blockers

ENG = "eng"
NOW = datetime(2026, 10, 25, 3, 50, tzinfo=timezone.utc)
FIVE_RESET = NOW + timedelta(hours=2, minutes=13)
WEEK_RESET = NOW + timedelta(days=2, hours=5)


def _uuid() -> str:
    return str(uuid.uuid4())


def _acct(label, *, org=None, seat=None, registered, week=0.40, five=0.05,
          withheld_mark=None) -> Account:
    return Account(
        account_id=account_id_for(ENG, label),
        owner_tenant=ENG,
        label=label,
        windows={"five_hour": WindowReading(five, FIVE_RESET),
                 "seven_day": WindowReading(week, WEEK_RESET)},
        observed_at=NOW,
        organisation_id=org,
        seat_id=seat,
        registered_at=registered,
        duplicate_of=withheld_mark,
        duplicate_withheld=withheld_mark is not None,
    )


def test_two_accounts_with_different_identities_and_one_spent_are_both_offered():
    team = organisation_identity(_uuid())
    spent = _acct("ai-eng-2", org=team, seat=seat_identity(_uuid()),
                  registered=NOW - timedelta(days=5), week=0.97, five=0.41)
    other = _acct("saga-team", org=team, seat=seat_identity(_uuid()),
                  registered=NOW - timedelta(days=1), week=0.61)
    assert duplicates([spent, other]) == {}
    picked = {choose([spent, other], ENG, NOW, assign_floor=0.0).label,
              choose([spent, other], ENG, NOW, assign_floor=0.0,
                     exclude=["eng:saga-team"]).label}
    assert picked == {"ai-eng-2", "saga-team"}


def test_two_accounts_with_the_same_identity_are_held_together():
    team, seat = organisation_identity(_uuid()), seat_identity(_uuid())
    older = _acct("older", org=team, seat=seat, registered=NOW - timedelta(days=3), week=0.97)
    newer = _acct("newer", org=team, seat=seat, registered=NOW - timedelta(hours=1), week=0.10)
    assert duplicates([older, newer]) == {
        "eng:older": ("eng:newer", False), "eng:newer": ("eng:older", True)}
    assert choose([older, newer], ENG, NOW, exclude=["eng:older"]) is None


def test_an_account_with_no_seat_reading_is_offered_even_with_identical_readings():
    """The fallback #2282 kept: same organisation, a seat unrecorded, the same
    figures -- withheld. A reading is not an identity, so it is offered."""
    team = organisation_identity(_uuid())
    kept = _acct("ai-eng-2", org=team, seat=seat_identity(_uuid()),
                 registered=NOW - timedelta(days=5))
    unknown = _acct("aieng-claude1", org=team, registered=NOW - timedelta(days=1))
    # MUTATION: pairing a seatless account by `readings_identical` withholds it.
    assert duplicates([kept, unknown]) == {}
    assert choose([kept, unknown], ENG, NOW, exclude=["eng:ai-eng-2"]).label == "aieng-claude1"
    # Neither seat recorded: the same.
    both = _acct("ai-eng-3", org=team, registered=NOW - timedelta(days=4))
    assert duplicates([both, unknown]) == {}
    # No organisation either.
    bare = _acct("devops-main", registered=NOW - timedelta(days=2))
    assert duplicates([bare, unknown]) == {}
    # It is still FLAGGED as possible -- shown, never withheld.
    assert identical_reading_pairs([kept, unknown])["eng:aieng-claude1"][0] is not None


def test_a_stored_mark_on_an_account_with_no_identity_does_not_withhold_it(broker):
    """Marks the readings fallback wrote, until the next sweep clears them."""
    team = organisation_identity(_uuid())
    kept = _acct("ai-eng-2", org=team, registered=NOW - timedelta(days=5), week=0.97)
    marked = _acct("saga-team", org=team, registered=NOW - timedelta(days=1),
                   withheld_mark="eng:ai-eng-2")
    # MUTATION: honouring every stored `duplicate_withheld` keeps saga-team out.
    assert choose([kept, marked], ENG, NOW, exclude=["eng:ai-eng-2"]).label == "saga-team"
    assert all("withheld" not in b for b in pool_blockers([kept, marked], NOW, ENG))
    assert account_to_api(marked, now=NOW)["can_take_new_agent"] is True

    # And the hold transaction, which reads the raw document, agrees.
    store = AccountStore(broker.db)
    store.register(ENG, "saga-team")
    store.record_reading("eng:saga-team", {
        "five_hour": WindowReading(0.05, FIVE_RESET)}, NOW)
    store.set_duplicate("eng:saga-team", "eng:ai-eng-2", withheld=True)
    assert acquire_hold(broker.db, "eng:saga-team", tenant_id=ENG, now=NOW) is not None


def test_a_stored_mark_the_listed_partner_no_longer_shares_does_not_withhold():
    team = organisation_identity(_uuid())
    kept = _acct("ai-eng-2", org=team, seat=seat_identity(_uuid()),
                 registered=NOW - timedelta(days=5), week=0.97)
    seat = _acct("saga-team", org=team, seat=seat_identity(_uuid()),
                 registered=NOW - timedelta(days=1), withheld_mark="eng:ai-eng-2")
    # MUTATION: the organisation-only mark, left until the sweep, withholds it.
    assert choose([kept, seat], ENG, NOW, exclude=["eng:ai-eng-2"]).label == "saga-team"


def test_a_stored_mark_on_a_known_identity_whose_partner_is_not_listed_still_withholds():
    """A lent account's pair may be outside the list a borrower's choose() reads."""
    team, seat = organisation_identity(_uuid()), seat_identity(_uuid())
    newer = _acct("newer", org=team, seat=seat, registered=NOW - timedelta(hours=1),
                  withheld_mark="eng:older")
    assert choose([newer], ENG, NOW) is None


def test_the_park_record_names_the_reason_and_the_identity_kind_never_its_value():
    team_id, login = _uuid(), _uuid()
    team, seat = organisation_identity(team_id), seat_identity(login)
    kept = _acct("ai-eng-2", org=team, seat=seat, registered=NOW - timedelta(days=5))
    again = _acct("again", org=team, seat=seat, registered=NOW - timedelta(hours=1))
    blockers = {b["account"]: b for b in pool_blockers([kept, again], NOW, ENG)}
    # MUTATION: the park's block without `reason` and `matched_on` says only whom.
    assert blockers["again"]["withheld"] == {
        "same_subscription_as": "ai-eng-2",
        "reason": "registered on the same organisation and seat as another account",
        "matched_on": DUPLICATE_MATCHED_ON,
    }
    assert DUPLICATE_MATCHED_ON == "organisation_and_seat"
    text = json.dumps(blockers)
    for value in (team, seat, team_id, login):
        assert value not in text


def test_the_listing_names_the_reason_and_the_identity_kind_never_its_value():
    team_id, login = _uuid(), _uuid()
    team, seat = organisation_identity(team_id), seat_identity(login)
    newer = _acct("newer", org=team, seat=seat, registered=NOW - timedelta(hours=1),
                  withheld_mark="eng:older")
    shown = account_to_api(newer, now=NOW)
    assert shown["duplicate_reason"] == "registered on the same organisation and seat as eng:older"
    assert shown["duplicate_matched_on"] == DUPLICATE_MATCHED_ON
    assert {"duplicate_of": "eng:older",
            "reason": "registered on the same organisation and seat as eng:older",
            "matched_on": DUPLICATE_MATCHED_ON} in shown["blocked_by"]
    text = json.dumps(shown)
    for value in (team, seat, team_id, login):
        assert value not in text


def test_an_add_with_no_seat_is_never_held_on_an_existing_seatless_account(broker):
    """The sweep after an add reaches the same answer as `duplicates`."""
    from quota_broker.main import mark_duplicates

    clock = [NOW - timedelta(days=5), NOW - timedelta(days=1)]
    store = AccountStore(broker.db, now=lambda: clock.pop(0) if clock else NOW)
    store.register(ENG, "ai-eng-2")
    store.register(ENG, "saga-team")
    shared = Organisation(identity=organisation_identity(_uuid()), name="Team", plan="Team")
    store.record_organisation("eng:ai-eng-2", shared)
    store.record_organisation("eng:saga-team", shared)
    for account_id in ("eng:ai-eng-2", "eng:saga-team"):
        store.record_reading(account_id, {
            "five_hour": WindowReading(0.05, FIVE_RESET),
            "seven_day": WindowReading(0.40, WEEK_RESET)}, NOW)
    # MUTATION: the readings fallback marks saga-team withheld here.
    assert mark_duplicates(store) == []
    assert {(a.duplicate_of, a.duplicate_withheld) for a in store.list()} == {(None, False)}
