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

BEING THE ONLY WRITER IS ALSO WHAT MAKES RETENTION THIS MODULE'S JOB. Every
publish in this platform goes through `add_version` -- the refresher's two
halves, and account registration's two -- so a superseded version is expired
here, once, rather than at four call sites that would drift apart. A rotation
that leaves its predecessor ENABLED has not rotated: the old credential is
still retrievable by anything holding `secretAccessor`, which is exactly what
`scripts/create-secrets.sh:226` says the rotation exists to prevent. Measured
on 2026-09-22, `swarm-tenant-u-bogdan-anthropic` held 1,816 ENABLED versions
and zero destroyed, and only `latest` has ever been read -- so 1,815 live
credentials had no consumer and no expiry. See `RETAINED_VERSIONS`.

Discovery goes through LABELS rather than by taking the secret name apart.
`swarm-tenant-u-bogdan-anthropic-refresh` splits ambiguously -- tenant ids
contain dashes (`u-<user>` is the personal-tenant form) -- and a mis-split would
refresh under the wrong tenant id and publish the result to a secret belonging
to someone else. The labels are written by the same script that creates the
secret, so they cannot disagree with it.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterator, Sequence

from .credentials import REFRESH_SUFFIX

#: The only role this service ever grants on a secret: read a payload, nothing
#: else. Not admin, not versionManager -- an accessor cannot rotate, relabel or
#: delete the secret it can read.
ACCESSOR_ROLE = "roles/secretmanager.secretAccessor"

log = logging.getLogger(__name__)

#: Set by scripts/create-secrets.sh on every tenant credential it writes.
CREDENTIAL_LABEL = "component=tenant-credential"

#: How many ENABLED versions of a platform secret survive a publish.
#:
#: THREE, and the number is argued from how the readers actually behave and
#: from who else writes -- not from taste:
#:
#:   * NOTHING IN THIS PLATFORM EVER READS AN OLDER VERSION. Every reader
#:     resolves `latest`: `SecretManagerStore.access` below pins
#:     `/versions/latest`, `agent_worker.secrets.SecretManagerClient.access`
#:     defaults to it, and the Cloud Run mount `scheduler.dispatch` builds sets
#:     `version="latest"`. A worker that started before a publish is holding
#:     the PAYLOAD it already resolved, not a reference to a version, so
#:     destroying that version cannot fail an attempt that is mid-flight. On
#:     reader behaviour alone the honest answer would be one.
#:   * ROLLBACK NEEDS ONE STEP BACK. Undoing a bad publish means re-publishing
#:     what was there before, and that means still being able to read it. That
#:     is the second.
#:   * THE REFRESHER IS NOT THE ONLY WRITER. `swarm_api.credentials` adds
#:     versions to a tenant's provider secret, and every human holding
#:     `secretVersionAdder` can add one by hand. Keeping exactly one would let
#:     a refresh landing a second after an operator's rotation destroy the
#:     operator's version. The third is that race.
#:
#: Three turns ~1,752 versions a year per account into three. Getting it wrong
#: upwards costs two versions; getting it wrong downwards costs a credential
#: nobody can get back, so the asymmetry is spent on the safe side.
RETAINED_VERSIONS = 3

#: The secret names this platform owns, and the ONLY ones a version may ever be
#: destroyed from.
#:
#: THIS IS THE MOST IMPORTANT LINE IN THIS FILE. `saga-agents-staging` is
#: SHARED: another team's cluster, VPC and secrets live in the same project, and
#: the broker's `swarmSecretLister` grant is project-WIDE rather than per-secret
#: (docs/audits/2026-09-22/track-c-secret-iam-findings.md, Finding 1). Nothing
#: outside this expression stands between a retention bug and another team's
#: credentials, and Secret Manager's delayed destruction gives them a day to
#: notice rather than a way to refuse.
#:
#: Anchored with `\A`/`\Z` rather than `^`/`$` -- `$` also matches before a
#: trailing newline, so `^...$` would accept `"swarm-tenant-eng-anthropic\n"`
#: and with it whatever a caller had appended. The body admits only the
#: characters a Secret Manager secret id may contain, so `/` cannot appear and a
#: name cannot re-point the resource path at another secret or another project,
#: and the leading anchor is what refuses `agents-staging-swarm-tenant-x`.
_PLATFORM_SECRET_NAME = re.compile(r"\Aswarm-(?:tenant|account)-[A-Za-z0-9_-]+\Z")

