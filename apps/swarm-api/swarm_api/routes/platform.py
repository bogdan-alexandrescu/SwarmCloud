"""Platform read models: /v1/stats, /v1/capacity, /v1/providers."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context

router = APIRouter(prefix="/v1", tags=["platform"])


@router.get("/stats")
def stats(
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return ctx.submissions.stats(auth)


@router.get("/capacity")
def capacity(
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return ctx.submissions.capacity(auth)


@router.get("/providers")
def providers(
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return ctx.submissions.providers(auth)
