"""Jev's usage and cost, per workspace and day (JEV-USAGE-PAGE, part of #2688).

The owner chose on 2 November 2026 to build a Jev usage and cost page: no one
could see how many calls, tokens or dollars Jev uses, and its price was not
recorded anywhere. This module is the read behind it; `routes/jev_usage.py`
serves it and the console's Capacity › Jev usage draws it.

WHAT IT SUMS. The worker settles each attempt's Jev spend into `jev_usage`
(`agent_worker.judge.budget.FirestoreMonthlyLedger.settle_attempt`, lane
JEV-JUDGMENTS-RUN): one row per attempt, `<tenant>_<task>_g<generation>`,
`kind: attempt`, with its `calls` and `input_tokens`, and one month document,
`<tenant>_<YYYY-MM>`, whose `input_tokens` the monthly cap is held to. An
attempt row may also carry, and this read sums when it does:

* `settled_at` -- when the attempt settled (UTC). Without it, the row's own
  last write time (Firestore's `update_time`), which is the settle's;
* `by_kind` -- `{kind: {calls, input_tokens, output_tokens, outcomes}}`, the
  attempt's calls split by who asked: J1-J3 and J5 are the worker's judgments
  (one kind per question), J4 the quality gate, `agent` the agent's decision
  tool, `browser` the Jev browser. `outcomes` counts `ok`, `failed` and
  `skipped:<reason>`, the outcome strings the worker's audit rows hold.

A row without `by_kind` is counted under kind `unattributed` with its calls
and input tokens and no outcomes: the worker's audit rows hold the split, but
they live in the attempt's own directory, and a row is never guessed apart.

DOLLARS ARE BLANK, NOT ZERO, WITHOUT A RATE. The rate is the platform setting
`jev_token_rates` (`{input, output}`, USD per million tokens, `store`), with
no default: nobody has recorded Jev's price, and an invented one would print
a figure. `usd` is None until the input rate is set, and stays None for any
figure holding output tokens while the output rate is not set. Output tokens
are None where no row recorded them, never 0.

PER-TENANT ISOLATION (invariant 9). Every query filters on `tenant_id`, and a
row whose own `tenant_id` is not the asked one is dropped again in code, so a
row filed under the wrong id cannot be shown. The route decides who may ask.

NO PAYLOAD. Only counts leave this module: the row's task id, generation and
every field not named above are never read into the answer, and a kind or a
skip reason that is not a short identifier is shown as `other`, so a stray
string in a record cannot reach the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from google.cloud.firestore_v1.base_query import FieldFilter

#: The worker's collection and its attempt rows' `kind`
#: (`agent_worker.judge.budget.MONTHLY_COLLECTION`, `ATTEMPT_KIND`).
COLLECTION = "jev_usage"
ATTEMPT_KIND = "attempt"
#: The page's window: a daily table and its total over the last 30 days, UTC.
WINDOW_DAYS = 30
#: The platform setting holding Jev's per-token rate (`store`).
RATE_SETTING = "jev_token_rates"
#: A row with no `by_kind` split.
UNATTRIBUTED = "unattributed"
#: A kind or a skip reason that is not a short identifier.
OTHER = "other"
#: The worker's call kinds, each with who asked it. A worker judgment is
#: shown by its question.
KIND_GROUPS: dict[str, str] = {
    "J0": "worker_judgment",
    "J1": "worker_judgment",
    "J2": "worker_judgment",
    "J3": "worker_judgment",
    "J5": "worker_judgment",
    "J4": "quality_gate",
    "agent": "decision_tool",
    "browser": "jev_browser",
    UNATTRIBUTED: UNATTRIBUTED,
}
OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"
SKIPPED_PREFIX = "skipped:"
_IDENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,39}$")


def _count(value: Any) -> int:
    """A non-negative integer count, 0 for anything else (a bool included)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _ident(value: Any) -> str:
    return value if isinstance(value, str) and _IDENT.match(value) else OTHER


def rates_of(value: Any) -> dict[str, float | None]:
    """`{input, output}` USD per million tokens; a missing or malformed one is None."""
    raw = value if isinstance(value, Mapping) else {}
    out: dict[str, float | None] = {}
    for unit in ("input", "output"):
        rate = raw.get(unit)
        ok = isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate >= 0 \
            and rate == rate and rate != float("inf")
        out[unit] = float(rate) if ok else None
    return out


