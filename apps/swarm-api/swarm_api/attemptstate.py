"""The worker's state, through swarm-api (docs/design/tenant-settings-integrity.md §3.2, §4).

WHY. Every tenant worker's service account can create, get and update any
document in the `swarm` database whose id it can name (§1.1), and its agent
can mint that account's token. So one tenant's agent can stop, redirect or
over-admit every other tenant (§1.3, #1457). Design B moves every read and
write the worker makes behind these routes, then removes the role. Each call
proves three things before anything is read for it (§4.1):

  * the ID token is the TENANT's worker account (`children.worker_email`),
    which closes every cross-tenant row: `401 attempt_unauthenticated`;
  * the attempt proof -- Ed25519 over `swarm-state-req/v1`, by the key the
    worker registered at `state/open` -- is the WORKER of this attempt, not
    its agent: `403 attempt_unproven`. A v1 (child-only) registration never
    signs a state request (§4.5);
  * then ONE Firestore transaction re-reads the task and the lease and
    applies the fence (invariant 5): `409 attempt_fenced`, nothing written.

The worker names only its tuple, `(tenant_id, task_id, attempt_id, lease_id,
generation)`. Every document written is derived from the tuple, never from a
name in the body: a release releases this tuple's lease and the pools THAT
LEASE names, through `swarm_common.admission.release_lease_in_transaction`,
the one release function (invariant 2). Transitions are only the worker's
(`states.worker_may_transition`, contract request 85), so an agent holding
the token cannot admit, requeue or dead-letter anything: `422`.

UNUSED UNTIL A TENANT IS SWITCHED. The routes are mounted, and nothing calls
them until lane ISO-WORKER-CORE's `SWARM_STATE_VIA_API` is on for a tenant
(§6, owner decision Q7). They write the documents and fields the worker
writes today, with the same names and types (§4.5), so the scheduler,
reconciler, broker and console read them unchanged and a tenant can be
switched back. Where a route is stricter than the direct path it says so at
the call.

Nothing here logs, stores or serves the token, the nonce, the proof or a
registration.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Literal, Mapping

from google.api_core import exceptions as gexc
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import AwareDatetime, Field

from swarm_common.admission import AdmissionConfig, _snapshot, release_lease_in_transaction
from swarm_common.config import Settings
from swarm_common.models import Attempt, EndCause, Tenant, new_id, retries_exhausted, utcnow
from swarm_common.states import (
    CONCURRENCY_STATES,
    TERMINAL_STATES,
    EventType,
    ParkReason,
    TaskState,
    worker_may_transition,
)

from .access import grant_id_for
from .childkey import PURPOSE_STATE, AttemptTuple, state_request_message, verify_proof
from .children import (
    ChildService,
    ChildUnauthenticated,
    ChildUnavailable,
    _Strict,
    parse_body,
)
from .errors import ApiError, ValidationFailed
from .forgeapp import GRANTS
from .mergesettings import COLLECTION as WORKSPACE_SETTINGS
from .provider_keys import KEYS_COLLECTION as PROVIDER_KEYS
from .repositories import parse_repository, repo_id_for
from .repoindex import BASE_LINE, INDEXER_PROFILE, KEPT_VERSIONS, VERSIONS_COLLECTION
from .rollup import derived_state_write
from .store import EVENTS, LEASES, TASKS
from .verdictpublish import spec_refusal
from .workspaces import personal_tenant_id

log = logging.getLogger(__name__)

ATTEMPTS = "attempts"
QUOTA = "quota"
TENANTS = "tenants"
REPOSITORIES = "repositories"
#: swarm-api's own graph-context document (`graphstage.SETTINGS_COLLECTION`).
TENANT_SETTINGS = "tenant_settings"
#: The park's upload carry (#166), `control.CARRY_COLLECTION` / `CARRY_INDEX_ID`.
CARRY = "carry"
CARRY_INDEX = "parks"
MERGE_SLOTS = "merge_slots"
MERGE_TRAIN_ENTRIES = "merge_train_entries"

#: The metadata keys the parks write, as the worker spells them
#: (`control.CHILD_AWAIT_RESUMES_METADATA_KEY`, `MERGE_WAIT_METADATA_KEY`,
#: `handoff.HUMAN_*`). Restated: the worker's image is not this package.
CHILD_AWAIT_RESUMES = "child_await_resumes"
MERGE_WAIT = "merge_wait"
HUMAN_WAIT_RESUMES = "human_wait_resumes"
#: An agent's release wait (AU7-REFUND): the block its park's detail carries
#: (`questions.RELEASE_WAIT_KEY`, what `mergewake.release_wait_of` reads) and
#: the waits refunded so far (`control.RELEASE_WAIT_RESUMES_METADATA_KEY`).
RELEASE_WAIT = "release_wait"
RELEASE_WAIT_RESUMES = "release_wait_resumes"
HUMAN_RETURNED = "human_returned"
HUMAN_READY = "human_ready"
CHILD_CASCADE = "child_cascade"

#: THE REFUND BOUNDS ARE THE SERVER'S. A worker sends no number (§4.4): an
#: agent that could send one would refund its task for ever. Each is the
#: worker's own today: `config.max_child_await_resumes` (4),
#: `handoff.MAX_HUMAN_RESUMES` (2), `merge.MERGE_CI_MAX_WAKES` (60),
#: `mergeslot.MERGE_SLOT_MAX_WAITS` and `mergetrain.TRAIN_MAX_WAITS` (200),
#: and `merge.MERGE_CI_FALLBACK_SECONDS` (900).
CHILD_AWAIT_MAX_RESUMES = 4
HUMAN_MAX_RESUMES = 2
CI_MAX_WAKES = 60
SLOT_MAX_WAITS = 200
TRAIN_MAX_WAITS = 200
CI_FALLBACK_SECONDS = 900
#: `control.RELEASE_WAIT_MAX_RESUMES` (4), which says why it is not the CI
#: wait's 60: a release wait is woken once per release, so each refund is a
#: whole release, the child await's shape and its bound.
RELEASE_WAIT_MAX_RESUMES = 4
#: A PROVIDER_QUOTA_EXHAUSTED park-and-exit (#2350, BGD 2026-10-27, "Refund,
#: bounded"): invariant 4's designed path, not a failed attempt, so it is
#: refunded like a CI wait, counted in `metadata.quota_park_resumes`
#: (`control.QUOTA_PARK_RESUMES_METADATA_KEY`, reserved by `validation`).
#: 5 BECAUSE four 24-31 s `accounts_full` parks took task_c61ee787 to
#: attempt_count 5 of 3, and its first real loss was then final: five rides
#: out a full account pool's churn, and past it a task that only ever parks
#: still ends at `max_attempts`. The worker restates it
#: (`control.QUOTA_MAX_RESUMES`) for its direct path and sends no number here.
QUOTA_PARK_RESUMES = "quota_park_resumes"
QUOTA_MAX_RESUMES = 5
#: `mergeslot.MERGE_SLOT_WAIT` and `mergetrain.TRAIN_WAITING`: the CI-park
#: codes counted on their own counters and off the CI clock.
MERGE_SLOT_WAIT = "merge_slot_wait"
TRAIN_WAITING = "merge_train_waiting"

#: A checkpoint archive's digest (`control.ARCHIVE_DIGEST_RE`, #346).
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
#: An event id the worker chooses for an idempotent event: `new_id("evt")`.
_EVENT_ID = re.compile(r"^evt_[0-9a-f]{20}$")
#: A lease release's reason, `parked:<reason>`, `terminal:<state>`, `retry:<cause>`.
_REASON = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
_PROVIDER = re.compile(r"^[a-z0-9-]{1,64}$")
_GITHUB_URL = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
#: A task id as the worker accepts one in `builds_on` (`lifecycle._TASK_ID_RE`).
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}$")
#: An incremental index run's base line in its SIGNED prompt
#: (`indexrun.requested_base`): exactly one, or no base at all.
_BASE_SHA_LINE = re.compile(r"^" + re.escape(BASE_LINE) + r"([0-9a-f]{40})$", re.MULTILINE)
#: How far `upstream` follows `builds_on` back from this task's own: the
#: worker's own bound (`lifecycle._MAX_WORKFLOW_STEPS`, #978), read from the
#: FROZEN `Settings` default as the worker reads it. Every link is a distinct
#: step of one workflow, so a longer chain is one no control plane wrote.
BUILDS_ON_MAX_DEPTH: int = next(
    f.default for f in dataclasses.fields(Settings) if f.name == "max_workflow_steps"
)
#: How many task documents `upstream` reads walking `depends_on` back through
#: this task's workflow (#2070): every ancestor is a distinct step of it, so
#: the same bound. The worker's walk (`owner_questions.MAX_ANCESTORS`) reads
#: no more than this.
WORKFLOW_ANCESTORS_MAX: int = BUILDS_ON_MAX_DEPTH

#: The attempt document's usage fields a worker may write, by `kind` (§4.2):
#: the frozen `Attempt`'s names, nothing else. `cpu_limit_source` takes the
#: worker's `metrics.CPU_LIMIT_SOURCES`.
USAGE_FIELDS: dict[str, tuple[str, ...]] = {
    "resource": ("peak_rss_bytes", "peak_disk_bytes", "oom_near_miss"),
    "cpu": ("cpu_seconds", "peak_cpu_cores", "mean_cpu_cores", "cpu_limit_cores",
            "cpu_limit_source"),
    "spend": ("input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens", "cost_usd"),
}
CPU_LIMIT_SOURCES = ("cgroup", "resource_class")
assert all(
    name in Attempt.__dataclass_fields__ for names in USAGE_FIELDS.values() for name in names
)

#: The largest free-form JSON a body may carry in one field (a result summary,
#: an event's detail, an upload manifest). Firestore's document limit is 1 MiB.
MAX_FREE_FORM_BYTES = 256 * 1024
#: How many waiters a release reads the state of before handing the slot on
#: (`mergeslot.HANDOFF_READS`).
HANDOFF_READS = 8
#: `inputs.find_ci_fix_tasks`'s callers ask for a handful.
MAX_CI_FIX_TASKS = 50


# --------------------------------------------------------------------------
# Refusals (§4.2): the child routes' shapes, under their own codes
# --------------------------------------------------------------------------


class AttemptUnauthenticated(ApiError):
    status_code = 401
    code = "attempt_unauthenticated"


class AttemptUnproven(ApiError):
    status_code = 403
    code = "attempt_unproven"


class AttemptFenced(ApiError):
    status_code = 409
    code = "attempt_fenced"


class TransitionRefused(ValidationFailed):
    code = "transition_refused"


class StateUnavailable(ApiError):
    status_code = 503
    code = "state_unavailable"


class UpstreamNotDeclared(ApiError):
    status_code = 403
    code = "upstream_not_declared"


class MergeWakeRefused(ApiError):
    status_code = 403
    code = "merge_wake_refused"


class StateConflict(ApiError):
    status_code = 409
    code = "state_conflict"


# --------------------------------------------------------------------------
# Bodies. Strict: an unknown key -- a `runner_profile`, an image, a pool
# name -- is a 422 (invariant 10). The tuple is in every one.
# --------------------------------------------------------------------------


class _Tuple(_Strict):
    tenant_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=1)


class _Requested(_Tuple):
    #: Idempotency (§4.2): a repeat of a seen id is answered without a write.
    request_id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")


class EventIn(_Strict):
    type: str = Field(min_length=1, max_length=64)
    detail: dict[str, Any] = Field(default_factory=dict)


class OpenBody(_Tuple):
    nonce: str = Field(min_length=1, max_length=128)
    public_key: str = Field(min_length=1, max_length=64)
    #: Owner decision Q4: a heap `make_non_dumpable` could not protect registers
    #: anyway, flagged, and its key signs state only.
    unprotected: bool = False
    #: The attempt whose checkpoint the worker restores, for its digest binding.
    restore_attempt_id: str | None = Field(default=None, min_length=1, max_length=128)


class TransitionBody(_Requested):
    to: str = Field(min_length=1, max_length=32)
    detail: dict[str, Any] = Field(default_factory=dict)


class AttemptStartBody(_Tuple):
    backend: str = Field(min_length=1, max_length=32)
    execution_name: str | None = Field(default=None, max_length=256)


class CheckpointBody(_Tuple):
    checkpoint_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    uri: str = Field(min_length=1, max_length=1024)
    size_bytes: int = Field(ge=0)
    seq: int = Field(ge=0)
    archive_sha256: str


class UsageBody(_Tuple):
    kind: Literal["resource", "cpu", "spend"]
    fields: dict[str, Any]


class CiWait(_Strict):
    code: str = Field(min_length=1, max_length=64)
    head: str = Field(default="", max_length=64)
    pull_request: int = Field(ge=0)
    pending: list[str] = Field(default_factory=list, max_length=200)


class ParkBody(_Requested):
    reason: str = Field(min_length=1, max_length=64)
    next_eligible_at: AwareDatetime | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    uploads: list[Any] | None = None
    announce: list[EventIn] = Field(default_factory=list, max_length=4)
    ci: CiWait | None = None


class FinishBody(_Requested):
    state: str = Field(min_length=1, max_length=32)
    exit_code: int | None = None
    error: str | None = Field(default=None, max_length=8192)
    end_cause: str | None = Field(default=None, max_length=64)
    result_summary: dict[str, Any] | None = None


class RetryBody(_Requested):
    exit_code: int | None = None
    error: str = Field(max_length=8192)
    cause: str = Field(min_length=1, max_length=64)
    result_summary: dict[str, Any] | None = None
    retry_delay_seconds: int = Field(default=0, ge=0, le=86_400)
    detail: dict[str, Any] = Field(default_factory=dict)
    end_cause: str | None = Field(default=None, max_length=64)


class AttemptEndBody(_Tuple):
    exit_code: int | None = None
    error: str | None = Field(default=None, max_length=8192)


class HeartbeatBody(_Tuple):
    pass


class ReleaseBody(_Tuple):
    reason: str


class EventBody(_Tuple):
    event_id: str
    type: str = Field(min_length=1, max_length=64)
    detail: dict[str, Any] = Field(default_factory=dict)


class MergeSlotBody(_Tuple):
    op: Literal["peek", "acquire", "renew", "confirm", "release", "wake"]
    base: str = Field(min_length=1, max_length=256)
    updates: int | None = Field(default=None, ge=0)
    reason: str | None = None
    #: The SLOT's generation the caller holds (not the attempt's).
    slot_generation: int | None = Field(default=None, ge=0)
    wake_task_id: str | None = Field(default=None, min_length=1, max_length=128)


class MergeTrainBody(_Tuple):
    op: Literal["peek", "register"]
    base: str | None = Field(default=None, max_length=256)
    number: int | None = Field(default=None, ge=1)
    head: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=1024)
    message: str | None = Field(default=None, max_length=65_536)


class CiFixBody(_Tuple):
    pull_request: int = Field(ge=1)
    root_task_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=20, ge=1, le=MAX_CI_FIX_TASKS)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def jsonable(value: Any) -> Any:
    """A Firestore value as JSON: instants as ISO 8601, everything else as is."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _bounded(value: Any, what: str) -> Any:
    try:
        size = len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        raise ValidationFailed(f"{what} is not JSON") from None
    if size > MAX_FREE_FORM_BYTES:
        raise ValidationFailed(f"{what} is larger than {MAX_FREE_FORM_BYTES} bytes")
    return value


