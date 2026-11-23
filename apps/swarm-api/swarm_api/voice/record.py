"""A Voice it session's record: its transcript, kept redacted and served as
one text file, and the short summary written once when it ends.

    voice_transcripts/{session}            tenant_id, person, project_id, session_id, lines
    voice_transcripts/{session}/lines/{n}  who (person | voice), text, handed, at,
                                           tenant_id, person

WHAT IS KEPT. Each turn that passes through swarm-api -- a text turn
(`sessions.VoiceSessions.turn`) and a speech turn (the audio relay's
`speech_relay.ClientTurns`, wrapped by `Recording`) -- adds the person's line
and the voice's reply. Every text passes through `swarm_redaction.redact`, the
masking Voice it applies to every other text it stores (trace.py, cards.py,
visuals.py), before it is written, and again when it is served. A person line
the fast front handed to the planner is marked `handed`. A GPT-Live session's
turns run on swarm-voice's sideband and never pass through here: its
transcript holds nothing. Recording never fails a turn: a write that fails is
logged by session id and the turn's answer stands.

WHO READS. Only the session's own person in the selected workspace: the
session is found as every other read finds it (the projects whose `tenant_id`
and `person` are the caller's, then the session under each), so another
person's or another workspace's session is the same 404 as a missing one, and
nothing under it is read.

THE SUMMARY (`Summaries`). When a session ends (`sessions.VoiceSessions._end`
calls `after_end`), the session document's `summary` field is claimed in a
transaction, so it is written once whatever ends it again:

  capped   the session ended at its cap (`budget_cap`), or its ledger is at a
           cap already, or one more summary at SUMMARY_ESTIMATE_USD would pass
           one: no model is asked
  pending  claimed; swarm-voice is asked on a daemon thread, so the end
           returns without waiting for it
  written  swarm-voice's few sentences, redacted, with the model
  failed   no summary: swarm-voice could not be reached or made none, or the
           ledger could not be read (a cap that cannot be checked is not
           passed). The end is never blocked: every error is logged and
           recorded here, never raised into the end

COUNTED. A written summary's token counts are recorded as one turn of the
session (`voice_projects/{p}/turns/{vt_...}`, `kind: summary`), so the ledger
(ledger.py, which prices every turn's `usage`) counts it in the session's
spend and against its caps; a plan's (`billing: subscription`) is reserved
there as every unmeasured planner turn is.

DELETED WITH THE SESSION. Deleting a Voice it project deletes each session's
transcript (`refs_for`), as it deletes its trace.
"""

from __future__ import annotations

import logging
import secrets
import threading
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from swarm_redaction import redact

from swarm_common.admission import _snapshot

from ..errors import NotFound
from . import sessions as vs
from . import store as vstore

log = logging.getLogger(__name__)

COLLECTION = "voice_transcripts"
LINES = "lines"
PERSON = "person"
VOICE = "voice"
#: The longest line kept; the most lines one session keeps.
MAX_TEXT = 4_000
MAX_LINES = 2_000

SUMMARY_FIELD = "summary"
NONE = "none"
PENDING = "pending"
WRITTEN = "written"
CAPPED = "capped"
FAILED = "failed"
#: A summary turn's `kind` on the session's turns.
SUMMARY_KIND = "summary"
#: What one summary is held against the caps before it is asked for: a few
#: thousand tokens on the haiku class is cents, well under this.
SUMMARY_ESTIMATE_USD = 0.05
SUMMARY_MAX = 1_200


def _mask(text: Any, limit: int = MAX_TEXT) -> str:
    return redact(str(text or "")[:limit]).text


def _ref(db: Any, session_id: str) -> Any:
    return db.collection(COLLECTION).document(session_id)


def refs_for(db: Any, session_id: str) -> list[Any]:
    """A session's transcript documents, its lines before the parent, for the
    project delete (store.VoiceStore.delete_project)."""
    ref = _ref(db, session_id)
    refs = [snap.reference for snap in ref.collection(LINES).stream()]
    if ref.get().exists:
        refs.append(ref)
    return refs


