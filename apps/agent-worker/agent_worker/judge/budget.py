"""The per-task Jev budget, the circuit breaker and the audit, in one file.

WHY A FILE. Two processes call Jev for one attempt: the worker (at staging,
the gate and the post-steps) and the agent's `jev_decide` MCP server (owner
override of context-quality.md §5.1). The owner's budget -- at most 8 calls
and 120k input tokens per task (§8 Q4) -- and §5.5's breaker are per task,
not per process, so both read and write one ledger under an exclusive
`flock`. A call is admitted by reserving its slot under that lock, so two
processes racing for the eighth call cannot both get it.

WHAT IT HOLDS. Counts, the breaker, and §5.4 step 6's audit rows --
`{call_id, kind, backend, bytes, input_tokens, ms, outcome}`. Never a
payload, an answer or a key: the file lives in the attempt directory, which
the agent can read.

RESIDUAL. The `jev_decide` process runs as the agent's user, so an agent can
rewrite this file: raise its own allowance, or lower the spend `settle` will
charge to the month. The budget is a cost control against a cooperative
agent, not a security boundary. The backend's own records (the Internal
API's logs, TypeSafe's billing) are the authority when they disagree with
the ledger.

MONTHLY CAP. `monthly_cap_tokens` and `month_used_tokens` are read by the
worker when the attempt starts; a call is admitted only while this task's
spend still fits under what was left of the month. The task's spend is added
to the workspace's month once, by `settle`, when the attempt ends.

PER TASK, ACROSS ATTEMPTS (lane JEV-JUDGMENTS-RUN, 2026-11-02). The worker
settles each attempt with `settle_attempt`: in ONE transaction, fenced by the
attempt's generation (invariant 5), it adds the attempt's tokens to the
month's document and writes the attempt's own row
(`<tenant>_<task>_g<generation>`, `kind: attempt`, its calls and tokens). The
next attempt of the task reads those rows (`FirestoreMonthlyLedger
.task_spend`) and starts its ledger with their calls and tokens carried, so
the owner's 8 calls and 120k tokens are per task, not per attempt. What is
carried was charged to the month by the attempt that spent it, so it is
created as already settled and never charged twice.

BY KIND (owner, 2026-11-02: "Worker lane writes them"). The same fenced
transaction also writes the attempt row's `settled_at` and `by_kind:
{kind: {calls, input_tokens, output_tokens, outcomes}}`, summed from the
audit rows this settle takes (`Ledger.take_unsettled_attempt`), which is
the shape swarm-api's Jev usage read sums (`swarm_api.jevusage`). A skip
is an outcome, not a call. `malformed` is a failed call. `output_tokens` is
None while no audit row records any: never 0. Calls or tokens the audit did
not keep (it is bounded) are filed under `unattributed`, with no outcomes.
A stale generation's settle writes none of it, and its audit rows stay
unsettled with its calls.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol

#: Owner decision 2026-10-14 (§8 Q4 A): hard per-task limits.
MAX_CALLS_PER_TASK = 8
MAX_INPUT_TOKENS_PER_TASK = 120_000
#: What a call is assumed to cost before it is made: jev-browser's measured
#: average (RESULTS.md, 2026-09-17, about 2,850 input tokens per call). A
#: character count of the state would under-count -- the API's own prompt
#: costs tokens too -- and admitting a call the budget cannot afford is the
#: one outcome the hard cap exists to prevent.
MEASURED_TOKENS_PER_CALL = 2_850
#: §5.5: two consecutive failures in one attempt open the breaker.
BREAKER_FAILURES = 2
#: The audit is bounded so a looping caller cannot grow the file without end.
MAX_AUDIT_ROWS = 200

LEDGER_VERSION = 1


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @classmethod
    def create(cls, path: Path, *, max_calls: int = MAX_CALLS_PER_TASK,
               max_input_tokens: int = MAX_INPUT_TOKENS_PER_TASK,
               monthly_cap_tokens: int | None = None, month_used_tokens: int = 0,
               calls: int = 0, input_tokens: int = 0) -> "Ledger":
        """The attempt's ledger. An existing one is kept, so registering the
        tool twice in one attempt does not refill the budget. `calls` and
        `input_tokens` carry an earlier attempt's spend on the same task,
        already charged to the month by that attempt: they count against the
        task's limits and are never settled again."""
        ledger = cls(path)
        ledger.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not ledger.path.exists():
            ledger._write({
                "version": LEDGER_VERSION,
                "max_calls": max_calls, "max_input_tokens": max_input_tokens,
                "monthly_cap_tokens": monthly_cap_tokens, "month_used_tokens": month_used_tokens,
                "calls": calls, "input_tokens": input_tokens,
                "settled_calls": calls, "settled_tokens": input_tokens,
                "consecutive_failures": 0, "breaker_open": False, "unavailable_logged": False,
                "audit": [],
            })
        return ledger

    def _write(self, state: Mapping[str, Any]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, separators=(",", ":"))
        os.replace(tmp, self.path)

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        lock = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            state = self.read()
            yield state
            self._write(state)
        finally:
            os.close(fd)  # closing releases the lock

    def read(self) -> dict[str, Any]:
        """The ledger, or a closed one when it cannot be read: an unreadable
        budget admits nothing rather than everything."""
        try:
            state = json.loads(self.path.read_text())
        except (OSError, ValueError):
            state = None
        if not isinstance(state, dict) or state.get("version") != LEDGER_VERSION:
            return {"version": LEDGER_VERSION, "max_calls": 0, "max_input_tokens": 0,
                    "monthly_cap_tokens": None, "month_used_tokens": 0, "calls": 0,
                    "input_tokens": 0, "settled_calls": 0, "settled_tokens": 0,
                    "consecutive_failures": 0,
                    "breaker_open": True, "unavailable_logged": True, "audit": []}
        return state

    def admit(self, *, estimate: int) -> str | None:
        """Reserve one call, or say why not: `breaker_open`, `budget_calls`,
        `budget_tokens` or `budget_monthly`."""
        with self._locked() as state:
            reason = _refusal(state, max(estimate, MEASURED_TOKENS_PER_CALL))
            if reason is None:
                state["calls"] = int(state["calls"]) + 1
            return reason

    def record(self, row: Mapping[str, Any], *, succeeded: bool | None) -> bool:
        """Add one audit row, its tokens, and its effect on the breaker
        (None: a skip, which is neither). True exactly once: when this row
        opened the breaker, so `jev_unavailable` is logged once per attempt."""
        with self._locked() as state:
            state["input_tokens"] = int(state["input_tokens"]) + int(row.get("input_tokens") or 0)
            audit = state.setdefault("audit", [])
            if len(audit) < MAX_AUDIT_ROWS:
                audit.append(dict(row))
            if succeeded is True:
                state["consecutive_failures"] = 0
            elif succeeded is False:
                state["consecutive_failures"] = int(state["consecutive_failures"]) + 1
                if state["consecutive_failures"] >= BREAKER_FAILURES:
                    state["breaker_open"] = True
            if state["breaker_open"] and not state["unavailable_logged"]:
                state["unavailable_logged"] = True
                return True
            return False

    def take_unsettled(self) -> int:
        """The tokens not yet charged to the month, marked charged."""
        with self._locked() as state:
            delta = int(state["input_tokens"]) - int(state.get("settled_tokens") or 0)
            if delta <= 0:
                return 0
            state["settled_tokens"] = int(state["input_tokens"])
            return delta

    def take_unsettled_attempt(self) -> tuple[int, int, list[dict[str, Any]]]:
        """`(calls, tokens, audit rows)` this attempt spent and has not yet
        settled, marked settled: what `settle_attempt` writes."""
        with self._locked() as state:
            calls = max(0, int(state["calls"]) - int(state.get("settled_calls") or 0))
            tokens = max(0, int(state["input_tokens"]) - int(state.get("settled_tokens") or 0))
            audit = state.get("audit") if isinstance(state.get("audit"), list) else []
            done = min(len(audit), max(0, int(state.get("settled_audit") or 0)))
            state["settled_calls"] = int(state.get("settled_calls") or 0) + calls
            state["settled_tokens"] = int(state.get("settled_tokens") or 0) + tokens
            state["settled_audit"] = len(audit)
            return calls, tokens, [row for row in audit[done:] if isinstance(row, dict)]

    def restore_unsettled(self, calls: int, tokens: int, rows: int = 0) -> None:
        """Undo `take_unsettled_attempt` after a settle that did not land."""
        with self._locked() as state:
            state["settled_calls"] = int(state.get("settled_calls") or 0) - calls
            state["settled_tokens"] = int(state.get("settled_tokens") or 0) - tokens
            state["settled_audit"] = max(0, int(state.get("settled_audit") or 0) - rows)


