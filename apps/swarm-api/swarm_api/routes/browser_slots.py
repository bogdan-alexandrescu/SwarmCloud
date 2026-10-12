"""The browser-session slot (docs/design/live-browser.md §5.5.1, lane LB-L8c).

    POST /v1/tasks/{task_id}/browser-session/slot   WORKER ONLY: swarm-api creates the
                                                    personal tenant's carry or login
                                                    secret and grants that tenant's
                                                    worker read and version-add on it

No `current_auth` and no `tenant_scope`, as L9b's worker routes: the tenant is
the worker's ID token checked against the body's, and the worker is the attempt
key's signature over the RAW body (`browser_logins.BROWSER_LOGIN_PURPOSE`).
`swarm_api.browser_slots` holds every check.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from starlette.concurrency import run_in_threadpool

from ..browser_logins import BrowserLogins
from ..browser_slots import BrowserSlots, SecretManagerSlotVault
from ..children import PROOF_HEADER, TIMESTAMP_HEADER
from ..deps import AppContext, get_context
from .browser_logins import browser_logins

router = APIRouter(tags=["browser-logins"])


def slot_vault(request: Request, ctx: AppContext = Depends(get_context)) -> Any:
    """Secret Manager, built on first use and kept on the app: building the
    app builds no client, and a test sets `app.state.browser_slot_vault`."""
    vault = getattr(request.app.state, "browser_slot_vault", None)
    if vault is None:
        vault = SecretManagerSlotVault(ctx.settings.project_id)
        request.app.state.browser_slot_vault = vault
    return vault


def browser_slots(
    ctx: AppContext = Depends(get_context),
    logins: BrowserLogins = Depends(browser_logins),
    vault: Any = Depends(slot_vault),
) -> BrowserSlots:
    return BrowserSlots(settings=ctx.settings, db=ctx.db, logins=logins, vault=vault, now=ctx.now)


@router.post("/v1/tasks/{task_id}/browser-session/slot")
async def browser_session_slot(
    task_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    service: BrowserSlots = Depends(browser_slots),
) -> dict:
    raw = await request.body()
    return await run_in_threadpool(
        lambda: service.slot(
            serverless_authorization or authorization, task_id, raw,
            path=request.url.path, proof=proof, timestamp=timestamp,
        )
    )
