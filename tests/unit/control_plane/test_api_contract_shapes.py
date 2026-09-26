"""The spend-field bug, generalised so the next one cannot happen quietly.

WHAT HAPPENED. `control.record_spend` merge-set five fields onto the attempt
document; `attempt_from_dict` never read them back; `attempt_to_api` faithfully
served `null` for every attempt that had ever run. Every cost and token figure
in the product was unreachable for months, with a full green suite.

WHY THE SUITE WAS GREEN. The only test touching those fields asserted they are
`None` when the document does not carry them -- an assertion the bug SATISFIES.
Nothing asserted they are present when the document carries them. Half of a
pair of assertions is not a weaker test, it is a test that cannot fail on the
bug it is about.

So this file asserts BOTH directions, for EVERY decoder, over EVERY field,
derived from the dataclass rather than typed out:

  * present-when-present: a document carrying a distinct value for every field
    decodes to exactly those values. A decoder that drops a field fails here,
    and it fails for a field added tomorrow without anybody editing this file.
  * absent-when-absent: a document carrying only the required keys decodes to
    the dataclass defaults, and an optional field stays None rather than
    becoming 0 or "". `None` and `0` are different facts about spend.

and the third direction, which is where a field goes missing without any
decoder being involved at all:

  * served-when-modelled: every field on the model reaches the public JSON,
    unless it is on a named exclusion list that says why. `current_generation`
    and `current_lease_id` are on that list and it took a production incident
    to notice they were missing from `task_to_api`.

Offline: no credentials, no emulator, no network. These are pure functions over
dicts.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

from swarm_common.models import (
    Attempt,
    EndCause,
    Lease,
    ProviderState,
    QuotaState,
    SlotPool,
    Task,
    TaskEvent,
    Tenant,
    Workflow,
    WorkflowStep,
)
from swarm_common.states import EventType, ParkReason, TaskState

from swarm_api.codec import (
    attempt_from_dict,
    attempt_to_api,
    event_from_dict,
    event_to_firestore,
    lease_from_dict,
    lease_to_api,
    pool_from_dict,
    pool_to_api,
    quota_from_dict,
    quota_to_api,
    quota_to_firestore,
    task_from_dict,
    task_to_api,
    task_to_firestore,
    tenant_from_dict,
    tenant_to_api,
    tenant_to_firestore,
    workflow_from_dict,
    workflow_to_api,
    workflow_to_firestore,
)

MOMENT = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# A distinct value for every field, derived from its annotation
# --------------------------------------------------------------------------
#
# Derived rather than written out, so a field added to a frozen model is
# covered the moment it exists. A hand-written fixture is a list somebody has
# to remember to extend -- which is exactly how the five spend fields went
# unasserted while sitting in the same dataclass as `peak_rss_bytes`, which was
# asserted.
#
# Every value must DIFFER FROM THE DEFAULT. A generator that happened to
# produce the default would make "the decoder read this field" and "the decoder
# ignored this field" indistinguishable -- the shape of the original bug, this
# time in the test.

def _distinct(annotation: str, name: str, index: int, default: Any) -> Any:
    base = annotation.replace(" | None", "").strip()

    if base == "str":
        return f"{name}-value"
    if base == "int":
        return 1000 + index
    if base == "float":
        return 0.5 + index
    if base == "bool":
        # `enabled` defaults to True and `cancel_requested` to False. The
        # opposite of whatever the default is, so the value is always a change.
        return not bool(default)
    if base == "datetime":
        return MOMENT + timedelta(minutes=index)
    if base == "dict[str, Any]":
        return {f"{name}_key": f"{name}_value"}
    if base == "dict[str, str]":
        return {f"{name}_key": f"{name}_value"}
    if base == "list[str]":
        return [f"{name}-0", f"{name}-1"]
    if base == "list[dict[str, Any]]":
        return [{"reason": "TENANT_LIMIT", "pool": f"tenant:{name}"}]
    if base == "list[WorkflowStep]":
        return [
            WorkflowStep(
                step_id="s1",
                runner_profile="mock",
                input={"prompt": "a"},
                depends_on=[],
                resource_class="standard",
                input_from={"s0": "out.txt"},
                timeout_seconds=900,
                task_id="task_s1",
            )
        ]
    if base == "TaskState":
        return TaskState.RUNNING if default is not TaskState.RUNNING else TaskState.PARKED
    if base == "ParkReason":
        return ParkReason.PROVIDER_COOLDOWN
    if base == "EndCause":
        return EndCause.WORKFLOW_SWEEP
    if base == "EventType":
        return next(iter(EventType))
    if base == "ProviderState":
        return ProviderState.THROTTLED
    raise AssertionError(
        f"no distinct value is defined for {name}: {annotation!r}. "
        "A new annotation in a frozen model needs a value here, or this file "
        "silently stops covering that field."
    )


def _default_of(f: dataclasses.Field[Any]) -> Any:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return f.default_factory()  # type: ignore[misc]
    return dataclasses.MISSING


def _populated(cls: type) -> Any:
    """An instance of `cls` with every field set to a non-default value."""
    kwargs = {
        f.name: _distinct(f.type, f.name, i, _default_of(f))
        for i, f in enumerate(dataclasses.fields(cls))
    }
    return cls(**kwargs)


def _encode(value: Any) -> dict[str, Any]:
    """Firestore's view of the object, enums flattened the way writers do."""
    out: dict[str, Any] = {}
    for f in dataclasses.fields(value):
        v = getattr(value, f.name)
        if hasattr(v, "value"):
            v = v.value
        elif isinstance(v, list) and v and dataclasses.is_dataclass(v[0]):
            v = [dataclasses.asdict(item) for item in v]
        out[f.name] = v
    return out


