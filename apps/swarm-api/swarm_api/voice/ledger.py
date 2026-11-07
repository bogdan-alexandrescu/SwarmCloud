"""Voice it's ledger: what a session and the person's day have spent and
reserved, against $100 and $300 (docs/design/voice-it.md §3.8, §6, §10 phase 3).

COMPUTED, NEVER COUNTED. Nothing here writes a total. Every figure is derived
each time it is read from facts other code already keeps:

  Talk          the session's seconds (`talk_seconds` when swarm-voice wrote
                it, else live_at -> ended_at, or -> now while live) at
                `gpt-live-1`'s per-minute price (`modelprices.per_minute_cost`)
  Planner       each turn's `usage` at the platform's `model_prices`
                (Opus 5.5 unless the turn names its model). A turn run on a
                Claude subscription account from the pool (`usage.billing ==
                "subscription"`, swarm-voice's planner, §3.6) has no measured
                price: its estimate is RESERVED, never spent and never $0, so
                it still counts against both caps, and `subscription_turns`
                says how many were not measured
  Work running  each `voice_starts` row's step budgets times 1.2 (the
                worker's guard kills a step at 120%, agent-budget.md §5),
                plus any follow-up allowance reserved with it, until the
                item it started ends
  Work finished the item's measured cost (every attempt's `cost_usd`), which
                REPLACES its reservation; an ended item whose attempts did
                not all report a cost keeps its reservation (null is not
                zero, and a ceiling is never swapped for a floor)
  Follow-ups    tasks carrying the project's tag (`voice:<project>`) that no
                start covers: fix rounds, merge steps the platform started;
                their recorded `cost_usd`

  Speech machine the own-speech machine's minutes from the start call to
                the stop, at the catalogue's price, per session
                (`speech_machine_lines`, own-speech §13): read by Voice it ›
                Telemetry; not yet one of LINES, so not against the caps

A second running total is the kind of mirrored value that has drifted here
before; the start rows hold only what was reserved for them, which is a fact
of the start, not a sum.

THE CAPS. `reserve_or_refuse` refuses a start when what is spent and reserved
plus the new reservation would pass $100 for the session or $300 for the
person's calendar day, in the time zone saved in `user_preferences` (UTC when
none is saved). The refusal names the line that binds. A total exactly AT a
cap is allowed: "pass" is strictly above. Turns run one at a time and a
person has one live session (sessions.py), so the check and the start it
admits cannot race.

THE DAY is computed from timestamps with the zone read now, never from the
`day` stored on a start, so a changed zone moves every line to the day it
falls on. A session's talk counts on the day it started; a turn, a start and
a follow-up each on the day it was created.

PER-TENANT ISOLATION (invariant 9). Every read is under the workspace the
caller resolves to and the caller's own projects: projects are listed by
`tenant_id ==` and `person ==`, starts by `tenant_id ==` and `project_id ==`,
tasks, workflows and runs are read only when their `tenant_id` is the
caller's. A session under another tenant's project, or another person's, is
the same 404 as a missing one. The day covers the person's projects in the
selected workspace only.

NOT COUNTED, and said in every answer: pod compute for lanes, CI runner
minutes and deploys. No SwarmCloud cost figure includes them today (§3.8).
"""

from __future__ import annotations

import logging
import math
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

from google.cloud.firestore_v1.base_query import FieldFilter

from .. import modelprices
from ..errors import Conflict, NotFound, ValidationFailed
from . import sessions as vs
from . import speech_catalogue as cat
from . import speech_machine as sm
from . import store as vstore

log = logging.getLogger(__name__)

#: BGD's caps (§1 decision record): $100 a session, $300 a day.
SESSION_CAP_USD = 100.0
DAY_CAP_USD = 300.0
#: The worker's guard kills a step at 120% of its budget (agent-budget.md §5),
#: so 120% of a step's budget is the most it can spend.
RESERVE_FACTOR = 1.2
#: The live voice model and the planner's model (§3.8).
TALK_MODEL = "gpt-live-1"
PLANNER_MODEL = "claude-opus-5-5"
#: `usage.billing` of a turn run on a pool subscription account (§3.6).
BILLING = "billing"
SUBSCRIPTION = "subscription"
#: The largest one step's budget the ledger accepts: far above any lane, far
#: below a figure that would make the arithmetic meaningless.
MAX_STEP_USD = 1_000.0
#: The most steps one start reserves (a twenty-lane wave is well inside it).
MAX_STEPS = 200

