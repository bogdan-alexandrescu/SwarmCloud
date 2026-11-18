"""swarm-api's own cron parser: every field form, DST in two zones, the Vixie OR.

docs/schedules.md §2.3 and §6.3, acceptance for lane S1 (§9): "The parser's
table test covers every field form, the DST gap and repeat in
`Europe/London` and `America/New_York`, and the Vixie OR."

Pure: no cloud, no emulator, no clock. Every instant is fixed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_api import cronexpr

UTC = timezone.utc


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def _sets(text: str) -> tuple[list[int], ...]:
    expr = cronexpr.parse(text)
    return tuple(sorted(s) for s in (expr.minutes, expr.hours, expr.days, expr.months, expr.weekdays))


ALL_DAYS = list(range(1, 32))
ALL_MONTHS = list(range(1, 13))
ALL_WEEK = list(range(0, 7))

# --------------------------------------------------------------------------
# Every field form, each with the sets it must produce
# --------------------------------------------------------------------------

FORMS = [
    # (expression, minutes, hours, days, months, weekdays)
    ("* * * * *", list(range(60)), list(range(24)), ALL_DAYS, ALL_MONTHS, ALL_WEEK),
    ("5 9 * * *", [5], [9], ALL_DAYS, ALL_MONTHS, ALL_WEEK),
    ("0 9-17 * * *", [0], list(range(9, 18)), ALL_DAYS, ALL_MONTHS, ALL_WEEK),
    ("*/15 * * * *", [0, 15, 30, 45], list(range(24)), ALL_DAYS, ALL_MONTHS, ALL_WEEK),
    ("0 8-18/5 * * *", [0], [8, 13, 18], ALL_DAYS, ALL_MONTHS, ALL_WEEK),
    ("10/20 * * * *", [10, 30, 50], list(range(24)), ALL_DAYS, ALL_MONTHS, ALL_WEEK),
    ("0,30 6,18 * * *", [0, 30], [6, 18], ALL_DAYS, ALL_MONTHS, ALL_WEEK),
    ("0 0 1,15-17 * *", [0], [0], [1, 15, 16, 17], ALL_MONTHS, ALL_WEEK),
    ("0 0 1 JAN,jul *", [0], [0], [1], [1, 7], ALL_WEEK),
    ("0 0 1 mar-MAY *", [0], [0], [1], [3, 4, 5], ALL_WEEK),
    ("0 9 * * MON-FRI", [0], [9], ALL_DAYS, ALL_MONTHS, [1, 2, 3, 4, 5]),
    ("0 9 * * sat,sun", [0], [9], ALL_DAYS, ALL_MONTHS, [0, 6]),
    ("0 9 * * 7", [0], [9], ALL_DAYS, ALL_MONTHS, [0]),
    ("0 9 * * 5-7", [0], [9], ALL_DAYS, ALL_MONTHS, [0, 5, 6]),
    ("0 9 * * */2", [0], [9], ALL_DAYS, ALL_MONTHS, [0, 2, 4, 6]),
    ("@hourly", [0], list(range(24)), ALL_DAYS, ALL_MONTHS, ALL_WEEK),
    ("@daily", [0], [0], ALL_DAYS, ALL_MONTHS, ALL_WEEK),
    ("@weekly", [0], [0], ALL_DAYS, ALL_MONTHS, [0]),
    ("@monthly", [0], [0], [1], ALL_MONTHS, ALL_WEEK),
    ("  0   9  *  *  1  ", [0], [9], ALL_DAYS, ALL_MONTHS, [1]),
]


@pytest.mark.parametrize("text, minutes, hours, days, months, weekdays", FORMS)
def test_every_field_form_parses_to_its_sets(text, minutes, hours, days, months, weekdays):
    assert _sets(text) == (minutes, hours, days, months, weekdays)


REFUSALS = [
    ("0 0 9 * * *", "seconds_field"),
    ("0 9 * *", "field_count"),
    ("", "empty"),
    ("@reboot", "unknown_alias"),
    ("@yearly", "unknown_alias"),
    ("0 0 L * *", "quartz_syntax"),
    ("0 0 15W * *", "quartz_syntax"),
    ("0 0 * * 5#3", "quartz_syntax"),
    ("0 0 ? * MON", "quartz_syntax"),
    ("0 0 * * FRI-MON", "reversed_range"),
    ("0 17-9 * * *", "reversed_range"),
    ("60 * * * *", "out_of_range"),
    ("0 24 * * *", "out_of_range"),
    ("0 0 0 * *", "out_of_range"),
    ("0 0 * 13 *", "out_of_range"),
    ("0 0 * * 8", "out_of_range"),
    ("*/0 * * * *", "bad_step"),
    ("*/61 * * * *", "bad_step"),
    ("0,,5 * * * *", "bad_field"),
    ("1-2-3 * * * *", "bad_field"),
    ("0 0 * * MONDAY", "bad_character"),
    ("0 0 31 2 *", "never_fires"),
    ("0 0 30,31 2 *", "never_fires"),
]


@pytest.mark.parametrize("text, code", REFUSALS)
def test_refused_forms_say_why(text, code):
    with pytest.raises(cronexpr.CronError) as caught:
        cronexpr.parse(text)
    assert caught.value.code == code


def test_quartz_letters_inside_names_are_not_mistaken_for_quartz():
    # WED holds a W and JUL an L; they are names, not Quartz's W and L.
    assert _sets("0 0 1 JUL WED")[3:] == ([7], [3])


def test_the_29th_of_february_is_valid_and_fires_in_leap_years_only():
    expr = cronexpr.parse("0 0 29 2 *")
    tz = cronexpr.zone("UTC")
    assert cronexpr.next_n(expr, _utc(2026, 10, 9), tz, 2) == [_utc(2028, 2, 29), _utc(2032, 2, 29)]


# --------------------------------------------------------------------------
# Next firings: strictly after, ordered, in UTC
# --------------------------------------------------------------------------


def test_next_after_is_strictly_after_and_a_late_tick_does_not_move_the_slot():
    expr = cronexpr.parse("0 9 * * *")
    tz = cronexpr.zone("UTC")
    assert cronexpr.next_after(expr, _utc(2026, 10, 9, 8, 59), tz) == _utc(2026, 10, 9, 9)
    # A tick exactly on the slot asks for the one after it.
    assert cronexpr.next_after(expr, _utc(2026, 10, 9, 9), tz) == _utc(2026, 10, 10, 9)
    # Seconds past the slot: still the next day's, never 09:01.
    assert cronexpr.next_after(expr, _utc(2026, 10, 9, 9, 0, 30), tz) == _utc(2026, 10, 10, 9)


def test_next_after_refuses_a_naive_instant():
    with pytest.raises(ValueError):
        cronexpr.next_after(cronexpr.parse("@daily"), datetime(2026, 10, 9), cronexpr.zone("UTC"))


# --------------------------------------------------------------------------
# Daylight saving: the gap fires once at its end, the repeat once at its first
# --------------------------------------------------------------------------

DST = [
    # zone, expression, after, the next three slots (UTC)
    # Europe/London springs forward 2026-03-29 01:00 GMT -> 02:00 BST: 01:30
    # does not exist, so it fires once at the first instant after the gap.
    (
        "Europe/London",
        "30 1 * * *",
        _utc(2026, 3, 28, 12),
        [_utc(2026, 3, 29, 1, 0), _utc(2026, 3, 30, 0, 30), _utc(2026, 3, 31, 0, 30)],
    ),
    # Every missing minute of the gap collapses into that one firing.
    (
        "Europe/London",
        "*/15 1 * * *",
        _utc(2026, 3, 29, 0, 0),
        [_utc(2026, 3, 29, 1, 0), _utc(2026, 3, 30, 0, 0), _utc(2026, 3, 30, 0, 15)],
    ),
    # Europe/London falls back 2026-10-25 02:00 BST -> 01:00 GMT: 01:30 happens
    # twice, and fires once, at its first (BST) occurrence.
    (
        "Europe/London",
        "30 1 * * *",
        _utc(2026, 10, 24, 12),
        [_utc(2026, 10, 25, 0, 30), _utc(2026, 10, 26, 1, 30), _utc(2026, 10, 27, 1, 30)],
    ),
    # Hourly across the repeat: 01:00 local fires once (00:00Z); the second
    # 01:00 (01:00Z) is not a slot; then 02:00 GMT.
    (
        "Europe/London",
        "0 * * * *",
        _utc(2026, 10, 24, 23, 30),
        [_utc(2026, 10, 25, 0, 0), _utc(2026, 10, 25, 2, 0), _utc(2026, 10, 25, 3, 0)],
    ),
    # America/New_York springs forward 2026-03-08 02:00 EST -> 03:00 EDT.
    (
        "America/New_York",
        "30 2 * * *",
        _utc(2026, 3, 7, 12),
        [_utc(2026, 3, 8, 7, 0), _utc(2026, 3, 9, 6, 30), _utc(2026, 3, 10, 6, 30)],
    ),
    # America/New_York falls back 2026-11-01 02:00 EDT -> 01:00 EST.
    (
        "America/New_York",
        "30 1 * * *",
        _utc(2026, 10, 31, 12),
        [_utc(2026, 11, 1, 5, 30), _utc(2026, 11, 2, 6, 30), _utc(2026, 11, 3, 6, 30)],
    ),
    # A slot outside the transition hour keeps its wall time on both sides.
    (
        "America/New_York",
        "0 9 * * *",
        _utc(2026, 10, 31, 12),
        [_utc(2026, 10, 31, 13, 0), _utc(2026, 11, 1, 14, 0), _utc(2026, 11, 2, 14, 0)],
    ),
]


@pytest.mark.parametrize("zone_name, text, after, expected", DST)
def test_daylight_saving_gap_and_repeat_each_fire_once(zone_name, text, after, expected):
    tz = cronexpr.zone(zone_name)
    slots = cronexpr.next_n(cronexpr.parse(text), after, tz, 3)
    assert slots == expected
    assert len(set(slots)) == len(slots)


def test_a_tick_inside_the_repeated_hour_does_not_fire_the_second_occurrence():
    # 01:10 GMT on 2026-10-25 is the SECOND 01:10 in London. 01:30's first
    # occurrence (00:30Z) has passed; the second (01:30Z) is not a slot.
    tz = cronexpr.zone("Europe/London")
    nxt = cronexpr.next_after(cronexpr.parse("30 1 * * *"), _utc(2026, 10, 25, 1, 10), tz)
    assert nxt == _utc(2026, 10, 26, 1, 30)


def test_the_resolved_gap_is_the_transition_instant_itself():
    tz = cronexpr.zone("Europe/London")
    instant = cronexpr.resolve_wall(datetime(2026, 3, 29, 1, 45), tz)
    assert instant == _utc(2026, 3, 29, 1, 0)
    assert instant.astimezone(tz).hour == 2 and instant.astimezone(tz).minute == 0


# --------------------------------------------------------------------------
# The Vixie OR
# --------------------------------------------------------------------------


def test_vixie_or_when_both_day_fields_are_restricted():
    # 2026-10-01 is a Thursday. The 1st OR a Monday.
    tz = cronexpr.zone("UTC")
    slots = cronexpr.next_n(cronexpr.parse("0 9 1 * MON"), _utc(2026, 9, 30), tz, 6)
    assert [s.date().isoformat() for s in slots] == [
        "2026-10-01", "2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26", "2026-11-01",
    ]


def test_a_star_led_day_field_ands_as_vixie_does():
    # `*/2` begins with `*`, so it is "unrestricted" to Vixie and ANDs: odd
    # days that are Mondays only. Control: without the day of week, every odd day.
    tz = cronexpr.zone("UTC")
    anded = cronexpr.next_n(cronexpr.parse("0 9 */2 * MON"), _utc(2026, 10, 1), tz, 3)
    assert [s.date().isoformat() for s in anded] == ["2026-10-05", "2026-10-19", "2026-11-09"]
    odd = cronexpr.next_n(cronexpr.parse("0 9 */2 * *"), _utc(2026, 10, 1), tz, 3)
    assert [s.date().isoformat() for s in odd] == ["2026-10-01", "2026-10-03", "2026-10-05"]


def test_one_restricted_day_field_alone_is_not_widened():
    tz = cronexpr.zone("UTC")
    mondays = cronexpr.next_n(cronexpr.parse("0 9 * * MON"), _utc(2026, 9, 30), tz, 2)
    assert [s.date().isoformat() for s in mondays] == ["2026-10-05", "2026-10-12"]


# --------------------------------------------------------------------------
# Words, preview and the minimum gap
# --------------------------------------------------------------------------

WORDS = [
    ("0 9 1 * MON", "09:00 on the 1st, or on Mondays"),
    ("0 9 * * 1-5", "09:00 on weekdays"),
    ("0 10 * * sat,sun", "10:00 on weekends"),
    ("0 9 * * MON,WED", "09:00 on Mondays and Wednesdays"),
    ("*/15 * * * *", "every 15 minutes"),
    ("* * * * *", "every minute"),
    ("@hourly", "every hour"),
    ("5 * * * *", "every hour at :05"),
    ("0 */2 * * *", "every 2 hours"),
    ("@daily", "00:00 every day"),
    ("@weekly", "00:00 on Sundays"),
    ("@monthly", "00:00 on the 1st"),
    ("0 9,17 * * *", "09:00 and 17:00 every day"),
    ("0,30 9-17 * * *", "every 30 minutes from 09:00 to 17:59"),
    ("0 0 1 JAN,JUL *", "00:00 on the 1st in January and July"),
    ("0 0 1-5 * *", "00:00 on the 1st to 5th"),
    ("0 9 */2 * MON", "09:00 on odd-numbered days that fall on Mondays"),
]


@pytest.mark.parametrize("text, expected", WORDS)
def test_words(text, expected):
    assert cronexpr.words(cronexpr.parse(text)) == expected


def test_preview_gives_words_the_next_five_in_the_zone_and_the_smallest_gap():
    answer = cronexpr.preview("0 9 * * 1-5", "Europe/London", _utc(2026, 10, 9, 12))
    assert answer["words"] == "09:00 on weekdays"
    assert answer["next"] == [
        "2026-10-12T09:00:00+01:00",
        "2026-10-13T09:00:00+01:00",
        "2026-10-14T09:00:00+01:00",
        "2026-10-15T09:00:00+01:00",
        "2026-10-16T09:00:00+01:00",
    ]
    # Weekday to weekday is a day; the DST change falls on a Sunday, inside a
    # Friday-to-Monday gap, so it does not shorten the smallest one.
    assert answer["min_gap_minutes"] == 24 * 60


def test_min_gap_catches_a_short_interval_whatever_the_spelling():
    tz = cronexpr.zone("UTC")
    now = _utc(2026, 10, 9)
    every_five = ",".join(str(m) for m in range(0, 60, 5))
    for text in ("*/5 * * * *", f"{every_five} * * * *", "0-55/5 * * * *"):
        assert cronexpr.min_gap(cronexpr.parse(text), now, tz) == timedelta(minutes=5)
    assert cronexpr.min_gap(cronexpr.parse("0,5 9 * * *"), now, tz) == timedelta(minutes=5)


@pytest.mark.parametrize("name", ["", "Mars/Olympus", "../etc/passwd", "/etc/localtime"])
def test_unknown_zones_are_refused(name):
    with pytest.raises(cronexpr.CronError) as caught:
        cronexpr.zone(name)
    assert caught.value.code == "unknown_timezone"
