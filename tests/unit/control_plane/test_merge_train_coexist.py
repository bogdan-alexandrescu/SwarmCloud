"""One train per base, and nothing lands unread-green or on a red base (lane 8, docs/design/merging-without-a-laptop.md §8).

  * With the repository's own `MERGE_TRAIN` variable `on`, the SwarmCloud
    lane composes nothing, lands nothing, abandons a train it had and reports
    `repository_train_on`; turning the variable off hands the base back on
    the next tick.
  * THE LANDING CHECK: a rider whose train's required checks were not read
    green at the tested head is never merged, and nothing is merged onto a
    base whose own required checks are red at its head.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

from swarm_api import mergetrain

from .test_merge_train_coordinator import API, _entry, _ride, clock, ctx  # noqa: F401
from .test_merge_train_gather_platform import (  # noqa: F401
    at, github, register, settings, tick,
)


def _lane(db) -> dict:
    return next(doc for path, doc in db.docs.items()
                if path.startswith(f"{mergetrain.LANES_COLLECTION}/"))


# --------------------------------------------------------------------------
# the repository's own train wins
# --------------------------------------------------------------------------

def test_with_the_repository_train_on_the_lane_composes_nothing(ctx, db, github, clock):
    settings(db, gather_min=1)
    github.variable = "on"
    entry_id, _ = _ride(db, github, 11, minute=0)
    at(clock, 0)
    report = tick(ctx, clock)
    assert report.composed == 0 and report.repository_train_on == 1
    assert report.to_api()["repository_train_on"] == 1
    assert _lane(db)["status"] == mergetrain.REPOSITORY_TRAIN_ON
    assert _entry(db, entry_id)["state"] == mergetrain.QUEUED
    assert not github.path_calls("POST", f"{API}/git/refs")
    assert not github.path_calls("POST", f"{API}/pulls")

    # Off: the platform takes the base on the next tick.
    github.variable = "off"
    at(clock, 2)
    assert tick(ctx, clock).composed == 1
    assert _lane(db)["status"] is None


def test_a_running_train_is_abandoned_when_the_repository_train_comes_on(ctx, db, github, clock):
    settings(db, gather_min=1)
    github.verdict = lambda _n: None
    entry_id, _ = _ride(db, github, 12, minute=0)
    at(clock, 0)
    tick(ctx, clock)
    assert _entry(db, entry_id)["state"] == mergetrain.RIDING
    github.verdict = lambda _n: "success"
    github.variable = "on"
    at(clock, 2)
    report = tick(ctx, clock)
    assert report.repository_train_on == 1
    assert github.main_landed == []
    assert not github.path_calls("PUT", f"{API}/pulls/")
    assert _entry(db, entry_id)["state"] == mergetrain.QUEUED
    lane = _lane(db)
    assert lane["train"] is None and lane["last"]["outcome"] == mergetrain.REPOSITORY_TRAIN_ON
    assert not any(b.startswith(mergetrain.BRANCH_PREFIX) for b in github.branches)


def test_a_ready_pull_request_does_not_board_while_the_repository_train_is_on(
        ctx, db, github, clock):
    settings(db, gather_min=1)
    register(db)
    github.permissions = {"writer-account": "write"}
    github.open_pull(13)
    github.label(13, "writer-account")
    github.variable = "on"
    at(clock, 0)
    report = tick(ctx, clock)
    assert report.boarded == 0 and report.repository_train_on == 1
    # Not even read: who labelled it is asked only when the lane may board.
    assert not github.path_calls("GET", f"{API}/issues/")


# --------------------------------------------------------------------------
# the landing check
# --------------------------------------------------------------------------

def test_a_rider_not_read_green_at_its_tested_head_is_never_merged(ctx, db, github, clock):
    settings(db, gather_min=1)
    github.verdict = lambda _n: None
    entry_id, _ = _ride(db, github, 21, minute=0)
    at(clock, 0)
    tick(ctx, clock)
    # A train put in `landing` with no green reading (a tick that died, or a
    # document written by anything but the judge), its checks red now.
    lane = _lane(db)
    lane["train"] = {**lane["train"], "state": "landing"}
    lane["train"].pop("green_head", None)
    github.verdict = lambda numbers: "failure" if numbers else "success"
    at(clock, 2)
    tick(ctx, clock)
    assert not github.path_calls("PUT", f"{API}/pulls/")
    assert github.main_landed == []
    assert _entry(db, entry_id)["state"] == mergetrain.QUEUED
    assert _lane(db)["last"]["outcome"] == "not_read_green"


def test_a_landing_train_read_green_now_lands(ctx, db, github, clock):
    settings(db, gather_min=1)
    github.verdict = lambda _n: None
    _ride(db, github, 22, minute=0)
    at(clock, 0)
    tick(ctx, clock)
    lane = _lane(db)
    lane["train"] = {**lane["train"], "state": "landing"}
    github.verdict = lambda _n: "success"
    at(clock, 2)
    tick(ctx, clock)
    assert github.main_landed == [22]


def test_nothing_lands_on_a_red_base(ctx, db, github, clock):
    settings(db, gather_min=1, base_red_wait_minutes=20)
    github.base_red = True
    entry_id, _ = _ride(db, github, 23, minute=0)
    at(clock, 0)
    tick(ctx, clock)
    for minute in (2, 10, 21):
        at(clock, minute)
        report = tick(ctx, clock)
        assert report.waiting == 1
        assert not github.path_calls("PUT", f"{API}/pulls/")
        assert _lane(db)["status"] == "base_red"
    # Past the wait, the train is abandoned; its rider waits for another.
    at(clock, 22)
    tick(ctx, clock)
    assert github.main_landed == []
    assert _entry(db, entry_id)["state"] == mergetrain.QUEUED
    assert _lane(db)["last"]["outcome"] == "base_red"
    # Green again: it lands.
    github.base_red = False
    for minute in (24, 26):
        at(clock, minute)
        tick(ctx, clock)
    assert github.main_landed == [23]
