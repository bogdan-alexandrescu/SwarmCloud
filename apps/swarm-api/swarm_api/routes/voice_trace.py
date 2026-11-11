"""Voice it's debug trace (voice/trace.py; the owner, 10 November 2026).

    POST /v1/voice/sessions/{session_id}/trace
         {engine, session_events, turn}      -> {turn}      swarm-voice only
    GET  /v1/voice/trace/sessions[?scope=]   -> {sessions, scope}
    GET  /v1/voice/trace/sessions/{session_id}[?scope=]
                                             -> {session_id, engine, started,
                                                 minutes, turns, events, scope}

THE WRITE is swarm-voice's alone: it is on the session grant's write list
(`voice.grant.WRITE_ROUTES`), so the caller has already proved swarm-voice's
identity and a live session's grant, and here it must have come through that
grant (`AuthContext.via`) and name its own session; a person's own sign-in is
403, as is another session's id. The tenant, the person and the project come
from the session document (`stream.find_session`). Each kept turn also goes to
the session's card stream as one `trace.turn` event (voice/stream.py), so Talk
shows the turn's line as it finishes. swarm-voice sends a trace only for a
session that started with debug mode on; with it off nothing reaches here.

THE READS follow Voice it › Telemetry's rule exactly (routes/voice_telemetry.py,
`reader` and `_tenant`, imported, not restated): the caller is a Voice it person
(VOICE_IT_PEOPLE, else the platform owner) or a platform admin, else 403; the
view is the selected workspace's (`tenant_scope`), or with `scope=platform`
every workspace's, for a platform admin only (`require_admin`). A session of
another workspace is the same 404 as one with no trace.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from starlette.concurrency import run_in_threadpool

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..voice import grant as vgrant
from ..voice import store as vstore
from ..voice import stream as vstream
from ..voice import trace as vtrace
from .voice_telemetry import _scope_word, _tenant, reader

log = logging.getLogger(__name__)

#: swarm-voice's write, beside the session's other grant writes.
write_router = APIRouter(prefix="/v1/voice/sessions", tags=["voice"])
#: The reads, beside Telemetry's.
router = APIRouter(prefix="/v1/voice/trace", tags=["voice"])

WRITE_ROUTE = ("POST", "/v1/voice/sessions/{session_id}/trace")


def _started(db: Any, found: vstream.FoundSession) -> datetime | None:
    snap = (db.collection(vstore.PROJECTS).document(found.project_id)
            .collection(vstore.SESSIONS).document(found.session_id).get())
    created = (snap.to_dict() or {}).get("created_at") if snap.exists else None
    return created if isinstance(created, datetime) else None


@write_router.post("/{session_id}/trace")
async def write_trace(
    session_id: str,
    body: dict[str, Any] = Body(...),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    if auth.via != vgrant.VIA:
        raise vgrant.RouteNotGranted("only swarm-voice writes a Voice it trace")
    if auth.voice_session != session_id:
        raise vgrant.RouteNotGranted("a Voice it grant writes only to its own session")
    vstore.check_id(session_id, "vs_", "session_id")
    person = auth.email.strip().lower()

    def _write() -> dict[str, Any]:
        found = vstream.find_session(ctx.db, person=person, session_id=session_id,
                                     tenants=frozenset({auth.tenant_id}))
        kept = vtrace.Trace(ctx.db, now=ctx.now).record(
            tenant_id=found.tenant_id, person=person, project_id=found.project_id,
            session_id=found.session_id, engine=body.get("engine"),
            session_events=body.get("session_events", {}), turn=body.get("turn"),
            started_at=_started(ctx.db, found))
        vstore.VoiceStore(ctx.db, now=ctx.now).append_event(
            tenant_id=found.tenant_id, person=person, project_id=found.project_id,
            session_id=found.session_id, event=vtrace.stream_event(kept))
        log.info("voice trace kept session=%s turn=%s", found.session_id, kept["n"])
        return {"turn": vtrace.served_turn(kept)}

    return await run_in_threadpool(_write)


@router.get("/sessions")
async def list_traced_sessions(
    scope: str | None = Query(default=None, description="workspace | platform"),
    auth: AuthContext = Depends(reader),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict[str, Any]:
    tenant = _tenant(auth, scope, tenant_id)
    rows = await run_in_threadpool(vtrace.Trace(ctx.db, now=ctx.now).sessions, tenant_id=tenant)
    return {"sessions": rows, "scope": _scope_word(tenant)}


@router.get("/sessions/{session_id}")
async def read_trace(
    session_id: str,
    scope: str | None = Query(default=None, description="workspace | platform"),
    auth: AuthContext = Depends(reader),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict[str, Any]:
    tenant = _tenant(auth, scope, tenant_id)
    answer = await run_in_threadpool(vtrace.Trace(ctx.db, now=ctx.now).session, session_id,
                                     tenant_id=tenant)
    return {**answer, "scope": _scope_word(tenant)}


__all__ = ["WRITE_ROUTE", "router", "write_router"]