# --------------------------------------------------------------------------
# The decoder table
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Codec:
    name: str
    cls: type
    encode: Callable[[Any], dict[str, Any]]
    decode: Callable[[dict[str, Any]], Any]
    #: The keys a writer always writes. Everything else must survive absence.
    required: tuple[str, ...]
    #: Public JSON, or None when the model has no public shape.
    to_api: Callable[[Any], dict[str, Any]] | None
    #: Model fields the public shape deliberately omits, and why.
    api_omits: dict[str, str] = dataclasses.field(default_factory=dict)
    #: Public keys that are computed rather than carried on the model.
    api_computed: tuple[str, ...] = ()


CODECS: tuple[Codec, ...] = (
    Codec(
        name="Task",
        cls=Task,
        encode=lambda t: task_to_firestore(t),
        decode=task_from_dict,
        required=("id", "tenant_id", "created_at", "updated_at", "state",
                  "runner_profile", "resource_class"),
        to_api=task_to_api,
        api_omits={
            # BOTH OF THESE WERE MISSING AND IT COST AN INCIDENT TO NOTICE.
            # They are the fencing pair: a UI cannot tell a live attempt from a
            # stale one without them. They are listed here as a deliberate
            # omission ONLY because the task page serves the lease and attempt
            # rows separately, which carry both; if that stops being true this
            # entry is the thing to delete.
            "current_generation": "served on the lease and attempt rows instead",
            "current_lease_id": "served on the lease and attempt rows instead",
            # Contract request 23 (2026-09-25). The typed cause is the outcome
            # ledger's input: GET /v1/outcomes classifies every ended task by
            # it and serves the classes. The task page says why a task ended
            # in `last_error`, which every writer still writes; serving the
            # cause here as well is a public shape change nobody has asked
            # for, and would be a second answer to "why" beside that one.
            "end_cause": "classified and served by GET /v1/outcomes; the task says why in last_error",
        },
        # The input and metadata are served MASKED (owner decision,
        # 2026-09-26), and these say how many masks each took.
        api_computed=("dispatch", "input_redaction_count", "metadata_redaction_count"),
    ),
    Codec(
        name="TaskEvent",
        cls=TaskEvent,
        encode=event_to_firestore,
        decode=event_from_dict,
        required=("event_id", "task_id", "tenant_id", "type", "at"),
        to_api=None,
    ),
    Codec(
        name="Attempt",
        cls=Attempt,
        encode=_encode,
        decode=attempt_from_dict,
        required=("attempt_id", "task_id", "tenant_id", "created_at"),
        to_api=attempt_to_api,
        # Contract request #26: the CPU reading's age, against the route's
        # own clock, computed from `cpu_measured_at`.
        api_computed=("cpu_reading_age_seconds",),
    ),
    Codec(
        name="Lease",
        cls=Lease,
        encode=_encode,
        decode=lease_from_dict,
        required=("lease_id", "task_id", "tenant_id", "created_at",
                  "dispatch_deadline", "expires_at"),
        to_api=lease_to_api,
        api_omits={
            # Renamed, not dropped: docs/web-ui/02 trap B. Nothing ever writes
            # STARTING or RUNNING to a lease, so calling this column "state"
            # invites a reader to compare it with the task's state.
            "state": "served as `dispatch_state`, because it is not the task's state",
        },
        api_computed=("dispatch_state", "released", "expired", "dispatch_overdue"),
    ),
    Codec(
        name="SlotPool",
        cls=SlotPool,
        encode=_encode,
        decode=lambda d: pool_from_dict(d["name"], d),
        required=("name",),
        to_api=pool_to_api,
        api_computed=("effective_limit", "available"),
    ),
    Codec(
        name="QuotaState",
        cls=QuotaState,
        encode=quota_to_firestore,
        decode=quota_from_dict,
        required=("provider", "tenant_id"),
        to_api=quota_to_api,
        api_computed=("effective_limit",),
    ),
    Codec(
        name="Tenant",
        cls=Tenant,
        encode=tenant_to_firestore,
        decode=tenant_from_dict,
        required=("tenant_id",),
        to_api=tenant_to_api,
    ),
    Codec(
        name="Workflow",
        cls=Workflow,
        encode=workflow_to_firestore,
        decode=workflow_from_dict,
        required=("workflow_id", "tenant_id", "created_at", "updated_at", "state"),
        to_api=workflow_to_api,
        #: `state` is SERVED DERIVED from the step rollup, because the stored
        #: field was written once as QUEUED and advanced by nothing. These two
        #: say which answer the caller got. They are computed on purpose, and
        #: naming them here is what makes that a decision rather than drift:
        #: delete either from the codec and the shape test below fails.
        api_computed=("stored_state", "state_source"),
    ),
)

