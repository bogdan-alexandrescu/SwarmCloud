"""The account pool: isolation, choosing well, and keeping idle accounts alive.

The property under test throughout is the one the pool exists for: no agent
stops because its account ran out of quota, and no tenant reaches an account
nobody lent them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from quota_broker.accounts import (
    Account,
    AccountError,
    AccountState,
    WindowReading,
    account_id_for,
    choose,
    due_for_refresh,
    secret_name,
    validate_label,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _acct(label, owner="u-bogdan", *, state=AccountState.AVAILABLE, lend_to=(),
          five_hour=0.0, seven_day=0.0, observed=NOW, assigned=0, resets_in=3.0):
    windows = {}
    if five_hour is not None:
        windows["five_hour"] = WindowReading(five_hour, NOW + timedelta(hours=resets_in))
    if seven_day is not None:
        windows["seven_day"] = WindowReading(seven_day, NOW + timedelta(days=4))
    return Account(
        account_id=account_id_for(owner, label),
        owner_tenant=owner,
        label=label,
        state=state,
        lend_to=tuple(lend_to),
        windows=windows,
        observed_at=observed,
        assigned=assigned,
    )


# -- isolation -------------------------------------------------------------

def test_an_account_serves_only_its_owner_by_default():
    """CONTRACT.md invariant 9. A tenant reaching another tenant's account is
    the same failure as reaching their provider key, because it IS their
    provider key."""
    a = _acct("personal", owner="u-bogdan")
    assert a.may_serve("u-bogdan")
    assert not a.may_serve("eng")
    assert not a.may_serve("")


def test_lending_is_explicit_per_account_and_not_transitive():
    a = _acct("personal", owner="u-bogdan", lend_to=("eng",))
    assert a.may_serve("eng")
    # research was never named, and eng cannot pass the loan along.
    assert not a.may_serve("research")


def test_a_borrowed_account_is_spent_after_the_tenants_own():
    """A loan is a courtesy. Burning a lender's quota while your own sits idle
    is how lending stops being offered."""
    mine = _acct("mine", owner="eng", five_hour=0.5)
    borrowed = _acct("theirs", owner="u-bogdan", lend_to=("eng",), five_hour=0.0)
    # The borrowed one has MORE headroom and still must not win.
    assert choose([mine, borrowed], "eng", NOW).label == "mine"


def test_a_tenant_with_no_eligible_account_gets_none_not_someone_elses():
    a = _acct("personal", owner="u-bogdan")
    assert choose([a], "eng", NOW) is None


# -- choosing well ---------------------------------------------------------

def test_the_binding_window_decides_not_the_average():
    """5% weekly and 90% five-hour is 5% of headroom, not 47%. Averaging sends
    agents at an account that is about to refuse them."""
    a = _acct("a", five_hour=0.10, seven_day=0.95)
    assert a.headroom(NOW) == pytest.approx(0.05)


def test_an_account_past_its_reset_is_full_again_without_being_probed():
    """`resetsAt` is what makes a drained account returnable on a schedule.

    Observed at the later moment too, because a reading from two hours ago is
    separately stale -- and conflating "the window reset" with "we have not
    looked recently" would hide whichever of the two was actually true.
    """
    later = NOW + timedelta(hours=2)
    spent = _acct("spent", five_hour=1.0, seven_day=0.0, resets_in=1.0)
    assert spent.headroom(NOW) == pytest.approx(0.0)

    fresh = spent.with_reading(spent.windows, later)
    assert fresh.headroom(later) == pytest.approx(1.0)


def test_the_account_with_the_most_headroom_wins():
    low = _acct("low", five_hour=0.8)
    high = _acct("high", five_hour=0.1)
    mid = _acct("mid", five_hour=0.4)
    assert choose([low, high, mid], "u-bogdan", NOW).label == "high"


def test_load_is_spread_rather_than_stacked_when_headroom_ties():
    busy = _acct("busy", five_hour=0.2, assigned=4)
    idle = _acct("idle", five_hour=0.2, assigned=0)
    assert choose([busy, idle], "u-bogdan", NOW).label == "idle"


def test_a_nearly_spent_account_is_not_handed_to_a_NEW_agent():
    """An agent started at 2% headroom hits the wall almost immediately and has
    to be rescued by the swap machinery. Not starting it there is cheaper."""
    nearly = _acct("nearly", five_hour=0.95)
    assert choose([nearly], "u-bogdan", NOW) is None


def test_an_unobserved_account_is_assignable():
    """Otherwise onboarding is a chicken and egg: an account cannot report
    utilisation until something runs on it."""
    fresh = Account(account_id="u-bogdan:new", owner_tenant="u-bogdan", label="new")
    assert fresh.headroom(NOW) == pytest.approx(1.0)
    assert choose([fresh], "u-bogdan", NOW) is not None


def test_a_stale_reading_is_discounted_but_not_treated_as_exhausted():
    """These accounts are shared with a human at a laptop, so utilisation moves
    without the platform seeing it. Half-trust prefers a fresh reading without
    stalling a quiet pool."""
    stale = _acct("stale", five_hour=0.0, observed=NOW - timedelta(hours=2))
    assert stale.headroom(NOW) == pytest.approx(0.5)
    assert choose([stale], "u-bogdan", NOW) is not None


def test_choosing_is_deterministic():
    a, b = _acct("aaa", five_hour=0.2), _acct("bbb", five_hour=0.2)
    assert choose([a, b], "u-bogdan", NOW).label == choose([b, a], "u-bogdan", NOW).label


# -- state -----------------------------------------------------------------

@pytest.mark.parametrize("state", [
    AccountState.PAUSED, AccountState.DRAINING, AccountState.REAUTH_REQUIRED,
])
def test_only_an_available_account_takes_new_agents(state):
    assert choose([_acct("a", state=state)], "u-bogdan", NOW) is None


def test_a_newer_reading_wins_and_an_older_one_is_discarded():
    """Reports arrive from several pods at once and out of order. Letting a
    stale one land would make an exhausted account look available -- the wrong
    direction to be wrong in."""
    a = _acct("a", five_hour=0.9, observed=NOW)
    older = a.with_reading(
        {"five_hour": WindowReading(0.1, NOW + timedelta(hours=3))},
        NOW - timedelta(minutes=10),
    )
    assert older.headroom(NOW) == pytest.approx(a.headroom(NOW))

    newer = a.with_reading(
        {"five_hour": WindowReading(0.1, NOW + timedelta(hours=3))},
        NOW + timedelta(minutes=1),
    )
    assert newer.headroom(NOW) == pytest.approx(0.9)


# -- keeping idle accounts alive -------------------------------------------

def test_every_account_is_swept_including_ones_nobody_is_using():
    """THE reason "no human ever steps in" is true rather than aspirational.

    A refresh token that is never exchanged expires. A pool where two accounts
    are busy and three are idle is a pool where the idle three rot silently
    until the day they are needed.
    """
    busy = _acct("busy", assigned=3)
    idle = _acct("idle", assigned=0)
    never_used = Account(account_id="u-bogdan:new", owner_tenant="u-bogdan", label="new")
    due = due_for_refresh([busy, idle, never_used], NOW)
    assert {a.label for a in due} == {"busy", "idle", "new"}


def test_a_paused_or_draining_account_is_still_refreshed():
    """Paused means "no new work", not "let the credential die". An operator who
    pauses an account for a day must not find it needs a browser login after."""
    due = due_for_refresh(
        [_acct("p", state=AccountState.PAUSED), _acct("d", state=AccountState.DRAINING)],
        NOW,
    )
    assert {a.label for a in due} == {"p", "d"}


def test_a_dead_account_is_not_retried_forever():
    """REAUTH_REQUIRED means the refresh token is gone. Retrying cannot bring it
    back, and spending the endpoint's rate limit rediscovering that every five
    minutes makes one broken account a problem for the working ones."""
    due = due_for_refresh([_acct("dead", state=AccountState.REAUTH_REQUIRED)], NOW)
    assert due == []


def test_next_reset_reports_only_what_is_actually_blocking():
    """An account with room has nothing to wait for. Reporting its next window
    rollover as a "reset" would schedule a wake-up for an account that never
    stopped -- and would read, to whoever is looking, as though it had."""
    spent = _acct("spent", five_hour=1.0, seven_day=0.0, resets_in=2.0)
    assert spent.next_reset(NOW) == NOW + timedelta(hours=2)

    # Plenty of room, and its five-hour window still rolls over at some point.
    assert _acct("free", five_hour=0.0).next_reset(NOW) is None

    # Constrained on the WEEKLY, which is the one that must be reported -- the
    # five-hour rollover would be a false promise of capacity.
    weekly = _acct("weekly", five_hour=0.0, seven_day=1.0, resets_in=1.0)
    assert weekly.next_reset(NOW) == NOW + timedelta(days=4)


# -- names -----------------------------------------------------------------

def test_the_secret_name_is_derived_never_supplied():
    """A caller who could name the secret could name someone else's."""
    # Two dashes between tenant and label: a single one made
    # ("acme-prod","x") and ("acme","prod-x") the same secret.
    assert secret_name("u-bogdan", "personal") == "swarm-account-u-bogdan--personal"


