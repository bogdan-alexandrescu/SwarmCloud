"""Contract request 85: `WORKER_TRANSITIONS`, the edges a tenant worker may ask swarm-api to apply.

Accepted by the owner 2026-10-17 on PR 1464 (docs/design/tenant-settings-integrity.md
§5 request A, part of #1457). The state routes of design B refuse every edge a
worker does not make today, so an agent holding its worker's token cannot move
its own task anywhere the scheduler, the reconciler or a person decides. The set
is held here to the edges the worker's code writes (`agent_worker/control.py`,
`handoff.py`), each one also legal in `_ALLOWED`.
"""

from __future__ import annotations

import itertools

import pytest

from swarm_common import states
from swarm_common.states import (
    CONCURRENCY_STATES,
    TERMINAL_STATES,
    WORKER_TRANSITIONS,
    TaskState,
    can_transition,
    worker_may_transition,
)

S = TaskState

#: Request 85's set, written out edge by edge, with the worker call site that
#: makes each one. A new worker edge is a contract change, and this table is
#: where its review starts.
WORKER_EDGES = {
    # advance_to_starting / advance_to_running
    (S.LEASED, S.DISPATCHED),
    (S.DISPATCHED, S.STARTING),
    (S.STARTING, S.RUNNING),
    # finish(FAILED) from the crash handler (`_safe_finish`), and
    # finish(CANCELLED) for a cancel read before the runner started, at
    # whatever pre-running state the worker found (a restarted container of
    # the same generation finds STARTING)
    (S.LEASED, S.FAILED),
    (S.LEASED, S.CANCELLED),
    (S.DISPATCHED, S.FAILED),
    (S.DISPATCHED, S.CANCELLED),
    (S.STARTING, S.FAILED),
    (S.STARTING, S.CANCELLED),
    # park / await park / ci park / human park; fail_retryably and the
    # handoff's "person returned control" (READY); finish
    (S.RUNNING, S.PARKED),
    (S.RUNNING, S.READY),
    (S.RUNNING, S.SUCCEEDED),
    (S.RUNNING, S.FAILED),
    (S.RUNNING, S.CANCELLED),
}

ALL_PAIRS = list(itertools.product(TaskState, TaskState))


def test_a_worker_may_not_take_its_running_task_back_to_leased():
    """The design's own example: LEASED is admission's, never a worker's."""
    assert worker_may_transition(S.RUNNING, S.LEASED) is False


def test_every_worker_edge_is_legal_in_the_state_machine():
    illegal = sorted(
        (a.value, b.value) for a, b in WORKER_TRANSITIONS if b not in states._ALLOWED[a]
    )
    assert illegal == []


def test_the_worker_set_is_exactly_the_edges_the_worker_writes():
    assert set(WORKER_TRANSITIONS) == WORKER_EDGES


def test_the_set_is_immutable():
    assert isinstance(WORKER_TRANSITIONS, frozenset)


@pytest.mark.parametrize(("frm", "to"), ALL_PAIRS, ids=lambda s: s.value)
def test_worker_may_transition_is_membership_and_legality(frm, to):
    expected = (frm, to) in WORKER_EDGES and can_transition(frm, to)
    assert worker_may_transition(frm, to) is expected


def test_a_worker_never_admits_or_requeues_a_task_that_holds_nothing():
    """Invariant 1: LEASED is reached only by admission, and a task that holds
    no capacity (PARKED, READY, QUEUED, SUBMITTED) is moved only by the
    scheduler or a person. PARKED -> READY is a person's (swarm-api's
    `human-ready`), not the worker's."""
    for frm, to in WORKER_TRANSITIONS:
        assert to is not S.LEASED
        assert frm in CONCURRENCY_STATES, (frm.value, to.value)
    assert worker_may_transition(S.PARKED, S.READY) is False
    assert worker_may_transition(S.READY, S.LEASED) is False


def test_a_worker_never_leaves_a_terminal_state_or_dead_letters():
    """FAILED -> READY is the reconciler's retry and DEAD_LETTERED is the
    scheduler's; neither is a worker's."""
    for frm, to in WORKER_TRANSITIONS:
        assert frm not in TERMINAL_STATES
        assert to is not S.DEAD_LETTERED
    assert worker_may_transition(S.FAILED, S.READY) is False


@pytest.mark.parametrize(
    ("frm", "to"),
    [(S.STARTING, S.PARKED), (S.STARTING, S.SUCCEEDED), (S.RUNNING, S.DEAD_LETTERED)],
    ids=lambda s: s.value,
)
def test_an_edge_the_state_machine_refuses_is_not_a_worker_edge(frm, to):
    """The design's sketch named these; `_ALLOWED` refuses them, so the
    request drops them rather than adding them to `_ALLOWED`."""
    assert can_transition(frm, to) is False
    assert worker_may_transition(frm, to) is False
