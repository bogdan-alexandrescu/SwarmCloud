"""The SwarmCloud merge train keeps the repository-level train's rules (lane 6, docs/design/merging-without-a-laptop.md §6.3).

  * A red train re-runs its failed Actions runs ONCE before bisecting, as
    `retry_failed_runs` does: a flaky check must not eject a culprit-free
    train.
  * While the base's own required checks are red at its head, a red train is
    held, not bisected, up to `base_red_wait_minutes` (`main_red_gate`).
  * A required check that has no run `required_start_minutes` after compose
    fails the train `required_check_not_started`, naming it, instead of
    waiting out the max train age.
  * The required context only the train produces (`merge-train`) is not
    waited for on the composed head; it is posted at each rider's head just
    before that rider lands, as `land_train` does.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

from swarm_api import forgechecks, mergetrain

from .test_merge_train_coordinator import API, CHECK, _entry, _ride, clock, ctx  # noqa: F401
from .test_merge_train_gather_platform import at, github, settings, tick  # noqa: F401

E2E = "ci / e2e"


def _lane(db) -> dict:
    return next(doc for path, doc in db.docs.items()
                if path.startswith(f"{mergetrain.LANES_COLLECTION}/"))


# --------------------------------------------------------------------------
# retry once
# --------------------------------------------------------------------------

def test_a_red_train_is_retried_once_and_lands_when_the_retry_is_green(ctx, db, github, clock):
    settings(db, gather_min=1)
    flaky = {"red": True}
    github.verdict = lambda numbers: "failure" if (numbers and flaky["red"]) else "success"
    github.on_rerun = lambda: flaky.update(red=False)
    rides = [_ride(db, github, n, minute=0) for n in (11, 12)]

    at(clock, 0)
    assert tick(ctx, clock).composed == 1
    at(clock, 2)
    report = tick(ctx, clock)
    assert github.reruns == [5001]
    # Not bisected: both still ride the same train.
    assert all(_entry(db, e)["state"] == mergetrain.RIDING for e, _ in rides)
    assert _lane(db)["train"]["retried_at"] is not None
    assert report.waiting == 1
    at(clock, 4)
    tick(ctx, clock)
    assert github.main_landed == [11, 12]


def test_a_train_still_red_after_its_one_retry_is_bisected(ctx, db, github, clock):
    settings(db, gather_min=1)
    github.verdict = lambda numbers: "failure" if 12 in numbers else "success"
    rides = {n: _ride(db, github, n, minute=0) for n in (11, 12)}
    at(clock, 0)
    tick(ctx, clock)
    at(clock, 2)
    tick(ctx, clock)
    assert github.reruns == [5001]
    at(clock, 4)
    tick(ctx, clock)
    # One retry per train, then halves.
    assert github.reruns == [5001]
    lane = _lane(db)
    assert lane["last"]["outcome"] == "train_red"
    assert all(_entry(db, e)["state"] == mergetrain.QUEUED for e, _ in rides.values())


def test_a_red_reading_just_after_the_retry_is_not_final(ctx, db, github, clock):
    settings(db, gather_min=1)
    github.verdict = lambda numbers: "failure" if numbers else "success"
    _ride(db, github, 13, minute=0)
    at(clock, 0)
    tick(ctx, clock)
    at(clock, 2)
    tick(ctx, clock)
    at(clock, 2.5)
    report = tick(ctx, clock)
    assert report.ejected == 0 and report.waiting == 1


# --------------------------------------------------------------------------
# a red base holds
# --------------------------------------------------------------------------

def test_a_red_base_holds_a_red_train_up_to_the_wait(ctx, db, github, clock):
    settings(db, gather_min=1, base_red_wait_minutes=30)
    github.verdict = lambda numbers: "failure" if numbers else "success"
    github.base_red = True
    entry_id, _ = _ride(db, github, 21, minute=0)
    at(clock, 0)
    tick(ctx, clock)
    at(clock, 2)
    tick(ctx, clock)          # the one retry
    for minute in (4, 10, 20, 33):
        at(clock, minute)
        report = tick(ctx, clock)
        assert report.ejected == 0 and report.waiting == 1
        assert _entry(db, entry_id)["state"] == mergetrain.RIDING
        assert _lane(db)["status"] == "base_red"
    # Thirty minutes after the hold began (minute 4), the train fails as red.
    at(clock, 34)
    tick(ctx, clock)
    assert _entry(db, entry_id)["state"] == mergetrain.EJECTED
    assert _entry(db, entry_id)["reason"] == "train_red"


def test_a_green_base_does_not_hold_a_red_train(ctx, db, github, clock):
    settings(db, gather_min=1, base_red_wait_minutes=30)
    github.verdict = lambda numbers: "failure" if numbers else "success"
    entry_id, _ = _ride(db, github, 22, minute=0)
    for minute in (0, 2, 4):
        at(clock, minute)
        tick(ctx, clock)
    assert _entry(db, entry_id)["state"] == mergetrain.EJECTED


# --------------------------------------------------------------------------
# a required check that never starts
# --------------------------------------------------------------------------

def test_a_required_check_that_never_starts_fails_the_train_and_is_named(ctx, db, github, clock):
    settings(db, gather_min=1, required_start_minutes=15)
    github.required = [CHECK, E2E]
    github.silent = {E2E}
    entry_id, _ = _ride(db, github, 31, minute=0)
    at(clock, 0)
    tick(ctx, clock)
    at(clock, 14)
    assert tick(ctx, clock).waiting == 1
    assert _entry(db, entry_id)["state"] == mergetrain.RIDING
    at(clock, 15)
    tick(ctx, clock)
    entry = _entry(db, entry_id)
    assert entry["state"] == mergetrain.EJECTED
    assert entry["reason"] == "required_check_not_started"
    assert E2E in entry["detail"] and CHECK not in entry["detail"].split(" never")[0]
    assert github.main_landed == []


def test_not_started_names_only_what_nothing_reported():
    required = [forgechecks.RequiredCheck(CHECK, None), forgechecks.RequiredCheck(E2E, None),
                forgechecks.RequiredCheck("lint", 15368)]
    runs = [{"name": CHECK, "status": "in_progress"}, {"name": "lint", "app": {"id": 1}}]
    statuses = [{"context": "lint", "state": "success"}]
    assert forgechecks.not_started(required, runs, statuses) == [E2E, "lint"]
    assert forgechecks.not_started(required, runs, [{"context": E2E}]) == ["lint"]


# --------------------------------------------------------------------------
# the train-only context
# --------------------------------------------------------------------------

def test_the_train_only_context_is_posted_at_each_head_before_it_lands(ctx, db, github, clock):
    settings(db, gather_min=1)
    github.required = [CHECK, "merge-train"]
    heads = {}
    for n in (41, 42):
        _ride(db, github, n, minute=0)
        heads[n] = github.pulls[n]["head"]["sha"]
    at(clock, 0)
    tick(ctx, clock)
    at(clock, 2)
    tick(ctx, clock)
    assert github.main_landed == [41, 42]
    assert github.posted == [(heads[41], "merge-train"), (heads[42], "merge-train")]
    order = [(c[0], c[1]) for c in github.calls
             if c[1].startswith(f"{API}/statuses/") or c[1].endswith("/merge")]
    assert order == [("POST", f"{API}/statuses/{heads[41]}"), ("PUT", f"{API}/pulls/41/merge"),
                     ("POST", f"{API}/statuses/{heads[42]}"), ("PUT", f"{API}/pulls/42/merge")]


def test_no_context_is_posted_when_the_base_does_not_require_it(ctx, db, github, clock):
    settings(db, gather_min=1)
    _ride(db, github, 43, minute=0)
    for minute in (0, 2):
        at(clock, minute)
        tick(ctx, clock)
    assert github.main_landed == [43]
    assert github.posted == []
