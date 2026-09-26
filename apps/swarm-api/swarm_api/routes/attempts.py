"""Attempts across every task of the caller's tenant.

`GET /v1/tasks/{id}/attempts` answers "what happened on each try of THIS task".
Nothing answered "what did this tenant's runs cost" short of one request per
task -- the N+1 loop docs/web-ui/ui-audit-and-build-prompt.md §B9.S3 predicts
will otherwise be written in a browser. `Store.list_attempts` had accepted
`task_id=None` all along, and the `attempts-tenant-created` index was declared
for it; only a route was missing.

TENANT SCOPE (invariant 9). The tenant comes from `tenant_scope` -- the verified
identity, through the principal-collision check -- never from a path, a query
string or a header. A `tenant_id` in the query string is not a parameter here
and is ignored.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from ..codec import attempt_to_api
from ..deps import AppContext, get_context, paged_limit, tenant_scope
from ..errors import ValidationFailed

router = APIRouter(prefix="/v1/attempts", tags=["attempts"])


def _utc(moment: datetime | None) -> datetime | None:
    """A caller's timestamp with no offset is read as UTC, as every stored one is."""
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@router.get("")
def list_attempts(
    since: datetime | None = Query(default=None, description="created_at >= since"),
    until: datetime | None = Query(default=None, description="created_at < until"),
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Every attempt of the caller's tenant, newest first, paged.

    COVERAGE IS WHAT MAKES A COST FIGURE HONEST. A sum of `cost_usd` over rows
    where some costs were never reported is a lower bound that looks like a
    total. `coverage` counts, over the rows on THIS page, how many there are
    and how many carry a reported `cost_usd` -- so a caller can draw
    "measured on 2 of 7" without a second pass.

    `with_spend` counts `cost_usd is not None`, and only that:

      * $0.00 IS a measurement -- a `mock` run costs nothing on purpose -- and
        is counted;
      * an absent cost is "not reported" (`control.record_spend` omits a key
        the runner did not report rather than writing zero) and is not;
      * token counts without a cost do not make a COST measured, so they are
        not counted either. The token fields are served on every row for a
        caller that wants to count those instead.

    `since` is inclusive and `until` exclusive, so adjacent windows tile. The
    rows are `attempt_to_api`'s shape, the one `/v1/tasks/{id}/attempts`
    serves, so there is one serialiser for the five spend fields to be right in.
    """
    since, until = _utc(since), _utc(until)
    if since is not None and until is not None and since >= until:
        raise ValidationFailed(
            "since must be earlier than until",
            detail={"since": since.isoformat(), "until": until.isoformat()},
        )
    page = ctx.store.page_attempts(
        tenant_id,
        limit=paged_limit(ctx, limit),
        page_token=page_token,
        since=since,
        until=until,
    )
    rows = page.items
    read_at = ctx.now()
    return {
        "tenant_id": tenant_id,
        # The clock each row's `cpu_reading_age_seconds` is taken against
        # (contract request #26).
        "read_at": read_at,
        "attempts": [attempt_to_api(attempt, read_at=read_at) for attempt in rows],
        "next_page_token": page.next_page_token,
        "coverage": {
            "attempts": len(rows),
            "with_spend": sum(1 for attempt in rows if attempt.cost_usd is not None),
        },
    }
