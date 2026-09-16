"""Per-tenant provider keys in Secret Manager.

Invariant 9, the part that matters most: a tenant's provider key must never be
reachable from another tenant's pod. That is enforced in three places at once
and all three live in this file.

  1. NAME. The secret is `swarm-tenant-<tenant>-<provider>`, taken from
     `Tenant.secret_name` in the frozen contract, so there is exactly one
     spelling of a tenant's secret anywhere in the platform.
  2. IAM. The binding is made on the SECRET RESOURCE, never at project level: a
     project-level grant would make every tenant's key readable by every
     tenant's workload. The policy is READ, the tenant's service account is
     added to `roles/secretmanager.secretAccessor` if it is not already there,
     and the result is written back with the etag it was read at.

     It is deliberately a merge and not a replace. This endpoint is reachable by
     any authenticated tenant member, and a replace would wipe every binding
     terraform put on the secret -- including the accessor grant for the real
     worker identity and the `secretVersionAdder` grant that is the admin
     rotation path -- leaving the tenant locked out of its own key. The etag
     makes the read-modify-write safe against a concurrent rotation, and a
     policy that already carries the accessor is not written at all.
  3. READ PATH. There is no read path. This module can write a version and
     report metadata; it has no function that returns payload bytes, so no
     future route can accidentally expose one.

The plaintext key exists only as a local in `put_credential` and is never
logged, never written to Firestore, and never included in a response.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from swarm_common.models import Tenant

from .errors import UpstreamUnavailable, ValidationFailed

log = logging.getLogger(__name__)

#: Secret Manager's own limit on a payload is 64 KiB; a provider API key is
#: two orders of magnitude smaller, so anything near the limit is a mistake
#: (a pasted file, a JSON blob) and is rejected rather than stored.
MAX_KEY_BYTES = 8 * 1024
MIN_KEY_BYTES = 8

#: The one role a tenant's worker needs on its own secret. Nothing here ever
#: grants a role on the PROJECT, and nothing here grants any other role.
ACCESSOR_ROLE = "roles/secretmanager.secretAccessor"


@dataclass(frozen=True)
class CredentialWriteResult:
    provider: str
    secret_id: str
    secret_name: str
    version: str
    accessor: str
    created: bool

    def to_api(self) -> dict[str, Any]:
        """Deliberately carries no payload and no payload length."""
        return {
            "provider": self.provider,
            "secret_id": self.secret_id,
            "secret_name": self.secret_name,
            "version": self.version,
            "accessor_service_account": self.accessor,
            "secret_created": self.created,
        }


class CredentialWriter(Protocol):
    def put_credential(self, tenant: Tenant, provider: str, api_key: str) -> CredentialWriteResult: ...


def validate_key_material(api_key: str) -> str:
    key = api_key.strip()
    encoded = key.encode("utf-8")
    if len(encoded) < MIN_KEY_BYTES:
        raise ValidationFailed("api_key is too short to be a provider key")
    if len(encoded) > MAX_KEY_BYTES:
        raise ValidationFailed(
            f"api_key exceeds {MAX_KEY_BYTES} bytes; that is a file, not a key"
        )
    if any(ch in key for ch in ("\n", "\r", "\x00")):
        raise ValidationFailed("api_key must be a single line with no NUL bytes")
    return key


class SecretManagerCredentials:
    """Writes a tenant's key into that tenant's own secret, bound to that tenant."""

    def __init__(
        self,
        project_id: str,
        *,
        client: Any | None = None,
        labels: dict[str, str] | None = None,
        replication_locations: tuple[str, ...] = (),
    ) -> None:
        if not project_id:
            raise ValueError("project_id is required")
        self._project_id = project_id
        self._client = client
        self._labels = dict(labels or {"managed-by": "swarm-api"})
        self._replication_locations = tuple(replication_locations)

    def _secret_client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    @property
    def parent(self) -> str:
        return f"projects/{self._project_id}"

    def put_credential(self, tenant: Tenant, provider: str, api_key: str) -> CredentialWriteResult:
        key = validate_key_material(api_key)
        if not tenant.service_account:
            raise UpstreamUnavailable(
                f"tenant {tenant.tenant_id!r} has no service account yet; "
                "provisioning must complete before a key can be bound to it"
            )

        from google.api_core import exceptions as gexc
        from google.cloud import secretmanager

        client = self._secret_client()
        secret_id = tenant.secret_name(provider)
        secret_name = f"{self.parent}/secrets/{secret_id}"
        created = False

        replication: dict[str, Any]
        if self._replication_locations:
            replication = {
                "user_managed": {
                    "replicas": [{"location": loc} for loc in self._replication_locations]
                }
            }
        else:
            replication = {"automatic": {}}

        try:
            client.create_secret(
                request={
                    "parent": self.parent,
                    "secret_id": secret_id,
                    "secret": {
                        "replication": replication,
                        "labels": {
                            **self._labels,
                            "tenant": tenant.tenant_id,
                            "provider": provider,
                        },
                    },
                }
            )
            created = True
        except gexc.AlreadyExists:
            # Re-registering a key is the normal rotation path.
            pass
        except gexc.GoogleAPICallError as exc:
            raise UpstreamUnavailable(f"could not create the tenant secret: {exc.message}") from None

        self._grant_accessor(client, secret_name, tenant, gexc)

        try:
            version = client.add_secret_version(
                request={
                    "parent": secret_name,
                    "payload": secretmanager.SecretPayload(data=key.encode("utf-8")),
                }
            )
        except gexc.GoogleAPICallError as exc:
            raise UpstreamUnavailable(f"could not store the key: {exc.message}") from None
        finally:
            # Drop the plaintext reference as early as possible.
            key = ""

        version_name = getattr(version, "name", None) or f"{secret_name}/versions/latest"
        log.info(
            "stored provider credential tenant=%s provider=%s version=%s",
            tenant.tenant_id,
            provider,
            version_name,
        )
        return CredentialWriteResult(
            provider=provider,
            secret_id=secret_id,
            secret_name=secret_name,
            version=version_name,
            accessor=tenant.service_account,
            created=created,
        )


    def _grant_accessor(self, client: Any, secret_name: str, tenant: Tenant, gexc: Any) -> bool:
        """Add the tenant's service account to the secret's accessor binding.

        Returns True if the policy was written, False if the grant was already
        there. Read-modify-write under the policy's own etag, retried once: a
        concurrent rotation invalidates the etag and Secret Manager answers
        ABORTED rather than clobbering, which is the behaviour we want and the
        reason the etag is carried at all.
        """
        accessor = f"serviceAccount:{tenant.service_account}"
        last_error: Any = None
        for attempt in range(2):
            try:
                policy = client.get_iam_policy(request={"resource": secret_name})
            except gexc.GoogleAPICallError as exc:
                raise UpstreamUnavailable(
                    f"could not read the tenant secret's IAM policy: {exc.message}"
                ) from None

            binding = _unconditional_binding(policy, ACCESSOR_ROLE)
            if binding is not None and accessor in list(binding.members):
                log.info(
                    "tenant secret already bound to its accessor tenant=%s secret=%s",
                    tenant.tenant_id,
                    secret_name,
                )
                return False
            if binding is None:
                binding = policy.bindings.add()
                binding.role = ACCESSOR_ROLE
            binding.members.append(accessor)

            try:
                client.set_iam_policy(
                    request={"resource": secret_name, "policy": policy}
                )
                return True
            except gexc.Aborted as exc:          # etag no longer current
                last_error = exc
                log.info(
                    "secret IAM policy changed under us, re-reading (attempt %d)",
                    attempt + 1,
                )
            except gexc.GoogleAPICallError as exc:
                raise UpstreamUnavailable(
                    f"could not bind the tenant secret to its service account: "
                    f"{exc.message}"
                ) from None
        raise UpstreamUnavailable(
            "the tenant secret's IAM policy is being modified concurrently; "
            f"retry the registration ({getattr(last_error, 'message', last_error)})"
        )


def _unconditional_binding(policy: Any, role: str) -> Any | None:
    """The role's binding, ignoring conditional ones.

    A conditional binding with the same role grants access only when its
    expression holds, so adding a member to it would not actually grant the
    worker anything. Those are left untouched.
    """
    for binding in policy.bindings:
        if binding.role != role:
            continue
        condition = getattr(binding, "condition", None)
        if condition is not None and getattr(condition, "expression", ""):
            continue
        return binding
    return None


class InMemoryCredentials:
    """Local-development writer. Still exposes no read path."""

    def __init__(self) -> None:
        self._versions: dict[str, int] = {}
        self._bindings: dict[str, str] = {}

    def put_credential(self, tenant: Tenant, provider: str, api_key: str) -> CredentialWriteResult:
        validate_key_material(api_key)
        if not tenant.service_account:
            raise UpstreamUnavailable(f"tenant {tenant.tenant_id!r} has no service account yet")
        secret_id = tenant.secret_name(provider)
        created = secret_id not in self._versions
        self._versions[secret_id] = self._versions.get(secret_id, 0) + 1
        self._bindings[secret_id] = tenant.service_account
        return CredentialWriteResult(
            provider=provider,
            secret_id=secret_id,
            secret_name=f"projects/local/secrets/{secret_id}",
            version=f"projects/local/secrets/{secret_id}/versions/{self._versions[secret_id]}",
            accessor=tenant.service_account,
            created=created,
        )
