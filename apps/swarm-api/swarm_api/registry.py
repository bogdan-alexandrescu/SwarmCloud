"""The registry of job-made group and service workspaces
(docs/design/workspace-kinds.md §4.4, lane WK-RESOLVE; part of #847).

WHAT IT ANSWERS. Two questions `Authenticator` asks on every request that
resolves a tenant:

  * `registered_groups()`: the group emails of the `ready` records of kind
    `group`, in `ready_at` order. `auth.py` walks them AFTER every
    `TENANT_GROUPS` entry, so a Terraform group always wins and a job-made
    group is a member's default only when no Terraform group is above it
    (owner answer Q4 (a), 2026-10-19). Membership is still asked one group at
    a time with `checkTransitiveMembership` (`groups.py`); nothing here or
    there lists a caller's groups.
  * `admitted_services()`: the account emails of the `ready` records of kind
    `service`, admitted at the front door beside `ALLOWED_USERS`.

ONLY `ready` COUNTS. A record being built (`requested` to `applying`), one
that stopped (`needs_owner`, `failed`, `denied`) and the reserved
`deprovisioned` register nothing, so a half-made workspace is not a tenant
anyone can name and a service account is not admitted before its identity,
secrets and namespace have been read back (§4.8). A service record that is not
ready is still reported (`pending_service`), so its account is told
WORKSPACE_NOT_READY rather than "not an allowed domain".

A RECORD IS RE-CHECKED HERE, NOT TRUSTED. swarm-api validates a request
and the job writes `ready`, but the record is the only thing between a
Firestore document and a tenant boundary, so each one must still pass the
request's own checks NOW, against today's settings: the same
`workspaces.validate_kind_request` over `KindRules.from_settings`, one rule
for both paths and nothing to drift. So a group is in an allowed domain, is
not a service account or an `ADMIN_GROUPS` entry, and derives neither a `u-`
id (a person's own tenant) nor a Terraform tenant's id; a service is an
account of THIS project (Q7 (a)), not one of the platform's `swarm-` accounts,
not a secret admin, and not already resolved by ALLOWED_USERS or a listing.
The id the check derives must be the record's document id.

A SERVICE IS PINNED TO ITS UNIQUE ID. GCP lets a deleted account be recreated
under the same address (the reason a listed account pins its `uid`,
`swarm_common.identity.TenantMember`), so an address alone would admit the
recreated account into the old one's tenant. The job's S1 writes the account's
`service.unique_id` (§4.1); `admitted_services` maps each address to it, and
`auth.py` admits a token only when its subject is that id. A ready record
without one admits nobody.

A record that fails is left out and logged by workspace id, never by email.

CACHED, AND NEVER A GUESS. Read with two equality queries, cached for
`cache_seconds` (60). A read that fails serves the last good one for up to
`stale_seconds` (600) and logs `registry_stale`; with no good read in that
window it raises `RegistryUnavailable`, a 503. Without the registry swarm-api
cannot know whether a caller belongs to a job-made group, and serving would
risk a misfiled task -- the reason a failed group lookup is a 503 too
(Q5 (a)). A failure is never cached, so the next request reads again.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping

from google.cloud.firestore_v1.base_query import FieldFilter

from .errors import ApiError, UpstreamUnavailable
from .workspaces import (
    GROUP,
    NONE,
    READY,
    SERVICE,
    WORKSPACES,
    KindRules,
    state_of,
    validate_kind_request,
)

log = logging.getLogger(__name__)

#: §4.4: how long one instance trusts a read, and how long past a failure it
#: keeps serving the last good one. The design's numbers; `ApiSettings`
#: carries the deployment's (WORKSPACE_REGISTRY_CACHE_SECONDS,
#: WORKSPACE_REGISTRY_STALE_SECONDS).
CACHE_SECONDS = 60.0
STALE_SECONDS = 600.0


class RegistryUnavailable(UpstreamUnavailable):
    """The registry has not been read within `stale_seconds`: 503, retry."""

    code = "registry_unavailable"


@dataclass(frozen=True)
class RegistrySnapshot:
    """One read of the registry. `services` maps each admitted account to the
    `unique_id` its token's subject must carry. `pending` maps a service
    account whose record is not `ready` to that record's
    `{workspace_id, state}`, for the 403."""

    groups: tuple[str, ...] = ()
    services: Mapping[str, str] = field(default_factory=dict)
    pending: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


#: What an Authenticator built without a registry reads: nothing registered,
#: nothing admitted, which is exactly the behaviour before the registry.
EMPTY = RegistrySnapshot()


def _address(value: Any) -> str:
    return str(value or "").strip().lower()


def _ready_order(item: tuple[str, str, Any]) -> tuple[int, Any, str]:
    _, tenant_id, ready_at = item
    if isinstance(ready_at, datetime):
        return (0, ready_at.timestamp(), tenant_id)
    return (1, 0.0, tenant_id)


class WorkspaceRegistry:
    """The `ready` group and service records, read and cached.

    `db` is the API's own Firestore client, and nothing else is held. Nothing
    is read at construction, so building one costs nothing until the first
    request."""

    def __init__(
        self,
        db: Any,
        rules: KindRules,
        *,
        cache_seconds: float = CACHE_SECONDS,
        stale_seconds: float = STALE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._rules = rules
        self._terraform_groups = frozenset(rules.tenant_groups)
        self._cache_seconds = cache_seconds
        self._stale_seconds = max(stale_seconds, cache_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._good: RegistrySnapshot | None = None
        self._good_at = 0.0

    @classmethod
    def from_settings(cls, db: Any, settings: Any,
                      *, clock: Callable[[], float] = time.monotonic) -> "WorkspaceRegistry":
        return cls(
            db,
            KindRules.from_settings(settings),
            cache_seconds=float(getattr(settings, "workspace_registry_cache_seconds",
                                        CACHE_SECONDS)),
            stale_seconds=float(getattr(settings, "workspace_registry_stale_seconds",
                                        STALE_SECONDS)),
            clock=clock,
        )

    # -- the two questions ------------------------------------------------------

    def registered_groups(self) -> tuple[str, ...]:
        return self.snapshot().groups

    def admitted_services(self) -> frozenset[str]:
        return frozenset(self.snapshot().services)

    def snapshot(self) -> RegistrySnapshot:
        """The current registry: cached, else read, else the last good read
        while it is younger than `stale_seconds`, else `RegistryUnavailable`."""
        now = self._clock()
        with self._lock:
            good, good_at = self._good, self._good_at
        if good is not None and now - good_at < self._cache_seconds:
            return good
        try:
            fresh = self._read()
        except Exception as exc:  # noqa: BLE001 - any Firestore failure
            if good is not None and now - good_at < self._stale_seconds:
                log.warning("registry_stale: serving the read from %.0f s ago (%s)",
                            now - good_at, type(exc).__name__)
                return good
            log.warning("registry_unavailable: no good read within %.0f s (%s)",
                        self._stale_seconds, type(exc).__name__)
            raise RegistryUnavailable(
                "the workspace registry could not be read, and it decides which tenants "
                "you may act in; retry shortly"
            ) from None
        with self._lock:
            self._good, self._good_at = fresh, now
        return fresh

    # -- the read ---------------------------------------------------------------

    def _records(self, kind: str) -> list[tuple[str, dict[str, Any]]]:
        query = self._db.collection(WORKSPACES).where(filter=FieldFilter("kind", "==", kind))
        return [(snap.id, snap.to_dict() or {}) for snap in query.stream()]

    def _read(self) -> RegistrySnapshot:
        groups: list[tuple[str, str, Any]] = []
        for tenant_id, record in self._records(GROUP):
            if state_of(record) != READY:
                continue
            group = self._group(tenant_id, record)
            if group:
                groups.append((group, tenant_id, record.get("ready_at")))
        seen: set[str] = set()
        ordered: list[str] = []
        for group, tenant_id, _ in sorted(groups, key=_ready_order):
            if tenant_id not in seen:
                seen.add(tenant_id)
                ordered.append(group)

        services: dict[str, str] = {}
        pending: dict[str, dict[str, Any]] = {}
        for tenant_id, record in self._records(SERVICE):
            account = self._valid(SERVICE, tenant_id, record)
            if not account:
                continue
            state = state_of(record)
            if state == READY:
                unique_id = str((record.get("service") or {}).get("unique_id") or "").strip()
                if not unique_id:
                    self._dropped(SERVICE, record, "no_unique_id")
                    continue
                services[account] = unique_id
            elif state != NONE:
                pending[account] = {"workspace_id": record.get("workspace_id"), "state": state}
        for account in services:
            pending.pop(account, None)
        return RegistrySnapshot(groups=tuple(ordered), services=services, pending=pending)

    def _group(self, tenant_id: str, record: Mapping[str, Any]) -> str:
        if _address(record.get("principal")) in self._terraform_groups:
            return ""  # already registered, first, by TENANT_GROUPS
        return self._valid(GROUP, tenant_id, record)

    def _valid(self, kind: str, tenant_id: str, record: Mapping[str, Any]) -> str:
        """The record's principal when the request's checks pass it today and
        derive exactly this document id; "" (and a log line) otherwise."""
        try:
            derived, principal = validate_kind_request(kind, str(record.get("principal") or ""),
                                                       self._rules)
        except (ApiError, ValueError) as exc:
            self._dropped(kind, record, getattr(exc, "code", "invalid"))
            return ""
        if derived != tenant_id:
            self._dropped(kind, record, "id_mismatch")
            return ""
        return principal

    @staticmethod
    def _dropped(kind: str, record: Mapping[str, Any], reason: str) -> None:
        log.warning("registry dropped a %s record: workspace=%s reason=%s",
                    kind, record.get("workspace_id") or "-", reason)


__all__ = [
    "CACHE_SECONDS",
    "EMPTY",
    "RegistrySnapshot",
    "RegistryUnavailable",
    "STALE_SECONDS",
    "WorkspaceRegistry",
]
