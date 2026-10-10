"""The worker's window onto the control plane.

Everything the worker knows about the outside world -- whether it is still the
rightful owner of its task, whether someone cancelled it, whether its provider
has run out of quota -- arrives through this module, and every durable fact the
worker produces leaves through it.

Three rules shape the design.

**Fencing is checked against the task document, not against the environment.**
The worker is handed a generation in its environment, but the authority is
`tasks/{id}.current_generation`. If a reconciler decided this attempt was dead
and a newer attempt was admitted, the task document is where that fact lives, so
that is what `validate_generation` reads -- before any workspace exists, before
any credential is fetched, before the runner is started.

**Releasing capacity goes through the frozen admission module.** There is
exactly one function in the codebase that decrements a pool, and it lives in
`swarm_common.admission`. The worker calls it inside a transaction like everyone
else, and it is idempotent, so the worker, the reconciler and a cancellation can
all race to release the same lease without inflating capacity.

**A fenced worker does not release the lease.** Its lease belongs to a
superseded generation; the live lease is someone else's. Releasing is the
reconciler's job precisely because the reconciler can order invalidation before
release, and touching pools on the way out of a fence is the one way a worker
could hand a slot to nobody.

**Every write to the task re-checks the fence inside its own transaction.**
That covers each state transition and the checkpoint pointer. Checking once
at startup and on each control poll was not enough. A worker can reach the
end of an attempt between two polls: on SIGTERM, a crash, or a runner that
exits on its own. A check made before the write also loses to a fence that
lands between that check and the write. So `_fenced_task` reads the task and
the lease through the same transaction that writes. If the attempt is fenced
it raises `FencedWriteRefused` and nothing is committed. `validate_generation`
and `_fenced_task` share one predicate (`_task_fence`, `_lease_fence`), so the
gate and the writes agree on what "fenced" means. An event that ANNOUNCES one
of those writes (QUOTA_EXHAUSTED ahead of a park) is written in the same
transaction (`transition`'s `events`), so it commits with the write or not at
all.

**Every document is checked against this worker's tenant before it is used.**
Firestore has no document-level IAM: `roles/datastore.user` is granted per
DATABASE, so the tenant service account this worker runs as can physically read
and write any task, lease, attempt or quota document in the `swarm` database.
Invariant 9 therefore cannot rest on IAM alone on this path, and every read
here compares `tenant_id` against the tenant the attempt was admitted for. A
mismatch raises `TenantMismatchError` and the worker exits having written
nothing -- not a state change, not a lease release, not even an event, because
all three would be writes into another tenant's data.

**Before the runner exists, every Firestore call is bounded.** Inside
`ControlPlane.startup_budget()` each read, write and transaction RPC this
module makes carries `startup_call_options`: the reads and writes directly,
and a transaction's own begin, commit and rollback through
`FirestoreTransactionRunner`. Outside it, every call keeps the library's
defaults. The lifecycle holds the window open from the generation check
until the runner child is built, and nowhere else.

**The worker reports provider outcomes; it never writes quota state.** The
quota broker is the single writer of `quota/{provider}:{tenant}` and of the
caps on the provider pools, because it is the one place AIMD runs: a 429
halves the target, a run of successes raises it by one, and enough 429s in a
row mark the provider EXHAUSTED. A worker writing the document beside it
bypassed all three, and the broker's sweep then retired the worker's THROTTLED
(it carried no cooldown) on its next tick. `update_quota_state` POSTs each
outcome to the broker with this worker's identity token instead; see
`BrokerQuotaReporter`.
"""

from __future__ import annotations

import functools
import json
import re
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
from urllib.parse import quote, urlparse

from swarm_common.admission import _snapshot, release_lease_in_transaction
from swarm_common.models import (
    Attempt,
    EndCause,
    ProviderState,
    TaskEvent,
    new_id,
    retries_exhausted,
    utcnow,
)
from swarm_common.states import (
    CONCURRENCY_STATES,
    TERMINAL_STATES,
    EventType,
    ParkReason,
    TaskState,
    assert_transition,
)

from .accountlease import fetch_identity_token
from .errors import (
    ControlPlaneError,
    ExitCode,
    FencedError,
    FencedWriteRefused,
    TenantMismatchError,
)
from .quota import UNKNOWN_WAIT_SECONDS
from .startup import StartupInterrupted

#: The attempt document's map of checkpoint id to the SHA-256 of its archive,
#: written by `ControlPlane.record_checkpoint` beside `checkpoints`. A retry
#: restores a checkpoint only when its archive digest is the one recorded here
#: (`Worker._recorded_checkpoint`, #347): the manifest carrying the digest sits
#: in the bucket, which every agent of the tenant can write.
#:
#: It is the frozen `Attempt.checkpoint_sha256` since contract request 51
#: (accepted by the owner 2026-10-09), and is looked up on the dataclass rather
#: than restated: a rename there fails this import, not a retry's restore.
CHECKPOINT_DIGESTS_FIELD = Attempt.__dataclass_fields__["checkpoint_sha256"].name


def recorded_checkpoint_digests(attempt: Mapping[str, Any] | None) -> dict[str, str]:
    """An attempt document's `checkpoint_sha256`, in the type `Attempt` gives it.

    Only `str -> str` entries are kept, as swarm-api's `codec.attempt_from_dict`
    keeps them. A missing document, a missing field (an attempt written before
    #348) or a value that is not a map is the EMPTY map, which contract request
    51 defines as "no digest recorded": the retry restores nothing from that
    attempt. It is never read as "anything goes".
    """
    raw = attempt.get(CHECKPOINT_DIGESTS_FIELD) if attempt else None
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}

#: What an archive digest is: `hashlib.sha256(...).hexdigest()`, 64 lowercase
#: hex characters. `record_checkpoint` writes nothing else, and a restore
#: refuses a manifest carrying anything else (#346): a missing digest on both
#: sides would otherwise compare equal, None == None.
ARCHIVE_DIGEST_RE = re.compile(r"[0-9a-f]{64}")

#: `ControlPlane._verified_max_attempts` before a verified spec pinned it.
_UNPINNED = object()

#: Child tasks (docs/design/child-tasks.md §6.4), restated from
#: `swarm_api.validation`, which this image does not carry;
#: tests/unit/worker/test_child_tasks_worker.py holds them equal. The awaits a
#: parent has had refunded, and the marker on a child cancelled because of
#: its parent.
CHILD_AWAIT_RESUMES_METADATA_KEY = "child_await_resumes"
CHILD_CASCADE_METADATA_KEY = "child_cascade"

#: A merge step's CI wait (lane MS2, docs/merge-step.md "Revised 2026-10-06"
#: §1): `metadata.merge_wait` on the task, written by `park_ci_pending`. Read by
#: swarm-api's wake tick (`swarm_api.mergewake`), which adds the marker below,
#: and by the scheduler's `_promote_ci_waits`, which wakes on it.
#: tests/unit/worker/test_merge_action.py holds the three restatements equal.
MERGE_WAIT_METADATA_KEY = "merge_wait"

#: Where a park records its uploads for the attempt that finishes the task
#: (#166): `tasks/{task}/carry/{attempt}` per park, and the index of the parks
#: at `tasks/{task}/carry/parks`. Known ids, because the tenant worker role can
#: get a document by id and cannot list or query (`ControlPlane.parked_uploads`
#: says why). `parks` cannot collide with an attempt id, which is `att_...`.
CARRY_COLLECTION = "carry"
CARRY_INDEX_ID = "parks"


def _is_carry_attempt_id(value: str) -> bool:
    """An index entry usable as a record's document id under this task's `carry`."""
    return (
        bool(value)
        and "/" not in value
        and value != CARRY_INDEX_ID
        and not value.startswith("__")
    )


def cancel_end_cause(task: Mapping[str, Any]) -> EndCause:
    """Why a cancelled task ended: CHILD_CASCADE when its parent's cancel, end
    or await deadline flagged it (contract request 41), else CANCEL_REQUESTED."""
    metadata = task.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get(CHILD_CASCADE_METADATA_KEY):
        return EndCause.CHILD_CASCADE
    return EndCause.CANCEL_REQUESTED

#: EVERY FIRESTORE CALL A WORKER MAKES WHILE ITS AGENT RUNS, AND ITS BUDGET
#: (#70, owner decision 2026-09-28): `(deadline, per-try timeout)` in seconds,
#: retries included. Before this they kept the library's defaults -- up to 300 s
#: of silent retries for a read, 60 s for each of a transaction's RPCs -- so a
#: Firestore outage held the supervision loop for minutes and then raised into
#: the crash handler, which failed the task for good. The loop is one thread:
#: while one call waits, nothing else (the heartbeat included) happens.
#:
#:   heartbeat   90 s, UNDER the 120 s the beat extends the lease by
#:               (`heartbeat_extension_seconds`, the platform's
#:               `lease_timeout_seconds`). A beat that is still retrying when
#:               the lease it is extending has expired is a beat for a lease
#:               the reconciler may already have reclaimed. Held under the
#:               extension whatever it is configured to (`_heartbeat_deadline`).
#:               And held to ONE BEAT INTERVAL when the worker gives its beat
#:               (#426): a beat retrying for 90 s is the reconciler's whole
#:               grace spent on one beat, with the next not started.
#:   poll        30 s. The control poll runs every `control_poll_seconds`, and
#:               a failed one is asked again on the next tick.
#:   checkpoint  60 s for the pointer write and its owner check: the archive
#:               is already in the bucket, and the next interval tries again.
#:   event       30 s. An event is an audit record; the loop does not wait on it.
#:   attempt     30 s for this attempt's own document (its end, its usage, its
#:               spend), written on the way out, when the exit is waiting.
#:   tenant      30 s for the tenant read of a credential reload, made between
#:               one runner and the next with nothing beating. It kept the
#:               library's 300 s; it is now one read like the poll's, and an
#:               outage past it leaves through the same exit 69.
#:
#: LIBRARY DEFAULTS, ON PURPOSE: the terminal transitions (`finish`, through
#: `transition`) and the lease release. Each is made once, on the way out, by
#: an attempt whose agent has stopped, and is the write that ends the task;
#: cutting it short would leave the reconciler to repair what one longer
#: retry would have written.
#:
#: What happens when a budget is spent is the lifecycle's: a failed beat or
#: poll is logged and the loop goes on while the lease is live, and once the
#: lease this worker last extended has run out, the worker checkpoints, stops
#: the runner and exits 69 for a requeue (`Worker._exit_control_plane_outage`).
#: Inside `startup_budget()` the startup budget applies instead, as before.
MID_RUN_BUDGETS: dict[str, tuple[float, float]] = {
    "heartbeat": (90.0, 10.0),
    "poll": (30.0, 10.0),
    "checkpoint": (60.0, 10.0),
    "event": (30.0, 10.0),
    "attempt": (30.0, 10.0),
    "tenant": (30.0, 10.0),
}


