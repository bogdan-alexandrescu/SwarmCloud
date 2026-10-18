"""Await-input steps, the scheduler's half (docs/design/human-steps.md §4.4, lane HW-SCHED).

A workflow step whose runner profile is `control_plane` (`await-input`) waits
for a tenant member's answer. No container ever runs it: swarm-api ends it,
PARKED -> SUCCEEDED on the answer or its declared default, PARKED ->
DEAD_LETTERED on expiry, PARKED -> CANCELLED on a cancel (§4.6). So the
scheduler's whole part is to PARK IT AND NEVER LEASE IT:

  * when its dependencies are satisfied, the dependency rule promotes it to
    READY as it promotes any step, and parks it AWAITING_INPUT in the same
    call, guarded on the promotion it just wrote (§4.4 step 1);
  * admission parks a READY one before any capacity is evaluated or any lease
    taken (§4.4 step 2) -- the backstop for a step submitted with no
    dependencies, or promoted by hand;
  * no sweep promotes it, and the HUMAN_REQUIRED sweep never ends it. The one
    sweep that reads it applies `on_step_failure: fail_workflow` (§4.4 step 3).

The park writes `metadata.await_input.opened_at` and `expires_at`, computed
here, so that no reader re-derives the deadline. Nothing in this module takes
or releases capacity: a park costs nothing (invariant 1), and the step holds no
lease from the moment it is READY to the moment swarm-api ends it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from swarm_common.profiles import RUNNER_PROFILES

from .codec import as_datetime

log = logging.getLogger(__name__)

#: The key of the declaration under the signed `metadata.dispatch`, and of the
#: platform-written window under `metadata` (§4.7).
AWAIT_INPUT_KEY = "await_input"


def is_control_plane(runner_profile: str) -> bool:
    """True for a profile the platform never leases (contract request 93).

    An unknown profile is not one: admission parks it MANUAL_PAUSE first.
    """
    profile = RUNNER_PROFILES.get(runner_profile)
    return profile is not None and bool(getattr(profile, "control_plane", False))


def declared_seconds(metadata: Any, *, max_seconds: int) -> int:
    """`expires_after_seconds` from the signed declaration, held to `max_seconds`.

    swarm-api refuses a declaration over the bound or without the field at
    submission (§4.1), so the bound is only ever applied to a document written
    by something else. Such a step still ends within the bound, rather than
    waiting with no end, which is the failure the required field exists to
    prevent. swarm-api verifies the spec signature before it acts on the
    deadline (§4.6); this reads the declaration, it does not trust it further.
    """
    dispatch = metadata.get("dispatch") if isinstance(metadata, dict) else None
    block = dispatch.get(AWAIT_INPUT_KEY) if isinstance(dispatch, dict) else None
    value = block.get("expires_after_seconds") if isinstance(block, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        log.warning(
            "await-input declaration has no usable expires_after_seconds (%r); "
            "the wait is held to %d seconds", value, max_seconds,
        )
        return max_seconds
    return min(value, max_seconds)


def wait_window(metadata: Any, now: datetime, *, max_seconds: int) -> dict[str, Any]:
    """The `metadata.await_input` to write with the park: `opened_at` and `expires_at`.

    `expires_at` is `opened_at` plus the declared seconds. A step parked once
    already keeps its window: a step an operator promotes by hand is parked
    again by admission, and that must not extend a wait its author bounded.
    Every other key of the block (an `answer` swarm-api wrote) is kept.
    """
    stored = metadata.get(AWAIT_INPUT_KEY) if isinstance(metadata, dict) else None
    block: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}
    try:
        opened = as_datetime(block.get("opened_at"))
        expires = as_datetime(block.get("expires_at"))
    except (TypeError, ValueError):
        opened = expires = None
    if opened is None or expires is None:
        opened = now
        expires = now + timedelta(seconds=declared_seconds(metadata, max_seconds=max_seconds))
    block["opened_at"] = opened
    block["expires_at"] = expires
    return block
