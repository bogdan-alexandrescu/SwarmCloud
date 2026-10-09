"""Contract request 52: a step the control plane finishes without a worker ends SUCCEEDED from PARKED (#748).

Accepted by the owner 2026-10-09. Before it, `_ALLOWED` let a task reach
SUCCEEDED only from RUNNING, so the gated integrator of a MERGE verdict --
which runs no agent -- still took a lease, an execution and a clone only to
open its pull request (116 s per workflow, measured 2026-10-06). The request
adds exactly one edge, PARKED -> SUCCEEDED, and nothing else: these tests
hold the PARKED row to the request's block, and every other way to SUCCEEDED
to what it was.
"""

from __future__ import annotations

import pytest

from swarm_common.states import InvalidTransition, TaskState, assert_transition, can_transition

#: The PARKED row exactly as contract request 52's "The requested change" block writes it.
PARKED_ROW = frozenset(
    {TaskState.READY, TaskState.CANCELLED, TaskState.DEAD_LETTERED, TaskState.SUCCEEDED}
)


def test_a_parked_step_may_end_succeeded():
    assert can_transition(TaskState.PARKED, TaskState.SUCCEEDED) is True
    assert_transition(TaskState.PARKED, TaskState.SUCCEEDED)


@pytest.mark.parametrize("to", list(TaskState))
def test_every_other_parked_edge_is_unchanged(to):
    assert can_transition(TaskState.PARKED, to) is (to in PARKED_ROW)


@pytest.mark.parametrize("to", [TaskState.RUNNING, TaskState.LEASED, TaskState.FAILED])
def test_parked_still_cannot_skip_to_a_live_or_failed_state(to):
    """The control: the request opens one terminal edge, not a way round
    admission. A PARKED step still reaches RUNNING only through READY and a
    lease (invariants 1 to 3)."""
    assert can_transition(TaskState.PARKED, to) is False
    with pytest.raises(InvalidTransition):
        assert_transition(TaskState.PARKED, to)


def test_succeeded_is_reached_only_from_running_and_parked():
    sources = {frm for frm in TaskState if can_transition(frm, TaskState.SUCCEEDED)}
    assert sources == {TaskState.RUNNING, TaskState.PARKED}