def budget_call_options(deadline: float, timeout: float) -> dict[str, Any]:
    """`retry` and `timeout` for one call: retried until `deadline`, each try `timeout`.

    The predicate is the one the startup budget uses
    (`__main__.firestore_startup_call_options`): DEADLINE_EXCEEDED, INTERNAL
    and UNAVAILABLE, the errors Firestore's own default retries for a read. A
    transaction's commit narrows it again (`_commit_retry`). Without the
    google client library -- a unit test with no grpc -- the per-try timeout
    alone, which an in-memory store ignores.
    """
    try:
        from google.api_core import exceptions as core_exceptions  # lazy: grpc
        from google.api_core.retry import Retry, if_exception_type
    except ImportError:  # pragma: no cover - the image always carries it
        return {"timeout": timeout}
    return {
        "retry": Retry(
            initial=0.5,
            maximum=10.0,
            multiplier=2.0,
            predicate=if_exception_type(
                core_exceptions.DeadlineExceeded,
                core_exceptions.InternalServerError,
                core_exceptions.ServiceUnavailable,
            ),
            timeout=deadline,
        ),
        "timeout": min(timeout, deadline),
    }


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlSignals:
    """A point-in-time read of everything that can stop or divert this worker."""

    state: TaskState
    generation: int
    cancel_requested: bool
    lease_released: bool
    lease_expires_at: datetime | None = None
    provider_state: ProviderState | None = None
    provider_paused: bool = False
    retry_after_seconds: int | None = None
    quota_reset_at: datetime | None = None
    observed_at: datetime | None = None

    def is_fenced(self, generation: int) -> bool:
        """True when this worker no longer owns the task."""
        return (
            self.generation != generation
            or self.lease_released
            or self.state in TERMINAL_STATES
        )

    def wants_stop(self, generation: int) -> bool:
        return self.cancel_requested or self.is_fenced(generation)


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    # Firestore's server timestamps expose .timestamp_pb() / .ToDatetime() shims.
    to_dt = getattr(value, "ToDatetime", None)
    if callable(to_dt):
        return to_dt()
    return None


def _as_state(value: Any) -> TaskState:
    if isinstance(value, TaskState):
        return value
    try:
        return TaskState(value)
    except (ValueError, TypeError) as exc:
        raise ControlPlaneError(f"task document holds an unknown state {value!r}") from exc


def _count(value: Any) -> int:
    """A non-negative count from the tenant-writable task document, else 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


# ---------------------------------------------------------------------------
# The fence: ONE definition
# ---------------------------------------------------------------------------
#
# Read by `validate_generation` before the agent starts, and by `_fenced_task`
# inside every transaction that writes the task. Two copies of this rule would
# drift, and then the gate and the writes would disagree about whether an
# attempt still owns its task. That disagreement is the defect these exist to
# remove. Split in two only because the lease is read after the task, in the
# same order as before, so a task-level fence is reported ahead of a lease
# document that another tenant owns.


def _task_fence(task: dict[str, Any], *, generation: int) -> tuple[int, str] | None:
    """Why `task` no longer belongs to the attempt at `generation`, or None.

    Returns `(observed_generation, reason)`.
    """
    current = int(task.get("current_generation", 0))
    state = _as_state(task.get("state"))
    if current != generation:
        return current, "task has moved to a newer generation"
    if state in TERMINAL_STATES:
        return current, f"task is already terminal ({state.value})"
    if state not in CONCURRENCY_STATES:
        # READY or PARKED means the lease was reclaimed underneath us.
        return current, f"task no longer holds capacity (state={state.value})"
    return None


def _lease_fence(
    task: dict[str, Any],
    lease: dict[str, Any] | None,
    *,
    generation: int,
    task_id: str,
    lease_id: str,
) -> tuple[int, str] | None:
    """Why this attempt's lease no longer gives it the task, or None."""
    current = int(task.get("current_generation", 0))
    if lease is None:
        return current, "lease document is gone"
    if lease.get("released_at") is not None:
        return current, "lease has already been released"
    if int(lease.get("generation", -1)) != generation:
        return int(lease.get("generation", -1)), "lease belongs to a different generation"
    if lease.get("task_id") != task_id:
        return current, "lease belongs to a different task"
    if task.get("current_lease_id") not in (None, lease_id):
        return current, "task points at a different lease"
    return None


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class TransactionRunner(Protocol):
    """Runs a function inside one Firestore transaction, retrying on contention.

    `call_options` is the `retry` and `timeout` for the transaction's own
    RPCs. `ControlPlane` passes it only when it is not empty, so a runner that
    takes the body alone still fits everywhere outside the startup window.
    """

    def run(
        self, fn: Callable[[Any], Any], *, call_options: Mapping[str, Any] | None = None
    ) -> Any: ...


class FirestoreTransactionRunner:
    def __init__(self, db: Any) -> None:
        self._db = db

    def run(
        self, fn: Callable[[Any], Any], *, call_options: Mapping[str, Any] | None = None
    ) -> Any:
        from google.cloud import firestore  # lazy: unit tests never import grpc

        from google.cloud.firestore_v1.transaction import Transaction  # lazy: grpc

        transaction = self._db.transaction()
        if call_options and isinstance(transaction, Transaction):
            # The same kind of transaction the client makes, with its begin,
            # commit and rollback under the budget. See `_with_budget`, whose
            # overrides make the library's own RPCs and so need the library's
            # own class: a transaction from anything else (an in-memory
            # store) has no RPCs to budget and runs as it was given. Every
            # call is a mid-run one since #70, the heartbeat included.
            transaction = _with_budget(type(transaction))(self._db, budget=call_options)

        @firestore.transactional
        def _inner(txn: Any) -> Any:
            return fn(txn)

        return _inner(transaction)


def _commit_retry(retry: Any) -> Any:
    """`retry` for a transaction's commit: the same deadline, a narrower predicate.

    A commit is retried only on errors that mean it was not applied:
    UNAVAILABLE and RESOURCE_EXHAUSTED. DEADLINE_EXCEEDED and INTERNAL can
    arrive after the commit landed, and a second commit of the same
    transaction id is then refused. That would report a failed transition that
    had in fact been made. The library's own default for `commit` draws the
    same line. A read has no such hazard, which is why the read budget retries
    DEADLINE_EXCEEDED and INTERNAL.
    """
    with_predicate = getattr(retry, "with_predicate", None)
    if with_predicate is None:
        return retry
    from google.api_core import exceptions as core_exceptions  # lazy: grpc
    from google.api_core.retry import if_exception_type

    return with_predicate(
        if_exception_type(core_exceptions.ServiceUnavailable, core_exceptions.ResourceExhausted)
    )


@functools.lru_cache(maxsize=None)
def _with_budget(base: type) -> type:
    """`base`, a Firestore `Transaction` class, with its three RPCs under a budget.

    WHY A SUBCLASS. `Transaction._begin`, `_commit` and `_rollback` call the
    Firestore API with no `retry` or `timeout`, so each gets the library's
    default: 60 s of retries per call (google-cloud-firestore 2.30.0,
    services/firestore/transports/base.py). No public argument reaches them.
    These three overrides make the same requests the library makes, with the
    budget added. They read the same private attributes the library's own
    methods read (`_client._firestore_api`, `_id`, `_write_pbs`), and uv.lock
    pins the library version they were read from.
    `tests/unit/worker/test_startup_window_under_real_transactions.py` runs
    them inside the library's own `firestore.transactional`.

    TWO CHANGES TO ROLLBACK, besides the budget. `firestore.transactional`
    calls `_rollback` from `except BaseException`, and whatever `_rollback`
    raises replaces the exception that was unwinding:

      * A transaction whose `begin_transaction` never returned has no id. The
        library raises `ValueError("... cannot be rolled back.")` for that,
        and a SIGTERM's `StartupInterrupted`, or the `RetryError` that ended
        the begin, left the transaction as that ValueError. Here there is
        nothing to roll back, so nothing is raised and the original goes on.
      * A rollback RPC that fails is added to the unwinding exception as a
        note and not raised. Firestore expires an abandoned transaction by
        itself. What unwound is the error the caller has to act on.

    Under a SIGTERM the rollback is one try, with the per-call timeout and no
    retries. The process has been asked to leave, and the rollback only frees
    locks that Firestore frees on its own when the transaction expires.
    """
    from google.cloud.firestore_v1.base_transaction import (  # lazy: grpc
        _CANT_BEGIN,
        _CANT_COMMIT,
        _CANT_ROLLBACK,
    )

    class BudgetedTransaction(base):  # type: ignore[misc, valid-type]
        def __init__(self, client: Any, *, budget: Mapping[str, Any]) -> None:
            super().__init__(client)
            self._budget = dict(budget)

        def _begin(self, retry_id: bytes | None = None) -> None:
            if self.in_progress:
                raise ValueError(_CANT_BEGIN.format(self._id))
            response = self._client._firestore_api.begin_transaction(
                request={
                    "database": self._client._database_string,
                    "options": self._options_protobuf(retry_id),
                },
                metadata=self._client._rpc_metadata,
                **self._budget,
            )
            self._id = response.transaction

        def _commit(self) -> list:
            if not self.in_progress:
                raise ValueError(_CANT_COMMIT)
            options = dict(self._budget)
            if "retry" in options:
                options["retry"] = _commit_retry(options["retry"])
            response = self._client._firestore_api.commit(
                request={
                    "database": self._client._database_string,
                    "writes": self._write_pbs,
                    "transaction": self._id,
                },
                metadata=self._client._rpc_metadata,
                **options,
            )
            self._clean_up()
            self.write_results = list(response.write_results)
            self.commit_time = response.commit_time
            return self.write_results

        def _rollback(self) -> None:
            unwinding = sys.exc_info()[1]
            if not self.in_progress:
                if unwinding is None:
                    raise ValueError(_CANT_ROLLBACK)
                return
            options = dict(self._budget)
            if isinstance(unwinding, StartupInterrupted):
                options["retry"] = None
            try:
                self._client._firestore_api.rollback(
                    request={
                        "database": self._client._database_string,
                        "transaction": self._id,
                    },
                    metadata=self._client._rpc_metadata,
                    **options,
                )
            except Exception as exc:
                if unwinding is None:
                    raise
                unwinding.add_note(
                    "the transaction's rollback failed as well, and was not "
                    f"raised: {type(exc).__name__}: {exc}"
                )
            finally:
                self._clean_up()

    BudgetedTransaction.__name__ = f"Budgeted{base.__name__}"
    BudgetedTransaction.__qualname__ = BudgetedTransaction.__name__
    return BudgetedTransaction


