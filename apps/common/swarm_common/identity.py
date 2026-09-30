"""Caller identity and tenant resolution.

Auth is a Google-issued ID token restricted to the configured hosted domains.
There is no shared platform bearer token anywhere in this system: a shared
secret carries no identity, and without identity there is no tenant to attribute
a task to, no way to scope a list response, and no boundary to enforce.

A TENANT IS A GOOGLE GROUP. `eng@saga.xyz` is one tenant whose members share a
quota budget, provider keys and artifacts. A user in no mapped group falls back
to a personal tenant so nobody is ever hard-blocked from the platform.

A TENANT MAY ALSO LIST SERVICE ACCOUNTS (contract request 30). A service
account is not a Workspace principal, and making it a member of the tenant's
group would hand it every grant that group holds across the company. So a
listed account resolves to its tenant by an EXACT match on the email AND the
unique id its verified token carries, before any group is consulted -- never
by a pattern, a prefix or a domain.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass


class AuthError(Exception):
    """Authentication failed. Never include token material in the message."""


@dataclass(frozen=True)
class Principal:
    email: str
    subject: str
    domain: str
    groups: tuple[str, ...] = ()


#: A user-managed service account: `<account id>@<project id>.iam.gserviceaccount.com`,
#: both halves 6-30 characters as GCP names them. Google-managed accounts
#: (`<number>-compute@developer.gserviceaccount.com`, `<project>@appspot...`)
#: do NOT match on purpose: every workload in a project can run as those, so
#: listing one would put the whole project in the tenant. A human address
#: cannot match either -- no Workspace domain ends in `.iam.gserviceaccount.com`.
#: This regex alone does not pin the PROJECT: `settings.py` does that at
#: startup with an `endswith` check this frozen module has no project id to
#: perform itself -- see contract request 30, item 2. terraform/infra/variables.tf
#: validates `tenants.*.service_accounts` with the same expression, and
#: scripts/lib/check-contract-parity.sh holds the two equal.
SERVICE_ACCOUNT_EMAIL = re.compile(
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$"
)


@dataclass(frozen=True)
class TenantMember:
    """A service account a tenant lists besides its group or user principal.

    `kind` and `principal` are the TENANT's, not the account's: the tenant id
    is derived from them by the same function that derives it for a human
    member, so a listed account and the group it stands beside can never name
    two different tenants.

    Every field is normalised ONCE, here, rather than at each comparison.
    """

    email: str       # swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com
    kind: str        # "group" | "user" -- the tenant's kind
    principal: str   # eng@saga.xyz -- the tenant's principal
    uid: str         # the account's OAuth2 unique id (the token's `sub`
                     # claim), from Terraform's
                     # data.google_service_account.unique_id -- pinned beside
                     # the email so an account deleted and recreated under
                     # the same address, which GCP permits, does not inherit
                     # the old one's tenant.

    def __post_init__(self) -> None:
        object.__setattr__(self, "email", self.email.strip().lower())
        object.__setattr__(self, "kind", self.kind.strip().lower())
        object.__setattr__(self, "principal", self.principal.strip().lower())
        object.__setattr__(self, "uid", self.uid.strip())


def tenant_member_for(
    email: str, subject: str, members: Iterable[TenantMember]
) -> TenantMember | None:
    """The listed member whose email AND unique id (`sub`) both match, or None.

    Exact equality on both, nothing else. An entry that is not a user-managed
    service-account address is never matched even when listed. An email match
    with no matching `uid` is treated as no match at all -- the caller falls
    through to today's refusal exactly as an unlisted account would. An
    address listed under two tenants raises rather than being resolved by
    order.
    """
    wanted = email.strip().lower()
    if not SERVICE_ACCOUNT_EMAIL.fullmatch(wanted):
        return None
    found = [m for m in members if m.email == wanted and m.uid == subject]
    tenants = {(m.kind, m.principal) for m in found}
    if len(tenants) > 1:
        raise AuthError("a service account is listed under more than one tenant")
    return found[0] if found else None


_TENANT_SAFE = re.compile(r"[^a-z0-9-]+")


#: The worker service account is `swarm-agent-worker-<tenant>` and GCP caps a
#: service account id at 30 characters. The prefix is 19, so a tenant id has 11.
#: This MUST track terraform/modules/tenancy, scripts/register-tenant.sh and
#: kubernetes/render.py; an earlier value of 22 was computed from a `swarm-t-`
#: prefix that no longer exists, and would mint ids no worker could be named for.
_GSA_PREFIX = "swarm-agent-worker-"
_GSA_ACCOUNT_ID_MAX = 30
_MAX_TENANT_ID = _GSA_ACCOUNT_ID_MAX - len(_GSA_PREFIX)


def _slug(principal: str, prefix: str = "") -> str:
    """Slugify an email local part into a tenant id that cannot collide.

    Two problems make the naive `split("@")[0]` version unsafe, and both are
    reachable inside a single domain:

      * SLUGIFICATION IS LOSSY. `eng.team@` and `eng-team@` both reduce to
        `eng-team`. Two distinct Google groups would silently become ONE tenant,
        sharing a namespace, a service account, provider credentials and
        artifacts -- the exact cross-tenant merge the whole design exists to
        prevent.
      * LENGTH. A long group name yields an id no GCP service account can be
        named for, because `swarm-agent-worker-<id>` must fit in 30 characters. Silently
        truncating reintroduces collisions at the truncation boundary.

    So whenever the slug is not a faithful, short-enough rendering of the local
    part, a short digest of the FULL principal is appended. The digest is
    deterministic, so a given group always resolves to the same tenant, and
    readable ids like `eng` survive untouched.
    """
    local = principal.split("@", 1)[0].lower()
    slug = _TENANT_SAFE.sub("-", local).strip("-")
    if not slug:
        raise ValueError(f"cannot derive a tenant id from {principal!r}")

    budget = _MAX_TENANT_ID - len(prefix)
    lossy = slug != local
    too_long = len(slug) > budget

    if lossy or too_long:
        digest = hashlib.sha256(principal.lower().encode("utf-8")).hexdigest()[:6]
        keep = max(1, budget - len(digest) - 1)
        slug = f"{slug[:keep].rstrip('-')}-{digest}"

    return f"{prefix}{slug}"


def tenant_id_for_group(group_email: str) -> str:
    """`eng@saga.xyz` -> `eng`. Stable, collision-free, safe as a k8s namespace."""
    return _slug(group_email)


def tenant_id_for_user(email: str) -> str:
    """Personal fallback tenant, namespaced so it cannot collide with a group."""
    return _slug(email, prefix="u-")


def assert_allowed_domain(email: str, allowed: tuple[str, ...]) -> str:
    if "@" not in email:
        raise AuthError("token subject is not an email address")
    domain = email.rsplit("@", 1)[1].lower()
    if domain not in {d.lower() for d in allowed}:
        raise AuthError(f"domain {domain} is not permitted")
    return domain


def resolve_tenant(
    principal: Principal,
    group_priority: tuple[str, ...],
    service_accounts: tuple[TenantMember, ...] = (),
) -> str:
    """Pick the caller's tenant.

    A caller whose verified email AND subject match a listed service account
    resolves to the tenant that lists it, FIRST and regardless of
    `principal.groups`.

    Otherwise `group_priority` is the admin-ordered list of group emails that
    map to tenants. First match wins, so a user in several mapped groups lands
    deterministically in the same tenant on every request -- which matters
    because the tenant determines which secrets and which GCS prefix they get.
    """
    member = tenant_member_for(principal.email, principal.subject, service_accounts)
    if member is not None:
        if member.kind == "group":
            return tenant_id_for_group(member.principal)
        if member.kind == "user":
            return tenant_id_for_user(member.principal)
        # settings.py already refuses this shape at startup; reaching it here
        # means that check was bypassed.
        raise AuthError(f"a listed tenant member names an unknown kind {member.kind!r}")

    member_of = {g.lower() for g in principal.groups}
    for group in group_priority:
        if group.lower() in member_of:
            return tenant_id_for_group(group)
    return tenant_id_for_user(principal.email)