def _refusal(state: Mapping[str, Any], estimate: int) -> str | None:
    if state.get("breaker_open"):
        return "breaker_open"
    if int(state["calls"]) >= int(state["max_calls"]):
        return "budget_calls"
    spent = int(state["input_tokens"])
    if spent + estimate > int(state["max_input_tokens"]):
        return "budget_tokens"
    cap = state.get("monthly_cap_tokens")
    if cap is not None:
        unsettled = spent - int(state.get("settled_tokens") or 0)
        if int(state.get("month_used_tokens") or 0) + unsettled + estimate > int(cap):
            return "budget_monthly"
    return None


# -- the workspace's month -----------------------------------------------------


class MonthlyLedger(Protocol):
    def used(self, workspace: str, month: str) -> int: ...

    def add(self, workspace: str, month: str, tokens: int) -> None: ...


class InMemoryMonthlyLedger:
    def __init__(self) -> None:
        self.totals: dict[tuple[str, str], int] = {}

    def used(self, workspace: str, month: str) -> int:
        return self.totals.get((workspace, month), 0)

    def add(self, workspace: str, month: str, tokens: int) -> None:
        self.totals[(workspace, month)] = self.used(workspace, month) + tokens


#: One document per workspace and calendar month (UTC), `<tenant>_<YYYY-MM>`,
#: and one row per settled attempt, `<tenant>_<task>_g<generation>`.
MONTHLY_COLLECTION = "jev_usage"
#: The attempt rows' `kind`; the month documents carry none.
ATTEMPT_KIND = "attempt"
#: `by_kind`'s kind for calls and tokens no audit row kept, and for an audit
#: row naming no kind: swarm-api's own name for an unsplit row.
UNATTRIBUTED_KIND = "unattributed"
OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"
SKIPPED_PREFIX = "skipped:"