# ---------------------------------------------------------------------------
# Control plane
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Quota reports to the broker
# ---------------------------------------------------------------------------

#: Broker route per provider state the worker can observe. THROTTLED and
#: EXHAUSTED are both "the provider answered 429": every runner labels its
#: `quota.json` EXHAUSTED (`runners.base.write_quota_signal`), so sending that
#: to `/exhausted` would mark a tenant exhausted on its first 429 and skip the
#: broker's exhaustion threshold, which is the thing that decides it.
#: COOLDOWN, DISABLED and UNKNOWN are absent on purpose: the worker only ever
#: holds them because it READ them from the broker (`poll`), and reporting the
#: broker's own state back to it is not an observation.
QUOTA_REPORT_ROUTES: dict[ProviderState, str] = {
    ProviderState.AVAILABLE: "success",
    ProviderState.THROTTLED: "rate-limit",
    ProviderState.EXHAUSTED: "rate-limit",
}

#: Seconds one report may take. Short: it sits on the way to a park, and a
#: worker waiting on a control-plane service is a worker holding a slot.
QUOTA_REPORT_TIMEOUT_SECONDS = 5


class QuotaReporter(Protocol):
    def report(
        self, *, provider: str, tenant_id: str, route: str, body: Mapping[str, Any]
    ) -> None: ...