TASKS = "tasks"
WORKFLOWS = "workflows"
RUNS = "issue_runs"
#: Task (and workflow) states that end the item (swarm_common.states).
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED"})

SESSION = "session"
DAY = "day"
LINES = ("talk", "planner", "work_running", "work_finished", "followups")
NOT_COUNTED = ("pod compute for lanes", "CI runner minutes", "deploys")

#: `detail.reason` of a refusal; the code is the established `conflict`
#: (tests/unit/control_plane/test_new_refusals_ship_off.py).
SESSION_CAP_REACHED = "voice_session_cap"
DAY_CAP_REACHED = "voice_day_cap"

#: Why a session's Speech machine figure is null: a stop whose machine had
#: no catalogue price (null is not zero, as for an unmeasured attempt).
MACHINE_UNPRICED = "machine_unpriced"
#: `in` takes at most 30 values per query.
_IN_MAX = 30


class VoiceCapReached(Conflict):
    pass


def _usd(value: float) -> float:
    return round(float(value), 6)


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def zone(name: Any) -> zoneinfo.ZoneInfo:
    """The saved zone, or UTC when none is saved (or it is no longer known)."""
    if isinstance(name, str) and name:
        try:
            return zoneinfo.ZoneInfo(name)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            log.warning("voice ledger: saved time zone is unknown; using UTC")
    return zoneinfo.ZoneInfo("UTC")


def day_of(when: Any, tz: zoneinfo.ZoneInfo) -> str | None:
    at = _aware(when)
    return at.astimezone(tz).date().isoformat() if at else None


# --------------------------------------------------------------------------
# The work Voice it started
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkState:
    """One started item as the platform holds it now."""

    ended: bool
    #: Every attempt's `cost_usd`, summed, when every attempt reported one
    #: (0.0 for an item that never ran); None when any did not or the read failed.
    cost_usd: float | None
    task_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaggedTask:
    task_id: str
    created_at: datetime | None
    #: Every recorded `cost_usd` of its attempts (a floor while it runs).
    cost_usd: float


class WorkReader(Protocol):
    def state(self, tenant_id: str, kind: str, target_id: str) -> WorkState: ...

    def tagged(self, tenant_id: str, tag: str) -> list[TaggedTask]: ...


class FirestoreWork:
    """`WorkReader` over the platform's documents, each read only in the
    caller's tenant. A document in another tenant, or missing, is an item
    that has not ended and has no measured cost: its reservation holds."""

    def __init__(self, db: Any) -> None:
        self._db = db

    def _doc(self, collection: str, doc_id: str, tenant_id: str) -> dict[str, Any] | None:
        if not doc_id:
            return None
        snap = self._db.collection(collection).document(doc_id).get()
        data = snap.to_dict() if snap.exists else None
        return data if data and data.get("tenant_id") == tenant_id else None

    def _workflow_tasks(self, data: Mapping[str, Any] | None) -> list[str]:
        steps = (data or {}).get("steps") or []
        return [str(s["task_id"]) for s in steps if isinstance(s, Mapping) and s.get("task_id")]

    def _measured(self, tenant_id: str, task_ids: Sequence[str]) -> float | None:
        from ..attempt_totals import totals_for

        if not task_ids:
            return 0.0
        totals = totals_for(self._db, tenant_id, list(task_ids))
        cost = 0.0
        for task_id in task_ids:
            row = totals.get(task_id) or {}
            if row.get("attempts_read") != "ok" or row.get("cost_incomplete"):
                return None
            cost += float(row.get("cost_usd_total") or 0.0)
        return cost

    def state(self, tenant_id: str, kind: str, target_id: str) -> WorkState:
        if kind == "agent":
            task = self._doc(TASKS, target_id, tenant_id)
            ended = bool(task) and str((task or {}).get("state")) in TERMINAL
            ids: list[str] = [target_id] if task else []
        elif kind == "workflow":
            workflow = self._doc(WORKFLOWS, target_id, tenant_id)
            ended = bool(workflow) and str((workflow or {}).get("state")) in TERMINAL
            ids = self._workflow_tasks(workflow)
        elif kind == "run":
            from ..issueruns import TERMINAL_RUN_STATES

            run = self._doc(RUNS, target_id, tenant_id)
            terminal = {s.value for s in TERMINAL_RUN_STATES}
            ended = bool(run) and str((run or {}).get("state")) in terminal
            ids = [str(run["planner_task_id"])] if run and run.get("planner_task_id") else []
            if run and run.get("workflow_id"):
                ids += self._workflow_tasks(self._doc(WORKFLOWS, str(run["workflow_id"]), tenant_id))
        else:
            return WorkState(ended=False, cost_usd=None)
        ids = list(dict.fromkeys(ids))
        return WorkState(ended=ended, cost_usd=self._measured(tenant_id, ids) if ended else None,
                         task_ids=tuple(ids))

    def tagged(self, tenant_id: str, tag: str) -> list[TaggedTask]:
        from ..attempt_totals import totals_for

        rows = [(snap.id, snap.to_dict() or {}) for snap in (
            self._db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("metadata.claude_session", "==", tag))
            .stream())]
        rows = [(i, d) for i, d in rows if d.get("tenant_id") == tenant_id]
        totals = totals_for(self._db, tenant_id, [i for i, _ in rows])
        return [TaggedTask(task_id=i, created_at=_aware(d.get("created_at")),
                           cost_usd=float((totals.get(i) or {}).get("cost_usd_total") or 0.0))
                for i, d in rows]


