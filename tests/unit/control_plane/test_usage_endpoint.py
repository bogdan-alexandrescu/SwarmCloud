"""Parsing the /api/oauth/usage response.

The fixture is a REAL captured 200 from the endpoint, not an invented shape.
That matters more here than usual: the endpoint is undocumented, and the whole
reason `Account.windows` went unpopulated for the life of the platform is that
nobody had the contract. A test written against a guess would have encoded the
guess.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quota_broker.usage import UsageUnavailable, parse

FIXTURE = json.loads((Path(__file__).parent / "usage_response.json").read_text())


def test_the_two_windows_that_matter_parse_from_a_real_response():
    windows = parse(FIXTURE)
    assert "five_hour" in windows
    assert "seven_day" in windows


def test_utilization_is_converted_from_percent_to_a_fraction():
    """The endpoint reports 0-100; WindowReading is documented 0.0-1.0.

    Getting this backwards would report a 34%-used account as 3400% used, or a
    full one as empty -- and the assign floor would then either starve the pool
    or overfill it. The fixture says 34.0 and 29.0.
    """
    windows = parse(FIXTURE)
    assert windows["five_hour"].utilization == pytest.approx(0.34)
    assert windows["seven_day"].utilization == pytest.approx(0.29)
    assert windows["five_hour"].remaining() == pytest.approx(0.66)


def test_reset_times_survive_with_their_timezone():
    windows = parse(FIXTURE)
    for key in ("five_hour", "seven_day"):
        assert windows[key].resets_at.tzinfo is not None, f"{key} lost its timezone"


def test_null_windows_are_omitted_not_recorded_as_zero():
    """The response is mostly nulls. A null window is NOT an unused window.

    Recording 0.0 for `seven_day_opus: null` would report unlimited headroom on
    a window nobody can see, which is the wrong direction to be wrong in.
    """
    windows = parse(FIXTURE)
    nulls = [k for k, v in FIXTURE.items() if v is None]
    assert nulls, "the fixture no longer exercises the null case"
    for key in nulls:
        assert key not in windows


def test_a_window_with_no_reset_time_is_skipped():
    """A real case in the fixture: `nimbus_quill` reports 0.0 with resets_at null.

    A reading without a reset time cannot answer "is this window clear yet",
    which is half of what a reading is for, so it is not recorded. Asserted
    against the fixture rather than a synthetic case because this is what the
    endpoint actually sends.
    """
    no_reset = [
        k for k, v in FIXTURE.items()
        if isinstance(v, dict) and v.get("utilization") is not None and v.get("resets_at") is None
    ]
    assert no_reset, "the fixture no longer exercises a window with no reset time"
    windows = parse(FIXTURE)
    for key in no_reset:
        assert key not in windows


def test_a_window_name_nobody_has_seen_is_kept_if_it_is_usable():
    """The endpoint invents window names -- seven_day_opus, nimbus_quill.

    `Account.windows` is a keyed dict precisely so a new one needs no code
    change, and filtering against a list of known names would throw that away.
    Synthetic, because every unknown window in the current fixture happens to be
    null or reset-less.
    """
    windows = parse({
        "juniper_tide": {"utilization": 12.0, "resets_at": "2026-01-01T00:00:00+00:00"},
    })
    assert "juniper_tide" in windows
    assert windows["juniper_tide"].utilization == pytest.approx(0.12)


def test_a_malformed_window_is_skipped_rather_than_guessed():
    assert parse({"five_hour": {"utilization": "thirty", "resets_at": "2026-01-01T00:00:00+00:00"}}) == {}
    assert parse({"five_hour": {"utilization": 34.0}}) == {}
    assert parse({"five_hour": {"utilization": 34.0, "resets_at": "not a date"}}) == {}
    assert parse({"five_hour": None}) == {}


def test_a_boolean_is_not_a_utilization():
    """bool subclasses int; `utilization: true` must not become 100% used."""
    assert parse({"five_hour": {"utilization": True, "resets_at": "2026-01-01T00:00:00+00:00"}}) == {}


def test_utilization_is_clamped_into_range():
    """An overage can report above 100. A utilisation above 1.0 makes
    `remaining()` negative and the assign floor behave unpredictably."""
    w = parse({"x": {"utilization": 140.0, "resets_at": "2026-01-01T00:00:00+00:00"}})
    assert w["x"].utilization == 1.0
    assert w["x"].remaining() == 0.0
