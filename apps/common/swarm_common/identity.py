"""Caller identity and tenant resolution.

Auth is a Google-issued ID token restricted to the configured hosted domains.
There is no shared platform bearer token anywhere in this system: a shared
secret carries no identity, and without identity there is no tenant to attribute
a task to, no way to scope a list response, and no boundary to enforce.

A TENANT IS A GOOGLE GROUP. `eng@saga.xyz` is one tenant whose members share a
quota budget, provider keys and artifacts. A user in no mapped group falls back
to a personal tenant so nobody is ever hard-blocked from the platform.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


class AuthError(Exception):
    """Authentication failed. Never include token material in the message."""


@dataclass(frozen=True)
class Principal:
    email: str
    subject: str
    domain: str
    groups: tuple[str, ...] = ()


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
        named for, because `swarm-t-<id>` must fit in 30 characters. Silently
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


def resolve_tenant(principal: Principal, group_priority: tuple[str, ...]) -> str:
    """Pick the caller's tenant.

    `group_priority` is the admin-ordered list of group emails that map to
    tenants. First match wins, so a user in several mapped groups lands
    deterministically in the same tenant on every request -- which matters
    because the tenant determines which secrets and which GCS prefix they get.
    """
    member_of = {g.lower() for g in principal.groups}
    for group in group_priority:
        if group.lower() in member_of:
            return tenant_id_for_group(group)
    return tenant_id_for_user(principal.email)
