"""The merge train's pure rules: select, judge, bisect, eject (TRAIN-WS).

docs/merge-trains.md §4. Ported from the repository-level train's rules
(PR 1294, `scripts/merge-train.sh` select/judge/plan): a train is up to N ready
pull requests, oldest first; a green train lands every one in order; a red
train of one ejects that one; a red train of more is bisected by halves -- the
first half rides next, then the rest, each half split again if it is red,
while every rider keeps its place in the queue -- so a single culprit is found
in about log2(N) trains and only it is ejected.

Pure functions, no I/O: the coordinator (`mergetrain.tick_tenant`) is tested
over a fake forge in test_merge_train_coordinator.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_api import mergetrain
from swarm_api.mergetrain import Rider

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


def _riders(*numbers: int) -> list[Rider]:
    return [Rider(entry_id=f"e{n}", number=n, head=f"{n:040x}",
                  enqueued_at=T0 + timedelta(minutes=i)) for i, n in enumerate(numbers)]


# --------------------------------------------------------------------------
# select
# --------------------------------------------------------------------------

def test_select_takes_the_oldest_ready_pull_requests_up_to_the_train_size():
    queued = list(reversed(_riders(1, 2, 3, 4, 5, 6)))
    assert [r.number for r in mergetrain.select(queued, size=4)] == [1, 2, 3, 4]


def test_select_departs_with_fewer_when_fewer_are_ready():
    assert [r.number for r in mergetrain.select(_riders(7, 8), size=4)] == [7, 8]
    assert mergetrain.select([], size=4) == []


def test_a_train_size_of_one_lands_one_pull_request_at_a_time():
    assert [r.number for r in mergetrain.select(_riders(1, 2, 3), size=1)] == [1]


def test_a_bisected_half_rides_before_anything_else():
    queued = _riders(1, 2, 3, 4)
    picked = mergetrain.select(queued, size=4, next_batch=["e3", "e4"])
    assert [r.number for r in picked] == [3, 4]


def test_a_bisected_half_whose_entries_left_the_queue_is_ignored():
    queued = _riders(1, 2)
    assert [r.number for r in mergetrain.select(queued, size=4, next_batch=["gone"])] == [1, 2]
    # Only the members still queued ride.
    assert [r.number for r in mergetrain.select(queued, size=4, next_batch=["e2", "gone"])] == [2]


def test_the_same_pull_request_never_rides_twice_in_one_train():
    a, b = _riders(5, 6)
    twin = Rider(entry_id="e5-again", number=5, head=a.head, enqueued_at=b.enqueued_at)
    assert [r.entry_id for r in mergetrain.select([a, b, twin], size=4)] == ["e5", "e6"]


# --------------------------------------------------------------------------
# judge
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reading, minutes, expected", [
    ("green", 5, "green"),
    ("red", 5, "red"),
    ("pending", 5, "pending"),
    ("none", 5, "pending"),
    ("pending", 90, "timed_out"),
    ("none", 200, "timed_out"),
    ("green", 200, "green"),      # a green reading lands even if it came late
    ("red", 200, "red"),
])
def test_judge(reading, minutes, expected):
    verdict = mergetrain.judge(reading, started_at=T0, now=T0 + timedelta(minutes=minutes),
                               max_age_minutes=90)
    assert verdict == expected


# --------------------------------------------------------------------------
# bisect and eject
# --------------------------------------------------------------------------

@pytest.mark.parametrize("size, first", [(2, 1), (3, 2), (4, 2), (5, 3), (10, 5)])
def test_bisect_splits_by_halves_first_half_rounded_up(size, first):
    batch = _riders(*range(1, size + 1))
    head, rest = mergetrain.bisect(batch)
    assert len(head) == first and head + rest == batch


def test_a_red_train_of_one_ejects_it():
    plan = mergetrain.on_failure(_riders(9))
    assert plan.eject == ["e9"] and plan.next_batch == [] and plan.requeue == []


def test_a_red_train_of_several_ejects_nobody_and_sends_its_first_half_next():
    plan = mergetrain.on_failure(_riders(1, 2, 3, 4))
    assert plan.eject == []
    assert plan.next_batch == ["e1", "e2"]
    assert plan.later == ["e3", "e4"]
    assert plan.requeue == ["e1", "e2", "e3", "e4"]


def _drive(numbers: list[int], culprits: set[int], *, size: int) -> tuple[list[list[int]], list[int], list[int]]:
    """Run the pure planner until the queue is empty: (trains, landed, ejected).

    A train is red exactly when it carries a culprit -- what CI on the
    composed head would say."""
    queue = _riders(*numbers)
    # The lane's untested halves, the top last: a red train pushes its rest,
    # then its first half, which rides next.
    halves: list[list[str]] = []
    trains: list[list[int]] = []
    landed: list[int] = []
    ejected: list[int] = []
    for _ in range(50):
        batch = mergetrain.select(queue, size=size, next_batch=halves[-1] if halves else [])
        if halves:
            halves.pop()
        if not batch:
            break
        trains.append([r.number for r in batch])
        if any(r.number in culprits for r in batch):
            plan = mergetrain.on_failure(batch)
            if plan.next_batch:
                halves += [plan.later, plan.next_batch]
            ejected += [r.number for r in batch if r.entry_id in plan.eject]
            queue = [r for r in queue if r.entry_id not in plan.eject]
        else:
            landed += [r.number for r in batch]
            queue = [r for r in queue if r not in batch]
    return trains, landed, ejected


def test_a_green_queue_lands_in_trains_of_n_in_submission_order():
    trains, landed, ejected = _drive([1, 2, 3, 4, 5, 6], set(), size=4)
    assert trains == [[1, 2, 3, 4], [5, 6]]
    assert landed == [1, 2, 3, 4, 5, 6] and ejected == []


def test_a_single_culprit_is_found_by_halves_and_alone_is_ejected():
    trains, landed, ejected = _drive([1, 2, 3, 4], {3}, size=4)
    assert trains == [[1, 2, 3, 4], [1, 2], [3, 4], [3], [4]]
    assert landed == [1, 2, 4] and ejected == [3]


def test_a_culprit_first_in_the_train():
    trains, landed, ejected = _drive([1, 2, 3, 4], {1}, size=4)
    assert trains == [[1, 2, 3, 4], [1, 2], [1], [2], [3, 4]]
    assert landed == [2, 3, 4] and ejected == [1]


def test_two_culprits_are_both_ejected_and_everything_else_lands():
    trains, landed, ejected = _drive([1, 2, 3, 4, 5, 6, 7, 8], {2, 7}, size=8)
    assert sorted(ejected) == [2, 7]
    assert sorted(landed) == [1, 3, 4, 5, 6, 8]
    # Bisection never retests what a red train's halves already split: 11
    # trains, where re-forming from the queue after each ejection took 16 --
    # more than landing the 8 one at a time.
    assert trains == [[1, 2, 3, 4, 5, 6, 7, 8], [1, 2, 3, 4], [1, 2], [1], [2], [3, 4],
                      [5, 6, 7, 8], [5, 6], [7, 8], [7], [8]]


def test_bisection_never_reorders_what_lands():
    _, landed, _ = _drive(list(range(1, 11)), {6}, size=10)
    assert landed == sorted(landed)


# --------------------------------------------------------------------------
# the coordinator and the worker agree on every shared name
# --------------------------------------------------------------------------

def test_the_shared_names_are_the_workers():
    from agent_worker import mergetrain as worker_train

    assert mergetrain.ENTRIES_COLLECTION == worker_train.ENTRIES_COLLECTION
    assert mergetrain.TRAIN_WAITING == worker_train.TRAIN_WAITING
    assert mergetrain.QUEUED == worker_train.QUEUED
    assert mergetrain.RIDING == worker_train.RIDING
    assert mergetrain.LANDED == worker_train.LANDED
    assert mergetrain.EJECTED == worker_train.EJECTED
    assert mergetrain.RETURNED == worker_train.RETURNED
    assert mergetrain.entry_id_for("eng", "task_x") == worker_train.entry_id_for("eng", "task_x")


def test_the_wake_tick_leaves_a_train_wait_alone():
    from swarm_api import mergewake

    assert mergewake.MERGE_TRAIN_WAITING == mergetrain.TRAIN_WAITING
