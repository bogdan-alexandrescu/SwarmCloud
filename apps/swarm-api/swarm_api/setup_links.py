"""One-time links minted by Admin › Setup (docs/design/day-one-plan.md §5.2,
§5.3; review finding 21, review-fix finding 1; lane DAYONE-SETUP-API-1).

ONE ROUTE, THREE KINDS. `POST /v1/admin/setup/links` mints:

* `pool-account`: a sign-in link for a named Claude pool-account owner. The
  owner must be a member of the FIRST team's group, and the link is refused
  until that team exists, because the existing sign-in (`/v1/accounts/authorize`
  -> `/exchange`) files the account into the caller's selected workspace and
  needs role `member` there. Redeeming answers the first team's workspace id,
  which the console sends as `X-Swarm-Tenant` on that sign-in, so the account
  lands with `owner_tenant` = the first team.
* `team-git`: the form for a team's git slot `swarm-tenant-<tenant>-git`. The
  value is written by swarm-api straight into Secret Manager; it never passes
  through a terminal, a log or this repository (CLAUDE.md, forge-token rule).
* `addon-secret`: the form for one add-on's secret, the add-on named by the
  label (`ADDON_SECRETS`), written the same way.

A LINK IS A HASH. The token is returned once, in the mint's answer; Firestore
keeps only its SHA-256 (`setup_links/<digest>`), so a reader of the database
cannot open a link. Each link is for one person (their verified email, checked
on redemption), single use (consumed in a transaction) and expires 24 hours
after it is minted.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .errors import Forbidden, NotFound

COLLECTION = "setup_links"
LINK_TTL = timedelta(hours=24)

POOL_ACCOUNT = "pool-account"
TEAM_GIT = "team-git"
ADDON_SECRET = "addon-secret"
KINDS: tuple[str, ...] = (POOL_ACCOUNT, TEAM_GIT, ADDON_SECRET)

#: The console page each kind opens; `{token}` is the one-time token. One
#: page serves the three kinds (review finding 21).
LINK_FORM = "/admin/setup/link/{token}"
FORMS: dict[str, str] = dict.fromkeys(KINDS, LINK_FORM)

#: Add-on name -> the one secret its link may write (design §12's add-ons).
#: The CI add-on has no link: its credentials are Workload Identity and the
#: merge App, set up by its own runbook.
ADDON_SECRETS: dict[str, str] = {
    "bright-data-egress": "swarm-egress-credential",
    "tailscale": "swarm-tailscale-oauth",
    "typesafe": "swarm-typesafe-fwd-key",
    "brave-search": "swarm-search-api-key",
}


def team_git_secret(tenant_id: str) -> str:
    return f"swarm-tenant-{tenant_id}-git"


# The codes are the established `not_found` and `forbidden`, not new ones: a
# new 4xx code ships report-only behind a refusal switch (swarm_api.refusals,
# owner decision 2026-10-08), and continuing past either of these is the
# breach itself, a link opened twice or by someone else. The served sentence
# tells the person which.


class LinkInvalid(NotFound):
    """Unknown, used or expired: one answer for all three, so a probe learns
    nothing about which tokens ever existed."""


class LinkNotYours(Forbidden):
    pass


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SetupLinks:
    def __init__(self, db: Any, *, now: Callable[[], datetime]) -> None:
        self._db = db
        self._now = now

    def _ref(self, token_digest: str) -> Any:
        return self._db.collection(COLLECTION).document(token_digest)

    def mint(self, *, kind: str, label: str, owner: str, by: str,
             tenant_id: str | None = None, team_id: str | None = None) -> tuple[str, dict[str, Any]]:
        """A fresh token and the record kept under its digest."""
        if kind not in KINDS:
            raise NotFound(f"no link kind {kind!r}")
        token = secrets.token_urlsafe(32)
        now = self._now()
        record = {
            "kind": kind,
            "label": label,
            "owner": owner.strip().lower(),
            "tenant_id": tenant_id,
            "team_id": team_id,
            "created_by": by,
            "created_at": now,
            "expires_at": now + LINK_TTL,
            "used_at": None,
        }
        self._ref(digest(token)).set(record)
        return token, record

    def _check(self, record: dict[str, Any] | None, caller: str) -> dict[str, Any]:
        if record is None or record.get("used_at") is not None:
            raise LinkInvalid("this link is not valid: ask the admin for a new one")
        expires = record.get("expires_at")
        if not isinstance(expires, datetime) or self._now() >= expires:
            raise LinkInvalid("this link is not valid: ask the admin for a new one")
        if record.get("owner") != caller:
            raise LinkNotYours("this link was made for someone else")
        return record

    def peek(self, token: str, *, email: str) -> dict[str, Any]:
        """The link `email` may open, refused as `redeem` would, NOT consumed.

        The form kinds check their value and write it before `redeem`, so a
        missing value or a Secret Manager failure leaves the link usable."""
        snapshot = self._ref(digest(token or "")).get()
        record = dict(snapshot.to_dict() or {}) if snapshot.exists else None
        return dict(self._check(record, (email or "").strip().lower()))

    def redeem(self, token: str, *, email: str) -> dict[str, Any]:
        """Consume the link for `email`, in one transaction, or refuse.

        A caller who is not the named owner is refused and the link is NOT
        consumed, so holding someone else's token neither opens it nor burns it.
        """
        from google.cloud import firestore  # lazy: nothing built at import time

        ref = self._ref(digest(token or ""))
        caller = (email or "").strip().lower()
        found: dict[str, Any] = {}

        @firestore.transactional
        def _consume(transaction: Any) -> None:
            snapshot = ref.get(transaction=transaction)
            record = dict(snapshot.to_dict() or {}) if snapshot.exists else None
            record = self._check(record, caller)
            record["used_at"] = self._now()
            transaction.set(ref, record)
            found.clear()
            found.update(record)

        _consume(self._db.transaction())
        return found

__all__ = [
    "ADDON_SECRET",
    "ADDON_SECRETS",
    "COLLECTION",
    "KINDS",
    "LINK_TTL",
    "LinkInvalid",
    "LinkNotYours",
    "POOL_ACCOUNT",
    "SetupLinks",
    "TEAM_GIT",
    "digest",
    "team_git_secret",
]
