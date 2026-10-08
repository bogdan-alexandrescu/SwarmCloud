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
import re
from dataclasses import dataclass
from typing import Any, Mapping

from swarm_common.models import FORGE_CREDENTIAL, Tenant, utcnow

from . import indexrun


class SecretError(RuntimeError):
    pass


class CredentialMissing(RuntimeError):
    """The tenant has no key registered for this provider."""

    def __init__(self, tenant_id: str, provider: str) -> None:
        super().__init__(f"tenant {tenant_id} has no {provider} credential registered")
        self.tenant_id = tenant_id
        self.provider = provider


class ForgeWriteRefused(RuntimeError):
    """The task's forge credential does not allow this write (docs/onboarding.md §3.3).

    `cause` is the worker's word for why (a read grant, a revoked grant, an
    unreadable one); `reason` says it in a sentence. Neither holds a value.
    """

    def __init__(self, cause: str, reason: str) -> None:
        super().__init__(f"{cause}: {reason}")
        self.cause = cause
        self.reason = reason


@dataclass(frozen=True)
class ResolvedCredentials:
    env: dict[str, str]
    secret_names: tuple[str, ...]


def load_tenant(
    db: Any, tenant_id: str, *, call_options: dict[str, Any] | None = None
) -> Tenant:
    """Read this worker's OWN tenant document.

    `tenant_id` is the worker's admitted tenant, never anything a caller
    supplied, and the document is rejected if it carries a different id: a
    `tenants/{id}` document whose `tenant_id` field says something else is
    either corruption or another tenant writing into this one's record, and
    trusting it would pick the wrong secret name two lines later.

    `call_options` is the read's `retry` and `timeout`. The lifecycle passes
    its startup budget before the runner starts (`ControlPlane.call_options`),
    and the mid-run `tenant` budget for a credential reload
    (`control.MID_RUN_BUDGETS`, #70).
    """
    snap = db.collection("tenants").document(tenant_id).get(**dict(call_options or {}))
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
    any_of: bool = False,
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
    if any_of:
        # The declared names are INTERCHANGEABLE, not a set that must all be
        # present: claude-code takes either metered API access or a Claude
        # subscription token, and a tenant has exactly one. Requiring all of
        # them would refuse a JSON secret that supplies precisely the credential
        # the tenant pays for, with "does not supply CLAUDE_CODE_OAUTH_TOKEN"
        # for an API-key tenant and the mirror image for a subscription one.
        if not env:
            raise SecretError(
                f"secret {secret_name} supplies none of "
                f"{', '.join(secret_env_names)} for provider {provider}"
            )
    elif missing:
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
    provider: str = GIT_PROVIDER,
) -> str | None:
    """The task's forge token, or None when the tenant has not registered one.

    `provider` is the suffix `forge_suffix` read from the task's SIGNED
    `forge_credential` (contract request 54): `git`, the tenant's own token,
    by default; `git-u-<hex>`, a member's user slot, or `git-r-<hex>`, a
    repository's. Whichever it is, the name is `Tenant.secret_name` under this
    worker's OWN tenant, and the read is the `latest` version, which swarm-api's
    refresher keeps at least two hours from expiry (docs/onboarding.md §3.3
    step 3). Nothing caches it: a caller that reads again gets the version
    current at that moment, which is how an attempt outlives an 8-hour user
    token.

    None is not an error for the tenant token: a public repository clones
    without a credential, and a private one fails with git's own message, which
    is the right diagnosis. A user or repository slot is not listed in
    `tenant.credentials` (swarm-api creates it, not the tenant document), so it
    is read directly, and a read that fails is a `SecretError` naming the
    secret, never its value. The value is registered with the logger before it
    is returned so that every path that might echo it is already scrubbing it.
    """
    if client is None:
        return None
    if not FORGE_CREDENTIAL.fullmatch(provider or ""):
        raise SecretError(f"{provider!r} is not a forge credential suffix")
    if provider == GIT_PROVIDER and GIT_PROVIDER not in tenant.credentials:
        return None
    secret_name = tenant.secret_name(provider)
    if provider == GIT_PROVIDER:
        payload = client.access(secret_name).strip()
    else:
        try:
            payload = client.access(secret_name).strip()
        except Exception as exc:  # noqa: BLE001 -- the type is enough; never the value
            raise SecretError(
                f"secret {secret_name} could not be read ({type(exc).__name__})"
            ) from None
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


