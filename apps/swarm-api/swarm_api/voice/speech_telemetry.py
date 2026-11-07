"""The own-speech engine's telemetry, stored and served
(docs/design/voice-it-own-speech.md §12).

WHAT IS STORED. The speech server sends a `metrics` frame every 5 s and one
`timing` frame per utterance (`kind: stt`) and per synthesis (`kind: tts`);
the relay hands each to `sink`, which stores it as one sample:

    voice_speech_sessions/{session}/samples/{seq}
        {kind, at, generation, session_id, tenant_id, expire_at, data}

`seq` is the write's time in epoch milliseconds, zero-padded, then the
generation and a short random part, so the ids sort by time and two sockets
of one session (a rotation, a reconnect) never write the same one. `expire_at`
is `at` plus SAMPLE_RETENTION (30 days), set on write; terraform's TTL policy
on the `samples` collection group (voice_speech.tf) deletes them.

The same transaction folds the sample into the session's own document,
`voice_speech_sessions/{session}`: its tenant, person and project, the
generation, the start's phases (from the lease), the first and last sample's
time, the counts, the raw
per-utterance and per-synthesis values (at most MAX_VALUES each) and the peaks
of the 5 s readings. It carries no `expire_at`, so it is kept when its samples
expire. No percentile, no day and no total is stored anywhere: a session's
percentiles and minutes, and every day, are computed on read from these
documents (the ledger's rule: never a second counter).

FENCED (invariant 5). Every sample names the generation of the lease that
admitted its socket; the write reads the lease in its transaction and a
generation that is no longer the lease's (or a lease another session now
holds) writes nothing. A stale holder stops writing.

NOTHING BUT NUMBERS. A frame is checked against ALLOWED, a list per kind of
the fields the server sends and their types; any other field, at any depth,
refuses the whole sample (`SampleRefused`). A sample therefore never carries
audio, a transcript, a spoken line or a token: the frames that do (`final`,
`partial`, `speak.start`, PCM) never reach this module, and a field of that
kind added to a telemetry frame is refused rather than stored.

DAYS ARE THE VIEWER'S. `days` places each session on the day it started in
the zone the console sends (`tz`), so one session can sit on 5 November for a
viewer in Los Angeles and on 6 November for one in London; nothing per day is
written.

MACHINE COST AND STOCK-OUTS (§12.4, §12.5) are read, never stored. A
session's `machine_usd` is the ledger's Speech machine line
(`ledger.speech_machine_lines`, §13): its stops' `voice_audit` rows, priced
from the catalogue when the machine stopped, plus the running start's minutes
so far; a day's is the sum over its sessions. `stockouts` counts the starts
the zone refused for want of an L4 (`speech_machine.stockouts`, the
lifecycle's AUDIT_NO_L4 rows): a session's carry its id, a day's are placed
by the row's own time, so a start refused outright, which never wrote a
sample, still counts on its day. When a source cannot be read its figure is
null and `machine_usd_reason` or `stockouts_reason` says which
(LEDGER_UNREADABLE, AUDIT_UNREADABLE, `ledger.MACHINE_UNPRICED`); the rest of
the row is served.

WHO READS (routes/voice_telemetry.py). `tenant_id=None` is the platform view
(every workspace's sessions, for platform admins); a tenant id is that
workspace's sessions only. The filter is applied here, on the stored tenant,
never on a value the caller typed.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot

from ..errors import NotFound, ValidationFailed
from .. import preferences
from . import ledger
from . import speech_machine as sm

log = logging.getLogger("swarm_api.voice.telemetry")

SESSIONS = "voice_speech_sessions"
SAMPLES = "samples"

#: Firestore's TTL deletes a sample this long after it was written (§12.3).
SAMPLE_RETENTION = timedelta(days=30)
#: The raw values a session document keeps per metric: an hour of talk is a
#: few hundred utterances, so this bounds a runaway session, not a real one.
MAX_VALUES = 2000
#: The longest span `days` and `sessions` answer, in days.
MAX_SPAN_DAYS = 400
#: A session list page and a live read's batch.
MAX_SESSIONS = 500
MAX_SAMPLES = 2000

#: Why a served machine cost or stock-out count is null: its source, the
#: ledger's Speech machine line or the machine's audit rows, could not be read.
LEDGER_UNREADABLE = "ledger_unreadable"
AUDIT_UNREADABLE = "audit_unreadable"

KIND_METRICS = "metrics"
KIND_STT = "stt"
KIND_TTS = "tts"

_NUM = (int, float)
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_WORD = re.compile(r"^[a-z_.]{1,32}$")
_SEQ = re.compile(r"^\d{13}-\d{1,12}-[0-9a-f]{6}$")


class SampleRefused(ValueError):
    """A frame with a field telemetry does not store; the message names the
    field's path and never its value."""