def rate_problem(value: Any) -> str | None:
    """Why `value` is not a `jev_token_rates` setting, or None."""
    if not isinstance(value, Mapping):
        return "jev_token_rates is an object: {input, output}, USD per million tokens"
    unknown = sorted(set(value) - {"input", "output"})
    if unknown:
        return f"jev_token_rates has no field {unknown[0]!r}"
    parsed = rates_of(value)
    for unit in ("input", "output"):
        if value.get(unit) is not None and parsed[unit] is None:
            return f"jev_token_rates.{unit} is a non-negative number of USD per million tokens"
    return None


@dataclass
class Tally:
    """Calls, tokens and outcomes of one kind on one day, or of a total."""

    calls: int = 0
    input_tokens: int = 0
    #: None until a row records output tokens.
    output_tokens: int | None = None
    ok: int = 0
    failed: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def add(self, other: "Tally") -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        if other.output_tokens is not None:
            self.output_tokens = (self.output_tokens or 0) + other.output_tokens
        self.ok += other.ok
        self.failed += other.failed
        for reason, n in other.skipped.items():
            self.skipped[reason] = self.skipped.get(reason, 0) + n

    def usd(self, rates: Mapping[str, float | None]) -> float | None:
        """Dollars at `rates`, or None when a rate the figure needs is not set."""
        if rates.get("input") is None:
            return None
        total = self.input_tokens * float(rates["input"] or 0) / 1_000_000
        if self.output_tokens:
            if rates.get("output") is None:
                return None
            total += self.output_tokens * float(rates["output"] or 0) / 1_000_000
        return round(total, 6)

    def as_dict(self, rates: Mapping[str, float | None]) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "outcomes": {
                "ok": self.ok,
                "failed": self.failed,
                "skipped": sum(self.skipped.values()),
                "skipped_by_reason": dict(sorted(self.skipped.items())),
            },
            "usd": self.usd(rates),
        }


def _kind_tally(value: Any) -> Tally:
    raw = value if isinstance(value, Mapping) else {}
    tally = Tally(calls=_count(raw.get("calls")), input_tokens=_count(raw.get("input_tokens")))
    if "output_tokens" in raw and raw.get("output_tokens") is not None:
        tally.output_tokens = _count(raw.get("output_tokens"))
    outcomes = raw.get("outcomes")
    for outcome, n in (outcomes.items() if isinstance(outcomes, Mapping) else ()):
        n = _count(n)
        if not n or not isinstance(outcome, str):
            continue
        if outcome == OUTCOME_OK:
            tally.ok += n
        elif outcome == OUTCOME_FAILED:
            tally.failed += n
        elif outcome.startswith(SKIPPED_PREFIX):
            reason = _ident(outcome[len(SKIPPED_PREFIX):])
            tally.skipped[reason] = tally.skipped.get(reason, 0) + n
    return tally


def row_kinds(data: Mapping[str, Any]) -> dict[str, Tally]:
    """One attempt row's tallies by kind: its `by_kind` split, or the whole
    row as `unattributed`."""
    split = data.get("by_kind")
    if isinstance(split, Mapping) and split:
        out: dict[str, Tally] = {}
        for kind, value in split.items():
            out.setdefault(_ident(kind), Tally()).add(_kind_tally(value))
        return out
    return {UNATTRIBUTED: Tally(calls=_count(data.get("calls")),
                                input_tokens=_count(data.get("input_tokens")))}


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def row_day(data: Mapping[str, Any], update_time: Any = None) -> date | None:
    """The UTC day an attempt row counts on: its `settled_at`, else its last write."""
    when = _utc(data.get("settled_at")) or _utc(update_time)
    return when.astimezone(timezone.utc).date() if when else None


def window(now: datetime, days: int = WINDOW_DAYS) -> tuple[date, date]:
    """The first and last UTC day of the window ending today."""
    today = now.astimezone(timezone.utc).date()
    return today - timedelta(days=days - 1), today


def months_between(first: date, last: date) -> list[str]:
    out: list[str] = []
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


@dataclass(frozen=True)
class UsageRow:
    """One attempt row as read: its data and Firestore's last write time."""

    data: Mapping[str, Any]
    update_time: Any = None


