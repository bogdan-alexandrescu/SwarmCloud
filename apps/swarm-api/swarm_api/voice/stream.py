"""The Voice it card stream: a session's cards and events, in sequence order,
as server-sent events (docs/design/voice-it.md §3.5, §3.7, §10 phase 2).

    GET /v1/voice/sessions/{session_id}/events[?after=N]
        text/event-stream: `card.upsert`, `card.remove`, `session.state`,
        `meter.update`, the stage's `visual.upsert`, `note.upsert` and
        `focus` (visuals.py), and, while debug mode is on, `trace.turn`
        (trace.py), and `voice.tier.reopen` (a spent Live key's next tier,
        sessions.py THE REOPEN), the work feed's `work.step`,
        `work.delegation`, `roster.update` and `paused` (workfeed.py), each
        with `id: <seq>`

FIRESTORE IS THE MEETING POINT, NOT A RELAY. Every event is stored before it
is sent (`store.VoiceStore.append_event`, numbered from the session's
`event_seq` counter), and this module only reads the session's `events`
collection by sequence, about once a second. swarm-voice's writes land on
whichever swarm-api instance takes them and the stream is served by whichever
holds the browser's request; neither needs the other.

RESUME. Every frame carries its sequence as the SSE `id`, so `EventSource`
sends `Last-Event-ID` by itself on a reconnect and the stream starts after
it; `?after=N` does the same for a first connection. A reconnect loses
nothing and repeats nothing.

IT ENDS ITSELF BEFORE 600 SECONDS. swarm-api's Cloud Run request timeout is
660 s, and a stream it cuts may be cut mid-frame. The stream stops polling
once another poll would pass `MAX_SECONDS` (540 s, "about nine minutes"),
which leaves a full minute under 600 s for a slow last read, and the
browser reconnects from the last id. It also ends once the session has ended
and every event before that is sent; the reconnect then answers 204, which
tells `EventSource` to stop.

THE TENANT COMES FROM THE SESSION, NEVER FROM THE REQUEST. `EventSource`
sends no custom header, so the URL carries the session id and nothing else.
`find_session` looks for the session among the caller's own projects (the
person is the verified email), takes the tenant from the session document,
and answers it only when that tenant is one the caller verifiably belongs to
on this request (`AuthContext.tenant_choices`, or the tenant it resolved to).
Another tenant's session, another person's, and a missing one are the same
404 (invariant 9).

NOTHING SECRET. A frame carries the event's own fields less the tenant and
the person; cards were checked on write (`cards.validate`, which refuses a
credential-shaped string). The stage's events are cut again to what the
visual and note reads serve (`visuals.visual_view`, `visuals.note_view`; a
focus is its visual id, anchor and caption), and a reopen is its tier's
name and nothing else (REOPEN_TIERS: never a key or an account), so a visual's frame names its
body's key, never the body, and a field a writer added is never sent. The
work feed's four are cut to their whitelists (WORK_TYPES): a step is its
phrase from the fixed table, its method, its class and its ids; a delegation
its id and state; a roster row its kind, id, state and counts; a pause its
voice state and cause. A row that does not fit is skipped, never sent in
part. Logs name session ids and sequence numbers only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable

import anyio.to_thread
from google.cloud.firestore_v1.base_query import FieldFilter

from ..errors import NotFound, ValidationFailed
from . import cards as vcards
from . import sessions as vs
from . import store as vstore
from . import trace as vtrace
from . import visuals as vvis
from . import workfeed as vwork

log = logging.getLogger(__name__)

#: The events the stream carries (§3.5). Anything else in the log is skipped
#: and logged by sequence, never sent.
SESSION_STATE = "session.state"
METER_UPDATE = "meter.update"
#: The stage's three (visuals.py writes them): a visual drawn or drawn again,
#: a note pinned, edited, resolved or made work, and the planner's focus.
STAGE_TYPES = (vvis.VISUAL_UPSERT, vvis.NOTE_UPSERT, vvis.FOCUS)
#: A spent Live key's next tier is held: the console offers again (sessions.py).
TIER_REOPEN = vs.REOPEN_EVENT
#: The work feed's (§3.12.5, §3.12.6, §3.12.7): each framed through its
#: whitelist in `work_payload`, never as stored.
WORK_TYPES = (vwork.WORK_STEP, vwork.WORK_DELEGATION, vwork.ROSTER_UPDATE, vwork.PAUSED)
EVENT_TYPES = (vcards.CARD_UPSERT, vcards.CARD_REMOVE, SESSION_STATE, METER_UPDATE,
               *STAGE_TYPES, vtrace.TRACE_TURN, TIER_REOPEN, *WORK_TYPES)

#: What a `session.state` event may say.
VOICE_STATES = ("listening", "thinking", "speaking", "working", "ended")

#: swarm-api's Cloud Run request timeout (terraform/infra) is 660 s; the
#: stream must be over well before 600 s. Nine minutes, the design's figure.
MAX_SECONDS = 540.0
#: One indexed query a second per open stream (§3.5: about 3,600 reads an hour).
POLL_SECONDS = 1.0
#: A comment line this often keeps a proxy from closing an idle stream.
KEEPALIVE_SECONDS = 15.0
#: How long the browser waits before it reconnects (`retry:`), in ms.
RETRY_MS = 1000

#: The fields a stored event carries that a frame never does.
_PRIVATE = ("tenant_id", "person")


# ---- finding the session ----------------------------------------------------


@dataclass(frozen=True)
class FoundSession:
    tenant_id: str
    project_id: str
    session_id: str
    state: str


def tenants_of(auth: Any) -> frozenset[str]:
    """The tenants this caller verifiably acts in on this request: every one
    `tenant_choices` lists (what X-Swarm-Tenant may select) and the one the
    request resolved to. Never a value the caller typed."""
    found = {str(t) for t, _principal in (getattr(auth, "tenant_choices", ()) or ())}
    if getattr(auth, "tenant_id", ""):
        found.add(str(auth.tenant_id))
    return frozenset(found)


def _missing(session_id: str) -> NotFound:
    return NotFound(f"voice session {session_id!r} not found")


def find_session(db: Any, *, person: str, session_id: str,
                 tenants: Iterable[str]) -> FoundSession:
    """The caller's session, with the tenant its document names; `NotFound`
    when no project of this person's holds it, when the session or its
    project is another person's or another tenant's, or when the session's
    tenant is not one the caller belongs to."""
    vstore.check_id(session_id, "vs_", "session_id")
    allowed = frozenset(tenants)
    projects = (db.collection(vs.PROJECTS)
                .where(filter=FieldFilter("person", "==", person))
                .stream())
    owners: dict[str, tuple[str, dict[str, Any]]] = {}
    refs = []
    for project in projects:
        data = project.to_dict() or {}
        if data.get("person") != person:
            continue
        ref = db.collection(vs.PROJECTS).document(project.id) \
            .collection(vs.SESSIONS).document(session_id)
        owners[ref.path] = (project.id, data)
        refs.append(ref)
    # get_all does not keep the order it was given: match by path.
    for snap in (db.get_all(refs) if refs else ()):
        if not snap.exists:
            continue
        session = snap.to_dict() or {}
        project_id, project = owners[snap.reference.path]
        tenant_id = session.get("tenant_id")
        if session.get("person") == person and tenant_id \
                and project.get("tenant_id") == tenant_id and tenant_id in allowed:
            return FoundSession(tenant_id=str(tenant_id), project_id=project_id,
                                session_id=session_id, state=str(session.get("state") or ""))
    raise _missing(session_id)


def resume_after(last_event_id: str | None, after: int | None) -> int:
    """The sequence to start after: `Last-Event-ID` (what `EventSource` sends
    on a reconnect) when present, else `?after=`, else 0."""
    raw = (last_event_id or "").strip()
    if raw:
        if not raw.isdigit() or len(raw) > 12:
            raise ValidationFailed("Last-Event-ID is a sequence number",
                                   detail={"field": "Last-Event-ID"})
        return int(raw)
    return int(after or 0)


# ---- one frame --------------------------------------------------------------


def _id(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def stage_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    """A stage event's payload as the reads serve it, or None when the row is
    not one `visuals.Visuals` writes (no id, a kind or state it never has)."""
    kind = event.get("type")
    if kind == vvis.VISUAL_UPSERT:
        visual = event.get("visual")
        if not isinstance(visual, dict) or not _id(visual.get("visual_id")) \
                or visual.get("kind") not in vvis.KINDS:
            return None
        return {"visual": vvis.visual_view(visual)}
    if kind == vvis.NOTE_UPSERT:
        note = event.get("note")
        if not isinstance(note, dict) or not _id(note.get("note_id")) \
                or not _id(note.get("visual_id")) or note.get("state") not in vvis.NOTE_STATES:
            return None
        return {"note": vvis.note_view({k: v for k, v in note.items() if k not in _PRIVATE})}
    if kind == vvis.FOCUS:
        anchor, caption = event.get("anchor"), event.get("caption")
        if not _id(event.get("visual_id")) or not (anchor is None or isinstance(anchor, dict)) \
                or not (caption is None or isinstance(caption, str)):
            return None
        return {"visual_id": event["visual_id"], "anchor": anchor, "caption": caption}
    return None


def work_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    """A work feed event cut to its whitelist, or None when it does not fit."""
    kind = event.get("type")
    if kind == vwork.WORK_STEP:
        return vwork.step_payload(event)
    if kind == vwork.WORK_DELEGATION:
        return vwork.delegation_payload(event)
    if kind == vwork.ROSTER_UPDATE:
        return vwork.roster_payload(event)
    if kind == vwork.PAUSED:
        return vwork.paused_payload(event, states=vs.VOICE_REPORTS, causes=vs.REOPEN_CAUSES)
    return None


def frame(event: dict[str, Any], *, session_id: str) -> str | None:
    """One stored event as an SSE frame, or None when it is not one the
    stream carries (logged by sequence)."""
    seq = event.get("seq")
    kind = event.get("type")
    if not isinstance(seq, int) or kind not in EVENT_TYPES:
        log.warning("voice stream skipped an event session=%s seq=%s", session_id, seq)
        return None
    if kind == vcards.CARD_UPSERT:
        card = event.get("card")
        if not isinstance(card, dict) or card.get("kind") not in vcards.BY_KIND:
            log.warning("voice stream skipped a card of no known kind session=%s seq=%s",
                        session_id, seq)
            return None
    if kind == SESSION_STATE and event.get("state") not in VOICE_STATES:
        log.warning("voice stream skipped a session state session=%s seq=%s", session_id, seq)
        return None
    if kind == vtrace.TRACE_TURN:
        # Debug mode's turn line: its number, the cards drawn and refused and
        # the no-cards code, as trace.stream_event kept them, and nothing else.
        if not isinstance(event.get("n"), int):
            log.warning("voice stream skipped a trace turn session=%s seq=%s", session_id, seq)
            return None
        data = {"type": kind, "seq": seq, "at": event.get("at"),
                **{k: event.get(k) for k in ("n", "drawn", "refused", "none")}}
    elif kind == TIER_REOPEN:
        # The tier's name, from the closed list, and nothing a writer added.
        if event.get("tier") not in vs.REOPEN_TIERS:
            log.warning("voice stream skipped a tier reopen session=%s seq=%s", session_id, seq)
            return None
        data = {"type": kind, "seq": seq, "at": event.get("at"), "tier": event["tier"]}
    elif kind in WORK_TYPES:
        work = work_payload(event)
        if work is None:
            log.warning("voice stream skipped a work event session=%s seq=%s", session_id, seq)
            return None
        data = {"type": kind, "seq": seq, "at": event.get("at"), **work}
    elif kind in STAGE_TYPES:
        payload = stage_payload(event)
        if payload is None:
            log.warning("voice stream skipped a stage event session=%s seq=%s", session_id, seq)
            return None
        data = {"type": kind, "seq": seq, "at": event.get("at"), **payload}
    else:
        data = {k: v for k, v in event.items() if k not in _PRIVATE}
    data["at"] = vcards._iso(data.get("at"))
    body = json.dumps(data, default=str, separators=(",", ":"))
    return f"id: {seq}\nevent: {kind}\ndata: {body}\n\n"


# ---- the stream -------------------------------------------------------------


async def _thread(fn: Callable[..., Any], **kwargs: Any) -> Any:
    return await anyio.to_thread.run_sync(lambda: fn(**kwargs))


class CardStream:
    """One open stream. Iterate it for SSE text; it ends by itself.

    `clock` is monotonic seconds and `sleep` waits; both are injected so a
    test can run nine minutes in no time."""

    def __init__(self, store: vstore.VoiceStore, db: Any, *, found: FoundSession,
                 person: str, after: int,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 max_seconds: float = MAX_SECONDS,
                 poll_seconds: float = POLL_SECONDS,
                 keepalive_seconds: float = KEEPALIVE_SECONDS) -> None:
        self._store = store
        self._db = db
        self._found = found
        self._person = person
        self.after = after
        self._clock = clock
        self._sleep = sleep
        self._max = max_seconds
        self._poll = poll_seconds
        self._keepalive = keepalive_seconds
        self.ended_by: str | None = None

    def _session_state(self) -> str | None:
        """The session's state, or None when it is no longer this person's in
        this tenant (a deleted project)."""
        snap = (self._db.collection(vs.PROJECTS).document(self._found.project_id)
                .collection(vs.SESSIONS).document(self._found.session_id).get())
        data = snap.to_dict() if snap.exists else None
        if not data or data.get("tenant_id") != self._found.tenant_id \
                or data.get("person") != self._person:
            return None
        return str(data.get("state") or "")

    def _events(self) -> list[dict[str, Any]]:
        return self._store.events_after(
            tenant_id=self._found.tenant_id, person=self._person,
            project_id=self._found.project_id, session_id=self._found.session_id,
            after=self.after)

    def pending(self) -> list[dict[str, Any]]:
        """The events after `after`, read once (the route's 204 check)."""
        return self._events()

    async def __aiter__(self) -> AsyncIterator[str]:
        start = self._clock()
        last_sent = start
        yield f"retry: {RETRY_MS}\n\n"
        while True:
            try:
                state = await _thread(self._session_state)
                events = await _thread(self._events) if state is not None else []
            except NotFound:
                state, events = None, []
            for event in events:
                text = frame(event, session_id=self._found.session_id)
                seq = event.get("seq")
                if isinstance(seq, int) and seq > self.after:
                    self.after = seq
                if text is not None:
                    last_sent = self._clock()
                    yield text
            if state is None:
                self.ended_by = "gone"
                return
            if state == vs.ENDED:
                self.ended_by = "session_ended"
                return
            now = self._clock()
            if now - start + self._poll >= self._max:
                self.ended_by = "time"
                log.info("voice stream closed for reconnect session=%s after=%s",
                         self._found.session_id, self.after)
                return
            if now - last_sent >= self._keepalive:
                last_sent = now
                yield ": keep-alive\n\n"
            await self._sleep(self._poll)