#: A version's own resource name, as Secret Manager returns it. Parsed rather
#: than trusted: the project and the secret in it are re-checked against the
#: secret being pruned, so a wrong `parent`, a paging bug or a client that
#: returned somebody else's versions cannot become a destroy.
_VERSION_NAME = re.compile(
    r"\Aprojects/(?P<project>[^/]+)/secrets/(?P<secret>[^/]+)/versions/(?P<number>[0-9]+)\Z"
)


def owned_by_this_platform(name: str) -> bool:
    """Whether `name` is a secret this platform created and may expire.

    Exported because it is worth testing directly and worth being able to cite
    from elsewhere -- never worth restating. A second spelling of this rule is
    how the shared project gets a destroy it did not authorise.
    """
    return bool(_PLATFORM_SECRET_NAME.match(name))


def _version_state(version: Any) -> str:
    """The state name, for a real enum or a plain string.

    Anything this cannot read comes back as "" and is therefore NOT enabled,
    which means it is neither counted towards the retained set nor destroyed.
    Unreadable metadata must not authorise a destroy.
    """
    state = getattr(version, "state", None)
    if state is None:
        return ""
    return str(getattr(state, "name", state))


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
        #: secret name -> the error type the retention warning was last emitted
        #: for. A missing IAM binding is ONE fact, not one fact per publish, and
        #: `_warned` in `quota_broker.credentials` makes the same choice for the
        #: same reason. The deduplication is of the LOG only: the pass itself is
        #: attempted again on every publish, because the next sweep retrying is
        #: what stops a transient failure leaving a secret growing forever.
        self._retention_warned: dict[str, str] = {}

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
        """Publish `payload`, then expire what it superseded.

        RETENTION RUNS AFTER THE WRITE AND CANNOT FAIL IT. The credential
        landing is the point; expiring its predecessors is housekeeping, and a
        `PermissionDenied` on housekeeping must not turn a successful rotation
        into a failed one -- the caller would report the tenant as unrefreshed
        and the next sweep would publish the same token again, which is the
        version leak this retention exists to end, rebuilt one layer up.
        """
        from google.api_core import exceptions as gexc
        from google.cloud import secretmanager

        client = self._secret_client()
        try:
            created = client.add_secret_version(
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

        self._expire_superseded(name, str(getattr(created, "name", "") or ""))

    def _expire_superseded(self, secret: str, just_written: str) -> None:
        """Destroy every ENABLED version older than the newest RETAINED_VERSIONS.

        WHY THIS IS SECURITY AND NOT TIDINESS. Only `latest` is ever read, so a
        superseded version has no consumer -- but it stays RETRIEVABLE by every
        principal holding `secretAccessor` on the secret until something
        destroys it. `swarm-tenant-u-bogdan-anthropic` held 1,816 enabled
        versions on 2026-09-22: 1,815 past credentials, all live, none needed.
        A credential that rotates and leaves its predecessor enabled has not
        rotated.

        FOUR THINGS ARE CHECKED IN CODE BEFORE ANYTHING IS DESTROYED, and each
        one refuses the WHOLE pass rather than skipping a version, because a
        retention pass that has already been surprised once is not one to keep
        running:

          1. the secret is one this platform owns (`owned_by_this_platform`);
          2. every version's own resource name parses, and names THIS project
             and THIS secret -- re-derived from the API's answer rather than
             from the local variable, so a wrong parent or a paging bug is
             caught here instead of in another team's audit log;
          3. the version this publish just wrote is not in the destroy set;
          4. at least one enabled version survives.

        NOTHING RECORDS THAT A SECRET HAS BEEN PRUNED. The set is recomputed
        from a live listing on every publish, so a destroy that failed for any
        reason is simply attempted again by the next one -- which is what keeps
        a transient failure from becoming permanent growth. The corollary is
        this design's honest limit: a secret nothing ever publishes to again is
        never pruned again. That is the right trade, because a secret nothing
        publishes to is a secret that is not growing.
        """
        try:
            self._destroy_stale_versions(secret, just_written)
        except Exception as exc:  # noqa: BLE001 -- housekeeping may never fail a publish
            self._warn_retention(secret, type(exc).__name__)

    def _destroy_stale_versions(self, secret: str, just_written: str) -> None:
        if not owned_by_this_platform(secret):
            # Not an exception: this is a refusal, and it is the one refusal in
            # this file that must be impossible to mistake for an outage.
            log.error(
                "refusing to expire versions of a secret this platform does "
                "not own; saga-agents-staging is shared and retention never "
                "leaves the swarm-tenant-*/swarm-account-* names",
                extra={"secret": secret},
            )
            return

        client = self._secret_client()
        parent = f"{self.parent}/secrets/{secret}"
        enabled: list[tuple[int, str]] = []
        for version in client.list_secret_versions(request={"parent": parent}):
            name = str(getattr(version, "name", "") or "")
            match = _VERSION_NAME.match(name)
            if match is None:
                log.error(
                    "a secret version came back with a resource name this "
                    "cannot parse, so nothing was expired",
                    extra={"secret": secret},
                )
                return
            if match.group("project") != self._project_id or match.group("secret") != secret:
                log.error(
                    "a version listed under one secret names another, so "
                    "nothing was expired",
                    extra={"secret": secret, "listed_under": match.group("secret")},
                )
                return
            # DISABLED and DESTROYED versions are already unreadable, so they
            # are neither a risk worth expiring nor a survivor worth counting.
            # Counting them would let three dead versions shield live ones from
            # retention, and destroying one raises FailedPrecondition.
            if _version_state(version) == "ENABLED":
                enabled.append((int(match.group("number")), name))

        enabled.sort(reverse=True)
        keep, stale = enabled[:RETAINED_VERSIONS], enabled[RETAINED_VERSIONS:]
        if not stale:
            return
        if not keep:
            log.error(
                "retention would have left this secret with no enabled "
                "version, so nothing was expired",
                extra={"secret": secret, "enabled": len(enabled)},
            )
            return
        if just_written and any(name == just_written for _, name in stale):
            log.error(
                "retention selected the version this publish just wrote, so "
                "nothing was expired; the credential is live and unaffected",
                extra={"secret": secret},
            )
            return

        for _, name in stale:
            client.destroy_secret_version(request={"name": name})
        self._retention_warned.pop(secret, None)
        log.info(
            "expired superseded secret versions",
            extra={"secret": secret, "destroyed": len(stale), "kept": len(keep)},
        )

    def _warn_retention(self, secret: str, error: str) -> None:
        """Say once per process, per secret, per error type, what went wrong.

        Named rather than counted. `PermissionDenied` here means the broker
        holds neither `secretmanager.versions.list` nor
        `secretmanager.versions.destroy` on this secret -- which was true of
        the live policy on 2026-09-22 -- and that is a binding somebody has to
        grant, not something a retry resolves. Anything else is transient, and
        the next publish tries again either way.
        """
        if self._retention_warned.get(secret) == error:
            return
        self._retention_warned[secret] = error
        log.warning(
            "could not expire superseded versions of this secret, so its past "
            "credentials stay retrievable; the broker needs "
            "secretmanager.versions.list and secretmanager.versions.destroy "
            "on it. The publish itself succeeded and the next one retries",
            extra={"secret": secret, "error": error},
        )

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


__all__ = [
    "CREDENTIAL_LABEL",
    "RETAINED_VERSIONS",
    "SecretManagerStore",
    "SecretMissing",
    "owned_by_this_platform",
]
