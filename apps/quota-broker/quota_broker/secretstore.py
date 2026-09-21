"""Secret Manager access for the one service that WRITES tenant credentials.

The agent-worker also reads tenant secrets, but only ever reads, and only its
own tenant's. This store is different in two ways that matter:

  * it adds versions, so the broker's service account holds
    `secretmanager.secretVersionAdder` on tenant credential secrets; and
  * it enumerates tenants, so it holds `secretmanager.secrets.list` on the
    project.

Neither is granted to workers. A worker that could enumerate secrets could
enumerate tenants, and a worker that could add versions could overwrite another
tenant's credential with one it controls.

Discovery goes through LABELS rather than by taking the secret name apart.
`swarm-tenant-u-bogdan-anthropic-refresh` splits ambiguously -- tenant ids
contain dashes (`u-<user>` is the personal-tenant form) -- and a mis-split would
refresh under the wrong tenant id and publish the result to a secret belonging
to someone else. The labels are written by the same script that creates the
secret, so they cannot disagree with it.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, Sequence

from .credentials import REFRESH_SUFFIX

#: The only role this service ever grants on a secret: read a payload, nothing
#: else. Not admin, not versionManager -- an accessor cannot rotate, relabel or
#: delete the secret it can read.
ACCESSOR_ROLE = "roles/secretmanager.secretAccessor"

log = logging.getLogger(__name__)

#: Set by scripts/create-secrets.sh on every tenant credential it writes.
CREDENTIAL_LABEL = "component=tenant-credential"


class SecretMissing(KeyError):
    """No such secret, or it has no versions yet.

    Distinct from every other failure on purpose: a tenant with no refresh
    secret is the NORMAL case (they use a static API key), while a tenant whose
    refresh secret cannot be read because Secret Manager returned 503 is an
    incident. Collapsing the two makes an outage look like an empty fleet.
    """


class SecretManagerStore:
    def __init__(self, project_id: str, *, client: Any | None = None) -> None:
        if not project_id:
            raise ValueError("project_id is required")
        self._project_id = project_id
        self._client = client

    @property
    def parent(self) -> str:
        return f"projects/{self._project_id}"

    def _secret_client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager  # lazy: keeps import cost off startup

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def access(self, name: str) -> str:
        from google.api_core import exceptions as gexc

        client = self._secret_client()
        try:
            response = client.access_secret_version(
                request={"name": f"{self.parent}/secrets/{name}/versions/latest"}
            )
        except gexc.NotFound:
            raise SecretMissing(name) from None
        except gexc.FailedPrecondition:
            # The secret exists but has no enabled version -- created and never
            # populated. Same practical meaning as missing, same handling.
            raise SecretMissing(name) from None
        return response.payload.data.decode("utf-8")

    def ensure_secret(
        self,
        name: str,
        *,
        labels: dict[str, str],
        accessors: Sequence[str],
        region: str,
    ) -> bool:
        """Create the secret if it is absent, and bind its readers. Idempotent.

        Returns True when it created one, False when it already existed.

        WHY THIS LIVES HERE RATHER THAN IN TERRAFORM. A pool account's secret
        name contains a LABEL the operator chooses at registration time, so
        there is no name for terraform to declare in advance. Something at
        request time has to make it, and that something must be the single
        writer for subscription credentials -- two components creating and
        rotating the same secret is how a rotating credential gets bricked.

        CREATION AND BINDING ARE ONE OPERATION, deliberately. A secret created
        without an accessor binding is one the tenant's pod cannot read, and
        that failure does not surface here: it surfaces much later, inside a
        job, as an unexplained auth error. Splitting them would make a
        half-provisioned account a state this platform can be in.

        The binding is ADDITIVE -- read, append, write. Replacing the policy
        would drop any grant made outside this call, and on a re-registration
        that would quietly revoke a tenant that had been lent the account.

        Replication is user-managed and pinned to `region` because that is what
        every other secret in this platform uses; automatic replication would
        put the tenant's credential in regions the deployment never chose.
        """
        from google.api_core import exceptions as gexc
        from google.cloud import secretmanager

        client = self._secret_client()
        created = False
        try:
            client.create_secret(
                request={
                    "parent": self.parent,
                    "secret_id": name,
                    "secret": secretmanager.Secret(
                        replication=secretmanager.Replication(
                            user_managed=secretmanager.Replication.UserManaged(
                                replicas=[
                                    secretmanager.Replication.UserManaged.Replica(
                                        location=region
                                    )
                                ]
                            )
                        ),
                        labels=dict(labels),
                    ),
                }
            )
            created = True
        except gexc.AlreadyExists:
            # Re-registering an existing label is the supported way to replace
            # a credential, so this is an ordinary path and not a conflict.
            pass

        if accessors:
            self._grant_accessors(name, accessors)
        return created

    def _grant_accessors(self, name: str, members: Sequence[str]) -> None:
        client = self._secret_client()
        resource = f"{self.parent}/secrets/{name}"
        policy = client.get_iam_policy(request={"resource": resource})

        binding = None
        for existing in policy.bindings:
            if existing.role == ACCESSOR_ROLE:
                binding = existing
                break
        if binding is None:
            binding = policy.bindings.add()
            binding.role = ACCESSOR_ROLE

        present = set(binding.members)
        missing = [m for m in members if m not in present]
        if not missing:
            # Nothing to do, and saying so matters: setIamPolicy on an
            # unchanged policy still burns a write quota and still races any
            # other writer of the same policy.
            return
        binding.members.extend(missing)
        client.set_iam_policy(request={"resource": resource, "policy": policy})

    def add_version(self, name: str, payload: str) -> None:
        from google.api_core import exceptions as gexc
        from google.cloud import secretmanager

        client = self._secret_client()
        try:
            client.add_secret_version(
                request={
                    "parent": f"{self.parent}/secrets/{name}",
                    "payload": secretmanager.SecretPayload(data=payload.encode("utf-8")),
                }
            )
        except gexc.NotFound:
            # Refusing loudly beats creating the secret here. A secret this
            # service invents has no tenant labels and no accessor binding, so
            # the worker that needs it could not read it, and the failure would
            # surface later as an unexplained auth error inside a job.
            raise SecretMissing(name) from None

    def subscription_tenants(self) -> list[tuple[str, str]]:
        """Every (tenant_id, provider) pair that has a refresh credential."""
        pairs: list[tuple[str, str]] = []
        for secret in self._list():
            name = secret.name.rsplit("/", 1)[-1]
            if not name.endswith(REFRESH_SUFFIX):
                continue
            labels = dict(getattr(secret, "labels", {}) or {})
            tenant, provider = labels.get("tenant"), labels.get("provider")
            if not tenant or not provider:
                # Pre-dates the labels, or was created by hand. Skipped rather
                # than guessed at, and named so an operator can relabel it.
                log.warning(
                    "refresh secret has no tenant/provider labels and is skipped",
                    extra={"secret": name},
                )
                continue
            pairs.append((tenant, provider))
        return sorted(set(pairs))

    def _list(self) -> Iterator[Any]:
        client = self._secret_client()
        return iter(
            client.list_secrets(
                request={"parent": self.parent, "filter": f"labels.{CREDENTIAL_LABEL}"}
            )
        )


__all__ = ["SecretManagerStore", "SecretMissing", "CREDENTIAL_LABEL"]