IDS = [c.name for c in CODECS]


# --------------------------------------------------------------------------
# 1. Present when present
# --------------------------------------------------------------------------

@pytest.mark.parametrize("codec", CODECS, ids=IDS)
def test_every_field_a_document_carries_is_read_back(codec: Codec):
    """THE ASSERTION THAT WAS MISSING FOR THE FIVE SPEND FIELDS.

    Every field carries a distinct, non-default value on the way in. A decoder
    that forgets one gives back the default, and the default is not what went
    in, so the comparison names the field it dropped.
    """
    original = _populated(codec.cls)
    decoded = codec.decode(codec.encode(original))

    dropped = [
        f.name
        for f in dataclasses.fields(codec.cls)
        if getattr(decoded, f.name) != getattr(original, f.name)
    ]
    assert not dropped, (
        f"{codec.name}: {dropped} did not survive the round trip through "
        f"Firestore. The value was written and the decoder gave back the "
        f"default, which is how five spend fields served null for months."
    )


@pytest.mark.parametrize("codec", CODECS, ids=IDS)
def test_the_populated_fixture_is_not_accidentally_all_defaults(codec: Codec):
    """The test above only means something if the values differ from the defaults.

    Without this, a `_distinct` that returned the default for some annotation
    would make the round-trip test pass for a decoder that reads nothing --
    the bug under test, reproduced inside its own test.
    """
    original = _populated(codec.cls)
    for f in dataclasses.fields(codec.cls):
        default = _default_of(f)
        if default is dataclasses.MISSING:
            continue
        assert getattr(original, f.name) != default, (
            f"{codec.name}.{f.name} was populated with its own default "
            f"({default!r}), so the round-trip test cannot tell a decoder that "
            "reads it from one that does not."
        )


# --------------------------------------------------------------------------
# 2. Absent when absent
# --------------------------------------------------------------------------

@pytest.mark.parametrize("codec", CODECS, ids=IDS)
def test_a_minimal_document_decodes_to_the_declared_defaults(codec: Codec):
    """The other half of the pair, and the half that already existed.

    An absent optional must come back as its default -- crucially `None` and
    not `0`. None means NOT REPORTED; 0 means measured and free. A UI that
    renders those the same way is lying about one of them.
    """
    populated = _populated(codec.cls)
    full = codec.encode(populated)
    minimal = {k: full[k] for k in codec.required}

    decoded = codec.decode(minimal)

    for f in dataclasses.fields(codec.cls):
        if f.name in codec.required:
            continue
        default = _default_of(f)
        if default is dataclasses.MISSING:
            continue
        if isinstance(default, datetime):
            # `updated_at` and friends fall back to "now" when absent, which is
            # a value this test cannot pin. That it is a datetime at all is the
            # assertable part.
            assert isinstance(getattr(decoded, f.name), datetime)
            continue
        assert getattr(decoded, f.name) == default, (
            f"{codec.name}.{f.name} was absent from the document and decoded "
            f"to {getattr(decoded, f.name)!r} rather than its default {default!r}."
        )


@pytest.mark.parametrize(
    "field_name",
    ("input_tokens", "output_tokens", "cache_read_input_tokens",
     "cache_creation_input_tokens", "cost_usd", "exit_code", "peak_rss_bytes",
     # Contract request #15 (accepted 2026-09-25): an idle agent's 0.0 cores
     # is a measurement, not a missing one.
     "cpu_seconds", "peak_cpu_cores", "mean_cpu_cores", "cpu_limit_cores"),
)
def test_a_measured_zero_is_not_turned_back_into_not_measured(field_name: str):
    """`data.get(k) or None` would undo the fix while looking like it.

    A mock task costs nothing ON PURPOSE. A run whose result could not be
    parsed costs an unknown amount. Zero and None are different facts, and the
    coalescing idiom that collapses them is one character away from the
    correct one.
    """
    doc = {
        "attempt_id": "att_1",
        "task_id": "task_1",
        "tenant_id": "eng",
        "created_at": MOMENT,
        field_name: 0,
    }
    decoded = attempt_from_dict(doc)
    assert getattr(decoded, field_name) == 0
    assert getattr(decoded, field_name) is not None, (
        f"a measured zero for {field_name} came back as 'not measured'"
    )
    assert attempt_to_api(decoded)[field_name] == 0


