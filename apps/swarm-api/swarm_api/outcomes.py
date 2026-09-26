"""Outcomes over a span: what the platform's work ended as, bucketed by when it ended.

WHY THIS FILE EXISTS. "How did the historic runs go over the last two weeks" was
answered by the Timeline stacking outcomes (placed by the day work ended) on top
of still-open work (placed by the day it was submitted) inside one column, over
the newest 200 tasks. A column's height measured nothing, and on dev 416
cancels flattened 28 failures (16-25 Sep 2026). Issue #185 records the owner's
decisions; this module is the server half of them, `GET /v1/outcomes`.

ONE BASIS PER FIGURE, AND THE RESPONSE SAYS WHICH. Every outcome figure is
placed by the task's `completed_at`. Exactly one series is not: `submitted`, the
throughput lane's arrivals, placed by `created_at` (owner decision, 2026-09-25).
The response names both under `basis`, so no client restates which is which.

THE RATE EXCLUDES CANCELS (owner decision). k = succeeded, n = succeeded +
failed + dead_lettered. A bucket with nothing decided has no rate -- null, drawn
as a gap -- never 0 %. NULL MEANS NOT KNOWN, NEVER ZERO, everywhere below: a 0 is
always a measurement, and a bucket that could not be read carries no numbers at
all rather than a partial sum.

THE SHAPE IS rollup.py's: DERIVE, WRITE, DRIFT CHECK.

  * DERIVE (`Outcomes.derive_day`) reads one tenant's tasks that ended in one UTC
    day, their attempts and their workflow parents, plus the tasks that ARRIVED
    that day, and turns each into a small tuple (`tuple_from_docs`). It is the
    only implementation: no copy in the scheduler, the worker or the UI.
  * WRITE persists those tuples as `outcome_days/{tenant}_{YYYY-MM-DD}`, so a
    30-day view reads ~31 documents instead of every task and attempt in the
    span. A sealed day is written once; the live day at most once a minute per
    tenant. Write-on-read follows the workflow read routes' precedent.
  * DRIFT CHECK (`Outcomes.maintain`, `POST /v1/admin/outcomes/rollup`)
    re-derives stored sealed days and REPORTS disagreement; it repairs only
    when asked. `agrees` is three-valued exactly as in `rollup.drift_of`.

WHY A SEALED DAY IS COMPLETE. Every tuple is a task that is already terminal.
The worker records spend before its terminal write (`lifecycle._upload_outputs`
calls `_record_spend` first; the `_cleanup` backstop covers a crash) and the
terminal writers stamp `completed_at` with their own clock. So once a UTC day
plus `OUTCOMES_SEAL_GRACE_S` has passed, nothing more can land in it -- which is
also why cost is placed by the TASK's end and not by when an attempt ran. The
write that seals a day is always a FULL re-derive, never an increment, so a live
cut that missed a slow commit is corrected at the seal.

TENANT ISOLATION (invariant 9). Tenant scope reads only documents whose id is
built from `tenant_scope`'s resolved id, and every query below leads with a
`tenant_id` equality. Platform scope exists only behind `require_admin`, which
the route runs before any of this is reached.

NOTHING IS BUILT AT IMPORT. No Firestore client, no cache: the 60 s cache lives
on the `Outcomes` instance the composition root builds.

WHY A TASK ENDED IS READ FROM THE TASK, THEN FROM ITS TEXT. Contract requests
23 and 24 were accepted by the owner on 2026-09-25 (#185, decision 9):
`Task.end_cause` is written by every terminal writer, and `RunnerProfile.
cost_declared` says which runners declare their cost. A task that carries an
end cause is classified by it and nothing else (`classify_failure`,
`cancel_cause`); the `last_error` prefix classifier below is the fallback for
tasks that ended before the field existed, or for an end no cause names.

A CANCEL CASCADE IS SPLIT BY ITS PARENTS (decision 2). "an upstream workflow
step did not succeed" is written after a FAILED parent and after a CANCELLED
one. A task that carries `end_cause` says which; one that does not is split at
derive time by reading its `depends_on` parents -- the documents the wait
figure already reads -- into "after a failure" and "after a cancel".
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import re
import threading
import time as _time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.models import EndCause, utcnow
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import TERMINAL_STATES, TaskState

from .codec import as_datetime
from .errors import NotFound, UpstreamUnavailable, ValidationFailed
from .rollup import UNKNOWN, effective_state
from .store import ATTEMPTS, TASKS

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Constants. Each carries its reason, because each is a decision.
# --------------------------------------------------------------------------

#: The rollup collection. swarm-api owns it and is its only writer.
COLLECTION = "outcome_days"

#: A UTC day is sealable this long after it ends. Fifteen minutes covers the
#: worker's spend write, its terminal write and Firestore's commit skew with a
#: wide margin; after it nothing can land in the day (see the module docstring).
#: A bucket is `open`, not `sealed`, for the same fifteen minutes after its end.
OUTCOMES_SEAL_GRACE_S = 900

#: A live doc's cut trails its build by this much, so a task committed a little
#: after the build with a `completed_at` a little before it is still caught by
#: the next delta rather than missed until the seal.
OUTCOMES_LIVE_SKEW_S = 120

#: A live doc is rewritten when it is older than this: at most one write per
#: tenant per minute however many people are looking.
OUTCOMES_LIVE_REWRITE_S = 60

#: What one request may spend building missing or stale tenant-days. Past it the
#: remaining days come back `unread` with reason `derive_budget`, and a
#: re-request continues where this one stopped, because what this one built was
#: written. A cold 14-day platform view on dev is ~4k reads, inside the budget;
#: a cold 90-day view takes the admin backfill route or several views.
OUTCOMES_DERIVE_READ_BUDGET = 5_000
OUTCOMES_DERIVE_SECONDS = 20.0

#: A payload is served from memory for this long. The cache key carries the
#: minute, so an entry never outlives the minute it was built in.
OUTCOMES_CACHE_S = 60
_CACHE_MAX_ENTRIES = 256

#: Bumped with ANY change to the tuple shape or to the classifier. A stored day
#: on another version of EITHER is re-derived on the next read that needs it
#: (`_decode_stored` checks both; until 2026-09-25 it checked only
#: DERIVE_VERSION, so a classifier bump alone would have been stored and never
#: acted on).
#:
#: 2 (2026-09-25, #185 decisions 2, 4 and 9): the cancel cascade is split into
#: after_failure and after_cancel by the parents' states, InputUnavailable is
#: its own class, and `Task.end_cause` is read before any text. Every sealed
#: day was classified by version 1's rules, so every one is re-derived.
DERIVE_VERSION = 2
CLASSIFIER_VERSION = 2

#: Firestore caps a document at 1 MiB. A day whose tuples pass this many bytes
#: is split into shard documents `{id}_s{n}`. The whole write is one batch, and
#: Firestore caps a request at 10 MiB, so at most 12 parts (the base and 11
#: shards) are written; a day that needs more is unread with `too_large`.
SHARD_BYTES = 700_000
MAX_SHARDS = 11

#: Point reads per `get_all` call.
_GET_ALL_CHUNK = 100
#: Firestore's cap on the values of one `in` filter.
_IN_CHUNK = 30

#: Tenants an admin's platform view lists. Past it `scope.tenants_complete` is
#: false rather than the view silently covering the first 200.
TENANT_LIST_LIMIT = 200

#: Response limits (contract).
MAX_BUCKETS = 2_000
MAX_SPAN_DAYS = 400
MONTH_MIN_DAYS = 60
GROUP_ROWS_MAX = 100
SERIES_MAX_BUCKETS = 120
WORKFLOW_ROWS_MAX = 20
FAILING_STEPS_MAX = 10
#: Below this many values a statistic lists them all, because a p95 of three
#: numbers is a figure that means nothing.
SMALL_N = 5

#: Two-sided 95 % normal quantile for the Wilson interval.
WILSON_Z = 1.959964

#: The worker's CANNOT-START exit (`agent_worker.errors.ExitCode.CONFIG`, and
#: `reconciler.detect.WORKER_EXIT_CANNOT_START`). The swarm-api image carries
#: neither package, so it is restated here and held to both by
#: tests/unit/control_plane/test_outcomes_classifier_parity.py. Contract request
#: 21 names this module as the third reader it anticipated.
EXIT_CANNOT_START = 78

#: What each worker exit status means, for `retries.not_final.by_exit`. The
#: numbers are `agent_worker.errors.ExitCode`'s and `startup.EXIT_INTERRUPTED`
#: (143); held to them by the same parity test. An exit not named here is shown
#: as its number, and a missing one as "no exit recorded", never as 0.
EXIT_LABELS: dict[int, str] = {
    0: "exited 0",
    1: "failed",
    69: "dependency unavailable",
    70: "generation fenced",
    71: "cancelled",
    75: "parked",
    76: "timeout",
    78: "could not start",
    79: "tenant mismatch",
    143: "interrupted",
}

#: Runner profiles whose cost is DECLARED by the runner rather than measured
#: from a provider. Included in every sum and marked, so a reader can tell a
#: runner's deliberate $0.00 from a real bill. READ FROM THE CATALOGUE
#: (`RunnerProfile.cost_declared`, contract request 24, accepted 2026-09-25),
#: never named here: a profile that starts declaring its cost is marked the
#: moment the catalogue says so.
DECLARED_COST_PROFILES: frozenset[str] = frozenset(
    name for name, profile in RUNNER_PROFILES.items() if profile.cost_declared
)

# --------------------------------------------------------------------------
# Vocabulary, served in every response so no client restates the order
# --------------------------------------------------------------------------

#: FIXED order. A filter never re-ranks it. `inputs_unavailable` (decision 4,
#: 2026-09-25) sits before `outputs_missing` because that is the order an
#: attempt meets them in: its inputs are staged before the agent starts, and
#: its outputs are checked after the agent ends.
FAILURE_CLASSES: tuple[tuple[str, str], ...] = (
    ("runner_error", "runner error"),
    ("timeout", "timeout"),
    ("lost_worker", "lost worker"),
    ("could_not_start", "could not start"),
    ("inputs_unavailable", "inputs unavailable"),
    ("outputs_missing", "outputs missing"),
    ("dispatch_failed", "dispatch failed"),
    ("other", "other"),
    ("no_reason", "no reason recorded"),
)
#: `after_cancel` (decision 2) follows `after_failure`: both are the cascade
#: a parent's end sent down its dependants, and they differ only in which end.
CANCEL_CAUSES: tuple[tuple[str, str], ...] = (
    ("requested", "requested"),
    ("after_failure", "after a failure"),
    ("after_cancel", "after a cancel"),
    ("workflow_sweep", "workflow sweep"),
    ("other", "other"),
)
FAILURE_KEYS: tuple[str, ...] = tuple(key for key, _ in FAILURE_CLASSES)
CANCEL_KEYS: tuple[str, ...] = tuple(key for key, _ in CANCEL_CAUSES)

VOCAB: dict[str, Any] = {
    "failure_classes": [{"key": k, "label": v} for k, v in FAILURE_CLASSES],
    "cancel_causes": [{"key": k, "label": v} for k, v in CANCEL_CAUSES],
    "classifier_version": CLASSIFIER_VERSION,
}

#: Where each terminal state lands in the fold.
_OUTCOME: dict[str, str] = {
    TaskState.SUCCEEDED.value: "succeeded",
    TaskState.FAILED.value: "failed",
    TaskState.DEAD_LETTERED.value: "dead_lettered",
    TaskState.CANCELLED.value: "cancelled",
}
_TERMINAL_VALUES = frozenset(s.value for s in TERMINAL_STATES)
_FAILED_VALUES = frozenset({TaskState.FAILED.value, TaskState.DEAD_LETTERED.value})


def _check_coverage() -> None:
    """Fail at import if the frozen contract grew a terminal state the fold cannot place.

    A new terminal state missing from `_OUTCOME` would be dropped from every
    count -- work ended and nothing says so. Raised, not asserted, so `python -O`
    cannot strip it (the same reasoning as rollup._check_coverage).
    """
    missing = _TERMINAL_VALUES - set(_OUTCOME)
    if missing:
        raise RuntimeError(
            "swarm_api.outcomes cannot place terminal states "
            f"{sorted(missing)}; add them to _OUTCOME before shipping"
        )


_check_coverage()

# --------------------------------------------------------------------------
# The classifier. A task that carries `end_cause` is classified by it alone
# (`_FAILURE_OF_CAUSE`, `_CANCEL_OF_CAUSE`). The text rules below are the
# FALLBACK, for tasks that ended before the field existed. Every pattern is
# pinned against its writer's source literal by
# tests/unit/control_plane/test_outcomes_classifier_parity.py, so a reworded
# message turns CI red instead of drifting into "other".
# --------------------------------------------------------------------------

#: What each typed end cause is, as a failure class. Keyed by the frozen enum,
#: so a value added there and not here is caught by
#: `test_every_end_cause_has_exactly_one_class`, not read as "other".
_FAILURE_OF_CAUSE: dict[str, str] = {
    EndCause.TIMEOUT.value: "timeout",
    EndCause.CANNOT_START.value: "could_not_start",
    EndCause.LOST_WORKER.value: "lost_worker",
    EndCause.OUTPUTS_MISSING.value: "outputs_missing",
    EndCause.INPUTS_UNAVAILABLE.value: "inputs_unavailable",
    EndCause.DISPATCH_FAILED.value: "dispatch_failed",
    EndCause.RUNNER_ERROR.value: "runner_error",
}
#: ... and as a cancel cause. The two maps partition `EndCause`.
_CANCEL_OF_CAUSE: dict[str, str] = {
    EndCause.CANCEL_REQUESTED.value: "requested",
    EndCause.FAILED_PARENT.value: "after_failure",
    EndCause.CANCELLED_PARENT.value: "after_cancel",
    EndCause.WORKFLOW_SWEEP.value: "workflow_sweep",
}


def _cause_value(end_cause: Any) -> str | None:
    """The stored end cause as its string value, or None when there is none."""
    if end_cause is None:
        return None
    value = end_cause.value if isinstance(end_cause, EndCause) else str(end_cause).strip()
    return value or None


#: agent_worker/lifecycle.py, the timeout branch of the terminal write.
_TIMEOUT_RE = re.compile(r"runner exceeded its [0-9.]+s timeout and was killed")
#: reconciler/detect.py `cannot_start_error`, all three sources.
_CANNOT_START_RE = re.compile(
    rf"(?:worker could not start: |worker exited {EXIT_CANNOT_START}: could not start)"
)
#: reconciler/repair.py: the text it requeues with, which becomes the final text
#: when reconciler/store.py downgrades READY to FAILED on exhausted retries.
_LOST_WORKER_RE = re.compile(r"reconciled: ")
#: agent_worker/expected_outputs.py, via lifecycle._fail_for_missing_outputs.
_OUTPUTS_MISSING_RE = re.compile(r"expected outputs missing")
#: agent_worker/inputs.py: every `InputUnavailable` the worker raises while it
#: stages a declared `input_from` artifact, before the agent starts. The worker
#: ends the task with `str(exc)` as the whole `last_error`, so each message
#: opens with its own literal. On dev, 2026-09-25, 10 of 13 "runner errors"
#: were two of these: "upstream task X did not produce an artifact named Y"
#: (6) and "upstream tasks X, Y all stage Z" (4). The parity test renders the
#: opening literal of EVERY raise in inputs.py and holds each to this pattern.
_INPUTS_UNAVAILABLE_RE = re.compile(
    r"(?:upstream tasks? \S"
    r"|metadata\.input_from\b"
    r"|input artifact "
    r"|the recorded location of "
    r"|the \S+ declared inputs total "
    r"|could not stage .+ from upstream task "
    r"|staging .+ from upstream task )",
    re.DOTALL,
)
#: scheduler/store.py `f"{error_code} (attempt {reference})"`; the codes are
#: dispatch.py's DispatchError codes and loop.py's scheduler_internal_error.
_DISPATCH_FAILED_RE = re.compile(r"[a-z][a-z0-9_]* \(attempt [^)]+\)")

#: agent_worker/control.py, scheduler/store.py and reconciler/repair.py prefix
#: every cancel they end on a request with this.
_REQUESTED_PREFIX = "cancelled on request; "
#: scheduler/loop.py, a step whose parent did not succeed. It fires for a
#: FAILED, DEAD_LETTERED **or CANCELLED** parent (loop.py
#: `_FAILED_PARENT_STATES`), so the text alone cannot say which: `cancel_cause`
#: splits it by the parents' states (decision 2).
_AFTER_FAILURE_TEXT = "an upstream workflow step did not succeed"
#: A parent in one of these states made its dependant's cancel "after a
#: failure"; CANCELLED made it "after a cancel". A failure wins when a step had
#: both, as the scheduler's own end cause does (loop.py `_parent_cause`).
_PARENT_FAILED_STATES = frozenset({TaskState.FAILED.value, TaskState.DEAD_LETTERED.value})
_PARENT_CANCELLED_STATE = TaskState.CANCELLED.value
#: scheduler/loop.py, the fail_workflow sweep.
_SWEEP_RE = re.compile(
    r"workflow step .+ is (?:FAILED|DEAD_LETTERED) and on_step_failure is "
    r"fail_workflow, so steps that had not started were cancelled",
    re.DOTALL,
)


def _state_value(state: Any) -> str:
    return state.value if isinstance(state, TaskState) else str(state)


def classify_failure(
    state: Any,
    last_error: str | None,
    final_attempt_exit_code: int | None,
    *,
    end_cause: Any = None,
) -> str | None:
    """Why a FAILED or DEAD_LETTERED task failed, as one fixed class. First match wins.

    Stage 0 is the task's own `end_cause` (contract request 23). A task that
    carries one is classified by it and by nothing else: the writer that ended
    the task said why, and no text is read over it. A cause that names no
    failure (a cancel's cause on a failed task, or a value this image does not
    know) is `other` -- counted, never dropped, and never re-guessed from text.

    Everything below is the FALLBACK, for a task with no end cause.

    Stage 1 is the exit code, and only where its meaning on a failed task is
    unambiguous: 78 is the worker's CANNOT-START. DELIBERATELY NOT 76 -> timeout:
    on a timeout the worker records the runner CHILD's exit status
    (lifecycle.py passes `result.exit_code`, a signal status after procman
    kills the child) and nothing ever raises ChildTimeout, so no attempt carries
    76 and that rule would count zero timeouts.

    Stage 2 matches `last_error` (leading whitespace stripped) against each
    writer's own text. Stage 3 asks who wrote the end: a final attempt with an
    exit code means the worker wrote it (the runner's error, a stderr tail,
    "runner exited N"); anything else set and unmatched is `other`, which is
    always a counted row, never dropped.

    DEAD_LETTERED is classified exactly like FAILED. No writer produces it today.
    Any other state has no failure class: None.
    """
    if _state_value(state) not in _FAILED_VALUES:
        return None
    cause = _cause_value(end_cause)
    if cause is not None:
        return _FAILURE_OF_CAUSE.get(cause, "other")
    if final_attempt_exit_code == EXIT_CANNOT_START:
        return "could_not_start"
    text = str(last_error).lstrip() if last_error is not None else ""
    if not text:
        return "no_reason"
    if _TIMEOUT_RE.match(text):
        return "timeout"
    if _CANNOT_START_RE.match(text):
        return "could_not_start"
    if _LOST_WORKER_RE.match(text):
        return "lost_worker"
    if _OUTPUTS_MISSING_RE.match(text):
        return "outputs_missing"
    if _INPUTS_UNAVAILABLE_RE.match(text):
        return "inputs_unavailable"
    if _DISPATCH_FAILED_RE.fullmatch(text):
        return "dispatch_failed"
    if final_attempt_exit_code is not None:
        return "runner_error"
    return "other"


def cancel_cause(
    cancel_requested: bool,
    last_error: str | None,
    *,
    end_cause: Any = None,
    parent_states: Sequence[Any] | None = None,
) -> str:
    """Why a CANCELLED task was cancelled.

    Stage 0 is the task's own `end_cause`, exactly as in `classify_failure`:
    the scheduler writes FAILED_PARENT or CANCELLED_PARENT from the parents it
    read when it cancelled, so a task that carries one is never split again.

    The FALLBACK, for a task with no end cause: `requested` covers both the
    flag (the API cancel and the workflow cancel set it on every step) and the
    prefix the terminal writers put on a cancel they ended on a request.

    "an upstream workflow step did not succeed" is split by `parent_states`,
    the states of the task's `depends_on` parents as the derive read them
    (None for one it could not read): a FAILED or DEAD_LETTERED parent makes it
    `after_failure`, else a CANCELLED one makes it `after_cancel`. With no
    parent read in either state the text cannot say which, and it is `other`
    -- never guessed to be a failure, which is the overclaim decision 2 ends.

    Everything unrecognised is `other` -- including the worker's "runner
    stopped on SIGTERM" without the flag, and a null last_error without the
    flag.
    """
    cause = _cause_value(end_cause)
    if cause is not None:
        return _CANCEL_OF_CAUSE.get(cause, "other")
    text = str(last_error).lstrip() if last_error is not None else ""
    if cancel_requested or text.startswith(_REQUESTED_PREFIX):
        return "requested"
    if text == _AFTER_FAILURE_TEXT:
        states = {_state_value(s) for s in (parent_states or ()) if s is not None}
        if states & _PARENT_FAILED_STATES:
            return "after_failure"
        if _PARENT_CANCELLED_STATE in states:
            return "after_cancel"
        return "other"
    if _SWEEP_RE.fullmatch(text):
        return "workflow_sweep"
    return "other"


def exit_label(code: int | None) -> str:
    if code is None:
        return "no exit recorded"
    return EXIT_LABELS.get(code, f"exit {code}")


# --------------------------------------------------------------------------
# Small arithmetic: the rate interval and nearest-rank percentiles
# --------------------------------------------------------------------------

def wilson(k: int, n: int) -> dict[str, Any] | None:
    """k of n with its 95 % Wilson interval, clamped to [0, 1], all 4 dp.

    None when n is 0: a bucket where nothing was decided has no rate, and the
    only honest drawing of that is a gap. Check value: 272 of 300 -> 0.9067,
    0.8684-0.9346.
    """
    if n <= 0:
        return None
    z2 = WILSON_Z * WILSON_Z
    p = k / n
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    half = WILSON_Z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    return {
        "k": k,
        "n": n,
        "p": round(p, 4),
        "lo": round(max(0.0, centre - half), 4),
        "hi": round(min(1.0, centre + half), 4),
    }


def percentile(sorted_values: Sequence[float], pct: int) -> float:
    """Exact nearest rank: value[ceil(pct/100 * n) - 1]. No sketches.

    Integer arithmetic for the rank, so 95 % of 20 is rank 19 and never 20
    because of a float that came out as 19.000000000000004.
    """
    n = len(sorted_values)
    if n == 0:
        raise ValueError("percentile of no values")
    rank = -(-pct * n // 100)
    return sorted_values[max(0, min(n, rank) - 1)]


def _stat(values: list[float]) -> dict[str, Any] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50_s": round(percentile(ordered, 50), 1),
        "p95_s": round(percentile(ordered, 95), 1),
        "max_s": round(ordered[-1], 1),
        "values_s": [round(v, 1) for v in ordered] if len(ordered) < SMALL_N else None,
    }


def _money(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _cost(sum_usd: float, attempts: int, reporting: int) -> dict[str, Any]:
    """The /v1/attempts rule: a sum is only a sum over what reported.

    `sum_usd` is null when nothing reported -- "not measured", never $0.00 --
    and `reporting < attempts` is what a reader draws as a partial figure.
    """
    return {
        "sum_usd": _money(sum_usd) if reporting else None,
        "attempts": attempts,
        "reporting": reporting,
    }


# --------------------------------------------------------------------------
# Time: epoch milliseconds for tuples, wall-clock boundaries for buckets
# --------------------------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ONE_MS = timedelta(milliseconds=1)
_ONE_DAY = timedelta(days=1)


def _ms(value: Any) -> int | None:
    moment = as_datetime(value)
    if moment is None:
        return None
    return (moment - _EPOCH) // _ONE_MS


def _from_ms(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_iso_ms(ms: int) -> str:
    return _utc_iso(_from_ms(ms))


def _local_iso(moment: datetime, tz: ZoneInfo) -> str:
    """A boundary in the viewer's zone, always with its own offset."""
    return moment.astimezone(tz).isoformat(timespec="seconds")


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time(), timezone.utc)


