"""API-process settings.

`swarm_common.config.Settings` is frozen and holds everything the platform as a
whole needs. This module adds only what the API process alone needs and nothing
that belongs in the frozen contract: which Google groups map to tenants (in
admin-defined priority order), which groups grant admin rights, how long a
Cloud Identity membership answer may be cached, and which Pub/Sub topic wakes
the scheduler.

Concurrency limits deliberately do NOT appear here -- they live in Firestore so
an admin can change them through the admin API without a redeploy.

Two settings here are SAFETY settings rather than tuning knobs, and both fail
the process at start rather than degrading quietly:

  * `hardened` (derived from ENVIRONMENT) turns the optional-by-default token
    audience check into a required one. An unpinned audience means any Google
    ID token from an allowed domain authenticates, including one a third-party
    SaaS obtained when an employee signed in with Google.
  * `REQUIRE_AUTH=false` used to be parsed and then never read by anything, so
    an operator could set it and believe auth was off (or on). Nothing in this
    build can disable authentication, so the value is refused outright instead
    of being silently ignored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from swarm_common.config import Settings


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - misconfiguration
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ApiSettings:
    """Everything the API needs at process start."""

    core: Settings

    #: Admin-registered tenant groups, in priority order. `resolve_tenant` walks
    #: this list and the FIRST match wins, so the order is what makes a user in
    #: several groups land in the same tenant on every request.
    iap_audiences: tuple[str, ...] = ()
    tenant_groups: tuple[str, ...] = ()

    #: Membership in any of these grants the admin surface. Kept separate from
    #: tenant groups so an admin still belongs to a normal tenant for their own
    #: tasks.
    admin_groups: tuple[str, ...] = ()
    #: Admin by EMAIL rather than by group membership.
    #:
    #: An escape hatch, and it exists because admin is otherwise unreachable
    #: here: `is_admin` is computed from Cloud Identity group membership, and
    #: this platform's service account cannot read groups at all -- the Groups
    #: API does not authorize through GCP IAM, and a *.gserviceaccount.com
    #: identity is not a Workspace principal. With no resolvable groups,
    #: `admin_set` is empty and NOBODY is an admin, which 403s every operator
    #: screen.
    #:
    #: It is strictly worse operationally than a group: changing it needs a
    #: deploy. It is NOT weaker authentication -- the email still comes from
    #: the verified IAP assertion, the same source the group lookup would have
    #: started from. Delete it in the same change that grants swarm-api a
    #: Workspace Group Reader role.
    admin_users: tuple[str, ...] = ()
    #: Authorise these exact addresses, regardless of their domain.
    #:
    #: WHY THIS EXISTS, and it is not the same idea as `admin_users` above.
    #: That one decides who is an ADMIN among callers already authorised;
    #: this decides who is a CALLER at all, and it is the difference between
    #: this platform being deployable outside a Google Workspace or not.
    #:
    #: Authorisation is otherwise by hosted domain, which is exactly right for
    #: an organisation: `allowed_domains = ["saga.xyz"]` means the people in
    #: that organisation. It collapses for anyone else. A single developer on a
    #: personal project has no domain of their own, so the only value that
    #: would let them in is `["gmail.com"]` -- which authorises every Google
    #: account on earth to run agents on their billing account. The safe
    #: configuration has to be expressible, or the unsafe one gets used.
    #:
    #: It is ADDITIVE and never subtractive: an address here is admitted even
    #: when its domain is not listed, and a domain that IS listed still works.
    #: Setting only this and leaving `allowed_domains` empty is the solo
    #: deployment; setting only domains is the organisation; setting both
    #: admits a named contractor alongside the company.
    #:
    #: It is not weaker authentication. The address still comes from a verified
    #: Google ID token or IAP assertion -- the same source the domain check
    #: reads -- so this changes WHICH verified identities are admitted, never
    #: whether the identity was verified.
    allowed_users: tuple[str, ...] = ()
    #: The Workspace user this service account acts AS when reading groups.
    #:
    #: Cloud Identity's Groups API does not authorize through GCP IAM -- a
    #: service account is not a Workspace principal and every lookup returns
    #: Error(2028). Domain-wide delegation, authorised in the Admin console
    #: against this service account's OAuth client id and scoped to
    #: cloud-identity.groups.readonly, lets it act as a real user that the
    #: API will answer.
    #:
    #: Empty disables delegation, which is correct for local development where
    #: google.auth.default() returns user credentials that cannot delegate
    #: anyway.
    groups_impersonate_user: str = ""

    #: Cloud Identity membership checks are a network round trip on the request
    #: path, so answers are cached briefly per (caller, group).
    group_cache_ttl_seconds: int = 120

    #: Pub/Sub topic that wakes the scheduler after a submission. Empty disables
    #: the nudge; the Cloud Scheduler safety tick still drains the queue.
    dispatch_topic: str = ""

    #: Base URL of the quota broker, which owns the account pool.
    #:
    #: The account routes in this API are a PROXY: refreshing an OAuth
    #: credential revokes the one it replaces, so the broker is the platform's
    #: single writer for subscription credentials and this service never writes
    #: one itself. Empty therefore means the account routes REFUSE with a 503 --
    #: there is no local path to fall back to, and inventing one would be the
    #: second writer the design exists to prevent.
    quota_broker_url: str = ""
    #: Audience for the ID token this service mints to call that broker.
    #:
    #: NOT THE SAME VALUE AS THE URL, and assuming it was fails only once
    #: deployed. terraform gives the broker service a CUSTOM AUDIENCE
    #: (`https://swarm-quota-broker.<env>.swarm.internal`, infra/locals.tf
    #: `push_audiences`) and the broker verifies `aud` against exactly that
    #: string, which is not its Cloud Run URL. Empty falls back to the URL,
    #: which is right for a plain service and for a local broker.
    quota_broker_audience: str = ""

    #: Maximum page size a caller may request on any list endpoint.
    max_page_size: int = 200
    default_page_size: int = 50

    #: Rate-limit burst. The sustained rate comes from the frozen Settings.
    rate_limit_burst: int = 40

    #: How a tenant's Google service account is spelled. This is a REFERENCE to
    #: an identity created out of band (terraform's tenancy module, or
    #: scripts/register-tenant.sh); the API never creates one. Terraform's
    #: spelling is the default because terraform is what seeds the tenant
    #: document in a deployed environment.
    tenant_service_account_prefix: str = "swarm-agent-worker"

    #: Kubernetes namespace prefix, same reasoning: terraform's
    #: `namespace_prefix` default.
    tenant_namespace_prefix: str = "swarm-tenant-"

    @property
    def project_id(self) -> str:
        return self.core.project_id

    @property
    def hardened(self) -> bool:
        """True outside local development, where safety checks are mandatory."""
        return self.core.environment.strip().lower() not in {"dev", "test", "local"}

    @classmethod
    def from_env(cls) -> "ApiSettings":
        core = Settings.from_env()
        if not _bool("REQUIRE_AUTH", True):
            # Historically parsed and then read by nothing, which made it look
            # like a working switch in .env.example and docker-compose.yml.
            raise ValueError(
                "REQUIRE_AUTH=false is refused: this service cannot serve an "
                "unauthenticated request in any environment. Every route resolves a "
                "tenant from a verified Google ID token, and without one there is no "
                "tenant to attribute work to. Remove the variable."
            )
        return cls(
            core=core,
            # One per backend service fronting this platform. Terraform sets
            # it; without it the IAP path is OFF rather than unpinned, because
            # an unpinned audience accepts an assertion minted by IAP for any
            # backend in any project.
            iap_audiences=_csv("IAP_AUDIENCES"),
            tenant_groups=_csv("TENANT_GROUPS"),
            admin_groups=_csv("ADMIN_GROUPS"),
            admin_users=_csv("ADMIN_USERS"),
            allowed_users=_csv("ALLOWED_USERS"),
            groups_impersonate_user=os.environ.get("GROUPS_IMPERSONATE_USER", "").strip(),
            group_cache_ttl_seconds=_int("GROUP_CACHE_TTL_SECONDS", 120),
            dispatch_topic=os.environ.get("DISPATCH_TOPIC", "").strip(),
            quota_broker_url=os.environ.get("QUOTA_BROKER_URL", "").strip(),
            quota_broker_audience=os.environ.get("QUOTA_BROKER_AUDIENCE", "").strip(),
            max_page_size=_int("MAX_PAGE_SIZE", 200),
            default_page_size=_int("DEFAULT_PAGE_SIZE", 50),
            rate_limit_burst=_int("RATE_LIMIT_BURST", max(40, core.requests_per_second * 2)),
            tenant_service_account_prefix=os.environ.get(
                "TENANT_SERVICE_ACCOUNT_PREFIX", "swarm-agent-worker"
            ).strip()
            or "swarm-agent-worker",
            tenant_namespace_prefix=os.environ.get(
                "TENANT_NAMESPACE_PREFIX", "swarm-tenant-"
            ).strip()
            or "swarm-tenant-",
        )
