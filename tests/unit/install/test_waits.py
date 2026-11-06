"""Bounded waits with a countdown (docs/design/day-one-install.md §2.2; DAYONE-ENGINE-1).

On a fake clock: a wait past its bound gives up with exit 4 and says what to
look at, never looping for ever; every poll's countdown event carries what is
usually expected and the hard deadline.
"""

from __future__ import annotations

import io

import pytest

from swarm_install.events import Events
from swarm_install.exitcodes import MET, UNMET, ExitCode
from swarm_install.waits import IAM_PROPAGATION, WaitGaveUp, WaitSpec, wait_until

from .test_state_store import FakeClock


def _events(clock: FakeClock) -> Events:
    return Events(clock.now, json_lines=True, out=io.StringIO())


def test_wait_gives_up_at_bound_with_exit_4() -> None:
    clock = FakeClock()
    events = _events(clock)
    polls: list[float] = []

    def never() -> str:
        polls.append(clock.now())
        return UNMET

    start = clock.now()
    with pytest.raises(WaitGaveUp) as raised:
        wait_until("iam.propagation", IAM_PROPAGATION, never, clock=clock, events=events)

    assert raised.value.exit_code == ExitCode.PAUSED == 4
    assert clock.now() - start == IAM_PROPAGATION.bound_s, "it gives up AT the bound, not past it"
    assert len(polls) == IAM_PROPAGATION.bound_s / IAM_PROPAGATION.interval_s + 1
    gave_up = events.of_kind("wait_gave_up")
    assert len(gave_up) == 1
    assert gave_up[0]["look_at"] == IAM_PROPAGATION.look_at_for(None)
    assert "swarm install" in str(raised.value), "it says how to resume"


def test_wait_returns_when_met() -> None:
    clock = FakeClock()
    answers = iter([UNMET, UNMET, MET])
    wait_until("dns", IAM_PROPAGATION, lambda: next(answers), clock=clock, events=_events(clock))
    assert clock.sleeps == [10.0, 10.0]


def test_countdown_event_carries_expected_and_deadline() -> None:
    clock = FakeClock()
    events = _events(clock)
    spec = WaitSpec(
        reason="the managed certificate", expected="up to 60 min",
        expected_s=1200, bound_s=3600, look_at="DNS", interval_s=30,
    )
    start = clock.now()
    answers = iter([UNMET, UNMET, MET])
    wait_until("edge.certificate", spec, lambda: next(answers), clock=clock, events=events)

    countdown = events.of_kind("wait_countdown")
    assert len(countdown) == 2
    first, second = countdown
    for event in countdown:
        assert event["step"] == "edge.certificate"
        assert event["reason"] == "the managed certificate"
        assert event["expected"] == "up to 60 min"
        assert event["expected_end"] == start + 1200
        assert event["deadline"] == start + 3600
    assert first["remaining_s"] == 3600
    assert second["remaining_s"] == 3570


def test_bound_is_required_to_hold_the_interval() -> None:
    with pytest.raises(ValueError):
        WaitSpec(reason="x", expected="x", expected_s=1, bound_s=5, look_at="x", interval_s=10)


def test_a_wait_names_the_verb_that_resumes_it() -> None:
    """Audit 4e F13: a day-two verb's wait says to run that verb again, not
    `swarm install`; without a verb the text is today's."""
    clock = FakeClock()
    with pytest.raises(WaitGaveUp) as default:
        wait_until("iam.propagation", IAM_PROPAGATION, lambda: UNMET, clock=clock, events=_events(clock))
    assert str(default.value).endswith("Run swarm install again to resume.")

    clock = FakeClock()
    with pytest.raises(WaitGaveUp) as named:
        wait_until("upgrade.drain", IAM_PROPAGATION, lambda: UNMET, clock=clock, events=_events(clock),
                   resume="swarm upgrade")
    assert str(named.value).endswith("Run swarm upgrade again to resume.")
    assert "swarm install" not in str(named.value)
    assert named.value.resume == "swarm upgrade"