def _day_end(day: date) -> datetime:
    return _day_start(day) + _ONE_DAY


def _seal_at(day: date) -> datetime:
    return _day_end(day) + timedelta(seconds=OUTCOMES_SEAL_GRACE_S)


def _utc_days(since: datetime, until: datetime) -> list[date]:
    """Every UTC day that overlaps [since, until)."""
    first = since.astimezone(timezone.utc).date()
    last = (until - timedelta(microseconds=1)).astimezone(timezone.utc).date()
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _wall(moment: datetime, tz: ZoneInfo) -> datetime:
    return moment.astimezone(tz).replace(tzinfo=None)


def _resolve_wall(wall: datetime, tz: ZoneInfo) -> datetime:
    """The UTC instant at which `tz`'s wall clock reads `wall` (naive).

    AMBIGUOUS (a fall-back hour): the FIRST occurrence, fold=0.
    NONEXISTENT (a spring-forward gap): the first instant after the gap --
    Santiago skips 00:00-01:00 on 6 Sep 2026, so that day begins at 01:00-03:00.

    The gap is found by search rather than by trusting fold=0, because fold=0
    maps a missing time with the PRE-transition offset, which lands on the
    transition only when the missing time is the first one skipped.
    """
    first = wall.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)
    if _wall(first, tz) == wall:
        return first
    other = wall.replace(tzinfo=tz, fold=1).astimezone(timezone.utc)
    lo_s = int(min(first, other).timestamp())
    hi_s = int(math.ceil(max(first, other).timestamp()))
    if _wall(datetime.fromtimestamp(hi_s, timezone.utc), tz) < wall:
        return first
    while hi_s - lo_s > 1:
        mid = (lo_s + hi_s) // 2
        if _wall(datetime.fromtimestamp(mid, timezone.utc), tz) >= wall:
            hi_s = mid
        else:
            lo_s = mid
    return datetime.fromtimestamp(hi_s, timezone.utc)