def month_of(now: datetime) -> str:
    """The calendar month (UTC) a use counts in, `YYYY-MM`."""
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def attempt_row_id(workspace: str, task_id: str, generation: int) -> str:
    return f"{workspace}_{task_id}_g{int(generation)}"


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _outcome(value: Any) -> str:
    """An audit row's outcome as `by_kind` counts it: `ok`, `skipped:<reason>`,
    or `failed` for every other (`failed`, `malformed`)."""
    if value == OUTCOME_OK or (isinstance(value, str) and value.startswith(SKIPPED_PREFIX)):
        return value
    return OUTCOME_FAILED


def split_by_kind(audit: list[Mapping[str, Any]], *, calls: int,
                  tokens: int) -> dict[str, dict[str, Any]]:
    """`{kind: {calls, input_tokens, output_tokens, outcomes}}` of one
    settle's audit rows. A skip was never admitted, so it is an outcome and
    not a call. What `calls` and `tokens` hold beyond the rows (the audit is
    bounded) is `unattributed`, with no outcomes: not recorded, not zero."""
    split: dict[str, dict[str, Any]] = {}
    for row in audit:
        kind = row.get("kind")
        kind = kind if isinstance(kind, str) and kind else UNATTRIBUTED_KIND
        entry = split.setdefault(kind, {"calls": 0, "input_tokens": 0, "output_tokens": None,
                                        "outcomes": {}})
        outcome = _outcome(row.get("outcome"))
        if not outcome.startswith(SKIPPED_PREFIX):
            entry["calls"] += 1
        entry["input_tokens"] += _count(row.get("input_tokens"))
        if "output_tokens" in row and row.get("output_tokens") is not None:
            entry["output_tokens"] = (entry["output_tokens"] or 0) + _count(row["output_tokens"])
        entry["outcomes"][outcome] = entry["outcomes"].get(outcome, 0) + 1
    rest_calls = calls - sum(entry["calls"] for entry in split.values())
    rest_tokens = tokens - sum(entry["input_tokens"] for entry in split.values())
    if rest_calls > 0 or rest_tokens > 0:
        entry = split.setdefault(UNATTRIBUTED_KIND, {"calls": 0, "input_tokens": 0,
                                                     "output_tokens": None})
        entry["calls"] += max(0, rest_calls)
        entry["input_tokens"] += max(0, rest_tokens)
    return split


