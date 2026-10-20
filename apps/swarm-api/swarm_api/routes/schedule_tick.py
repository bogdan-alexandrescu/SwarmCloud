"""`POST /v1/admin/schedules/tick`: the schedule tick (docs/schedules.md §2, lane S2).

Called every minute by Cloud Scheduler (`google_cloud_scheduler_job.
schedule_tick`, lane S4) with an OIDC token for `swarm-schedule-tick`, with
NO tenant parameter: one query covers every tenant's due schedules, personal
ones included (§2.1).

WHO MAY CALL IT: the schedule tick and nobody else (owner decision SD10).
`auth.SCHEDULE_TICK_ROUTES` lets the tick's identity through `admin_auth`;
this handler then refuses every other caller that got that far -- an admin
included, because an admin's token would otherwise fire every tenant's due
schedules at will, and the rollup sweeper, which `require_admin` already
refuses here. While `SCHEDULE_TICK_USERS` is unset (lane S13 renders it) the
tick admits nobody, and the 403 says that is why.

It submits nothing as the caller: every firing is submitted as its schedule's
stored owner, in its schedule's stored tenant (`schedulefire.
schedule_owner_auth`).

AWAIT-INPUT EXPIRY RIDES THE SAME MINUTE (docs/design/human-steps.md §4.6).
After `run_tick`, `awaitinput.expire_due` ends every waiting step past its
`expires_at` as its signed declaration says: dead-lettered under `fail`, its
default written and the step SUCCEEDED under `default`. Here because swarm-api
is the bucket writer, so every write that ends an await-input step is in one
service, and because the tick is a service account -- which the answer route
refuses -- expiry is its own function and never the answer route. While the
`await-input` profile is unavailable it reads nothing and the report has no
`await_input` key, so the tick is exactly what it was.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from .. import awaitinput
from ..auth import AuthContext, SCHEDULE_TICK_USERS_ENV
from ..deps import AppContext, admin_auth, get_context
from ..errors import Forbidden
from ..schedulefire import run_tick

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin", tags=["schedules"])


@router.post("/schedules/tick")
def schedule_tick(
    request: Request,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Claim and fire every due schedule, finish stale claims, advance live firings.

    Always 200 to the tick: one schedule's error is counted in the report
    and the page goes on (§2.10). `{"disabled": true}` while the owner's
    SCHEDULES_ENABLED switch is off (§2.11).
    """
    if not auth.is_schedule_tick:
        if not ctx.authenticator.schedule_tick_configured:
            raise Forbidden(
                f"POST /v1/admin/schedules/tick admits only the schedule tick's identity, and "
                f"{SCHEDULE_TICK_USERS_ENV} is not configured in this deployment, so it admits "
                "nobody (docs/schedules.md §9, lane S13)"
            )
        raise Forbidden("POST /v1/admin/schedules/tick admits only the schedule tick's identity")
    report = run_tick(ctx)
    ctx.metrics.admin_actions.labels(action="schedule_tick").inc()
    body = report.to_api()
    try:
        expired = awaitinput.expire_due(ctx, getattr(request.app.state, "input_writer", None))
    except Exception:
        # One minute's sweep is retried by the next; it must not turn the
        # schedules' report into a 500 (§2.10's "always 200 to the tick").
        log.exception("await-input expiry sweep failed; the next tick retries it")
        expired = awaitinput.ExpiryReport(errors=1)
    if expired is not None:
        body["await_input"] = expired.to_api()
    return body
