"""One-time links minted by Admin › Setup (docs/design/day-one-plan.md §5.2,
§5.3; review finding 21, review-fix finding 1; lane DAYONE-SETUP-API-1).

ONE ROUTE, FIVE KINDS. `POST /v1/admin/setup/links` mints:

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
* `voice-api-key`: the form for Voice it's OpenAI developer API key (the
  owner, 4 November 2026; docs/design/voice-it.md). Minted from Admin ›
  Accounts by a platform admin FOR THAT ADMIN ALONE, opened in their own
  console session within `VOICE_KEY_TTL` (ten minutes), and its value is
  forwarded once to the quota broker's `POST /v1/accounts` with provider
  `openai-api`, which stores it in its one broker-only secret. swarm-api
  keeps no copy: not in the link's record, a log line or an answer.
* `api-key`: the form for a developer API key in the account pool
  (docs/design/llm-providers.md §4.1). Minted by a platform admin naming the
  vendor (`API_KEY_PROVIDERS`), the label and the workspace that will own the
  key, for one person (often the one who holds the vendor account), within
  `API_KEY_TTL` (ten minutes, as `VOICE_KEY_TTL`). Its value is forwarded once
  to the broker's `POST /v1/accounts` as a pool key, under the workspace the
  link recorded and with no lend. A key the broker refuses REOPENS the link
  (`reopen`), so the person may type it again; swarm-api keeps no copy.

A LINK IS A HASH. The token is returned once, in the mint's answer; Firestore
keeps only its SHA-256 (`setup_links/<digest>`), so a reader of the database
cannot open a link. Each link is for one person (their verified email, checked
on redemption), single use (consumed in a transaction) and expires 24 hours
after it is minted (`voice-api-key`, `api-key`: ten minutes, `LINK_TTLS`).
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
VOICE_API_KEY = "voice-api-key"
#: The `api-key` kind: a developer API key for the account pool.
API_ACCOUNT = "api-key"
KINDS: tuple[str, ...] = (POOL_ACCOUNT, TEAM_GIT, ADDON_SECRET, VOICE_API_KEY, API_ACCOUNT)

#: The broker's two API key providers an `api-key` link may name: Anthropic's
#: and OpenAI's developer keys (`quota_broker.api_key.PROVIDERS`).
API_KEY_PROVIDERS: tuple[str, ...] = ("anthropic-api", "openai-api")

#: A Voice it API key link lasts ten minutes, not a day: it is for the admin
#: who minted it, opened in the session that minted it, so a link still unused
#: after ten minutes is one that leaked or was forgotten.
VOICE_KEY_TTL = timedelta(minutes=10)
#: An `api-key` link lasts as long, as the design asks (llm-providers.md
#: §4.1): a key link still unused after ten minutes leaked or was forgotten.
API_KEY_TTL = VOICE_KEY_TTL
LINK_TTLS: dict[str, timedelta] = {**dict.fromkeys(KINDS, LINK_TTL), VOICE_API_KEY: VOICE_KEY_TTL,
                                   API_ACCOUNT: API_KEY_TTL}

#: The console page each kind opens; `{token}` is the one-time token. One
#: page serves every kind (review finding 21).
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
             tenant_id: str | None = None, team_id: str | None = None,
             extra: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        """A fresh token and the record kept under its digest. `extra`: the
        kind's own fields (an `api-key` link's provider and Voice it share),
        never a value the link's form carries."""
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
            "expires_at": now + LINK_TTLS[kind],
            "used_at": None,
            **(extra or {}),
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

    def reopen(self, token: str, *, used_at: datetime) -> bool:
        """Undo this request's own `redeem`, in one transaction, and say if it did.

        Only a link whose `used_at` is still the one `redeem` wrote is reopened,
        so a link some other request consumed stays consumed. The expiry is
        unchanged: a reopened link still dies when it was going to."""
        from google.cloud import firestore  # lazy: nothing built at import time

        ref = self._ref(digest(token or ""))
        reopened: list[bool] = []

        @firestore.transactional
        def _reopen(transaction: Any) -> None:
            reopened.clear()
            snapshot = ref.get(transaction=transaction)
            record = dict(snapshot.to_dict() or {}) if snapshot.exists else None
            if record is None or record.get("used_at") != used_at:
                return
            record["used_at"] = None
            transaction.set(ref, record)
            reopened.append(True)

        _reopen(self._db.transaction())
        return bool(reopened)

__all__ = [
    "ADDON_SECRET",
    "ADDON_SECRETS",
    "API_ACCOUNT",
    "API_KEY_PROVIDERS",
    "API_KEY_TTL",
    "COLLECTION",
    "KINDS",
    "LINK_TTL",
    "LINK_TTLS",
    "LinkInvalid",
    "LinkNotYours",
    "POOL_ACCOUNT",
    "SetupLinks",
    "TEAM_GIT",
    "VOICE_API_KEY",
    "VOICE_KEY_TTL",
    "digest",
    "team_git_secret",
]