@pytest.mark.parametrize("bad", [
    "", "Personal", "has space", "trailing-", "-leading", "under_score",
    "a" * 41, "../escape", "x/y",
])
def test_a_label_that_would_break_a_secret_or_an_annotation_is_refused(bad):
    """Refused at onboarding, where a person is watching, rather than at
    dispatch, where nobody is."""
    with pytest.raises(AccountError):
        validate_label(bad)


def test_an_account_with_no_owner_is_refused():
    with pytest.raises(AccountError):
        secret_name("", "personal")


def test_firestore_round_trip_preserves_everything_that_matters():
    a = _acct("personal", lend_to=("eng",), five_hour=0.3, seven_day=0.6, assigned=2)
    back = Account.from_firestore(a.to_firestore())
    assert back.owner_tenant == a.owner_tenant
    assert back.lend_to == a.lend_to
    assert back.assigned == a.assigned
    assert back.headroom(NOW) == pytest.approx(a.headroom(NOW))
    assert back.may_serve("eng")


def test_the_state_an_account_had_before_its_credential_died_survives_a_round_trip():
    """The record of where a recovered account belongs. A sweep marks
    REAUTH_REQUIRED and a person clears it, so without this the only state
    recovery could choose is AVAILABLE -- and an account an operator had
    deliberately paused would come back in service with nothing saying the
    pause had been overruled."""
    a = replace(_acct("personal", state=AccountState.REAUTH_REQUIRED),
                state_before_reauth=AccountState.PAUSED)

    back = Account.from_firestore(a.to_firestore())

    assert back.state is AccountState.REAUTH_REQUIRED
    assert back.state_before_reauth is AccountState.PAUSED