# --------------------------------------------------------------------------
# Pricing one fact
# --------------------------------------------------------------------------


def talk_seconds(session: Mapping[str, Any], now: datetime) -> float:
    """swarm-voice's reported seconds when it wrote them, else the session's
    live span: live_at to ended_at, or to now while it is live."""
    reported = session.get("talk_seconds")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool) and math.isfinite(reported):
        return max(float(reported), 0.0)
    live_at = _aware(session.get("live_at"))
    if live_at is None:
        return 0.0
    end = _aware(session.get("ended_at"))
    if end is None:
        end = now if session.get("state") == vs.LIVE else live_at
    return max((end - live_at).total_seconds(), 0.0)


def turn_cost(usage: Any, prices: Mapping[str, Mapping[str, Any]]) -> float | None:
    """A turn's planner cost: `usage` is either the counts of one model
    (`input_tokens`, `output_tokens`, `cache_read_input_tokens`,
    `cache_creation_input_tokens`, optionally `model`) or `{"by_model":
    {model: counts}}`. None when a model has no price; 0.0 with no usage."""
    if not isinstance(usage, Mapping) or not usage:
        return 0.0
    usage = {k: v for k, v in usage.items() if k != BILLING}
    by_model = usage.get("by_model")
    if not isinstance(by_model, Mapping):
        model = usage.get("model") if isinstance(usage.get("model"), str) else PLANNER_MODEL
        by_model = {model: usage}
    if not by_model:
        return 0.0
    return modelprices.estimate_cost(by_model, prices)


def step_budgets(steps: Any) -> list[float]:
    """Each step's budget in USD: `{"usd": n}` or `{"budget": {"usd": n}}`.
    A step with no USD budget is refused: the planner puts one on every step
    (§3.8), and a ceiling the ledger cannot price is no ceiling."""
    if not isinstance(steps, (list, tuple)) or not steps or len(steps) > MAX_STEPS:
        raise ValidationFailed(f"steps is a list of 1 to {MAX_STEPS} step budgets",
                               detail={"field": "steps"})
    out: list[float] = []
    for n, step in enumerate(steps):
        budget = step.get("budget", step) if isinstance(step, Mapping) else None
        usd = budget.get("usd") if isinstance(budget, Mapping) else None
        if (isinstance(usd, bool) or not isinstance(usd, (int, float)) or not math.isfinite(usd)
                or usd <= 0 or usd > MAX_STEP_USD):
            raise ValidationFailed(
                f"step {n} has no USD budget the ledger can reserve (0 < usd <= {MAX_STEP_USD:g})",
                detail={"field": "steps", "index": n})
        out.append(float(usd))
    return out


def reservation_of(reservation: Any) -> float:
    """What a start reserved, recomputed from its step budgets: their sum
    times 1.2, plus its follow-up allowance. A start with no steps recorded
    falls back to its stored `reserved_usd`."""
    if not isinstance(reservation, Mapping):
        return 0.0
    allowance = reservation.get("allowance_usd")
    allowance = float(allowance) if isinstance(allowance, (int, float)) and not isinstance(
        allowance, bool) and allowance > 0 else 0.0
    steps = reservation.get("steps")
    if isinstance(steps, list) and steps:
        try:
            return sum(step_budgets(steps)) * RESERVE_FACTOR + allowance
        except ValidationFailed:
            pass
    stored = reservation.get("reserved_usd")
    if isinstance(stored, (int, float)) and not isinstance(stored, bool) and stored > 0:
        return float(stored)
    return allowance


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------


