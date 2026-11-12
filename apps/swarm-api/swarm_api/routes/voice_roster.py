"""Voice it's agent roster route (docs/design/voice-it.md §3.12.6;
swarm_api/voice/roster.py).

    GET /v1/voice/projects/{project_id}/roster
        -> {project_id, rows: [{kind, id, title, state, done, total}]}

The page reads it once when it mounts or its stream reconnects, then applies
the follower's `roster.update` events; with no live session (a resumed
project before Start) the Projects pane reads it once.

WHO. The same guards as every Voice it route: the caller on the Voice it list
(403), and the project the caller's own in the console's selected workspace
(`tenant_scope`); another person's project, another tenant's and a missing
one are the same 404. The session grant does not reach it (it is on neither
of `voice.grant`'s lists): swarm-voice's follower reads the platform's own
routes instead (swarm_voice/follow.py).

It reads and answers; nothing here admits, reserves or runs work.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from ..deps import AppContext, get_context, tenant_scope
from ..voice import roster as vroster
from ..voice import store as vstore
from .voice import allowed_person

router = APIRouter(prefix="/v1/voice", tags=["voice"])


@router.get("/projects/{project_id}/roster")
async def project_roster(
    project_id: str,
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    vstore.check_id(project_id, "vp_", "project_id")
    roster = vroster.Roster(ctx.db, vstore.VoiceStore(ctx.db, now=ctx.now))
    rows = await run_in_threadpool(lambda: roster.rows(
        tenant_id=tenant_id, person=person, project_id=project_id))
    return {"project_id": project_id, "rows": rows}