def test_a_document_with_no_remembered_state_reads_as_nothing_remembered():
    """Both a document written before the field existed and an account an
    operator typed REAUTH_REQUIRED onto. Recovery from either is AVAILABLE."""
    raw = _acct("personal").to_firestore()
    del raw["state_before_reauth"]
    assert Account.from_firestore(raw).state_before_reauth is None


def test_an_unrecognised_remembered_state_does_not_hide_the_account():
    """Deliberately more forgiving than `state`, which is allowed to raise and
    take the document out of `AccountStore.list` with it. This field only says
    where to put the account back; the account itself is fine, and dropping it
    from the pool over an advisory value would be the larger failure."""
    raw = _acct("personal").to_firestore()
    raw["state_before_reauth"] = "ASCENDED"
    assert Account.from_firestore(raw).state_before_reauth is None


# -- secret names must not collide across tenants --------------------------


def test_two_ordinary_registrations_cannot_share_a_secret():
    """CONTRACT.md invariant 9, broken with no attacker and no malice.

    With a single dash, `("acme-prod", "x")` and `("acme", "prod-x")` produced
    the identical secret name. Registering the second bound the second tenant's
    worker service account as an accessor on a secret holding the FIRST
    tenant's live refresh and access tokens.

    Reported in docs/audits/2026-09-18/06-quota-broker-accounts.md and left
    unfixed while account onboarding was a script an operator ran. It became
    urgent when registration moved into the Settings page, where the label is
    chosen by whoever is signed in.
    """
    from quota_broker.accounts import secret_name

    assert secret_name("acme-prod", "x") != secret_name("acme", "prod-x")


def test_a_tenant_id_that_could_forge_a_separator_is_refused():
    """The separator is only unambiguous while neither side can contain it."""
    from quota_broker.accounts import AccountError, secret_name

    with pytest.raises(AccountError, match="separates"):
        secret_name("acme--evil", "x")


def test_a_label_containing_the_separator_is_still_unambiguous():
    """A label MAY contain `--` and that is harmless, because the name splits
    at the FIRST separator and everything after it is the label by definition.
    Only the tenant side has to be constrained."""
    from quota_broker.accounts import secret_name

    assert secret_name("acme", "prod--x") == "swarm-account-acme--prod--x"

    # The tenant side is what must be constrained, and is: a tenant containing
    # the separator is refused outright, so no second reading of that name
    # exists.
    from quota_broker.accounts import AccountError

    with pytest.raises(AccountError, match="separates"):
        secret_name("acme--prod", "x")


def test_a_recorded_secret_name_survives_a_change_to_how_names_are_made():
    """The reason the name is stored rather than derived.

    When `secret_name` moved from one dash to two -- to stop
    ("acme-prod","x") and ("acme","prod-x") colliding -- every account already
    registered began deriving a name that had never existed. Nothing said so:
    the refresh sweep reports "no_refresh_credential" for a missing refresh
    secret, which is also exactly what a healthy API-key tenant reports, and
    the usage poller simply skips. Three live accounts were orphaned that way
    and the platform looked entirely healthy.
    """
    from quota_broker.accounts import Account

    recorded = Account(
        account_id="u-bogdan:personal",
        owner_tenant="u-bogdan",
        label="personal",
        secret_ref="swarm-account-u-bogdan-personal",   # the pre-change name
    )
    assert recorded.secret == "swarm-account-u-bogdan-personal"
    assert recorded.secret != secret_name("u-bogdan", "personal")


def test_a_document_written_before_the_field_existed_still_resolves():
    """Backwards compatibility for every account registered before this.
    Empty means derive, which is the only thing such a document can do."""
    from quota_broker.accounts import Account

    legacy = Account.from_firestore(
        {"account_id": "t:l", "owner_tenant": "t", "label": "l"}
    )
    assert legacy.secret == secret_name("t", "l")


def test_the_recorded_name_survives_a_firestore_round_trip():
    """It is only useful if it persists; an in-memory-only field would be lost
    the first time the document was read back."""
    from quota_broker.accounts import Account

    original = Account(
        account_id="t:l", owner_tenant="t", label="l", secret_ref="legacy-name"
    )
    assert Account.from_firestore(original.to_firestore()).secret == "legacy-name"
