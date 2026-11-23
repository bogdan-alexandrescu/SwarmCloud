"""A Voice it session's record (voice/record.py): its transcript, downloaded
as one text file, and its end-of-session summary.

    GET /v1/voice/sessions/{session_id}/transcript
        -> 200 text/plain, `Content-Disposition: attachment;
           filename="voice-session-<id>.txt"`: one line per side of each turn,
           "person: ..." or "voice: ...", redacted
    GET /v1/voice/sessions/{session_id}/summary
        -> {session_id, state: none | pending | written | capped | failed,
            text (written only), at (written only)}

Both are the session's person's alone, under the same checks as every other
read of a session: Voice it open to the caller (`allowed_person`), the
selected workspace (`tenant_scope`), and the session found under the caller's
own projects in it, so another person's or workspace's session is the same 404
as a missing one.

AN ENDED SESSION WITH NO SUMMARY YET. Ending a session from the page starts
its summary (routes/voice.py `end_session`); a session ended another way (its
speech machine, the abandoned sweep) has none claimed, so the first read of
its summary claims and starts it, once, under the same caps.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from ..deps import AppContext, get_context, tenant_scope
from ..voice import record as vrecord
from ..voice import sessions as vs
from . import voice_speech as rvs
from .voice import allowed_person, voice_client

router = APIRouter(prefix="/v1/voice", tags=["voice"])


def summaries_for(request: Request, ctx: AppContext, client: Any) -> vrecord.Summaries:
    """The summaries, over the ledger the caps are enforced on
    (voice_speech.budget_ledger) and `app.state.voice_summary_spawn` when one
    is set (a test's), else a daemon thread."""
    return vrecord.Summaries(ctx.db, client=client, now=ctx.now,
                             ledger=lambda: rvs.budget_ledger(request, ctx),
                             spawn=getattr(request.app.state, "voice_summary_spawn", None))


@router.get("/sessions/{session_id}/transcript", response_model=None)
async def download_transcript(
    session_id: str,
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> Response:
    text = await run_in_threadpool(vrecord.transcript_file, ctx.db, tenant_id=tenant_id,
                                   person=person, session_id=session_id)
    return Response(content=text, media_type="text/plain; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="voice-session-{session_id}.txt"',
        "Cache-Control": "no-store"})


@router.get("/sessions/{session_id}/summary")
async def read_summary(
    session_id: str,
    request: Request,
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    client: Any = Depends(voice_client),
) -> dict:
    def _read() -> dict[str, Any]:
        view = vrecord.summary_view(ctx.db, tenant_id=tenant_id, person=person, session_id=session_id)
        if view["state"] != vrecord.NONE:
            return view
        project_id, data = vrecord.find_session(ctx.db, tenant_id=tenant_id, person=person,
                                                session_id=session_id)
        if data.get("state") != vs.ENDED or client is None:
            return view
        summaries_for(request, ctx, client).after_end(
            tenant_id=tenant_id, person=person, project_id=project_id, session_id=session_id,
            cause=str(data.get("end_cause") or ""))
        return vrecord.summary_view(ctx.db, tenant_id=tenant_id, person=person, session_id=session_id)

    return await run_in_threadpool(_read)