class _Totals:
    def __init__(self, cap: float) -> None:
        self.cap = cap
        self.lines = {name: {"spent_usd": 0.0, "reserved_usd": 0.0} for name in LINES}
        self.notes = {"unpriced_turns": 0, "subscription_turns": 0, "unmeasured_items": 0, "running_items": 0,
                      "finished_items": 0, "followup_tasks": 0}

    def add(self, line: str, *, spent: float = 0.0, reserved: float = 0.0) -> None:
        self.lines[line]["spent_usd"] += spent
        self.lines[line]["reserved_usd"] += reserved

    @property
    def spent(self) -> float:
        return sum(v["spent_usd"] for v in self.lines.values())

    @property
    def reserved(self) -> float:
        return sum(v["reserved_usd"] for v in self.lines.values())

    def view(self) -> dict[str, Any]:
        spent, reserved = _usd(self.spent), _usd(self.reserved)
        total = _usd(spent + reserved)
        return {
            "cap_usd": self.cap,
            "spent_usd": spent,
            "reserved_usd": reserved,
            "total_usd": total,
            "remaining_usd": _usd(max(self.cap - total, 0.0)),
            "lines": [{"line": name, "spent_usd": _usd(v["spent_usd"]),
                       "reserved_usd": _usd(v["reserved_usd"])} for name, v in self.lines.items()],
            **self.notes,
        }


