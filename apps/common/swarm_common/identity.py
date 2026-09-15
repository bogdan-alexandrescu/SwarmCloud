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


def tenant_id_for_group(group_email: str) -> str:
    """`eng@saga.xyz` -> `eng`. Stable, and safe as a k8s namespace suffix."""
    local = group_email.split("@", 1)[0].lower()
    slug = _TENANT_SAFE.sub("-", local).strip("-")
    if not slug:
        raise ValueError(f"cannot derive a tenant id from {group_email!r}")
    return slug


def tenant_id_for_user(email: str) -> str:
    """Personal fallback tenant, namespaced so it cannot collide with a group."""
    local = email.split("@", 1)[0].lower()
    slug = _TENANT_SAFE.sub("-", local).strip("-")
    if not slug:
        raise ValueError(f"cannot derive a tenant id from {email!r}")
    return f"u-{slug}"


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