# --------------------------------------------------------------------------
# 3. Served when modelled
# --------------------------------------------------------------------------

@pytest.mark.parametrize("codec", [c for c in CODECS if c.to_api], ids=[c.name for c in CODECS if c.to_api])
def test_every_modelled_field_reaches_the_public_shape(codec: Codec):
    """A field nobody serves is a field nobody can read, however well it decodes.

    This is the direction the spend fields' sibling bug took: the model gained
    `current_generation` and `current_lease_id`, the decoder read them, and
    `task_to_api` never sent them -- so a client could not tell a live attempt
    from a stale one. Omissions are allowed, but only by NAME, with a reason.
    """
    assert codec.to_api is not None
    payload = codec.to_api(_populated(codec.cls))
    modelled = {f.name for f in dataclasses.fields(codec.cls)}

    missing = sorted(modelled - set(payload) - set(codec.api_omits))
    assert not missing, (
        f"{codec.name}: {missing} exist on the model and are served by nothing. "
        "Either serve them, or list them in api_omits with the reason."
    )

    stale = sorted(set(codec.api_omits) - modelled)
    assert not stale, (
        f"{codec.name}: api_omits names {stale}, which the model no longer has. "
        "An exclusion list that outlives its field silences a real gap later."
    )


@pytest.mark.parametrize("codec", [c for c in CODECS if c.to_api], ids=[c.name for c in CODECS if c.to_api])
def test_the_public_shape_invents_no_key(codec: Codec):
    """Every key a caller sees comes off the model or is a declared computation."""
    assert codec.to_api is not None
    payload = codec.to_api(_populated(codec.cls))
    modelled = {f.name for f in dataclasses.fields(codec.cls)}

    invented = sorted(set(payload) - modelled - set(codec.api_computed))
    assert not invented, (
        f"{codec.name}: {invented} appear in the public JSON and come from "
        "nowhere on the model. If they are computed, declare them in "
        "api_computed so the derivation is a decision rather than an accident."
    )


@pytest.mark.parametrize("codec", [c for c in CODECS if c.to_api], ids=[c.name for c in CODECS if c.to_api])
def test_every_served_value_is_the_value_that_was_modelled(codec: Codec):
    """Not merely present: the same value.

    A serialiser that sends `attempt.peak_rss_bytes` under the key
    `input_tokens` would pass a presence check and serve a wrong number, which
    is worse than serving none.
    """
    assert codec.to_api is not None
    populated = _populated(codec.cls)
    payload = codec.to_api(populated)

    wrong = []
    for f in dataclasses.fields(codec.cls):
        if f.name not in payload or f.name in codec.api_omits:
            continue
        served, held = payload[f.name], getattr(populated, f.name)
        if hasattr(held, "value"):
            held = held.value
        if isinstance(held, list) and held and dataclasses.is_dataclass(held[0]):
            continue  # nested shapes are compared by their own codec
        if isinstance(served, list) and isinstance(held, list):
            if sorted(map(str, served)) != sorted(map(str, held)):
                wrong.append(f.name)
            continue
        if served != held:
            wrong.append(f.name)

    assert not wrong, f"{codec.name}: {wrong} are served under their own key with a different value"


# --------------------------------------------------------------------------
# 4. The nested shape the generic pass cannot reach
# --------------------------------------------------------------------------

def test_every_workflow_step_field_survives_and_is_served():
    """`WorkflowStep` is nested inside Workflow, so the table above skips it.

    It matters more than most: `input_from` is how an artifact passes from one
    agent to the next, and it is the field whose absence made the platform's
    headline feature unreachable at the other end of the same seam.
    """
    original = _populated(Workflow)
    step = workflow_from_dict(workflow_to_firestore(original)).steps[0]
    expected = original.steps[0]

    dropped = [f.name for f in dataclasses.fields(WorkflowStep)
               if getattr(step, f.name) != getattr(expected, f.name)]
    assert not dropped, f"WorkflowStep {dropped} did not survive the round trip"

    served = workflow_to_api(original)["steps"][0]
    missing = sorted({f.name for f in dataclasses.fields(WorkflowStep)} - set(served))
    assert not missing, f"WorkflowStep {missing} exist on the model and are served by nothing"
    assert served["input_from"] == expected.input_from
