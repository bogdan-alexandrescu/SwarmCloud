"""Contract requests 98 and 99: a task that can never run may end FAILED from QUEUED, READY or PARKED (part of #1433).

Accepted by the owner 2026-10-18, answering MCP-SCHED's question on PR 1504:
registered-mcp-servers.md §4.10 ends a task whose per-run MCP credential
expired in the queue FAILED `mcp_credential_expired`, with no lease, rather
than parking it for ever or dead-lettering it. The request adds exactly one
edge, QUEUED -> FAILED (MCP-FAILED-EDGE).

Request 99, accepted by the owner the same day, adds READY -> FAILED and
PARKED -> FAILED (MCP-FAILED-EDGE-B): no task is stored QUEUED while it waits
for admission (`SchedulerStore.ready_tasks` reads READY), so admission writes
the ending from READY and the credential sweep from PARKED. These tests hold
the WHOLE table, so the next edge anyone adds has to say so here too.
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

#: `_ALLOWED` row by row, as contract requests 98 and 99 leave it. Before 98
#: the QUEUED row was READY, PARKED and CANCELLED; before 99 the PARKED row had
#: no FAILED and the READY row was LEASED, PARKED and CANCELLED.
TABLE: dict[TaskState, frozenset[TaskState]] = {
    S.SUBMITTED: frozenset({S.QUEUED, S.CANCELLED}),
    S.QUEUED: frozenset({S.READY, S.PARKED, S.CANCELLED, S.FAILED}),
    S.PARKED: frozenset({S.READY, S.CANCELLED, S.DEAD_LETTERED, S.SUCCEEDED, S.FAILED}),
    S.READY: frozenset({S.LEASED, S.PARKED, S.CANCELLED, S.FAILED}),
    # LEASED -> PARKED: a dispatch the platform refused (contract request 123).
    S.LEASED: frozenset({S.DISPATCHED, S.READY, S.PARKED, S.FAILED, S.CANCELLED}),
    S.DISPATCHED: frozenset({S.STARTING, S.READY, S.FAILED, S.CANCELLED}),
    S.STARTING: frozenset({S.RUNNING, S.READY, S.FAILED, S.CANCELLED}),
    S.RUNNING: frozenset({S.SUCCEEDED, S.FAILED, S.CANCELLED, S.PARKED, S.READY}),
    S.SUCCEEDED: frozenset(),
    S.FAILED: frozenset({S.READY, S.DEAD_LETTERED}),
    S.CANCELLED: frozenset(),
    S.DEAD_LETTERED: frozenset(),
}


@pytest.mark.parametrize("frm", [S.QUEUED, S.READY, S.PARKED])
def test_a_waiting_task_may_end_failed(frm):
    """98 is QUEUED; 99 is READY and PARKED, the states admission and the
    credential sweep actually hold a task in."""
    assert can_transition(frm, S.FAILED) is True
    assert_transition(frm, S.FAILED)


def test_the_table_names_every_state():
    assert set(TABLE) == set(TaskState)


@pytest.mark.parametrize("frm", list(TaskState))
@pytest.mark.parametrize("to", list(TaskState))
def test_no_other_edge_appeared_or_went(frm, to):
    """All 144 pairs: QUEUED, READY and PARKED -> FAILED are the only edges
    the two requests add."""
    assert can_transition(frm, to) is (to in TABLE[frm])


def test_submitted_still_cannot_fail():
    """The control: the requests open three waiting states and not the
    fourth. A SUBMITTED task has not been accepted into the queue yet; it is
    swarm-api's to refuse, not the scheduler's to fail."""
    assert can_transition(S.SUBMITTED, S.FAILED) is False
    with pytest.raises(InvalidTransition):
        assert_transition(S.SUBMITTED, S.FAILED)


@pytest.mark.parametrize("terminal", [S.SUCCEEDED, S.CANCELLED, S.DEAD_LETTERED])
def test_no_other_terminal_state_gained_a_way_out(terminal):
    assert {to for to in TaskState if can_transition(terminal, to)} == set()


def test_failed_before_a_lease_skips_every_state_that_holds_capacity():
    """Invariants 1-3: each new edge leaves a state that holds nothing for a
    terminal one, so it takes no lease and has no reservation to release."""
    assert not {S.QUEUED, S.READY, S.PARKED} & CONCURRENCY_STATES
    sources = {frm for frm in TaskState if can_transition(frm, S.FAILED)}
    assert sources - CONCURRENCY_STATES == {S.QUEUED, S.READY, S.PARKED}