def _number(value: Any) -> bool:
    return value is None or (isinstance(value, _NUM) and not isinstance(value, bool)
                             and math.isfinite(float(value)))


def _flag(value: Any) -> bool:
    return value is None or isinstance(value, bool)


def _ident(value: Any) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool)) \
        or (isinstance(value, str) and bool(_ID.match(value)))


def _word(value: Any) -> bool:
    return value is None or (isinstance(value, str) and bool(_WORD.match(value)))


_COUNTERS = {name: _number for name in (
    "utterances", "stt_errors", "stt_audio_seconds", "syntheses", "tts_errors",
    "tts_cancelled", "tts_audio_seconds", "protocol_errors")}

#: Per kind, every field a stored sample may carry: a checker, or a nested
#: allow-list for a reading the server sends as an object.
ALLOWED: dict[str, dict[str, Any]] = {
    KIND_METRICS: {
        "at": _number,
        "gpu": {"util_pct": _number, "mem_used_mib": _number, "mem_total_mib": _number},
        "cpu_pct": _number,
        "memory": {"used_mib": _number, "total_mib": _number},
        "disk": {"used_gb": _number, "total_gb": _number},
        "vllm": {"queue": _number, "kv_cache_pct": _number},
        "counters": _COUNTERS,
        "open_sockets": _number,
        "phase": _word,
    },
    KIND_STT: {
        "seq": _ident, "final_ms": _number, "first_partial_ms": _number, "audio_s": _number,
        "processing_s": _number, "rtf": _number, "ok": _flag, "cut": _flag,
    },
    KIND_TTS: {
        "id": _ident, "first_audio_ms": _number, "audio_s": _number, "synth_s": _number,
        "rtf": _number, "cancelled": _flag, "ok": _flag,
    },
}

#: The per-utterance and per-synthesis values a session keeps, by kind.
VALUES = {
    KIND_STT: {"stt_final_ms": "final_ms", "stt_first_partial_ms": "first_partial_ms",
               "stt_rtf": "rtf"},
    KIND_TTS: {"tts_first_audio_ms": "first_audio_ms", "tts_rtf": "rtf"},
}

#: The 5 s readings whose peak a session keeps: name -> path in the sample.
PEAKS = {
    "gpu_util_pct": ("gpu", "util_pct"),
    "gpu_mem_used_mib": ("gpu", "mem_used_mib"),
    "cpu_pct": ("cpu_pct",),
    "memory_used_mib": ("memory", "used_mib"),
    "disk_used_gb": ("disk", "used_gb"),
    "vllm_queue": ("vllm", "queue"),
    "kv_cache_pct": ("vllm", "kv_cache_pct"),
}


def _check(data: Mapping[str, Any], allowed: Mapping[str, Any], path: str) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for name, value in data.items():
        where = f"{path}.{name}" if path else str(name)
        rule = allowed.get(name) if isinstance(name, str) else None
        if rule is None:
            raise SampleRefused(f"telemetry does not store the field {where!r}")
        if isinstance(rule, dict):
            if value is None:
                clean[name] = None
                continue
            if not isinstance(value, Mapping):
                raise SampleRefused(f"telemetry field {where!r} is not an object")
            clean[name] = _check(value, rule, where)
        elif not rule(value):
            raise SampleRefused(f"telemetry field {where!r} has a value it does not store")
        else:
            clean[name] = value
    return clean


def kind_of(frame: Mapping[str, Any]) -> str:
    t = frame.get("t")
    if t == "metrics":
        return KIND_METRICS
    if t == "timing" and frame.get("kind") in (KIND_STT, KIND_TTS):
        return str(frame["kind"])
    raise SampleRefused("not a telemetry frame")


