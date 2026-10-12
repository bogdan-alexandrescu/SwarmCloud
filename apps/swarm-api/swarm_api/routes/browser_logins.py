"""Named browser logins and the workflow carry (docs/design/live-browser.md §5.5.1, lane LB-L9b).

    GET    /v1/browser-logins                         the signed-in person's own logins,
                                                      never a value; anyone else 404
    POST   /v1/tasks/{task_id}/browser-login          "Save as login": {name, expires_in_days}
                                                      (default 14, at most 30) -> pending
    DELETE /v1/browser-logins/{name}                  revoke: every secret version destroyed
    POST   /v1/tasks/{task_id}/browser-login/pending  WORKER ONLY: the saves to store before
                                                      the wipe
    POST   /v1/tasks/{task_id}/browser-login/report   WORKER ONLY: {event: stored | used |
                                                      refreshed, name, site?}
    POST   /v1/admin/browser-logins/carry-sweep       deletes the carry of every terminal
                                                      personal workflow; the rollup sweeper
                                                      or an admin

The worker routes declare no `current_auth` and no `tenant_scope`, as the
hand-off's do (routes/tasks.py): the tenant is the worker's ID token checked
against the body's, and the worker is the attempt key's signature over the RAW
body, under its own purpose (`browser_logins.BROWSER_LOGIN_PURPOSE`).
`swarm_api.browser_logins` holds every check.

NO ROUTE RETURNS OR ACCEPTS A VALUE. The jar moves gate to gate and into
Secret Manager under the worker's identity; every body here forbids the fields
it does not name, so a cookie posted in any field is a 422 that does not echo it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request, status
from starlette.concurrency import run_in_threadpool

from ..auth import AuthContext
from ..browser_logins import BrowserLogins, CarryReaper, SaveLogin, SecretManagerVault
from ..children import PROOF_HEADER, TIMESTAMP_HEADER, ChildService
from ..deps import AppContext, admin_auth, current_auth, get_context, submission_scope, tenant_scope
from .children import child_service

router = APIRouter(tags=["browser-logins"])


def browser_vault(request: Request, ctx: AppContext = Depends(get_context)) -> Any:
    """Secret Manager, built on first use and kept on the app: building the
    app builds no client, and a test sets `app.state.browser_vault`."""
    vault = getattr(request.app.state, "browser_vault", None)
    if vault is None:
        vault = SecretManagerVault(ctx.settings.project_id)
        request.app.state.browser_vault = vault
    return vault


def browser_logins(
    ctx: AppContext = Depends(get_context),
    children: ChildService = Depends(child_service),
    vault: Any = Depends(browser_vault),
) -> BrowserLogins:
    return BrowserLogins(
        settings=ctx.settings, db=ctx.db, store=ctx.store, children=children, vault=vault,
        now=ctx.now,
    )


# -- the person's routes ---------------------------------------------------


@router.get("/v1/browser-logins")
def list_browser_logins(
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    service: BrowserLogins = Depends(browser_logins),
) -> dict:
    return {"items": service.list(tenant_id, email=auth.email, member_scope=auth.member_scope)}


@router.post("/v1/tasks/{task_id}/browser-login", status_code=status.HTTP_202_ACCEPTED)
def save_browser_login(
    task_id: str,
    body: SaveLogin,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    service: BrowserLogins = Depends(browser_logins),
) -> dict:
    """Accepted, not done: the worker stores the jar and reports `stored`."""
    return service.save(tenant_id, task_id, body, email=auth.email,
                        member_scope=auth.member_scope, submitted_by=submitted_by)


@router.delete("/v1/browser-logins/{name}")
def revoke_browser_login(
    name: str,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    service: BrowserLogins = Depends(browser_logins),
) -> dict:
    return service.revoke(tenant_id, name, email=auth.email, member_scope=auth.member_scope)


# -- the worker's routes -----------------------------------------------------


@router.post("/v1/tasks/{task_id}/browser-login/pending")
async def pending_browser_logins(
    task_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    service: BrowserLogins = Depends(browser_logins),
) -> dict:
    raw = await request.body()
    items = await run_in_threadpool(
        lambda: service.pending(
            serverless_authorization or authorization, task_id, raw,
            path=request.url.path, proof=proof, timestamp=timestamp,
        )
    )
    return {"items": items}


@router.post("/v1/tasks/{task_id}/browser-login/report")
async def report_browser_login(
    task_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    service: BrowserLogins = Depends(browser_logins),
) -> dict:
    raw = await request.body()
    return await run_in_threadpool(
        lambda: service.report(
            serverless_authorization or authorization, task_id, raw,
            path=request.url.path, proof=proof, timestamp=timestamp,
        )
    )


# -- the carry sweep -----------------------------------------------------------


@router.post("/v1/admin/browser-logins/carry-sweep")
def carry_sweep(
    _auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
    vault: Any = Depends(browser_vault),
) -> dict:
    return CarryReaper(db=ctx.db, vault=vault, now=ctx.now).sweep().to_api()
