"""The QA identity's routes (docs/design/qa-identity.md §4.3; lane QI-API, part of #1649).

Three kinds of caller:

  * THE WORKER asks for a credential for its live attempt
    (`POST /v1/attempts/{attempt_id}/qa-identity`). No `current_auth`: the
    tenant is its ID token checked against the body's, and the worker is the
    attempt key's signature over the raw body, as for the egress ticket. The
    answer is `Cache-Control: no-store` and the route is in
    `main.UNCAPTURED_ROUTES`.
  * A PLATFORM ADMIN lists, grants and revokes (`/v1/admin/qa-identities`,
    `admin_auth`).
  * ANY MEMBER of a workspace, whatever its role, reads whether its own
    workspace is granted (`GET /v1/qa-identities/mine`), so the submit form
    offers the option only where it would be accepted. A GET is a `viewer`
    read in `roles.required_role`, so every role reaches it.

Nothing here echoes a request header, and no message carries a value from the
request: the codes and sentences are `swarm_api.qa_identity`'s.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from starlette.concurrency import run_in_threadpool

from ..auth import AuthContext
from ..children import PROOF_HEADER, TIMESTAMP_HEADER, ChildService
from ..deps import AppContext, admin_auth, get_context, tenant_scope
from ..errors import NotFound
from ..qa_identity import (
    IDENTITY,
    IamJwtSigner,
    QaIdentityGrants,
    QaIdentityService,
    admin_list,
    mine,
)
from ..schemas import StrictModel
from .children import child_service

router = APIRouter(prefix="/v1", tags=["qa-identity"])

#: The route, as the app mounts it. `main.UNCAPTURED_ROUTES` names it.
ISSUE_ROUTE = "/v1/attempts/{attempt_id}/qa-identity"


class GrantBody(StrictModel):
    #: Checked, 1-200 characters, by `qa_identity.check_reason`, so the refusal
    #: is the module's own sentence rather than a validation list.
    reason: Any = None


def _signer(request: Request) -> Any:
    """The app's signer: a test's, or IAM Credentials, built once on first use."""
    signer = getattr(request.app.state, "qa_identity_signer", None)
    if signer is None:
        signer = IamJwtSigner()
        request.app.state.qa_identity_signer = signer
    return signer


def _known(identity: str) -> None:
    if identity != IDENTITY:
        raise NotFound("no such QA identity")


@router.post("/attempts/{attempt_id}/qa-identity")
async def issue_qa_identity(
    attempt_id: str,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    ctx: AppContext = Depends(get_context),
    children: ChildService = Depends(child_service),
) -> dict:
    """WORKER ONLY: the QA credential for this live attempt, proven and fenced
    as the egress ticket is, re-reading the tenant's grant every time."""
    raw = await request.body()
    service = QaIdentityService(settings=ctx.settings, db=ctx.db, children=children,
                                signer=_signer(request), now=ctx.now)
    answer = await run_in_threadpool(
        lambda: service.issue(
            serverless_authorization or authorization,
            attempt_id,
            raw,
            path=request.url.path,
            proof=proof,
            timestamp=timestamp,
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return answer


@router.get("/qa-identities/mine")
def get_mine(
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """This workspace's QA identity: configured, and granted or not."""
    return mine(ctx.settings, ctx.db, tenant_id)


@router.get("/admin/qa-identities")
def list_qa_identities(
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return admin_list(ctx.settings, ctx.db)


@router.put("/admin/qa-identities/{identity}/grants/{tenant_id}")
def grant_qa_identity(
    identity: str,
    tenant_id: str,
    body: GrantBody,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    _known(identity)
    return QaIdentityGrants(ctx.db, ctx.settings, now=ctx.now).grant(
        tenant_id, reason=body.reason, by=auth.email)


@router.delete("/admin/qa-identities/{identity}/grants/{tenant_id}")
def revoke_qa_identity(
    identity: str,
    tenant_id: str,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    _known(identity)
    return QaIdentityGrants(ctx.db, ctx.settings, now=ctx.now).revoke(tenant_id, by=auth.email)
