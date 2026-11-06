"""Jev spend through the worker's state routes (lane AQ-JEV-ACCOUNTING, 2026-11-03).

WHY. A tenant switched to the state routes holds no Firestore client, so the
worker's direct settle (`agent_worker.judge.budget.settle_attempt`) never ran
for it: its Jev calls were not settled, and with a monthly cap set every call
was skipped, because an unread month counts as spent. And on either path the
ledger the cap was held to is a file the agent's user can edit, read once at
start, so two concurrent attempts could each spend the same remainder. These
three routes, beside the others in `attemptstate`, make swarm-api the
authority:

* `state/jev-reserve` (`reserve`), at admission. In ONE transaction, fenced
  (invariant 5): the task's spend so far (its earlier attempts' rows), the
  workspace's cap (`code_intelligence.jev_monthly_cap_tokens`, only as
  `judgment.trusted_block` lets a reader act on it), the month's spend and
  every other live reservation; it grants the attempt at most what is left
  of the task's 8 calls and 120k tokens and of the month, and holds it.
  Two attempts reserving at once are serialised by the transaction, so they
  cannot both take the last of the month. The body is the tuple alone: the
  worker names no number.
* `state/jev-usage` (`record_call`), one record per call, keyed by its
  `call_id`: the call's document, the attempt's row (`calls`,
  `input_tokens`, `by_kind`, as `jevusage` reads it), the month's total and
  the reservation's remainder, in one transaction. A record already stored
  is answered `replayed` and charges nothing. Not fenced on the task: a
  cancelled or superseded attempt's spend is still spend.
* `state/jev-settle` (`settle`), when the attempt ends however it ended:
  the outcome, the calls the worker counted beyond its records (charged
  under `unattributed`), and the reservation released. Once: a second settle
  is `replayed`.

RECONCILIATION. A worker that dies before settling leaves its reservation
held. The task's next attempt's reserve releases it and settles the dead
attempt's row as `abandoned`, its unreported spend UNKNOWN (null), never 0;
a reservation no call renews for RESERVATION_TTL is released by any reserve.

DOCUMENTS (collection `jev_usage`, the worker's):
* `<tenant>_<YYYY-MM>`: the month, `input_tokens` -- as before;
* `<tenant>_<task>_g<generation>`, `kind: attempt` -- as before, plus
  `reservation`, `over_grant` and `settlement`;
* `<tenant>_<task>_g<generation>_c<call_id>`, `kind: call`: one call;
* `<tenant>_reservations`, `kind: reservations`: `held`, each live
  reservation by `<task>_g<generation>`.
`jevusage` reads only `kind: attempt` rows, swarm-api's own `kind:
contract_review` rows (the submission review, `jevusage.record_contract_review`)
and month documents, so the two new kinds change no total it serves. The
submission review adds its tokens to the same month document's
`input_tokens`, so a reserve here admits against them too.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Literal, Mapping

from google.api_core import exceptions as gexc
from google.cloud import firestore
from pydantic import Field

from swarm_common.admission import _snapshot
from swarm_jev.client import MEASURED_TOKENS_PER_CALL

from .attemptstate import AttemptState, Call, _Tuple
from .judgment import settings_mac, trusted_block
from .jevusage import COLLECTION, cap_of
from .mergesettings import COLLECTION as WORKSPACE_SETTINGS

log = logging.getLogger(__name__)

#: The worker's per-task limits (`agent_worker.judge.budget.MAX_CALLS_PER_TASK`,
#: `MAX_INPUT_TOKENS_PER_TASK`): restated, the worker's image is not this
#: package; tests/unit/control_plane/test_jev_account.py holds them equal.
#: MEASURED_TOKENS_PER_CALL is imported from `swarm_jev.client`, where alone
#: it is stated (owner, 4 November 2026).
#: 9 calls and 150,000 tokens: today's 8 and 120,000 plus JC2's reserve of one
#: call and 30,000 tokens ON TOP (jev-contract.md, the owner's answer 8). The
#: worker's ledger keeps the reserve from `jev_decide` and J0-J5; the grant
#: only has to hold it.
MAX_CALLS_PER_TASK = 9
MAX_INPUT_TOKENS_PER_TASK = 150_000
#: How long a reservation is held without a call renewing it. 6 hours BECAUSE
#: a step's time limit is at most a few hours and every recorded call renews
#: it; past it the worker is gone, and a held remainder would starve the
#: workspace's other attempts of the month.
RESERVATION_TTL = timedelta(hours=6)
#: How many earlier attempt rows a reserve reads for the task's spend. A task
#: past it is refused a grant: an unread spend is not a free one.
MAX_GENERATIONS_READ = 256
ATTEMPT_KIND = "attempt"
CALL_KIND = "call"
RESERVATIONS_KIND = "reservations"
UNATTRIBUTED_KIND = "unattributed"
ABANDONED = "abandoned"
SKIPPED_PREFIX = "skipped:"

_IDENT = r"^[A-Za-z0-9_.-]{1,48}$"
_MODEL = r"^[A-Za-z0-9_.:@/+-]{1,128}$"


class JevReserveBody(_Tuple):
    """The tuple and nothing else: no allowance, cap or count from a worker."""


class JevCallBody(_Tuple):
    call_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,96}$")
    source: Literal["judgment", "quality_hook", "browser"]
    kind: str = Field(pattern=_IDENT)
    backend: str | None = Field(default=None, pattern=_IDENT)
    requested_model: str | None = Field(default=None, pattern=_MODEL)
    served_model: str | None = Field(default=None, pattern=_MODEL)
    input_tokens: int = Field(ge=0, le=10_000_000)
    output_tokens: int | None = Field(default=None, ge=0, le=10_000_000)
    tokens_estimated: bool = False
    outcome: str = Field(pattern=r"^(ok|failed|malformed|skipped:[a-z_]{1,48})$")


class JevSettleBody(_Tuple):
    outcome: Literal["succeeded", "failed", "parked", "cancelled", "retried", "crashed"]
    unrecorded_calls: int = Field(default=0, ge=0, le=10_000)
    unrecorded_tokens: int = Field(default=0, ge=0, le=100_000_000)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def month_of(now: datetime) -> str:
    return now.strftime("%Y-%m")


def row_id(tenant_id: str, task_id: str, generation: int) -> str:
    return f"{tenant_id}_{task_id}_g{int(generation)}"


def held_key(task_id: str, generation: int) -> str:
    return f"{task_id}_g{int(generation)}"


def _doc(snap: Any) -> dict[str, Any] | None:
    snap = _snapshot(snap)
    return (snap.to_dict() or {}) if snap.exists else None


def _add_kind(by_kind: Any, kind: str, *, calls: int, tokens: int, output: int | None,
              outcome: str | None) -> dict[str, Any]:
    """The row's `by_kind` with one call (or one unattributed remainder)
    added: `jevusage.row_kinds`'s shape. `output_tokens` stays None until a
    record states one."""
    merged = {k: dict(v) for k, v in (by_kind.items() if isinstance(by_kind, Mapping) else ())
              if isinstance(k, str) and isinstance(v, Mapping)}
    entry = merged.setdefault(kind, {"calls": 0, "input_tokens": 0, "output_tokens": None})
    entry["calls"] = _count(entry.get("calls")) + calls
    entry["input_tokens"] = _count(entry.get("input_tokens")) + tokens
    if output is not None:
        entry["output_tokens"] = _count(entry.get("output_tokens")) + output
    if outcome is not None:
        outcomes = dict(entry.get("outcomes") or {})
        outcomes[outcome] = _count(outcomes.get(outcome)) + 1
        entry["outcomes"] = outcomes
    return merged


def grant_for(*, task_calls: int, task_tokens: int, cap: int | None, month_used: int,
              others_held: int) -> tuple[int, int, str | None]:
    """`(calls, tokens, reason)` an attempt is granted: what is left of the
    task's limits, its tokens no more than what is left of the month after
    every other live reservation; nothing when that cannot pay for one call
    at its measured cost."""
    calls = max(0, MAX_CALLS_PER_TASK - task_calls)
    tokens = max(0, MAX_INPUT_TOKENS_PER_TASK - task_tokens)
    if calls == 0:
        return 0, 0, "budget_calls"
    reason = "budget_tokens"
    if cap is not None:
        left = max(0, cap - month_used - others_held)
        if left < tokens:
            tokens, reason = left, "budget_monthly"
    if tokens < MEASURED_TOKENS_PER_CALL:
        return 0, 0, reason
    return calls, tokens, None


class JevAccount:
    """The three routes' logic over one `AttemptState` (its transaction, its
    fences, its clock)."""

    def __init__(self, state: AttemptState) -> None:
        self._state = state

    def _ref(self, doc_id: str) -> Any:
        return self._state._ref(COLLECTION, doc_id)

    def _cap(self, settings_doc: Mapping[str, Any] | None, tenant_id: str) -> int | None:
        block = (settings_doc or {}).get("code_intelligence")
        trusted = trusted_block(tenant_id, "code_intelligence",
                                block if isinstance(block, Mapping) else None,
                                settings_mac(self._state._settings))
        return cap_of(trusted)

    # -- state/jev-reserve ---------------------------------------------------

    def reserve(self, call: Call) -> dict[str, Any]:
        attempt = call.attempt
        tenant, task_id, gen = attempt.tenant_id, attempt.task_id, attempt.generation
        now = self._state._now()
        month = month_of(now)
        own = held_key(task_id, gen)
        row_ref = self._ref(row_id(tenant, task_id, gen))
        held_ref = self._ref(f"{tenant}_reservations")
        month_ref = self._ref(f"{tenant}_{month}")
        settings_ref = self._state._ref(WORKSPACE_SETTINGS, tenant)

        def _apply(txn: Any) -> dict[str, Any]:
            self._state._fenced(txn, attempt)
            row = _doc(txn.get(row_ref)) or {}
            held_doc = _doc(txn.get(held_ref)) or {}
            month_doc = _doc(txn.get(month_ref)) or {}
            settings_doc = _doc(txn.get(settings_ref))
            first = max(1, gen - MAX_GENERATIONS_READ)
            earlier = {g: _doc(txn.get(self._ref(row_id(tenant, task_id, g))))
                       for g in range(first, gen)}
            stored = row.get("reservation")
            if isinstance(stored, Mapping) and "settlement" not in row:
                # A replay (a lost answer): the reservation as first granted.
                return {"ok": True, "replayed": True, **dict(stored)}
            held = {k: dict(v) for k, v in (held_doc.get("held") or {}).items()
                    if isinstance(k, str) and isinstance(v, Mapping)}
            released: list[str] = []
            abandoned: list[int] = []
            for key, entry in list(held.items()):
                earlier_gen = entry.get("generation")
                superseded = (entry.get("task_id") == task_id and isinstance(earlier_gen, int)
                              and earlier_gen < gen)
                expires = entry.get("expires_at")
                lapsed = isinstance(expires, datetime) and expires <= now
                if superseded or lapsed or key == own:
                    del held[key]
                    released.append(key)
                    if superseded and earlier_gen in earlier \
                            and earlier[earlier_gen] is not None \
                            and "settlement" not in earlier[earlier_gen]:
                        abandoned.append(earlier_gen)
            task_calls = sum(_count((d or {}).get("calls")) for d in earlier.values())
            task_tokens = sum(_count((d or {}).get("input_tokens")) for d in earlier.values())
            cap = self._cap(settings_doc, tenant)
            others = sum(_count(e.get("remaining_tokens")) for e in held.values())
            if first > 1:
                calls, tokens, reason = 0, 0, "budget_calls"
            else:
                calls, tokens, reason = grant_for(
                    task_calls=task_calls, task_tokens=task_tokens, cap=cap,
                    month_used=_count(month_doc.get("input_tokens")), others_held=others)
            reservation = {"task_calls": task_calls, "task_input_tokens": task_tokens,
                           "granted_calls": calls, "granted_tokens": tokens, "reason": reason,
                           "cap_tokens": cap, "month": month}
            if tokens > 0:
                held[own] = {"task_id": task_id, "generation": gen,
                             "attempt_id": attempt.attempt_id, "remaining_tokens": tokens,
                             "reserved_at": now, "expires_at": now + RESERVATION_TTL}
            for g in abandoned:
                txn.set(self._ref(row_id(tenant, task_id, g)), {
                    "settlement": {"outcome": ABANDONED, "at": now,
                                   "unreported_calls": None, "unreported_tokens": None}},
                    merge=True)
            txn.set(held_ref, {"kind": RESERVATIONS_KIND, "tenant_id": tenant, "held": held})
            txn.set(row_ref, {"kind": ATTEMPT_KIND, "tenant_id": tenant, "task_id": task_id,
                              "generation": gen, "month": month,
                              "calls": _count(row.get("calls")),
                              "input_tokens": _count(row.get("input_tokens")),
                              "reservation": reservation}, merge=True)
            return {"ok": True, "replayed": False, "released": released, **reservation}

        return self._state._run(_apply)

    # -- state/jev-usage -----------------------------------------------------

    def record_call(self, call: Call) -> dict[str, Any]:
        body: JevCallBody = call.body
        attempt = call.attempt
        tenant, task_id, gen = attempt.tenant_id, attempt.task_id, attempt.generation
        now = self._state._now()
        month = month_of(now)
        call_ref = self._ref(f"{row_id(tenant, task_id, gen)}_c{body.call_id}")
        row_ref = self._ref(row_id(tenant, task_id, gen))
        held_ref = self._ref(f"{tenant}_reservations")
        month_ref = self._ref(f"{tenant}_{month}")
        skipped = body.outcome.startswith(SKIPPED_PREFIX)
        calls = 0 if skipped else 1
        tokens = body.input_tokens

        def _apply(txn: Any) -> dict[str, Any]:
            self._state._attempt_doc_ok(txn, attempt)
            if _doc(txn.get(call_ref)) is not None:
                return {"ok": True, "replayed": True}
            row = _doc(txn.get(row_ref)) or {}
            held_doc = _doc(txn.get(held_ref))
            month_doc = _doc(txn.get(month_ref)) or {}
            txn.set(call_ref, {
                "kind": CALL_KIND, "tenant_id": tenant, "task_id": task_id, "generation": gen,
                "attempt_id": attempt.attempt_id, "month": month, "call_id": body.call_id,
                "source": body.source, "call_kind": body.kind, "backend": body.backend,
                "requested_model": body.requested_model, "served_model": body.served_model,
                "input_tokens": tokens, "output_tokens": body.output_tokens,
                "tokens_estimated": body.tokens_estimated, "outcome": body.outcome,
                "recorded_at": now})
            total_calls = _count(row.get("calls")) + calls
            total_tokens = _count(row.get("input_tokens")) + tokens
            update: dict[str, Any] = {
                "kind": ATTEMPT_KIND, "tenant_id": tenant, "task_id": task_id,
                "generation": gen, "month": row.get("month") or month,
                "calls": total_calls, "input_tokens": total_tokens, "settled_at": now,
                "by_kind": _add_kind(row.get("by_kind"), body.kind, calls=calls, tokens=tokens,
                                     output=body.output_tokens,
                                     outcome=body.outcome if body.outcome == "ok" or skipped
                                     else "failed")}
            reservation = row.get("reservation")
            over = False
            if isinstance(reservation, Mapping):
                limit_calls = _count(reservation.get("task_calls")) \
                    + _count(reservation.get("granted_calls"))
                limit_tokens = _count(reservation.get("task_input_tokens")) \
                    + _count(reservation.get("granted_tokens"))
                spent_calls = _count(reservation.get("task_calls")) + total_calls
                spent_tokens = _count(reservation.get("task_input_tokens")) + total_tokens
                if spent_calls > limit_calls or spent_tokens > limit_tokens:
                    # Recorded, never refused: spend is spend. Shown, never silent.
                    over = True
                    update["over_grant"] = {"calls": max(0, spent_calls - limit_calls),
                                            "input_tokens": max(0, spent_tokens - limit_tokens)}
            txn.set(row_ref, update, merge=True)
            if tokens:
                txn.set(month_ref, {"tenant_id": tenant, "month": month,
                                    "input_tokens": _count(month_doc.get("input_tokens"))
                                    + tokens}, merge=True)
            key = held_key(task_id, gen)
            held = dict((held_doc or {}).get("held") or {})
            if isinstance(held.get(key), Mapping):
                entry = dict(held[key])
                entry["remaining_tokens"] = max(0, _count(entry.get("remaining_tokens")) - tokens)
                entry["expires_at"] = now + RESERVATION_TTL
                held[key] = entry
                txn.set(held_ref, {"kind": RESERVATIONS_KIND, "tenant_id": tenant,
                                   "held": held})
            return {"ok": True, "replayed": False, "over_grant": over}

        return self._state._run(_apply)

    # -- state/jev-settle ----------------------------------------------------

    def settle(self, call: Call) -> dict[str, Any]:
        body: JevSettleBody = call.body
        attempt = call.attempt
        tenant, task_id, gen = attempt.tenant_id, attempt.task_id, attempt.generation
        now = self._state._now()
        month = month_of(now)
        row_ref = self._ref(row_id(tenant, task_id, gen))
        held_ref = self._ref(f"{tenant}_reservations")
        month_ref = self._ref(f"{tenant}_{month}")
        key = held_key(task_id, gen)

        def _apply(txn: Any) -> dict[str, Any]:
            self._state._attempt_doc_ok(txn, attempt)
            row = _doc(txn.get(row_ref)) or {}
            held_doc = _doc(txn.get(held_ref))
            month_doc = _doc(txn.get(month_ref)) or {}
            settled = row.get("settlement")
            if isinstance(settled, Mapping) and settled.get("outcome") != ABANDONED:
                return {"ok": True, "replayed": True, "outcome": settled.get("outcome")}
            held = dict((held_doc or {}).get("held") or {})
            entry = held.pop(key, None)
            released = _count(entry.get("remaining_tokens")) if isinstance(entry, Mapping) else 0
            if held_doc is not None:
                txn.set(held_ref, {"kind": RESERVATIONS_KIND, "tenant_id": tenant,
                                   "held": held})
            update: dict[str, Any] = {
                "kind": ATTEMPT_KIND, "tenant_id": tenant, "task_id": task_id,
                "generation": gen, "month": row.get("month") or month, "settled_at": now,
                "calls": _count(row.get("calls")) + body.unrecorded_calls,
                "input_tokens": _count(row.get("input_tokens")) + body.unrecorded_tokens,
                "settlement": {"outcome": body.outcome, "at": now,
                               "unrecorded_calls": body.unrecorded_calls,
                               "unrecorded_tokens": body.unrecorded_tokens,
                               "released_tokens": released}}
            if body.unrecorded_calls or body.unrecorded_tokens:
                update["by_kind"] = _add_kind(row.get("by_kind"), UNATTRIBUTED_KIND,
                                              calls=body.unrecorded_calls,
                                              tokens=body.unrecorded_tokens, output=None,
                                              outcome=None)
            txn.set(row_ref, update, merge=True)
            if body.unrecorded_tokens:
                txn.set(month_ref, {"tenant_id": tenant, "month": month,
                                    "input_tokens": _count(month_doc.get("input_tokens"))
                                    + body.unrecorded_tokens}, merge=True)
            return {"ok": True, "replayed": False, "outcome": body.outcome,
                    "released_tokens": released}

        return self._state._run(_apply)


# -- the control plane's re-ask (contractreask) ------------------------------

#: The re-ask's row beside the attempt's own: `<attempt row>_reask`. A row of
#: its own, `kind: attempt`, so `jevusage` counts it with every other row,
#: while the task's per-attempt limits (`reserve` reads `row_id` rows only)
#: never charge the worker's next attempt for what the control plane asked.
REASK_SUFFIX = "_reask"
REASK_SOURCE = "contract_reask"


def reask_row_id(tenant_id: str, task_id: str, generation: int) -> str:
    return f"{row_id(tenant_id, task_id, generation)}{REASK_SUFFIX}"


class ReaskLedger:
    """The re-ask's `swarm_jev.JevLedger` (the owner's answer, 4 November
    2026: the re-ask's calls are counted in the workspace's monthly
    `jev_usage` and admitted against `code_intelligence.jev_monthly_cap_tokens`).

    One call per ledger: a second is refused `budget_calls` (REASK_TRIES
    bounds the step). `admit` reads, in one transaction, the cap (only as
    `trusted_block` lets a reader act on it), the month's spend and every live
    reservation, and refuses `budget_monthly` when what is left after them
    cannot pay for the call at its estimate, never below its measured cost:
    the re-ask never spends a remainder the worker's attempts hold. `record`
    writes, in one transaction keyed by the call's `call_id`, the call's
    document, the re-ask's row and the month's total, as `record_call` does;
    a record already stored charges nothing."""

    def __init__(self, db: Any, settings: Any, now: Any, *, tenant_id: str, task_id: str,
                 generation: int, attempt_id: str | None) -> None:
        self._db = db
        self._settings = settings
        self._now = now
        self._tenant = tenant_id
        self._task = task_id
        self._gen = int(generation)
        self._attempt = attempt_id
        self.rows: list[dict[str, Any]] = []
        self.refused: str | None = None

    def _ref(self, collection: str, doc_id: str) -> Any:
        return self._db.collection(collection).document(doc_id)

    def _run(self, body: Any) -> Any:
        return firestore.transactional(body)(self._db.transaction())

    def admit(self, *, estimate: int) -> str | None:
        if self.rows:
            return "budget_calls"
        month = month_of(self._now())
        settings_ref = self._ref(WORKSPACE_SETTINGS, self._tenant)
        month_ref = self._ref(COLLECTION, f"{self._tenant}_{month}")
        held_ref = self._ref(COLLECTION, f"{self._tenant}_reservations")

        def _apply(txn: Any) -> str | None:
            settings_doc = _doc(txn.get(settings_ref)) or {}
            block = settings_doc.get("code_intelligence")
            cap = cap_of(trusted_block(self._tenant, "code_intelligence",
                                       block if isinstance(block, Mapping) else None,
                                       settings_mac(self._settings)))
            if cap is None:
                return None
            month_doc = _doc(txn.get(month_ref)) or {}
            held_doc = _doc(txn.get(held_ref)) or {}
            held = held_doc.get("held") if isinstance(held_doc.get("held"), Mapping) else {}
            others = sum(_count(e.get("remaining_tokens")) for e in held.values()
                         if isinstance(e, Mapping))
            left = cap - _count(month_doc.get("input_tokens")) - others
            return "budget_monthly" if left < max(_count(estimate), MEASURED_TOKENS_PER_CALL) \
                else None

        self.refused = self._run(_apply)
        return self.refused

    def record(self, row: dict[str, Any], *, succeeded: bool | None = None) -> bool:
        self.rows.append(dict(row))
        now = self._now()
        month = month_of(now)
        outcome = str(row.get("outcome") or "failed")
        skipped = outcome.startswith(SKIPPED_PREFIX)
        calls = 0 if skipped else 1
        tokens = _count(row.get("input_tokens"))
        output = row.get("output_tokens")
        output = _count(output) if isinstance(output, int) else None
        kind = str(row.get("kind") or UNATTRIBUTED_KIND)
        reask_id = reask_row_id(self._tenant, self._task, self._gen)
        call_ref = self._ref(COLLECTION, f"{reask_id}_c{row.get('call_id')}")
        row_ref = self._ref(COLLECTION, reask_id)
        month_ref = self._ref(COLLECTION, f"{self._tenant}_{month}")

        def _apply(txn: Any) -> None:
            if _doc(txn.get(call_ref)) is not None:
                return
            stored = _doc(txn.get(row_ref)) or {}
            month_doc = _doc(txn.get(month_ref)) or {}
            txn.set(call_ref, {
                "kind": CALL_KIND, "tenant_id": self._tenant, "task_id": self._task,
                "generation": self._gen, "attempt_id": self._attempt, "month": month,
                "call_id": row.get("call_id"), "source": REASK_SOURCE, "call_kind": kind,
                "backend": row.get("backend"), "requested_model": row.get("requested_model"),
                "served_model": row.get("served_model"), "input_tokens": tokens,
                "output_tokens": output, "tokens_estimated": bool(row.get("tokens_estimated")),
                "outcome": outcome, "recorded_at": now})
            txn.set(row_ref, {
                "kind": ATTEMPT_KIND, "tenant_id": self._tenant, "task_id": self._task,
                "generation": self._gen, "source": REASK_SOURCE,
                "month": stored.get("month") or month,
                "calls": _count(stored.get("calls")) + calls,
                "input_tokens": _count(stored.get("input_tokens")) + tokens,
                "settled_at": now,
                "by_kind": _add_kind(stored.get("by_kind"), kind, calls=calls, tokens=tokens,
                                     output=output,
                                     outcome=outcome if outcome == "ok" or skipped
                                     else "failed")}, merge=True)
            if tokens:
                txn.set(month_ref, {"tenant_id": self._tenant, "month": month,
                                    "input_tokens": _count(month_doc.get("input_tokens"))
                                    + tokens}, merge=True)

        try:
            self._run(_apply)
        except gexc.GoogleAPIError as exc:
            # The call was made: its verdict is kept, and the spend it could
            # not record is logged, never silent.
            log.warning("jev re-ask call=%s tenant=%s not recorded: %s", row.get("call_id"),
                        self._tenant, type(exc).__name__)
        return False