def fence_view(task: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What every writing answer carries (§4.2), enough for `ControlSignals`."""
    if task is None:
        return None
    return {
        "state": task.get("state"),
        "current_generation": task.get("current_generation"),
        "current_lease_id": task.get("current_lease_id"),
        "cancel_requested": bool(task.get("cancel_requested")),
        "next_eligible_at": jsonable(task.get("next_eligible_at")),
    }


def fence_reason(
    task: Mapping[str, Any] | None, lease: Mapping[str, Any] | None, attempt: AttemptTuple
) -> str | None:
    """Why `attempt` no longer owns its task, or None. The worker's fence
    (`control._task_fence` and `_lease_fence`), plus the tenant and attempt
    checks a server must make because the tuple is the caller's.

    NOT `children._lease_is_live`: that refuses a lease whose `expires_at` has
    passed, and the direct path's writes do not. A swarm-api outage longer
    than one lease timeout would then fence every running attempt for good,
    where today the next beat extends the lease the reconciler has not yet
    reclaimed. The registration window (`state/open`) does use it.
    """
    if task is None:
        return "task document no longer exists"
    if task.get("tenant_id") != attempt.tenant_id:
        return "task belongs to another tenant"
    if task.get("current_generation") != attempt.generation:
        return "task has moved to a newer generation"
    state = task.get("state")
    if state in {s.value for s in TERMINAL_STATES}:
        return f"task is already terminal ({state})"
    if state not in {s.value for s in CONCURRENCY_STATES}:
        return f"task no longer holds capacity (state={state})"
    if lease is None:
        return "lease document is gone"
    if lease.get("tenant_id") != attempt.tenant_id:
        return "lease belongs to another tenant"
    if lease.get("released_at") is not None:
        return "lease has already been released"
    if lease.get("generation") != attempt.generation:
        return "lease belongs to a different generation"
    if lease.get("task_id") != attempt.task_id or lease.get("attempt_id") != attempt.attempt_id:
        return "lease belongs to a different attempt"
    if task.get("current_lease_id") not in (None, attempt.lease_id):
        return "task points at a different lease"
    return None


def _lease_is_this_tuples(lease: Mapping[str, Any] | None, attempt: AttemptTuple) -> bool:
    return (
        lease is not None
        and lease.get("tenant_id") == attempt.tenant_id
        and lease.get("task_id") == attempt.task_id
        and lease.get("attempt_id") == attempt.attempt_id
        and lease.get("generation") == attempt.generation
    )


def request_event_id(attempt: AttemptTuple, op: str, request_id: str) -> str:
    """The event a requested write announces itself with: its id IS the
    idempotency record (§4.2, "no new collection"). A repeat finds it."""
    digest = hashlib.sha256(
        f"swarm-state-request/v1\n{attempt.encode()}\n{op}\n{request_id}".encode("utf-8")
    ).hexdigest()
    return f"evt_{digest[:20]}"


def _event_type(value: str) -> EventType:
    try:
        return EventType(value)
    except ValueError:
        raise ValidationFailed("type must be an EventType value") from None


def _end_cause(value: str | None) -> EndCause | None:
    if value is None:
        return None
    try:
        return EndCause(value)
    except ValueError:
        raise ValidationFailed("end_cause must be an EndCause value") from None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def cancel_end_cause(task: Mapping[str, Any]) -> EndCause:
    """The worker's `control.cancel_end_cause`: CHILD_CASCADE when the parent's
    cascade flagged the task (contract request 41), else CANCEL_REQUESTED."""
    metadata = task.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get(CHILD_CASCADE):
        return EndCause.CHILD_CASCADE
    return EndCause.CANCEL_REQUESTED


def owner_repo(repository_url: Any) -> tuple[str, str] | None:
    """`(owner, repo)` of a signed `https://github.com/owner/repo(.git)`, else None."""
    if not isinstance(repository_url, str):
        return None
    matched = _GITHUB_URL.match(repository_url.strip())
    if matched is None:
        return None
    try:
        return parse_repository(f"{matched.group(1)}/{matched.group(2)}")
    except ValueError:
        return None


def slot_id(tenant_id: str, repository: str, base: str) -> str:
    """`mergeslot.slot_id`: the tenant, then a digest of `owner/repo` and the base."""
    digest = hashlib.sha256(f"{repository.lower()}\n{base}".encode()).hexdigest()[:40]
    return f"{tenant_id}--{digest}"


def entry_id_for(tenant_id: str, task_id: str) -> str:
    """`mergetrain.entry_id_for`."""
    digest = hashlib.sha256(f"{tenant_id}\n{task_id}".encode("utf-8")).hexdigest()
    return "mte_" + digest[:32]


# --------------------------------------------------------------------------
# The merge slot's transitions (`agent_worker.mergeslot`), run here
# --------------------------------------------------------------------------
#
# Moved, not redesigned: these are the worker's pure functions over the slot
# document, so a tenant switched to the routes and one still on the direct
# path take turns on the same document by the same rules. They run here
# because a function cannot be sent over HTTP, and because the `ended` set --
# which holder or waiter has finished -- is now read by swarm-api inside the
# tenant, not claimed by the caller.

MERGE_SLOT_LEASE_SECONDS = 45 * 60
MERGE_SLOT_WAITER_STALE_SECONDS = 2 * MERGE_SLOT_LEASE_SECONDS
MERGE_SLOT_MAX_WAITERS = 500
SLOT_WAKE_REASON = "merge_slot_granted"
_WAKE_MARKER = "wake_requested_at"


@dataclass(frozen=True)
class Claimant:
    task_id: str
    attempt_id: str
    submitted_at: datetime


@dataclass
class _Walk:
    """One `state/upstream` request's tally of task documents of this tenant
    whose spec signature did not verify: each stops the rule that read it,
    and the count is logged, so a rewrite is never mistaken for an absence."""
    unverified: int = 0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _builds_on(metadata: Mapping[str, Any]) -> str | None:
    """The task id a dispatch block's `builds_on` names, or None."""
    raw = _mapping(metadata.get("dispatch")).get("builds_on")
    if not isinstance(raw, str) or not _TASK_ID.match(raw.strip()):
        return None
    return raw.strip()


def _instant(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _holder(doc: Mapping[str, Any]) -> Mapping[str, Any] | None:
    holder = doc.get("holder")
    return holder if isinstance(holder, Mapping) and isinstance(holder.get("task_id"), str) else None


def _waiters(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = doc.get("waiters")
    if not isinstance(raw, list):
        return []
    return [dict(w) for w in raw
            if isinstance(w, Mapping) and isinstance(w.get("task_id"), str)
            and isinstance(w.get("submitted_at"), datetime)]


def _order(entry: Mapping[str, Any]) -> tuple[datetime, str]:
    return entry["submitted_at"], entry["task_id"]


def holder_expired(holder: Mapping[str, Any], now: datetime) -> bool:
    expires = _instant(holder.get("expires_at"))
    return expires is None or expires <= now


def _grant(entry: Mapping[str, Any], *, generation: int, now: datetime, how: str,
           attempt_id: str | None = None, updates: int | None = None) -> dict[str, Any]:
    return {
        "task_id": entry["task_id"],
        "attempt_id": attempt_id,
        "submitted_at": entry["submitted_at"],
        "generation": generation,
        "granted_at": now,
        "granted_by": how,
        "renewed_at": now,
        "expires_at": now + timedelta(seconds=MERGE_SLOT_LEASE_SECONDS),
        "updates_at_acquire": updates,
    }


def _live_waiters(doc: Mapping[str, Any], *, now: datetime, ended: set[str],
                  drop: str | None = None) -> list[dict[str, Any]]:
    stale = now - timedelta(seconds=MERGE_SLOT_WAITER_STALE_SECONDS)
    kept = []
    for entry in _waiters(doc):
        if entry["task_id"] == drop or entry["task_id"] in ended:
            continue
        seen = _instant(entry.get("seen_at"))
        if seen is not None and seen < stale:
            continue
        kept.append(entry)
    return sorted(kept, key=_order)


def _renewed(out: dict[str, Any], holder: Mapping[str, Any], me: Claimant, *, now: datetime,
             updates: int | None) -> dict[str, Any]:
    renewed = dict(holder)
    renewed["attempt_id"] = me.attempt_id
    renewed["renewed_at"] = now
    renewed["expires_at"] = now + timedelta(seconds=MERGE_SLOT_LEASE_SECONDS)
    if not isinstance(renewed.get("updates_at_acquire"), int) and updates is not None:
        renewed["updates_at_acquire"] = updates
    out["holder"] = renewed
    out["updated_at"] = now
    return out


def _held(holder: Mapping[str, Any]) -> dict[str, int]:
    return {"generation": _count(holder.get("generation")),
            "updates_at_acquire": _count(holder.get("updates_at_acquire"))}


def slot_acquire(doc: Mapping[str, Any] | None, me: Claimant, *, tenant_id: str,
                 repository: str, base: str, now: datetime, updates: int,
                 ended: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """`mergeslot.acquire`: `(document, {"held": ...} or {"waiting": ...})`."""
    out = dict(doc or {})
    out.update(tenant_id=tenant_id, repository=repository, base=base)
    out["generation"] = _count(out.get("generation"))
    holder = _holder(out)
    if holder is not None and holder["task_id"] == me.task_id:
        out = _renewed(out, holder, me, now=now, updates=updates)
        out["waiters"] = _live_waiters(out, now=now, ended=ended, drop=me.task_id)
        return out, {"held": _held(out["holder"])}
    waiters = _live_waiters(out, now=now, ended=ended, drop=me.task_id)
    mine = {"task_id": me.task_id, "submitted_at": me.submitted_at, "seen_at": now}
    previous = next((w for w in _waiters(out) if w["task_id"] == me.task_id), None)
    mine["queued_at"] = _instant((previous or {}).get("queued_at")) or now
    free = holder is None or holder_expired(holder, now) or holder["task_id"] in ended
    if not free:
        assert holder is not None
        queue = sorted([*waiters, mine], key=_order)[:MERGE_SLOT_MAX_WAITERS]
        out["waiters"] = queue
        position = next((i for i, w in enumerate(queue, 1) if w["task_id"] == me.task_id),
                        len(queue) + 1)
        return out, {"waiting": {"holder": holder["task_id"], "position": position,
                                 "waiters": len(queue), "wake": None}}
    if holder is not None:
        out["last_release"] = {"task_id": holder["task_id"], "generation": holder.get("generation"),
                               "reason": "ended" if holder["task_id"] in ended else "expired",
                               "at": now, "by": me.task_id}
    queue = sorted([*waiters, mine], key=_order)
    first = queue[0]
    out["generation"] += 1
    if first["task_id"] == me.task_id:
        out["holder"] = _grant(mine, generation=out["generation"], now=now, how="acquire",
                               attempt_id=me.attempt_id, updates=updates)
        out["waiters"] = queue[1:MERGE_SLOT_MAX_WAITERS + 1]
        out["updated_at"] = now
        return out, {"held": {"generation": out["generation"], "updates_at_acquire": updates}}
    out["holder"] = _grant(first, generation=out["generation"], now=now, how="handoff")
    rest = queue[1:MERGE_SLOT_MAX_WAITERS + 1]
    out["waiters"] = rest
    out["updated_at"] = now
    position = next((i for i, w in enumerate(rest, 1) if w["task_id"] == me.task_id), len(rest))
    return out, {"waiting": {"holder": first["task_id"], "position": position,
                             "waiters": len(rest), "wake": first["task_id"]}}


def slot_renew(doc: Mapping[str, Any] | None, me: Claimant, *, now: datetime,
               updates: int | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """`mergeslot.renew`: extend the slot's lease if this step holds it."""
    if doc is None:
        return None, None
    holder = _holder(doc)
    if holder is None or holder["task_id"] != me.task_id:
        return None, None
    out = _renewed(dict(doc), holder, me, now=now, updates=updates)
    return out, _held(out["holder"])


def slot_release(doc: Mapping[str, Any] | None, me: Claimant, *, now: datetime, reason: str,
                 ended: set[str], generation: int | None = None
                 ) -> tuple[dict[str, Any] | None, str | None]:
    """`mergeslot.release`: give the slot up and leave the queue; who it went to."""
    if doc is None:
        return None, None
    out = dict(doc)
    holder = _holder(out)
    listed = any(w["task_id"] == me.task_id for w in _waiters(out))
    mine = holder is not None and holder["task_id"] == me.task_id and (
        generation is None or _count(holder.get("generation")) == generation)
    if not mine and not listed:
        return None, None
    waiters = _live_waiters(out, now=now, ended=ended, drop=me.task_id)
    out["waiters"] = waiters
    out["updated_at"] = now
    if not mine:
        return out, None
    assert holder is not None
    out["last_release"] = {"task_id": me.task_id, "generation": holder.get("generation"),
                           "reason": reason, "at": now, "by": me.task_id}
    if not waiters:
        out["holder"] = None
        return out, None
    out["generation"] = _count(out.get("generation")) + 1
    first = waiters[0]
    out["holder"] = _grant(first, generation=out["generation"], now=now, how="handoff")
    out["waiters"] = waiters[1:]
    out["last_release"]["handed_to"] = first["task_id"]
    return out, first["task_id"]


def register_entry(existing: Mapping[str, Any] | None, *, fields: Mapping[str, Any],
                   now: Any) -> tuple[dict[str, Any] | None, Mapping[str, Any]]:
    """`mergetrain.register_entry`: queue the step's entry, or keep it."""
    if existing is not None and existing.get("tenant_id") != fields.get("tenant_id"):
        raise StateConflict("the merge train entry under this step's id is another tenant's")
    if existing is None:
        new = {**fields, "state": "queued", "returns": 0, "enqueued_at": now, "updated_at": now}
        return new, new
    same_head = existing.get("head") == fields.get("head")
    if same_head and existing.get("state") in ("queued", "riding", "landed", "ejected"):
        return None, existing
    new = {**fields, "state": "queued", "returns": int(existing.get("returns") or 0),
           "enqueued_at": existing.get("enqueued_at", now), "updated_at": now}
    return new, new


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One authenticated, proven call: its tuple and its parsed body."""

    attempt: AttemptTuple
    body: Any


class AttemptState:
    """The state routes' logic. Built per request from the app context."""

    def __init__(
        self,
        *,
        settings: Any,
        db: Any,
        verifier: Any,
        now: Callable[[], datetime] = utcnow,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._db = db
        self._now = now
        self._clock = clock
        # The child service holds the worker-token check, the registrations and
        # the nonce; this module adds the STATE purpose to each. No rate
        # limiter: a refused heartbeat costs a running attempt its lease, and
        # every call past the token still needs the attempt key.
        self.children = ChildService(
            settings=settings, db=db, store=None, submissions=None, verifier=verifier,
            limiter=None, now=now, clock=clock,
        )
        self._lease_extension = AdmissionConfig().lease_timeout_seconds

    @classmethod
    def from_context(cls, ctx: Any, *, verifier: Any) -> "AttemptState":
        return cls(settings=ctx.settings, db=ctx.db, verifier=verifier, now=ctx.now)

    # -- the three proofs (§4.1) -------------------------------------------

    def _authenticate(self, authorization: str | None, tenant_id: str) -> None:
        try:
            self.children._require_configured()
        except ChildUnavailable as exc:
            raise StateUnavailable(exc.message) from None
        try:
            self.children.authenticate_worker(authorization, tenant_id)
        except ChildUnauthenticated:
            raise AttemptUnauthenticated(
                "only this tenant's worker service account may call this route"
            ) from None

    def _prove(self, attempt: AttemptTuple, *, method: str, path: str, body: bytes,
               proof: str | None, timestamp: str | None) -> None:
        """Every failure is the same 403, so none is an oracle."""
        unproven = AttemptUnproven("the attempt proof did not verify for this attempt")
        if not proof or not timestamp or not re.fullmatch(r"\d{1,12}", timestamp):
            raise unproven
        skew = int(getattr(self._settings, "child_proof_skew_seconds", 120))
        if abs(self._clock() - int(timestamp)) > skew:
            raise unproven
        registration = self.children._registration(attempt, PURPOSE_STATE)
        if registration is None:
            raise unproven
        message = state_request_message(method, path, body, timestamp)
        if not verify_proof(registration["public_key"], proof, message):
            raise unproven

    def call(self, model: type[_Tuple], attempt_id: str, raw: bytes, *,
             authorization: str | None, proof: str | None, timestamp: str | None,
             path: str, method: str = "POST") -> Call:
        """Parse, authenticate, prove: what every route but `open` does first."""
        body = parse_body(model, raw)
        self._authenticate(authorization, body.tenant_id)
        attempt = AttemptTuple(body.tenant_id, body.task_id, attempt_id, body.lease_id,
                               body.generation)
        self._prove(attempt, method=method, path=path, body=raw, proof=proof,
                    timestamp=timestamp)
        return Call(attempt, body)

    def call_get(self, attempt_id: str, *, tenant_id: str, task_id: str, lease_id: str,
                 generation: int, authorization: str | None, proof: str | None,
                 timestamp: str | None, signed_path: str) -> AttemptTuple:
        """A GET's proof covers the path WITH its query, so the tuple cannot be
        swapped under a captured proof (as `children.list_children`)."""
        self._authenticate(authorization, tenant_id)
        attempt = AttemptTuple(tenant_id, task_id, attempt_id, lease_id, generation)
        self._prove(attempt, method="GET", path=signed_path, body=b"", proof=proof,
                    timestamp=timestamp)
        return attempt

    # -- Firestore ------------------------------------------------------------

    def _ref(self, collection: str, doc_id: str) -> Any:
        return self._db.collection(collection).document(doc_id)

    def _task_ref(self, attempt: AttemptTuple) -> Any:
        return self._ref(TASKS, attempt.task_id)

    def _event_ref(self, attempt: AttemptTuple, event_id: str) -> Any:
        return self._task_ref(attempt).collection(EVENTS).document(event_id)

    def _run(self, body: Callable[[Any], Any]) -> Any:
        """`body` in one transaction. A Firestore fault is a retryable 503."""
        try:
            return firestore.transactional(body)(self._db.transaction())
        except ApiError:
            raise
        except (gexc.GoogleAPIError, ValueError) as exc:
            # ValueError: `firestore.transactional` gives up on contention so.
            raise StateUnavailable(
                f"the state store did not answer ({type(exc).__name__}); retry"
            ) from None

    def _read(self, ref: Any) -> dict[str, Any] | None:
        try:
            snap = ref.get()
        except gexc.GoogleAPIError as exc:
            raise StateUnavailable(
                f"the state store did not answer ({type(exc).__name__}); retry"
            ) from None
        return (snap.to_dict() or {}) if snap.exists else None

    def _fenced(self, txn: Any, attempt: AttemptTuple) -> dict[str, Any]:
        """Read the task and the lease THROUGH `txn`; 409 unless this attempt
        still owns its task. Every read the caller makes after this is still
        before its first write (Firestore forbids a read after one)."""
        task_snap = _snapshot(txn.get(self._task_ref(attempt)))
        task = (task_snap.to_dict() or {}) if task_snap.exists else None
        lease_snap = _snapshot(txn.get(self._ref(LEASES, attempt.lease_id)))
        lease = (lease_snap.to_dict() or {}) if lease_snap.exists else None
        reason = fence_reason(task, lease, attempt)
        if reason is not None:
            raise AttemptFenced(f"this attempt no longer owns its task: {reason}")
        assert task is not None
        return task

    def _seen(self, txn: Any, attempt: AttemptTuple, event_id: str) -> dict[str, Any] | None:
        """A repeat of a requested write: the task as it now stands, or None."""
        if not _snapshot(txn.get(self._event_ref(attempt, event_id))).exists:
            return None
        task_snap = _snapshot(txn.get(self._task_ref(attempt)))
        task = (task_snap.to_dict() or {}) if task_snap.exists else {}
        if task.get("tenant_id") != attempt.tenant_id:
            raise AttemptFenced("this attempt no longer owns its task")
        return task

    def _event(self, attempt: AttemptTuple, kind: EventType, detail: Mapping[str, Any],
               *, at: datetime, event_id: str | None = None) -> tuple[Any, dict[str, Any]]:
        """`control._event_write`'s document, for this tuple."""
        event_id = event_id or new_id("evt")
        return self._event_ref(attempt, event_id), {
            "event_id": event_id,
            "task_id": attempt.task_id,
            "tenant_id": attempt.tenant_id,
            "type": kind.value,
            "at": at,
            "attempt_id": attempt.attempt_id,
            "lease_id": attempt.lease_id,
            "generation": attempt.generation,
            "detail": dict(detail),
        }

    def _release(self, txn: Any, attempt: AttemptTuple, reason: str, *, now: datetime,
                 events: list[tuple[Any, dict[str, Any]]]) -> bool:
        """THIS TUPLE'S lease, through the one release function (invariant 2).

        It reads the lease and its pools and then writes them, so a caller
        calls it after every read of its own and before any write.
        """
        released = release_lease_in_transaction(
            txn, db=self._db, lease_id=attempt.lease_id, reason=reason, now=now
        )
        if released:
            events.append(self._event(attempt, EventType.LEASE_RELEASED, {"reason": reason},
                                      at=now))
        return released

    @staticmethod
    def _answer(task: Mapping[str, Any] | None, **extra: Any) -> dict[str, Any]:
        return {"ok": True, "task": fence_view(task), **extra}

    # -- state/open (§4.3) -------------------------------------------------

    def open(self, attempt_id: str, raw: bytes, *, authorization: str | None) -> dict[str, Any]:
        """The worker's FIRST call: spend the nonce on a v2 key, then hand back
        what `validate_generation`, `fetch_task`, `fetch_lease`,
        `fetch_attempt` and `parked_uploads` read today."""
        body: OpenBody = parse_body(OpenBody, raw)
        self._authenticate(authorization, body.tenant_id)
        attempt = AttemptTuple(body.tenant_id, body.task_id, attempt_id, body.lease_id,
                               body.generation)
        registered = self.children.register_state_key(
            attempt, nonce=body.nonce, public_key=body.public_key, unprotected=body.unprotected
        )
        # `search_tool`, from the setting the `child-key` answer reads
        # (children.py): a switched worker never calls that route, so without
        # it here the tenant's agents silently lose the search tool. A
        # state-only key is no child path, as the tombstone there is not.
        registered = {**registered, "search_tool": not body.unprotected and bool(
            getattr(self._settings, "search_tool_enabled", False))}
        task = self._read(self._task_ref(attempt))
        lease = self._read(self._ref(LEASES, attempt.lease_id))
        reason = fence_reason(task, lease, attempt)
        if reason is not None:
            raise AttemptFenced(f"this attempt no longer owns its task: {reason}")
        assert task is not None and lease is not None
        own = self._own_attempt(attempt, attempt.attempt_id)
        restore = (
            self._own_attempt(attempt, body.restore_attempt_id)
            if body.restore_attempt_id else None
        )
        return {
            "ok": True,
            "registration": registered,
            "task": jsonable(task),
            "fence": fence_view(task),
            "lease": jsonable(lease),
            "attempt": jsonable(own),
            "restore_attempt": jsonable(restore),
            "parked_uploads": self._parked_uploads(attempt),
        }

    def _own_attempt(self, attempt: AttemptTuple, attempt_id: str) -> dict[str, Any] | None:
        """An attempt document of THIS task, or None. Another task's is never served."""
        doc = self._read(self._ref(ATTEMPTS, attempt_id))
        if doc is None or doc.get("tenant_id") != attempt.tenant_id:
            return None
        if doc.get("task_id") not in (None, attempt.task_id):
            return None
        return doc

    def _parked_uploads(self, attempt: AttemptTuple) -> list[dict[str, Any]]:
        """`ControlPlane.parked_uploads`, read by id under this task (#166)."""
        carry = self._task_ref(attempt).collection(CARRY)
        index = self._read(carry.document(CARRY_INDEX))
        if index is None or index.get("tenant_id") != attempt.tenant_id:
            return []
        found: list[tuple[Any, str, list[Any]]] = []
        seen: set[str] = set()
        for item in index.get("attempts") if isinstance(index.get("attempts"), list) else []:
            attempt_id = item.get("attempt_id") if isinstance(item, Mapping) else None
            if (not isinstance(attempt_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",
                                                                    attempt_id)
                    or attempt_id == attempt.attempt_id or attempt_id in seen):
                continue
            seen.add(attempt_id)
            record = self._read(carry.document(attempt_id))
            if (record is None or record.get("tenant_id") != attempt.tenant_id
                    or record.get("task_id") != attempt.task_id
                    or record.get("attempt_id") != attempt_id
                    or not isinstance(record.get("artifacts"), list)):
                continue
            found.append((record.get("at"), attempt_id, record["artifacts"]))
        # Oldest first; a record with no instant last, as the direct path orders them.
        found.sort(key=lambda r: (0, r[0].timestamp()) if isinstance(r[0], datetime) else (1, 0.0))
        return [{"attempt_id": a, "artifacts": jsonable(arts)} for _, a, arts in found]

    # -- state/transition --------------------------------------------------

    def transition(self, call: Call) -> dict[str, Any]:
        body: TransitionBody = call.body
        attempt = call.attempt
        try:
            to = TaskState(body.to)
        except ValueError:
            raise TransitionRefused("to must be a task state") from None
        if to not in (TaskState.DISPATCHED, TaskState.STARTING, TaskState.RUNNING):
            raise TransitionRefused(
                "this route walks LEASED -> DISPATCHED -> STARTING -> RUNNING; a park, a "
                "finish or a retry has its own route"
            )
        detail = _bounded(dict(body.detail), "detail")
        event_id = request_event_id(attempt, "transition", body.request_id)
        now = self._now()
        kind = {TaskState.DISPATCHED: EventType.DISPATCHED, TaskState.STARTING: EventType.STARTING,
                TaskState.RUNNING: EventType.RUNNING}[to]
        event = self._event(attempt, kind, {**detail, "request_id": body.request_id}, at=now,
                            event_id=event_id)

        def _apply(txn: Any) -> dict[str, Any]:
            seen = self._seen(txn, attempt, event_id)
            if seen is not None:
                return self._answer(seen, repeat=True)
            task = self._fenced(txn, attempt)
            current = TaskState(task["state"])
            if current is to:
                return self._answer(task, repeat=True)
            if not worker_may_transition(current, to):
                raise TransitionRefused(
                    f"a worker may not move a task from {current.value} to {to.value}"
                )
            payload: dict[str, Any] = {"state": to.value, "updated_at": now}
            if to is TaskState.STARTING:
                payload["started_at"] = now
            workflow = self._workflow_write(txn, attempt, task, payload, now)
            txn.set(*event)
            txn.update(self._task_ref(attempt), payload)
            if workflow is not None:
                txn.update(*workflow)
            return self._answer({**task, **payload})

        return self._run(_apply)

    def _workflow_write(self, txn: Any, attempt: AttemptTuple, task: Mapping[str, Any],
                        payload: Mapping[str, Any], now: datetime) -> tuple[Any, dict[str, Any]] | None:
        """The step's workflow's derived state, written in this transaction (#2838).

        Reads only, so a caller calls it before its first write; the caller
        applies the returned update beside the task's own."""
        return derived_state_write(
            txn, self._db, tenant_id=attempt.tenant_id, workflow_id=task.get("workflow_id"),
            now=now, changed={attempt.task_id: {**task, **payload}},
        )

    # -- the attempt document ----------------------------------------------

    def _attempt_doc_ok(self, txn: Any, attempt: AttemptTuple) -> dict[str, Any] | None:
        """This attempt's own document as `txn` reads it; 409 if it names
        another tuple. Not fenced on the task: a superseded attempt still
        records its own end and usage, as the direct path does
        (`record_cpu_usage`'s docstring), and its lease being released is the
        normal state after a park or a finish."""
        snap = _snapshot(txn.get(self._ref(ATTEMPTS, attempt.attempt_id)))
        doc = (snap.to_dict() or {}) if snap.exists else None
        if doc is None:
            lease = _snapshot(txn.get(self._ref(LEASES, attempt.lease_id)))
            if not lease.exists or not _lease_is_this_tuples(lease.to_dict() or {}, attempt):
                raise AttemptFenced("this attempt's lease is not this tuple's")
            return None
        if (doc.get("tenant_id") != attempt.tenant_id
                or doc.get("task_id") not in (None, attempt.task_id)
                or doc.get("generation") not in (None, attempt.generation)):
            raise AttemptFenced("this attempt's document names another attempt")
        return doc

    def attempt_start(self, call: Call) -> dict[str, Any]:
        body: AttemptStartBody = call.body
        attempt = call.attempt
        now = self._now()

        def _apply(txn: Any) -> dict[str, Any]:
            task = self._fenced(txn, attempt)
            txn.set(self._ref(ATTEMPTS, attempt.attempt_id), {
                "attempt_id": attempt.attempt_id,
                "task_id": attempt.task_id,
                "tenant_id": attempt.tenant_id,
                "generation": attempt.generation,
                "lease_id": attempt.lease_id,
                "backend": body.backend,
                "created_at": now,
                "execution_name": body.execution_name,
                "started_at": now,
                "completed_at": None,
                "exit_code": None,
                "error": None,
                "peak_rss_bytes": None,
                "peak_disk_bytes": None,
                "oom_near_miss": False,
                "checkpoints": [],
            })
            return self._answer(task)

        return self._run(_apply)

    def checkpoint(self, call: Call) -> dict[str, Any]:
        """`record_checkpoint`: the attempt's record and the task's pointer, in
        ONE fenced transaction. Stricter than the direct path, which merges
        the record before its fenced pointer write: a stale attempt here
        records nothing, since a record no pointer names restores nothing."""
        body: CheckpointBody = call.body
        attempt = call.attempt
        if not _DIGEST.fullmatch(body.archive_sha256):
            raise ValidationFailed("archive_sha256 must be a SHA-256 hex digest (#346)")
        if body.uri.startswith("gs://"):
            # Anchored at the object path, `gs://<bucket>/tenants/<t>/tasks/<task>/`
            # (`checkpoint.py`'s layout). The bucket is not pinned -- swarm-api
            # is not told it -- and restore reads with the tenant's own account,
            # whose bucket grant is prefix-conditioned.
            prefix = re.escape(f"/tenants/{attempt.tenant_id}/tasks/{attempt.task_id}/")
            if not re.match(rf"^gs://[a-z0-9][a-z0-9._-]{{1,221}}[a-z0-9]{prefix}", body.uri):
                raise ValidationFailed("uri must be under this task's own prefix")
        elif not body.uri.startswith("file:///"):
            raise ValidationFailed("uri must be a gs:// or file:/// URI")
        now = self._now()
        event = self._event(attempt, EventType.CHECKPOINT_COMPLETED, {
            "checkpoint_id": body.checkpoint_id, "uri": body.uri,
            "size_bytes": body.size_bytes, "seq": body.seq}, at=now)

        def _apply(txn: Any) -> dict[str, Any]:
            task = self._fenced(txn, attempt)
            doc = self._attempt_doc_ok(txn, attempt) or {}
            existing = [c for c in doc.get("checkpoints") or [] if isinstance(c, str)]
            if body.checkpoint_id not in existing:
                existing.append(body.checkpoint_id)
            stored = doc.get("checkpoint_sha256")
            digests = {k: v for k, v in (stored.items() if isinstance(stored, Mapping) else ())
                       if isinstance(k, str) and isinstance(v, str)}
            digests[body.checkpoint_id] = body.archive_sha256
            txn.set(self._ref(ATTEMPTS, attempt.attempt_id), {
                "checkpoints": existing,
                "checkpoint_sha256": digests,
                "tenant_id": attempt.tenant_id,
                "task_id": attempt.task_id,
                "attempt_id": attempt.attempt_id,
            }, merge=True)
            txn.update(self._task_ref(attempt), {"latest_checkpoint": body.uri, "updated_at": now})
            txn.set(*event)
            return self._answer(task)

        return self._run(_apply)

    def usage(self, call: Call) -> dict[str, Any]:
        """`record_resource_usage`, `record_cpu_usage`, `record_spend`: named
        fields only, typed as the frozen `Attempt` types them. `cost_usd` is
        the runner's figure, as the direct path stores it: swarm-api holds no
        price table to recompute it from (§4.2 asked for one "where a price
        is known"; none is)."""
        body: UsageBody = call.body
        attempt = call.attempt
        allowed = USAGE_FIELDS[body.kind]
        unknown = sorted(set(body.fields) - set(allowed))
        if unknown:
            raise ValidationFailed(f"{body.kind} usage takes only {', '.join(allowed)}")
        doc: dict[str, Any] = {}
        for key, value in body.fields.items():
            if key == "oom_near_miss":
                if not isinstance(value, bool):
                    raise ValidationFailed("oom_near_miss must be a boolean")
                doc[key] = value
            elif key == "cpu_limit_source":
                if value not in CPU_LIMIT_SOURCES:
                    raise ValidationFailed("cpu_limit_source must be cgroup or resource_class")
                doc[key] = value
            elif key in ("cost_usd", "cpu_seconds", "peak_cpu_cores", "mean_cpu_cores",
                         "cpu_limit_cores"):
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                    raise ValidationFailed(f"{key} must be a non-negative number")
                doc[key] = float(value)
            else:
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValidationFailed(f"{key} must be a non-negative integer")
                doc[key] = value
        if body.kind == "cpu":
            if "cpu_limit_source" in doc and "cpu_limit_cores" not in doc:
                raise ValidationFailed("cpu_limit_source is written only beside cpu_limit_cores")
            if not any(k not in ("cpu_limit_cores", "cpu_limit_source") for k in doc):
                raise ValidationFailed("cpu usage needs a measured figure, not a limit alone")
            doc["cpu_measured_at"] = self._now()
        if not doc:
            raise ValidationFailed("fields is empty")
        doc["tenant_id"] = attempt.tenant_id

        def _apply(txn: Any) -> dict[str, Any]:
            self._attempt_doc_ok(txn, attempt)
            txn.set(self._ref(ATTEMPTS, attempt.attempt_id), doc, merge=True)
            return {"ok": True}

        return self._run(_apply)

    def attempt_end(self, call: Call) -> dict[str, Any]:
        body: AttemptEndBody = call.body
        attempt = call.attempt
        now = self._now()

        def _apply(txn: Any) -> dict[str, Any]:
            self._attempt_doc_ok(txn, attempt)
            txn.set(self._ref(ATTEMPTS, attempt.attempt_id), {
                "completed_at": now, "exit_code": body.exit_code, "error": body.error,
                "tenant_id": attempt.tenant_id}, merge=True)
            return {"ok": True}

        return self._run(_apply)

    # -- the lease -----------------------------------------------------------

    def heartbeat(self, call: Call) -> dict[str, Any]:
        """`ControlPlane.heartbeat`: extend THIS tuple's lease, by the lease
        timeout, only while this attempt owns its task; 409, nothing written,
        otherwise. Stricter than the direct beat, which checks the lease alone:
        a lease the reconciler has not yet released under a task that moved
        to a newer generation is not extended here (invariant 5)."""
        attempt = call.attempt
        now = self._now()
        payload = {"heartbeat_at": now,
                   "expires_at": now + timedelta(seconds=self._lease_extension)}
        lease_ref = self._ref(LEASES, attempt.lease_id)

        def _apply(txn: Any) -> dict[str, Any]:
            task = self._fenced(txn, attempt)
            txn.update(lease_ref, payload)
            return self._answer(task, expires_at=payload["expires_at"].isoformat())

        return self._run(_apply)

    def release(self, call: Call) -> dict[str, Any]:
        """`ControlPlane.release_lease`: THIS tuple's lease and the pools IT
        names. The body can name no pool and no other lease (the strict model,
        and the proof binds the tuple). Not fenced on the task, as the direct
        path is not: a release is how an attempt gives back what it holds."""
        body: ReleaseBody = call.body
        attempt = call.attempt
        if not _REASON.fullmatch(body.reason):
            raise ValidationFailed("reason must be a short release reason")
        now = self._now()

        def _apply(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(self._ref(LEASES, attempt.lease_id)))
            if not snap.exists or not _lease_is_this_tuples(snap.to_dict() or {}, attempt):
                raise AttemptFenced("the lease is not this attempt's")
            events: list[tuple[Any, dict[str, Any]]] = []
            released = self._release(txn, attempt, body.reason, now=now, events=events)
            for event in events:
                txn.set(*event)
            return {"ok": True, "released": released}

        return self._run(_apply)

    # -- parks, finishes, retries --------------------------------------------

    def park(self, call: Call) -> dict[str, Any]:
        """Every park the worker makes, in ONE fenced transaction (§4.2):
        PARKED (or READY, a person's return), the refund where the reason
        refunds, the carry, the attempt's end, the events and the lease
        release through the one release function. An injected failure
        anywhere leaves none of it.

        The refund rule is the reason's, with the server's bound (§4.4):
        CHILDREN_INCOMPLETE is `park_awaiting_children`, CI_PENDING is
        `park_ci_pending` (which needs `ci`), HUMAN_REQUIRED is `handoff`'s
        human park, and DEPENDENCY_INCOMPLETE whose detail carries a
        `release_wait` block is `park_release_wait` (AU7-REFUND),
        PROVIDER_QUOTA_EXHAUSTED is `ControlPlane.park`'s quota refund
        (#2350); any other reason is `ControlPlane.park`, no refund.
        """
        body: ParkBody = call.body
        attempt = call.attempt
        try:
            reason = ParkReason(body.reason)
        except ValueError:
            raise ValidationFailed("reason must be a ParkReason value") from None
        if (reason is ParkReason.CI_PENDING) != (body.ci is not None):
            raise ValidationFailed("ci is sent with a CI_PENDING park, and only with one")
        detail = _bounded(dict(body.detail), "detail")
        uploads = _bounded(list(body.uploads), "uploads") if body.uploads else None
        announced = [self._event(attempt, _event_type(e.type), _bounded(dict(e.detail), "detail"),
                                 at=self._now()) for e in body.announce]
        event_id = request_event_id(attempt, "park", body.request_id)
        now = self._now()

        def _apply(txn: Any) -> dict[str, Any]:
            seen = self._seen(txn, attempt, event_id)
            if seen is not None:
                return self._answer(seen, repeat=True)
            task = self._fenced(txn, attempt)
            current = TaskState(task["state"])
            if not worker_may_transition(current, TaskState.PARKED):
                raise TransitionRefused(f"a worker may not park a task that is {current.value}")
            index_ref = self._task_ref(attempt).collection(CARRY).document(CARRY_INDEX)
            index = None
            if uploads:
                isnap = _snapshot(txn.get(index_ref))
                index = (isnap.to_dict() or {}) if isnap.exists else None
                if index is not None and index.get("tenant_id") != attempt.tenant_id:
                    raise StateConflict("the task's carry index is another tenant's")
            payload, event_kind, event_detail, release_reason = self._park_payload(
                task, reason, body, detail, attempt, now)
            if event_kind is EventType.READY and not worker_may_transition(current,
                                                                           TaskState.READY):
                raise TransitionRefused(f"a worker may not requeue a task that is {current.value}")
            workflow = self._workflow_write(txn, attempt, task, payload, now)
            # Every read above; the release reads the lease and its pools, then writes.
            events: list[tuple[Any, dict[str, Any]]] = list(announced)
            events.append(self._event(attempt, event_kind, event_detail, at=now,
                                      event_id=event_id))
            self._release(txn, attempt, release_reason, now=now, events=events)
            txn.update(self._task_ref(attempt), payload)
            if workflow is not None:
                txn.update(*workflow)
            if uploads:
                self._record_carry(txn, attempt, index_ref, index, uploads, now)
            txn.set(self._ref(ATTEMPTS, attempt.attempt_id), {
                "completed_at": now, "exit_code": 75, "error": reason.value,
                "tenant_id": attempt.tenant_id}, merge=True)
            for event in events:
                txn.set(*event)
            return self._answer({**task, **payload},
                                refunded=event_detail.get("attempt_refunded", False))

        return self._run(_apply)

    @staticmethod
    def _park_payload(task: Mapping[str, Any], reason: ParkReason, body: ParkBody,
                      detail: dict[str, Any], attempt: AttemptTuple, now: datetime
                      ) -> tuple[dict[str, Any], EventType, dict[str, Any], str]:
        metadata = dict(task.get("metadata") or {})
        attempt_count = _count(task.get("attempt_count"))
        payload: dict[str, Any] = {
            "state": TaskState.PARKED.value,
            "park_reason": reason.value,
            "current_lease_id": None,
            "blocked_by": [{"reason": reason.value, **detail}],
            "updated_at": now,
        }
        extra: dict[str, Any] = {}
        refund = False
        release_wait = (reason is ParkReason.DEPENDENCY_INCOMPLETE
                        and isinstance(detail.get(RELEASE_WAIT), Mapping))
        if reason is ParkReason.CHILDREN_INCOMPLETE:
            used = _count(metadata.get(CHILD_AWAIT_RESUMES))
            refund = used < CHILD_AWAIT_MAX_RESUMES and attempt_count > 0
            payload["next_eligible_at"] = now
            if refund:
                metadata[CHILD_AWAIT_RESUMES] = used + 1
                payload["metadata"] = metadata
            extra = {CHILD_AWAIT_RESUMES: used + (1 if refund else 0)}
        elif reason is ParkReason.HUMAN_REQUIRED:
            used = _count(metadata.get(HUMAN_WAIT_RESUMES))
            refund = used < HUMAN_MAX_RESUMES and attempt_count > 0
            payload["next_eligible_at"] = now
            if refund:
                metadata[HUMAN_WAIT_RESUMES] = used + 1
                payload["metadata"] = metadata
            extra = {HUMAN_WAIT_RESUMES: used + (1 if refund else 0)}
        elif release_wait:
            # An agent's release wait (AU7-REFUND): refunded within the
            # server's bound. Its instant is the park's, as the direct path
            # writes it; only the merge-wake tick ends the wait.
            used = _count(metadata.get(RELEASE_WAIT_RESUMES))
            refund = used < RELEASE_WAIT_MAX_RESUMES and attempt_count > 0
            payload["next_eligible_at"] = now
            if refund:
                metadata[RELEASE_WAIT_RESUMES] = used + 1
                payload["metadata"] = metadata
            extra = {RELEASE_WAIT_RESUMES: used + (1 if refund else 0)}
        elif reason is ParkReason.CI_PENDING:
            ci = body.ci
            assert ci is not None
            counter = ("slot_waits" if ci.code == MERGE_SLOT_WAIT
                       else "train_waits" if ci.code == TRAIN_WAITING else "wakes")
            bound = {"slot_waits": SLOT_MAX_WAITS, "train_waits": TRAIN_MAX_WAITS,
                     "wakes": CI_MAX_WAKES}[counter]
            ci_clock = counter == "wakes"
            last = _mapping(metadata.get(MERGE_WAIT))
            counts = {name: _count(last.get(name)) for name in ("wakes", "slot_waits",
                                                                "train_waits")}
            refund = counts[counter] < bound and attempt_count > 0
            if refund:
                counts[counter] += 1
            first = last.get("first_parked_at")
            names = [str(n)[:256] for n in ci.pending]
            metadata[MERGE_WAIT] = {
                "code": ci.code, "head": ci.head, "pull_request": ci.pull_request,
                "pending": names, **counts, "updates": _count(last.get("updates")),
                "first_parked_at": (first if isinstance(first, datetime) else now)
                if ci_clock else None,
                "parked_at": now,
            }
            payload["metadata"] = metadata
            payload["next_eligible_at"] = now + timedelta(seconds=CI_FALLBACK_SECONDS)
            payload["blocked_by"] = [{"reason": reason.value, "code": ci.code, "head": ci.head,
                                      "pending": names}]
            extra = {"wakes": counts[counter], "code": ci.code, "head": ci.head,
                     "pending": names}
        else:
            if body.next_eligible_at is None:
                raise ValidationFailed(f"a {reason.value} park needs next_eligible_at")
            payload["next_eligible_at"] = body.next_eligible_at
            if reason is ParkReason.PROVIDER_QUOTA_EXHAUSTED:
                # #2350: refunded within the server's bound; past it the
                # park spends an attempt, as every quota park did before.
                used = _count(metadata.get(QUOTA_PARK_RESUMES))
                refund = used < QUOTA_MAX_RESUMES and attempt_count > 0
                if refund:
                    metadata[QUOTA_PARK_RESUMES] = used + 1
                    payload["metadata"] = metadata
                extra = {QUOTA_PARK_RESUMES: used + (1 if refund else 0)}
        if refund:
            payload["attempt_count"] = attempt_count - 1
        kind = EventType.PARKED
        event_detail: dict[str, Any] = {
            "reason": reason.value, "next_eligible_at": payload["next_eligible_at"],
            **({"attempt_refunded": refund} if release_wait or reason in (
                ParkReason.CHILDREN_INCOMPLETE, ParkReason.CI_PENDING,
                ParkReason.HUMAN_REQUIRED, ParkReason.PROVIDER_QUOTA_EXHAUSTED) else {}),
            **extra, **detail}
        release_reason = f"parked:{reason.value}"
        if reason is ParkReason.HUMAN_REQUIRED:
            mark = metadata.get(HUMAN_RETURNED)
            left = _count(task.get("max_attempts") or 1) - _count(
                payload.get("attempt_count", attempt_count))
            if (isinstance(mark, Mapping) and mark.get("attempt_id") == attempt.attempt_id
                    and mark.get("generation") == attempt.generation and left > 0):
                # LB-RETURN: a person returned control while this attempt parked.
                by = str(mark.get("by") or "")
                metadata.pop(HUMAN_RETURNED, None)
                metadata[HUMAN_READY] = {"by": by, "at": now.isoformat()}
                payload.update({"state": TaskState.READY.value, "park_reason": None,
                                "next_eligible_at": None, "blocked_by": [],
                                "metadata": metadata})
                kind = EventType.READY
                event_detail = {"reason": "human_returned", "park_reason": reason.value,
                                "by": by, "attempt_refunded": refund, **extra, **detail}
                release_reason = "requeued:human_returned"
        return payload, kind, event_detail, release_reason

    def _record_carry(self, txn: Any, attempt: AttemptTuple, index_ref: Any,
                      index: Mapping[str, Any] | None, uploads: list[Any], now: datetime) -> None:
        """`control._record_parked_uploads`: the manifest and the index, by id."""
        carry = self._task_ref(attempt).collection(CARRY)
        txn.set(carry.document(attempt.attempt_id), {
            "tenant_id": attempt.tenant_id, "task_id": attempt.task_id,
            "attempt_id": attempt.attempt_id, "at": now, "artifacts": list(uploads)})
        listed = (index or {}).get("attempts")
        attempts = [item for item in (listed if isinstance(listed, list) else [])
                    if isinstance(item, Mapping) and item.get("attempt_id") != attempt.attempt_id]
        attempts.append({"attempt_id": attempt.attempt_id, "at": now})
        txn.set(index_ref, {"tenant_id": attempt.tenant_id, "task_id": attempt.task_id,
                            "attempts": attempts, "updated_at": now})

    def finish(self, call: Call) -> dict[str, Any]:
        """`ControlPlane.finish`: the terminal state, the attempt's end, the
        event and the release, in ONE fenced transaction."""
        body: FinishBody = call.body
        attempt = call.attempt
        try:
            to = TaskState(body.state)
        except ValueError:
            raise TransitionRefused("state must be a task state") from None
        if to not in TERMINAL_STATES:
            raise TransitionRefused(f"{to.value} is not terminal; finish ends the task")
        cause = _end_cause(body.end_cause)
        summary = (_bounded(dict(body.result_summary), "result_summary")
                   if body.result_summary is not None else None)
        event_id = request_event_id(attempt, "finish", body.request_id)
        now = self._now()
        kind = {TaskState.SUCCEEDED: EventType.SUCCEEDED, TaskState.FAILED: EventType.FAILED,
                TaskState.CANCELLED: EventType.CANCELLED,
                TaskState.DEAD_LETTERED: EventType.DEAD_LETTERED}[to]

        def _apply(txn: Any) -> dict[str, Any]:
            seen = self._seen(txn, attempt, event_id)
            if seen is not None:
                return self._answer(seen, repeat=True)
            task = self._fenced(txn, attempt)
            current = TaskState(task["state"])
            if not worker_may_transition(current, to):
                raise TransitionRefused(
                    f"a worker may not move a task from {current.value} to {to.value}"
                )
            payload: dict[str, Any] = {
                "state": to.value, "updated_at": now, "completed_at": now,
                "current_lease_id": None, "last_error": body.error,
                "end_cause": cause.value if cause is not None else None,
            }
            if summary is not None:
                payload["result_summary"] = summary
            events = [self._event(attempt, kind, {"exit_code": body.exit_code,
                                                  "error": body.error,
                                                  "request_id": body.request_id},
                                  at=now, event_id=event_id)]
            workflow = self._workflow_write(txn, attempt, task, payload, now)
            self._release(txn, attempt, f"terminal:{to.value}", now=now, events=events)
            txn.update(self._task_ref(attempt), payload)
            if workflow is not None:
                txn.update(*workflow)
            txn.set(self._ref(ATTEMPTS, attempt.attempt_id), {
                "completed_at": now, "exit_code": body.exit_code, "error": body.error,
                "tenant_id": attempt.tenant_id}, merge=True)
            for event in events:
                txn.set(*event)
            return self._answer({**task, **payload})

        return self._run(_apply)

    def retry(self, call: Call) -> dict[str, Any]:
        """`ControlPlane.fail_retryably`: CANCELLED on a requested cancel,
        FAILED once retries are exhausted (`models.retries_exhausted`, the
        scheduler's own predicate), READY otherwise -- decided from the
        document, with `max_attempts` the stored (signed) value -- then the
        attempt's end, the event and the release, in ONE fenced transaction."""
        body: RetryBody = call.body
        attempt = call.attempt
        cause = _end_cause(body.end_cause)
        if not _REASON.fullmatch(body.cause):
            raise ValidationFailed("cause must be a short cause name")
        summary = (_bounded(dict(body.result_summary), "result_summary")
                   if body.result_summary is not None else None)
        detail = _bounded(dict(body.detail), "detail")
        event_id = request_event_id(attempt, "retry", body.request_id)
        now = self._now()

        def _apply(txn: Any) -> dict[str, Any]:
            seen = self._seen(txn, attempt, event_id)
            if seen is not None:
                return self._answer(seen, repeat=True)
            task = self._fenced(txn, attempt)
            current = TaskState(task["state"])
            attempt_count = _count(task.get("attempt_count"))
            max_attempts = _count(task.get("max_attempts", 3))
            if task.get("cancel_requested"):
                target = TaskState.CANCELLED
            elif retries_exhausted(attempt_count, max_attempts):
                target = TaskState.FAILED
            else:
                target = TaskState.READY
            if not worker_may_transition(current, target):
                raise TransitionRefused(
                    f"a worker may not move a task from {current.value} to {target.value}"
                )
            payload: dict[str, Any] = {
                "state": target.value, "updated_at": now, "current_lease_id": None,
                "last_error": (f"cancelled on request; {body.error}"
                               if target is TaskState.CANCELLED else body.error),
            }
            if summary is not None:
                payload["result_summary"] = summary
            if target is TaskState.READY:
                payload["next_eligible_at"] = now + timedelta(seconds=body.retry_delay_seconds)
                release_reason = f"retry:{body.cause}"
            else:
                payload["completed_at"] = now
                payload["next_eligible_at"] = None
                ended = cancel_end_cause(task) if target is TaskState.CANCELLED else cause
                payload["end_cause"] = ended.value if ended is not None else None
                release_reason = f"terminal:{target.value}"
            kind = {TaskState.READY: EventType.RETRYING, TaskState.FAILED: EventType.FAILED,
                    TaskState.CANCELLED: EventType.CANCELLED}[target]
            events = [self._event(attempt, kind, {
                "exit_code": body.exit_code, "error": body.error, "cause": body.cause,
                **detail, "to_state": target.value, "attempt_count": attempt_count,
                "max_attempts": max_attempts, "retries_exhausted": target is TaskState.FAILED,
                "request_id": body.request_id}, at=now, event_id=event_id)]
            workflow = self._workflow_write(txn, attempt, task, payload, now)
            self._release(txn, attempt, release_reason, now=now, events=events)
            txn.update(self._task_ref(attempt), payload)
            if workflow is not None:
                txn.update(*workflow)
            txn.set(self._ref(ATTEMPTS, attempt.attempt_id), {
                "completed_at": now, "exit_code": body.exit_code, "error": body.error,
                "tenant_id": attempt.tenant_id}, merge=True)
            for event in events:
                txn.set(*event)
            return self._answer({**task, **payload})

        return self._run(_apply)

    def event(self, call: Call) -> dict[str, Any]:
        """`ControlPlane.emit`: one event in this task's stream, stamped with
        this tuple. Its id is the caller's, so a retry writes it once. Not
        fenced, as `emit` is not: it is an audit record, carrying its own
        generation."""
        body: EventBody = call.body
        attempt = call.attempt
        if not _EVENT_ID.fullmatch(body.event_id):
            raise ValidationFailed("event_id must be an evt_ id")
        kind = _event_type(body.type)
        detail = _bounded(dict(body.detail), "detail")
        ref, document = self._event(attempt, kind, detail, at=self._now(),
                                    event_id=body.event_id)

        def _apply(txn: Any) -> dict[str, Any]:
            task_snap = _snapshot(txn.get(self._task_ref(attempt)))
            task = (task_snap.to_dict() or {}) if task_snap.exists else None
            if task is None or task.get("tenant_id") != attempt.tenant_id:
                raise AttemptFenced("the task is not this attempt's")
            if _snapshot(txn.get(ref)).exists:
                return {"ok": True, "repeat": True}
            txn.set(ref, document)
            return {"ok": True}

        return self._run(_apply)

    # -- reads ----------------------------------------------------------------

    def _own_task(self, attempt: AttemptTuple) -> dict[str, Any]:
        task = self._read(self._task_ref(attempt))
        if task is None or task.get("tenant_id") != attempt.tenant_id:
            raise AttemptFenced("the task is not this attempt's")
        return task

    def poll(self, attempt: AttemptTuple, provider: str | None) -> dict[str, Any]:
        """`ControlPlane.poll`: the fence view, the lease, and this tenant's quota."""
        task = self._read(self._task_ref(attempt))
        if task is not None and task.get("tenant_id") != attempt.tenant_id:
            task = None
        lease = self._read(self._ref(LEASES, attempt.lease_id))
        if lease is not None and not _lease_is_this_tuples(lease, attempt):
            lease = None
        quota = None
        if provider is not None:
            if not _PROVIDER.fullmatch(provider):
                raise ValidationFailed("provider must be a provider name")
            quota = self._read(self._ref(QUOTA, f"{provider}:{attempt.tenant_id}"))
        return {
            "ok": True,
            "task": fence_view(task),
            "fenced": fence_reason(task, lease, attempt),
            "lease": None if lease is None else {
                "released": lease.get("released_at") is not None,
                "expires_at": jsonable(lease.get("expires_at")),
            },
            "quota": jsonable(quota),
        }

    def context(self, attempt: AttemptTuple) -> dict[str, Any]:
        """What the worker reads of its OWN tenant's settings, keys and grant
        (`secrets.load_tenant`, `jevkey`, `judge/attempt.py`, `graphstage.py`).
        Every id is derived from the tuple and the task's signed fields."""
        task = self._own_task(attempt)
        tenant_id = attempt.tenant_id
        tenant = self._read(self._ref(TENANTS, tenant_id))
        if tenant is not None and tenant.get("tenant_id") not in (None, tenant_id):
            raise StateConflict("tenants/<id> declares another tenant")
        keys: dict[str, Any] = {}
        own_key = _typesafe_secret(tenant_id)
        keys[own_key] = jsonable(self._read(self._ref(PROVIDER_KEYS, own_key)))
        submitted_by = task.get("submitted_by")
        personal: str | None = None
        if isinstance(submitted_by, str) and "@" in submitted_by:
            email = submitted_by.strip().lower()
            candidate = personal_tenant_id(email)
            doc = self._read(self._ref(TENANTS, candidate))
            principal = (doc or {}).get("principal")
            if ((doc or {}).get("kind") == "user" and isinstance(principal, str)
                    and principal.strip().lower() == email):
                personal = candidate
                key = _typesafe_secret(candidate)
                keys[key] = jsonable(self._read(self._ref(PROVIDER_KEYS, key)))
        grant = None
        repo = owner_repo(task.get("repository_url"))
        if repo is not None and isinstance(submitted_by, str) and "@" in submitted_by:
            grant_id = grant_id_for(tenant_id, submitted_by, repo_id_for(tenant_id, *repo))
            found = self._read(self._ref(GRANTS, grant_id))
            if found is not None and found.get("tenant_id") in (None, tenant_id):
                grant = {"id": grant_id, **found}
        return {
            "ok": True,
            "tenant": jsonable(tenant),
            "workspace_settings": jsonable(self._read(self._ref(WORKSPACE_SETTINGS, tenant_id))),
            "tenant_settings": jsonable(self._read(self._ref(TENANT_SETTINGS, tenant_id))),
            "provider_keys": keys,
            "personal_tenant_id": personal,
            "forge_grant": jsonable(grant),
        }

    def upstream(self, attempt: AttemptTuple, upstream_task_id: str) -> dict[str, Any]:
        """`fetch_parent_states` and `inputs.fetch_upstream_task`: ONLY an id
        this task declares -- its `depends_on`, its `metadata.input_from`,
        its dispatch block's `input_parents` or its merge step's
        `merge_target.pull_request`, all covered by the spec signature --
        or one `_derived_upstream` derives, and only one of this
        tenant. Anything else is the same 403, whether or not it exists.

        Every id is taken from the SIGNED copy of the document it is read
        from (SIGNED-UPSTREAM, BGD 2026-10-26, part of #2373): a task document
        is tenant-writable after signing (docs/design/tenant-settings-
        integrity.md §1.1), so a field rewritten then would otherwise widen
        what this attempt's reads may name to any task of its tenant
        (invariant 9). The attempt's own task is verified once, here, under
        the tuple's id; one whose spec does not verify declares nothing and
        serves only itself, whose id is the tuple's rather than a field. Each
        document a walk follows is verified before its fields are; one that
        fails is counted in the log, never skipped as if absent.
        swarm-api rewrites none of the signed fields once a task is stored:
        it signs at submission (`specsigning`), and every later write to a
        task (`transition`, `park`, `finish`, `handoff`) touches unsigned
        keys only."""
        task = self._own_task(attempt)
        if upstream_task_id == attempt.task_id:
            return {"ok": True, "task": jsonable(task)}
        refusal = spec_refusal(self._settings, task, attempt.task_id)
        if refusal is not None:
            # The reason names the check, never a value from the document.
            log.warning("state upstream unverified own task tenant=%s task=%s attempt=%s: %s",
                        attempt.tenant_id, attempt.task_id, attempt.attempt_id, refusal)
            raise UpstreamNotDeclared(
                "this task's spec does not verify, so it declares no upstream")
        declared: set[str] = {d for d in task.get("depends_on") or [] if isinstance(d, str)}
        metadata = _mapping(task.get("metadata"))
        declared |= {k for k in _mapping(metadata.get("input_from")) if isinstance(k, str)}
        dispatch = _mapping(metadata.get("dispatch"))
        declared |= {k for k in _mapping(dispatch.get("input_parents")) if isinstance(k, str)}
        # A merge-only continuation (`issueci.merge_workflow`,
        # `MergeSources(continued=True)`) depends on nothing: the task it
        # merges, in another workflow, is named only here, and is the
        # merge's first read (#2379 F01, F02). Signed like the rest; the
        # tenant check below still holds it to this tenant.
        target = _mapping(dispatch.get("merge_target")).get("pull_request")
        if isinstance(target, str) and target:
            declared.add(target)
        refused = UpstreamNotDeclared("that task is not one this task declares as upstream")
        walk = _Walk()
        if upstream_task_id not in declared and not self._derived_upstream(
                attempt, task, upstream_task_id, walk):
            if walk.unverified:
                log.warning("state upstream unverified=%d tenant=%s task=%s attempt=%s: a "
                            "document the rules followed failed its spec signature",
                            walk.unverified, attempt.tenant_id, attempt.task_id,
                            attempt.attempt_id)
            raise refused
        doc = self._read(self._ref(TASKS, upstream_task_id))
        if doc is None:
            return {"ok": True, "task": None}
        if doc.get("tenant_id") != attempt.tenant_id:
            raise refused
        return {"ok": True, "task": jsonable(doc)}

    def _derived_upstream(self, attempt: AttemptTuple, task: Mapping[str, Any],
                          upstream_task_id: str, walk: "_Walk") -> bool:
        """The reads a worker makes past its declared set (#1457, owner
        decision 2026-10-17), each id derived HERE from documents this
        service reads, never taken from the request. `task` is the
        attempt's own, already verified by `upstream`; every other task
        document whose fields a rule follows is verified by the id it was
        read by (`_verified`) before it is, and one that fails stops that
        rule there and is counted in `walk`:

          * an index run's base version's task: the `task_id` of the
            `index_versions/<sha>` entry, under this tenant's registration of
            the signed repository, for the one base line of the signed prompt
            (`lifecycle._stage_base`), when that task's own signed spec is an
            index run of this repository at that commit;
          * an ancestor along `builds_on` (`lifecycle._builds_on_base`): from
            this task's own signed `dispatch.builds_on`, following each
            verified ancestor's `builds_on` only through this tenant's tasks
            of this workflow, at most `BUILDS_ON_MAX_DEPTH` tasks deep;
          * the verdict gate's review task (`lifecycle._reviewed_patches`):
            the signed `dispatch.verdict_gate.task_id`, when that task's
            signed `workflow_id` is this task's workflow;
          * a workflow ancestor along `depends_on` (#2070,
            `owner_questions.workflow_ancestors` and the merge's re-read of
            the answers to `needs_owner`): from this task's own signed
            `depends_on`, following each verified ancestor's `depends_on`
            only through this tenant's tasks of this workflow, one batched
            read per level, at most `WORKFLOW_ANCESTORS_MAX` documents read;
          * a merge step's own task and the branch it merges (#2373,
            `agent_worker.merge._own_doc`, `_fix_round_heads`,
            `_recorded_fix_heads`): the attempt's own task id, from the
            tuple (served by `upstream` before any rule); the root whose
            `swarm/<root>` branch the pull request is on, from this task's
            signed `dispatch.merge_target.pull_request` and that task's own
            signed `dispatch.continues`; and any task of this tenant whose
            signed dispatch block `continues` that root -- a merge's CI-fix
            or conflict fix round, or a PR-side fixer of the same branch.
            Every task document is tenant-writable after signing
            (docs/design/tenant-settings-integrity.md §1.1), so each of the
            three `metadata.dispatch` fields followed is read only from a
            document whose spec signature verifies HERE, by the id it was
            read by (`verdictpublish.spec_refusal`): the merge step's own
            (in `upstream`), the opener's and the candidate's. Unverified, a
            rewrite of any one would serve any same-tenant task it named.
            The worker still holds each one to its signed spec as well.

        The caller still refuses a task of another tenant, so a derived id
        serves only this tenant's document.
        """
        return (self._is_index_base(attempt, task, upstream_task_id, walk)
                or self._is_builds_on_ancestor(attempt, task, upstream_task_id, walk)
                or self._is_gate_review(attempt, task, upstream_task_id, walk)
                or self._is_workflow_ancestor(attempt, task, upstream_task_id, walk)
                or self._is_merge_fix_record(attempt, task, upstream_task_id, walk))

    def _is_merge_fix_record(self, attempt: AttemptTuple, task: Mapping[str, Any],
                             upstream_task_id: str, walk: "_Walk") -> bool:
        target = _mapping(_mapping(_mapping(task.get("metadata")).get("dispatch"))
                          .get("merge_target")).get("pull_request")
        if not isinstance(target, str) or not target:
            return False
        opener = self._read(self._ref(TASKS, target))
        if opener is None or not self._verified(attempt, opener, target, walk):
            return False
        continued = _mapping(_mapping(opener.get("metadata")).get("dispatch")).get("continues")
        root = continued if isinstance(continued, str) and continued else target
        if upstream_task_id == root:
            return True
        doc = self._read(self._ref(TASKS, upstream_task_id))
        if doc is None or not self._verified(attempt, doc, upstream_task_id, walk):
            return False
        return _mapping(_mapping(doc.get("metadata")).get("dispatch")).get("continues") == root

    def _signed(self, attempt: AttemptTuple, doc: Mapping[str, Any], task_id: str) -> bool:
        """`doc`, read by `task_id`, is this tenant's and its spec signature verifies:
        its `tenant_id` and `metadata.dispatch` are the ones swarm-api signed."""
        return (doc.get("tenant_id") == attempt.tenant_id
                and spec_refusal(self._settings, doc, task_id) is None)

    def _verified(self, attempt: AttemptTuple, doc: Mapping[str, Any], task_id: str,
                  walk: "_Walk") -> bool:
        """`_signed`, counting in `walk` a document of this tenant that failed:
        another tenant's is refused, as it always was, and is not this tenant's
        rewrite to count."""
        if self._signed(attempt, doc, task_id):
            return True
        if doc.get("tenant_id") == attempt.tenant_id:
            walk.unverified += 1
        return False

    def _read_tasks(self, task_ids: list[str]) -> dict[str, dict[str, Any]]:
        """The task documents of `task_ids` that exist, by id, in ONE batched read."""
        if not task_ids:
            return {}
        refs = [self._ref(TASKS, task_id) for task_id in task_ids]
        try:
            snaps = list(self._db.get_all(refs))
        except gexc.GoogleAPIError as exc:
            raise StateUnavailable(
                f"the state store did not answer ({type(exc).__name__}); retry"
            ) from None
        return {snap.id: snap.to_dict() or {} for snap in snaps if snap.exists}

    def _is_index_base(self, attempt: AttemptTuple, task: Mapping[str, Any],
                       upstream_task_id: str, walk: "_Walk") -> bool:
        prompt = _mapping(task.get("input")).get("prompt")
        bases = set(_BASE_SHA_LINE.findall(prompt)) if isinstance(prompt, str) else set()
        repo = owner_repo(task.get("repository_url"))
        if len(bases) != 1 or repo is None:
            return False
        ref = self._ref(REPOSITORIES, repo_id_for(attempt.tenant_id, *repo))
        registration = self._read(ref)
        if registration is None or registration.get("tenant_id") != attempt.tenant_id:
            return False
        base = bases.pop()
        version = self._read(ref.collection(VERSIONS_COLLECTION).document(base))
        if version is None or version.get("task_id") != upstream_task_id:
            return False
        # `index_versions/<sha>` carries no signature, so the id it names is
        # held to that task's own signed spec: an index run
        # (`repoindex.indexer_task`) of this repository at exactly this commit.
        doc = self._read(self._ref(TASKS, upstream_task_id))
        return (doc is not None and self._verified(attempt, doc, upstream_task_id, walk)
                and doc.get("runner_profile") == INDEXER_PROFILE
                and doc.get("repository_ref") == base
                and owner_repo(doc.get("repository_url")) == repo)

    def _is_builds_on_ancestor(self, attempt: AttemptTuple, task: Mapping[str, Any],
                               upstream_task_id: str, walk: "_Walk") -> bool:
        workflow_id = task.get("workflow_id")
        current = _builds_on(_mapping(task.get("metadata")))
        seen: set[str] = {attempt.task_id}
        for _ in range(BUILDS_ON_MAX_DEPTH):
            if current is None or current in seen:
                return False
            if current == upstream_task_id:
                return True
            seen.add(current)
            doc = self._read(self._ref(TASKS, current))
            if (doc is None or not self._verified(attempt, doc, current, walk)
                    or not isinstance(workflow_id, str) or not workflow_id
                    or doc.get("workflow_id") != workflow_id):
                return False
            current = _builds_on(_mapping(doc.get("metadata")))
        return False

    def _is_workflow_ancestor(self, attempt: AttemptTuple, task: Mapping[str, Any],
                              upstream_task_id: str, walk: "_Walk") -> bool:
        workflow_id = task.get("workflow_id")
        if not isinstance(workflow_id, str) or not workflow_id:
            return False
        level = {d for d in task.get("depends_on") or [] if isinstance(d, str)}
        seen: set[str] = {attempt.task_id}
        while level and len(seen) <= WORKFLOW_ANCESTORS_MAX:
            pending = sorted(level - seen)
            # The same bound as one read at a time: at most
            # WORKFLOW_ANCESTORS_MAX documents across the walk.
            batch = pending[:WORKFLOW_ANCESTORS_MAX + 1 - len(seen)]
            seen.update(batch)
            docs = self._read_tasks(batch)
            following: set[str] = set()
            for current in batch:
                doc = docs.get(current)
                if (doc is None or not self._verified(attempt, doc, current, walk)
                        or doc.get("workflow_id") != workflow_id):
                    continue
                if current == upstream_task_id:
                    return True
                following |= {d for d in doc.get("depends_on") or [] if isinstance(d, str)}
            if len(batch) < len(pending):
                return False
            level = following - seen
        return False

    def _is_gate_review(self, attempt: AttemptTuple, task: Mapping[str, Any],
                        upstream_task_id: str, walk: "_Walk") -> bool:
        gate = _mapping(_mapping(_mapping(task.get("metadata")).get("dispatch"))
                        .get("verdict_gate"))
        review = gate.get("task_id")
        workflow_id = task.get("workflow_id")
        if (not isinstance(review, str) or review.strip() != upstream_task_id
                or not isinstance(workflow_id, str) or not workflow_id):
            return False
        doc = self._read(self._ref(TASKS, upstream_task_id))
        return doc is None or (self._verified(attempt, doc, upstream_task_id, walk)
                               and doc.get("workflow_id") == workflow_id)

    def repository(self, attempt: AttemptTuple) -> dict[str, Any]:
        """`indexrun.resolve_base` and `graphstage`'s reads: the registration
        of the task's signed repository and its kept index versions, read
        server-side (the worker role could never run that query, §1.3).

        At most KEPT_VERSIONS + 1 of them, and always the promoted one: a
        repository may keep up to 100 (`index.keep_versions`, lane
        INDEX-RETENTION), and the promoted version is the base an incremental
        run builds on, wherever its id sorts."""
        task = self._own_task(attempt)
        repo = owner_repo(task.get("repository_url"))
        if repo is None:
            return {"ok": True, "repo_id": None, "registration": None, "versions": []}
        repo_id = repo_id_for(attempt.tenant_id, *repo)
        ref = self._ref(REPOSITORIES, repo_id)
        registration = self._read(ref)
        if registration is None or registration.get("tenant_id") != attempt.tenant_id:
            return {"ok": True, "repo_id": repo_id, "registration": None, "versions": []}
        try:
            rows = list(ref.collection(VERSIONS_COLLECTION).limit(KEPT_VERSIONS + 1).stream())
            current = (registration.get("index") or {}).get("current_sha")
            if isinstance(current, str) and current and all(row.id != current for row in rows):
                promoted = ref.collection(VERSIONS_COLLECTION).document(current).get()
                if promoted.exists:
                    rows = [promoted, *rows[:KEPT_VERSIONS]]
        except gexc.GoogleAPIError as exc:
            raise StateUnavailable(f"the state store did not answer ({type(exc).__name__})") \
                from None
        versions = [{"id": row.id, **(row.to_dict() or {})} for row in rows]
        return {"ok": True, "repo_id": repo_id, "registration": jsonable(registration),
                "versions": jsonable(versions)}

    def ci_fix_tasks(self, call: Call) -> dict[str, Any]:
        """`inputs.find_ci_fix_tasks`, run here, tenant-restricted twice."""
        body: CiFixBody = call.body
        attempt = call.attempt
        self._own_task(attempt)
        query = (
            self._db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", attempt.tenant_id))
            .where(filter=FieldFilter("metadata.ci_fix.pull_request", "==", body.pull_request))
            .where(filter=FieldFilter("metadata.dispatch.continues", "==", body.root_task_id))
            .limit(body.limit)
        )
        try:
            rows = list(query.stream())
        except gexc.GoogleAPIError as exc:
            raise StateUnavailable(f"the state store did not answer ({type(exc).__name__})") \
                from None
        found = [{"id": row.id, "task": jsonable(row.to_dict() or {})} for row in rows
                 if (row.to_dict() or {}).get("tenant_id") == attempt.tenant_id]
        return {"ok": True, "tasks": found}

    # -- the merge slot and the merge train --------------------------------

    def _state_of(self, tenant_id: str, task_id: str) -> str | None:
        doc = self._read(self._ref(TASKS, task_id))
        if doc is None or doc.get("tenant_id") != tenant_id:
            return None  # never read as ended: a read that fails takes no live holder's slot
        return doc.get("state")

    def _ended(self, tenant_id: str, task_id: str) -> bool:
        return self._state_of(tenant_id, task_id) in {s.value for s in TERMINAL_STATES}

    def _wake(self, attempt: AttemptTuple, slot: str, task_id: str, now: datetime) -> bool:
        """`FirestoreMergeSlots.wake`, guarded on the waiter being LISTED in
        this tenant's slot -- its holder or one of its waiters -- and of this
        tenant. Fenced like every slot write."""
        slot_ref = self._ref(MERGE_SLOTS, slot)
        ref = self._ref(TASKS, task_id)
        refused = MergeWakeRefused("only a waiter of this tenant listed in this slot is woken")

        def _apply(txn: Any) -> bool:
            slot_snap = _snapshot(txn.get(slot_ref))
            doc = (slot_snap.to_dict() or {}) if slot_snap.exists else {}
            snap = _snapshot(txn.get(ref))
            self._fenced(txn, attempt)
            if doc.get("tenant_id") != attempt.tenant_id:
                raise refused
            holder = _holder(doc)
            listed = (holder is not None and holder["task_id"] == task_id) or any(
                w["task_id"] == task_id for w in _waiters(doc))
            data = (snap.to_dict() or {}) if snap.exists else None
            if not listed or data is None or data.get("tenant_id") != attempt.tenant_id:
                raise refused
            if (data.get("state") != TaskState.PARKED.value
                    or data.get("park_reason") != ParkReason.CI_PENDING.value):
                return False
            metadata = dict(_mapping(data.get("metadata")))
            wait = dict(_mapping(metadata.get(MERGE_WAIT)))
            if wait.get("code") != MERGE_SLOT_WAIT or wait.get(_WAKE_MARKER):
                return False
            wait[_WAKE_MARKER] = now
            wait["wake_reason"] = SLOT_WAKE_REASON
            metadata[MERGE_WAIT] = wait
            txn.update(ref, {"metadata": metadata, "updated_at": now})
            return True

        return bool(self._run(_apply))

    def merge_slot(self, call: Call) -> dict[str, Any]:
        body: MergeSlotBody = call.body
        attempt = call.attempt
        task = self._own_task(attempt)
        repo = owner_repo(task.get("repository_url"))
        if repo is None:
            raise ValidationFailed("this task names no GitHub repository, so it has no merge slot")
        repository = f"{repo[0]}/{repo[1]}"
        slot = slot_id(attempt.tenant_id, repository, body.base)
        slot_ref = self._ref(MERGE_SLOTS, slot)
        submitted = task.get("created_at")
        now = self._now()
        if not isinstance(submitted, datetime) or submitted.tzinfo is None:
            submitted = now
        me = Claimant(attempt.task_id, attempt.attempt_id, submitted)

        def _mine(doc: dict[str, Any] | None) -> dict[str, Any] | None:
            if doc is not None and doc.get("tenant_id") != attempt.tenant_id:
                raise StateConflict("the merge slot under this id is another tenant's")
            return doc

        def _transact(fn: Callable[[dict[str, Any] | None], tuple[Any, Any]]) -> Any:
            def _apply(txn: Any) -> Any:
                snap = _snapshot(txn.get(slot_ref))
                self._fenced(txn, attempt)
                new, result = fn(_mine((snap.to_dict() or {}) if snap.exists else None))
                if new is not None:
                    txn.set(slot_ref, new)
                return result
            return self._run(_apply)

        if body.op == "peek":
            return {"ok": True, "slot": slot, "document": jsonable(_mine(self._read(slot_ref)))}
        if body.op == "wake":
            if body.wake_task_id is None:
                raise ValidationFailed("wake names wake_task_id")
            return {"ok": True, "slot": slot,
                    "woken": self._wake(attempt, slot, body.wake_task_id, now)}
        if body.op == "confirm":
            holder = _holder(_mine(self._read(slot_ref)) or {})
            held = (holder is not None and holder["task_id"] == attempt.task_id
                    and body.slot_generation is not None
                    and _count(holder.get("generation")) == body.slot_generation)
            return {"ok": True, "slot": slot, "held": held}
        if body.op == "renew":
            if body.updates is None:
                raise ValidationFailed("renew names updates")
            held = _transact(lambda doc: slot_renew(doc, me, now=now, updates=body.updates))
            return {"ok": True, "slot": slot, "held": held}
        if body.op == "acquire":
            if body.updates is None:
                raise ValidationFailed("acquire names updates")
            peek = _mine(self._read(slot_ref)) or {}
            holder = _holder(peek)
            ended: set[str] = set()
            if (holder is not None and holder["task_id"] != attempt.task_id
                    and not holder_expired(holder, now)
                    and self._ended(attempt.tenant_id, holder["task_id"])):
                ended.add(holder["task_id"])
            result = _transact(lambda doc: slot_acquire(
                doc, me, tenant_id=attempt.tenant_id, repository=repository, base=body.base,
                now=now, updates=body.updates or 0, ended=ended))
            woke = None
            waiting = result.get("waiting")
            if waiting and waiting.get("wake"):
                woke = self._wake_quietly(attempt, slot, waiting["wake"], now)
            return {"ok": True, "slot": slot, **result, "woke": woke}
        # release
        reason = body.reason or "released"
        if not _REASON.fullmatch(reason):
            raise ValidationFailed("reason must be a short release reason")
        peek = _mine(self._read(slot_ref)) or {}
        ended = set()
        for entry in _live_waiters(peek, now=now, ended=set(), drop=attempt.task_id)[
                :HANDOFF_READS]:
            if not self._ended(attempt.tenant_id, entry["task_id"]):
                break
            ended.add(entry["task_id"])
        handed = _transact(lambda doc: slot_release(
            doc, me, now=now, reason=reason, ended=ended, generation=body.slot_generation))
        woke = self._wake_quietly(attempt, slot, handed, now) if handed else None
        return {"ok": True, "slot": slot, "handed_to": handed, "woke": woke}

    def _wake_quietly(self, attempt: AttemptTuple, slot: str, task_id: str,
                      now: datetime) -> bool:
        """A handoff's wake, as `SlotUse._wake`: the park's fallback instant wakes
        it anyway, so only a fence is raised."""
        try:
            return self._wake(attempt, slot, task_id, now)
        except AttemptFenced:
            raise
        except ApiError:
            return False

    def merge_train(self, call: Call) -> dict[str, Any]:
        """`mergetrain.FirestoreTrainEntries`: the entry of THIS tenant's THIS
        task, its repository the task's signed one. Fenced."""
        body: MergeTrainBody = call.body
        attempt = call.attempt
        task = self._own_task(attempt)
        entry_id = entry_id_for(attempt.tenant_id, attempt.task_id)
        ref = self._ref(MERGE_TRAIN_ENTRIES, entry_id)
        if body.op == "peek":
            doc = self._read(ref)
            if doc is not None and doc.get("tenant_id") != attempt.tenant_id:
                raise StateConflict("the merge train entry under this id is another tenant's")
            return {"ok": True, "entry": jsonable(doc)}
        repo = owner_repo(task.get("repository_url"))
        if repo is None:
            raise ValidationFailed("this task names no GitHub repository")
        missing = [n for n in ("base", "number", "head", "title", "message")
                   if getattr(body, n) is None]
        if missing:
            raise ValidationFailed(f"register names {', '.join(missing)}")
        fields = {"tenant_id": attempt.tenant_id, "task_id": attempt.task_id,
                  "repository": f"{repo[0]}/{repo[1]}",
                  "repository_url": task.get("repository_url"), "base": body.base,
                  "number": body.number, "head": body.head, "title": body.title,
                  "message": body.message}
        now = self._now()

        def _apply(txn: Any) -> Any:
            snap = _snapshot(txn.get(ref))
            self._fenced(txn, attempt)
            new, entry = register_entry((snap.to_dict() or {}) if snap.exists else None,
                                        fields=fields, now=now)
            if new is not None:
                new = {**new, "entry_id": entry_id}
                txn.set(ref, new)
                return new
            return entry

        return {"ok": True, "entry": jsonable(self._run(_apply))}


def _typesafe_secret(tenant_id: str) -> str:
    """`swarm-tenant-<tenant>-typesafe`, through the frozen spelling (`jevkey.secret_id`)."""
    return Tenant(tenant_id=tenant_id, kind="group", principal="",
                  created_at=utcnow()).secret_name("typesafe")
