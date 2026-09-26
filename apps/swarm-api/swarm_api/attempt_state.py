"""Whether an attempt has ended, judged the way the UI judges it.

MOVED HERE from `swarm_api.attempt_usage` (#184 follow-up). That module read
the worker's CPU figures back off HEARTBEAT events for
`GET /v1/tasks/{id}/attempts?include=usage`, the interim path #188 built while
the frozen `Attempt` had no CPU fields. Contract request #15 was accepted on
#184 (2026-09-25), the figures are typed attempt fields now, and the events
reader went with the path it served. This judgement did not belong to it:
`/answer` uses it to tell "not yet" from "absent", and it is kept whole.
"""

from __future__ import annotations

from datetime import datetime, timezone

from swarm_common.models import Attempt, Task
from swarm_common.states import CONCURRENCY_STATES, TERMINAL_STATES


def attempt_is_over(attempt: Attempt, task: Task, *, is_latest: bool) -> bool:
    """Whether this attempt has ended, on the evidence the UI's `attemptEnd` uses.

    Restated from `apps/swarm-ui/src/duration.ts` so the server's "live" and
    the drawer's "running" are the same judgement:

      * its document records `completed_at`;
      * it is not the task's newest attempt (a newer one superseded it);
      * the task is terminal;
      * the task no longer holds this attempt's lease -- on a task document
        written no earlier than the attempt existed, since an older read says
        nothing about an attempt admitted after it.
    """
    if attempt.completed_at is not None:
        return True
    if not is_latest:
        return True
    if task.state in TERMINAL_STATES:
        return True
    # A null pointer is NOT holding: the API always serves `current_lease_id`,
    # and null there means the task holds no lease -- the UI's `letGo` reads it
    # the same way (only a pointer the payload OMITS is read as holding).
    holds = task.state in CONCURRENCY_STATES and task.current_lease_id == attempt.lease_id
    if holds:
        return False
    return _utc(task.updated_at) >= _utc(attempt.created_at)


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