def _merge_by_kind(before: Any, added: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The row's `by_kind` with one settle's split added, never replaced."""
    merged: dict[str, Any] = {kind: dict(value) for kind, value in
                              (before.items() if isinstance(before, Mapping) else ())
                              if isinstance(kind, str) and isinstance(value, Mapping)}
    for kind, value in added.items():
        old = merged.get(kind, {})
        entry: dict[str, Any] = {
            "calls": _count(old.get("calls")) + _count(value.get("calls")),
            "input_tokens": _count(old.get("input_tokens")) + _count(value.get("input_tokens")),
            "output_tokens": None,
        }
        for side in (old, value):
            if side.get("output_tokens") is not None:
                entry["output_tokens"] = (entry["output_tokens"] or 0) \
                    + _count(side.get("output_tokens"))
        old_outcomes, new_outcomes = old.get("outcomes"), value.get("outcomes")
        if isinstance(old_outcomes, Mapping) or isinstance(new_outcomes, Mapping):
            outcomes: dict[str, int] = {}
            for side in (old_outcomes, new_outcomes):
                for outcome, n in (side.items() if isinstance(side, Mapping) else ()):
                    if isinstance(outcome, str):
                        outcomes[outcome] = outcomes.get(outcome, 0) + _count(n)
            entry["outcomes"] = outcomes
        merged[kind] = entry
    return merged


class FirestoreMonthlyLedger:
    """The month's total in Firestore, added to in a transaction so two
    attempts settling at once both count.

    `runner` is `control.FirestoreTransactionRunner` (or the tests' fake):
    `run(fn)` calls `fn(transaction)` inside one transaction.
    """

    def __init__(self, db: Any, runner: Any) -> None:
        self._db = db
        self._runner = runner

    def _ref(self, workspace: str, month: str) -> Any:
        return self._db.collection(MONTHLY_COLLECTION).document(f"{workspace}_{month}")

    def used(self, workspace: str, month: str) -> int:
        snapshot = self._ref(workspace, month).get()
        data = snapshot.to_dict() if snapshot.exists else None
        value = (data or {}).get("input_tokens")
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    def add(self, workspace: str, month: str, tokens: int) -> None:
        ref = self._ref(workspace, month)

        def _apply(txn: Any) -> None:
            snapshot = txn.get(ref)
            data = snapshot.to_dict() if snapshot.exists else None
            before = (data or {}).get("input_tokens")
            before = before if isinstance(before, int) and not isinstance(before, bool) else 0
            txn.set(ref, {"tenant_id": workspace, "month": month,
                          "input_tokens": before + tokens})

        self._runner.run(_apply)

    def task_spend(self, workspace: str, task_id: str) -> tuple[int, int]:
        """`(calls, input_tokens)` the task's settled attempts spent, from
        their rows: this tenant's only, as the rows name it."""
        from google.cloud.firestore_v1.base_query import FieldFilter  # lazy: grpc

        query = (self._db.collection(MONTHLY_COLLECTION)
                 .where(filter=FieldFilter("tenant_id", "==", workspace))
                 .where(filter=FieldFilter("task_id", "==", task_id))
                 .where(filter=FieldFilter("kind", "==", ATTEMPT_KIND)))
        calls = tokens = 0
        for snapshot in query.stream():
            data = snapshot.to_dict() or {}
            calls += _count(data.get("calls"))
            tokens += _count(data.get("input_tokens"))
        return calls, tokens

    def settle_attempt(self, workspace: str, month: str, *, task_id: str, generation: int,
                       calls: int, tokens: int,
                       fence: Callable[[Any], Any] | None = None,
                       audit: list[Mapping[str, Any]] | None = None,
                       settled_at: datetime | None = None) -> None:
        """In ONE transaction: `fence(txn)` first, which reads the task and
        its lease and raises for a superseded attempt (invariant 5), so a
        stale attempt writes nothing; then the month's total and the
        attempt's row, each added to, never replaced. The row's `by_kind` is
        summed from `audit`, this settle's audit rows (`split_by_kind`), and
        added to what an earlier settle of the attempt wrote; `settled_at`
        is this settle's time (UTC)."""
        when = (settled_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        month_ref = self._ref(workspace, month)
        row_ref = self._db.collection(MONTHLY_COLLECTION).document(
            attempt_row_id(workspace, task_id, generation))

        def _apply(txn: Any) -> None:
            if fence is not None:
                fence(txn)
            month_snap = txn.get(month_ref)
            row_snap = txn.get(row_ref)
            month_data = (month_snap.to_dict() if month_snap.exists else None) or {}
            row_data = (row_snap.to_dict() if row_snap.exists else None) or {}
            if tokens:
                txn.set(month_ref, {"tenant_id": workspace, "month": month,
                                    "input_tokens": _count(month_data.get("input_tokens"))
                                    + tokens})
            txn.set(row_ref, {"kind": ATTEMPT_KIND, "tenant_id": workspace,
                              "task_id": task_id, "generation": int(generation),
                              "month": month,
                              "calls": _count(row_data.get("calls")) + calls,
                              "input_tokens": _count(row_data.get("input_tokens")) + tokens,
                              "settled_at": when,
                              "by_kind": _merge_by_kind(
                                  row_data.get("by_kind"),
                                  split_by_kind(list(audit or ()), calls=calls,
                                                tokens=tokens))})

        self._runner.run(_apply)


def settle_attempt(ledger: Ledger, usage: FirestoreMonthlyLedger, *, workspace: str,
                   task_id: str, generation: int, month: str,
                   fence: Callable[[Any], Any] | None = None,
                   log: Callable[..., None] | None = None,
                   settled_at: datetime | None = None) -> tuple[int, int] | None:
    """Settle this attempt's not-yet-settled calls, tokens and audit rows
    into `jev_usage`: one row per attempt (with `by_kind` and `settled_at`)
    and the month's total, fenced. Returns what was settled, or None when
    the write did not land (a fence, an outage), which is logged by type and
    returned to the unsettled count, its audit rows with it. It never
    raises: accounting never fails the attempt."""
    calls, tokens, audit = ledger.take_unsettled_attempt()
    try:
        usage.settle_attempt(workspace, month, task_id=task_id, generation=generation,
                             calls=calls, tokens=tokens, fence=fence, audit=audit,
                             settled_at=settled_at)
    except Exception as exc:  # noqa: BLE001
        ledger.restore_unsettled(calls, tokens, len(audit))
        if log is not None:
            log("jev_settle_failed", error=type(exc).__name__, calls=calls, tokens=tokens)
        return None
    return calls, tokens


def settle(ledger: Ledger, monthly: MonthlyLedger, *, workspace: str, month: str,
           log: Callable[..., None] | None = None) -> int:
    """Charge the task's not-yet-charged tokens to the workspace's month, once.
    Returns what was charged. A failed charge is logged and returned to the
    unsettled count, so a later settle can retry it."""
    tokens = ledger.take_unsettled()
    if tokens <= 0:
        return 0
    try:
        monthly.add(workspace, month, tokens)
    except Exception as exc:  # accounting never fails the attempt
        with ledger._locked() as state:
            state["settled_tokens"] = int(state["settled_tokens"]) - tokens
        if log is not None:
            log("jev_settle_failed", error=type(exc).__name__, tokens=tokens)
        return 0
    return tokens
