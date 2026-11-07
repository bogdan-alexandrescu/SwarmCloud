"""Voice it › Telemetry's routes (docs/design/voice-it-own-speech.md §12.4;
voice/speech_telemetry.py).

    GET /v1/voice/telemetry/sessions?from=&to=&tz=[&scope=platform]
        -> {sessions: [summary, ...], scope, tz}
    GET /v1/voice/telemetry/sessions/{session_id}?tz=[&scope=platform]
        -> {session: summary, samples: [...], scope, tz}
    GET /v1/voice/telemetry/days?from=&to=&tz=[&scope=platform]
        -> {days: [{day, sessions, minutes, utterances, ...}, ...], scope, tz}

A session and a day both carry `machine_usd` (the ledger's Speech machine
line) and `stockouts` (starts the zone refused), each null with its
`*_reason` when its source cannot be read (speech_telemetry.py).
    GET /v1/voice/telemetry/live[?after=<seq>][&scope=platform]
        -> text/event-stream: the running session's samples, resuming by seq

WHO. Every route is for a platform admin or a Voice it person
(VOICE_IT_PEOPLE), the readers Voice it › Settings has; anyone else is 403.
`scope=platform` is every workspace's sessions and is `require_admin`'s,
checked before any parameter is validated, so a non-admin learns nothing from
a refused platform read. Without it the read is the caller's own workspace,
the tenant `tenant_scope` resolved (invariant 9): another workspace's session
is a 404, never a 403, and `live` shows nothing while another workspace's
session holds the machine.

DAYS ARE THE VIEWER'S. `from` and `to` are days, both included, in `tz`, the
IANA zone the console sends; a session's `day` and every row of `days` are
placed in that zone when read.

LIVE resumes like the card stream: `Last-Event-ID` or `after` is the last
sample's seq; each sample is one `sample` event with its seq as the id. With no
session of the caller's on the machine it answers one `idle` event and ends;
when the session it follows lets the lease go it sends `end` and ends.
`app.state.voice_telemetry_timing` ({poll_seconds, sleep, max_polls}) is a
test's clock.

L6 mounts this `router` beside the Voice it router (§14).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Callable

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..auth import AuthContext, require_admin
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import ValidationFailed
from ..voice import sessions as vs
from ..voice import speech_machine as sm
from ..voice import speech_telemetry as tel

router = APIRouter(prefix="/v1/voice/telemetry", tags=["voice"])

SCOPE_PLATFORM = "platform"
SCOPE_WORKSPACE = "workspace"
#: The (method, template) `require_admin` is handed: not a pool-admin route.
PLATFORM_ROUTE = ("GET", "/v1/voice/telemetry")
LIVE_POLL_SECONDS = 2.0


def reader(auth: AuthContext = Depends(current_auth),
           ctx: AppContext = Depends(get_context)) -> AuthContext:
    """The caller when they may read telemetry; 403 otherwise."""
    people = tuple(getattr(ctx.settings, "voice_people", ()) or ())
    if not (auth.is_admin or vs.is_allowed(people, auth.email)):
        raise vs.not_allowed()
    return auth


def _tenant(auth: AuthContext, scope: str | None, tenant_id: str) -> str | None:
    """None for the platform view (admins only), else the caller's workspace."""
    if scope == SCOPE_PLATFORM:
        require_admin(auth, PLATFORM_ROUTE)
        return None
    if scope not in (None, "", SCOPE_WORKSPACE):
        raise ValidationFailed("scope is workspace or platform.", detail={"field": "scope"})
    return tenant_id


def _telemetry(ctx: AppContext) -> tel.Telemetry:
    return tel.Telemetry(ctx.db, now=ctx.now)


def _scope_word(tenant_id: str | None) -> str:
    return SCOPE_PLATFORM if tenant_id is None else SCOPE_WORKSPACE