# --------------------------------------------------------------------------
# The task's forge credential and its grant (docs/onboarding.md §3.3, OB5)
# --------------------------------------------------------------------------

#: `swarm_api.forgeapp.GRANTS`: one document per person x repository, id
#: `{tenant_id}__{user_hash}__{repo_id}`, written and deleted by swarm-api's
#: Access routes (`swarm_api.access.AccessService.grant` / `revoke` /
#: `remove_org`). RESTATED, because the worker's image does not carry
#: swarm-api; tests/unit/worker/test_forge_credential.py holds it, and the
#: id recipe, to swarm-api's own.
FORGE_GRANTS = "forge_grants"
#: The values of a grant's `mode` and of `Task.forge_access`.
WRITE, READ = "write", "read"
#: A user slot's suffix. Its 16 hex are the person's `user_hash`, the same
#: hex that sits in the middle of their grant ids (`swarm_api.gittokens.
#: provider_suffix`, `swarm_api.forgeapp.user_hash`).
_USER_SLOT = re.compile(r"git-u-([0-9a-f]{16})")


def forge_suffix(task: Mapping[str, Any]) -> str:
    """The provider suffix of the task's forge secret, `git` when it names none.

    CALL ONLY ON THE VERIFIED DOCUMENT. `forge_credential` is covered by the
    spec signature (`specsign.canonical_step_spec`, format 3), and the worker
    reads it only after the generation check and the signature check have both
    passed, in that order (invariant 5; docs/onboarding.md §3.6 item 5). A value
    of another shape is refused rather than defaulted: it was signed, so it is
    a platform bug, and reading the tenant token instead would run the task as
    somebody it was not resolved to.
    """
    value = task.get("forge_credential")
    if value is None:
        return GIT_PROVIDER
    if not isinstance(value, str) or not FORGE_CREDENTIAL.fullmatch(value):
        raise SecretError("the task's forge_credential is not a forge credential suffix")
    return value


def forge_read_only(task: Mapping[str, Any]) -> bool:
    """True when the task's signed `forge_access` is `read` (D9). None is `write`."""
    return task.get("forge_access") == READ


def user_slot_hash(suffix: str) -> str | None:
    """The `user_hash` a `git-u-<hex>` suffix names, else None."""
    match = _USER_SLOT.fullmatch(suffix or "")
    return match.group(1) if match else None


def grant_refusal(
    db: Any,
    *,
    tenant_id: str,
    suffix: str,
    repository_url: str | None,
    write: bool,
    call_options: dict[str, Any] | None = None,
) -> str | None:
    """Why the task's grant no longer covers what it is about to do, or None.

    docs/onboarding.md §3.3 step 4: before cloning, and again before each push
    or pull request, the worker re-reads the person's grant for the task's
    repository by id. Only for a user slot: the tenant token and a repository
    token are not a person's, and no grant stands behind them.

    TENANT-SCOPED (invariant 9). The id is built from this worker's OWN tenant
    (`tenant_id`, the admitted one, never a document's), the hex of the signed
    suffix, and the repo_id of the signed repository URL by the registration's
    recipe (`indexrun.repo_id_for`). A document there that names another
    tenant, another person or another repository is not this task's grant,
    and reads as no grant.

    `write` asks for a push or a pull request: a grant changed to `read` since
    submission refuses it, as a deleted one does. A grant is not cached: each
    call reads the document again.
    """
    hashed = user_slot_hash(suffix)
    if hashed is None:
        return None
    where = indexrun.target(tenant_id, repository_url)
    if isinstance(where, str):
        return f"no grant can cover this repository: {where}"
    doc_id = f"{tenant_id}__{hashed}__{where.repo_id}"
    snap = db.collection(FORGE_GRANTS).document(doc_id).get(**dict(call_options or {}))
    data = (snap.to_dict() or {}) if snap.exists else None
    if (
        data is None
        or data.get("tenant_id") != tenant_id
        or data.get("user_hash") != hashed
        or data.get("repo_id") != where.repo_id
    ):
        return (
            f"the submitter's grant on {where.repo_id} has been removed since the "
            "task was submitted"
        )
    if write and data.get("mode") != WRITE:
        return (
            f"the submitter's grant on {where.repo_id} is now {data.get('mode')!r}, "
            "not 'write'"
        )
    return None
