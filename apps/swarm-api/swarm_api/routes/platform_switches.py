"""The platform switches (docs/design/managed-plugins.md section 11).

    GET /v1/admin/platform-switches   each switch's value in force, its
                                      stored value, its Terraform default
                                      and options; the document's version
    PUT /v1/admin/platform-switches   `{version, work_contracts?, contract_review?}`:
                                      a switch left out keeps its value, a
                                      null resets it to the Terraform
                                      default; a stale `version` is 409

Platform admins only (`admin_auth`), each change audited as
`platform_switch_set` (`swarm_api.platformswitches`). The console's
confirmation dialog draws from GET: the value in force, what it would
become and the Terraform default beside it.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import Field

from ..auth import AuthContext
from ..deps import AppContext, admin_auth, get_context
from ..judgment import settings_mac
from .. import platformswitches
from ..platformswitches import CONTRACT_REVIEW, WORK_CONTRACTS, PlatformSwitches
from ..schemas import StrictModel

router = APIRouter(prefix="/v1/admin", tags=["admin"])


class PlatformSwitchesPut(StrictModel):
    version: int = Field(ge=0)
    work_contracts: Literal["off", "on"] | None = None
    contract_review: Literal["off", "report", "enforce"] | None = None


def _switches(ctx: AppContext) -> PlatformSwitches:
    return PlatformSwitches(ctx.db, mac=settings_mac(ctx.settings), now=ctx.now)


@router.get("/platform-switches")
def get_platform_switches(
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return _switches(ctx).view()


@router.put("/platform-switches")
def put_platform_switches(
    body: PlatformSwitchesPut,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    # Only the switches the body names: one left out keeps its value, and a
    # named null is "reset to the Terraform default".
    changes = {name: getattr(body, name) for name in (WORK_CONTRACTS, CONTRACT_REVIEW)
               if name in body.model_fields_set}
    switches = _switches(ctx)
    switches.set(changes, version=body.version, by=(auth.email or "").strip().lower())
    # This instance's readers act on the change at once; the others within
    # platformswitches.CACHE_SECONDS.
    platformswitches.invalidate()
    return switches.view()