def record_lines(db: Any, *, tenant_id: str, person: str, project_id: str, session_id: str,
                 lines: Iterable[tuple[str, str, bool]], now: Callable[[], datetime]) -> int:
    """Append (who, text, handed) lines, redacted; how many were written.
    The counter is the parent's `lines`, read and written in the same
    transaction, so two turns never share a number."""
    rows = [(who, _mask(text), bool(handed)) for who, text, handed in lines
            if who in (PERSON, VOICE) and str(text or "").strip()]
    if not rows:
        return 0
    ref = _ref(db, session_id)

    @firestore.transactional
    def _apply(txn: Any) -> int:
        snap = _snapshot(txn.get(ref))
        data = (snap.to_dict() or {}) if snap.exists else {}
        if snap.exists and not vstore.VoiceStore._mine(data, tenant_id, person):
            raise NotFound(f"voice session {session_id!r} not found")
        seq = int(data.get(LINES) or 0)
        room = max(MAX_LINES - seq, 0)
        kept = rows[:room]
        if not kept:
            return 0
        at = now()
        txn.set(ref, {"tenant_id": tenant_id, "person": person, "project_id": project_id,
                      "session_id": session_id, LINES: seq + len(kept), "updated_at": at})
        for i, (who, text, handed) in enumerate(kept, start=seq + 1):
            txn.set(ref.collection(LINES).document(f"{i:06d}"), {
                "who": who, "text": text, "handed": handed, "at": at, "n": i,
                "tenant_id": tenant_id, "person": person})
        return len(kept)

    return _apply(db.transaction())


def record_turn_quietly(db: Any, *, tenant_id: str, person: str, project_id: str, session_id: str,
                        said: str, reply: str, handed: bool, now: Callable[[], datetime]) -> None:
    """`record_lines` for one turn, never raising: a turn is never failed by
    its transcript."""
    try:
        record_lines(db, tenant_id=tenant_id, person=person, project_id=project_id,
                     session_id=session_id, lines=[(PERSON, said, handed), (VOICE, reply, False)], now=now)
    except Exception as exc:  # noqa: BLE001
        log.warning("voice transcript: session=%s turn not recorded (%s)", session_id, type(exc).__name__)


def find_session(db: Any, *, tenant_id: str, person: str, session_id: str) -> tuple[str, dict[str, Any]]:
    """(project id, session document) of this person's session in this
    tenant; the same `NotFound` for anyone else's and a missing one."""
    vstore.check_id(session_id, "vs_", "session_id")
    person = person.strip().lower()
    projects = (db.collection(vs.PROJECTS)
                .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                .where(filter=FieldFilter("person", "==", person))
                .stream())
    for project in projects:
        if not vstore.VoiceStore._mine(project.to_dict(), tenant_id, person):
            continue
        snap = db.collection(vs.PROJECTS).document(project.id).collection(vs.SESSIONS).document(session_id).get()
        data = (snap.to_dict() or {}) if snap.exists else None
        if data is not None and vstore.VoiceStore._mine(data, tenant_id, person):
            return project.id, data
    raise NotFound(f"voice session {session_id!r} not found")


def lines_of(db: Any, *, tenant_id: str, person: str, session_id: str) -> list[dict[str, Any]]:
    """The session's kept lines in order, each re-masked; only rows that are
    this person's in this tenant."""
    rows = [snap.to_dict() or {} for snap in _ref(db, session_id).collection(LINES).stream()]
    rows = [r for r in rows if vstore.VoiceStore._mine(r, tenant_id, person)]
    rows.sort(key=lambda r: int(r.get("n") or 0))
    return [{"who": r.get("who"), "text": _mask(r.get("text")), "handed": bool(r.get("handed"))}
            for r in rows if r.get("who") in (PERSON, VOICE)]