class VoiceLedger:
    """One person's Voice it spend in one tenant, computed on every read."""

    def __init__(self, db: Any, *, now: Callable[[], datetime],
                 prices: Callable[[], Mapping[str, Mapping[str, Any]]],
                 time_zone: Callable[[str], str | None],
                 work: WorkReader | None = None) -> None:
        self._db = db
        self._now = now
        self._prices = prices
        self._time_zone = time_zone
        self._work = work if work is not None else FirestoreWork(db)

    # ---- reads -----------------------------------------------------------

    def _projects(self, tenant_id: str, person: str) -> list[tuple[str, dict[str, Any]]]:
        rows = (self._db.collection(vs.PROJECTS)
                .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                .where(filter=FieldFilter("person", "==", person))
                .stream())
        return [(snap.id, snap.to_dict() or {}) for snap in rows
                if vstore.VoiceStore._mine(snap.to_dict(), tenant_id, person)]

    def _children(self, project_id: str, collection: str, tenant_id: str,
                  person: str) -> list[tuple[str, dict[str, Any]]]:
        rows = self._db.collection(vs.PROJECTS).document(project_id).collection(collection).stream()
        return [(snap.id, snap.to_dict() or {}) for snap in rows
                if vstore.VoiceStore._mine(snap.to_dict(), tenant_id, person)]

    def _starts(self, project_id: str, tenant_id: str, person: str) -> list[tuple[str, dict[str, Any]]]:
        rows = (self._db.collection(vstore.STARTS)
                .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                .where(filter=FieldFilter("project_id", "==", project_id))
                .stream())
        return [(snap.id, snap.to_dict() or {}) for snap in rows
                if vstore.VoiceStore._mine(snap.to_dict(), tenant_id, person)]

    # ---- the computation -------------------------------------------------

    def compute(self, *, tenant_id: str, person: str, session_id: str) -> dict[str, Any]:
        """The session's and the day's lines. The same 404 for another
        tenant's session, another person's, and a missing one."""
        vstore.check_id(session_id, "vs_", "session_id")
        now = self._now()
        zone_name = self._time_zone(person)
        tz = zone(zone_name)
        today = day_of(now, tz)
        prices = self._prices()
        session = _Totals(SESSION_CAP_USD)
        day = _Totals(DAY_CAP_USD)
        found = False

        for project_id, _project in self._projects(tenant_id, person):
            sessions = self._children(project_id, vs.SESSIONS, tenant_id, person)
            found = found or any(sid == session_id for sid, _ in sessions)
            for sid, doc in sessions:
                cost = modelprices.per_minute_cost(TALK_MODEL, talk_seconds(doc, now)) or 0.0
                if sid == session_id:
                    session.add("talk", spent=cost)
                if day_of(doc.get("created_at"), tz) == today:
                    day.add("talk", spent=cost)

            for _tid, turn in self._children(project_id, vstore.TURNS, tenant_id, person):
                cost = turn_cost(turn.get("usage"), prices)
                usage = turn.get("usage")
                subscription = isinstance(usage, Mapping) and usage.get(BILLING) == SUBSCRIPTION
                mine = turn.get("session_id") == session_id
                today_turn = day_of(turn.get("created_at"), tz) == today
                for totals, counts in ((session, mine), (day, today_turn)):
                    if not counts:
                        continue
                    if cost is None:
                        totals.notes["unpriced_turns"] += 1
                    elif subscription:
                        # Not measured: the estimate holds against the cap.
                        totals.add("planner", reserved=cost)
                        totals.notes["subscription_turns"] += 1
                    else:
                        totals.add("planner", spent=cost)

            starts = self._starts(project_id, tenant_id, person)
            covered: set[str] = set()
            for _sid, start in starts:
                state = self._work.state(tenant_id, str(start.get("kind")), str(start.get("target_id")))
                covered.update(state.task_ids)
                reserved = reservation_of(start.get("reservation"))
                targets = []
                if start.get("session_id") == session_id:
                    targets.append(session)
                if day_of(start.get("at"), tz) == today:
                    targets.append(day)
                for totals in targets:
                    if not state.ended:
                        totals.add("work_running", reserved=reserved)
                        totals.notes["running_items"] += 1
                    elif state.cost_usd is None:
                        # Ended, but not every attempt reported a cost: the
                        # ceiling holds rather than a floor taking its place.
                        totals.add("work_finished", reserved=reserved)
                        totals.notes["unmeasured_items"] += 1
                    else:
                        totals.add("work_finished", spent=state.cost_usd)
                        totals.notes["finished_items"] += 1

            tag = f"voice:{project_id}"
            ordered = sorted(((_aware(s.get("at")), s.get("session_id")) for _, s in starts),
                             key=lambda pair: pair[0] or datetime.min.replace(tzinfo=timezone.utc))
            for task in self._work.tagged(tenant_id, tag):
                if task.task_id in covered:
                    continue
                owner = _follows(ordered, task.created_at)
                if owner == session_id:
                    session.add("followups", spent=task.cost_usd)
                    session.notes["followup_tasks"] += 1
                if day_of(task.created_at, tz) == today:
                    day.add("followups", spent=task.cost_usd)
                    day.notes["followup_tasks"] += 1

        if not found:
            raise NotFound(f"voice session {session_id!r} not found")
        return {
            "session_id": session_id,
            "date": today,
            "time_zone": zone_name or "UTC",
            "computed_at": now.isoformat(),
            SESSION: session.view(),
            DAY: day.view(),
            "not_counted": list(NOT_COUNTED),
        }

    # ---- the gate --------------------------------------------------------

    def reserve_or_refuse(self, *, tenant_id: str, person: str, session_id: str,
                          steps: Any, allowance_usd: float = 0.0) -> dict[str, Any]:
        """The reservation for a start of `steps` (each with a USD budget),
        to pass to `VoiceStore.record_start`; or `VoiceCapReached` naming the
        cap the start would pass. Nothing is written: the start row the
        caller records is what later reads count."""
        budgets = step_budgets(steps)
        if (isinstance(allowance_usd, bool) or not isinstance(allowance_usd, (int, float))
                or not math.isfinite(allowance_usd) or allowance_usd < 0
                or allowance_usd > MAX_STEP_USD):
            raise ValidationFailed("allowance_usd is a USD amount", detail={"field": "allowance_usd"})
        reservation = {
            "steps": [{"usd": b} for b in budgets],
            "budget_usd": _usd(sum(budgets)),
            "allowance_usd": _usd(allowance_usd),
            "reserved_usd": _usd(sum(budgets) * RESERVE_FACTOR + allowance_usd),
        }
        ledger = self.compute(tenant_id=tenant_id, person=person, session_id=session_id)
        new = reservation["reserved_usd"]
        for key, reason, what in ((SESSION, SESSION_CAP_REACHED, "this session"),
                                  (DAY, DAY_CAP_REACHED, f"your day ({ledger['date']}, {ledger['time_zone']})")):
            cap = ledger[key]
            after = _usd(cap["total_usd"] + new)
            if after > cap["cap_usd"]:
                raise VoiceCapReached(
                    f"Refused: this start reserves ${new:.2f} and {what} has "
                    f"${cap['spent_usd']:.2f} spent and ${cap['reserved_usd']:.2f} reserved, "
                    f"so it would reach ${after:.2f}, past the ${cap['cap_usd']:.0f} cap. "
                    "Nothing was started.",
                    detail={"reason": reason, "line": key, "cap_usd": cap["cap_usd"],
                            "spent_usd": cap["spent_usd"], "reserved_usd": cap["reserved_usd"],
                            "requested_usd": new, "would_be_usd": after})
        return {**reservation, "day": ledger["date"]}


