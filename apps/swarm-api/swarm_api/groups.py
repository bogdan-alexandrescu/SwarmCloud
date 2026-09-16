"""Cloud Identity group membership, the way the project actually permits.

Read CONTRACT.md's "Verified operational constraint" section before changing
anything here. Tested live against saga-agents-staging:

    groups:lookup?groupKey.id=eng@saga.xyz                 WORKS
    groups/{id}/memberships:checkTransitiveMembership      WORKS
    groups/-/memberships:searchTransitiveGroups            403 Error(4013)

So we never ask "which groups is this caller in?" -- that enumeration is the
call that 403s, and it would also be more privilege than the platform needs.
We ask the inverse question once per admin-registered tenant group: "is this
caller in THAT group?". The platform therefore only ever learns about groups an
admin deliberately registered.

Every Cloud Identity call REQUIRES `x-goog-user-project: <project_id>`. Without
it the call fails 403 SERVICE_DISABLED naming gcloud's shared client project as
the consumer, which reads like a permissions problem but is a billing-project
problem.

`checkTransitiveMembership` is a network round trip on a request path that must
stay fast, so answers are cached per (caller, group) with a short TTL.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol

from swarm_common.models import utcnow

log = logging.getLogger(__name__)

CLOUD_IDENTITY_ROOT = "https://cloudidentity.googleapis.com/v1"
GROUPS_READONLY_SCOPE = "https://www.googleapis.com/auth/cloud-identity.groups.readonly"


class HttpSession(Protocol):
    """The slice of `requests.Session` / `AuthorizedSession` we depend on."""

    def get(self, url: str, **kwargs: Any) -> Any: ...


@dataclass
class _CacheEntry:
    value: Any
    expires_at: datetime


class GroupLookupError(Exception):
    """Cloud Identity could not answer. Never fatal: callers fall back."""


class MembershipResolver(Protocol):
    def groups_for(self, member_email: str, candidate_groups: tuple[str, ...]) -> tuple[str, ...]: ...


class CloudIdentityGroups:
    """Resolve membership by checking each admin-registered group in turn."""

    def __init__(
        self,
        project_id: str,
        *,
        session: HttpSession | None = None,
        ttl_seconds: int = 120,
        timeout_seconds: float = 5.0,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        if not project_id:
            raise ValueError("project_id is required: it is the x-goog-user-project value")
        self._project_id = project_id
        self._session = session
        self._ttl = timedelta(seconds=max(1, ttl_seconds))
        self._timeout = timeout_seconds
        self._now = now
        self._lock = threading.Lock()
        self._group_names: dict[str, _CacheEntry] = {}
        self._memberships: dict[tuple[str, str], _CacheEntry] = {}

    # -- plumbing ---------------------------------------------------------

    def _ensure_session(self) -> HttpSession:
        if self._session is None:
            # Imported lazily so unit tests never need application default
            # credentials just to construct the app.
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(scopes=[GROUPS_READONLY_SCOPE])
            self._session = AuthorizedSession(credentials)
        return self._session

    def _get(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        session = self._ensure_session()
        response = session.get(
            url,
            params=params,
            # Without this header the call 403s with SERVICE_DISABLED, naming
            # gcloud's shared client project as the consumer.
            headers={"x-goog-user-project": self._project_id},
            timeout=self._timeout,
        )
        status = getattr(response, "status_code", 200)
        if status != 200:
            body = ""
            try:
                body = response.text[:500]
            except Exception:  # pragma: no cover - defensive
                body = "<unreadable>"
            raise GroupLookupError(f"cloud identity returned {status}: {body}")
        return response.json()

    def _cached(self, store: dict, key: Any) -> Any | None:
        with self._lock:
            entry = store.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._now():
                store.pop(key, None)
                return None
            return entry.value

    def _store(self, store: dict, key: Any, value: Any) -> None:
        with self._lock:
            store[key] = _CacheEntry(value=value, expires_at=self._now() + self._ttl)

    # -- public API -------------------------------------------------------

    def group_resource_name(self, group_email: str) -> str:
        """`eng@saga.xyz` -> `groups/01abc...`.

        Uses groups:lookup, which is the call verified to work in this project.
        """
        key = group_email.lower()
        cached = self._cached(self._group_names, key)
        if cached is not None:
            return cached
        payload = self._get(
            f"{CLOUD_IDENTITY_ROOT}/groups:lookup",
            {"groupKey.id": group_email},
        )
        name = payload.get("name", "")
        if not name:
            raise GroupLookupError(f"groups:lookup returned no name for {group_email}")
        self._store(self._group_names, key, name)
        return name

    def is_member(self, member_email: str, group_email: str) -> bool:
        """Transitive membership check against ONE registered group."""
        key = (member_email.lower(), group_email.lower())
        cached = self._cached(self._memberships, key)
        if cached is not None:
            return bool(cached)

        group_name = self.group_resource_name(group_email)
        # The query is an expression, not a bare value: member_key_id == '<email>'.
        query = f"member_key_id == '{_escape_query_literal(member_email)}'"
        payload = self._get(
            f"{CLOUD_IDENTITY_ROOT}/{group_name}/memberships:checkTransitiveMembership",
            {"query": query},
        )
        has = bool(payload.get("hasMembership", False))
        self._store(self._memberships, key, has)
        return has

    def groups_for(self, member_email: str, candidate_groups: tuple[str, ...]) -> tuple[str, ...]:
        """Which of the admin-registered groups this caller belongs to.

        A lookup failure for one group is logged and treated as "not a member"
        rather than failing the request: the caller then falls back to their
        personal tenant, which is the documented behaviour for a user in no
        mapped group. Failing closed here would take the whole API down every
        time Cloud Identity had a bad minute.
        """
        found: list[str] = []
        for group in candidate_groups:
            try:
                if self.is_member(member_email, group):
                    found.append(group)
            except GroupLookupError as exc:
                log.warning("group membership check failed for group=%s: %s", group, exc)
            except Exception as exc:  # pragma: no cover - transport level
                log.warning("group membership check errored for group=%s: %r", group, exc)
        return tuple(found)


def _escape_query_literal(value: str) -> str:
    """Escape a value for embedding in a Cloud Identity query literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class StaticGroups:
    """Membership from a fixed mapping. For local runs and tests only."""

    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        self._mapping = {k.lower(): tuple(v) for k, v in mapping.items()}

    def groups_for(self, member_email: str, candidate_groups: tuple[str, ...]) -> tuple[str, ...]:
        mine = {g.lower() for g in self._mapping.get(member_email.lower(), ())}
        return tuple(g for g in candidate_groups if g.lower() in mine)


__all__ = [
    "CLOUD_IDENTITY_ROOT",
    "CloudIdentityGroups",
    "GroupLookupError",
    "MembershipResolver",
    "StaticGroups",
]