def transcript_file(db: Any, *, tenant_id: str, person: str, session_id: str) -> str:
    """The session's transcript as one text file: one line per turn side."""
    person = person.strip().lower()
    find_session(db, tenant_id=tenant_id, person=person, session_id=session_id)
    lines = lines_of(db, tenant_id=tenant_id, person=person, session_id=session_id)
    body = "\n".join(f"{line['who']}: {' '.join(line['text'].split())}" for line in lines)
    return body + "\n" if body else ""


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def summary_view(db: Any, *, tenant_id: str, person: str, session_id: str) -> dict[str, Any]:
    """The session's summary as the console reads it."""
    _project, data = find_session(db, tenant_id=tenant_id, person=person.strip().lower(),
                                  session_id=session_id)
    stored = data.get(SUMMARY_FIELD)
    stored = stored if isinstance(stored, Mapping) else {}
    state = stored.get("state") if stored.get("state") in (PENDING, WRITTEN, CAPPED, FAILED) else NONE
    text = _mask(stored.get("text"), SUMMARY_MAX) if state == WRITTEN and stored.get("text") else None
    return {"session_id": session_id, "state": state, "text": text,
            "at": _iso(stored.get("at")) if state == WRITTEN else None}


def summary_lines(lines: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The lines swarm-voice is sent: who and text alone."""
    return [{"who": str(line["who"]), "text": str(line["text"])} for line in lines]


class Summaries:
    """Writes an ended session's summary once (module docstring)."""

    def __init__(self, db: Any, *, client: Any, now: Callable[[], datetime],
                 ledger: Callable[[], Any] | None,
                 spawn: Callable[[Callable[[], None]], None] | None = None) -> None:
        self._db = db
        self._client = client
        self._now = now
        self._ledger = ledger
        self._spawn = spawn or _daemon

    def _session_ref(self, project_id: str, session_id: str) -> Any:
        return self._db.collection(vs.PROJECTS).document(project_id).collection(vs.SESSIONS).document(session_id)

    def _claim(self, project_id: str, session_id: str, state: str) -> bool:
        """Set `summary.state` when none is set yet; whether this call did."""
        ref = self._session_ref(project_id, session_id)

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            data = (snap.to_dict() or {}) if snap.exists else {}
            if not snap.exists or isinstance(data.get(SUMMARY_FIELD), Mapping):
                return False
            txn.update(ref, {SUMMARY_FIELD: {"state": state, "at": None, "text": None}})
            return True

        return _apply(self._db.transaction())

    def _set(self, project_id: str, session_id: str, fields: dict[str, Any]) -> None:
        self._session_ref(project_id, session_id).update({SUMMARY_FIELD: fields})

    def _at_cap(self, tenant_id: str, person: str, session_id: str) -> bool:
        if self._ledger is None:
            return False
        ledger = self._ledger().compute(tenant_id=tenant_id, person=person, session_id=session_id)
        for line in ("session", "day"):
            cap = ledger[line]
            if float(cap["total_usd"]) + SUMMARY_ESTIMATE_USD > float(cap["cap_usd"]):
                return True
        return False

    def after_end(self, *, tenant_id: str, person: str, project_id: str, session_id: str,
                  cause: str) -> None:
        """Called once a session is closed. Never raises."""
        try:
            self._after_end(tenant_id=tenant_id, person=person, project_id=project_id,
                            session_id=session_id, cause=cause)
        except Exception as exc:  # noqa: BLE001
            log.warning("voice summary: session=%s not started (%s)", session_id, type(exc).__name__)
            try:
                self._set(project_id, session_id, {"state": FAILED, "at": None, "text": None})
            except Exception:  # noqa: BLE001
                log.warning("voice summary: session=%s failure not recorded", session_id)

    def _after_end(self, *, tenant_id: str, person: str, project_id: str, session_id: str,
                   cause: str) -> None:
        if cause == vs.END_BUDGET_CAP:
            self._claim(project_id, session_id, CAPPED)
            return
        if not self._claim(project_id, session_id, PENDING):
            return
        try:
            capped = self._at_cap(tenant_id, person, session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("voice summary: session=%s ledger not read (%s)", session_id, type(exc).__name__)
            self._set(project_id, session_id, {"state": FAILED, "at": None, "text": None})
            return
        if capped:
            self._set(project_id, session_id, {"state": CAPPED, "at": None, "text": None})
            return
        self._spawn(lambda: self._write(tenant_id=tenant_id, person=person, project_id=project_id,
                                        session_id=session_id, cause=cause))

    def _handed(self, tenant_id: str, person: str, project_id: str, session_id: str,
                lines: list[dict[str, Any]]) -> list[str]:
        handed = [line["text"] for line in lines if line["who"] == PERSON and line["handed"]]
        starts = vstore.VoiceStore(self._db, now=self._now)._starts(tenant_id, project_id)
        for snap in starts:
            start = snap.to_dict() or {}
            if vstore.VoiceStore._mine(start, tenant_id, person) and start.get("session_id") == session_id:
                handed.append(f"started {start.get('kind')} {start.get('target_id')}")
        return handed

    def _write(self, *, tenant_id: str, person: str, project_id: str, session_id: str, cause: str) -> None:
        try:
            lines = lines_of(self._db, tenant_id=tenant_id, person=person, session_id=session_id)
            ask = getattr(self._client, "summarise", None)
            if not callable(ask):
                raise vs.VoiceUnreachable("swarm-voice takes no summary here.")
            said = ask(session_id, {
                "person": person, "transcript": summary_lines(lines)[-MAX_LINES:],
                "handed": self._handed(tenant_id, person, project_id, session_id, lines)[:100],
                "ended": cause})
            text = _mask(said.get("summary"), SUMMARY_MAX).strip()
            if not text:
                raise vs.VoiceUnreachable("swarm-voice answered no summary.")
            model = str(said.get("model") or "")
            usage = {k: v for k, v in dict(said.get("usage") or {}).items()
                     if isinstance(v, int) and not isinstance(v, bool)}
            if said.get("billing") == "subscription":
                usage["billing"] = "subscription"
            usage["model"] = model
            vstore.VoiceStore(self._db, now=self._now).record_turn(
                tenant_id=tenant_id, person=person, project_id=project_id,
                turn_id="vt_" + secrets.token_hex(12),
                fields={"session_id": session_id, "kind": SUMMARY_KIND, "usage": usage})
            self._set(project_id, session_id, {"state": WRITTEN, "text": text, "at": self._now(),
                                               "model": model})
            log.info("voice summary written session=%s", session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("voice summary: session=%s failed (%s)", session_id, type(exc).__name__)
            try:
                self._set(project_id, session_id, {"state": FAILED, "at": None, "text": None})
            except Exception:  # noqa: BLE001
                log.warning("voice summary: session=%s failure not recorded", session_id)


def _daemon(run: Callable[[], None]) -> None:
    threading.Thread(target=run, name="voice-summary", daemon=True).start()


class Recording:
    """The speech relay's `Turns`, recording each turn's two lines."""

    def __init__(self, turns: Any, db: Any, *, tenant_id: str, person: str, project_id: str,
                 now: Callable[[], datetime]) -> None:
        self._turns = turns
        self._db = db
        self._where = {"tenant_id": tenant_id, "person": person, "project_id": project_id}
        self._now = now

    def turn(self, session_id: str, text: str, anchor: dict[str, Any] | None) -> Any:
        line = self._turns.turn(session_id, text, anchor)
        record_turn_quietly(self._db, session_id=session_id, said=text,
                            reply=str(getattr(line, "reply", "") or ""),
                            handed=bool(getattr(line, "delegation_id", None)), now=self._now, **self._where)
        return line

    def delegation(self, session_id: str, delegation_id: str, *, timeout: float) -> str | None:
        said = self._turns.delegation(session_id, delegation_id, timeout=timeout)
        if said:
            try:
                record_lines(self._db, session_id=session_id, lines=[(VOICE, said, False)], now=self._now,
                             **self._where)
            except Exception as exc:  # noqa: BLE001
                log.warning("voice transcript: session=%s line not recorded (%s)", session_id,
                            type(exc).__name__)
        return said