def _follows(starts: Sequence[tuple[datetime | None, Any]], created_at: datetime | None) -> Any:
    """The session of the latest start at or before `created_at`: the work a
    follow-up follows. None when no start precedes it."""
    owner = None
    for at, session_id in starts:
        if at is not None and created_at is not None and at <= created_at:
            owner = session_id
    return owner



def speech_machine_lines(db: Any, sessions: Mapping[str, str], *,
                         now: Callable[[], datetime]) -> dict[str, dict[str, Any]]:
    """The Speech machine line (docs/design/voice-it-own-speech.md §13) of
    each session in `sessions` ({session_id: tenant_id}): the machine's
    minutes from the start call to the stop, at the catalogue's price, for
    the session that held the lease.

    A finished start is its stop's `voice_audit` row, which
    `SpeechMachine.stop` priced from the catalogue when it wrote it; the
    start still holding the lease is its minutes from `started_at` to now at
    the same catalogue's price. A row under another tenant is not this
    session's. `{usd, minutes, reason}` per session: `usd` is None with
    `reason` MACHINE_UNPRICED when a stop carries no price. A read that fails
    raises: the caller says the ledger could not be read."""
    lines = {sid: {"usd": 0.0, "minutes": 0.0, "reason": None} for sid in sessions}

    def add(line: dict[str, Any], minutes: Any, usd: Any) -> None:
        line["minutes"] = round(line["minutes"] + float(minutes or 0.0), 3)
        if line["reason"] is not None:
            return
        if not isinstance(usd, (int, float)) or isinstance(usd, bool):
            line.update(usd=None, reason=MACHINE_UNPRICED)
        else:
            line["usd"] = round(line["usd"] + float(usd), 4)

    ids = sorted(lines)
    for i in range(0, len(ids), _IN_MAX):
        query = (db.collection(vs.AUDIT)
                 .where(filter=FieldFilter("session_id", "in", ids[i:i + _IN_MAX]))
                 .where(filter=FieldFilter("action", "==", sm.AUDIT_STOPPED)))
        for snap in query.stream():
            row = snap.to_dict() or {}
            sid = row.get("session_id")
            if sid not in lines or row.get("tenant_id") != sessions[sid]:
                continue
            detail = row.get("detail") or {}
            add(lines[sid], detail.get("minutes"), detail.get("usd"))
    snap = db.collection(sm.COLLECTION).document(sm.LEASE).get()
    lease = (snap.to_dict() or {}) if getattr(snap, "exists", False) else {}
    sid = lease.get("session_id")
    started = _aware(lease.get("started_at"))
    # STOPPING: the stop has not written its row yet, so the lease still counts.
    if sid in lines and lease.get("tenant_id") == sessions[sid] and started is not None \
            and lease.get("state") in (sm.HOLDING | {sm.STOPPING}):
        minutes = round(max(0.0, (now() - started).total_seconds()) / 60.0, 3)
        machine = cat.machine(str(lease.get("machine") or "")) \
            or cat.machine(cat.DEFAULTS["machine"])
        add(lines[sid], minutes,
            round(minutes / 60.0 * machine.usd_per_hour, 4) if machine else None)
    return lines


def speech_machine_stops(db: Any, *, start: datetime, end: datetime,
                         tenant_id: str | None) -> list[dict[str, Any]]:
    """The Speech machine line's finished starts stopped in [start, end),
    under one tenant or (None) every tenant: `{session_id, tenant_id, at,
    minutes, usd}` from each stop's `voice_audit` row, `usd` None when the
    stop carries no price. Read by `at` alone (a single-field index) and
    filtered here, so no composite index. A read that fails raises."""
    query = (db.collection(vs.AUDIT)
             .where(filter=FieldFilter("at", ">=", start))
             .where(filter=FieldFilter("at", "<", end)))
    stops = []
    for snap in query.stream():
        row = snap.to_dict() or {}
        at = _aware(row.get("at"))
        if row.get("action") != sm.AUDIT_STOPPED or at is None \
                or (tenant_id is not None and row.get("tenant_id") != tenant_id):
            continue
        detail = row.get("detail") or {}
        usd = detail.get("usd")
        stops.append({"session_id": row.get("session_id"), "tenant_id": row.get("tenant_id"),
                      "at": at, "minutes": detail.get("minutes"),
                      "usd": float(usd) if isinstance(usd, (int, float))
                      and not isinstance(usd, bool) else None})
    return stops
