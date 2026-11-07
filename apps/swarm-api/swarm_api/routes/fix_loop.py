"""A person's answer to a fix loop parked for them (docs/design/fix-loop.md §4.5).

    POST /v1/workflows/{workflow_id}/fix-loop/rounds   {rounds?, note?}
    POST /v1/workflows/{workflow_id}/fix-loop/end      {outcome: "stop"}

Lane FL-DRIVE. The decisions are `swarm_api.fixloop`'s; this module is the
HTTP half. BGD's Q4 (a) (2026-10-27): any full member of the workflow's
tenant may add rounds -- one by default -- and no loop ever runs more than
`fixloop.FIX_LOOP_ROUNDS_CEILING` (10). The tenant is the caller's token's
(`tenant_scope`), never a parameter, so another tenant's workflow is a 404.
Adding a round writes who and when in a transaction guarded on the park, and
returns the loop to PARKED FIX_ROUNDS_RUNNING: it holds nothing, and the
round it submits is an ordinary admitted task (invariant 1). The optional
note is at most 4 KiB, redacted before it is stored and fenced as data in
the fix round's prompt. `end` takes only `stop`: the loop ends FAILED
`fix_loop_exhausted`, as its 7-day expiry would (Q3 (c)).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from .. import fixloop
from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from .tasks import require_member

router = APIRouter(tags=["workflows"])

ROUNDS_ROUTE = "/v1/workflows/{workflow_id}/fix-loop/rounds"
END_ROUTE = "/v1/workflows/{workflow_id}/fix-loop/end"


class AddRounds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # More than the ceiling allows is granted up to it, not refused: the
    # person asked for "more", and the answer says how many they got.
    rounds: int = Field(default=1, ge=1)
    # Bounded in bytes by `fixloop.add_rounds` (NOTE_MAX_BYTES, 4 KiB).
    note: str | None = None


class EndLoop(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["stop"]


@router.post(ROUNDS_ROUTE)
def add_rounds(
    workflow_id: str,
    body: AddRounds,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Give a loop parked for a person more rounds, and drive it again."""
    require_member(ctx, auth, fresh=True)
    result = fixloop.add_rounds(ctx, tenant_id=tenant_id, workflow_id=workflow_id,
                                by=auth.email, rounds=body.rounds, note=body.note)
    return {"fix_loop": result}


@router.post(END_ROUTE)
def end_loop(
    workflow_id: str,
    body: EndLoop,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Accept the last verdict: the loop ends FAILED `fix_loop_exhausted`."""
    require_member(ctx, auth, fresh=True)
    result = fixloop.stop(ctx, tenant_id=tenant_id, workflow_id=workflow_id, by=auth.email)
    return {"fix_loop": result}
