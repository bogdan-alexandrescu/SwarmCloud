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

How swarm-api is authorised to ask is `GROUPS_AUTH` (`GROUPS_AUTH_MODES`;
docs/design/day-one-install.md section 8): `admin_role`, the Workspace "Groups
Reader" admin role assigned to swarm-api's own service account, which then asks
as itself; or `delegation`, domain-wide delegation acting as
`GROUPS_IMPERSONATE_USER`, the fallback and the default.
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

#: `GROUPS_AUTH`'s values. `admin_role`: swarm-api's service account holds the
#: Workspace "Groups Reader" admin role and calls the Groups API as itself, so
#: `delegation.delegate()` is never reached. `delegation`: it acts as
#: `GROUPS_IMPERSONATE_USER` through domain-wide delegation. The default is
#: `delegation` because that is what the live deployment is set up for; installs
#: set `admin_role` (BGD's Q2 answer, design section 14).
GROUPS_AUTH_ADMIN_ROLE = "admin_role"
GROUPS_AUTH_DELEGATION = "delegation"
GROUPS_AUTH_MODES = (GROUPS_AUTH_ADMIN_ROLE, GROUPS_AUTH_DELEGATION)

#: How long a caller's last answer for a group may stand in for one Cloud
#: Identity cannot give now (`groups_for`). A day: the hourly doctor re-probes
#: every registered group and alerts on one that does not answer, so a broken
#: group is named long before that; past a day the answer is too old to bet a
#: tenant on, and the 503 returns.
LAST_ANSWER_SECONDS = 24 * 60 * 60


def parse_groups_auth(raw: str | None) -> str:
    """`GROUPS_AUTH` normalised, or ValueError naming the variable. Unset or
    blank is `delegation`."""
    mode = (raw or "").strip().lower() or GROUPS_AUTH_DELEGATION
    if mode not in GROUPS_AUTH_MODES:
        raise ValueError(
            f"GROUPS_AUTH must be one of {', '.join(GROUPS_AUTH_MODES)}, got {raw!r}"
        )
    return mode


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
    def groups_for(
        self, member_email: str, candidate_groups: tuple[str, ...], *, fresh: bool = False
    ) -> tuple[str, ...]: ...


