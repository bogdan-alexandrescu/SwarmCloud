"""Voice it's debug trace: one session's planner turns, kept while debug mode
is on (the owner, 10 November 2026: "Trace on Telemetry").

    voice_traces/{session}              tenant_id, person, project_id, session_id,
                                        engine, started_at, updated_at, turns,
                                        cards, refused, events
    voice_traces/{session}/turns/{n}    one planner turn, as `check_turn` keeps it,
                                        with the tier it ran on

WHY. On 10 November the GPT voice engine told the person "there is a card on
screen" while the planner had drawn nothing, and finding out why took reading
the voice pod's logs by hand. A trace answers it from the console: what the
planner was given, what it read, what it drew or was refused, why nothing was
drawn, what the voice was told and how long that took.

WHO WRITES. Only swarm-voice, through its session grant
(`POST /v1/voice/sessions/{session_id}/trace`, routes/voice_trace.py), and
only for a session that started with debug mode on (voice/settings.py): with
it off swarm-voice sends nothing, so nothing here is written. The tenant, the
person and the project come from the session document, never from the body.

WHAT IS KEPT. Every text (the transcript lines, the handoff, a card's title, a
mock-up's frame labels, a refusal's reason, the reply) passes through
`swarm_redaction.redact`, the masking Voice it applies to every other text it
stores (cards.py, visuals.py, readback.py), before it is written. A read is
its tool's name and target, never what it returned. Counts are whole numbers;
anything else in the body is refused 422, so nothing the body names but this
module does not know is ever stored.

THE TIER. Each turn keeps the tier its planner ran on (the owner, 11 November
2026: "store and show the tier"), as swarm-voice sends it beside the engine:
one of the broker's tier names (`TIERS`), or absent. Absent is kept as None
and served as None, "not recorded", for a turn whose planner never finished
and for every turn kept before the field was; it is never read as a plan,
since a plan is only one of the three tiers and guessing it would show a key's
turn as a plan's. Any other value is refused 422, as every other field is.

DELETED WITH THE SESSION. Deleting a Voice it project deletes its sessions'
documents (store.VoiceStore.delete_project), and with them each session's
trace (`refs_for`), in the same batches.

LISTED BY TENANT. The recent sessions with a trace are one equality query on
`tenant_id` (the single-field index Firestore keeps by default, so a fresh
deployment needs no index), sorted here, the way Telemetry lists its sessions.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Callable

from google.cloud.firestore_v1.base_query import FieldFilter
from swarm_redaction import redact

from ..errors import NotFound, ValidationFailed
from . import store as vstore
from .settings import CHAIN_TIERS

#: The tiers a turn names: the broker's tier names (`quota_broker.accounts.TIERS`),
#: as the Voice it chain line already restates them (settings.CHAIN_TIERS).
TIERS: tuple[str, ...] = CHAIN_TIERS

COLLECTION = "voice_traces"
TURNS = "turns"

#: The stream event a finished turn sends to Talk (voice/stream.py).
TRACE_TURN = "trace.turn"

#: The engines a trace names, by code, with the words the console shows.
ENGINES: dict[str, str] = {"own": "Own speech", "gpt-live": "GPT-Live"}

#: Why nothing was drawn, when nothing was: a fixed code and its plain sentence.
NONE_CODES: dict[str, str] = {
    "empty_transcript": "The transcript was empty: the planner was given nothing to plan from.",
    "refused": "Every card or mock-up the planner tried to draw was refused.",
    "planner_error": "The planner could not run or finish the turn.",
    "timeout": "The planner ran out of time before it drew anything.",
    "no_grant": "The session held no grant, so the planner could not reach swarm-api.",
    "answered_in_words": "Answered in words: no card needed.",
}

#: The sideband's counts, per turn and per session (replies: per session only).
TURN_EVENTS = ("pieces", "finals", "empty_finals", "handoffs", "barge_ins")
SESSION_EVENTS = (*TURN_EVENTS, "replies")

WHO = ("person", "voice")

MAX_LINES = 200
MAX_TEXT = 2000
MAX_TITLE = 200
MAX_ITEMS = 40
MAX_FRAMES = 12
MAX_COUNT = 10**6
MAX_TURN = 10**4
MAX_SECONDS = 24 * 3600.0
#: The recent sessions the list answers, newest first.
RECENT = 50


def _refuse(field: str, message: str) -> ValidationFailed:
    return ValidationFailed(message, detail={"field": field})


def mask(value: Any, field: str, limit: int = MAX_TEXT) -> str:
    """`value`, a string, cut to `limit` and masked; 422 otherwise."""
    if not isinstance(value, str):
        raise _refuse(field, f"{field} is text")
    return redact(value[:limit]).text


def _mask_or_none(value: Any, field: str, limit: int = MAX_TEXT) -> str | None:
    return None if value is None else mask(value, field, limit)


def _list(value: Any, field: str, limit: int = MAX_ITEMS) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise _refuse(field, f"{field} is a list of at most {limit}")
    return value


def _counts(value: Any, field: str, names: tuple[str, ...]) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) - set(names):
        raise _refuse(field, f"{field} counts only {', '.join(names)}")
    counts: dict[str, int] = {}
    for name in names:
        n = value.get(name, 0)
        if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= MAX_COUNT:
            raise _refuse(f"{field}.{name}", f"{field}.{name} is a whole number")
        counts[name] = n
    return counts


def _drawn(value: Any, n: int) -> dict[str, Any]:
    field = f"turn.drawn[{n}]"
    if not isinstance(value, Mapping):
        raise _refuse(field, f"{field} is an object")
    kind = mask(value.get("kind"), f"{field}.kind", 64)
    item: dict[str, Any] = {"kind": kind,
                            "title": _mask_or_none(value.get("title"), f"{field}.title", MAX_TITLE)}
    frames = value.get("frames")
    if frames is not None:
        item["frames"] = [mask(f, f"{field}.frames", MAX_TITLE)
                          for f in _list(frames, f"{field}.frames", MAX_FRAMES)]
    refused = value.get("refused")
    if refused is not None:
        item["refused"] = mask(refused, f"{field}.refused", 500)
    return item


def _read(value: Any, n: int) -> dict[str, Any]:
    field = f"turn.read[{n}]"
    if not isinstance(value, Mapping):
        raise _refuse(field, f"{field} is an object")
    return {"tool": mask(value.get("tool"), f"{field}.tool", 64),
            "target": _mask_or_none(value.get("target"), f"{field}.target", MAX_TITLE)}


def check_turn(turn: Any, tier: Any = None) -> dict[str, Any]:
    """The turn as it is kept: every text masked, every count a whole number,
    `tier` one of TIERS or None (not recorded); 422 naming the field otherwise."""
    if tier is not None and (not isinstance(tier, str) or tier not in TIERS):
        raise _refuse("tier", f"tier is null or one of {', '.join(TIERS)}")
    if not isinstance(turn, Mapping):
        raise _refuse("turn", "turn is an object")
    n = turn.get("n")
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_TURN:
        raise _refuse("turn.n", "turn.n is the turn's number, from 1")
    secs = turn.get("secs")
    if secs is not None and (not isinstance(secs, (int, float)) or isinstance(secs, bool)
                             or not math.isfinite(secs) or not 0 <= secs <= MAX_SECONDS):
        raise _refuse("turn.secs", "turn.secs is null or seconds")
    lines = []
    for i, line in enumerate(_list(turn.get("lines", []), "turn.lines", MAX_LINES)):
        if not isinstance(line, Mapping) or line.get("who") not in WHO:
            raise _refuse(f"turn.lines[{i}]", "a line is {who: person|voice, text}")
        lines.append({"who": line["who"], "text": mask(line.get("text"), f"turn.lines[{i}].text")})
    none = turn.get("none")
    if none is not None and none not in NONE_CODES:
        raise _refuse("turn.none", f"turn.none is null or one of {', '.join(NONE_CODES)}")
    drawn = [_drawn(d, i) for i, d in enumerate(_list(turn.get("drawn", []), "turn.drawn"))]
    return {
        "n": n,
        "secs": None if secs is None else round(float(secs), 3),
        "lines": lines,
        "handoff": _mask_or_none(turn.get("handoff"), "turn.handoff"),
        "read": [_read(r, i) for i, r in enumerate(_list(turn.get("read", []), "turn.read"))],
        "drawn": drawn,
        "none": none,
        "told": mask(turn.get("told", ""), "turn.told"),
        "events": _counts(turn.get("events", {}), "turn.events", TURN_EVENTS),
        "tier": tier,
    }


def stream_event(turn: Mapping[str, Any]) -> dict[str, Any]:
    """Talk's one small line for a kept turn: its number, the cards drawn and
    refused (kind and masked title; a refusal's masked reason) and the
    no-cards code, and the tier the turn ran on (None: not recorded)."""
    drawn = [d for d in turn["drawn"] if "refused" not in d]
    refused = [d for d in turn["drawn"] if "refused" in d]
    return {"type": TRACE_TURN, "n": turn["n"],
            "drawn": [{"kind": d["kind"], "title": d["title"]} for d in drawn],
            "refused": [{"kind": d["kind"], "title": d["title"], "reason": d["refused"]}
                        for d in refused],
            "none": turn["none"], "tier": turn.get("tier")}


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _ref(db: Any, session_id: str) -> Any:
    return db.collection(COLLECTION).document(session_id)


def refs_for(db: Any, session_id: str) -> list[Any]:
    """A session's trace documents, its turns before its summary, for the
    project delete (store.VoiceStore.delete_project)."""
    ref = _ref(db, session_id)
    refs = [snap.reference for snap in ref.collection(TURNS).stream()]
    if ref.get().exists:
        refs.append(ref)
    return refs


class Trace:
    def __init__(self, db: Any, *, now: Callable[[], datetime]) -> None:
        self._db = db
        self._now = now

    def record(self, *, tenant_id: str, person: str, project_id: str, session_id: str,
               engine: Any, session_events: Any, turn: Any,
               started_at: datetime | None, tier: Any = None) -> dict[str, Any]:
        """Keep one finished turn and the session's counts; the kept turn."""
        if engine not in ENGINES:
            raise _refuse("engine", f"engine is one of {', '.join(ENGINES)}")
        events = _counts(session_events, "session_events", SESSION_EVENTS)
        kept = check_turn(turn, tier)
        now = self._now()
        ref = _ref(self._db, session_id)
        snap = ref.get()
        data = (snap.to_dict() or {}) if snap.exists else {}
        if snap.exists and (data.get("tenant_id") != tenant_id or data.get("person") != person):
            raise NotFound(f"voice session {session_id!r} not found")
        ref.collection(TURNS).document(f"{kept['n']:04d}").set(
            {**kept, "at": now, "tenant_id": tenant_id})
        cards = sum(1 for d in kept["drawn"] if "refused" not in d)
        refused = len(kept["drawn"]) - cards
        ref.set({
            "tenant_id": tenant_id, "person": person, "project_id": project_id,
            "session_id": session_id, "engine": engine,
            "started_at": data.get("started_at") or started_at or now, "updated_at": now,
            "turns": max(int(data.get("turns") or 0), kept["n"]),
            "cards": int(data.get("cards") or 0) + cards,
            "refused": int(data.get("refused") or 0) + refused,
            "events": events,
        })
        return kept

    def _owned(self, session_id: str, tenant_id: str | None) -> dict[str, Any]:
        vstore.check_id(session_id, "vs_", "session_id")
        snap = _ref(self._db, session_id).get()
        data = (snap.to_dict() or {}) if snap.exists else None
        if data is None or (tenant_id is not None and data.get("tenant_id") != tenant_id):
            raise NotFound(f"no trace for voice session {session_id!r}")
        return data

    def _minutes(self, data: Mapping[str, Any]) -> float | None:
        """From the session's start to its end, else to its last traced turn."""
        started = data.get("started_at")
        end = data.get("updated_at")
        project_id, session_id = data.get("project_id"), data.get("session_id")
        if isinstance(project_id, str) and isinstance(session_id, str):
            snap = (self._db.collection(vstore.PROJECTS).document(project_id)
                    .collection(vstore.SESSIONS).document(session_id).get())
            ended = (snap.to_dict() or {}).get("ended_at") if snap.exists else None
            end = ended if isinstance(ended, datetime) else end
        if not isinstance(started, datetime) or not isinstance(end, datetime):
            return None
        return round(max(0.0, (end - started).total_seconds()) / 60.0, 2)

    def session(self, session_id: str, *, tenant_id: str | None) -> dict[str, Any]:
        """One session's trace, in the console's TraceSession shape; 404 for a
        session with no trace or another workspace's (`tenant_id` None: any)."""
        data = self._owned(session_id, tenant_id)
        rows = (_ref(self._db, session_id).collection(TURNS).order_by("n").stream())
        turns = [served_turn(snap.to_dict() or {}) for snap in rows]
        return {**_summary(data), "minutes": self._minutes(data), "turns": turns,
                "events": _counts(data.get("events") or {}, "events", SESSION_EVENTS)}

    def sessions(self, *, tenant_id: str | None) -> list[dict[str, Any]]:
        """The recent sessions with a trace, newest first, at most RECENT."""
        query = self._db.collection(COLLECTION)
        if tenant_id is not None:
            query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
        rows = [snap.to_dict() or {} for snap in query.stream()]
        rows = [r for r in rows if isinstance(r.get("started_at"), datetime)]
        rows.sort(key=lambda r: r["started_at"], reverse=True)
        return [{**_summary(r), "turns": int(r.get("turns") or 0),
                 "cards": int(r.get("cards") or 0), "refused": int(r.get("refused") or 0)}
                for r in rows[:RECENT]]


def _summary(data: Mapping[str, Any]) -> dict[str, Any]:
    engine = data.get("engine")
    return {"session_id": data.get("session_id"), "project_id": data.get("project_id"),
            "engine": ENGINES.get(str(engine), str(engine)), "engine_code": engine,
            "started": _iso(data.get("started_at"))}


def _pieces(text: Any) -> list[str]:
    """A kept text as the console's `Masked[]`: one piece, already masked in
    place by `redact` (it marks what it hid inside the text, not as a kind)."""
    return [text if isinstance(text, str) else ""]


def served_turn(row: Mapping[str, Any]) -> dict[str, Any]:
    """A kept turn in the console's TraceTurn shape (snake_case): `read` as
    "tool: target" strings, `none` as the plain sentence beside its code,
    `tier` the tier's name or None for a turn that recorded none."""
    code = row.get("none")
    reads: Iterable[Mapping[str, Any]] = row.get("read") or []
    return {
        "n": row.get("n"),
        "at": _iso(row.get("at")),
        "secs": row.get("secs"),
        "lines": [{"who": line.get("who"), "text": _pieces(line.get("text"))}
                  for line in row.get("lines") or []],
        "handoff": None if row.get("handoff") is None else _pieces(row.get("handoff")),
        "read": [f"{r.get('tool')}: {r['target']}" if r.get("target") else str(r.get("tool"))
                 for r in reads],
        "drawn": [dict(d) for d in row.get("drawn") or []],
        "none": NONE_CODES.get(code) if isinstance(code, str) else None,
        "none_code": code,
        "told": _pieces(row.get("told")),
        "events": dict(row.get("events") or {}),
        "tier": row.get("tier") if row.get("tier") in TIERS else None,
    }


__all__ = [
    "COLLECTION",
    "ENGINES",
    "NONE_CODES",
    "SESSION_EVENTS",
    "TIERS",
    "TRACE_TURN",
    "TURNS",
    "TURN_EVENTS",
    "Trace",
    "check_turn",
    "mask",
    "refs_for",
    "served_turn",
    "stream_event",
]
