"""`GET /v1/issues/preview?issue=<ref>`: the issue a run would plan, before it exists (#454).

Intake mock-up 1A (owner pick, 2026-10-02): the console shows the issue's
title, body, labels, state and comment count as the reference is typed, read
with the caller's TENANT's forge token, so a private repository previews for
exactly the people whose runs could clone it. Everything about the token is
`swarm_api.forge`'s; this route resolves the tenant, parses the reference and
logs the outcome's code -- never the token, never the forge's text.

A CLOSED issue is served with `state: "closed"`. The console warns; nothing
here refuses it, because planning a closed issue is sometimes the point.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from swarm_common.models import Tenant

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import ValidationFailed
from ..forge import ForgeReadError, IssueIsPullRequest, preview
from ..issueruns import auto_merge_availability, auto_merge_default
from ..repositories import registered_merge_policy
from ..validation import PullRequestReference, parse_issue_ref

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/issues", tags=["issues"])


@router.get("/preview")
def preview_issue(
    issue: str = Query(min_length=1, max_length=1024),
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    try:
        ref = parse_issue_ref(issue)
    except PullRequestReference as refused:
        raise IssueIsPullRequest(str(refused)) from None
    except ValueError as refused:
        raise ValidationFailed(str(refused), detail={"field": "issue"}) from None
    # The secret is named from the tenant id through the frozen
    # `Tenant.secret_name`; a tenant declared in Terraform may have no
    # Firestore document yet, and its secret is named the same either way.
    tenant = ctx.store.get_tenant(tenant_id) or Tenant(
        tenant_id=tenant_id,
        kind="group",
        principal=auth.tenant_principal or auth.email,
        created_at=ctx.now(),
    )
    try:
        body = preview(ref, tenant, tokens=ctx.forge_tokens, issues=ctx.forge)
    except ForgeReadError as refused:
        log.info("issue preview tenant=%s issue=%s outcome=%s", tenant_id, ref.short, refused.code)
        raise
    log.info("issue preview tenant=%s issue=%s outcome=ok", tenant_id, ref.short)
    # Whether POST /v1/runs would take `auto_merge: true` now, and why not:
    # the submit form draws the switch from this rather than guessing.
    return {
        "issue": body,
        "tenant_id": tenant_id,
        "auto_merge": auto_merge_availability(
            default=auto_merge_default(
                registered_merge_policy(ctx.db, ctx.now, tenant_id, ref.owner, ref.repo),
                bool(ctx.store.get_platform_settings().get("merge_by_default")),
            )
        ),
    }