class BrokerQuotaReporter:
    """POST one provider outcome to `/v1/quota/{provider}/{tenant}/{route}`.

    Authenticated exactly as the account pool's client is
    (`accountlease.AccountBroker`): the metadata server's identity token for
    the broker's audience. The broker derives the tenant from that token and
    refuses a path naming any other one, so the tenant in the path is a
    claim this worker cannot widen. No retry: see `update_quota_state`.
    """

    def __init__(
        self,
        base_url: str,
        *,
        audience: str | None = None,
        token_fetcher: Any = None,
        timeout: int = QUOTA_REPORT_TIMEOUT_SECONDS,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._audience = (audience or self._base).rstrip("/")
        self._fetch_token = token_fetcher or fetch_identity_token
        self._timeout = timeout

    @classmethod
    def for_broker(
        cls, url: str | None, audience: str | None
    ) -> "BrokerQuotaReporter | None":
        """From `WorkerConfig.quota_broker_url`/`quota_broker_audience`, the one
        reader of the variables the scheduler puts on every worker
        (`scheduler.dispatch.worker_env`). None when there is no broker."""
        if not url:
            return None
        return cls(url, audience=audience)

    def report(
        self, *, provider: str, tenant_id: str, route: str, body: Mapping[str, Any]
    ) -> None:
        path = (
            f"/v1/quota/{quote(provider, safe='')}/{quote(tenant_id, safe='')}/{route}"
        )
        url = f"{self._base}{path}"
        req = urllib.request.Request(
            url, data=json.dumps(dict(body)).encode("utf-8"), method="POST"
        )
        req.add_header(
            "Authorization",
            f"Bearer {self._fetch_token(self._audience, timeout=self._timeout)}",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            raise ControlPlaneError(
                f"the quota broker answered {exc.code} on {path}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            host = urlparse(url).hostname or self._base
            raise ControlPlaneError(f"could not reach the quota broker at {host}: {exc}") from exc


class ControlPlane:
    """Firestore-backed control-plane operations for exactly one attempt."""

    def __init__(
        self,
        db: Any,
        *,
        task_id: str,
        attempt_id: str,
        lease_id: str,
        tenant_id: str,
        generation: int,
        logger: Any,
        txn_runner: TransactionRunner | None = None,
        heartbeat_extension_seconds: int = 120,
        heartbeat_interval_seconds: int | None = None,
        startup_call_options: Mapping[str, Any] | None = None,
        quota_reporter: QuotaReporter | None = None,
        finish_announcer: Any | None = None,
    ) -> None:
        self._db = db
        self.task_id = task_id
        self.attempt_id = attempt_id
        self.lease_id = lease_id
        self.tenant_id = tenant_id
        self.generation = generation
        self._log = logger
        self._txn = txn_runner or FirestoreTransactionRunner(db)
        self._heartbeat_extension = heartbeat_extension_seconds
        # The worker's beat (`WorkerConfig.heartbeat_interval_seconds`); None
        # leaves the heartbeat's budget as `MID_RUN_BUDGETS` and the extension
        # make it. See `_heartbeat_deadline`.
        self._heartbeat_interval = heartbeat_interval_seconds
        self._lease_released = False
        # `retry` and `timeout` for every Firestore call a worker makes before
        # its runner exists, applied inside `startup_budget()`. The entrypoint
        # supplies a bounded budget (`__main__.firestore_startup_call_options`).
        # Empty means the library's defaults, which for a document read is up
        # to 300 s of silent retries, and 60 s for each of a transaction's
        # begin, commit and rollback. Calls made while the agent runs carry
        # their own budgets instead (`MID_RUN_BUDGETS`, #70).
        self._startup_call: dict[str, Any] = dict(startup_call_options or {})
        # True inside `startup_budget()`. Read by `call_options`, which every
        # call this class makes goes through.
        self._budgeted = False
        # Where provider outcomes go: the quota broker, which the entrypoint
        # builds from `WorkerConfig` (`BrokerQuotaReporter.for_broker`). None
        # means this deployment has no broker; outcomes are then logged and
        # not recorded.
        self._quota_reporter: QuotaReporter | None = quota_reporter
        # Publishes the `task_finished` wake once this attempt has ENDED its
        # task (`finishwake.PubSubFinishAnnouncer`, #636). None means no wake:
        # the scheduler's safety tick releases the dependants, as before.
        self._finish_announcer = finish_announcer
        # The mid-run budgets, built once (`MID_RUN_BUDGETS`).
        self._mid_run: dict[str, dict[str, Any]] = {}
        # provider -> until when the broker already knows of a rate limit,
        # because this worker reported it or read it back in `poll`. A 429
        # report inside that window is the same event and is not sent again;
        # see `update_quota_state`.
        self._rate_limit_known_until: dict[str, datetime] = {}
        # The task's `max_attempts` as the VERIFIED spec carries it, once the
        # lifecycle has verified one (`pin_verified_max_attempts`, #346).
        # `fail_retryably` decides on it rather than on the live document.
        self._verified_max_attempts: Any = _UNPINNED

    def pin_verified_max_attempts(self, value: Any) -> None:
        """Decide retries on `value`, the verified task document's `max_attempts`.

        `max_attempts` is covered by the spec signature (`specsign`), so after
        `Worker._verify_spec` the verified copy is the one to trust: a tenant
        agent can write the live field (Firestore has no document-level IAM),
        and lowering it would end a task its signer gave more attempts, raising
        it would retry past them (#346). `attempt_count` is NOT covered and is
        still read live, inside the transaction. Absent or None means the
        signer set none, which is the default of 3, not whatever is written
        there later.
        """
        self._verified_max_attempts = 3 if value is None else value

    @property
    def quota_reporter(self) -> QuotaReporter | None:
        """Where `update_quota_state` sends provider outcomes; None without a broker."""
        return self._quota_reporter

    @property
    def startup_call_options(self) -> dict[str, Any]:
        return dict(self._startup_call)

    @contextmanager
    def startup_budget(self) -> Iterator[None]:
        """Every Firestore call made inside carries `startup_call_options`.

        Reads, writes, events, the heartbeat, and a transaction's own begin,
        commit and rollback. The lifecycle opens it for the generation check,
        again from `record_attempt_start` until the runner child is built, and
        for the attempt's own record on the two exits taken from that window.
        Nowhere else: the same calls made while an agent runs carry the
        mid-run budgets (`MID_RUN_BUDGETS`).

        ONE SWITCH, rather than a budget argument on each call. Before this,
        the budget was passed call by call, and the calls nobody passed it to
        (the three transactions in `advance_to_running`, the STARTING and
        RUNNING events, the first heartbeat) kept 60 s and 300 s defaults.
        """
        previous = self._budgeted
        self._budgeted = True
        try:
            yield
        finally:
            self._budgeted = previous

    @property
    def heartbeat_extension_seconds(self) -> int:
        """How far each heartbeat moves the lease's `expires_at`."""
        return self._heartbeat_extension

    def call_options(self, call: str | None = None) -> dict[str, Any]:
        """`retry` and `timeout` for a Firestore call made now.

        The startup budget inside `startup_budget()`. Outside it, the mid-run
        budget of `call` (`MID_RUN_BUDGETS`, #70), or empty -- the library's
        defaults -- for a call that names none: the terminal transitions and
        the lease release, made once on the way out. Public because the
        lifecycle's own startup reads (`secrets.load_tenant`,
        `inputs.stage_inputs`) take the same options.
        """
        if self._budgeted:
            return dict(self._startup_call)
        if call is None:
            return {}
        if call not in self._mid_run:
            deadline, timeout = MID_RUN_BUDGETS[call]
            if call == "heartbeat":
                deadline = self._heartbeat_deadline(deadline)
            self._mid_run[call] = budget_call_options(deadline, timeout)
        return dict(self._mid_run[call])

    def _heartbeat_deadline(self, deadline: float) -> float:
        """The heartbeat's deadline, held under the lease extension (#70).

        90 s against the 120 s extension the platform configures. A shorter
        extension -- a test, or a deployment that lowered
        `lease_timeout_seconds` -- keeps the same proportion, so a beat never
        retries past the expiry of the lease it is extending.

        AND NO LONGER THAN ONE BEAT (#426). 90 s is the reconciler's whole
        grace: a beat that kept retrying for it was the lease's silence, with
        no other beat started meanwhile. Held to the worker's beat interval, a
        beat that cannot land gives way to the next one on time, and the lease
        is still extended by whichever lands first. The outage rule is
        unchanged: it reads `_lease_live_until`, not how long one call tried.
        """
        deadline = min(deadline, 0.75 * self._heartbeat_extension)
        if self._heartbeat_interval is not None:
            deadline = min(deadline, float(self._heartbeat_interval))
        return deadline

    def _run_transaction(self, fn: Callable[[Any], Any], *, call: str | None = None) -> Any:
        options = self.call_options(call)
        if options:
            return self._txn.run(fn, call_options=options)
        # Called exactly as before when there is no budget, so a runner written
        # to the one-argument form still works outside the window.
        return self._txn.run(fn)

    # -- raw reads ---------------------------------------------------------
    def _task_ref(self) -> Any:
        return self._db.collection("tasks").document(self.task_id)

    def _lease_ref(self) -> Any:
        return self._db.collection("leases").document(self.lease_id)

    def _attempt_ref(self) -> Any:
        return self._db.collection("attempts").document(self.attempt_id)

    def _quota_ref(self, provider: str) -> Any:
        return self._db.collection("quota").document(f"{provider}:{self.tenant_id}")

    # -- tenant scoping ----------------------------------------------------
    def _assert_tenant(self, data: dict[str, Any], *, kind: str, document_id: str) -> dict[str, Any]:
        """Refuse a document that belongs to a different tenant.

        An absent `tenant_id` is refused too. Treating "missing" as "mine" is
        how an unscoped read becomes a cross-tenant read the first time a
        document is written by something that forgot the field.
        """
        actual = data.get("tenant_id")
        if actual != self.tenant_id:
            raise TenantMismatchError(
                kind=kind,
                document_id=document_id,
                expected=self.tenant_id,
                actual=actual if isinstance(actual, str) else None,
            )
        return data

    def fetch_task(self) -> dict[str, Any]:
        snap = self._task_ref().get(**self.call_options("poll"))
        if not snap.exists:
            raise FencedError(self.generation, -1, "task document no longer exists")
        return self._assert_tenant(
            snap.to_dict() or {}, kind="task", document_id=self.task_id
        )

    def fetch_task_snapshot(self) -> tuple[dict[str, Any], Any]:
        """`fetch_task`, and beside the dict the snapshot's Firestore `create_time`.

        Contract request 34's legacy window admits an unsigned task only if
        Firestore created it before SPEC_LEGACY_CUTOVER. `create_time` is set
        by Firestore and no client can write it, so stripping the signature
        off a newer task does not qualify it. None when the client gives none.
        """
        snap = self._task_ref().get(**self.call_options())
        if not snap.exists:
            raise FencedError(self.generation, -1, "task document no longer exists")
        doc = self._assert_tenant(snap.to_dict() or {}, kind="task", document_id=self.task_id)
        return doc, getattr(snap, "create_time", None)

    def fetch_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        """Another attempt's document, by id; None when there is none.

        Read by the restore (#347), which binds the checkpoint it is pointed
        at to the attempt document that recorded it. Refused, with
        `TenantMismatchError`, when the document is another tenant's.
        """
        snap = self._db.collection("attempts").document(attempt_id).get(
            **self.call_options("poll")
        )
        if not snap.exists:
            return None
        return self._assert_tenant(snap.to_dict() or {}, kind="attempt", document_id=attempt_id)

    def fetch_parent_states(self, parent_ids: Sequence[str]) -> dict[str, TaskState | None]:
        """Each parent task's state, by id: contract request 34, decision 7.

        `parent_ids` is the VERIFIED task's `depends_on` -- a signed field --
        and nothing else; the caller never passes an id it read from an
        unsigned one. One bounded point read per distinct id (`depends_on` is
        capped at the workflow step limit), in the order given.

        None for a parent with no document, or whose `state` is not a state
        the contract names: neither is SUCCEEDED, and neither is evidence the
        dependency is met. Refused, with `TenantMismatchError`, when a parent
        document is another tenant's -- a parent swarm-api would never have
        signed, and reading its state would be a cross-tenant read.
        """
        states: dict[str, TaskState | None] = {}
        for parent_id in dict.fromkeys(parent_ids):
            snap = self._db.collection("tasks").document(parent_id).get(**self.call_options())
            if not snap.exists:
                states[parent_id] = None
                continue
            doc = self._assert_tenant(snap.to_dict() or {}, kind="task", document_id=parent_id)
            try:
                states[parent_id] = TaskState(doc.get("state"))
            except ValueError:
                states[parent_id] = None
        return states

    def fetch_lease(self) -> dict[str, Any] | None:
        snap = self._lease_ref().get(**self.call_options("poll"))
        if not snap.exists:
            return None
        return self._assert_tenant(
            snap.to_dict() or {}, kind="lease", document_id=self.lease_id
        )

    def parked_uploads(self) -> list[tuple[str, list[Any]]]:
        """What each earlier PARKED attempt of this task uploaded, oldest first (#166).

        READ BY ID, NEVER BY QUERY (#166, reopened 2026-10-06). The first
        version read the parks off a query on the task's PARKED events, which
        needs `datastore.entities.list`. The tenant worker role
        (`swarmTenantWorkerFirestore`, terraform/bootstrap/platform_roles.tf)
        drops that permission on purpose, so a worker cannot enumerate another
        tenant's documents: the query 403'd on every attempt in dev and nothing
        was ever carried. A park now writes its uploads to
        `tasks/{task}/carry/{attempt}` and lists itself on
        `tasks/{task}/carry/parks`, in its own fenced transaction
        (`_record_parked_uploads`); this reads the index and then each record,
        each with a plain get.

        `(attempt_id, artifacts)` per park, in the order the parks happened,
        this attempt's own excluded. Every document is checked against this
        worker's tenant, as every read here is, and a record that names
        another task or another attempt than its id is refused the same way:
        it is not this task's park. Called once, as the finishing attempt
        describes its result; outside the startup window, so it keeps the
        library's defaults like the rest of that epilogue.
        """
        carry = self._task_ref().collection(CARRY_COLLECTION)
        index_snap = carry.document(CARRY_INDEX_ID).get()
        if not index_snap.exists:
            return []
        index = self._assert_tenant(
            index_snap.to_dict() or {}, kind="carry index", document_id=CARRY_INDEX_ID
        )
        listed = index.get("attempts")
        attempt_ids: list[str] = []
        for item in listed if isinstance(listed, list) else []:
            attempt_id = item.get("attempt_id") if isinstance(item, dict) else None
            if (
                isinstance(attempt_id, str)
                and _is_carry_attempt_id(attempt_id)
                and attempt_id != self.attempt_id
                and attempt_id not in attempt_ids
            ):
                attempt_ids.append(attempt_id)
        found: list[tuple[Any, str, list[Any]]] = []
        for attempt_id in attempt_ids:
            snap = carry.document(attempt_id).get()
            if not snap.exists:
                continue
            record = self._assert_tenant(
                snap.to_dict() or {}, kind="carry record", document_id=attempt_id
            )
            if record.get("task_id") != self.task_id or record.get("attempt_id") != attempt_id:
                raise TenantMismatchError(
                    kind="carry record",
                    document_id=attempt_id,
                    expected=f"{self.task_id}/{attempt_id}",
                    actual=f"{record.get('task_id')}/{record.get('attempt_id')}",
                )
            artifacts = record.get("artifacts")
            if not isinstance(artifacts, list):
                continue
            found.append((_as_datetime(record.get("at")), attempt_id, artifacts))
        found.sort(key=lambda item: (item[0] is None, item[0] or datetime.min))
        return [(attempt_id, artifacts) for _, attempt_id, artifacts in found]

    def _read_carry_index(self, txn: Any) -> dict[str, Any] | None:
        """The task's carry index as `txn` reads it, for `_record_parked_uploads`.

        A READ, so it is made before the transaction's first write, which
        Firestore requires. None when there is none yet.
        """
        ref = self._task_ref().collection(CARRY_COLLECTION).document(CARRY_INDEX_ID)
        snap = _snapshot(txn.get(ref, **self.call_options()))
        if not snap.exists:
            return None
        return self._assert_tenant(
            snap.to_dict() or {}, kind="carry index", document_id=CARRY_INDEX_ID
        )

    def _record_parked_uploads(
        self, txn: Any, index: dict[str, Any] | None, artifacts: list[Any], *, at: datetime
    ) -> None:
        """Write this park's uploads where the next attempt gets them by id (#166).

        Two documents under the task's own: `carry/{attempt}`, the upload
        manifest, and `carry/parks`, the index of the attempts that parked.
        One record per attempt keeps each document the size of one manifest,
        well under Firestore's 1 MiB, however many times a task parks. Written
        in the park's own transaction, after its fence read: a superseded
        attempt is refused before this, so it records nothing (invariant 5).
        """
        carry = self._task_ref().collection(CARRY_COLLECTION)
        txn.set(
            carry.document(self.attempt_id),
            {
                "tenant_id": self.tenant_id,
                "task_id": self.task_id,
                "attempt_id": self.attempt_id,
                "at": at,
                "artifacts": list(artifacts),
            },
        )
        listed = (index or {}).get("attempts")
        attempts = [
            item for item in (listed if isinstance(listed, list) else [])
            if isinstance(item, dict) and item.get("attempt_id") != self.attempt_id
        ]
        attempts.append({"attempt_id": self.attempt_id, "at": at})
        txn.set(
            carry.document(CARRY_INDEX_ID),
            {
                "tenant_id": self.tenant_id,
                "task_id": self.task_id,
                "attempts": attempts,
                "updated_at": at,
            },
        )

    # -- fencing -----------------------------------------------------------
    def validate_generation(self) -> ControlSignals:
        """THE safety gate. Raises FencedError if this worker is superseded.

        Called before the workspace is created, before credentials are read and
        before the runner is started. Everything below the raise is what does
        NOT happen when an attempt has been superseded.

        It is also the tenant gate: `fetch_task` and `fetch_lease` both refuse a
        document carrying another tenant's id, and the attempt document is
        checked here as well, so an attempt cannot be pointed at a task, a lease
        and an attempt that do not all belong to the same tenant.

        Called only before the runner, inside `startup_budget()`, so all three
        reads carry the startup budget, and a Firestore that cannot be reached
        raises within it rather than after 300 s of silent retries. The caller
        logs what it raised.
        """
        task = self.fetch_task()
        attempt_snap = self._attempt_ref().get(**self.call_options("poll"))
        if attempt_snap.exists:
            self._assert_tenant(
                attempt_snap.to_dict() or {}, kind="attempt", document_id=self.attempt_id
            )
        fence = _task_fence(task, generation=self.generation)
        if fence is not None:
            raise FencedError(self.generation, *fence)

        lease = self.fetch_lease()
        fence = _lease_fence(
            task,
            lease,
            generation=self.generation,
            task_id=self.task_id,
            lease_id=self.lease_id,
        )
        if fence is not None:
            raise FencedError(self.generation, *fence)
        assert lease is not None  # `_lease_fence` refuses a missing lease

        return ControlSignals(
            state=_as_state(task.get("state")),
            generation=int(task.get("current_generation", 0)),
            cancel_requested=bool(task.get("cancel_requested")),
            lease_released=False,
            lease_expires_at=_as_datetime(lease.get("expires_at")),
            observed_at=utcnow(),
        )

    def _fenced_task(self, txn: Any, *, write: str) -> dict[str, Any]:
        """Read the task and the lease THROUGH `txn`; refuse if this attempt is fenced.

        Returns the task as `txn` read it. The caller writes only after this
        returns, because Firestore forbids a read after a write inside a
        transaction. If the fence lands after these reads, the commit is
        refused and the body is run again against fresh reads, so the write is
        never made on a stale answer.

        Raises `FencedWriteRefused` with nothing written, and
        `TenantMismatchError` for a document that is not this tenant's.
        """
        # Through the transaction, and still under the budget inside the
        # startup window: `Transaction.get` is a `batch_get_documents` call,
        # with its 300 s default. `firestore.transactional` retries only an
        # ABORTED commit, never a read.
        options = self.call_options()
        task_snap = _snapshot(txn.get(self._task_ref(), **options))
        if not task_snap.exists:
            raise FencedWriteRefused(
                self.generation, -1, "task document no longer exists", write=write
            )
        task = self._assert_tenant(
            task_snap.to_dict() or {}, kind="task", document_id=self.task_id
        )
        fence = _task_fence(task, generation=self.generation)
        if fence is None:
            lease_snap = _snapshot(txn.get(self._lease_ref(), **options))
            lease = (
                self._assert_tenant(
                    lease_snap.to_dict() or {}, kind="lease", document_id=self.lease_id
                )
                if lease_snap.exists
                else None
            )
            fence = _lease_fence(
                task,
                lease,
                generation=self.generation,
                task_id=self.task_id,
                lease_id=self.lease_id,
            )
        if fence is not None:
            raise FencedWriteRefused(self.generation, *fence, write=write)
        return task

    def ensure_owner(self, *, write: str) -> None:
        """Refuse, with `FencedWriteRefused`, work this attempt no longer owns.

        It writes nothing. It is for work that must be refused BEFORE its first
        side effect. A checkpoint uploads its archive long before it writes the
        pointer to it, and a stale archive left in the task's prefix is work a
        superseded attempt should not have written (a restore no longer
        chooses by listing the prefix, #347). It uses the same
        predicate as every guarded write, so it cannot pass where a write would
        be refused. It does not replace the check in the write's own
        transaction, because the fence can still land in between.

        IT IS NOT A READ-ONLY TRANSACTION. `self._txn.run` in production is
        `FirestoreTransactionRunner.run`, which opens `db.transaction()`, and
        `read_only` defaults to False there. So this is a read-write
        transaction that reads the task and the lease and commits no writes.
        The cost is that Firestore holds its locks on those two documents for
        the two reads and the empty commit. A reconciler fencing this task at
        that instant waits for them, or one of the two transactions is aborted
        and retried by `firestore.transactional`. Nothing is written either
        way. A read-only transaction would take no locks. It was not used
        because `TransactionRunner` has one entry point, and no test in this
        repository runs the worker against a real Firestore transaction.
        """
        self._run_transaction(lambda txn: self._fenced_task(txn, write=write), call="checkpoint")

    # -- polling -----------------------------------------------------------
    def poll(self, provider: str | None = None) -> ControlSignals:
        """One read of task + lease (+ quota), used by the supervision loop.

        The quota preflight makes the one poll that comes before the runner,
        inside `startup_budget()`, and so under the budget. The supervision
        loop's polls carry `MID_RUN_BUDGETS["poll"]` (#70) on each read.
        """
        try:
            task = self.fetch_task()
        except FencedError:
            return ControlSignals(
                state=TaskState.CANCELLED,
                generation=-1,
                cancel_requested=False,
                lease_released=True,
                observed_at=utcnow(),
            )
        lease = self.fetch_lease() or {}
        provider_state: ProviderState | None = None
        provider_paused = False
        retry_after: int | None = None
        reset_at: datetime | None = None

        if provider:
            qsnap = self._quota_ref(provider).get(**self.call_options("poll"))
            if qsnap.exists:
                q = qsnap.to_dict() or {}
                try:
                    provider_state = ProviderState(q.get("state", ProviderState.UNKNOWN.value))
                except ValueError:
                    provider_state = ProviderState.UNKNOWN
                provider_paused = provider_state in (
                    ProviderState.EXHAUSTED,
                    ProviderState.COOLDOWN,
                    ProviderState.DISABLED,
                )
                retry_after = q.get("retry_after_seconds")
                reset_at = _as_datetime(q.get("reset_at")) or _as_datetime(q.get("cooldown_until"))
                if provider_paused:
                    # The broker already holds this stop. A park that follows
                    # from it must not report it back as a fresh 429.
                    self._note_rate_limit_known(
                        provider,
                        retry_after_seconds=int(retry_after) if retry_after is not None else None,
                        reset_at=reset_at,
                    )

        return ControlSignals(
            state=_as_state(task.get("state")),
            generation=int(task.get("current_generation", -1)),
            cancel_requested=bool(task.get("cancel_requested")),
            lease_released=lease.get("released_at") is not None or not lease,
            lease_expires_at=_as_datetime(lease.get("expires_at")),
            provider_state=provider_state,
            provider_paused=provider_paused,
            retry_after_seconds=int(retry_after) if retry_after is not None else None,
            quota_reset_at=reset_at,
            observed_at=utcnow(),
        )

    # -- events ------------------------------------------------------------
    def _event_write(
        self,
        event_type: EventType,
        detail: dict[str, Any] | None = None,
        *,
        at: datetime | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """The event document, and the reference it is written to.

        One shape for both ways an event is written: on its own (`emit`) and
        inside a fenced transaction (`transition`'s `events`).
        """
        event = TaskEvent(
            event_id=new_id("evt"),
            task_id=self.task_id,
            tenant_id=self.tenant_id,
            type=event_type,
            at=at or utcnow(),
            attempt_id=self.attempt_id,
            lease_id=self.lease_id,
            generation=self.generation,
            detail=detail or {},
        )
        ref = self._task_ref().collection("events").document(event.event_id)
        return ref, {
            "event_id": event.event_id,
            "task_id": event.task_id,
            "tenant_id": event.tenant_id,
            "type": event.type.value,
            "at": event.at,
            "attempt_id": event.attempt_id,
            "lease_id": event.lease_id,
            "generation": event.generation,
            "detail": event.detail,
        }

    def emit(
        self,
        event_type: EventType,
        detail: dict[str, Any] | None = None,
        *,
        at: datetime | None = None,
    ) -> None:
        ref, document = self._event_write(event_type, detail, at=at)
        # A retry after a DEADLINE_EXCEEDED that had landed rewrites the same
        # event id with the same bytes, so the read budget's predicate is safe.
        ref.set(document, **self.call_options("event"))
        self._log.info("event", event_type=event_type.value, detail=document["detail"])

    # -- state transitions -------------------------------------------------
    def _current_state(self) -> TaskState:
        return _as_state(self.fetch_task().get("state"))

    def transition(
        self,
        to_state: TaskState,
        *,
        fields: dict[str, Any] | None = None,
        events: Sequence[tuple[EventType, dict[str, Any]]] = (),
        parked_uploads: list[Any] | None = None,
    ) -> None:
        """Move the task to `to_state`, ONLY while this attempt still owns it.

        The state is read inside the same transaction as the fence, and the
        transition is checked against that read. The earlier version read the
        task and then updated it blind. On the way out of a fenced attempt,
        that parked a task the reconciler had fenced. It also wrote PARKED or
        FAILED over a newer attempt the scheduler had already admitted.

        `events` are written in the same transaction, ahead of the state
        change, so they commit with it or not at all. They are for an event
        that ANNOUNCES this write. Emitted on its own before the write, the
        announcement lands even when the write it announces is refused, in a
        stream that by then belongs to a newer generation.

        `parked_uploads`, from `park` only, is the parking attempt's upload
        manifest, recorded in the same transaction for the attempt that
        finishes the task (`_record_parked_uploads`, #166).

        Raises `FencedWriteRefused` with nothing written, events included.
        """
        write = f"transition to {to_state.value}"
        # Built once, outside the body. A contended transaction runs the body
        # again, and each run must write the same event ids, so the retry
        # cannot leave the announcement in the stream twice.
        announced = [self._event_write(kind, detail) for kind, detail in events]

        def _apply(txn: Any) -> None:
            task = self._fenced_task(txn, write=write)
            current = _as_state(task.get("state"))
            if current is not to_state:
                assert_transition(current, to_state)
            index = self._read_carry_index(txn) if parked_uploads else None
            for ref, document in announced:
                txn.set(ref, document)
            if parked_uploads:
                self._record_parked_uploads(txn, index, parked_uploads, at=utcnow())
            if current is to_state:
                if fields:
                    txn.update(self._task_ref(), {**fields, "updated_at": utcnow()})
                return
            payload: dict[str, Any] = {"state": to_state.value, "updated_at": utcnow()}
            payload.update(fields or {})
            txn.update(self._task_ref(), payload)

        self._run_transaction(_apply)
        for _, document in announced:
            self._log.info("event", event_type=document["type"], detail=document["detail"])

    def advance_to_starting(self) -> bool:
        """Walk LEASED -> DISPATCHED -> STARTING, stopping there.

        The window in which the worker registers its child-task attempt key
        (docs/design/child-tasks.md §3.2 step 3): swarm-api accepts a
        registration only while the task is STARTING, and the agent is spawned
        only after RUNNING. `advance_to_running` finishes the walk from here.

        Writes no STARTING event: the caller emits the one STARTING event once
        the registration is decided, so its outcome rides on it as detail
        rather than on a second STARTING. Returns True when this call made the
        transition, i.e. when that event is owed.
        """
        state = self._current_state()
        if state is TaskState.LEASED:
            self.transition(TaskState.DISPATCHED)
            state = TaskState.DISPATCHED
        if state is TaskState.DISPATCHED:
            self.transition(TaskState.STARTING, fields={"started_at": utcnow()})
            return True
        return False

    def cancel_cause(self) -> EndCause:
        """`cancel_end_cause` of the task as it is now. One read, on a cancel only."""
        try:
            return cancel_end_cause(self.fetch_task())
        except Exception:  # the cause must never stop the cancel itself
            return EndCause.CANCEL_REQUESTED

    def advance_to_running(self) -> None:
        """Walk LEASED -> DISPATCHED -> STARTING -> RUNNING legally.

        The dispatcher normally sets DISPATCHED, but a worker that starts faster
        than that write lands here at LEASED, and skipping a hop would trip the
        frozen state machine rather than silently corrupting it.
        """
        state = self._current_state()
        if state is TaskState.LEASED:
            self.transition(TaskState.DISPATCHED)
            state = TaskState.DISPATCHED
        if state is TaskState.DISPATCHED:
            self.transition(TaskState.STARTING, fields={"started_at": utcnow()})
            self.emit(EventType.STARTING)
            state = TaskState.STARTING
        if state is TaskState.STARTING:
            self.transition(TaskState.RUNNING)
            self.emit(EventType.RUNNING)
            return
        if state is TaskState.RUNNING:
            return
        raise ControlPlaneError(f"cannot start from state {state.value}")

    # -- attempt bookkeeping ----------------------------------------------
    def record_attempt_start(self, *, backend: str, execution_name: str | None) -> None:
        attempt = Attempt(
            attempt_id=self.attempt_id,
            task_id=self.task_id,
            tenant_id=self.tenant_id,
            generation=self.generation,
            lease_id=self.lease_id,
            backend=backend,
            created_at=utcnow(),
            execution_name=execution_name,
            started_at=utcnow(),
        )
        self._attempt_ref().set(
            {
                "attempt_id": attempt.attempt_id,
                "task_id": attempt.task_id,
                "tenant_id": attempt.tenant_id,
                "generation": attempt.generation,
                "lease_id": attempt.lease_id,
                "backend": attempt.backend,
                "created_at": attempt.created_at,
                "execution_name": attempt.execution_name,
                "started_at": attempt.started_at,
                "completed_at": None,
                "exit_code": None,
                "error": None,
                "peak_rss_bytes": None,
                "peak_disk_bytes": None,
                "oom_near_miss": False,
                "checkpoints": [],
            },
            # The attempt's first write, and the first write of all. Made
            # inside `startup_budget()`, like the reads before it.
            **self.call_options(),
        )

    def record_checkpoint(
        self,
        *,
        checkpoint_id: str,
        uri: str,
        size_bytes: int,
        seq: int,
        archive_sha256: str,
    ) -> None:
        # THE DIGEST IS REQUIRED (#346). It used to default to None, and a
        # caller that forgot it recorded a checkpoint no restore would accept.
        # Refused before anything is written: a record without it is no record.
        if not isinstance(archive_sha256, str) or not ARCHIVE_DIGEST_RE.fullmatch(
            archive_sha256
        ):
            raise ValueError(
                f"checkpoint {checkpoint_id} has no archive digest to record "
                "(a SHA-256 hex digest is required)"
            )
        # Read-modify-write rather than ArrayUnion: exactly one worker owns an
        # attempt document, so there is no contention to serialise, and this
        # keeps the Firestore sentinel types out of the worker's hot path.
        options = self.call_options("checkpoint")
        snap = self._attempt_ref().get(**options)
        existing: list[str] = []
        digests: dict[str, str] = {}
        if snap.exists:
            data = self._assert_tenant(
                snap.to_dict() or {}, kind="attempt", document_id=self.attempt_id
            )
            existing = list(data.get("checkpoints", []))
            # Read back in the typed shape (`Attempt.checkpoint_sha256`,
            # contract request 51), so the merge below writes only that shape.
            digests = recorded_checkpoint_digests(data)
        if checkpoint_id not in existing:
            existing.append(checkpoint_id)
        # What the next attempt binds the archive's bytes to (#347): the
        # manifest that also carries this digest sits in the bucket, which
        # every agent of the tenant can write; this document does not.
        digests[checkpoint_id] = archive_sha256
        fields: dict[str, Any] = {
            "checkpoints": existing,
            CHECKPOINT_DIGESTS_FIELD: digests,
            "tenant_id": self.tenant_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
        }
        # merge-set, not update: an attempt cancelled before it started has no
        # attempt document yet, and losing the record would be worse than
        # creating it late. `tenant_id`, `task_id` and `attempt_id` go in every
        # merge so a document this path creates is never one the tenant check,
        # or the retry's `_recorded_checkpoint` (which requires this task's
        # id, #346), would later refuse.
        self._attempt_ref().set(fields, merge=True, **options)

        # FENCED LIKE A TRANSITION. `latest_checkpoint` is what the next
        # attempt restores from. A stale worker repointing it would hand the
        # stale attempt's work to whatever runs after a newer attempt.
        def _point(txn: Any) -> None:
            self._fenced_task(txn, write="checkpoint pointer")
            txn.update(self._task_ref(), {"latest_checkpoint": uri, "updated_at": utcnow()})

        self._run_transaction(_point, call="checkpoint")
        self.emit(
            EventType.CHECKPOINT_COMPLETED,
            {"checkpoint_id": checkpoint_id, "uri": uri, "size_bytes": size_bytes, "seq": seq},
        )

    def record_resource_usage(
        self, *, peak_rss_bytes: int, peak_disk_bytes: int, oom_near_miss: bool
    ) -> None:
        self._attempt_ref().set(
            {
                "peak_rss_bytes": int(peak_rss_bytes),
                "peak_disk_bytes": int(peak_disk_bytes),
                "oom_near_miss": bool(oom_near_miss),
                "tenant_id": self.tenant_id,
            },
            merge=True,
            **self.call_options("attempt"),
        )

    def record_cpu_usage(self, fields: Mapping[str, Any]) -> None:
        """The attempt's CPU, onto the attempt, as typed fields (requests #15 and #26).

        `fields` is `metrics.attempt_cpu_fields`: the attempt's own figures,
        every runner combined, with anything not measured already left out.
        Only the keys the frozen `Attempt` declares are written: the four
        figures, and only as numbers -- `bool` excluded, for the reason
        `record_spend` gives -- and `cpu_limit_source`, only as one of
        `metrics.CPU_LIMIT_SOURCES`. A key that is absent is left as it is on
        the document: the write is a merge, so a null would erase a figure an
        earlier write recorded.

        `cpu_measured_at` (request #26, accepted on #184, 2026-09-26) is
        stamped HERE, on every write that carries a figure, with this worker's
        clock: it is when the reading was written, which is what a reader
        ages. It is never written without a figure beside it.

        The attempt document is this attempt's own. A superseded attempt still
        writes it, as it writes its memory peak -- the fence guards the task,
        its lease and its event stream, which this does not touch.
        """
        from .metrics import ATTEMPT_CPU_FIELDS, CPU_LIMIT_SOURCES

        doc: dict[str, Any] = {
            key: float(fields[key])
            for key in ATTEMPT_CPU_FIELDS
            if isinstance(fields.get(key), (int, float)) and not isinstance(fields.get(key), bool)
        }
        # A FIGURE, not a limit alone: a limit with nothing measured would be
        # dated as a reading the attempt never took (`attempt_cpu_fields`
        # never builds one, and this does not write one either).
        if not any(key != "cpu_limit_cores" for key in doc):
            return
        source = fields.get("cpu_limit_source")
        if source in CPU_LIMIT_SOURCES and "cpu_limit_cores" in doc:
            doc["cpu_limit_source"] = source
        doc["cpu_measured_at"] = utcnow()
        doc["tenant_id"] = self.tenant_id
        self._attempt_ref().set(doc, merge=True, **self.call_options("attempt"))

    def record_spend(self, usage: dict[str, Any]) -> None:
        """Token counts and cost, onto the attempt, as typed fields.

        `usage` is what `lifecycle._usage_summary` pulled out of the CLI result
        BEFORE truncation. Only the keys the frozen `Attempt` declares are
        written: the summary also carries num_turns, durations and a model list,
        which belong in the runner summary rather than as columns on an attempt.

        A key the runner did not report is OMITTED, not written as zero. None
        means "not reported" and zero means "cost nothing", and a mock task is
        genuinely the second while a result that failed to parse is the first.
        """
        if not usage:
            return
        fields = (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
        # `not isinstance(v, bool)` on every one of them: bool subclasses int in
        # Python, so `input_tokens: True` would land as a token count of 1. A
        # silently wrong number is the failure mode this whole field set exists
        # to avoid, and it is cheaper to exclude the type than to explain the
        # number later.
        doc: dict[str, Any] = {
            key: int(usage[key])
            for key in fields
            if isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool)
        }
        cost = usage.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            doc["cost_usd"] = float(cost)
        if not doc:
            return
        doc["tenant_id"] = self.tenant_id
        self._attempt_ref().set(doc, merge=True, **self.call_options("attempt"))

    def record_attempt_end(self, *, exit_code: int | None, error: str | None) -> None:
        self._attempt_ref().set(
            {
                "completed_at": utcnow(),
                "exit_code": exit_code,
                "error": error,
                "tenant_id": self.tenant_id,
            },
            merge=True,
            **self.call_options("attempt"),
        )

    # -- heartbeat ---------------------------------------------------------
    def heartbeat(self) -> bool:
        """Extend THIS attempt's lease, only while it is still this attempt's (#70).

        CONDITIONAL ON THE LEASE. The beat reads the lease and writes it in one
        transaction, and writes only when the lease is not released and is at
        this attempt's generation, for this task. It used to be a blind
        `update` of `heartbeat_at` and `expires_at`: a beat retried across an
        outage, or one in flight while the reconciler released the lease, landed
        on a released lease and gave it a fresh expiry (invariant 5). A lease
        written between the read and the write aborts the commit, and the body
        is run again against the new read.

        Returns True when the lease was extended, False when it was refused
        (released, another generation, another task, or gone), with nothing
        written. A refusal is not raised: the control poll meets the fence
        that caused it and acts on it, as it always has. A lease document of
        another tenant raises `TenantMismatchError`, as every read here does.

        Its budget is `MID_RUN_BUDGETS["heartbeat"]`, under the extension it
        grants; inside `startup_budget()`, the startup budget. The payload is
        built once, before the transaction, so a retry writes the same values.
        """
        now = utcnow()
        payload = {
            "heartbeat_at": now,
            "expires_at": now + timedelta(seconds=self._heartbeat_extension),
        }
        options = self.call_options("heartbeat")

        def _beat(txn: Any) -> bool:
            snap = _snapshot(txn.get(self._lease_ref(), **options))
            if not snap.exists:
                return False
            lease = self._assert_tenant(
                snap.to_dict() or {}, kind="lease", document_id=self.lease_id
            )
            if (
                lease.get("released_at") is not None
                or int(lease.get("generation", -1)) != self.generation
                or lease.get("task_id") != self.task_id
            ):
                return False
            txn.update(self._lease_ref(), payload)
            return True

        written = bool(self._run_transaction(_beat, call="heartbeat"))
        if not written:
            self._log.warning(
                "heartbeat refused: the lease is released, gone, or at another "
                "generation; nothing was written",
                lease_id=self.lease_id,
                generation=self.generation,
            )
        return written

    # -- quota -------------------------------------------------------------
    def _note_rate_limit_known(
        self,
        provider: str,
        *,
        retry_after_seconds: int | None,
        reset_at: datetime | None,
    ) -> None:
        now = utcnow()
        candidates: list[datetime] = []
        if retry_after_seconds is not None:
            candidates.append(now + timedelta(seconds=max(0, int(retry_after_seconds))))
        if reset_at is not None:
            candidates.append(reset_at if reset_at.tzinfo else reset_at.replace(tzinfo=now.tzinfo))
        # The same default the park decision uses for an unknown wait
        # (`quota.QuotaSignal.wait_seconds`), so the window this worker treats
        # as one event is the window it parked for.
        until = max(candidates) if candidates else now + timedelta(seconds=UNKNOWN_WAIT_SECONDS)
        known = self._rate_limit_known_until.get(provider)
        self._rate_limit_known_until[provider] = until if known is None else max(known, until)

    def update_quota_state(
        self,
        *,
        provider: str,
        state: ProviderState,
        retry_after_seconds: int | None = None,
        reset_at: datetime | None = None,
    ) -> bool:
        """Report what this worker learned about the provider to the quota broker.

        THE BROKER IS THE SINGLE WRITER of `quota/{provider}:{tenant}` and of
        the provider pools' caps, and it applies AIMD to every report
        (`quota_broker.aimd`): a 429 halves the adaptive target, a success
        counts toward the next additive increase, and `exhaustion_threshold`
        consecutive 429s mark the provider EXHAUSTED. This method writes no
        Firestore document. The rate-limit-run rules this method used to apply
        to `rate_limit_count` (CP-10, #85) are the broker's now
        (`aimd._run_count`, `aimd.record_success`).

        Which route (`QUOTA_REPORT_ROUTES`): AVAILABLE is a success; THROTTLED
        and EXHAUSTED are a 429. Any other state is the broker's own, read back
        by `poll`, and is not reported.

        ONE REPORT PER RATE-LIMIT WINDOW. The lifecycle reports a runner's 429
        when it reads `quota.json` and again as it parks on it, and a park on
        the broker's own stop (preflight, backpressure) reports the stop it
        read. Each of those would be another halving and another step toward
        EXHAUSTED for a single provider answer. So a 429 report while a window
        this worker already reported -- or read from the broker -- is still
        open is not sent: the broker already has it. A short wait that sleeps
        the window out and meets a fresh 429 is reported, because the window
        it would have matched has closed. A success always goes, and closes
        the window.

        NEVER RAISES AND NEVER WAITS LONGER THAN ONE SHORT CALL. A failed
        report is logged and dropped; no retry. The caller's park-and-exit on a
        429 (invariant 4) does not depend on the broker hearing about it: the
        park, the lease release and the exit go ahead either way, and the next
        report from any worker on this tenant brings the broker up to date.

        Returns True when the broker accepted a report.
        """
        route = QUOTA_REPORT_ROUTES.get(state)
        if route is None:
            self._log.info(
                "provider state not reported: it is the broker's own, not an observation",
                provider=provider,
                provider_state=state.value,
            )
            return False
        now = utcnow()
        if route == "rate-limit":
            known = self._rate_limit_known_until.get(provider)
            if known is not None and now < known:
                self._log.info(
                    "rate limit not reported again: the broker already has this window",
                    provider=provider,
                    known_until=known.isoformat(),
                )
                return False
        if self._quota_reporter is None:
            self._log.warning(
                "provider outcome not reported: no quota broker is configured "
                "(QUOTA_BROKER_URL is unset)",
                provider=provider,
                provider_state=state.value,
            )
            return False
        body: dict[str, Any] = {}
        if route == "rate-limit" and retry_after_seconds is not None:
            # The broker bounds it (0..86400); a provider asking for longer is
            # capped there, by `AimdConfig.max_cooldown_seconds`, anyway.
            body["retry_after_seconds"] = max(0, min(int(retry_after_seconds), 86_400))
        if route == "rate-limit" and reset_at is not None:
            body["reset_at"] = reset_at.isoformat()
        try:
            self._quota_reporter.report(
                provider=provider, tenant_id=self.tenant_id, route=route, body=body
            )
        except Exception as exc:
            self._log.warning(
                "could not report a provider outcome to the quota broker; carrying on",
                provider=provider,
                provider_state=state.value,
                route=route,
                error=str(exc)[:300],
            )
            return False
        if route == "rate-limit":
            self._note_rate_limit_known(
                provider, retry_after_seconds=retry_after_seconds, reset_at=reset_at
            )
        else:
            self._rate_limit_known_until.pop(provider, None)
        return True

    # -- lease release -----------------------------------------------------
    def release_lease(self, reason: str) -> bool:
        """Return capacity. Idempotent, and never called on the fenced path."""
        if self._lease_released:
            return False

        def _release(txn: Any) -> bool:
            return release_lease_in_transaction(
                txn, db=self._db, lease_id=self.lease_id, reason=reason
            )

        released = bool(self._run_transaction(_release))
        self._lease_released = True
        if released:
            self.emit(EventType.LEASE_RELEASED, {"reason": reason})
        return released

    # -- terminal outcomes -------------------------------------------------
    def park(
        self,
        *,
        reason: ParkReason,
        next_eligible_at: datetime,
        detail: dict[str, Any] | None = None,
        announce: Sequence[tuple[EventType, dict[str, Any]]] = (),
    ) -> None:
        """Quota path: checkpointed already, now give the slot back and exit.

        Order matters. The task leaves the concurrency states first, so that the
        instant the pools are decremented there is no document claiming this
        task is still running.

        The transition is fenced (see `transition`). A superseded attempt gets
        `FencedWriteRefused` here, before the event and before the release, so
        it writes nothing.

        `announce` is for an event that says this park is happening, such as
        QUOTA_EXHAUSTED. It is written in the park's own transaction, ahead of
        the state change. A caller that emitted it before calling here would
        leave it in the task's stream when the park is refused: a fence that
        lands during the park's uploads is met here, after the announcement.

        THE ATTEMPT'S END IS WRITTEN AFTER THE TRANSITION (#163, owner decision
        2026-09-28): exit 75 (`ExitCode.PARKED`) and the park reason as its
        `error`, the order `finish` uses. Without it a parked attempt's own
        document kept `completed_at: None` and read as still running, in the
        timeline and to the checkpoint collector. A fenced park raises in the
        transition, so it closes no document.

        THE UPLOADS ARE RECORDED IN THE SAME TRANSACTION (#166): a park that
        uploaded passes its manifest as `detail["artifacts"]`, and it is
        written where the finishing attempt reads it by id
        (`parked_uploads`). A fenced park records nothing.
        """
        uploads = (detail or {}).get("artifacts")
        self.transition(
            TaskState.PARKED,
            fields={
                "park_reason": reason.value,
                "next_eligible_at": next_eligible_at,
                "current_lease_id": None,
                "blocked_by": [{"reason": reason.value, **(detail or {})}],
            },
            events=announce,
            parked_uploads=uploads if isinstance(uploads, list) else None,
        )
        # Best effort: the park has landed, and a failure here must not stop
        # the event and the lease release below, which give the slot back.
        try:
            self.record_attempt_end(exit_code=ExitCode.PARKED, error=reason.value)
        except Exception as exc:
            self._log.warning(
                "the park landed but its attempt end was not recorded",
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
            )
        self.emit(
            EventType.PARKED,
            {"reason": reason.value, "next_eligible_at": next_eligible_at, **(detail or {})},
        )
        self.release_lease(f"parked:{reason.value}")

    def park_awaiting_children(
        self,
        *,
        max_resumes: int,
        detail: dict[str, Any] | None = None,
        uploads: list[Any] | None = None,
    ) -> bool:
        """The await park (docs/design/child-tasks.md §3.3): checkpointed and
        uploaded already; now PARKED on CHILDREN_INCOMPLETE, slot given back.

        ONE FENCED TRANSACTION, as every park: a superseded attempt gets
        `FencedWriteRefused` with nothing written, so it can neither park the
        task nor refund an attempt (invariant 5). Inside it, the attempt
        admission counted is refunded -- `attempt_count` down by one,
        `metadata.child_await_resumes` up by one -- while fewer than
        `max_resumes` have been, so waiting is not failing; past the bound the
        park counts like any attempt and a parent that awaits for ever still
        ends at `max_attempts` (§5 F14). `next_eligible_at` is the instant of
        the park, which the scheduler's await deadline is measured from; the
        scheduler's sweep, not a clock, promotes it.

        Returns whether the attempt was refunded.

        THE ATTEMPT'S END IS WRITTEN AFTER THE TRANSACTION, as `park` writes
        it (#163): exit 75 and CHILDREN_INCOMPLETE as its `error`. A refund
        gives back the attempt's COUNT, not the attempt: a resume is a new
        attempt document, so this one has ended. Without it a parent cancelled
        while it waited read as one attempt still running. A fenced await park
        raises in the transaction and closes no document.

        `uploads`, the manifest of what this attempt uploaded before the park,
        is recorded in the same transaction, as `park` records it (#166), for
        the attempt that resumes the parent and finishes it.
        """
        write = "await park"
        reason = ParkReason.CHILDREN_INCOMPLETE
        now = utcnow()

        def _apply(txn: Any) -> tuple[bool, int]:
            task = self._fenced_task(txn, write=write)
            current = _as_state(task.get("state"))
            assert_transition(current, TaskState.PARKED)
            index = self._read_carry_index(txn) if uploads else None
            metadata = dict(task.get("metadata") or {})
            used = metadata.get(CHILD_AWAIT_RESUMES_METADATA_KEY)
            used = used if isinstance(used, int) and not isinstance(used, bool) and used >= 0 else 0
            attempt_count = int(task.get("attempt_count", 0))
            refund = used < max_resumes and attempt_count > 0
            payload: dict[str, Any] = {
                "state": TaskState.PARKED.value,
                "park_reason": reason.value,
                "next_eligible_at": now,
                "current_lease_id": None,
                "blocked_by": [{"reason": reason.value, **(detail or {})}],
                "updated_at": now,
            }
            if refund:
                metadata[CHILD_AWAIT_RESUMES_METADATA_KEY] = used + 1
                payload["metadata"] = metadata
                payload["attempt_count"] = attempt_count - 1
            txn.update(self._task_ref(), payload)
            if uploads:
                self._record_parked_uploads(txn, index, uploads, at=now)
            return refund, used + (1 if refund else 0)

        refunded, resumes = self._run_transaction(_apply)
        # Best effort, as in `park`: the park has landed, and the event and
        # the release below are what give the slot back.
        try:
            self.record_attempt_end(exit_code=ExitCode.PARKED, error=reason.value)
        except Exception as exc:
            self._log.warning(
                "the await park landed but its attempt end was not recorded",
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
            )
        self.emit(
            EventType.PARKED,
            {
                "reason": reason.value,
                "next_eligible_at": now,
                "attempt_refunded": refunded,
                CHILD_AWAIT_RESUMES_METADATA_KEY: resumes,
                **(detail or {}),
            },
        )
        self.release_lease(f"parked:{reason.value}")
        return refunded

    def park_ci_pending(
        self,
        *,
        code: str,
        head: str,
        pull_request: int,
        pending: Sequence[str],
        max_wakes: int,
        fallback_seconds: int,
        counter: str = "wakes",
        ci_clock: bool = True,
    ) -> bool:
        """A merge step's CI wait: PARKED on CI_PENDING, slot given back.

        docs/merge-step.md "Revised 2026-10-06" §1, the await park's shape
        exactly (`park_awaiting_children`). ONE FENCED TRANSACTION: a
        superseded attempt gets `FencedWriteRefused` with nothing written, so
        it can neither park the task nor refund an attempt (invariant 5).
        Inside it:

          * the attempt admission counted is refunded -- `attempt_count` down
            by one, `merge_wait.wakes` up by one -- while fewer than
            `max_wakes` have been, so a slow CI does not use up the step's
            attempts; past the bound the park counts like any attempt, and a
            pull request whose CI never settles still ends at `max_attempts`;
          * `metadata.merge_wait` records what is waited on: the head the
            checks were read at, the pull request, the code and the pending
            names; `first_parked_at` and `updates` are carried over from the
            last park, and `wake_requested_at` is NOT -- this park waits for a
            mark made after it;
          * `next_eligible_at` is the park instant plus `fallback_seconds`:
            the scheduler wakes the park then even if swarm-api's tick never
            marks it, so a dead tick or a broken token never strands a merge.

        A wait for the repository's merge slot (`mergeslot.MERGE_SLOT_WAIT`,
        merge race #295) is refunded on its own `counter`, `slot_waits`,
        with its own `max_wakes`, and stops the CI clock (`ci_clock` False):
        its `first_parked_at` is cleared, so the next CI wait starts it
        afresh and a long queue never reads as `checks_timeout`.

        There is no checkpoint and no upload: a worker action keeps no
        workspace (invariant 8's exception, CR 36), and its durable state is
        this record plus GitHub, which the next attempt reads again.

        Returns whether the attempt was refunded. The attempt's end, the event
        and the release follow the transaction, as in every park.
        """
        write = "ci park"
        reason = ParkReason.CI_PENDING
        now = utcnow()
        names = [str(name) for name in pending]

        def _apply(txn: Any) -> tuple[bool, int]:
            task = self._fenced_task(txn, write=write)
            current = _as_state(task.get("state"))
            assert_transition(current, TaskState.PARKED)
            metadata = dict(task.get("metadata") or {})
            last = metadata.get(MERGE_WAIT_METADATA_KEY)
            last = last if isinstance(last, Mapping) else {}
            counts = {name: _count(last.get(name)) for name in ("wakes", "slot_waits")}
            counts.setdefault(counter, _count(last.get(counter)))
            attempt_count = int(task.get("attempt_count", 0))
            refund = counts[counter] < max_wakes and attempt_count > 0
            if refund:
                counts[counter] += 1
            wakes = counts[counter]
            first = last.get("first_parked_at")
            metadata[MERGE_WAIT_METADATA_KEY] = {
                "code": code,
                "head": head,
                "pull_request": pull_request,
                "pending": names,
                **counts,
                "updates": _count(last.get("updates")),
                "first_parked_at": (
                    (first if isinstance(first, datetime) else now) if ci_clock else None
                ),
                "parked_at": now,
            }
            payload: dict[str, Any] = {
                "state": TaskState.PARKED.value,
                "park_reason": reason.value,
                "next_eligible_at": now + timedelta(seconds=fallback_seconds),
                "current_lease_id": None,
                "blocked_by": [
                    {"reason": reason.value, "code": code, "head": head, "pending": names}
                ],
                "metadata": metadata,
                "updated_at": now,
            }
            if refund:
                payload["attempt_count"] = attempt_count - 1
            txn.update(self._task_ref(), payload)
            return refund, wakes

        refunded, wakes = self._run_transaction(_apply)
        # Best effort, as in `park`: the park has landed, and the event and
        # the release below are what give the slot back.
        try:
            self.record_attempt_end(exit_code=ExitCode.PARKED, error=reason.value)
        except Exception as exc:
            self._log.warning(
                "the CI park landed but its attempt end was not recorded",
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
            )
        self.emit(
            EventType.PARKED,
            {
                "reason": reason.value,
                "next_eligible_at": now + timedelta(seconds=fallback_seconds),
                "attempt_refunded": refunded,
                "wakes": wakes,
                "code": code,
                "head": head,
                "pending": names,
            },
        )
        self.release_lease(f"parked:{reason.value}")
        return refunded

    def finish(
        self,
        *,
        state: TaskState,
        exit_code: int | None,
        error: str | None = None,
        result_summary: dict[str, Any] | None = None,
        end_cause: EndCause | None = None,
    ) -> None:
        """Persist a terminal state, record the attempt's end, release the lease.

        The transition is fenced (see `transition`), and it comes first. A
        superseded attempt gets `FencedWriteRefused` before anything else is
        written: no attempt end, no event, no release.

        `end_cause` is WRITTEN ON EVERY TERMINAL STATE, None included (contract
        request 23): a success carries None, and so does an end no cause names.
        Written rather than omitted so the document says what this write
        decided, whatever an earlier write left there.
        """
        if state not in TERMINAL_STATES:
            raise ControlPlaneError(f"{state.value} is not terminal")
        fields: dict[str, Any] = {
            "completed_at": utcnow(),
            "current_lease_id": None,
            "last_error": error,
            "end_cause": end_cause.value if end_cause is not None else None,
        }
        if result_summary is not None:
            fields["result_summary"] = result_summary
        self.transition(state, fields=fields)
        self.record_attempt_end(exit_code=exit_code, error=error)
        event = {
            TaskState.SUCCEEDED: EventType.SUCCEEDED,
            TaskState.FAILED: EventType.FAILED,
            TaskState.CANCELLED: EventType.CANCELLED,
            TaskState.DEAD_LETTERED: EventType.DEAD_LETTERED,
        }[state]
        self.emit(event, {"exit_code": exit_code, "error": error})
        self.release_lease(f"terminal:{state.value}")
        self._announce_finished(state)

    def _announce_finished(self, state: TaskState) -> None:
        """Ring the scheduler: this task has ended, its dependants may run (#636).

        Last, after the terminal write and the lease release, so the
        scheduler that wakes sees the parent ended and the capacity back. A
        wake that fails is logged and nothing else -- the safety tick releases
        the dependants as it always has -- so this never raises.
        """
        if self._finish_announcer is None:
            return
        try:
            published = bool(
                self._finish_announcer.announce(
                    task_id=self.task_id, tenant_id=self.tenant_id, state=state
                )
            )
            outcome = "published" if published else "refused"
        except Exception as exc:
            # The type only: a transport error's text can name the request.
            outcome = f"error:{type(exc).__name__}"
        self._log.info("finish wake", state=state.value, outcome=outcome)

    def fail_retryably(
        self,
        *,
        exit_code: int | None,
        error: str,
        cause: str,
        result_summary: dict[str, Any] | None = None,
        retry_delay_seconds: int = 0,
        detail: dict[str, Any] | None = None,
        end_cause: EndCause | None = None,
    ) -> TaskState:
        """End this ATTEMPT as failed, and send the task back to READY if it has
        attempts left. Returns the state the task was left in.

        `end_cause` is what the task ends as if this attempt was its last and it
        ends FAILED. A CANCELLED end is always CANCEL_REQUESTED, because only a
        requested cancel picks it; READY ends nothing and writes no cause.

        `finish(FAILED)` ends the task for good. This ends only the attempt.
        One fenced transaction reads the task and picks the target:

          * CANCELLED when a cancel was requested. The user's decision survives
            a retry, as it does in the reconciler's `repair_task_state`.
          * FAILED once `attempt_count` has reached `max_attempts`, decided by
            `swarm_common.models.retries_exhausted`. That is the predicate the
            scheduler and the reconciler apply, not a second copy of it.
          * READY otherwise, with `next_eligible_at`. The scheduler admits it
            on a later drain as a new attempt, which acquires a new lease with
            a new generation. RUNNING -> READY is legal in the frozen state
            machine.

        The count is read from the DOCUMENT inside the transaction, never from
        the task this worker fetched when it started. The scheduler's
        dispatch-failure path decided from its own snapshot and was one attempt
        behind (`task_b568a623be8645eb87c6`, 2026-09-24). `max_attempts` is the
        other way round: it is covered by the spec signature, so once the spec
        is verified it comes from the verified copy
        (`pin_verified_max_attempts`, #346), never from the live field.

        Then the attempt's end, one event and the lease release, in the order
        `finish` uses. The task leaves the concurrency states before the pools
        are decremented. The event for READY is RETRYING with `cause`, because
        the task is not terminal and an event type that says it is would be
        read that way (see `EventType.CANCEL_REQUESTED`).

        Raises `FencedWriteRefused` with nothing written.
        """
        write = "retryable failure"
        now = utcnow()

        def _apply(txn: Any) -> tuple[TaskState, int, int]:
            task = self._fenced_task(txn, write=write)
            current = _as_state(task.get("state"))
            attempt_count = int(task.get("attempt_count", 0))
            # The verified spec's value once there is one (#346); before the
            # spec is verified, the document is all there is.
            pinned = self._verified_max_attempts
            max_attempts = int(
                task.get("max_attempts", 3) if pinned is _UNPINNED else pinned
            )
            if task.get("cancel_requested"):
                target = TaskState.CANCELLED
            elif retries_exhausted(attempt_count, max_attempts):
                target = TaskState.FAILED
            else:
                target = TaskState.READY
            assert_transition(current, target)
            payload: dict[str, Any] = {
                "state": target.value,
                "updated_at": now,
                "current_lease_id": None,
                "last_error": (
                    f"cancelled on request; {error}" if target is TaskState.CANCELLED else error
                ),
            }
            if result_summary is not None:
                payload["result_summary"] = result_summary
            if target is TaskState.READY:
                payload["next_eligible_at"] = now + timedelta(
                    seconds=max(0, retry_delay_seconds)
                )
            else:
                # Terminal. CLEARED, not omitted: a retry time on a task that
                # will never run again is a promise nothing keeps.
                payload["completed_at"] = now
                payload["next_eligible_at"] = None
                ended_as = (
                    cancel_end_cause(task) if target is TaskState.CANCELLED else end_cause
                )
                payload["end_cause"] = ended_as.value if ended_as is not None else None
            txn.update(self._task_ref(), payload)
            return target, attempt_count, max_attempts

        target, attempt_count, max_attempts = self._run_transaction(_apply)
        self.record_attempt_end(exit_code=exit_code, error=error)
        event = {
            TaskState.READY: EventType.RETRYING,
            TaskState.FAILED: EventType.FAILED,
            TaskState.CANCELLED: EventType.CANCELLED,
        }[target]
        self.emit(
            event,
            {
                "exit_code": exit_code,
                "error": error,
                "cause": cause,
                **(detail or {}),
                "to_state": target.value,
                "attempt_count": attempt_count,
                "max_attempts": max_attempts,
                "retries_exhausted": target is TaskState.FAILED,
            },
        )
        self.release_lease(
            f"retry:{cause}" if target is TaskState.READY else f"terminal:{target.value}"
        )
        if target is not TaskState.READY:
            self._announce_finished(target)
        return target