def _local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return _resolve_wall(datetime.combine(day, time()), tz)


def floor_boundary(instant: datetime, unit: str, tz: ZoneInfo) -> datetime:
    """The bucket boundary at or before `instant`, as a UTC instant.

    hour: the wall clock reads HH:00:00 (across a fall-back hour there are two,
    with the same label and different offsets); day: local 00:00; week: ISO
    Monday 00:00, the same as types.ts `bucketStart`; month: day 1 00:00.
    """
    local = instant.astimezone(tz)
    if unit == "hour":
        return local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    day = local.date()
    if unit == "week":
        day -= timedelta(days=day.weekday())
    elif unit == "month":
        day = day.replace(day=1)
    return _local_midnight(day, tz)


def next_boundary(boundary: datetime, unit: str, tz: ZoneInfo) -> datetime:
    """The boundary after `boundary`. A day bucket can be 23 h or 25 h long."""
    if unit == "hour":
        step = 1
        candidate = floor_boundary(boundary + timedelta(hours=step), "hour", tz)
        while candidate <= boundary:
            step += 1
            candidate = floor_boundary(boundary + timedelta(hours=step), "hour", tz)
        return candidate
    day = boundary.astimezone(tz).date()
    if unit == "day":
        day += _ONE_DAY
    elif unit == "week":
        day += timedelta(days=7)
    else:
        day = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return _local_midnight(day, tz)


def previous_boundary(boundary: datetime, unit: str, tz: ZoneInfo) -> datetime:
    return floor_boundary(boundary - timedelta(microseconds=1), unit, tz)


def ceil_boundary(instant: datetime, unit: str, tz: ZoneInfo) -> datetime:
    floor = floor_boundary(instant, unit, tz)
    return instant if floor == instant else next_boundary(floor, unit, tz)


def bucket_edges(since: datetime, until: datetime, unit: str, tz: ZoneInfo) -> list[datetime]:
    """Boundaries from `since` (a boundary) until the first one at or past `until`."""
    edges = [since]
    while edges[-1] < until:
        edges.append(next_boundary(edges[-1], unit, tz))
    return edges


# --------------------------------------------------------------------------
# Parameters. ONE error shape: every value arrives as a string and every
# refusal is errors.ValidationFailed, so the service envelope {code, message,
# detail} is the only 422 body this route can return.
# --------------------------------------------------------------------------

SPANS: dict[str, tuple[str, int]] = {
    "24h": ("hour", 24),
    "7d": ("day", 7),
    "14d": ("day", 14),
    "30d": ("day", 30),
    "90d": ("day", 90),
}
#: The owner's default span (issue #185).
DEFAULT_SPAN = "14d"
BUCKETS = ("hour", "day", "week", "month")
SCOPES = ("tenant", "platform")
KINDS = ("all", "standalone", "steps")
GROUPS = ("runner_profile", "submitted_by", "tenant_id")
COMPARES = ("none", "previous")

_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Params:
    tz_name: str
    tz: ZoneInfo
    requested: dict[str, Any]
    since: datetime
    until: datetime
    edges: tuple[datetime, ...]
    bucket: str
    bucket_chosen_by: str
    scope: str
    tenant: tuple[str, ...]
    exclude_tenant: tuple[str, ...]
    profile: tuple[str, ...]
    submitted_by: tuple[str, ...]
    kind: str
    group: str
    compare: str
    previous_edges: tuple[datetime, ...] | None

    def filters(self) -> dict[str, Any]:
        return {
            "profile": list(self.profile),
            "submitted_by": list(self.submitted_by),
            "kind": self.kind,
            "tenant": list(self.tenant),
            "exclude_tenant": list(self.exclude_tenant),
            "group": self.group,
        }

    def canonical(self) -> tuple[Any, ...]:
        """The request as the cache sees it, with the span UNRESOLVED."""
        return (
            self.tz_name,
            tuple(sorted(self.requested.items())),
            self.scope,
            self.tenant,
            self.exclude_tenant,
            self.profile,
            self.submitted_by,
            self.kind,
            self.group,
            self.compare,
        )


def _refuse(parameter: str, message: str, **detail: Any) -> ValidationFailed:
    return ValidationFailed(message, detail={"parameter": parameter, **detail})


def _one(raw: Mapping[str, Any], name: str) -> str | None:
    value = raw.get(name)
    if isinstance(value, (list, tuple)):
        value = value[-1] if value else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _many(raw: Mapping[str, Any], name: str) -> list[str]:
    value = raw.get(name)
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def platform_requested(raw: Mapping[str, Any]) -> bool:
    """Whether the caller asked for anything beyond their own tenant.

    Decided on the RAW values, before any validation, because the admin gate
    runs first: a non-admin must learn nothing about tenant ids, including
    whether the ones they named exist.
    """
    return (
        _one(raw, "scope") == "platform"
        or bool(_many(raw, "tenant"))
        or bool(_many(raw, "exclude_tenant"))
        or _one(raw, "group") == "tenant_id"
    )


@lru_cache(maxsize=1)
def _tz_database_present() -> bool:
    try:
        return bool(available_timezones())
    except Exception:  # pragma: no cover - depends on the image
        return False


