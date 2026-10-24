"""Each window is held to its own floor before a NEW agent is handed an account (#2222).

BGD, 2026-10-24: the 5-hour window keeps the 15% floor; the 7-day window's is
5%. Measured that morning: six of seven accounts had 2-14% of their week left
(ai-eng-2 and devops-team 14%, devops-main 12%), so one 15% floor refused all
six for one to four days and 28 agents waited on aieng-claude1's 5-hour reset.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quota_broker.accounts import (
    DEFAULT_ASSIGN_FLOOR,
    WINDOW_FLOORS,
    Account,
    AccountState,
    Unavailable,
    WindowReading,
    account_id_for,
    choose,
    eligibility,
    window_floor,
)

NOW = datetime(2026, 10, 24, 9, 4, tzinfo=timezone.utc)


def _acct(label, *, five_hour=0.0, seven_day=0.0, observed=NOW, five_resets_in=timedelta(hours=3),
          week_resets_in=timedelta(days=2)):
    windows = {}
    if five_hour is not None:
        windows["five_hour"] = WindowReading(five_hour, NOW + five_resets_in)
    if seven_day is not None:
        windows["seven_day"] = WindowReading(seven_day, NOW + week_resets_in)
    return Account(
        account_id=account_id_for("eng", label),
        owner_tenant="eng",
        label=label,
        state=AccountState.AVAILABLE,
        lend_to=(),
        windows=windows,
        observed_at=observed,
        assigned=0,
    )


def test_the_floors_are_bgds():
    # MUTATION: a 7-day floor back at 0.15 fails here and in every test below.
    assert WINDOW_FLOORS == {"seven_day": 0.05}
    assert window_floor("seven_day") == 0.05
    assert window_floor("five_hour") == DEFAULT_ASSIGN_FLOOR == 0.15
    # A window the map does not name keeps the default floor.
    assert window_floor("harbor_lantern") == 0.15


def test_an_account_with_14_percent_of_its_week_left_takes_a_new_agent():
    # devops-team as read at 09:04Z: its 5-hour window has reset, its week is 86% used.
    devops_team = _acct("devops-team", five_hour=0.40, seven_day=0.86,
                        five_resets_in=-timedelta(hours=4, minutes=44))
    assert devops_team.assignable(NOW)
    assert devops_team.next_reset(NOW) is None
    assert choose([devops_team], "eng", NOW) is devops_team


def test_an_account_under_5_percent_of_its_week_is_refused_until_its_week_resets():
    saga_personal = _acct("saga-personal", five_hour=0.11, seven_day=0.98,
                          five_resets_in=-timedelta(hours=3), week_resets_in=timedelta(hours=47))
    assert not saga_personal.assignable(NOW)
    assert set(saga_personal.blocking_windows(NOW)) == {"seven_day"}
    assert saga_personal.next_reset(NOW) == NOW + timedelta(hours=47)
    assert choose([saga_personal], "eng", NOW) is None


def test_the_five_hour_window_still_needs_15_percent():
    claude1 = _acct("aieng-claude1", five_hour=0.90, seven_day=0.39, five_resets_in=timedelta(hours=1))
    assert set(claude1.blocking_windows(NOW)) == {"five_hour"}
    assert choose([claude1], "eng", NOW) is None
    assert claude1.next_reset(NOW) == NOW + timedelta(hours=1)


def test_an_aged_reading_still_counts_at_half_on_the_week():
    # 8% left: assignable while fresh, under the 5% floor once halved.
    fresh = _acct("fresh", seven_day=0.92)
    aged = _acct("aged", seven_day=0.92, observed=NOW - timedelta(hours=2))
    assert fresh.assignable(NOW)
    assert not aged.assignable(NOW)


def test_a_caller_naming_one_floor_still_gets_one_floor_for_every_window():
    devops_team = _acct("devops-team", seven_day=0.86)
    assert not devops_team.assignable(NOW, floor=0.15)
    assert choose([devops_team], "eng", NOW, assign_floor=0.15) is None


def test_the_pool_as_read_on_the_morning_of_2026_10_24_has_three_accounts_to_offer():
    """Before #2222 every one of these was refused and the answer was aieng-claude1's 10:10Z."""
    past = -timedelta(hours=1)
    pool = [
        _acct("ai-eng-2", five_hour=0.04, seven_day=0.86, five_resets_in=past),
        _acct("ai-eng-3", five_hour=0.48, seven_day=0.97, five_resets_in=past),
        _acct("aieng-claude1", five_hour=0.99, seven_day=0.39, five_resets_in=timedelta(minutes=66)),
        _acct("devops-main", five_hour=0.21, seven_day=0.88, five_resets_in=past),
        _acct("devops-team", five_hour=0.40, seven_day=0.86, five_resets_in=past),
        _acct("saga-personal", five_hour=0.11, seven_day=0.98, five_resets_in=past),
    ]
    offered = {a.label for a in pool if a.assignable(NOW)}
    assert offered == {"ai-eng-2", "devops-main", "devops-team"}
    chosen = choose(pool, "eng", NOW)
    assert chosen is not None and chosen.label in {"ai-eng-2", "devops-team"}


def test_no_account_available_still_names_the_first_window_to_clear():
    week_a = _acct("a", seven_day=0.98, week_resets_in=timedelta(hours=34))
    hour_b = _acct("b", five_hour=0.99, seven_day=0.39, five_resets_in=timedelta(minutes=66))
    reason, when = eligibility([week_a, hour_b], "eng", NOW)
    assert reason is Unavailable.NO_ACCOUNT_AVAILABLE
    assert when == NOW + timedelta(minutes=66)