def summarise(tenant_id: str, rows: Iterable[UsageRow], *, now: datetime,
              rates: Mapping[str, float | None], month_used_tokens: int,
              cap_tokens: int | None) -> dict[str, Any]:
    """The page's answer from `tenant_id`'s attempt rows."""
    first, last = window(now)
    by_day: dict[date, dict[str, Tally]] = {}
    undated = Tally()
    for row in rows:
        data = row.data
        if data.get("tenant_id") != tenant_id or data.get("kind") != ATTEMPT_KIND:
            continue
        day = row_day(data, row.update_time)
        kinds = row_kinds(data)
        if day is None:
            for tally in kinds.values():
                undated.add(tally)
            continue
        if not first <= day <= last:
            continue
        slot = by_day.setdefault(day, {})
        for kind, tally in kinds.items():
            slot.setdefault(kind, Tally()).add(tally)

    total = Tally()
    total_by_kind: dict[str, Tally] = {}
    days_out: list[dict[str, Any]] = []
    for day in sorted(by_day, reverse=True):
        day_total = Tally()
        kinds_out = []
        for kind in sorted(by_day[day]):
            tally = by_day[day][kind]
            day_total.add(tally)
            total_by_kind.setdefault(kind, Tally()).add(tally)
            kinds_out.append({"kind": kind, "group": KIND_GROUPS.get(kind, OTHER),
                              **tally.as_dict(rates)})
        total.add(day_total)
        days_out.append({"day": day.isoformat(), "kinds": kinds_out, **day_total.as_dict(rates)})

    month = now.astimezone(timezone.utc).strftime("%Y-%m")
    return {
        "tenant_id": tenant_id,
        "window": {"from": first.isoformat(), "to": last.isoformat(), "days": WINDOW_DAYS},
        "rate": {"input_usd_per_million": rates.get("input"),
                 "output_usd_per_million": rates.get("output"),
                 "set": rates.get("input") is not None},
        "days": days_out,
        "total": {**total.as_dict(rates),
                  "by_kind": [{"kind": kind, "group": KIND_GROUPS.get(kind, OTHER),
                               **total_by_kind[kind].as_dict(rates)}
                              for kind in sorted(total_by_kind)]},
        "undated": undated.as_dict(rates) if undated.calls or undated.input_tokens else None,
        "month": {
            "month": month,
            "used_tokens": month_used_tokens,
            "cap_tokens": cap_tokens,
            "used_fraction": (round(month_used_tokens / cap_tokens, 4)
                              if cap_tokens else None),
        },
    }


class JevUsageReader:
    """Reads one workspace's `jev_usage` rows and month document."""

    def __init__(self, db: Any) -> None:
        self._db = db

    def rows(self, tenant_id: str, months: Iterable[str]) -> list[UsageRow]:
        out: list[UsageRow] = []
        for month in months:
            # Equality filters only, so Firestore serves them from its
            # single-field indexes with no composite index to deploy.
            query = (self._db.collection(COLLECTION)
                     .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                     .where(filter=FieldFilter("kind", "==", ATTEMPT_KIND))
                     .where(filter=FieldFilter("month", "==", month)))
            for snap in query.stream():
                out.append(UsageRow(snap.to_dict() or {}, getattr(snap, "update_time", None)))
        return out

    def month_used(self, tenant_id: str, month: str) -> int:
        snap = self._db.collection(COLLECTION).document(f"{tenant_id}_{month}").get()
        data = (snap.to_dict() or {}) if snap.exists else {}
        if data.get("tenant_id") not in (None, tenant_id):
            return 0
        return _count(data.get("input_tokens"))

    def read(self, tenant_id: str, *, now: datetime, rates: Mapping[str, float | None],
             cap_tokens: int | None) -> dict[str, Any]:
        first, last = window(now)
        return summarise(
            tenant_id, self.rows(tenant_id, months_between(first, last)), now=now,
            rates=rates, month_used_tokens=self.month_used(tenant_id, last.strftime("%Y-%m")),
            cap_tokens=cap_tokens)


def cap_of(block: Mapping[str, Any] | None) -> int | None:
    """`code_intelligence.jev_monthly_cap_tokens` as the worker reads it
    (`agent_worker.judge.settings.JudgeSettings.from_mapping`): a
    non-negative integer, else no cap."""
    cap = (block or {}).get("jev_monthly_cap_tokens")
    return cap if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 0 else None
