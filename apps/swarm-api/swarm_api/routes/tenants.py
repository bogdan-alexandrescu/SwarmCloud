"""Tenant self-service, including the write-only credential endpoint.

`POST /v1/tenants/me/credentials` is the only route in the service that accepts
key material. It writes the key into THAT tenant's own Secret Manager secret,
sets that secret's IAM policy to exactly that tenant's service account, and
returns metadata. There is no corresponding GET: `GET /v1/tenants/me` reports
which providers have a key registered, never the key.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from ..auth import AuthContext
from ..codec import tenant_to_api
from ..deps import AppContext, current_auth, get_context
from ..errors import ValidationFailed
from ..schemas import CredentialCreate
from ..validation import known_providers

router = APIRouter(prefix="/v1/tenants", tags=["tenants"])


@router.get("/me")
def get_me(
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = ctx.submissions.tenant_for(auth)
    return {
        "tenant": tenant_to_api(tenant),
        "principal": {
            "email": auth.email,
            "domain": auth.principal.domain,
            "groups": list(auth.principal.groups),
            "is_admin": auth.is_admin,
        },
    }


@router.post("/me/credentials", status_code=status.HTTP_201_CREATED)
def put_credential(
    body: CredentialCreate,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    provider = body.provider.strip().lower()
    allowed = known_providers()
    if provider not in allowed:
        raise ValidationFailed(
            f"unknown provider {provider!r}",
            detail={"known_providers": list(allowed)},
        )
    tenant = ctx.submissions.tenant_for(auth)
    result = ctx.credentials.put_credential(tenant, provider, body.api_key)
    ctx.store.register_credential(tenant.tenant_id, provider)
    ctx.metrics.credentials_written.labels(
        tenant=tenant.tenant_id, provider=provider
    ).inc()
    # `result.to_api()` carries no payload and no payload length by construction.
    return {
        "credential": result.to_api(),
        "note": (
            "stored write-only; no read path in this API can return key material"
        ),
    }
