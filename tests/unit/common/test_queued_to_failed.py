"""Contract request 98: a task that can never run may end FAILED from QUEUED (MCP-FAILED-EDGE, part of #1433).

Accepted by the owner 2026-10-18, answering MCP-SCHED's question on PR 1504:
registered-mcp-servers.md §4.10 ends a task whose per-run MCP credential
expired in the queue FAILED `mcp_credential_expired`, with no lease, rather
than parking it for ever or dead-lettering it. The request adds exactly one
edge, QUEUED -> FAILED. These tests hold the WHOLE table, so the next edge
anyone adds has to say so here too.
"""

from __future__ import annotations

import pytest

from swarm_common.states import (
    CONCURRENCY_STATES,
    InvalidTransition,
    TaskState,
    assert_transition,
    can_transition,
)

S = TaskState

#: `_ALLOWED` row by row, as contract request 98 leaves it. Before the request
#: the QUEUED row was READY, PARKED and CANCELLED.
TABLE: dict[TaskState, frozenset[TaskState]] = {
    S.SUBMITTED: frozenset({S.QUEUED, S.CANCELLED}),
    S.QUEUED: frozenset({S.READY, S.PARKED, S.CANCELLED, S.FAILED}),
    S.PARKED: frozenset({S.READY, S.CANCELLED, S.DEAD_LETTERED, S.SUCCEEDED}),
    S.READY: frozenset({S.LEASED, S.PARKED, S.CANCELLED}),
    S.LEASED: frozenset({S.DISPATCHED, S.READY, S.FAILED, S.CANCELLED}),
    S.DISPATCHED: frozenset({S.STARTING, S.READY, S.FAILED, S.CANCELLED}),
    S.STARTING: frozenset({S.RUNNING, S.READY, S.FAILED, S.CANCELLED}),
    S.RUNNING: frozenset({S.SUCCEEDED, S.FAILED, S.CANCELLED, S.PARKED, S.READY}),
    S.SUCCEEDED: frozenset(),
    S.FAILED: frozenset({S.READY, S.DEAD_LETTERED}),
    S.CANCELLED: frozenset(),
    S.DEAD_LETTERED: frozenset(),
}


def test_a_queued_task_may_end_failed():
    assert can_transition(S.QUEUED, S.FAILED) is True
    assert_transition(S.QUEUED, S.FAILED)


def test_the_table_names_every_state():
    assert set(TABLE) == set(TaskState)


@pytest.mark.parametrize("frm", list(TaskState))
@pytest.mark.parametrize("to", list(TaskState))
def test_no_other_edge_appeared_or_went(frm, to):
    """All 144 pairs: QUEUED -> FAILED is the only edge the request adds."""
    assert can_transition(frm, to) is (to in TABLE[frm])


@pytest.mark.parametrize("frm", [S.READY, S.PARKED, S.SUBMITTED])
def test_the_other_waiting_states_still_cannot_fail(frm):
    """The control: the request opens QUEUED -> FAILED and nothing else. READY
    and PARKED, the states admission and the credential sweep actually read,
    still cannot reach FAILED without a lease. Request 99 asks for those two;
    when it is applied this test and TABLE change with it."""
    assert can_transition(frm, S.FAILED) is False
    with pytest.raises(InvalidTransition):
        assert_transition(frm, S.FAILED)


def test_failed_from_queued_skips_every_state_that_holds_capacity():
    """Invariants 1-3: the edge leaves a state that holds nothing for a
    terminal one, so it takes no lease and has no reservation to release."""
    assert S.QUEUED not in CONCURRENCY_STATES
    sources = {frm for frm in TaskState if can_transition(frm, S.FAILED)}
    assert sources - CONCURRENCY_STATES == {S.QUEUED}