class CloudIdentityGroups:
    """Resolve membership by checking each admin-registered group in turn."""

    def __init__(
        self,
        project_id: str,
        *,
        session: HttpSession | None = None,
        impersonate_user: str | None = None,
        auth_mode: str = GROUPS_AUTH_DELEGATION,
        ttl_seconds: int = 120,
        last_answer_seconds: int = LAST_ANSWER_SECONDS,
        timeout_seconds: float = 5.0,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        if not project_id:
            raise ValueError("project_id is required: it is the x-goog-user-project value")
        self._project_id = project_id
        self._auth_mode = parse_groups_auth(auth_mode)
        # With the admin role there is no one to act as: a subject still
        # rendered beside it is ignored rather than delegated to.
        self._impersonate_user = (
            None if self._auth_mode == GROUPS_AUTH_ADMIN_ROLE
            else (impersonate_user or "").strip() or None
        )
        self._session = session
        self._ttl = timedelta(seconds=max(1, ttl_seconds))
        self._last_answer_window = timedelta(seconds=max(0, last_answer_seconds))
        self._timeout = timeout_seconds
        self._now = now
        self._lock = threading.Lock()
        self._group_names: dict[str, _CacheEntry] = {}
        self._memberships: dict[tuple[str, str], _CacheEntry] = {}
        # (caller, group) -> the last answer Cloud Identity gave and when.
        # Outlives the cache: read only when a group cannot answer now.
        self._last_answers: dict[tuple[str, str], tuple[bool, datetime]] = {}

    # -- plumbing ---------------------------------------------------------

    def _ensure_session(self) -> HttpSession:
        if self._session is None:
            # Imported lazily so unit tests never need application default
            # credentials just to construct the app.
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(scopes=[GROUPS_READONLY_SCOPE])

            # DOMAIN-WIDE DELEGATION, and the reason this platform needs it.
            #
            # Cloud Identity's Groups API does NOT authorize through GCP IAM.
            # It has three modes -- admin, non-admin and namespace -- and a
            # *.gserviceaccount.com identity satisfies none of them, because it
            # is not a principal in the Workspace domain. Every lookup returns
            # "Error(2028): Permission denied for resource <group>", which
            # reads like a missing role and is not one:
            # roles/cloudidentity.groupsReader is an ALPHA role with no
            # included permissions, and granting it at the organization
            # changed nothing when we tried.
            #
            # With delegation the service account ACTS AS a real Workspace
            # user, which does satisfy those modes. The grant is authorised in
            # the Admin console against this service account's OAuth client id
            # and is scoped to cloud-identity.groups.readonly: read group
            # membership, nothing else, no write, no mail, no files.
            #
            # `with_subject` exists ONLY on credentials loaded from a
            # service-account key file. This originally guarded on it directly,
            # on the assumption that the other case was a developer's user
            # credentials -- but the production case is Cloud Run, whose
            # metadata credentials have no `with_subject` either. So the
            # delegation branch never fired where it mattered, and every lookup
            # fell back to the service account's own identity and 403'd with
            # Error(2028), which reads exactly like the missing-role problem
            # delegation was introduced to solve. Observed live on 2026-09-20.
            #
            # `delegation.delegate` keeps the key-file path and adds the
            # keyless one: build the assertion, have Google sign it with the key
            # we are not allowed to hold, exchange it for a token that acts as
            # the subject. See swarm_api/delegation.py.
            #
            # GROUPS_AUTH=admin_role skips all of it: the Workspace "Groups
            # Reader" admin role, assigned to this service account's email,
            # lets it call the Groups API as itself (design section 8), and
            # `_impersonate_user` is None in that mode.
            if self._impersonate_user:
                from . import delegation

                credentials = delegation.delegate(
                    credentials,
                    subject=self._impersonate_user,
                    scopes=[GROUPS_READONLY_SCOPE],
                )

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
        """`eng@example.com` -> `groups/01abc...`.

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

    def is_member(self, member_email: str, group_email: str, *, fresh: bool = False) -> bool:
        """Transitive membership check against ONE registered group.

        `fresh` skips the cached answer and refreshes it with the one Cloud
        Identity gives now. An await-input answer asks this way
        (docs/design/human-steps.md §4.2): the ordinary request path's cached
        answer is up to `group_cache_ttl_seconds` old, and a person removed
        from the group a minute ago would still pass it. Only the membership
        is asked again; the group's resource name never changes for an email
        and stays cached.
        """
        key = (member_email.lower(), group_email.lower())
        cached = None if fresh else self._cached(self._memberships, key)
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
        with self._lock:
            self._last_answers[key] = (has, self._now())
        return has

    def _known_outside(self, member_email: str, group_email: str) -> bool:
        """True when Cloud Identity's last answer, within the window, was that
        `member_email` is NOT in `group_email`."""
        with self._lock:
            last = self._last_answers.get((member_email.lower(), group_email.lower()))
        if last is None:
            return False
        has, at = last
        return not has and self._now() - at < self._last_answer_window

    def groups_for(
        self, member_email: str, candidate_groups: tuple[str, ...], *, fresh: bool = False
    ) -> tuple[str, ...]:
        """Which of the admin-registered groups this caller belongs to.

        `fresh` asks every candidate past the cache (`is_member`), and never
        leans on a last answer (below).

        `candidate_groups` arrives in ADMIN PRIORITY ORDER, which is the order
        `resolve_tenant` walks, so a failure only matters when it lands ABOVE the
        first confirmed membership. That is the exact rule applied here:

          * a failure at a lower priority than a confirmed match cannot change
            the tenant, so it is logged and swallowed -- one flaky group must not
            take down the API;
          * a failure at a higher priority, or with no match at all, COULD change
            the tenant, so it raises -- unless this caller's last answer for that
            group, within `LAST_ANSWER_SECONDS`, was "not a member". Then the
            group is read as the "no" it last gave, so one unreadable group
            fails only its own members (and callers never asked about it), not
            everyone it outranks (design section 8, the 503-everyone trap).

        Failing open in that second case is not a cheap degradation: the tenant
        decides which Secret Manager secret, which GCS prefix and which namespace
        the caller's work runs under, so a caller silently demoted to their
        personal tenant during a Cloud Identity blip submits work their group
        cannot see afterwards and that may park as CREDENTIAL_MISSING or run
        against a different key. A 503 the client retries is the cheaper failure.
        """
        found: list[str] = []
        first_match: int | None = None
        first_failure: int | None = None
        failures: list[str] = []
        for index, group in enumerate(candidate_groups):
            try:
                member = self.is_member(member_email, group, fresh=fresh)
            except Exception as exc:
                # GroupLookupError, or anything the transport throws.
                if not fresh and self._known_outside(member_email, group):
                    log.warning(
                        "group membership check failed for group=%s; the caller's last "
                        "answer was not a member, so it stands: %s", group, exc)
                    continue
                log.warning("group membership check failed for group=%s: %r", group, exc)
                failures.append(group)
                first_failure = index if first_failure is None else first_failure
                continue
            if member:
                found.append(group)
                first_match = index if first_match is None else first_match

        if first_failure is not None and (first_match is None or first_failure < first_match):
            raise GroupLookupError(
                "cloud identity did not answer for "
                + ", ".join(failures)
                + "; a higher-priority group is unknown, so the caller's tenant "
                "cannot be resolved without possibly filing their work under the "
                "wrong one"
            )
        return tuple(found)


def check_group_readable(resolver: MembershipResolver, member_email: str,
                         group_email: str) -> bool:
    """The directory check of a group workspace request
    (docs/design/workspace-kinds.md §4.2.3, check 7): ask whether
    `member_email` -- the requesting admin -- is in THAT ONE group, past the
    cache. Any answer, true or false, proves the group exists and is readable
    under swarm-api's delegation; the boolean itself decides nothing.

    It is the same per-group question `groups_for` asks on every request, one
    `checkTransitiveMembership` with `x-goog-user-project`, so no call lists
    groups (`searchTransitiveGroups` 403s here). A lookup that fails raises
    `GroupLookupError`: with one candidate, `groups_for` raises on a failure,
    and anything else the transport throws is folded into the same error."""
    try:
        return bool(resolver.groups_for(member_email, (group_email,), fresh=True))
    except GroupLookupError:
        raise
    except Exception as exc:  # noqa: BLE001 - transport level, one answer
        raise GroupLookupError(f"directory check errored: {type(exc).__name__}") from exc


def _escape_query_literal(value: str) -> str:
    """Escape a value for embedding in a Cloud Identity query literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class StaticGroups:
    """Membership from a fixed mapping. For local runs and tests only."""

    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        self._mapping = {k.lower(): tuple(v) for k, v in mapping.items()}

    def groups_for(
        self, member_email: str, candidate_groups: tuple[str, ...], *, fresh: bool = False
    ) -> tuple[str, ...]:
        # No cache, so every answer is already fresh.
        mine = {g.lower() for g in self._mapping.get(member_email.lower(), ())}
        return tuple(g for g in candidate_groups if g.lower() in mine)


__all__ = [
    "CLOUD_IDENTITY_ROOT",
    "CloudIdentityGroups",
    "GROUPS_AUTH_ADMIN_ROLE",
    "GROUPS_AUTH_DELEGATION",
    "GROUPS_AUTH_MODES",
    "GroupLookupError",
    "LAST_ANSWER_SECONDS",
    "MembershipResolver",
    "StaticGroups",
    "check_group_readable",
    "parse_groups_auth",
]
