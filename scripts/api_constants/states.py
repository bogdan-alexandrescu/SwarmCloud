"""The `states` group: task states, reason codes, event types and tiers.

Read by introspection of the frozen contract (`swarm_common`): every member of
`TaskState`, `ParkReason`, `BlockedReason`, `EventType` and `EndCause` in
declaration order, `TERMINAL_STATES` and `CONCURRENCY_STATES` as sorted lists
(they are sets), the event each terminal state is announced with,
`CREDENTIAL_TIERS`, each resource class's capacity units at the code
DEFAULT sizes (`default_sizes()`, version 0: an admin may resize a class
since contract requests 109-111 and the live weights are served by
GET /v1/resource-classes, so a bundled console copy can only match the
defaults, and is held to them), every `ProviderState` member, the pool names
`pool_names_for` gives one sample task (the console parses the family off each
name and spells the provider-and-tenant pool the same way), and the quota
broker's sweep interval.

`QUOTA_SWEEP_INTERVAL_SECONDS` is the one value here that is not frozen
contract: it is the interval of the quota-refresh Cloud Scheduler job, whose
cron is `quota_refresh_schedule` in terraform/modules/scheduler/variables.tf.
Terraform cannot be imported, so it is stated once here, in Python, and
scripts/lib/check-contract-parity.sh holds it to that cron; the console's
copy is held to this file's output by
apps/swarm-ui/src/__tests__/contract.parity.test.ts.
"""

from __future__ import annotations

from typing import Any

INPUTS = (
    "scripts/api_constants/states.py",
    "apps/common/swarm_common/",
)

#: Seconds between two quota broker sweeps: the quota-refresh job's cron,
#: `*/5 * * * *` (`quota_refresh_schedule`), is every five minutes. The console
#: draws a quota reading older than twice this as stale. Changing the cron
#: without this fails check-contract-parity.sh.
QUOTA_SWEEP_INTERVAL_SECONDS = 300

#: The sample task whose pool list is published: any tenant, provider, class,
#: runner and backend give one name per family; these are the parity script's
#: long-standing sample.
POOL_SAMPLE = {
    "tenant_id": "parity",
    "provider": "anthropic",
    "resource_class": "standard",
    "runner_profile": "claude-code",
    "backend": "CLOUD_RUN_JOB",
}


def build() -> dict[str, Any]:
    from swarm_common.models import CREDENTIAL_TIERS, EndCause, ProviderState, pool_names_for
    from swarm_common.profiles import default_sizes
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
        "resource_class_units": {name: size.units for name, size in default_sizes().items()},
        "provider_state": [m.value for m in ProviderState],
        "pool_names": {**POOL_SAMPLE, "pools": list(pool_names_for(**POOL_SAMPLE))},
        "quota_sweep_interval_seconds": QUOTA_SWEEP_INTERVAL_SECONDS,
    }
