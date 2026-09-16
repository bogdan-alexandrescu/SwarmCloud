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

**Only the variables the frozen profile declares are exported.** The secret
payload may be a JSON object, and an earlier version of this module copied every
upper-case key in it into the child's environment. That turned
`roles/secretmanager.secretVersionAdder` -- which per-tenant admins hold, and
which deliberately does NOT allow reading the key back -- into arbitrary process
execution, because the runner reads `CLAUDE_CODE_BIN`, `CLAUDE_CODE_ARGS`,
`PATH` and `HTTPS_PROXY` from that same environment. The allowlist is
`RunnerProfile.secrets`, which comes from the frozen catalogue; anything else in
the payload is ignored and named in a warning so a misnamed key is diagnosable
without being obeyed.
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
    """Read this worker's OWN tenant document.

    `tenant_id` is the worker's admitted tenant, never anything a caller
    supplied, and the document is rejected if it carries a different id: a
    `tenants/{id}` document whose `tenant_id` field says something else is
    either corruption or another tenant writing into this one's record, and
    trusting it would pick the wrong secret name two lines later.
    """
    snap = db.collection("tenants").document(tenant_id).get()
    if not snap.exists:
        raise SecretError(f"tenant {tenant_id} does not exist")
    data = snap.to_dict() or {}
    stored = data.get("tenant_id")
    if stored is not None and stored != tenant_id:
        raise SecretError(
            f"tenants/{tenant_id} declares tenant_id {stored!r}; refusing to resolve "
            "credentials against a document that belongs to another tenant"
        )
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

    Exactly the names in `secret_env_names` -- the frozen profile's `secrets`
    tuple -- are exported. Every other key in the payload is dropped, because
    the child's environment also decides which binary the runner starts and how
    its libraries are loaded, and whoever can add a secret version is explicitly
    not trusted with that.
    """
    if not provider or not secret_env_names:
        return ResolvedCredentials(env={}, secret_names=())
    if provider not in tenant.credentials:
        raise CredentialMissing(tenant.tenant_id, provider)

    secret_name = tenant.secret_name(provider)
    payload = client.access(secret_name)
    env: dict[str, str] = {}
    ignored: list[str] = []
    parsed: Any = None
    stripped = payload.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
    if isinstance(parsed, dict):
        for key, value in parsed.items():
            if key in secret_env_names:
                if isinstance(value, (str, int, float)):
                    env[key] = str(value)
                continue
            ignored.append(str(key))
    else:
        for name in secret_env_names:
            env[name] = stripped

    if ignored:
        logger.warning(
            "ignoring secret payload keys the runner profile does not declare",
            secret=secret_name,
            provider=provider,
            ignored=sorted(ignored)[:20],
            declared=sorted(secret_env_names),
        )

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


#: The provider name under which a tenant registers the token used to clone its
#: repositories. It is a tenant credential like any other -- same naming rule,
#: same IAM scoping -- and deliberately NOT a platform-wide token: one token
#: that can clone every tenant's repositories would make a single malicious
#: repository in one tenant a credential compromise for all of them.
GIT_PROVIDER = "git"
GIT_TOKEN_ENV = "GIT_TOKEN"


def resolve_git_token(
    *,
    tenant: Tenant,
    client: SecretManagerClient | None,
    logger: Any,
) -> str | None:
    """The tenant's own clone token, or None when it has not registered one.

    None is not an error: a public repository clones without a credential, and a
    private one fails with git's own message, which is the right diagnosis. The
    value is registered with the logger before it is returned so that every path
    that might echo it is already scrubbing it.
    """
    if client is None or GIT_PROVIDER not in tenant.credentials:
        return None
    secret_name = tenant.secret_name(GIT_PROVIDER)
    payload = client.access(secret_name).strip()
    token = payload
    if payload.startswith("{"):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            value = parsed.get(GIT_TOKEN_ENV)
            if not isinstance(value, (str, int, float)):
                raise SecretError(
                    f"secret {secret_name} is a JSON object without a {GIT_TOKEN_ENV} string"
                )
            token = str(value).strip()
    if not token:
        return None
    logger.register_secret(token)
    logger.info("tenant git credential resolved", secret=secret_name)
    return token