def clean(frame: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """(kind, data): the frame as stored, or `SampleRefused` for a frame that
    is not telemetry or carries any field ALLOWED does not name."""
    if not isinstance(frame, Mapping):
        raise SampleRefused("not a telemetry frame")
    kind = kind_of(frame)
    body = {k: v for k, v in frame.items() if k not in ("t", "kind")}
    return kind, _check(body, ALLOWED[kind], "")


def _dig(data: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = data
    for part in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def percentiles(values: Iterable[Any]) -> dict[str, float | None]:
    """p50, p90 and max by nearest rank; None when there is nothing."""
    ordered = sorted(float(v) for v in values if _number(v) and v is not None)
    if not ordered:
        return {"p50": None, "p90": None, "max": None}

    def rank(p: float) -> float:
        return ordered[max(0, math.ceil(p * len(ordered)) - 1)]

    return {"p50": rank(0.5), "p90": rank(0.9), "max": ordered[-1]}


def sample_seq(at: datetime, generation: int) -> str:
    return f"{int(at.timestamp() * 1000):013d}-{int(generation)}-{secrets.token_hex(3)}"


# --------------------------------------------------------------------------
# Reading a span in the viewer's zone
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    first: date
    last: date
    zone: ZoneInfo

    @property
    def start(self) -> datetime:
        return datetime.combine(self.first, time(), self.zone).astimezone(timezone.utc)

    @property
    def end(self) -> datetime:
        return datetime.combine(self.last + timedelta(days=1), time(), self.zone) \
            .astimezone(timezone.utc)

    def days(self) -> list[date]:
        return [self.first + timedelta(days=i) for i in range((self.last - self.first).days + 1)]


def parse_zone(tz: str | None) -> ZoneInfo:
    """The viewer's IANA zone, or 422 naming `tz`."""
    if not preferences.is_time_zone(tz):
        raise ValidationFailed("tz must be an IANA time zone, the viewer's own.",
                               detail={"field": "tz"})
    return ZoneInfo(str(tz))


def parse_span(start: str | None, end: str | None, tz: str | None) -> Span:
    """`from` and `to` as YYYY-MM-DD, both included, in the IANA zone `tz`;
    422 naming the field otherwise."""
    zone = parse_zone(tz)
    days: list[date] = []
    for field, value in (("from", start), ("to", end)):
        try:
            days.append(date.fromisoformat(str(value or "")))
        except ValueError:
            raise ValidationFailed(f"{field} must be a day, YYYY-MM-DD.",
                                   detail={"field": field}) from None
    if days[1] < days[0]:
        raise ValidationFailed("to is before from.", detail={"field": "to"})
    if (days[1] - days[0]).days + 1 > MAX_SPAN_DAYS:
        raise ValidationFailed(f"at most {MAX_SPAN_DAYS} days at a time.", detail={"field": "to"})
    return Span(first=days[0], last=days[1], zone=zone)


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------


class Telemetry:
    def __init__(self, db: Any, *, now: Callable[[], datetime]) -> None:
        self._db = db
        self._now = now

    def _session_ref(self, session_id: str) -> Any:
        return self._db.collection(SESSIONS).document(session_id)

    # -- writing

    def record(self, owner: sm.SessionRef, generation: int, frame: Mapping[str, Any]) -> str | None:
        """Stores one frame as a sample and folds it into the session's
        document, in one transaction that reads the lease; the sample's seq,
        or None when `generation` is no longer the lease's (nothing written).
        `SampleRefused` for a frame with a field ALLOWED does not name."""
        kind, data = clean(frame)
        lease_ref = self._db.collection(sm.COLLECTION).document(sm.LEASE)
        session_ref = self._session_ref(owner.session_id)

        @firestore.transactional
        def _record(txn: Any) -> str | None:
            lease_snap = _snapshot(txn.get(lease_ref))
            lease = (lease_snap.to_dict() or {}) if lease_snap.exists else {}
            if lease.get("session_id") != owner.session_id \
                    or int(lease.get("generation") or 0) != int(generation):
                return None
            snap = _snapshot(txn.get(session_ref))
            current = (snap.to_dict() or {}) if snap.exists else {}
            at = self._now()
            seq = sample_seq(at, generation)
            txn.set(session_ref.collection(SAMPLES).document(seq), {
                "kind": kind, "at": at, "generation": int(generation),
                "session_id": owner.session_id, "tenant_id": owner.tenant_id,
                "expire_at": at + SAMPLE_RETENTION, "data": data,
            })
            folded = _folded(current, owner, int(generation), kind, data, at)
            # The start's phases (§4.2 step 5) are the lease's; the session keeps them.
            phases = lease.get("start_phases")
            if isinstance(phases, Mapping) and all(_number(v) for v in phases.values()):
                folded["start_phases"] = dict(phases)
            txn.set(session_ref, folded)
            return seq

        return _record(self._db.transaction())

    # -- reading

    def _sessions_in(self, span: Span, tenant_id: str | None) -> list[dict[str, Any]]:
        query = (self._db.collection(SESSIONS)
                 .where(filter=FieldFilter("first_at", ">=", span.start))
                 .where(filter=FieldFilter("first_at", "<", span.end)))
        found = []
        for snap in query.stream():
            doc = snap.to_dict() or {}
            if tenant_id is not None and doc.get("tenant_id") != tenant_id:
                continue
            found.append(doc)
        found.sort(key=lambda d: _utc(d.get("first_at")) or datetime.min.replace(tzinfo=timezone.utc))
        return found

    def _machine(self, docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
        """The ledger's Speech machine line of each session; None when the
        ledger cannot be read."""
        owners = {str(d["session_id"]): str(d.get("tenant_id"))
                  for d in docs if d.get("session_id")}
        try:
            return ledger.speech_machine_lines(self._db, owners, now=self._now)
        except Exception as exc:  # noqa: BLE001 - the figure is null, the read goes on
            log.warning("speech telemetry: the ledger's Speech machine line is unreadable (%s)",
                        type(exc).__name__)
            return None

    def _stockouts(self, tenant_id: str | None) -> list[dict[str, Any]] | None:
        """The machine's refused starts; None when its audit cannot be read."""
        try:
            return sm.stockouts(self._db, tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001 - the count is null, the read goes on
            log.warning("speech telemetry: the machine's audit rows are unreadable (%s)",
                        type(exc).__name__)
            return None

    def _summaries(self, docs: list[dict[str, Any]], tenant_id: str | None,
                   zone: ZoneInfo) -> list[dict[str, Any]]:
        lines, refused = self._machine(docs), self._stockouts(tenant_id)
        return [{**summary(doc, zone), **_machine_cost(lines, [doc]),
                 **_stockout_count(refused, lambda r, d=doc: r["session_id"] == d.get("session_id"))}
                for doc in docs]

    def sessions(self, span: Span, *, tenant_id: str | None) -> list[dict[str, Any]]:
        """Each session that started in the span, newest first, summarised
        with its machine cost and stock-outs."""
        docs = self._sessions_in(span, tenant_id)
        return self._summaries(list(reversed(docs[-MAX_SESSIONS:])), tenant_id, span.zone)

    def _owned(self, session_id: str, tenant_id: str | None) -> dict[str, Any]:
        snap = _snapshot(self._session_ref(session_id).get())
        doc = (snap.to_dict() or {}) if snap.exists else {}
        if not doc or (tenant_id is not None and doc.get("tenant_id") != tenant_id):
            raise NotFound(f"no telemetry for voice session {session_id!r}")
        return doc

    def session(self, session_id: str, *, tenant_id: str | None, zone: ZoneInfo) -> dict[str, Any]:
        """One session's summary and its samples still kept (30 days)."""
        doc = self._owned(session_id, tenant_id)
        [served] = self._summaries([doc], tenant_id, zone)
        return {"session": served,
                "samples": self.samples(session_id, tenant_id=tenant_id)}

    def samples(self, session_id: str, *, tenant_id: str | None,
                after: str | None = None) -> list[dict[str, Any]]:
        """The session's samples in order, those after `after` (a seq)."""
        ref = self._session_ref(session_id).collection(SAMPLES)
        rows = []
        for snap in ref.stream():
            doc = snap.to_dict() or {}
            if tenant_id is not None and doc.get("tenant_id") != tenant_id:
                continue
            if after is not None and snap.id <= after:
                continue
            rows.append((snap.id, doc))
        rows.sort(key=lambda r: r[0])
        return [_served_sample(seq, doc) for seq, doc in rows[:MAX_SAMPLES]]

    def days(self, span: Span, *, tenant_id: str | None) -> list[dict[str, Any]]:
        """One row per day of the span, every day present, computed now from
        the sessions that started on it in the span's zone."""
        by_day: dict[date, list[dict[str, Any]]] = {day: [] for day in span.days()}
        for doc in self._sessions_in(span, tenant_id):
            first = _utc(doc.get("first_at"))
            if first is None:
                continue
            day = first.astimezone(span.zone).date()
            if day in by_day:
                by_day[day].append(doc)
        lines = self._machine([doc for docs in by_day.values() for doc in docs])
        refused = self._stockouts(tenant_id)
        return [{**_day(day, docs), **_machine_cost(lines, docs),
                 **_stockout_count(refused, lambda r, d=day: r["at"].astimezone(span.zone).date() == d)}
                for day, docs in by_day.items()]


def _folded(current: Mapping[str, Any], owner: sm.SessionRef, generation: int, kind: str,
            data: Mapping[str, Any], at: datetime) -> dict[str, Any]:
    """The session document after one more sample."""
    doc = dict(current)
    doc.update({"session_id": owner.session_id, "tenant_id": owner.tenant_id,
                "person": owner.person, "project_id": owner.project_id,
                "generation": generation, "last_at": at})
    doc.setdefault("first_at", at)
    counts = dict(doc.get("counts") or {})
    counts[kind] = int(counts.get(kind) or 0) + 1
    if kind != KIND_METRICS and data.get("ok") is False:
        name = f"{kind}_errors"
        counts[name] = int(counts.get(name) or 0) + 1
    if kind != KIND_METRICS and _number(data.get("audio_s")) and data.get("audio_s") is not None:
        name = f"{kind}_audio_s"
        counts[name] = round(float(counts.get(name) or 0.0) + float(data["audio_s"]), 3)
    doc["counts"] = counts
    if kind in VALUES:
        values = {k: list(v) for k, v in (doc.get("values") or {}).items()}
        for name, field in VALUES[kind].items():
            value = data.get(field)
            kept = values.setdefault(name, [])
            if value is not None and len(kept) < MAX_VALUES:
                kept.append(value)
        doc["values"] = values
    else:
        peaks = dict(doc.get("peaks") or {})
        for name, path in PEAKS.items():
            value = _dig(data, path)
            if value is not None and (peaks.get(name) is None or value > peaks[name]):
                peaks[name] = value
        doc["peaks"] = peaks
    return doc


def _minutes(doc: Mapping[str, Any]) -> float:
    first, last = _utc(doc.get("first_at")), _utc(doc.get("last_at"))
    if first is None or last is None:
        return 0.0
    return round(max(0.0, (last - first).total_seconds()) / 60.0, 2)


def summary(doc: Mapping[str, Any], zone: ZoneInfo) -> dict[str, Any]:
    """A session's document as served: percentiles and minutes computed now."""
    counts = doc.get("counts") or {}
    values = doc.get("values") or {}
    first, last = _utc(doc.get("first_at")), _utc(doc.get("last_at"))
    return {
        "session_id": doc.get("session_id"),
        "tenant_id": doc.get("tenant_id"),
        "person": doc.get("person"),
        "generation": doc.get("generation"),
        "first_at": first.isoformat() if first else None,
        "last_at": last.isoformat() if last else None,
        "day": first.astimezone(zone).date().isoformat() if first else None,
        "minutes": _minutes(doc),
        "utterances": int(counts.get(KIND_STT) or 0),
        "syntheses": int(counts.get(KIND_TTS) or 0),
        "stt_errors": int(counts.get(f"{KIND_STT}_errors") or 0),
        "tts_errors": int(counts.get(f"{KIND_TTS}_errors") or 0),
        "stt_audio_s": float(counts.get(f"{KIND_STT}_audio_s") or 0.0),
        "tts_audio_s": float(counts.get(f"{KIND_TTS}_audio_s") or 0.0),
        "metrics_samples": int(counts.get(KIND_METRICS) or 0),
        "percentiles": {name: percentiles(values.get(name) or ())
                        for kind in VALUES for name in VALUES[kind]},
        "peaks": {name: (doc.get("peaks") or {}).get(name) for name in PEAKS},
        "start_phases": dict(doc.get("start_phases") or {}),
    }


def _day(day: date, docs: list[dict[str, Any]]) -> dict[str, Any]:
    def pooled(name: str) -> list[Any]:
        return [v for doc in docs for v in ((doc.get("values") or {}).get(name) or ())]

    def total(name: str) -> int:
        return sum(int((doc.get("counts") or {}).get(name) or 0) for doc in docs)

    return {
        "day": day.isoformat(),
        "sessions": len(docs),
        "minutes": round(sum(_minutes(doc) for doc in docs), 2),
        "utterances": total(KIND_STT),
        "syntheses": total(KIND_TTS),
        "stt_errors": total(f"{KIND_STT}_errors"),
        "tts_errors": total(f"{KIND_TTS}_errors"),
        "stt_final_ms_p90": percentiles(pooled("stt_final_ms"))["p90"],
        "tts_first_audio_ms_p90": percentiles(pooled("tts_first_audio_ms"))["p90"],
    }


def _machine_cost(lines: Mapping[str, Mapping[str, Any]] | None,
                  docs: list[dict[str, Any]]) -> dict[str, Any]:
    """`machine_usd` of these sessions, the sum of their ledger lines, and
    `machine_usd_reason`: null with the reason when any line is unknown."""
    if lines is None:
        return {"machine_usd": None, "machine_usd_reason": LEDGER_UNREADABLE}
    usd = 0.0
    for doc in docs:
        line = lines.get(str(doc.get("session_id")))
        if line is None:
            return {"machine_usd": None, "machine_usd_reason": LEDGER_UNREADABLE}
        if line.get("usd") is None:
            return {"machine_usd": None, "machine_usd_reason": line.get("reason")}
        usd += float(line["usd"])
    return {"machine_usd": round(usd, 4), "machine_usd_reason": None}


def _stockout_count(refused: list[dict[str, Any]] | None,
                    counts: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    if refused is None:
        return {"stockouts": None, "stockouts_reason": AUDIT_UNREADABLE}
    return {"stockouts": sum(1 for row in refused if counts(row)), "stockouts_reason": None}


def _served_sample(seq: str, doc: Mapping[str, Any]) -> dict[str, Any]:
    at = _utc(doc.get("at"))
    return {"seq": seq, "kind": doc.get("kind"), "at": at.isoformat() if at else None,
            "generation": doc.get("generation"), "data": doc.get("data") or {}}


def check_seq(value: str | None) -> str | None:
    """A resume cursor as `samples` reads it, or 422."""
    if value in (None, ""):
        return None
    if not _SEQ.match(str(value)):
        raise ValidationFailed("after must be a sample's seq.", detail={"field": "after"})
    return str(value)


# --------------------------------------------------------------------------
# The relay's hand-off
# --------------------------------------------------------------------------


def sink(telemetry: Telemetry, owner: sm.SessionRef, generation: int, *,
         loop: asyncio.AbstractEventLoop | None = None) -> Callable[[dict[str, Any]], None]:
    """The relay's `on_frame` for one socket: each frame is stored off the
    event loop (the relay calls it inline and speech must not wait on
    Firestore), and a refused or failed write is a log line with the
    exception's class and nothing of the frame. It never raises."""

    def _store(frame: dict[str, Any]) -> None:
        try:
            if telemetry.record(owner, generation, frame) is None:
                log.info("speech telemetry not written: generation %s is stale", generation)
        except SampleRefused as exc:
            log.warning("speech telemetry sample refused: %s", exc)
        except Exception as exc:  # noqa: BLE001 -- telemetry never stops speech
            log.warning("speech telemetry write failed (%s)", type(exc).__name__)

    def on_frame(frame: dict[str, Any]) -> None:
        try:
            running = loop or asyncio.get_running_loop()
        except RuntimeError:
            _store(frame)
            return
        running.run_in_executor(None, _store, dict(frame))

    return on_frame
