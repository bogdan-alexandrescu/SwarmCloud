"""Voice it's spend caps, enforced (docs/design/voice-it.md §3.8; the owner's
decision of 23 November 2026: the caps are enforced as designed).

THE SAME COMPUTATION. Every check reads `ledger.VoiceLedger`, which derives
each figure from the facts other code keeps, on every read. Nothing here
writes a total. What enforcement writes is what it DID, on the session
document's `budget` field (ledger.BUDGET_FIELD): when each cap was warned of,
when a cap was reached and when its grace ends, and the Budget card's id. A
second warning, a second grace or a second card is what that field prevents.

FOUR CHECKS.

  Start   `refuse_start`: a new session is refused, before anything is
          written, when the person's day is already at its $300 cap
          (spent plus reserved, at or over). 409 `conflict` with
          `detail.reason` voice_day_cap, the cap, what is spent and what is
          reserved: no session, no marker, no lease, no call to swarm-voice.
  Work    `admit_work`: swarm-voice asks before a delegation runs (`/work`
          `{delegation: {state: started}}`). A session already at a cap is
          refused (`voice_budget_capped`); otherwise the ledger's
          `reserve_or_refuse` holds one planner turn's estimate
          (ledger.DELEGATION_ESTIMATE_USD) against both caps and refuses
          when it would pass one (`voice_session_cap`, `voice_day_cap`).
          The refusal's reason is what the voice's one sentence is chosen by.
  Warn    `enforce`, crossing WARN_SHARE (80%) of the session cap or the day
          cap: once per session per cap, a notice for the voice to say at a
          gap (swarm-voice's updates.py) and a Budget card on the session.
  End     `enforce`, at a cap (total at or over it): nothing more starts, a
          notice for the voice, the Budget card says when the session ends,
          and CAP_GRACE_SECONDS later the session ends with the cause
          `budget_cap` through `VoiceSessions.end_for_budget`, the same end
          as the person's End (swarm-voice told, so billing stops; a speech
          session's machine stopped, its lease ending with END_BUDGET_CAP).

WHO RUNS `enforce`. swarm-voice's roster follower asks every tick (`/work`
`{budget: true}`), and every `/work` report runs it, so a live voice hears
its notices within seconds. The two-minute sweep
(routes/voice_speech.py, `POST /v1/internal/voice/speech/sweep`) runs it for
every live session too: with swarm-voice down, the grace still ends the
session.

RESERVATIONS COUNT. A cap is reached on spent PLUS reserved: running work at
its step budgets times 1.2, and subscription planner turns at their
estimate, which are reserved and never spent (ledger.py). So a session can
be capped while part of its total is a reservation that later shrinks to a
measured cost; it ends all the same, the conservative reading.

PER-TENANT ISOLATION (invariant 9). Every read is the ledger's, scoped to
the person's projects in the session's tenant, and the session document is
read only when its tenant and person are the caller's.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from google.cloud import firestore
from swarm_common.admission import _snapshot

from ..errors import NotFound, PreferencesUnavailable
from . import cards as vcards
from . import ledger as vl
from . import sessions as vs
from . import store as vstore

log = logging.getLogger(__name__)

#: A notice's kinds: a cap's 80% crossed, a cap reached.
WARNING = "warning"
CAPPED = "capped"
#: The Budget card's title by notice and cap (the catalogue's `budget` kind).
TITLES = {
    (WARNING, vl.SESSION): "80% of the session budget",
    (WARNING, vl.DAY): "80% of today's budget",
    (CAPPED, vl.SESSION): "Session budget reached",
    (CAPPED, vl.DAY): "Today's budget reached",
}
#: The ledger's line names as the card's table says them.
LINE_LABELS = {"talk": "Talk", "planner": "Planner", "speech_machine": "Speech machine",
               "work_running": "Work running", "work_finished": "Work finished",
               "followups": "Follow-ups"}


def _when(value: Any) -> datetime | None:
    return vl._aware(value)  # noqa: SLF001 -- the ledger's own reading of a timestamp


def saved_zone(read: Callable[[str], Any]) -> Callable[[str], str | None]:
    """The ledger's `time_zone` for enforcement: the saved zone, or UTC (None)
    when the preferences cannot be read, so an unreadable preference never
    blocks a start or leaves a cap unenforced; the GET of the ledger still
    says the preferences could not be read."""

    def zone_of(email: str) -> str | None:
        try:
            return read(email)
        except PreferencesUnavailable:
            log.warning("voice budget: preferences unreadable; the day is counted in UTC")
            return None

    return zone_of


class BudgetGuard:
    """The caps, enforced for one deployment. `ledger` computes; `end` ends a
    session for its cap: `VoiceSessions.end_for_budget`'s signature."""

    def __init__(self, db: Any, ledger: vl.VoiceLedger, *, now: Callable[[], datetime],
                 end: Callable[..., Any] | None) -> None:
        self._db = db
        self._ledger = ledger
        self._now = now
        self._end = end

    # ---- the start ---------------------------------------------------------

    def refuse_start(self, *, tenant_id: str, person: str) -> None:
        """`VoiceCapReached` when the person's day is at its cap; else nothing.
        Reads only: the caller writes nothing before this returns."""
        view = self._ledger.compute_day(tenant_id=tenant_id, person=person)
        day = view[vl.DAY]
        if day["total_usd"] < day["cap_usd"]:
            return
        raise vl.VoiceCapReached(
            f"Refused: today ({view['date']}, {view['time_zone']}) has ${day['spent_usd']:.2f} "
            f"spent and ${day['reserved_usd']:.2f} reserved, at the ${day['cap_usd']:.0f} day cap. "
            "No session was started.",
            detail={"reason": vl.DAY_CAP_REACHED, "line": vl.DAY, "cap_usd": day["cap_usd"],
                    "spent_usd": day["spent_usd"], "reserved_usd": day["reserved_usd"],
                    "date": view["date"], "time_zone": view["time_zone"]})

    # ---- delegated work ----------------------------------------------------

    def admit_work(self, *, tenant_id: str, person: str, project_id: str,
                   session_id: str) -> dict[str, Any]:
        """Whether one more delegation may start: the session's enforcement
        state (`enforce`'s answer) when it may; `VoiceCapReached` when the
        session is at a cap, or one more planner turn would pass one."""
        state = self.enforce(tenant_id=tenant_id, person=person, project_id=project_id,
                             session_id=session_id)
        if state["capped"] is not None:
            raise vl.VoiceCapReached(
                "Refused: this session reached its budget cap; nothing more is started.",
                detail={"reason": vl.SESSION_CAPPED, "line": state["capped"],
                        "ends_at": state["ends_at"]})
        self._ledger.reserve_or_refuse(tenant_id=tenant_id, person=person, session_id=session_id,
                                       steps=[{"usd": vl.DELEGATION_ESTIMATE_USD}])
        return state

    # ---- warn, cap, end ----------------------------------------------------

    def enforce(self, *, tenant_id: str, person: str, project_id: str,
                session_id: str) -> dict[str, Any]:
        """Compute the ledger, record any cap newly warned of or reached (once
        each), draw or update the Budget card, and end the session when its
        grace has run out. `{warned, capped, ends_at, notices, ended}`;
        `notices` are what this call newly recorded, for the voice to say."""
        person = person.strip().lower()
        view = self._ledger.compute(tenant_id=tenant_id, person=person, session_id=session_id)
        now = self._now()
        ref = (self._db.collection(vs.PROJECTS).document(project_id)
               .collection(vs.SESSIONS).document(session_id))

        @firestore.transactional
        def _record(txn: Any) -> tuple[dict[str, Any], list[dict[str, str]], bool]:
            snap = _snapshot(txn.get(ref))
            data = (snap.to_dict() or {}) if snap.exists else {}
            if not vstore.VoiceStore._mine(data, tenant_id, person):  # noqa: SLF001
                raise NotFound(f"voice session {session_id!r} not found")
            state = dict(data.get(vl.BUDGET_FIELD) or {})
            if data.get("state") != vs.LIVE:
                return state, [], False
            warned = dict(state.get("warned") or {})
            capped = dict(state.get("capped") or {})
            notices: list[dict[str, str]] = []
            for key in (vl.SESSION, vl.DAY):
                cap = view[key]
                if cap["total_usd"] >= cap["cap_usd"] * vl.WARN_SHARE and key not in warned:
                    warned[key] = now
                    notices.append({"notice": WARNING, "line": key})
                if cap["total_usd"] >= cap["cap_usd"] and not capped:
                    capped = {"line": key, "at": now,
                              "ends_at": now + timedelta(seconds=vl.CAP_GRACE_SECONDS)}
                    notices.append({"notice": CAPPED, "line": key})
            if notices:
                state.update(warned=warned, capped=capped or None,
                             card_id=state.get("card_id") or "vc_" + secrets.token_hex(16))
                txn.update(ref, {vl.BUDGET_FIELD: state})
            return state, notices, True

        state, notices, live = _record(self._db.transaction())
        if notices:
            self._draw(tenant_id=tenant_id, person=person, project_id=project_id,
                       session_id=session_id, state=state, notice=notices[-1], view=view)
        shown = vl.budget_view(state)
        ends_at = _when((state.get("capped") or {}).get("ends_at"))
        ended = False
        if live and ends_at is not None and now >= ends_at:
            if self._end is None:
                log.warning("voice budget: session=%s is past its grace and nothing can end it",
                            session_id)
            else:
                answer = self._end(tenant_id=tenant_id, person=person, session_id=session_id)
                ended = bool((answer or {}).get("changed", True))
                log.info("voice session ended for its budget cap session=%s line=%s",
                         session_id, shown["capped"])
        return {**shown, "notices": notices, "ended": ended}

    def _draw(self, *, tenant_id: str, person: str, project_id: str, session_id: str,
              state: Mapping[str, Any], notice: Mapping[str, str], view: Mapping[str, Any]) -> None:
        """The session's one Budget card, drawn by the platform: a new version
        for each notice, the latest saying what binds."""
        line = notice["line"]
        cap = view[line]
        ends_at = _when((state.get("capped") or {}).get("ends_at"))
        summary = (f"${cap['spent_usd']:.2f} spent and ${cap['reserved_usd']:.2f} reserved "
                   f"of the ${cap['cap_usd']:.0f} {'session' if line == vl.SESSION else 'day'} cap.")
        card = {
            "card_id": state.get("card_id"), "kind": "budget", "state": vcards.WAITING,
            "title": TITLES[(notice["notice"], line)], "created_by": "platform",
            "references": {name: None for name in vcards.REFERENCES},
            "summary": summary,
            "body": {
                "status": notice["notice"],
                "cap": line,
                "ends_at": ends_at.isoformat() if ends_at else None,
                "lines": [{"label": LINE_LABELS.get(row["line"], row["line"]),
                           "spent_usd": row["spent_usd"], "reserved_usd": row["reserved_usd"]}
                          for row in cap["lines"]],
                "spent_usd": cap["spent_usd"],
                "reserved_usd": cap["reserved_usd"],
                "session_cap_usd": view[vl.SESSION]["cap_usd"],
                "day_cap_usd": view[vl.DAY]["cap_usd"],
                "not_counted": list(view["not_counted"]),
            },
        }
        vcards.write_card(vstore.VoiceStore(self._db, now=self._now), tenant_id=tenant_id,
                          person=person, project_id=project_id, session_id=session_id, card=card)

    # ---- the sweep's backstop ------------------------------------------------

    def sweep(self) -> list[str]:
        """`enforce` for every live session a person's marker names; the ids
        ended for their cap. One session's failure never stops the sweep."""
        ended: list[str] = []
        for marker_snap in self._db.collection(vs.LIVE_MARKERS).stream():
            marker = marker_snap.to_dict() or {}
            project_id, session_id = marker.get("project_id"), marker.get("session_id")
            if not isinstance(project_id, str) or not isinstance(session_id, str):
                continue
            snap = (self._db.collection(vs.PROJECTS).document(project_id)
                    .collection(vs.SESSIONS).document(session_id).get())
            session = (snap.to_dict() or {}) if snap.exists else {}
            tenant_id, person = session.get("tenant_id"), session.get("person")
            if session.get("state") != vs.LIVE or not tenant_id or not person:
                continue
            try:
                done = self.enforce(tenant_id=str(tenant_id), person=str(person),
                                    project_id=project_id, session_id=session_id)
            except Exception as exc:  # noqa: BLE001 -- the type only; the next sweep tries again
                log.warning("voice budget sweep: session=%s failed (%s)", session_id,
                            type(exc).__name__)
                continue
            if done["ended"]:
                ended.append(session_id)
        return ended
