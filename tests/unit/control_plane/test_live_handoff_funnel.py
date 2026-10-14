"""Every hand-off records its funnel (LIVE-MEASURE; part of #1030).

Owner decision 2026-10-13: Phase 1 of the live-browser plan is "measure". The
analysis found 25 of 42 hand-offs expired, 13 with nobody present, and 7
AFTER the person pressed "I'm ready". Which step loses people -- the bell,
opening the view, taking control, typing -- was not recorded anywhere. Now,
per hand-off:

  requested -> first viewer joined -> control taken -> first input -> resolved

each as seconds from the request, on the `human_handoff_resolved` event's
`detail.funnel` and its audit entry, and as one structured log line
(`event=live_handoff_funnel`) that scripts/live-report.sh queries. And after
a park, "I'm ready" -> the resumed attempt's first frame, on the session
document and as `event=live_ready_first_frame`.

Milestones are written once, at the first time each happened, onto
`Task.human_wait` (a free-form dict in the frozen model), so they die with the
wait and are read by the one function that ends it (`resolution_writes`).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest

from swarm_api.handoff import HUMAN_READY_METADATA_KEY

from .test_handoff_api import (  # noqa: F401 - fixtures
    Attempt as HandoffAttempt,
    live_client,
    request_handoff,
    resolve_handoff,
    running,
    worker_tokens,
)
from .test_live_api import (  # noqa: F401 - fixtures
    TASK,
    Attempt,
    _admission,
    _service,
    _wait_opened_at,
    audit,
    bearer,
    client,
    clock,
    groups,
    ldb,
    live,
    open_session,
    people,
    relay,
    seed_running,
    session_doc,
    take,
)
from .conftest import seed_tenant

FUNNEL_LOGGER = "swarm_api.handoff"


def _resolved(db) -> dict[str, Any]:
    (event,) = [d for p, d in db.docs.items()
                if p.startswith(f"tasks/{TASK}/events/") and d["type"] == "human_handoff_resolved"]
    return event


def _log_fields(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    return [r.__dict__ for r in caplog.records
            if r.name == FUNNEL_LOGGER and getattr(r, "event", None) == event]


def test_the_funnel_is_recorded_from_request_to_return(client, ldb, live, clock, caplog):
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    _wait_opened_at(ldb, clock)
    service = _service(ldb, clock)
    clock.advance(12)
    assert service.watched(_admission()) is True       # the first viewer joins
    clock.advance(5)
    assert take(client, "alice").status_code == 200     # control taken at 17 s
    clock.advance(3)
    assert service.record_input(_admission(), keys=2, pointers=1) is True  # first input at 20 s
    clock.advance(20)
    returned = client.post(f"/v1/tasks/{TASK}/live/control/return", headers=bearer("alice"))
    assert returned.status_code == 200, returned.text

    funnel = _resolved(ldb)["detail"]["funnel"]
    assert funnel == {
        "outcome": "returned",
        "first_viewer_s": 12,
        "control_taken_s": 17,
        "first_input_s": 20,
        "resolved_s": 40,
        "person_present": True,
    }
    (row,) = audit(ldb, "handoff_resolved")
    assert row["detail"]["funnel"] == funnel
    (line,) = _log_fields(caplog, "live_handoff_funnel")
    assert line["task_id"] == TASK and line["tenant_id"] == "eng"
    assert line["request_id"] == "signin-1"
    assert (line["outcome"], line["first_viewer_s"], line["control_taken_s"],
            line["first_input_s"], line["resolved_s"], line["person_present"]) == (
        "returned", 12, 17, 20, 40, True)
    # Names and numbers: never the hand-off's reason or a person's address.
    assert "Sign in" not in repr(line) and "alice" not in repr(line)


def test_each_milestone_is_its_first_time_and_is_not_moved_later(client, ldb, live, clock):
    _wait_opened_at(ldb, clock)
    service = _service(ldb, clock)
    clock.advance(4)
    service.watched(_admission())
    first = ldb.docs[f"tasks/{TASK}"]["human_wait"]["first_viewer_at"]
    clock.advance(30)
    service.watched(_admission())  # the socket's periodic "still watching"
    assert ldb.docs[f"tasks/{TASK}"]["human_wait"]["first_viewer_at"] == first
    assert take(client, "alice").status_code == 200
    taken = ldb.docs[f"tasks/{TASK}"]["human_wait"]["control_taken_at"]
    clock.advance(2)
    service.record_input(_admission(), keys=0, pointers=0)  # an empty flush is no input
    assert "first_input_at" not in ldb.docs[f"tasks/{TASK}"]["human_wait"]
    service.record_input(_admission(), keys=1, pointers=0)
    typed = ldb.docs[f"tasks/{TASK}"]["human_wait"]["first_input_at"]
    clock.advance(2)
    service.record_input(_admission(), keys=1, pointers=0)
    wait = ldb.docs[f"tasks/{TASK}"]["human_wait"]
    assert (wait["control_taken_at"], wait["first_input_at"]) == (taken, typed)


def test_a_hand_off_nobody_opened_expires_with_an_empty_funnel(live_client, db, caplog):
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    seed_tenant(db, "eng")
    attempt = HandoffAttempt()
    running(live_client, db, attempt)
    assert request_handoff(live_client, attempt).status_code == 201
    assert resolve_handoff(live_client, attempt, outcome="expired").status_code == 200
    event = next(d for p, d in db.docs.items()
                 if "/events/" in p and d["type"] == "human_handoff_resolved")
    funnel = event["detail"]["funnel"]
    assert funnel["outcome"] == "expired"
    assert funnel["person_present"] is False
    assert (funnel["first_viewer_s"], funnel["control_taken_s"], funnel["first_input_s"]) == (
        None, None, None)
    assert isinstance(funnel["resolved_s"], int)
    (line,) = _log_fields(caplog, "live_handoff_funnel")
    assert line["outcome"] == "expired" and line["person_present"] is False
    # Answered twice, logged once: the second resolve finds nothing waiting.
    assert resolve_handoff(live_client, attempt, outcome="expired").status_code == 200
    assert len(_log_fields(caplog, "live_handoff_funnel")) == 1


def test_im_ready_to_the_resumed_attempts_first_frame(client, ldb, clock, caplog):
    """After a park, the person's "I'm ready" is the clock's start; the resumed
    attempt's gate reporting its first frame is the picture appearing."""
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    attempt = Attempt()
    ready_at = clock() - timedelta(seconds=47)
    seed_running(client, ldb, attempt, metadata={
        HUMAN_READY_METADATA_KEY: {"by": "alice@saga.xyz", "at": ready_at.isoformat()}})
    assert open_session(client, attempt).status_code == 201
    assert session_doc(ldb)["human_ready_at"] == ready_at
    service = _service(ldb, clock)
    first_frame = ready_at + timedelta(seconds=41.6)
    report = {"origin": None, "device": None,
              "agent": {"connected_at": None, "connections": 0, "first_frame_at": first_frame}}
    assert service.record_report(_admission(), report) is True
    assert session_doc(ldb)["ready_to_first_frame_seconds"] == pytest.approx(41.6)
    (row,) = audit(ldb, "ready_first_frame")
    assert row["detail"] == {"seconds": pytest.approx(41.6)}
    (line,) = _log_fields(caplog, "live_ready_first_frame")
    assert line["task_id"] == TASK and line["seconds"] == pytest.approx(41.6)
    assert line["attempt_id"] == attempt.tuple.attempt_id
    # The same first frame reported again is not a second measurement.
    later = {**report, "origin": {"scheme": "https", "host": "example.com"}}
    service.record_report(_admission(), later)
    assert len(_log_fields(caplog, "live_ready_first_frame")) == 1
    assert len(audit(ldb, "ready_first_frame")) == 1


def test_a_session_nobody_said_ready_for_measures_nothing(client, ldb, live, clock, caplog):
    caplog.set_level(logging.INFO, logger=FUNNEL_LOGGER)
    assert "human_ready_at" not in session_doc(ldb)
    report = {"origin": None, "device": None,
              "agent": {"connected_at": None, "connections": 0, "first_frame_at": clock()}}
    assert _service(ldb, clock).record_report(_admission(), report) is True
    assert "ready_to_first_frame_seconds" not in session_doc(ldb)
    assert not _log_fields(caplog, "live_ready_first_frame")