def _zone(raw_tz: str | None) -> ZoneInfo:
    if not raw_tz:
        raise _refuse(
            "tz",
            "tz is required: an IANA time zone such as Europe/Bucharest, which "
            "bucket boundaries are drawn in",
            tz=None,
            reason="unknown_zone",
        )
    try:
        return ZoneInfo(raw_tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        if not _tz_database_present():
            # Every zone failing is a deployment without a time zone database,
            # not a caller who misspelt one. Said as such, so nobody debugs
            # their query string.
            raise UpstreamUnavailable(
                "this deployment has no time zone database, so no tz can be "
                "resolved; the image needs tzdata"
            ) from None
        raise _refuse(
            "tz",
            f"{raw_tz!r} is not an IANA time zone",
            tz=raw_tz,
            reason="unknown_zone",
        ) from None


def _parse_moment(name: str, text: str, tz: ZoneInfo) -> datetime:
    """An ISO 8601 datetime WITH an offset, or YYYY-MM-DD read as local 00:00.

    An unencoded '+' in a query string arrives as a space, so a trailing
    ' HH:MM' is read back as '+HH:MM' rather than refused as malformed.
    """
    if _DATE_ONLY.fullmatch(text):
        try:
            return _local_midnight(date.fromisoformat(text), tz)
        except ValueError:
            raise _refuse(name, f"{name} is not a real date", value=text, reason="not_a_date") from None
    candidate = re.sub(r" (\d{2}:?\d{2})$", r"+\1", text)
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise _refuse(
            name,
            f"{name} must be an ISO 8601 datetime with an offset, or YYYY-MM-DD",
            value=text,
            reason="not_iso8601",
        ) from None
    if parsed.tzinfo is None:
        raise _refuse(
            name,
            f"{name} needs an offset (2026-09-12T00:00:00+03:00) or a bare date",
            value=text,
            reason="needs_offset",
        )
    return parsed.astimezone(timezone.utc)


def parse_params(raw: Mapping[str, Any], *, now: datetime) -> Params:
    """Validate and resolve every parameter. Pure: no store, no clock but `now`.

    THE SERVER OWNS ALIGNMENT and echoes the result, so the UI never re-derives
    a boundary: a span is resolved to local midnights (or hours), an explicit
    `since` is floored to the boundary containing it, `until` is ceiled to the
    next boundary and clamped to now, and the raw values go back in `requested`.
    """
    now = now.astimezone(timezone.utc)
    tz_raw = _one(raw, "tz")
    tz = _zone(tz_raw)
    assert tz_raw is not None  # _zone refused a missing one

    span = _one(raw, "span")
    since_raw = _one(raw, "since")
    until_raw = _one(raw, "until")
    bucket_raw = _one(raw, "bucket") or "auto"

    if span is not None and (since_raw is not None or until_raw is not None):
        raise _refuse(
            "span", "span and since/until are mutually exclusive", value=span, reason="exclusive"
        )
    if until_raw is not None and since_raw is None:
        raise _refuse(
            "until", "until is only valid together with since", value=until_raw, reason="needs_since"
        )
    if span is None and since_raw is None:
        span = DEFAULT_SPAN
    if span is not None and span not in SPANS:
        raise _refuse("span", f"unknown span {span!r}", value=span, allowed=list(SPANS))
    if bucket_raw != "auto" and bucket_raw not in BUCKETS:
        raise _refuse(
            "bucket", f"unknown bucket {bucket_raw!r}", value=bucket_raw, allowed=["auto", *BUCKETS]
        )

    if span is not None:
        unit, count = SPANS[span]
        if unit == "hour":
            since_dt = floor_boundary(now, "hour", tz) - timedelta(hours=count - 1)
        else:
            today = now.astimezone(tz).date()
            since_dt = _local_midnight(today - timedelta(days=count - 1), tz)
        until_dt = now
    else:
        assert since_raw is not None
        since_dt = _parse_moment("since", since_raw, tz)
        until_dt = _parse_moment("until", until_raw, tz) if until_raw is not None else now
        if since_dt > now:
            raise _refuse(
                "since", "since is in the future", value=since_raw, now=_utc_iso(now),
                reason="in_future",
            )

    raw_span = until_dt - since_dt
    if raw_span <= timedelta(0):
        raise _refuse(
            "since", "since must be earlier than until", value=since_raw, reason="empty_range"
        )
    if raw_span > timedelta(days=MAX_SPAN_DAYS):
        raise _refuse(
            "since",
            f"the span is at most {MAX_SPAN_DAYS} days",
            value=round(raw_span / _ONE_DAY, 2),
            max=MAX_SPAN_DAYS,
        )

    if bucket_raw == "auto":
        if raw_span <= timedelta(hours=48):
            bucket = "hour"
        elif raw_span <= timedelta(days=60):
            bucket = "day"
        else:
            bucket = "week"
        chosen_by = "server"
    else:
        bucket = bucket_raw
        chosen_by = "caller"
    if bucket == "month" and raw_span < timedelta(days=MONTH_MIN_DAYS):
        raise _refuse(
            "bucket",
            f"bucket=month needs a span of at least {MONTH_MIN_DAYS} days",
            value=bucket,
            reason="month_needs_60_days",
        )

    since = floor_boundary(since_dt, bucket, tz)
    until = min(ceil_boundary(until_dt, bucket, tz), now)
    if since >= until:
        raise _refuse(
            "since",
            "since must be earlier than until once aligned to the bucket",
            value=since_raw,
            reason="empty_range",
        )
    edges = bucket_edges(since, until, bucket, tz)
    if len(edges) - 1 > MAX_BUCKETS:
        raise _refuse(
            "bucket",
            f"at most {MAX_BUCKETS} buckets; choose a larger bucket or a shorter span",
            value=len(edges) - 1,
            max=MAX_BUCKETS,
        )

    tenant = _many(raw, "tenant")
    exclude = _many(raw, "exclude_tenant")
    scope_raw = _one(raw, "scope")
    group = _one(raw, "group") or "runner_profile"
    kind = _one(raw, "kind") or "all"
    compare = _one(raw, "compare") or "none"
    if scope_raw is not None and scope_raw not in SCOPES:
        raise _refuse("scope", f"unknown scope {scope_raw!r}", value=scope_raw, allowed=list(SCOPES))
    if group not in GROUPS:
        raise _refuse("group", f"unknown group {group!r}", value=group, allowed=list(GROUPS))
    if kind not in KINDS:
        raise _refuse("kind", f"unknown kind {kind!r}", value=kind, allowed=list(KINDS))
    if compare not in COMPARES:
        raise _refuse("compare", f"unknown compare {compare!r}", value=compare, allowed=list(COMPARES))
    if tenant and exclude:
        raise _refuse(
            "tenant", "tenant and exclude_tenant are mutually exclusive", reason="exclusive"
        )
    scope = scope_raw or ("platform" if (tenant or exclude) else "tenant")
    if scope == "tenant":
        if scope_raw == "tenant" and (tenant or exclude or group == "tenant_id"):
            raise _refuse(
                "scope",
                "scope=tenant reads your own tenant only; tenant, exclude_tenant and "
                "group=tenant_id are platform-scope parameters",
                reason="conflicting_scope",
            )
        if group == "tenant_id":
            raise _refuse(
                "group", "group=tenant_id is platform scope only", value=group,
                reason="platform_only",
            )

    profiles = sorted(set(_many(raw, "profile")))
    for name in profiles:
        if name not in RUNNER_PROFILES:
            raise _refuse(
                "profile",
                f"unknown runner_profile {name!r}",
                value=name,
                known_runner_profiles=sorted(RUNNER_PROFILES),
            )
    people = sorted({p.lower() for p in _many(raw, "submitted_by")})

    previous: tuple[datetime, ...] | None = None
    if compare == "previous":
        back = [since]
        for _ in range(len(edges) - 1):
            back.insert(0, previous_boundary(back[0], bucket, tz))
        previous = tuple(back)

    return Params(
        tz_name=tz_raw,
        tz=tz,
        requested={
            "span": span,
            "since": since_raw,
            "until": until_raw,
            "bucket": bucket_raw,
        },
        since=since,
        until=until,
        edges=tuple(edges),
        bucket=bucket,
        bucket_chosen_by=chosen_by,
        scope=scope,
        tenant=tuple(sorted(tenant)),
        exclude_tenant=tuple(sorted(exclude)),
        profile=tuple(profiles),
        submitted_by=tuple(people),
        kind=kind,
        group=group,
        compare=compare,
        previous_edges=previous,
    )


# --------------------------------------------------------------------------
# Tuples: one per ENDED task, one per ARRIVAL. Times are UTC epoch ms.
# --------------------------------------------------------------------------

ENDED_COLS: tuple[str, ...] = (
    "id",
    "state",
    "created",
    "completed",
    "eligible",
    "first_start",
    "profile",
    "submitted_by",
    "workflow_id",
    "step_id",
    "attempt_count",
    # How many attempt documents were found. Not in the contract's list; it is
    # what `retries.admissions_without_attempt_doc` is computed from, and
    # `nonfinal_exits` cannot tell "no attempt doc" from "exactly one".
    "att_docs",
    "timeout_s",
    "failure_class",
    "cancel_cause",
    "cost_usd",
    "att_started",
    "att_reporting",
    "retry_cost_usd",
    "retry_att_started",
    "retry_att_reporting",
    "nonfinal_exits",
)
ARRIVED_COLS: tuple[str, ...] = ("id", "created", "profile", "submitted_by", "step")


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _attempt_order(attempt: Mapping[str, Any]) -> tuple[int, int]:
    return (_int(attempt.get("generation")), _ms(attempt.get("created_at")) or 0)


def _spend(attempts: Iterable[Mapping[str, Any]]) -> tuple[float | None, int, int]:
    """(cost, started, reporting) over the attempts that STARTED.

    An attempt that never started could not spend. Of those that did, one with
    `cost_usd` set reported -- $0.00 is a measurement -- and tokens without a
    cost do not make a cost measured (the /v1/attempts rule).
    """
    started = [a for a in attempts if a.get("started_at") is not None]
    reported = [float(a["cost_usd"]) for a in started if a.get("cost_usd") is not None]
    cost = _money(sum(reported)) if reported else None
    return cost, len(started), len(reported)


def tuple_from_docs(
    task: Mapping[str, Any],
    attempts: Sequence[Mapping[str, Any]],
    parents: Mapping[str, Mapping[str, Any] | None],
) -> dict[str, Any]:
    """One ENDED task, reduced to what the fold needs. Pure.

    `parents` maps a depends_on task id to its document, or to None when it
    could not be read inside this tenant (missing, or another tenant's): that
    task's `eligible` is then None and its wait is excluded, never guessed.
    The same documents split a cascade cancel with no `end_cause` into "after a
    failure" and "after a cancel" (`cancel_cause`): no read is added for it.

    first_start is min(Attempt.started_at). Task.started_at is never used: every
    STARTING transition overwrites it (agent_worker/control.py).
    """
    created = _ms(task.get("created_at"))
    ordered = sorted(attempts, key=_attempt_order)
    final = ordered[-1] if ordered else None
    starts = [_ms(a.get("started_at")) for a in ordered if a.get("started_at") is not None]
    first_start = min(starts) if starts else None

    depends_on = list(task.get("depends_on") or [])
    eligible: int | None = created
    for parent_id in depends_on:
        parent = parents.get(parent_id)
        finished = _ms(parent.get("completed_at")) if parent else None
        if finished is None:
            eligible = None
            break
        eligible = max(eligible or 0, finished)
    parent_states = [
        (parents.get(parent_id) or {}).get("state") for parent_id in depends_on
    ]

    state = str(task.get("state"))
    last_error = task.get("last_error")
    end_cause = task.get("end_cause")
    final_exit = final.get("exit_code") if final is not None else None
    cost, started, reporting = _spend(ordered)
    lowest = _int(ordered[0].get("generation")) if ordered else 0
    retry_cost, retry_started, retry_reporting = _spend(
        a for a in ordered if _int(a.get("generation")) > lowest
    )
    submitted_by = task.get("submitted_by")
    timeout = task.get("timeout_seconds")
    return {
        "id": task.get("id"),
        "state": state,
        "created": created,
        "completed": _ms(task.get("completed_at")),
        "eligible": eligible,
        "first_start": first_start,
        "profile": task.get("runner_profile"),
        "submitted_by": str(submitted_by).strip().lower() if submitted_by else "",
        "workflow_id": task.get("workflow_id"),
        "step_id": task.get("step_id"),
        "attempt_count": _int(task.get("attempt_count")),
        "att_docs": len(ordered),
        "timeout_s": _int(timeout) if timeout is not None else None,
        "failure_class": classify_failure(state, last_error, final_exit, end_cause=end_cause),
        "cancel_cause": (
            cancel_cause(
                bool(task.get("cancel_requested")),
                last_error,
                end_cause=end_cause,
                parent_states=parent_states,
            )
            if state == TaskState.CANCELLED.value
            else None
        ),
        "cost_usd": cost,
        "att_started": started,
        "att_reporting": reporting,
        "retry_cost_usd": retry_cost,
        "retry_att_started": retry_started,
        "retry_att_reporting": retry_reporting,
        "nonfinal_exits": [a.get("exit_code") for a in ordered[:-1]],
    }


def arrival_from_doc(task: Mapping[str, Any]) -> dict[str, Any]:
    submitted_by = task.get("submitted_by")
    return {
        "id": task.get("id"),
        "created": _ms(task.get("created_at")),
        "profile": task.get("runner_profile"),
        "submitted_by": str(submitted_by).strip().lower() if submitted_by else "",
        "step": task.get("workflow_id") is not None,
    }


def _encode_rows(rows: Sequence[Mapping[str, Any]], cols: Sequence[str]) -> str:
    """Columnar JSON: the keys once, then one list per row. ~3x smaller."""
    return json.dumps(
        {"cols": list(cols), "rows": [[row.get(c) for c in cols] for row in rows]},
        separators=(",", ":"),
    )


def _decode_rows(text: Any, cols: Sequence[str]) -> list[dict[str, Any]]:
    obj = json.loads(text)
    if not isinstance(obj, dict) or obj.get("cols") != list(cols):
        raise ValueError("stored columns do not match this derive version")
    out = []
    for row in obj.get("rows") or []:
        if not isinstance(row, list) or len(row) != len(cols):
            raise ValueError("a stored row does not have one value per column")
        out.append(dict(zip(cols, row)))
    return out


def _split(rows: Sequence[Any], parts: int) -> list[list[Any]]:
    if not rows:
        return [[] for _ in range(parts)]
    size = math.ceil(len(rows) / parts)
    return [list(rows[i * size:(i + 1) * size]) for i in range(parts)]


def _pack(
    ended: Sequence[Mapping[str, Any]],
    arrived: Sequence[Mapping[str, Any]],
    *,
    shard_bytes: int,
    max_parts: int,
) -> list[tuple[str, str]] | None:
    """The day's payload split into parts that each fit a document, or None."""
    total = len(_encode_rows(ended, ENDED_COLS).encode()) + len(
        _encode_rows(arrived, ARRIVED_COLS).encode()
    )
    parts = max(1, math.ceil(total / max(1, shard_bytes)))
    while parts <= max_parts:
        packed = [
            (_encode_rows(e, ENDED_COLS), _encode_rows(a, ARRIVED_COLS))
            for e, a in zip(_split(ended, parts), _split(arrived, parts))
        ]
        if all(len(e.encode()) + len(a.encode()) <= shard_bytes for e, a in packed):
            return packed
        parts += 1
    return None


def day_doc_id(tenant_id: str, day: date) -> str:
    """`{tenant}_{YYYY-MM-DD}`. Tenant ids are [a-z0-9-] (identity._TENANT_SAFE),
    so the '_' cannot collide with any tenant's own id."""
    return f"{tenant_id}_{day.isoformat()}"


def shard_doc_id(base_id: str, part: int) -> str:
    return f"{base_id}_s{part}"


# --------------------------------------------------------------------------
# What one tenant-day contributes to a read
# --------------------------------------------------------------------------

@dataclass
class DayData:
    """A fresh derive of one tenant-day."""

    ended: list[dict[str, Any]]
    arrived: list[dict[str, Any]]
    reads: int


@dataclass
class StoredDay:
    """A decoded `outcome_days` document, base and shards together."""

    sealed: bool
    built_at: datetime | None
    built_through: datetime | None
    ended: list[dict[str, Any]]
    arrived: list[dict[str, Any]]
    reads: int = 0


@dataclass
class _Stored:
    exists: bool = False
    #: None when missing OR not usable (another derive or classifier version,
    #: or an encoding that did not decode) -- either way the day is derived again.
    doc: StoredDay | None = None
    #: Extra shards the stored base names, so a smaller rewrite deletes the rest.
    shards: int = 0
    failed: bool = False


@dataclass
class DayResult:
    """One tenant-day as the fold sees it."""

    status: str  # "sealed" | "live" | "unread"
    reason: str | None = None  # for unread: derive_budget | read_failed | too_large
    ended: list[dict[str, Any]] = field(default_factory=list)
    arrived: list[dict[str, Any]] = field(default_factory=list)
    #: A live day's stored cut (the delta after it was folded in on this read).
    built_through: datetime | None = None
    derived: bool = False


class _TooLarge(Exception):
    pass


class _Meter:
    """Billed Firestore reads spent on one payload."""

    __slots__ = ("reads",)

    def __init__(self) -> None:
        self.reads = 0

    def add(self, n: int) -> None:
        self.reads += max(0, int(n))


def _decode_stored(base: Mapping[str, Any], shards: Sequence[Mapping[str, Any] | None]) -> StoredDay | None:
    # BOTH versions, because each stored tuple carries its class as the
    # classifier of its day decided it. A day on another CLASSIFIER_VERSION
    # holds classes this code would not assign, and serving it would mix two
    # classifiers in one card. Until 2026-09-25 only DERIVE_VERSION was read
    # here, so the classifier's version was written and never acted on.
    if _int(base.get("derive_version"), -1) != DERIVE_VERSION:
        return None
    if _int(base.get("classifier_version"), -1) != CLASSIFIER_VERSION:
        return None
    try:
        built_at = as_datetime(base.get("built_at"))
        ended = _decode_rows(base.get("ended"), ENDED_COLS)
        arrived = _decode_rows(base.get("arrived"), ARRIVED_COLS)
        for shard in shards:
            # Every part of one write carries that write's built_at. A part from
            # another write means a torn read, which is re-derived, never merged.
            if shard is None or as_datetime(shard.get("built_at")) != built_at:
                return None
            ended.extend(_decode_rows(shard.get("ended"), ENDED_COLS))
            arrived.extend(_decode_rows(shard.get("arrived"), ARRIVED_COLS))
        return StoredDay(
            sealed=bool(base.get("sealed")),
            built_at=built_at,
            built_through=as_datetime(base.get("built_through")),
            ended=ended,
            arrived=arrived,
            reads=_int(base.get("reads")),
        )
    except (ValueError, TypeError, KeyError):
        return None


def _merge(old: Iterable[dict[str, Any]], new: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Dedupe by task id; the newer read wins."""
    by_id: dict[Any, dict[str, Any]] = {row["id"]: row for row in old}
    for row in new:
        by_id[row["id"]] = row
    return sorted(by_id.values(), key=lambda row: (row.get(key) or 0, str(row["id"])))


# --------------------------------------------------------------------------
# The fold. Pure: tuples in, the response's numbers out.
# --------------------------------------------------------------------------

_REASON_ORDER = ("read_failed", "too_large", "derive_budget")


def _worse(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if _REASON_ORDER.index(a) <= _REASON_ORDER.index(b) else b


class _Tally:
    __slots__ = (
        "submitted", "succeeded", "failed", "dead_lettered", "cancelled", "classes",
        "cost_sum", "cost_attempts", "cost_reporting",
    )

    def __init__(self) -> None:
        self.submitted = 0
        self.succeeded = 0
        self.failed = 0
        self.dead_lettered = 0
        self.cancelled = {key: 0 for key in CANCEL_KEYS}
        self.classes = {key: 0 for key in FAILURE_KEYS}
        self.cost_sum = 0.0
        self.cost_attempts = 0
        self.cost_reporting = 0

    def add_ended(self, t: Mapping[str, Any]) -> None:
        outcome = _OUTCOME[t["state"]]
        if outcome == "cancelled":
            cause = t.get("cancel_cause")
            self.cancelled[cause if cause in self.cancelled else "other"] += 1
        elif outcome == "succeeded":
            self.succeeded += 1
        else:
            if outcome == "failed":
                self.failed += 1
            else:
                self.dead_lettered += 1
            klass = t.get("failure_class")
            self.classes[klass if klass in self.classes else "other"] += 1
        self.cost_attempts += _int(t.get("att_started"))
        self.cost_reporting += _int(t.get("att_reporting"))
        if t.get("cost_usd") is not None:
            self.cost_sum += float(t["cost_usd"])

    def merge(self, other: "_Tally") -> None:
        self.submitted += other.submitted
        self.succeeded += other.succeeded
        self.failed += other.failed
        self.dead_lettered += other.dead_lettered
        for key in CANCEL_KEYS:
            self.cancelled[key] += other.cancelled[key]
        for key in FAILURE_KEYS:
            self.classes[key] += other.classes[key]
        self.cost_sum += other.cost_sum
        self.cost_attempts += other.cost_attempts
        self.cost_reporting += other.cost_reporting

    @property
    def decided(self) -> int:
        return self.succeeded + self.failed + self.dead_lettered

    @property
    def ended(self) -> int:
        return self.decided + sum(self.cancelled.values())

    def cancelled_api(self) -> dict[str, int]:
        return {"total": sum(self.cancelled.values()), **self.cancelled}

    def to_api(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted,
            "ended": self.ended,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "dead_lettered": self.dead_lettered,
            "cancelled": self.cancelled_api(),
            "rate": wilson(self.succeeded, self.decided),
            "failure_classes": dict(self.classes),
            "cost": _cost(self.cost_sum, self.cost_attempts, self.cost_reporting),
        }


#: When a bucket is unread EVERY number is null -- never a partial sum.
_UNREAD_FIELDS: dict[str, Any] = {
    "submitted": None,
    "ended": None,
    "succeeded": None,
    "failed": None,
    "dead_lettered": None,
    "cancelled": None,
    "rate": None,
    "failure_classes": None,
    "cost": None,
}


def _passes(t: Mapping[str, Any], params: Params, *, arrival: bool) -> bool:
    if params.profile and t.get("profile") not in params.profile:
        return False
    if params.submitted_by and t.get("submitted_by") not in params.submitted_by:
        return False
    if params.kind != "all":
        step = bool(t.get("step")) if arrival else t.get("workflow_id") is not None
        if (params.kind == "standalone") == step:
            return False
    return True


def _unread_days(
    days: Mapping[tuple[str, date], DayResult], tenants: Sequence[str], day_list: Sequence[date]
) -> dict[date, str]:
    out: dict[date, str] = {}
    for tenant in tenants:
        for day in day_list:
            result = days.get((tenant, day))
            if result is None:
                out[day] = _worse(out.get(day), "read_failed") or "read_failed"
            elif result.status == "unread":
                out[day] = _worse(out.get(day), result.reason or "read_failed") or "read_failed"
    return out


def _bucket_reasons(
    edges_ms: Sequence[int], until_ms: int, unread: Mapping[date, str]
) -> list[str | None]:
    """Each bucket's unread reason, or None. ANY overlapping unread tenant-day makes
    a bucket unread, in either scope."""
    reasons: list[str | None] = []
    for i in range(len(edges_ms) - 1):
        start = edges_ms[i]
        end = min(edges_ms[i + 1], until_ms)
        reason: str | None = None
        if unread:
            day = _from_ms(start).date()
            last = _from_ms(max(start, end - 1)).date()
            while day <= last:
                reason = _worse(reason, unread.get(day))
                day += _ONE_DAY
        reasons.append(reason)
    return reasons


def _gather(
    days: Mapping[tuple[str, date], DayResult],
    tenants: Sequence[str],
    day_list: Sequence[date],
    params: Params,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Filtered tuples, each tagged with its tenant, deduped by id.

    A task that ended, was reopened and ended again sits in more than one day
    doc. The latest ending wins; `reopened` counts the tasks that happened to.
    """
    ended: dict[Any, dict[str, Any]] = {}
    seen: Counter = Counter()
    arrived: dict[Any, dict[str, Any]] = {}
    for tenant in tenants:
        for day in day_list:
            result = days.get((tenant, day))
            if result is None or result.status == "unread":
                continue
            for t in result.ended:
                if t.get("completed") is None or t.get("state") not in _OUTCOME:
                    continue
                if not _passes(t, params, arrival=False):
                    continue
                row = dict(t, tenant=tenant)
                seen[row["id"]] += 1
                held = ended.get(row["id"])
                if held is None or row["completed"] > held["completed"]:
                    ended[row["id"]] = row
            for t in result.arrived:
                if t.get("created") is None or not _passes(t, params, arrival=True):
                    continue
                arrived.setdefault(t["id"], dict(t, tenant=tenant))
    reopened = sum(1 for n in seen.values() if n > 1)
    return list(ended.values()), list(arrived.values()), reopened


def _place(
    tuples: Iterable[dict[str, Any]],
    key: str,
    edges_ms: Sequence[int],
    until_ms: int,
    reasons: Sequence[str | None],
) -> list[tuple[int, dict[str, Any]]]:
    placed = []
    buckets = len(edges_ms) - 1
    for t in tuples:
        at = t.get(key)
        if at is None or at < edges_ms[0] or at >= until_ms:
            continue
        i = bisect.bisect_right(edges_ms, at) - 1
        if i < 0 or i >= buckets or reasons[i]:
            continue
        placed.append((i, t))
    placed.sort(key=lambda item: (item[1][key], str(item[1]["id"])))
    return placed


def _latency_side(tuples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """wait = first_start - eligible; run = completed - first_start (parks and
    retries included); total = completed - created. Tasks that never started are
    in n and total, and left out of wait and run. A negative duration (clock skew
    between two writers) is read as 0 rather than drawn below the axis."""
    wait: list[float] = []
    run: list[float] = []
    total: list[float] = []
    for t in tuples:
        if t.get("created") is not None:
            total.append(max(0, t["completed"] - t["created"]) / 1000.0)
        if t.get("first_start") is not None:
            run.append(max(0, t["completed"] - t["first_start"]) / 1000.0)
            if t.get("eligible") is not None:
                wait.append(max(0, t["first_start"] - t["eligible"]) / 1000.0)
    return {"n": len(tuples), "wait": _stat(wait), "run": _stat(run), "total": _stat(total)}


def _latency(kept: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], int]:
    rows: dict[str, dict[str, Any]] = {}
    wait_excluded = 0
    for t in kept:
        outcome = _OUTCOME[t["state"]]
        if outcome == "cancelled":
            continue
        row = rows.setdefault(
            str(t.get("profile")), {"succeeded": [], "failed": [], "timeouts": set()}
        )
        row["succeeded" if outcome == "succeeded" else "failed"].append(t)
        row["timeouts"].add(t.get("timeout_s"))
        if t.get("first_start") is not None and t.get("eligible") is None:
            wait_excluded += 1
    out = []
    for name, row in rows.items():
        timeouts = row["timeouts"]
        out.append(
            {
                "runner_profile": name,
                "timeout_s": next(iter(timeouts)) if len(timeouts) == 1 else None,
                "succeeded": _latency_side(row["succeeded"]),
                "failed": _latency_side(row["failed"]),
            }
        )
    out.sort(key=lambda r: (-(r["succeeded"]["n"] + r["failed"]["n"]), r["runner_profile"]))
    return {"percentile_method": "nearest_rank", "by_profile": out}, wait_excluded


def _retries(kept: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """`Task.attempt_count` counts ADMISSIONS (leases), not runs."""
    labels = ("0", "1", "2", "3+")
    tries = {
        label: {"attempts": label, "tasks": 0, "succeeded": 0, "failed": 0,
                "dead_lettered": 0, "cancelled": 0}
        for label in labels
    }
    needed = admitted = rescued = failed_after = 0
    exits: Counter = Counter()
    not_final = 0
    without_doc = 0
    for t in kept:
        count = max(0, _int(t.get("attempt_count")))
        outcome = _OUTCOME[t["state"]]
        row = tries["3+" if count >= 3 else str(count)]
        row["tasks"] += 1
        row[outcome] += 1
        if count >= 1:
            admitted += 1
        if count >= 2:
            needed += 1
            if outcome == "succeeded":
                rescued += 1
            elif outcome in ("failed", "dead_lettered"):
                failed_after += 1
        for code in t.get("nonfinal_exits") or []:
            exits[code] += 1
            not_final += 1
        without_doc += max(0, count - _int(t.get("att_docs")))
    by_exit = [
        {"exit_code": code, "label": exit_label(code), "n": n} for code, n in exits.items()
    ]
    by_exit.sort(key=lambda r: (-r["n"], r["exit_code"] is None, r["exit_code"] or 0))
    return {
        "tries": [tries[label] for label in labels],
        "needed_retry": {"k": needed, "of": admitted},
        "rescued": rescued,
        "failed_after_retry": failed_after,
        "not_final": {"attempts": not_final, "by_exit": by_exit},
        "admissions_without_attempt_doc": without_doc,
    }


def _spend_of(rows: Iterable[Mapping[str, Any]], prefix: str = "") -> dict[str, Any]:
    total = 0.0
    attempts = reporting = 0
    for t in rows:
        attempts += _int(t.get(f"{prefix}att_started"))
        reporting += _int(t.get(f"{prefix}att_reporting"))
        value = t.get(f"{prefix}cost_usd")
        if value is not None:
            total += float(value)
    return _cost(total, attempts, reporting)


def _cost_totals(kept: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Reported cost, by the task's END. Never a bill: only what attempts reported.

    `per_succeeded_task` takes only succeeded tasks whose EVERY started attempt
    reported a cost, so a task's figure is never a partial sum passed off as its
    price; `n` of `of` says how many that was.
    """
    by_outcome = {
        outcome: _spend_of(t for t in kept if _OUTCOME[t["state"]] == outcome)
        for outcome in ("succeeded", "failed", "dead_lettered", "cancelled")
    }
    succeeded = [t for t in kept if t["state"] == TaskState.SUCCEEDED.value]
    full = sorted(
        float(t["cost_usd"])
        for t in succeeded
        if t.get("cost_usd") is not None
        and _int(t.get("att_started")) > 0
        and _int(t.get("att_reporting")) == _int(t.get("att_started"))
    )
    declared = [t for t in kept if t.get("profile") in DECLARED_COST_PROFILES]
    return {
        **_spend_of(kept),
        "by_outcome": by_outcome,
        "per_succeeded_task": {
            "n": len(full),
            "of": len(succeeded),
            "p50_usd": _money(percentile(full, 50)) if full else None,
            "p95_usd": _money(percentile(full, 95)) if full else None,
            "max_usd": _money(full[-1]) if full else None,
            "values_usd": [_money(v) for v in full] if len(full) < SMALL_N else None,
        },
        # Attempts after each task's first (its lowest generation).
        "retries": _spend_of(kept, "retry_"),
        "declared": {"profiles": sorted(DECLARED_COST_PROFILES), **_spend_of(declared)},
    }


def _group_key(t: Mapping[str, Any], by: str) -> str:
    if by == "tenant_id":
        return str(t.get("tenant"))
    if by == "submitted_by":
        return str(t.get("submitted_by") or "")
    return str(t.get("profile"))


def _groups(
    placed_ended: Sequence[tuple[int, dict[str, Any]]],
    placed_arrived: Sequence[tuple[int, dict[str, Any]]],
    by: str,
    reasons: Sequence[str | None],
) -> dict[str, Any]:
    buckets = len(reasons)
    rows: dict[str, dict[str, Any]] = {}

    def row_for(key: str) -> dict[str, Any]:
        return rows.setdefault(key, {"tally": _Tally(), "k": [0] * buckets, "n": [0] * buckets})

    for _, t in placed_arrived:
        row_for(_group_key(t, by))["tally"].submitted += 1
    for i, t in placed_ended:
        row = row_for(_group_key(t, by))
        row["tally"].add_ended(t)
        outcome = _OUTCOME[t["state"]]
        if outcome == "succeeded":
            row["k"][i] += 1
            row["n"][i] += 1
        elif outcome != "cancelled":
            row["n"][i] += 1

    out = []
    for key, row in rows.items():
        tally: _Tally = row["tally"]
        series = None
        if buckets <= SERIES_MAX_BUCKETS:
            series = [
                None if reasons[i] else {"k": row["k"][i], "n": row["n"][i]}
                for i in range(buckets)
            ]
        out.append(
            {
                "key": key,
                "submitted": tally.submitted,
                "ended": tally.ended,
                "succeeded": tally.succeeded,
                "failed": tally.failed,
                "dead_lettered": tally.dead_lettered,
                "cancelled": tally.cancelled_api(),
                "rate": wilson(tally.succeeded, tally.decided),
                "cost": _cost(tally.cost_sum, tally.cost_attempts, tally.cost_reporting),
                "declared_cost": by == "runner_profile" and key in DECLARED_COST_PROFILES,
                "series": series,
            }
        )
    out.sort(key=lambda r: (-(r["failed"] + r["dead_lettered"]), -r["ended"], r["key"]))
    return {"by": by, "rows_total": len(out), "rows": out[:GROUP_ROWS_MAX]}


def _workflows_failed(kept: Sequence[Mapping[str, Any]], params: Params) -> dict[str, Any]:
    """Which workflows had a step end FAILED or DEAD_LETTERED in the span, and
    where it first broke. `state` and `steps` are filled in by the service from
    the owner-chosen rollup path; until then a row reads UNKNOWN / null.

    `cascade_cancelled` counts the cancels THE FAILURE caused: after a failure
    and the fail_workflow sweep. A step cancelled after a CANCELLED parent
    (`after_cancel`) is left out -- somebody stopped that branch, the failure
    did not.

    Under kind=standalone the block does not apply, and its counts are null
    rather than 0: nothing was counted, so no count is a measurement (the
    review of #196 found 0 served here, which reads as "no workflow failed").
    """
    if params.kind == "standalone":
        return {
            "applicable": False,
            "with_ended_steps": None,
            "with_failed_steps": None,
            "rows_total": None,
            "rows": [],
            "failing_steps": [],
        }
    by_workflow: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for t in kept:
        if t.get("workflow_id"):
            by_workflow[(str(t["tenant"]), str(t["workflow_id"]))].append(t)
    rows = []
    failing: Counter = Counter()
    for (tenant, workflow_id), tuples in by_workflow.items():
        failed = sorted(
            (t for t in tuples if t["state"] in _FAILED_VALUES),
            key=lambda t: (t["completed"], str(t["id"])),
        )
        for t in failed:
            if t.get("step_id"):
                failing[str(t["step_id"])] += 1
        if not failed:
            continue
        first = failed[0]
        last = max(t["completed"] for t in tuples)
        rows.append(
            (
                last,
                {
                    "workflow_id": workflow_id,
                    "tenant_id": tenant,
                    "submitted_by": first.get("submitted_by"),
                    "first_failed": {
                        "step_id": first.get("step_id"),
                        "task_id": first["id"],
                        "failure_class": first.get("failure_class"),
                        "ended_at": _utc_iso_ms(first["completed"]),
                    },
                    "cascade_cancelled": sum(
                        1 for t in tuples
                        if t.get("cancel_cause") in ("after_failure", "workflow_sweep")
                    ),
                    "last_ended_at": _utc_iso_ms(last),
                    "state": UNKNOWN,
                    "steps": None,
                },
            )
        )
    rows.sort(key=lambda item: (-item[0], item[1]["workflow_id"]))
    return {
        "applicable": True,
        "with_ended_steps": len(by_workflow),
        "with_failed_steps": len(rows),
        "rows_total": len(rows),
        "rows": [row for _, row in rows[:WORKFLOW_ROWS_MAX]],
        "failing_steps": [
            {"step_id": step, "n": n}
            for step, n in sorted(failing.items(), key=lambda kv: (-kv[1], kv[0]))[:FAILING_STEPS_MAX]
        ],
    }


def _previous(
    params: Params,
    tenants: Sequence[str],
    days: Mapping[tuple[str, date], DayResult],
    previous_days: Sequence[date],
) -> dict[str, Any] | None:
    if not params.previous_edges:
        return None
    edges = params.previous_edges
    edges_ms = [_ms(e) for e in edges]
    reasons = _bucket_reasons(edges_ms, edges_ms[-1], _unread_days(days, tenants, previous_days))
    ended, _, _ = _gather(days, tenants, previous_days, params)
    tally = _Tally()
    for _, t in _place(ended, "completed", edges_ms, edges_ms[-1], reasons):
        tally.add_ended(t)
    return {
        "since": _local_iso(edges[0], params.tz),
        "until": _local_iso(edges[-1], params.tz),
        "complete": not any(reasons),
        "succeeded": tally.succeeded,
        "failed": tally.failed,
        "dead_lettered": tally.dead_lettered,
        "cancelled_total": sum(tally.cancelled.values()),
        "rate": wilson(tally.succeeded, tally.decided),
    }


def fold(
    *,
    params: Params,
    tenants: Sequence[str],
    days: Mapping[tuple[str, date], DayResult],
    main_days: Sequence[date],
    previous_days: Sequence[date] = (),
    generated_at: datetime,
) -> dict[str, Any]:
    """Every figure the response carries, from the tuples. Pure.

    Totals, cards and groups are computed ONLY over buckets that were read
    (the TS-9 rule): a bucket is unread when any overlapping tenant-day is, and
    then it contributes nothing anywhere -- not a partial sum in one place and
    a full one in another.
    """
    edges_ms = [_ms(e) for e in params.edges]
    until_ms = _ms(params.until)
    generated_ms = _ms(generated_at)
    buckets_n = len(edges_ms) - 1
    reasons = _bucket_reasons(edges_ms, until_ms, _unread_days(days, tenants, main_days))
    ended, arrived, reopened = _gather(days, tenants, main_days, params)
    placed_ended = _place(ended, "completed", edges_ms, until_ms, reasons)
    placed_arrived = _place(arrived, "created", edges_ms, until_ms, reasons)

    tallies = [_Tally() for _ in range(buckets_n)]
    for i, _t in placed_arrived:
        tallies[i].submitted += 1
    for i, t in placed_ended:
        tallies[i].add_ended(t)

    buckets = []
    total = _Tally()
    read = 0
    grace_ms = OUTCOMES_SEAL_GRACE_S * 1000
    for i in range(buckets_n):
        end_ms = edges_ms[i + 1]
        if reasons[i]:
            state = "unread"
        elif end_ms + grace_ms <= generated_ms:
            state = "sealed"
        else:
            state = "open"
        row: dict[str, Any] = {
            "start": _local_iso(params.edges[i], params.tz),
            "end": _local_iso(params.edges[i + 1], params.tz),
            "state": state,
            "in_progress": end_ms > generated_ms,
            "unread_reason": reasons[i],
        }
        if reasons[i]:
            row.update(_UNREAD_FIELDS)
        else:
            row.update(tallies[i].to_api())
            total.merge(tallies[i])
            read += 1
        buckets.append(row)

    kept = [t for _, t in placed_ended]
    latency, wait_excluded = _latency(kept)
    totals = {
        "complete": read == buckets_n,
        "buckets": buckets_n,
        "buckets_read": read,
        "submitted": total.submitted,
        "ended": total.ended,
        "succeeded": total.succeeded,
        "failed": total.failed,
        "dead_lettered": total.dead_lettered,
        "cancelled": total.cancelled_api(),
        "rate": wilson(total.succeeded, total.decided),
        "failure_classes": dict(total.classes),
        "cost": _cost_totals(kept),
    }
    return {
        "buckets": buckets,
        "totals": totals,
        "retries": _retries(kept),
        "latency": latency,
        "groups": _groups(placed_ended, placed_arrived, params.group, reasons),
        "workflows_failed": _workflows_failed(kept, params),
        "previous": _previous(params, tenants, days, previous_days),
        "reopened": reopened,
        "wait_excluded": wait_excluded,
    }


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------

class Outcomes:
    """Reads, derives, writes and drift-checks the per-tenant per-day rollup.

    Built once in `deps.build_context` and hung on `AppContext.outcomes`, the way
    `WorkflowRollups` is, so a test drives the shipped code over an in-memory
    Firestore. The constructor's budgets exist so a test can reach the
    `derive_budget` and `too_large` paths without seeding thousands of tasks.
    """

    def __init__(
        self,
        *,
        store: Any,
        rollups: Any,
        metrics: Any = None,
        now: Callable[[], datetime] = utcnow,
        derive_read_budget: int = OUTCOMES_DERIVE_READ_BUDGET,
        derive_seconds: float = OUTCOMES_DERIVE_SECONDS,
        shard_bytes: int = SHARD_BYTES,
        max_shards: int = MAX_SHARDS,
        clock: Callable[[], float] = _time.monotonic,
    ) -> None:
        self._store = store
        self._rollups = rollups
        self._metrics = metrics
        self._now = now
        self._budget = derive_read_budget
        self._derive_seconds = derive_seconds
        self._shard_bytes = shard_bytes
        self._max_parts = 1 + max(0, max_shards)
        self._clock = clock
        self._cache: dict[Any, tuple[datetime, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    @property
    def _db(self) -> Any:
        return self._store.db

    def _utcnow(self) -> datetime:
        # Whole seconds, so `until` (which is `now` when the span runs to now)
        # and `generated_at` are the same instant as printed.
        return self._now().astimezone(timezone.utc).replace(microsecond=0)

    # -- reading tasks and attempts (every query leads with tenant_id) -----

    def _ended_docs(self, tenant_id: str, lo: datetime, hi: datetime, meter: _Meter) -> list[dict[str, Any]]:
        """Tasks of ONE tenant that ended in [lo, hi). Index: tasks-tenant-completed."""
        query = (
            self._db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("completed_at", ">=", lo))
            .where(filter=FieldFilter("completed_at", "<", hi))
            .order_by("completed_at", direction=firestore.Query.ASCENDING)
        )
        rows = [snap.to_dict() or {} for snap in query.stream()]
        meter.add(max(1, len(rows)))
        return rows

    def _arrived_docs(self, tenant_id: str, lo: datetime, hi: datetime, meter: _Meter) -> list[dict[str, Any]]:
        """Tasks of ONE tenant created in [lo, hi). The explicit DESC is what lets
        the existing tasks-tenant-created (tenant_id ASC, created_at DESC) serve it."""
        query = (
            self._db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("created_at", ">=", lo))
            .where(filter=FieldFilter("created_at", "<", hi))
            .order_by("created_at", direction=firestore.Query.DESCENDING)
        )
        rows = [snap.to_dict() or {} for snap in query.stream()]
        meter.add(max(1, len(rows)))
        return rows

    def _attempts(self, tenant_id: str, task_ids: Sequence[str], meter: _Meter) -> dict[str, list[dict[str, Any]]]:
        """Equality plus `in`, no ordering: merged single-field indexes serve it."""
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for chunk in _chunks(list(task_ids), _IN_CHUNK):
            query = (
                self._db.collection(ATTEMPTS)
                .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                .where(filter=FieldFilter("task_id", "in", list(chunk)))
            )
            rows = [snap.to_dict() or {} for snap in query.stream()]
            meter.add(max(1, len(rows)))
            for row in rows:
                if row.get("tenant_id") == tenant_id and row.get("task_id"):
                    out[str(row["task_id"])].append(row)
        return out

    def _get_tasks(self, tenant_id: str, task_ids: Sequence[str], meter: _Meter) -> dict[str, dict[str, Any] | None]:
        """Point reads, tenant-checked: another tenant's task reads as None."""
        out: dict[str, dict[str, Any] | None] = {task_id: None for task_id in task_ids}
        collection = self._db.collection(TASKS)
        for chunk in _chunks(list(task_ids), _GET_ALL_CHUNK):
            snaps = list(self._db.get_all([collection.document(task_id) for task_id in chunk]))
            meter.add(len(chunk))
            for snap in snaps:
                if not snap.exists:
                    continue
                data = snap.to_dict() or {}
                if data.get("tenant_id") == tenant_id:
                    out[snap.id] = data
        return out

    def _tuples(
        self,
        tenant_id: str,
        ended_docs: Sequence[dict[str, Any]],
        arrived_docs: Sequence[dict[str, Any]],
        meter: _Meter,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        mine_ended = [
            doc for doc in ended_docs
            if doc.get("tenant_id") == tenant_id
            and doc.get("state") in _TERMINAL_VALUES
            and doc.get("completed_at") is not None
            and doc.get("id")
        ]
        mine_arrived = [
            doc for doc in arrived_docs
            if doc.get("tenant_id") == tenant_id and doc.get("id") and doc.get("created_at") is not None
        ]
        attempts = self._attempts(tenant_id, [str(d["id"]) for d in mine_ended], meter)
        known: dict[str, dict[str, Any] | None] = {
            str(doc["id"]): doc for doc in [*mine_ended, *mine_arrived]
        }
        wanted = sorted(
            {
                str(parent)
                for doc in mine_ended
                for parent in (doc.get("depends_on") or [])
                if str(parent) not in known
            }
        )
        if wanted:
            known.update(self._get_tasks(tenant_id, wanted, meter))
        ended = [
            tuple_from_docs(doc, attempts.get(str(doc["id"]), []), known) for doc in mine_ended
        ]
        ended.sort(key=lambda t: (t["completed"], str(t["id"])))
        arrived = sorted(
            (arrival_from_doc(doc) for doc in mine_arrived),
            key=lambda t: (t["created"], str(t["id"])),
        )
        return ended, arrived

    def derive_day(self, tenant_id: str, day: date, meter: _Meter | None = None) -> DayData:
        """DERIVE one tenant's UTC day, in full. The only implementation."""
        meter = meter or _Meter()
        before = meter.reads
        lo, hi = _day_start(day), _day_end(day)
        ended_docs = self._ended_docs(tenant_id, lo, hi, meter)
        arrived_docs = self._arrived_docs(tenant_id, lo, hi, meter)
        ended, arrived = self._tuples(tenant_id, ended_docs, arrived_docs, meter)
        return DayData(ended=ended, arrived=arrived, reads=meter.reads - before)

    # -- the rollup documents ------------------------------------------------

    def _read_stored(self, keys: Sequence[tuple[str, date]], meter: _Meter) -> dict[tuple[str, date], _Stored]:
        """`get_all` of each tenant-day's base doc in chunks of 100, then its shards."""
        out = {key: _Stored() for key in keys}
        collection = self._db.collection(COLLECTION)
        bases: dict[tuple[str, date], dict[str, Any]] = {}
        for chunk in _chunks(list(keys), _GET_ALL_CHUNK):
            ids = {day_doc_id(tenant, day): (tenant, day) for tenant, day in chunk}
            try:
                snaps = list(self._db.get_all([collection.document(i) for i in ids]))
            except Exception:
                log.warning("outcome day documents could not be read", exc_info=True)
                for key in chunk:
                    out[key].failed = True
                continue
            meter.add(len(ids))
            for snap in snaps:
                key = ids.get(snap.id)
                if key is None or not snap.exists:
                    continue
                data = snap.to_dict() or {}
                bases[key] = data
                out[key].exists = True
                out[key].shards = max(0, _int(data.get("shards")))

        wanted = [
            (key, part, shard_doc_id(day_doc_id(*key), part))
            for key in bases
            for part in range(1, out[key].shards + 1)
        ]
        shard_docs: dict[tuple[tuple[str, date], int], dict[str, Any]] = {}
        for chunk in _chunks(wanted, _GET_ALL_CHUNK):
            by_id = {shard_id: (key, part) for key, part, shard_id in chunk}
            try:
                snaps = list(self._db.get_all([collection.document(i) for i in by_id]))
            except Exception:
                log.warning("outcome day shards could not be read", exc_info=True)
                for key, _, _ in chunk:
                    out[key].failed = True
                continue
            meter.add(len(by_id))
            for snap in snaps:
                if snap.exists and snap.id in by_id:
                    shard_docs[by_id[snap.id]] = snap.to_dict() or {}

        for key, base in bases.items():
            if out[key].failed:
                continue
            out[key].doc = _decode_stored(
                base, [shard_docs.get((key, part)) for part in range(1, out[key].shards + 1)]
            )
        return out

    @staticmethod
    def _cut(day: date, now: datetime, sealed: bool) -> datetime:
        if sealed:
            return _day_end(day)
        return max(_day_start(day), min(now - timedelta(seconds=OUTCOMES_LIVE_SKEW_S), _day_end(day)))

    def _write(
        self,
        tenant_id: str,
        day: date,
        data: DayData,
        *,
        sealed: bool,
        now: datetime,
        prior_shards: int,
    ) -> datetime:
        """WRITE one tenant-day: an idempotent whole-document set, one batch.

        No transaction, for rollup._persist's reason: two concurrent readers
        derive from the same task documents and write the same tuples, keyed by
        task id, so they converge. The batch keeps a day's parts from tearing.
        """
        packed = _pack(
            data.ended, data.arrived, shard_bytes=self._shard_bytes, max_parts=self._max_parts
        )
        if packed is None:
            raise _TooLarge()
        cut = self._cut(day, now, sealed)
        base_id = day_doc_id(tenant_id, day)
        collection = self._db.collection(COLLECTION)
        batch = self._db.batch()
        first_ended, first_arrived = packed[0]
        batch.set(
            collection.document(base_id),
            {
                "tenant_id": tenant_id,
                "day": day.isoformat(),
                "sealed": sealed,
                "derive_version": DERIVE_VERSION,
                "classifier_version": CLASSIFIER_VERSION,
                "built_at": now,
                "built_through": cut,
                "n_ended": len(data.ended),
                "n_arrived": len(data.arrived),
                "shards": len(packed) - 1,
                "reads": data.reads,
                "ended": first_ended,
                "arrived": first_arrived,
            },
        )
        for part, (ended, arrived) in enumerate(packed[1:], start=1):
            batch.set(
                collection.document(shard_doc_id(base_id, part)),
                {
                    "tenant_id": tenant_id,
                    "day": day.isoformat(),
                    "part": part,
                    "built_at": now,
                    "ended": ended,
                    "arrived": arrived,
                },
            )
        for part in range(len(packed), prior_shards + 1):
            batch.delete(collection.document(shard_doc_id(base_id, part)))
        batch.commit()
        return cut

    def _live(
        self, tenant_id: str, day: date, stored: _Stored, now: datetime, meter: _Meter
    ) -> DayResult:
        """A live day: the cached derive plus the delta since its cut, deduped by id."""
        doc = stored.doc
        assert doc is not None
        lo = doc.built_through or _day_start(day)
        hi = _day_end(day)
        before = meter.reads
        try:
            if lo < hi:
                ended_docs = self._ended_docs(tenant_id, lo, hi, meter)
                arrived_docs = self._arrived_docs(tenant_id, lo, hi, meter)
                delta_ended, delta_arrived = self._tuples(tenant_id, ended_docs, arrived_docs, meter)
            else:
                delta_ended, delta_arrived = [], []
        except Exception:
            log.warning("live outcome delta for %s %s failed", tenant_id, day, exc_info=True)
            return DayResult("unread", "read_failed")
        ended = _merge(doc.ended, delta_ended, "completed")
        arrived = _merge(doc.arrived, delta_arrived, "created")
        stale = doc.built_at is None or (now - doc.built_at) > timedelta(seconds=OUTCOMES_LIVE_REWRITE_S)
        if stale and not doc.sealed:
            try:
                self._write(
                    tenant_id,
                    day,
                    DayData(ended=ended, arrived=arrived, reads=doc.reads + meter.reads - before),
                    sealed=False,
                    now=now,
                    prior_shards=stored.shards,
                )
            except _TooLarge:
                return DayResult("unread", "too_large")
            except Exception:
                log.warning("live outcome day %s %s was not rewritten", tenant_id, day, exc_info=True)
        return DayResult("live", ended=ended, arrived=arrived, built_through=doc.built_through)

    def _collect(
        self, tenants: Sequence[str], days: Sequence[date], now: datetime, meter: _Meter
    ) -> tuple[dict[tuple[str, date], DayResult], int]:
        """Every tenant-day the read needs: stored, live-plus-delta, or derived now.

        Missing, stale and unsealed-past-seal days are derived newest first,
        under the per-request budget. Past it they are unread with reason
        `derive_budget`; a Firestore error is `read_failed`; a day too big even
        sharded is `too_large`. None of the three ever becomes a zero.
        """
        keys = [(tenant, day) for tenant in tenants for day in days]
        stored = self._read_stored(keys, meter)
        results: dict[tuple[str, date], DayResult] = {}
        derived = 0
        derive_reads = 0
        started = self._clock()
        for key in sorted(keys, key=lambda k: (k[1], k[0]), reverse=True):
            tenant_id, day = key
            entry = stored[key]
            if entry.failed:
                results[key] = DayResult("unread", "read_failed")
                continue
            sealable = now >= _seal_at(day)
            doc = entry.doc
            if doc is not None and doc.sealed and sealable:
                results[key] = DayResult("sealed", ended=doc.ended, arrived=doc.arrived)
                continue
            if doc is not None and not sealable:
                results[key] = self._live(tenant_id, day, entry, now, meter)
                continue
            if derive_reads >= self._budget or (self._clock() - started) >= self._derive_seconds:
                results[key] = DayResult("unread", "derive_budget")
                continue
            before = meter.reads
            try:
                data = self.derive_day(tenant_id, day, meter)
            except Exception:
                log.warning("outcome day %s %s could not be derived", tenant_id, day, exc_info=True)
                derive_reads += meter.reads - before
                results[key] = DayResult("unread", "read_failed")
                continue
            derive_reads += meter.reads - before
            derived += 1
            try:
                self._write(tenant_id, day, data, sealed=sealable, now=now, prior_shards=entry.shards)
            except _TooLarge:
                results[key] = DayResult("unread", "too_large")
                continue
            except Exception:
                # The derive is still true for THIS response; only the cache
                # was not refreshed, and the next read derives again.
                log.warning("outcome day %s %s was not written", tenant_id, day, exc_info=True)
            results[key] = DayResult(
                "sealed" if sealable else "live",
                ended=data.ended,
                arrived=data.arrived,
                built_through=None if sealable else self._cut(day, now, False),
                derived=True,
            )
        return results, derived

    # -- scope, workflows and counts ----------------------------------------

    def _resolve_scope(
        self, params: Params, own_tenant: str, meter: _Meter
    ) -> tuple[dict[str, Any], list[str], bool]:
        """(scope payload, tenant ids, whole_platform). `whole_platform` is True
        only for an admin's unfiltered, complete platform view -- the one case a
        count may drop its tenant filter. Tenant scope is never it."""
        if params.scope == "tenant":
            return {"kind": "tenant", "tenant_id": own_tenant}, [own_tenant], False
        listed = self._store.list_tenants(limit=TENANT_LIST_LIMIT)
        meter.add(max(1, len(listed)))
        known = sorted({t.tenant_id for t in listed})
        complete = len(listed) < TENANT_LIST_LIMIT
        named = params.tenant or params.exclude_tenant
        unknown = [t for t in named if t not in known]
        if unknown and not complete:
            found = []
            for tenant_id in unknown:
                meter.add(1)
                if self._store.get_tenant(tenant_id) is not None:
                    found.append(tenant_id)
            unknown = [t for t in unknown if t not in found]
        if unknown:
            parameter = "tenant" if params.tenant else "exclude_tenant"
            raise _refuse(
                parameter,
                f"unknown tenant {unknown[0]!r}",
                value=unknown,
                known_tenants=known,
            )
        if params.tenant:
            tenants = sorted(set(params.tenant))
        else:
            tenants = [t for t in known if t not in params.exclude_tenant]
        payload = {
            "kind": "platform",
            "tenants": tenants,
            "excluded": list(params.exclude_tenant),
            "tenants_complete": complete,
        }
        whole_platform = complete and not params.tenant and not params.exclude_tenant
        return payload, tenants, whole_platform

    def _enrich_workflows(self, block: dict[str, Any], meter: _Meter) -> None:
        """state and steps for the listed workflows, through WorkflowRollups.

        The owner-chosen rollup path, bounded by store._STEP_READ_BUDGET per
        tenant. Past the budget -- or with a workflow document missing -- a row
        keeps state UNKNOWN and steps null, and is still listed.
        """
        by_tenant: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in block["rows"]:
            by_tenant[row["tenant_id"]].append(row)
        for tenant_id, rows in by_tenant.items():
            workflows = []
            index: dict[str, dict[str, Any]] = {}
            for row in rows:
                meter.add(1)
                try:
                    workflow = self._store.get_workflow(tenant_id, row["workflow_id"])
                except NotFound:
                    continue
                except Exception:
                    log.warning("workflow %s could not be read", row["workflow_id"], exc_info=True)
                    continue
                workflows.append(workflow)
                index[workflow.workflow_id] = row
            if not workflows:
                continue
            try:
                results, report = self._rollups.for_workflows(tenant_id, workflows, persist=True)
            except Exception:
                log.warning("workflow rollups for %s failed", tenant_id, exc_info=True)
                continue
            meter.add(report.step_reads)
            for result in results:
                row = index.get(result.workflow.workflow_id)
                if row is None:
                    continue
                row["state"] = effective_state(result.rollup)
                if result.workflow.submitted_by:
                    row["submitted_by"] = result.workflow.submitted_by
                row["steps"] = _steps(result) if result.rollup.complete else None

    def _terminal_without_completed_at(
        self, tenants: Sequence[str], whole_platform: bool, meter: _Meter
    ) -> int | None:
        """count() of terminal tasks with no completed_at: what the ledger cannot place.

        Equality-only filters, which single-field indexes should serve by merge
        join. NOT VERIFIED against real Firestore -- the emulator does not
        enforce indexes -- so a failed count is null, never 0.
        """
        scopes: list[str | None] = [None] if whole_platform else list(tenants)
        total = 0
        try:
            for tenant_id in scopes:
                for state in sorted(_TERMINAL_VALUES):
                    query: Any = self._db.collection(TASKS)
                    if tenant_id is not None:
                        query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
                    query = query.where(filter=FieldFilter("state", "==", state)).where(
                        filter=FieldFilter("completed_at", "==", None)
                    )
                    meter.add(1)
                    for row in query.count().get():
                        for item in row:
                            total += int(item.value)
                            break
                        break
        except Exception:
            log.warning("terminal_without_completed_at could not be counted", exc_info=True)
            return None
        return total

    # -- the cache -----------------------------------------------------------

    def _cache_get(self, key: Any, now: datetime) -> dict[str, Any] | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires, payload = entry
            if now >= expires:
                self._cache.pop(key, None)
                return None
            return payload

    def _cache_put(self, key: Any, payload: dict[str, Any], now: datetime) -> None:
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            while len(self._cache) >= _CACHE_MAX_ENTRIES:
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)
            self._cache[key] = (now + timedelta(seconds=OUTCOMES_CACHE_S), payload)

    # -- GET /v1/outcomes ------------------------------------------------------

    def read(self, *, tenant_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """The whole response. `tenant_id` is tenant_scope's resolved id; the route
        has already run the admin gate if anything beyond it was asked for."""
        now = self._utcnow()
        params = parse_params(raw, now=now)
        meter = _Meter()
        scope, tenants, whole_platform = self._resolve_scope(params, tenant_id, meter)
        key = (
            scope["kind"],
            tenant_id if params.scope == "tenant" else None,
            tuple(tenants),
            params.canonical(),
            now.replace(second=0),
        )
        hit = self._cache_get(key, now)
        if hit is not None:
            # The ORIGINAL generated_at, so the age a reader is shown stays true.
            return {**hit, "cached": True}

        main_days = _utc_days(params.since, params.until)
        previous_days = (
            _utc_days(params.previous_edges[0], params.previous_edges[-1])
            if params.previous_edges
            else []
        )
        wanted = sorted(set(main_days) | set(previous_days))
        days, derived = self._collect(tenants, wanted, now, meter)
        folded = fold(
            params=params,
            tenants=tenants,
            days=days,
            main_days=main_days,
            previous_days=previous_days,
            generated_at=now,
        )
        self._enrich_workflows(folded["workflows_failed"], meter)
        terminal = self._terminal_without_completed_at(tenants, whole_platform, meter)

        census = {"total": 0, "sealed": 0, "live": 0, "unread": 0}
        unread = []
        cuts = []
        for tenant in tenants:
            for day in main_days:
                result = days.get((tenant, day)) or DayResult("unread", "read_failed")
                census["total"] += 1
                census[result.status] += 1
                if result.status == "unread":
                    unread.append({"tenant_id": tenant, "day": day.isoformat(), "reason": result.reason})
                elif result.status == "live" and result.built_through is not None:
                    cuts.append(result.built_through)
        unread.sort(key=lambda u: (u["day"], u["tenant_id"]))

        payload = {
            "scope": scope,
            "tz": params.tz_name,
            "requested": dict(params.requested),
            "since": _local_iso(params.since, params.tz),
            "until": _local_iso(params.until, params.tz),
            "bucket": params.bucket,
            "bucket_chosen_by": params.bucket_chosen_by,
            "basis": {"outcomes": "completed_at", "submitted": "created_at"},
            "filters": params.filters(),
            "vocab": VOCAB,
            "buckets": folded["buckets"],
            "totals": folded["totals"],
            "retries": folded["retries"],
            "latency": folded["latency"],
            "groups": folded["groups"],
            "workflows_failed": folded["workflows_failed"],
            "coverage": {
                "days": census,
                "unread": unread,
                "derived_now": derived,
                "built_through": _utc_iso(min(cuts)) if cuts else None,
                "reopened": folded["reopened"],
                "terminal_without_completed_at": terminal,
                "wait_excluded": folded["wait_excluded"],
                "seal_grace_s": OUTCOMES_SEAL_GRACE_S,
            },
            "previous": folded["previous"],
            "reads": meter.reads,
            "cached": False,
            "generated_at": _utc_iso(now),
        }
        previous = folded["previous"]
        # Only a COMPLETE payload is cached. One with unread days is not, so a
        # re-request continues the derive where this one stopped instead of
        # being handed the same gaps for a minute.
        if folded["totals"]["complete"] and (previous is None or previous["complete"]):
            self._cache_put(key, payload, now)
        return payload

    # -- POST /v1/admin/outcomes/rollup ----------------------------------------

    def maintain(
        self,
        *,
        tenant_id: str,
        since: str,
        until: str | None,
        repair: bool,
        build_missing: bool,
    ) -> dict[str, Any]:
        """Backfill missing days and DRIFT-CHECK stored sealed ones for one tenant.

        A disagreement is REPORTED -- counted and logged before anything
        touches it -- and repaired only with repair=true, and a repaired day
        still reads `repaired: true` beside its differences rather than
        vanishing from the report (rollup.drift_of's rule). A day whose derive
        could not complete is `unknown`, never "disagree".
        """
        now = self._utcnow()
        start = _parse_day("since", since)
        end = _parse_day("until", until) if until else now.date() + _ONE_DAY
        if end <= start:
            raise _refuse("until", "until must be after since", since=since, until=until)
        if (end - start).days > 31:
            raise _refuse(
                "until", "at most 31 UTC days per call", value=(end - start).days, max=31
            )
        days = [start + timedelta(days=i) for i in range((end - start).days)]
        meter = _Meter()
        stored = self._read_stored([(tenant_id, day) for day in days], meter)
        report = {
            "examined": 0, "built": 0, "agreed": 0, "disagreed": 0, "unknown": 0,
            "repaired": 0, "live": 0, "reads": 0,
        }
        drifted: list[dict[str, Any]] = []
        for day in days:
            report["examined"] += 1
            entry = stored[(tenant_id, day)]
            if entry.failed:
                report["unknown"] += 1
                continue
            sealable = now >= _seal_at(day)
            doc = entry.doc
            try:
                if doc is None:
                    if not entry.exists and not build_missing:
                        continue
                    # Missing, or on another derive or classifier version:
                    # exactly what the next read would rebuild.
                    data = self.derive_day(tenant_id, day, meter)
                    self._write(tenant_id, day, data, sealed=sealable, now=now, prior_shards=entry.shards)
                    report["built"] += 1
                    continue
                if not doc.sealed:
                    if sealable:
                        data = self.derive_day(tenant_id, day, meter)
                        self._write(tenant_id, day, data, sealed=True, now=now, prior_shards=entry.shards)
                        report["built"] += 1
                    else:
                        report["live"] += 1
                    continue
                data = self.derive_day(tenant_id, day, meter)
                difference = _diff(doc, data)
                if difference is None:
                    report["agreed"] += 1
                    continue
                report["disagreed"] += 1
                self._record_drift(tenant_id, day, difference)
                repaired = False
                if repair:
                    self._write(tenant_id, day, data, sealed=True, now=now, prior_shards=entry.shards)
                    repaired = True
                    report["repaired"] += 1
                drifted.append({"day": day.isoformat(), **difference, "repaired": repaired})
            except _TooLarge:
                log.warning("outcome day %s %s is too large to store", tenant_id, day)
                report["unknown"] += 1
            except Exception:
                log.warning("outcome day %s %s could not be checked", tenant_id, day, exc_info=True)
                report["unknown"] += 1
        report["reads"] = meter.reads
        return {"tenant_id": tenant_id, "report": report, "drifted": drifted}

    def _record_drift(self, tenant_id: str, day: date, difference: Mapping[str, Any]) -> None:
        if self._metrics is not None:
            try:
                self._metrics.outcome_day_drift.labels(direction="disagree").inc()
            except Exception:  # pragma: no cover - a metric must never fail a check
                log.debug("could not record outcome day drift", exc_info=True)
        log.warning(
            "outcome day %s %s drifted: stored=%s derived=%s missing=%s extra=%s changed=%s",
            tenant_id,
            day,
            difference["stored_n"],
            difference["derived_n"],
            difference["missing_ids"],
            difference["extra_ids"],
            difference["changed_ids"],
        )


def _parse_day(name: str, text: str) -> date:
    if not _DATE_ONLY.fullmatch(text or ""):
        raise _refuse(name, f"{name} must be YYYY-MM-DD (a UTC day)", value=text)
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise _refuse(name, f"{name} is not a real date", value=text) from None


def _diff(stored: StoredDay, derived: DayData) -> dict[str, Any] | None:
    """What a stored sealed day and a fresh derive disagree on, or None."""
    held = {t["id"]: t for t in stored.ended}
    fresh = {t["id"]: t for t in derived.ended}
    held_arrived = {t["id"]: t for t in stored.arrived}
    fresh_arrived = {t["id"]: t for t in derived.arrived}
    missing = sorted(
        str(i) for i in (fresh.keys() - held.keys()) | (fresh_arrived.keys() - held_arrived.keys())
    )
    extra = sorted(
        str(i) for i in (held.keys() - fresh.keys()) | (held_arrived.keys() - fresh_arrived.keys())
    )
    changed = sorted(
        {str(i) for i in held.keys() & fresh.keys() if held[i] != fresh[i]}
        | {str(i) for i in held_arrived.keys() & fresh_arrived.keys() if held_arrived[i] != fresh_arrived[i]}
    )
    if not (missing or extra or changed):
        return None
    return {
        "stored_n": len(held),
        "derived_n": len(fresh),
        "stored_arrived_n": len(held_arrived),
        "derived_arrived_n": len(fresh_arrived),
        "missing_ids": missing[:20],
        "extra_ids": extra[:20],
        "changed_ids": changed[:20],
    }


_OPEN_STEP_STATES = tuple(
    s.value for s in TaskState if s not in TERMINAL_STATES
)


def _steps(result: Any) -> dict[str, int]:
    counts = result.rollup.counts
    return {
        "total": len(result.workflow.steps),
        "succeeded": counts.get(TaskState.SUCCEEDED.value, 0),
        "failed": counts.get(TaskState.FAILED.value, 0),
        "dead_lettered": counts.get(TaskState.DEAD_LETTERED.value, 0),
        "cancelled": counts.get(TaskState.CANCELLED.value, 0),
        "open": sum(counts.get(s, 0) for s in _OPEN_STEP_STATES) + counts.get("unstarted", 0),
        "unreadable": counts.get("unreadable", 0),
    }
