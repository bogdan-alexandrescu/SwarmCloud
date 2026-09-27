"""A quota document's 429 count is the count of the current rate-limit run.

THE DEFECT (CP-10, visual QA 2026-09-25, #85). Provider quota drew `429s 1`
beside `Last 429 —` on a document no 429 had ever been reported for, and `0`
beside `25h ago` on another. The second is correct and the screen now says why
(the column is `429s (this run)`). The first was the worker:
`ControlPlane.update_quota_state` created a missing document with
`setdefault("rate_limit_count", 1)`, so a document whose FIRST report was a
clean run started life claiming a 429.

THE OWNER'S DECISION, 2026-09-25. The count is the number of 429s in the
current rate-limit run -- the run `aimd.refresh` ends ("The rate-limit run is
over") by writing 0. A run starts at a 429 and ends two ways: a clean run
reports the provider AVAILABLE, or the broker's `refresh()` retires the
cooldown and the reset window. Three rules in the worker make the count mean
that:

  (a) a NEW document gets 1 only when its first report is itself a 429
      (EXHAUSTED or THROTTLED); otherwise it starts at 0;
  (b) on UPDATE, a stored count with no `last_429_at` behind it is read as 0
      before anything is added -- so the seeded `1` corrects itself on the
      document's next report, with no one-off Firestore write;
  (c) an update with state AVAILABLE -- the clean-run report at the end of a
      successful attempt -- writes 0. Without it the column's name would be
      false, because `refresh()` never resets a document that is already
      AVAILABLE.

CONSEQUENCE, recorded rather than discovered: the broker's exhaustion
threshold (`AimdConfig.exhaustion_threshold`) now counts 429s since the last
clean run or retired cooldown. A document no worker reports on again keeps its
old count until one does.

These run the production `ControlPlane` against the in-memory store in
`fakes.py`, which has Firestore's `set`/`update` semantics. They were pushed
before the change they pin.
"""

from __future__ import annotations

import io
from datetime import timedelta

import pytest

from agent_worker.control import ControlPlane
from agent_worker.logs import build_logger
from swarm_common.models import ProviderState, utcnow

from fakes import FakeFirestore, FakeTransactionRunner

TENANT = "eng"
PROVIDER = "anthropic"
DOC = f"quota/{PROVIDER}:{TENANT}"

#: The two states that ARE a 429, as `update_quota_state` already decides for
#: `last_429_at`.
RATE_LIMITED = (ProviderState.THROTTLED, ProviderState.EXHAUSTED)


def _control(db: FakeFirestore) -> ControlPlane:
    return ControlPlane(
        db,
        task_id="task_1",
        attempt_id="att_1",
        lease_id="lease_1",
        tenant_id=TENANT,
        generation=1,
        logger=build_logger(
            task_id="task_1",
            attempt_id="att_1",
            tenant_id=TENANT,
            generation=1,
            runner_profile="claude-code",
            stream=io.StringIO(),
        ),
        txn_runner=FakeTransactionRunner(db),
    )


def _seed(db: FakeFirestore, **fields: object) -> None:
    db.seed(
        DOC,
        {
            "provider": PROVIDER,
            "tenant_id": TENANT,
            "state": ProviderState.AVAILABLE.value,
            "updated_at": utcnow() - timedelta(hours=25),
            "configured_hard_max": 50,
            "success_count": 0,
            **fields,
        },
    )


# -- (a) a new document ------------------------------------------------------


def test_a_document_whose_first_report_is_a_clean_run_starts_at_zero():
    db = FakeFirestore()
    _control(db).update_quota_state(provider=PROVIDER, state=ProviderState.AVAILABLE)
    doc = db.doc(DOC)
    assert doc["rate_limit_count"] == 0, (
        "a document created by a clean run claims a 429 nobody reported"
    )
    assert "last_429_at" not in doc


@pytest.mark.parametrize("state", RATE_LIMITED, ids=lambda s: s.value)
def test_a_document_whose_first_report_is_a_429_starts_at_one(state):
    db = FakeFirestore()
    _control(db).update_quota_state(provider=PROVIDER, state=state, retry_after_seconds=30)
    doc = db.doc(DOC)
    assert doc["rate_limit_count"] == 1
    assert doc["last_429_at"] is not None


def test_a_new_document_that_is_neither_starts_at_zero():
    db = FakeFirestore()
    _control(db).update_quota_state(provider=PROVIDER, state=ProviderState.COOLDOWN)
    assert db.doc(DOC)["rate_limit_count"] == 0


# -- (b) a stored count with no 429 behind it --------------------------------


@pytest.mark.parametrize("state", RATE_LIMITED, ids=lambda s: s.value)
def test_a_seeded_count_with_no_last_429_is_read_as_zero_before_adding(state):
    # The live `u-bogdan` document: `1`, and no `last_429_at`, because the old
    # code seeded it on a clean run.
    db = FakeFirestore()
    _seed(db, rate_limit_count=1)
    _control(db).update_quota_state(provider=PROVIDER, state=state)
    assert db.doc(DOC)["rate_limit_count"] == 1, (
        "the seeded 1 was added to instead of being read as no 429s at all"
    )


def test_a_seeded_count_with_no_last_429_is_dropped_by_a_report_that_is_not_a_429():
    db = FakeFirestore()
    _seed(db, rate_limit_count=1)
    _control(db).update_quota_state(provider=PROVIDER, state=ProviderState.COOLDOWN)
    assert db.doc(DOC)["rate_limit_count"] == 0


def test_429s_inside_a_run_still_add_up():
    # The control that shows (b) is not "always reset": a count with a 429
    # behind it is a real run and keeps counting.
    db = FakeFirestore()
    _seed(
        db,
        state=ProviderState.THROTTLED.value,
        rate_limit_count=2,
        last_429_at=utcnow() - timedelta(minutes=3),
    )
    _control(db).update_quota_state(provider=PROVIDER, state=ProviderState.THROTTLED)
    assert db.doc(DOC)["rate_limit_count"] == 3


# -- (c) a clean run ends the run --------------------------------------------


def test_a_clean_run_writes_zero_and_keeps_the_time_of_the_last_429():
    db = FakeFirestore()
    last = utcnow() - timedelta(hours=25)
    _seed(db, state=ProviderState.THROTTLED.value, rate_limit_count=4, last_429_at=last)
    _control(db).update_quota_state(provider=PROVIDER, state=ProviderState.AVAILABLE)
    doc = db.doc(DOC)
    assert doc["rate_limit_count"] == 0, (
        "a clean run did not end the rate-limit run, so `429s (this run)` counts "
        "429s from runs that are over"
    )
    # `Last 429` keeps its time: 0 beside `25h ago` is the correct reading.
    assert doc["last_429_at"] == last
    assert doc["state"] == ProviderState.AVAILABLE.value
