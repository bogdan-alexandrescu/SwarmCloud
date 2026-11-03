"""A worker switched to the state routes reads the admin's extra-repository budget.

WHY (docs/design/agent-quality/trusted-state.md, owner's pick of 3 November
2026). `additional_repositories_budget_gib` is a platform setting an admin
edits at run time in `control/settings`. A tenant switched to the state
routes builds no Firestore client, so before this its worker always applied
the 6 GiB default whatever the admin set, while swarm-api enforced the
admin's figure at submission. The worker now reads it from `state/context`,
and keeps the default only when the field is absent (or malformed, as for a
Firestore read). The worker's own `ChildApi` and `StateCall` speak to
swarm-api's real app in process; offline, no credentials, no emulator.
"""

from __future__ import annotations

import io

import pytest

from agent_worker import extrarepos
from agent_worker.children import AttemptKey, ChildApiError
from agent_worker.errors import FencedWriteRefused
from agent_worker.logs import build_logger
from agent_worker.statecall import StateCall
from control_plane.fakes import FakeFirestore as ApiFirestore
from state_routes_harness import (
    ATTEMPT,
    TASK,
    TENANT,
    RoutesOpener,
    api_client,
    remote_control,
    seed_attempt,
)

GIB = 1024 ** 3


@pytest.fixture
def api_db() -> ApiFirestore:
    return ApiFirestore()


def _switched_worker_state(api_db):
    """The attempt's opened `StateCall`, as the lifecycle hands it to
    `extrarepos.budget_bytes` in place of a Firestore client."""
    seed_attempt(api_db)
    control = remote_control(RoutesOpener(api_client(api_db)))
    control.validate_generation()
    return control._calls


def _logger(stream: io.StringIO):
    return build_logger(task_id=TASK, attempt_id=ATTEMPT, tenant_id=TENANT, generation=1,
                        runner_profile="mock", stream=stream)


def test_a_switched_worker_reads_the_admins_budget_from_the_state_context(api_db):
    state = _switched_worker_state(api_db)
    api_db.docs["control/settings"] = {extrarepos.BUDGET_SETTING: 11}
    assert extrarepos.budget_bytes(state, logger=_logger(io.StringIO())) == 11 * GIB
    # Read at every call, never cached: the admin's next change reaches the next read.
    api_db.docs["control/settings"] = {extrarepos.BUDGET_SETTING: 2}
    assert extrarepos.budget_bytes(state, logger=_logger(io.StringIO())) == 2 * GIB


def test_a_switched_worker_keeps_the_default_when_the_field_is_absent(api_db):
    state = _switched_worker_state(api_db)
    assert extrarepos.budget_bytes(state, logger=_logger(io.StringIO())) == 6 * GIB
    api_db.docs["control/settings"] = {"merge_by_default": True}
    assert extrarepos.budget_bytes(state, logger=_logger(io.StringIO())) == 6 * GIB



class _FencedAnswer:
    """`ChildApi.call` answering every call as swarm-api answers an attempt
    that no longer owns its task: 409 `attempt_fenced`."""

    def call(self, method: str, path: str, **_kwargs):
        raise ChildApiError(409, "attempt_fenced", "a newer generation owns the task",
                            retryable=False)


def test_a_fenced_budget_read_is_the_fence_not_the_default():
    """The refusal reaches the lifecycle rather than becoming the default
    budget, so the worker exits without cloning or running the agent
    (invariant 5); every other failure to read still means the default."""
    state = StateCall(_FencedAnswer(), tenant_id=TENANT, task_id=TASK, attempt_id=ATTEMPT,
                      lease_id="lease_1", generation=1, sleep=lambda _s: None)
    state._key = AttemptKey()  # as `state/open` leaves it; no proof is checked here
    with pytest.raises(FencedWriteRefused):
        extrarepos.budget_bytes(state, logger=_logger(io.StringIO()))
