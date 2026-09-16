"""Tenant provider credentials.

Invariant 9: a tenant's provider key must never be reachable from another
tenant's pod. Three things enforce it, and this module is the last of them:

1. the secret is named `swarm-tenant-<tenant>-<provider>` -- the name comes from
   the frozen `Tenant.secret_name()`, never from anything a caller supplied;
2. IAM grants the tenant's own service account access to exactly that secret,
   and the worker runs as that service account, so a worker that asked for
   another tenant's secret would be denied by Google rather than by this code;
3. the value is registered with the logger for redaction the moment it is read,
   and is passed to the child through its environment only -- never through
   argv, never written to the workspace, never included in an artifact.

A tenant that has not registered a key for the provider its runner needs is not
an error: the task parks as CREDENTIAL_MISSING, which costs nothing, and starts
again by itself once an admin adds the key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from swarm_common.models import Tenant, utcnow


class SecretError(RuntimeError):
    pass


class CredentialMissing(RuntimeError):
    """The tenant has no key registered for this provider."""

    def __init__(self, tenant_id: str, provider: str) -> None:
        super().__init__(f"tenant {tenant_id} has no {provider} credential registered")
        self.tenant_id = tenant_id
        self.provider = provider


@dataclass(frozen=True)
class ResolvedCredentials:
    env: dict[str, str]
    secret_names: tuple[str, ...]


def load_tenant(db: Any, tenant_id: str) -> Tenant:
    snap = db.collection("tenants").document(tenant_id).get()
    if not snap.exists:
        raise SecretError(f"tenant {tenant_id} does not exist")
    data = snap.to_dict() or {}
    return Tenant(
        tenant_id=tenant_id,
        kind=data.get("kind", "group"),
        principal=data.get("principal", ""),
        created_at=data.get("created_at") or utcnow(),
        display_name=data.get("display_name"),
        max_active=int(data.get("max_active", 20)),
        capacity_units=int(data.get("capacity_units", 40)),
        monthly_budget_usd=data.get("monthly_budget_usd"),
        enabled=bool(data.get("enabled", True)),
        credentials=list(data.get("credentials", [])),
        service_account=data.get("service_account"),
        gcs_prefix=data.get("gcs_prefix"),
        namespace=data.get("namespace"),
    )


class SecretManagerClient:
    """Thin wrapper so the lifecycle can be tested without Secret Manager."""

    def __init__(self, project_id: str, client: Any | None = None) -> None:
        self._project_id = project_id
        self._client = client

    def _get(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager  # lazy import

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def access(self, secret_name: str, version: str = "latest") -> str:
        name = f"projects/{self._project_id}/secrets/{secret_name}/versions/{version}"
        response = self._get().access_secret_version(request={"name": name})
        return response.payload.data.decode("utf-8")


def resolve_credentials(
    *,
    tenant: Tenant,
    provider: str | None,
    secret_env_names: tuple[str, ...],
    client: SecretManagerClient,
    logger: Any,
) -> ResolvedCredentials:
    """Fetch the tenant's key for `provider` and shape it into child env vars.

    The secret payload is either a bare key or a JSON object mapping env var
    names to values, which is what lets a runner that needs two variables (a key
    and a base URL, say) keep using one per-tenant-per-provider secret.
    """
    if not provider or not secret_env_names:
        return ResolvedCredentials(env={}, secret_names=())
    if provider not in tenant.credentials:
        raise CredentialMissing(tenant.tenant_id, provider)

    secret_name = tenant.secret_name(provider)
    payload = client.access(secret_name)
    env: dict[str, str] = {}
    parsed: Any = None
    stripped = payload.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
    if isinstance(parsed, dict):
        for name in secret_env_names:
            if name in parsed:
                env[name] = str(parsed[name])
        for key, value in parsed.items():
            if key in secret_env_names:
                continue
            if key.isupper() and isinstance(value, (str, int, float)):
                env[key] = str(value)
    else:
        for name in secret_env_names:
            env[name] = stripped

    missing = [name for name in secret_env_names if name not in env]
    if missing:
        raise SecretError(
            f"secret {secret_name} does not supply {', '.join(missing)} for provider {provider}"
        )
    for value in env.values():
        logger.register_secret(value)
    logger.info("tenant credentials resolved", provider=provider, secret=secret_name,
                variables=sorted(env))
    return ResolvedCredentials(env=env, secret_names=(secret_name,))
