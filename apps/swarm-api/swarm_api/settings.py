"""API-process settings.

`swarm_common.config.Settings` is frozen and holds everything the platform as a
whole needs. This module adds only what the API process alone needs and nothing
that belongs in the frozen contract: which Google groups map to tenants (in
admin-defined priority order), which groups grant admin rights, how long a
Cloud Identity membership answer may be cached, and which Pub/Sub topic wakes
the scheduler.

Concurrency limits deliberately do NOT appear here -- they live in Firestore so
an admin can change them through the admin API without a redeploy.
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
    tenant_groups: tuple[str, ...] = ()

    #: Membership in any of these grants the admin surface. Kept separate from
    #: tenant groups so an admin still belongs to a normal tenant for their own
    #: tasks.
    admin_groups: tuple[str, ...] = ()

    #: Cloud Identity membership checks are a network round trip on the request
    #: path, so answers are cached briefly per (caller, group).
    group_cache_ttl_seconds: int = 120

    #: Pub/Sub topic that wakes the scheduler after a submission. Empty disables
    #: the nudge; the Cloud Scheduler safety tick still drains the queue.
    dispatch_topic: str = ""

    #: Maximum page size a caller may request on any list endpoint.
    max_page_size: int = 200
    default_page_size: int = 50

    #: Rate-limit burst. The sustained rate comes from the frozen Settings.
    rate_limit_burst: int = 40

    #: Set false only for local development against the Firestore emulator.
    require_auth: bool = True

    @property
    def project_id(self) -> str:
        return self.core.project_id

    @classmethod
    def from_env(cls) -> "ApiSettings":
        core = Settings.from_env()
        return cls(
            core=core,
            tenant_groups=_csv("TENANT_GROUPS"),
            admin_groups=_csv("ADMIN_GROUPS"),
            group_cache_ttl_seconds=_int("GROUP_CACHE_TTL_SECONDS", 120),
            dispatch_topic=os.environ.get("DISPATCH_TOPIC", "").strip(),
            max_page_size=_int("MAX_PAGE_SIZE", 200),
            default_page_size=_int("DEFAULT_PAGE_SIZE", 50),
            rate_limit_burst=_int("RATE_LIMIT_BURST", max(40, core.requests_per_second * 2)),
            require_auth=_bool("REQUIRE_AUTH", True),
        )
