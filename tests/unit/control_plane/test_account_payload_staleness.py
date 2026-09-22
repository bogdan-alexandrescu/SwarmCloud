"""`stale` means two different things, on purpose, and nothing said so.

WHY THIS FILE EXISTS. Asked to check whether account quotas are accurate in the
UI, I found `account_to_api` (quota_broker/main.py) spelling the staleness rule
out by hand instead of calling `Account.is_stale`, and the two answering the
never-observed case DIFFERENTLY:

    account_to_api      observed_at is None  ->  stale = True
    Account.is_stale    observed_at is None  ->  False

That reads exactly like the restatement-drift CLAUDE.md warns about, so I
changed the payload to ask the method. `make test` refused it: two existing
tests assert the payload's answer, with their reasons written beside them --

    test_account_routes.py::test_an_account_with_no_reading_is_stale_not_zero
        '"Nothing has been observed" and "utilisation is zero" are different
         claims. `cs status` marks a projected figure with `~` for this reason;
         a number shown without that mark asserts it is current.'

    test_account_assign.py::test_the_assignment_carries_the_account_in_the_usual_shape
        assert account["stale"] is True, "nothing observed is not the same as zero"

They are right, and the change was wrong. The two are not one rule copied
badly; they are two questions that happen to share a word:

  * `Account.is_stale` asks A SCHEDULING QUESTION -- "should the broker
    discount this reading when it decides where to send work?" A never-observed
    account is NOT discounted, because `headroom()` deliberately treats it as
    FULL so a new account can be assigned before it has ever reported. Marking
    it stale there would halve the headroom of every account on its first day.

  * the payload's `stale` asks A PRESENTATION QUESTION -- "may a client show
    these figures as current?" A never-observed account has no current figures
    at all, so the honest answer is no.

So the disagreement is correct and the DOCUMENTATION of it is what was missing.
This file is that documentation, executable, sitting where the next person to
notice the difference will find it before they "fix" it as I tried to.

WHAT IS NOT PINNED HERE. Whether the two should keep sharing a name. They
should probably not, and that is in the report rather than in a rename: the
field is on a payload the web UI, `sc` and the MCP bridge all read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quota_broker.accounts import (
    DEFAULT_ASSIGN_FLOOR,
    DEFAULT_STALE_AFTER,
    Account,
    WindowReading,
    account_id_for,
)
from quota_broker.main import account_to_api

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def _acct(*, observed: datetime | None, utilisation: float = 0.4) -> Account:
    return Account(
        account_id=account_id_for("u-bogdan", "primary"),
        owner_tenant="u-bogdan",
        label="primary",
        windows={"five_hour": WindowReading(utilisation, NOW + timedelta(hours=2))}
        if observed is not None
        else {},
        observed_at=observed,
    )


# --------------------------------------------------------------------------
# The two meanings, pinned against each other
# --------------------------------------------------------------------------

def test_a_never_observed_account_is_stale_to_a_reader_and_not_to_the_scheduler() -> None:
    """THE DISAGREEMENT, asserted from both sides in one test.

    Written as one test rather than two so that neither side can be changed to
    agree with the other without this failing and the docstring above being
    read.
    """
    account = _acct(observed=None)

    # Presentation: there is nothing current to show, so nothing may be shown
    # as current.
    assert account_to_api(account, now=NOW)["stale"] is True
    assert account_to_api(account, now=NOW)["observed_at"] is None
    assert account_to_api(account, now=NOW)["windows"] == {}

    # Scheduling: a new account must be assignable before it can report, so it
    # is not discounted and its headroom is full.
    assert account.is_stale(NOW) is False
    assert account.headroom(NOW) == 1.0


def test_once_a_reading_exists_the_two_agree_at_every_age() -> None:
    """The disagreement is confined to the never-observed case, and that matters.

    If it were wider, the payload really would be a drifting copy. Sweeping the
    boundary rather than testing one side of it: the rule is
    `> DEFAULT_STALE_AFTER`, so the instant exactly at the threshold is not
    stale, and an off-by-one in either spelling shows up here.
    """
    for age in (
        timedelta(0),
        DEFAULT_STALE_AFTER - timedelta(seconds=1),
        DEFAULT_STALE_AFTER,
        DEFAULT_STALE_AFTER + timedelta(seconds=1),
        DEFAULT_STALE_AFTER * 4,
    ):
        account = _acct(observed=NOW - age)
        assert account_to_api(account, now=NOW)["stale"] is account.is_stale(NOW), (
            f"the payload and Account.is_stale disagree at age {age}, which is "
            "outside the one case they are documented to differ on"
        )


def test_a_genuinely_old_reading_is_reported_stale() -> None:
    """The control. `stale` has to be able to be true for a REASON, or the
    never-observed case above is the only thing that ever sets it."""
    payload = account_to_api(
        _acct(observed=NOW - DEFAULT_STALE_AFTER - timedelta(minutes=1)), now=NOW
    )

    assert payload["stale"] is True
    assert payload["observed_at"] is not None


# --------------------------------------------------------------------------
# The gap that IS a reporting defect: the figure served and the figure used
# --------------------------------------------------------------------------

def test_a_stale_reading_is_worth_half_to_the_scheduler_and_the_payload_omits_that() -> None:
    """THE ACCURACY GAP, pinned as it stands rather than papered over.

    `headroom()` HALVES the binding remaining for a stale reading -- "too old to
    trust, but not evidence of exhaustion either" -- and `choose()` admits on
    `headroom >= DEFAULT_ASSIGN_FLOOR`. The payload carries `utilization` and
    `stale` and does NOT carry headroom, so a client showing the utilisation it
    was given cannot say that the platform is acting on half of what is left.

    The worked consequence, which is in the report: with the floor at 0.15, a
    FRESH account is refused above 85% utilisation and a STALE one above 70%.
    Between those two figures the screen shows the same number for an account
    that will be used and one that will be refused, and nothing distinguishes
    them but the `~`.

    This test does not assert the gap is acceptable. It asserts it EXISTS, so
    that adding a served headroom field is a change that breaks a test and gets
    the UI updated with it, rather than a change nobody notices.
    """
    utilisation = 0.75
    fresh = _acct(observed=NOW, utilisation=utilisation)
    stale = _acct(
        observed=NOW - DEFAULT_STALE_AFTER - timedelta(minutes=1),
        utilisation=utilisation,
    )

    # Same figure on the wire, for both.
    assert (
        account_to_api(fresh, now=NOW)["windows"]["five_hour"]["utilization"]
        == account_to_api(stale, now=NOW)["windows"]["five_hour"]["utilization"]
    )

    # Different answers from the thing that decides.
    assert fresh.headroom(NOW) >= DEFAULT_ASSIGN_FLOOR
    assert stale.headroom(NOW) < DEFAULT_ASSIGN_FLOOR

    # And headroom is not on the payload at all, so no client can tell.
    assert "headroom" not in account_to_api(stale, now=NOW)


def test_utilisation_is_served_as_a_fraction_not_a_percentage() -> None:
    """The other half of "is the figure on screen the real one".

    `usage.parse` divides the endpoint's 0-100 by 100 and `WindowReading` holds
    0.0-1.0; `readingOf` in apps/swarm-ui/src/types.ts multiplies by 100 once,
    and its own comment says that multiplication is this file's job "precisely
    once, so no component can forget". A payload carrying a percentage would
    render 40% as 4000%; a client that did not scale would render it as 0%.
    """
    payload = account_to_api(_acct(observed=NOW, utilisation=0.4), now=NOW)

    assert payload["windows"]["five_hour"]["utilization"] == 0.4
