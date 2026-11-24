"""The `states` group: task states, reason codes, event types and tiers.

Read by introspection of the frozen contract (`swarm_common`): every member of
`TaskState`, `ParkReason`, `BlockedReason`, `EventType` and `EndCause` in
declaration order, `TERMINAL_STATES` and `CONCURRENCY_STATES` as sorted lists
(they are sets), the event each terminal state is announced with,
`CREDENTIAL_TIERS`, and each resource class's capacity units (the weights the
global budget charges).
"""

from __future__ import annotations

from typing import Any

INPUTS = (
    "scripts/api_constants/states.py",
    "apps/common/swarm_common/",
)


def build() -> dict[str, Any]:
    from swarm_common.models import CREDENTIAL_TIERS, EndCause
    from swarm_common.profiles import RESOURCE_CLASSES
    from swarm_common.states import (
        CONCURRENCY_STATES,
        TERMINAL_STATES,
        BlockedReason,
        EventType,
        ParkReason,
        TaskState,
    )

    return {
        "task_state": [m.value for m in TaskState],
        "park_reason": [m.value for m in ParkReason],
        "blocked_reason": [m.value for m in BlockedReason],
        "event_type": [m.value for m in EventType],
        "end_cause": [m.value for m in EndCause],
        "terminal_states": sorted(s.value for s in TERMINAL_STATES),
        "terminal_events": sorted(EventType[s.name].value for s in TERMINAL_STATES),
        "concurrency_states": sorted(s.value for s in CONCURRENCY_STATES),
        "credential_tiers": list(CREDENTIAL_TIERS),
        "resource_class_units": {name: rc.units for name, rc in RESOURCE_CLASSES.items()},
    }
