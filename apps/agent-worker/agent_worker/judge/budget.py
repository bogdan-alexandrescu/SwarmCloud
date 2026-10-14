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
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
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
        `input_tokens` carry an earlier attempt's spend on the same task."""
        ledger = cls(path)
        ledger.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not ledger.path.exists():
            ledger._write({
                "version": LEDGER_VERSION,
                "max_calls": max_calls, "max_input_tokens": max_input_tokens,
                "monthly_cap_tokens": monthly_cap_tokens, "month_used_tokens": month_used_tokens,
                "calls": calls, "input_tokens": input_tokens, "settled_tokens": 0,
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
                    "input_tokens": 0, "settled_tokens": 0, "consecutive_failures": 0,
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


#: One document per workspace and calendar month (UTC), `<tenant>_<YYYY-MM>`.
MONTHLY_COLLECTION = "jev_usage"


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
