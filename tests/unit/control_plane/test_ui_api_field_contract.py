"""The named field regressions between the console and this API, as behaviour.

WHAT THIS FILE USED TO BE. It read `apps/swarm-ui/src/types.ts` as text,
regexed each interface's fields, and compared them both ways with what the
routes served. That cross-language reading is gone (part of #72): the console's
shapes are now GENERATED from `swarm_api.wire_types` (`scripts/gen_api_types.py`
writes `apps/swarm-ui/src/generated/api-types.ts`, a drift check fails when it is
stale, and `tsc` holds every console interface that extends a generated one).
`test_wire_types.py` holds each model to what its routes serve, in both
directions, with the populated deployment that used to live here (`seeded`).

WHAT IT HOLDS NOW. The shipped bugs the old table was written for, each
asserted on the served payload and on the model the console's type is
generated from -- never on a `.ts` file:

  * `attempt_from_dict` dropped five spend fields, so the attempts route
    served `input_tokens: null` on every attempt that ever ran;
  * `TaskPage` said `next_cursor`, which this API has never sent, so paging
    could never have advanced past the first page;
  * a lease's own state served as `state` invites a client to read a
    DISPATCHED lease under a RUNNING task as a contradiction;
  * the reconciler's thresholds must arrive WITH the leases, not be restated;
  * an admin sees the platform counts and a non-admin gets no key at all.

Offline: FakeFirestore, StaticTokenVerifier, StaticGroups.
"""

from __future__ import annotations

import pytest

from swarm_api import wire_types

from .conftest import ADMIN_GROUP, ENG_GROUP
from .test_wire_types import get_json, rows, seeded  # noqa: F401 -- `seeded` is a fixture

SPEND_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens", "cost_usd")


def test_the_five_spend_fields_arrive_with_values_not_nulls(seeded):  # noqa: F811
    """`attempt_from_dict` dropped all five; a green suite asserted only nulls."""
    attempt = rows(
        get_json(seeded["client"], "/v1/tasks/task_1/attempts", "alice")["attempts"],
        "/v1/tasks/{id}/attempts",
    )[0]
    assert attempt["input_tokens"] == 12_345
    assert attempt["output_tokens"] == 678
    assert attempt["cache_read_input_tokens"] == 900
    assert attempt["cache_creation_input_tokens"] == 100
    assert attempt["cost_usd"] == pytest.approx(0.0642028)
    # And the model the console's `AttemptRow` is generated from declares all
    # five, so a screen can actually read them.
    for field in SPEND_FIELDS:
        assert field in wire_types.AttemptRow.model_fields, f"AttemptRow does not declare {field}"


def test_the_paging_key_is_the_one_this_api_actually_sends(seeded):  # noqa: F811
    """`types.ts` once said `next_cursor`. This API has never sent it.

    A page token nobody reads is not a crash: the first page renders, the
    control to advance never appears, and the screen looks complete.
    """
    page = get_json(seeded["client"], "/v1/tasks?limit=1", "alice")
    assert "next_page_token" in page
    assert "next_cursor" not in page
    declared = wire_types.TaskPage.model_fields
    assert "next_page_token" in declared
    assert "next_cursor" not in declared


def test_the_lease_state_is_not_offered_to_the_client_as_the_task_state(seeded):  # noqa: F811
    """Trap B: nothing ever writes STARTING or RUNNING to a lease document.

    Serving it as `state` invites a client to put it beside the task's state
    and read a DISPATCHED lease under a RUNNING task as a contradiction.
    """
    row = rows(get_json(seeded["client"], "/v1/admin/leases")["leases"], "/v1/admin/leases")[0]
    assert "dispatch_state" in row
    assert "state" not in row
    declared = wire_types.LeaseRow.model_fields
    assert declared["dispatch_state"].is_required()
    assert "state" not in declared


def test_the_thresholds_arrive_with_the_leases_rather_than_being_restated(seeded):  # noqa: F811
    """A `const GRACE = 90` in the client is the restatement drift, one layer out.

    The thresholds are served on both lease reads and are REQUIRED in the
    models the console's `LeasePage` and `LeaseHeartbeatPage` are generated
    from, so `leaseLiveliness` (which takes them as an argument; its own
    console tests drive it) always has the served values to read.
    """
    for path, user in (("/v1/admin/leases", "root"), ("/v1/leases", "alice")):
        thresholds = get_json(seeded["client"], path, user)["thresholds"]
        assert thresholds["heartbeat_grace_seconds"] > 0, path
        assert thresholds["lease_timeout_seconds"] > 0, path
    for model in (wire_types.LeasePage, wire_types.LeaseHeartbeatPage):
        field = model.model_fields["thresholds"]
        assert field.is_required(), f"{model.__name__}.thresholds must be required"
        assert field.annotation is wire_types.LeaseThresholds
    assert set(wire_types.LeaseThresholds.model_fields) == {
        "heartbeat_grace_seconds", "lease_timeout_seconds"}


def test_an_admin_sees_the_platform_counts_and_a_non_admin_gets_no_key_at_all(seeded):  # noqa: F811
    """An ABSENT field is not a count of nothing, and the client must be able
    to tell them apart -- which needs the field to be optional in the type."""
    # Both ask for the platform half (`?scope=platform`, archived issue 2149): without it
    # nobody is served the key, so the member's absence would prove nothing.
    admin = get_json(seeded["client"], "/v1/stats?scope=platform")
    member = get_json(seeded["client"], "/v1/stats?scope=platform", "alice")
    assert "platform_tasks_by_state" in admin
    assert "platform_tasks_by_state" not in member, (
        "a non-admin received the platform counts key; an empty dict here would "
        "render as a platform with no work"
    )
    assert not wire_types.Stats.model_fields["platform_tasks_by_state"].is_required(), (
        "Stats.platform_tasks_by_state must be OPTIONAL in the model (and so in the generated "
        "type): a required field that is absent reads as undefined, which is indistinguishable "
        "from a zero once a screen sums it"
    )


def test_the_groups_the_client_needs_are_named_here():
    """The fixture would be meaningless if alice were an admin by accident."""
    assert ENG_GROUP != ADMIN_GROUP
