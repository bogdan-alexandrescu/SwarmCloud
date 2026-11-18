"""The read-back routes and the gate's dependency
(docs/design/voice-it-own-speech.md §7; swarm_api/voice/readback.py).

    POST /v1/voice/sessions/{session_id}/readbacks/answer  {text}
        -> {answer: "go" | "refuse" | null, readback, to_planner}
    POST /v1/voice/sessions/{session_id}/readbacks/{readback_id}/go       -> {readback, ran}
    POST /v1/voice/sessions/{session_id}/readbacks/{readback_id}/not-that -> {readback}
    GET  /v1/voice/sessions/{session_id}/readbacks/pending                -> {readback | null}

THE ANSWER ROUTE (§7.3) takes each final utterance, before the planner. GPT-
Live's finals reach swarm-voice on the sideband, and swarm-voice posts them
here under the session grant (`voice.grant.WRITE_ROUTES`; a grant answers only
for its own session); the person's own call (the console, the own-speech
relay) answers for a session of theirs. "go" confirms the pending read-back
and "not that" refuses it, by the exact phrases `readback.verdict` knows; any
other sentence, a bare "yes" among them, is recorded as heard and answered
`to_planner: true`, so swarm-voice hands it to the planner as a turn. When
the answer came from swarm-voice, swarm-voice re-sends the held call itself on
`answer: "go"`; otherwise swarm-api tells swarm-voice to (`_tell_voice`).

A TAP (§7.2 steps 3 and 4) is the person's: Go confirms and tells swarm-voice
to re-send; Not that refuses. Either is 409 once the read-back is no longer
pending -- answered, superseded or expired (§7.4) -- and nothing runs. When
swarm-voice no longer holds the call (a restart, or swarm-voice not
deployed), the read-back is `expired` and nothing runs.

THE GATE (`readback_gate`) is the dependency a gated act route declares. A
request under a session grant to a route `readback.GATED_ROUTES` names does
not run without a confirmed read-back: it is held and answered 202
`readback_pending` (`voice.grant.gate`). A person's own click is not gated:
the read-back guards what was heard, not what was clicked.

WHO. The person routes take the same guards as every Voice it route: the
caller on the Voice it list (403), the console's selected workspace through
`tenant_scope`, and a session of the caller's own there (else the same 404 as
a missing one). Nothing here logs what was said.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context
from ..errors import ValidationFailed
from ..voice import cards as vcards
from ..voice import grant as vgrant
from ..voice import readback as vreadback
from ..voice import sessions as vs
from ..voice import store as vstore
from ..voice import stream as vstream
from .voice import allowed_person, voice_client

log = logging.getLogger("swarm_api.voice.readback")

router = APIRouter(prefix="/v1/voice", tags=["voice"])

#: swarm-voice's internal route that re-sends a held call (swarm_voice/readback.py).
VOICE_READBACK_PATH = "/internal/sessions/{session_id}/readback"
VOICE_READBACK_TIMEOUT_SECONDS = 30.0


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=vs.MAX_TURN_CHARS)


def readbacks_for(ctx: AppContext) -> vreadback.Readbacks:
    """The read-backs, drawing each one's card through the session's store
    (`cards.write_card`), so its stream event reaches the page."""
    store = vstore.VoiceStore(ctx.db, now=ctx.now)

    def draw(where: vreadback.Where, card: dict[str, Any]) -> dict[str, Any]:
        return vcards.write_card(store, tenant_id=where.tenant_id, person=where.person,
                                 project_id=where.project_id, session_id=where.session_id,
                                 card=card)

    return vreadback.Readbacks(ctx.db, now=ctx.now, cards=draw)


async def readback_gate(request: Request, auth: AuthContext = Depends(current_auth),
                        ctx: AppContext = Depends(get_context)) -> None:
    """Hold a gated act under a session grant until its read-back is
    confirmed (§7.2). Declared by every gated route a grant reaches."""
    if auth.via != vgrant.VIA:
        return
    template = getattr(request.scope.get("route"), "path", None)
    method = request.method.upper()
    if not template or vreadback.classify(method, template) in (None, vreadback.AS_HEARD):
        return
    session = vgrant.session_of(request.headers.get(vgrant.SESSION_HEADER),
                                tenant_id=auth.tenant_id, person=auth.email)
    body = await request.body()
    await run_in_threadpool(
        vgrant.gate, ctx.db, readbacks_for(ctx), session, method=method, route=template,
        path_params=dict(request.path_params), body=body,
        presented=request.headers.get(vreadback.READBACK_HEADER))


def _where(ctx: AppContext, auth: AuthContext, person: str, session_id: str) -> vreadback.Where:
    vs_check(session_id)
    tenants = vstream.tenants_of(auth)
    if auth.via == vgrant.VIA:
        if auth.voice_session != session_id:
            raise vgrant.RouteNotGranted("a Voice it grant answers only for its own session")
        tenants = frozenset({auth.tenant_id})
    found = vstream.find_session(ctx.db, person=person, session_id=session_id, tenants=tenants)
    if found.state != vs.LIVE:
        raise vreadback.ReadbackNotPending(
            f"voice session {session_id!r} has ended; it has no read-back to answer")
    return vreadback.Where(tenant_id=found.tenant_id, person=person,
                           project_id=found.project_id, session_id=found.session_id)


def vs_check(session_id: str) -> None:
    if not session_id.startswith("vs_") or len(session_id) > 64:
        raise ValidationFailed("session_id is a Voice it session id",
                                  detail={"field": "session_id"})


def _tell_voice(client: Any, where: vreadback.Where, readback_id: str,
                readbacks: vreadback.Readbacks) -> bool:
    """Ask swarm-voice to re-send the confirmed call. False, with the
    read-back marked expired, when swarm-voice does not hold it."""
    ran = False
    send = getattr(client, "readback", None)
    post = getattr(client, "_post", None)
    if callable(send) or callable(post):
        try:
            if callable(send):
                ran = bool(send(where.session_id, readback_id))
            else:
                status, _ = post(
                    VOICE_READBACK_PATH.format(session_id=where.session_id),
                    {"readback_id": readback_id}, VOICE_READBACK_TIMEOUT_SECONDS)
                ran = status == 200
        except (OSError, ValueError) as exc:
            log.warning("voice read-back %s did not reach swarm-voice (%s)",
                        readback_id, type(exc).__name__)
            ran = False
    if not ran:
        readbacks.lost(where, readback_id)
    return ran


@router.post("/sessions/{session_id}/readbacks/answer")
async def answer_readback(
    session_id: str,
    body: Answer,
    person: str = Depends(allowed_person),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    client: Any = Depends(voice_client),
) -> dict:
    where = await run_in_threadpool(_where, ctx, auth, person, session_id)
    readbacks = readbacks_for(ctx)
    answered = await run_in_threadpool(readbacks.answer, where, body.text)
    if answered["answer"] == vreadback.GO and auth.via != vgrant.VIA:
        # swarm-voice re-sends on its own answer; anyone else's go is told to it.
        rid = answered["readback"]["readback_id"]
        ran = await run_in_threadpool(_tell_voice, client, where, rid, readbacks)
        answered = {**answered, "ran": ran,
                    "readback": vreadback.view(await run_in_threadpool(readbacks.get, where, rid))}
    return answered


@router.post("/sessions/{session_id}/readbacks/{readback_id}/go")
async def go_readback(
    session_id: str,
    readback_id: str,
    person: str = Depends(allowed_person),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    client: Any = Depends(voice_client),
) -> dict:
    where = await run_in_threadpool(_where, ctx, auth, person, session_id)
    readbacks = readbacks_for(ctx)
    await run_in_threadpool(readbacks.decide, where, readback_id, go=True)
    ran = await run_in_threadpool(_tell_voice, client, where, readback_id, readbacks)
    doc = await run_in_threadpool(readbacks.get, where, readback_id)
    return {"readback": vreadback.view(doc), "ran": ran}


@router.post("/sessions/{session_id}/readbacks/{readback_id}/not-that")
async def refuse_readback(
    session_id: str,
    readback_id: str,
    person: str = Depends(allowed_person),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    where = await run_in_threadpool(_where, ctx, auth, person, session_id)
    doc = await run_in_threadpool(readbacks_for(ctx).decide, where, readback_id, go=False)
    return {"readback": vreadback.view(doc)}


@router.get("/sessions/{session_id}/readbacks/pending")
async def pending_readback(
    session_id: str,
    person: str = Depends(allowed_person),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    vs_check(session_id)
    found = await run_in_threadpool(
        vstream.find_session, ctx.db, person=person, session_id=session_id,
        tenants=vstream.tenants_of(auth))
    where = vreadback.Where(tenant_id=found.tenant_id, person=person,
                            project_id=found.project_id, session_id=found.session_id)
    doc = await run_in_threadpool(readbacks_for(ctx).pending, where)
    return {"readback": vreadback.view(doc) if doc else None}
