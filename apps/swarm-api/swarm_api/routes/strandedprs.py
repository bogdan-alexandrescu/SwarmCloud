"""Stranded pull requests (part of #295): SwarmCloud-opened PRs nothing is going to merge.

    GET  /v1/stranded-prs                         the last sweep's rows: the caller's tenant;
                                                  an admin, every tenant they belong to
    POST /v1/admin/stranded-prs/sweep?tenant_id=  classify, store, log; `redrive` submits merges

What is classified, how, and why is `swarm_api.strandedprs`. This module
decides only WHO may read and run it.

THE GET READS NO FORGE. It serves the document the last sweep wrote, so a
console polling it spends nobody's GitHub rate limit, and its rows are only
ever the named tenant's (invariant 9): a member reads their own tenant's, and
naming any other is refused. An admin reads every tenant they are a member
of (`AuthContext.tenant_choices`), or any one tenant by `tenant_id`, which an
admin may read on every other admin surface too.

THE SWEEP is the per-tenant Cloud Scheduler job `stranded_pr_sweep`
(terraform/modules/scheduler/jobs.tf), as the rollup sweeper -- admitted to
this route by name (`auth.ROLLUP_SWEEPER_ROUTES`) -- every 30 minutes, with
`redrive: false`. A sweeper call asking for `redrive: true` is REFUSED: a
scheduled identity's token, leaked, must not be able to submit merges. An
admin may run it either way.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from pydantic import StrictBool

from ..auth import AuthContext
from ..deps import AppContext, admin_auth, current_auth, get_context, tenant_scope
from ..errors import Forbidden
from ..schemas import StrictModel
from ..strandedprs import LOG_EVERY, REASONS, REMEDIES, STRANDED_AFTER, stored_rows, sweep_tenant

router = APIRouter(tags=["stranded-prs"])


class StrandedSweepRequest(StrictModel):
    #: Submit a `merge_pr` workflow for each `no_merge_step` / `behind` row
    #: whose checks are green: never a held one, never a red one.
    redrive: StrictBool = False


def _tenants_for(auth: AuthContext, own: str, named: str | None) -> list[str]:
    if named is not None:
        if named != own and not auth.is_admin:
            raise Forbidden(
                f"GET /v1/stranded-prs serves your own tenant's pull requests; "
                f"{named!r} is not your tenant"
            )
        return [named]
    if not auth.is_admin:
        return [own]
    return list(dict.fromkeys([own, *(tenant_id for tenant_id, _ in auth.tenant_choices)]))


@router.get("/v1/stranded-prs")
def list_stranded_prs(
    tenant_id: str | None = Query(default=None, min_length=1),
    auth: AuthContext = Depends(current_auth),
    own: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict[str, Any]:
    """The last sweep's stranded pull requests, one flat row each.

    `rows` across every tenant served, oldest first; `tenants` says when
    each was swept (null: never) and what could not be read. A row is
    `{tenant_id, repo, number, title, url, opened_at, age_hours, reason,
    detail, head_sha, remedy, checks, opened_by_task}`; `age_hours` is
    computed now, from `opened_at`, so a stale sweep still ages its rows.
    """
    now = ctx.now()
    rows: list[Any] = []
    tenants: list[dict[str, Any]] = []
    for tenant in _tenants_for(auth, own, tenant_id):
        found, stamp = stored_rows(ctx.db, tenant)
        rows.extend(found)
        tenants.append(stamp)
    served = [row.to_api(now) for row in rows]
    served.sort(key=lambda row: (row["opened_at"] is None, row["opened_at"] or "",
                                 row["tenant_id"], row["repo"], row["number"]))
    return {
        "stranded_after_hours": STRANDED_AFTER.total_seconds() / 3600,
        "log_every_hours": LOG_EVERY.total_seconds() / 3600,
        "reasons": list(REASONS),
        "remedies": list(REMEDIES),
        "tenants": tenants,
        "rows": served,
    }


@router.post("/v1/admin/stranded-prs/sweep")
def sweep_stranded_prs(
    tenant_id: str = Query(..., min_length=1),
    body: StrandedSweepRequest | None = Body(default=None),
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict[str, Any]:
    """One tenant's stranded-PR sweep (`strandedprs.sweep_tenant`)."""
    redrive = bool(body and body.redrive)
    if redrive and not auth.is_admin:
        raise Forbidden(
            "the stranded_pr_sweep job sweeps with redrive false; a redrive submits merges "
            "and is an admin's call"
        )
    report = sweep_tenant(ctx, tenant_id, redrive=redrive)
    ctx.metrics.admin_actions.labels(
        action="stranded_pr_redrive" if redrive else "stranded_pr_sweep"
    ).inc()
    return report.to_api()