@router.get("/sessions")
async def list_sessions(
    start: str | None = Query(default=None, alias="from"),
    end: str | None = Query(default=None, alias="to"),
    tz: str | None = Query(default=None, description="the viewer's IANA zone"),
    scope: str | None = Query(default=None, description="workspace | platform"),
    auth: AuthContext = Depends(reader),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict[str, Any]:
    tenant = _tenant(auth, scope, tenant_id)
    span = tel.parse_span(start, end, tz)
    rows = await run_in_threadpool(_telemetry(ctx).sessions, span, tenant_id=tenant)
    return {"sessions": rows, "scope": _scope_word(tenant), "tz": tz}


@router.get("/sessions/{session_id}")
async def read_session(
    session_id: str,
    tz: str | None = Query(default=None, description="the viewer's IANA zone"),
    scope: str | None = Query(default=None, description="workspace | platform"),
    auth: AuthContext = Depends(reader),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict[str, Any]:
    tenant = _tenant(auth, scope, tenant_id)
    zone = tel.parse_zone(tz)
    answer = await run_in_threadpool(_telemetry(ctx).session, session_id,
                                     tenant_id=tenant, zone=zone)
    return {**answer, "scope": _scope_word(tenant), "tz": tz}


@router.get("/days")
async def list_days(
    start: str | None = Query(default=None, alias="from"),
    end: str | None = Query(default=None, alias="to"),
    tz: str | None = Query(default=None, description="the viewer's IANA zone"),
    scope: str | None = Query(default=None, description="workspace | platform"),
    auth: AuthContext = Depends(reader),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict[str, Any]:
    tenant = _tenant(auth, scope, tenant_id)
    span = tel.parse_span(start, end, tz)
    rows = await run_in_threadpool(_telemetry(ctx).days, span, tenant_id=tenant)
    return {"days": rows, "scope": _scope_word(tenant), "tz": tz}


def _event(name: str, data: Any, event_id: str | None = None) -> str:
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _holding(ctx: AppContext, tenant: str | None) -> dict[str, Any] | None:
    """The lease while a session the caller may see holds the machine."""
    snap = ctx.db.collection(sm.COLLECTION).document(sm.LEASE).get()
    lease = (snap.to_dict() or {}) if getattr(snap, "exists", False) else {}
    if lease.get("state") not in sm.HOLDING or not lease.get("session_id"):
        return None
    if tenant is not None and lease.get("tenant_id") != tenant:
        return None
    return lease


async def _live(ctx: AppContext, tenant: str | None, after: str | None, *,
                poll_seconds: float, sleep: Callable[[float], Any],
                max_polls: int | None) -> AsyncIterator[str]:
    lease = await run_in_threadpool(_holding, ctx, tenant)
    if lease is None:
        yield _event("idle", {"session_id": None})
        return
    session_id = str(lease["session_id"])
    generation = lease.get("generation")
    telemetry = _telemetry(ctx)
    yield _event("session", {"session_id": session_id, "generation": generation,
                             "state": lease.get("state")})
    polls = 0
    while True:
        rows = await run_in_threadpool(telemetry.samples, session_id,
                                       tenant_id=tenant, after=after)
        for row in rows:
            after = row["seq"]
            yield _event("sample", row, row["seq"])
        now = await run_in_threadpool(_holding, ctx, tenant)
        if now is None or now.get("session_id") != session_id \
                or now.get("generation") != generation:
            yield _event("end", {"session_id": session_id})
            return
        polls += 1
        if max_polls is not None and polls >= max_polls:
            return
        await sleep(poll_seconds)


@router.get("/live", response_model=None)
async def live(
    request: Request,
    after: str | None = Query(default=None),
    scope: str | None = Query(default=None, description="workspace | platform"),
    auth: AuthContext = Depends(reader),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> StreamingResponse:
    tenant = _tenant(auth, scope, tenant_id)
    cursor = tel.check_seq(request.headers.get("last-event-id") or after)
    timing = dict(getattr(request.app.state, "voice_telemetry_timing", None) or {})
    stream = _live(ctx, tenant, cursor,
                   poll_seconds=float(timing.get("poll_seconds", LIVE_POLL_SECONDS)),
                   sleep=timing.get("sleep", asyncio.sleep),
                   max_polls=timing.get("max_polls"))
    return StreamingResponse(stream, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
