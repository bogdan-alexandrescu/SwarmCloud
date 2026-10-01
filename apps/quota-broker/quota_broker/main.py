"""Quota broker HTTP surface.

Callers are workers, not people. A worker reports what the provider did -- a
success, a 429, an exhausted window -- and the broker folds that into the AIMD
state and the pool caps. There is no human-facing surface here; the read models
a human wants are on the API's /v1/providers.

Because callers are workers, identity is a service account, and the tenant is
derived FROM that service account rather than taken from the request. A worker
running as tenant A's service account cannot report a 429 against tenant B and
throttle them, which would otherwise be a one-line denial of service.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Body, FastAPI, Header, Query, Request, Response
from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from pydantic import BaseModel, ConfigDict, Field

from swarm_common.logging_setup import configure_logging
from swarm_common.models import ProviderState, QuotaState
from swarm_common.profiles import RUNNER_PROFILES

from .aimd import quota_derived_limit_for
from .settings import _int
from .usage import DEFAULT_MAX_POLLS_PER_SWEEP
from .oauth import (
    OAUTH_REDIRECT_URI,
    Credential,
    CredentialError,
    build_authorize_url,
    challenge_for,
    new_state,
    new_verifier,
    parse_credential,
    split_pasted_code,
)
from .usagepoll import UsagePoller
from .accounts import (
    DEFAULT_HOLD_TTL,
    DEFAULT_STALE_AFTER,
    HOLD_END_EXPIRED,
    HOLD_END_RELEASED,
    HOLD_END_UNUSABLE,
    HOLD_LOG_COLLECTION,
    HOLD_LOG_RETENTION,
    AccountState,
    Hold,
    Unavailable,
    accounts_serving,
    choose,
    due_for_refresh,
    eligibility,
    holds_from_firestore,
    holds_to_firestore,
    hold_log_entry,
    secret_name,
    validate_label,
)
from .accountstore import COLLECTION as ACCOUNTS_COLLECTION
from .accountstore import _MALFORMED, AccountStore, MalformedAccountError
from .credentials import REFRESH_SUFFIX, CredentialRefresher
from .oauth import HttpTokenEndpoint
from .publishledger import FirestorePublishLedger, InMemoryPublishLedger
from .publishledger import fingerprint as publish_fingerprint
from .secretstore import SecretManagerStore
from .service import QuotaBroker, quota_to_firestore
from .settings import BrokerSettings
from .sweeplease import LeaseFence, build_sweep_lease, new_holder, txn_snapshot

log = logging.getLogger(__name__)

#: The prefix of a tenant's worker service account. ONE family is accepted
#: because only one exists: terraform/modules/tenancy and
#: scripts/register-tenant.sh both create `swarm-agent-worker-<tenant>`, the
#: name swarm_common.identity sizes tenant ids for. register-tenant.sh used to
#: create `swarm-t-<tenant>`; nothing does now, so an account by that name is
#: one somebody made by hand, and accepting it would let whoever holds
#: `iam.serviceAccounts.create` here act as any tenant (#176). This is an
#: authentication allow-list: an entry for an identity nothing provisions is a
#: way in, not a compatibility allowance.
WORKER_SA_PREFIXES = ("swarm-agent-worker",)


def worker_sa_pattern(project_id: str) -> re.Pattern[str]:
    """`swarm-agent-worker-<tenant>@<THIS project>...` -> `<tenant>`.

    The project id is PINNED. With `[^@]+` in its place, a service account named
    `swarm-agent-worker-eng` in an ATTACKER'S own Google Cloud project produces a
    genuine, Google-signed OIDC token that satisfies this pattern, and the broker
    would authorize it as tenant `eng` -- able to report 429s and exhaustion
    against them, and to read their quota state. Cloud Run's internal ingress and
    invoker IAM would be the only thing in the way, which makes this app-level
    check load-bearing exactly where it was weakest.
    """
    if not project_id:
        raise ValueError(
            "PROJECT_ID is required: without it the worker service account "
            "pattern cannot be pinned to this project and any project's "
            "similarly-named service account would authenticate as a tenant"
        )
    alternatives = "|".join(re.escape(p) for p in WORKER_SA_PREFIXES)
    return re.compile(
        rf"^(?:{alternatives})-(?P<tenant>[a-z0-9-]+)@"
        rf"{re.escape(project_id)}\.iam\.gserviceaccount\.com$"
    )


class BrokerAuthError(Exception):
    """The caller is not who they need to be. Answered with 403."""


class BrokerValidationError(Exception):
    """The caller is allowed, but the request names something that does not
    exist. Answered with 422: reporting it as 403 would send an operator
    hunting for a missing IAM grant that was never the problem."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SuccessReport(StrictModel):
    requests_remaining: int | None = Field(default=None, ge=0)
    tokens_remaining: int | None = Field(default=None, ge=0)
    reset_at: datetime | None = None


class AccountRegister(StrictModel):
    """Register an account, credential included.

    THE CREDENTIAL IS TAKEN AS PASTED, not as three parsed fields, and
    `oauth.parse_credential` is what reads it. That function already accepts
    Claude Code's keychain item verbatim -- `claudeAiOauth` wrapper and all --
    "because pasting that item verbatim is the obvious thing for an operator to
    do". Asking a UI to take it apart first would mean a second implementation
    of that shape, and the two would disagree the first time the shape changed.

    It must be a PAIR. `claude setup-token` mints one long-lived token with no
    refresh token beside it; when it expires a human logs in again. The pair
    Claude Code itself keeps can be exchanged for a new pair indefinitely,
    which is what makes "the only manual step is the first one" true rather
    than aspirational. Registration parses it here so a credential that cannot
    be refreshed is refused at the moment of pasting -- not discovered at the
    next sweep, long afterwards, with nothing pointing at the cause.
    """

    owner_tenant: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=64)
    provider: str = Field(default="anthropic", max_length=64)
    lend_to: list[str] = Field(default_factory=list, max_length=50)
    #: Write-only. No response model in this service contains it and no route
    #: returns it -- the same rule tenants.py states for provider keys.
    credential: str = Field(min_length=8, max_length=16384)


class AccountAuthorize(StrictModel):
    """Begin adding an account from a browser."""

    owner_tenant: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=64)
    provider: str = Field(default="anthropic", max_length=64)
    lend_to: list[str] = Field(default_factory=list, max_length=50)


class AccountExchange(StrictModel):
    """Finish adding it, with the code the callback page displayed."""

    state: str = Field(min_length=8, max_length=256)
    #: Accepted as pasted. The callback renders `<code>#<state>` and people
    #: paste what is on screen, so the whole thing is taken and split here.
    code: str = Field(min_length=4, max_length=2048)


class AccountLending(StrictModel):
    lend_to: list[str] = Field(default_factory=list, max_length=50)


class AccountStateChange(StrictModel):
    state: str = Field(min_length=1, max_length=32)
    reason: str = Field(default="", max_length=512)


class AccountAssign(StrictModel):
    """Ask for an account to run on. Carries a PROVIDER and nothing else.

    THERE IS NO TENANT FIELD AND THERE MUST NOT BE ONE. The tenant is derived
    from the caller's service account, exactly as every other route here
    derives it, because `Account.may_serve` is what enforces invariant 9 for
    the pool: an account is reachable by its owner and by the tenants its
    owner named in `lend_to`, and by nobody else. A tenant taken from the body
    would make that check a formality -- any worker could name any tenant and
    be handed that tenant's subscription credential, which is the one thing
    per-tenant isolation exists to prevent.
    """

    provider: str = Field(default="anthropic", max_length=64)

    #: Accounts this caller has ALREADY been handed during this attempt and
    #: could not use. Safe to take from the body in a way a tenant is not: it
    #: can only ever narrow what this caller is offered, never widen it, and
    #: `may_serve` still decides the set it narrows. Without it `choose()` is
    #: deterministic and would return the same unreadable account on every ask
    #: until the task ran out of attempts.
    exclude: list[str] = Field(default_factory=list, max_length=20)

    #: Which task and attempt this worker is running, from its OWN config, so
    #: the account can say who is on it (#379). OPTIONAL: an older worker image
    #: sends neither and must still be assigned. Safe to take from the body for
    #: the same reason `exclude` is -- it can change nothing about which
    #: account is chosen or who may use it. It is a CLAIM, recorded on the
    #: hold as sent; swarm-api checks that the hold's tenant owns the task
    #: before showing it to anyone, and shows "unverified" when it does not.
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    attempt_id: str | None = Field(default=None, min_length=1, max_length=128)


class AccountRelease(StrictModel):
    """Give back one named assignment.

    `assignment_id` is REQUIRED. Releasing used to need nothing but
    `may_serve`, so any tenant an account was lent to could decrement the
    owner's counter as often as it liked -- and `choose()` sorts on that
    number, so an account with five live agents could sort as idle and take a
    sixth. The id is proof that this caller held what it is giving back.
    """

    assignment_id: str = Field(min_length=1, max_length=128)
    #: Set when the account was assigned and its secret could not be read --
    #: no version yet, or no `secretAccessor` grant for this tenant. Recorded
    #: against (account, this tenant) so the next agent is not sent at the same
    #: wall, and so an operator can see the difference between an account
    #: nobody wants and one nobody can read.
    unusable: str = Field(default="", max_length=200)


class AttemptHoldsRelease(StrictModel):
    """Give back every hold stamped with one fenced attempt (#380).

    BOTH ids are required and neither may be empty. A hold taken before holds
    were stamped carries neither, and an empty `attempt_id` matching "no
    attempt" would release every one of those at once.
    """

    task_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)


class RateLimitReport(StrictModel):
    retry_after_seconds: int | None = Field(default=None, ge=0, le=86_400)
    reset_at: datetime | None = None


class HardMaxRequest(StrictModel):
    hard_max: int = Field(ge=0, le=100_000)


class BrokerMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.reports = Counter(
            "swarm_quota_reports_total",
            "Provider outcome reports, by provider and kind.",
            ["provider", "kind"],
            registry=self.registry,
        )
        self.decreases = Counter(
            "swarm_quota_decreases_total",
            "Multiplicative decreases applied, by provider.",
            ["provider"],
            registry=self.registry,
        )
        self.target = Gauge(
            "swarm_quota_adaptive_target",
            "Current AIMD target per provider and tenant.",
            ["provider", "tenant"],
            registry=self.registry,
        )
        self.derived_limit = Gauge(
            "swarm_quota_derived_limit",
            "Quota-derived pool cap per provider and tenant.",
            ["provider", "tenant"],
            registry=self.registry,
        )
        self.auth_failures = Counter(
            "swarm_quota_auth_failures_total",
            "Rejected broker calls, by kind.",
            ["kind"],
            registry=self.registry,
        )

    def observe(self, state: QuotaState) -> None:
        if state.adaptive_target is not None:
            self.target.labels(provider=state.provider, tenant=state.tenant_id).set(
                state.adaptive_target
            )
        self.derived_limit.labels(provider=state.provider, tenant=state.tenant_id).set(
            quota_derived_limit_for(state)
        )

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


class WorkerIdentity:
    """Verifies the caller's Google ID token and derives its tenant.

    `required=False` makes every anonymous caller a PLATFORM caller, which is
    how `REQUIRE_OIDC=false` turned a dev convenience into "anyone may set any
    tenant's hard max and run the sweep". It is accepted only in a local
    environment now, and `hardened` refuses the combination outright rather than
    letting a deployment inherit it from a copied .env.
    """

    def __init__(self, *, audience: str = "", platform_accounts: tuple[str, ...] = (),
                 required: bool = True, project_id: str = "",
                 hardened: bool = False) -> None:
        if hardened:
            if not required:
                raise ValueError(
                    "REQUIRE_OIDC=false is refused outside local development: with "
                    "it off every unauthenticated caller is treated as a platform "
                    "administrator, able to set any tenant's hard max and run the "
                    "quota sweep"
                )
            if not audience:
                raise ValueError(
                    "BROKER_AUDIENCE is required outside local development: "
                    "google-auth skips the 'aud' check entirely when no audience is "
                    "given, so a token minted for any other service would be accepted"
                )
        self._audience = audience or None
        self._platform = {a.lower() for a in platform_accounts if a}
        self._required = required
        self._pattern = worker_sa_pattern(project_id) if required else None
        self._request = None

    @property
    def required(self) -> bool:
        return self._required

    def resolve(self, authorization: str | None) -> tuple[str | None, bool]:
        """Return (tenant_id or None, is_platform_caller)."""
        if not self._required:
            return None, True
        if not authorization or not authorization.lower().startswith("bearer "):
            raise BrokerAuthError("missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token

        if self._request is None:
            self._request = google_requests.Request()
        try:
            claims = google_id_token.verify_oauth2_token(
                token, self._request, audience=self._audience
            )
        except ValueError:
            # Never echo the token or the library's message.
            raise BrokerAuthError("token verification failed") from None
        email = str(claims.get("email", "")).lower()
        if email in self._platform:
            return None, True
        match = self._pattern.match(email) if self._pattern else None
        if not match:
            raise BrokerAuthError("caller is not a swarm worker service account")
        return match.group("tenant"), False


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _known_provider(provider: str) -> str:
    providers = {p.provider for p in RUNNER_PROFILES.values() if p.provider}
    name = provider.strip().lower()
    if name not in providers:
        raise BrokerValidationError(
            f"unknown provider {provider!r}; known providers are "
            + ", ".join(sorted(providers))
        )
    return name


def build_broker(
    *,
    settings: BrokerSettings | None = None,
    db: Any | None = None,
) -> QuotaBroker:
    settings = settings or BrokerSettings.from_env()
    if db is None:
        from google.cloud import firestore

        db = firestore.Client(
            project=settings.project_id, database=settings.core.firestore_database
        )
    return QuotaBroker(db, settings=settings)


def _aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def credential_from_token_response(payload: dict[str, Any]) -> Credential:
    """Shape a token-endpoint response into a Credential.

    `expires_in` is SECONDS FROM NOW, not an absolute time. Reading it as an
    epoch would date the credential to 1970 and make every account look
    expired the moment it was added.
    """
    access = str(payload.get("access_token") or "")
    refresh = str(payload.get("refresh_token") or "")
    if not access or not refresh:
        raise CredentialError(
            "the token endpoint returned no refresh token, so this credential "
            "could not be kept alive. Nothing was saved."
        )
    expires_in = int(payload.get("expires_in") or 3600)
    return Credential(
        access_token=access,
        refresh_token=refresh,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
    )


def account_to_api(account: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """One account, shaped for a human to read.

    THE COLUMNS ARE `cs status`'s, deliberately. An operator who already reads
    that output on their laptop should not have to learn a second vocabulary
    for the same five facts about the same five accounts. So: the label, each
    window's utilisation, when the binding one clears, and the state.

    NO CREDENTIAL MATERIAL, and not even its length. `Credential.redacted()`
    exposes `access_token_len`, which is fine in a debug log and is not fine on
    a page -- a length is a real hint about a secret and nobody reading this
    screen needs it.

    `stale` is computed rather than stored, because a reading's age is what
    decides whether to trust it: `cs status` marks a projected figure with `~`
    for exactly this reason, and a number shown without that mark is a claim
    that it is current.
    """
    now = now or datetime.now(timezone.utc)
    windows: dict[str, Any] = {}
    for name, reading in (getattr(account, "windows", {}) or {}).items():
        windows[name] = {
            "utilization": round(float(reading.utilization), 4),
            "resets_at": reading.resets_at.isoformat(),
            "reset": reading.is_reset(now),
        }
    observed_at = getattr(account, "observed_at", None)
    stale = True
    if observed_at is not None:
        stale = (now - observed_at) > DEFAULT_STALE_AFTER

    # THE VERDICT AND THE RECORD ARE TWO FIELDS, and the verdict is the one a
    # reader can act on. `is_unreadable_for` is asked rather than re-derived
    # from a timestamp this function would have to publish, so the set below is
    # by construction the set `choose()` will skip -- the two cannot drift
    # apart the way a copy of the rule would.
    #
    # An object without the predicate reports every recorded tenant as
    # currently unreadable. That over-reports a problem rather than hiding one,
    # which is the only direction this field may fail in.
    unreadable_by = sorted(getattr(account, "unreadable_by", {}) or {})
    predicate = getattr(account, "is_unreadable_for", None)
    unreadable_now = (
        [t for t in unreadable_by if predicate(t, now)]
        if callable(predicate)
        else list(unreadable_by)
    )
    return {
        "account_id": account.account_id,
        "owner_tenant": account.owner_tenant,
        "label": account.label,
        "provider": account.provider,
        "state": account.state.value,
        "reason": account.reason,
        "lend_to": list(account.lend_to),
        "assigned": account.assigned,
        "windows": windows,
        "observed_at": observed_at.isoformat() if observed_at else None,
        #: True when no reading has arrived recently enough to be trusted.
        #: Distinct from "utilisation is zero", which is a measurement.
        "stale": stale,
        #: Tenants that were handed this account and reported they could not
        #: read its secret. An account nobody can read is not the same thing as
        #: an account nobody wants, and until this was surfaced the two looked
        #: identical on the listing: full headroom, zero agents, and every task
        #: for that tenant quietly failing.
        "unreadable_by": unreadable_by,
        #: The tenants `choose()` will REFUSE to hand this account to RIGHT NOW.
        #:
        #: Computed here rather than left to the reader, for the same reason
        #: `stale` and each window's `reset` are: the rule is time-limited --
        #: `Account.is_unreadable_for` forgets a report after
        #: DEFAULT_STALE_AFTER, deliberately, so a five-minute onboarding
        #: window does not become an account that never comes back -- and the
        #: server owns the clock. `unreadable_by` above carries no ages at all,
        #: so a reader given only that cannot tell a report five minutes old
        #: from one three days stale, and both render the same.
        #:
        #: Without this the listing was back where `unreadable_by` was added to
        #: stop it being: an account the pool will not hand out drawn as a
        #: healthy one with full headroom and zero agents, and every task for
        #: that tenant quietly failing.
        "unreadable_now": unreadable_now,
        #: Null means NEVER assigned, which is the shape of "this account is
        #: registered and no worker can reach the broker at all".
        "last_assigned_at": (
            last_assigned.isoformat()
            if isinstance(last_assigned := getattr(account, "last_assigned_at", None),
                          datetime)
            else None
        ),
    }


#: One DocumentSnapshot out of a Firestore transactional get -- see
#: `sweeplease.txn_snapshot` for why a get can be a generator. Imported under
#: the name this module has always used, so the broker has ONE copy of the
#: adapter and not one per module that runs a transaction.
_txn_snapshot = txn_snapshot


def _live_holds(data: dict[str, Any], now: datetime) -> list[Hold]:
    return [h for h in holds_from_firestore(data.get("holds")) if not h.is_expired(now)]


def _split_holds(data: dict[str, Any], now: datetime) -> tuple[list[Hold], list[Hold]]:
    """(live, expired). Every path that drops the expired ones must log them."""
    holds = holds_from_firestore(data.get("holds"))
    return (
        [h for h in holds if not h.is_expired(now)],
        [h for h in holds if h.is_expired(now)],
    )


def _log_ref(db: Any, assignment_id: str) -> Any:
    return db.collection(HOLD_LOG_COLLECTION).document(assignment_id)


def _close_expired(txn: Any, db: Any, account_id: str, expired: list[Hold]) -> None:
    """Close the records of holds that lapsed, IN the caller's transaction.

    `released_at` is the hold's own deadline -- when it stopped counting --
    rather than the moment this transaction happened to notice, so a span in
    the history ends where the counter stopped including it.
    """
    for hold in expired:
        txn.set(
            _log_ref(db, hold.assignment_id),
            hold_log_entry(hold, account_id, end=HOLD_END_EXPIRED, released_at=hold.expires_at),
        )


def _hold_payload(holds: list[Hold]) -> dict[str, Any]:
    """`assigned` is written from `holds` and never independently of it.

    One statement, one place, so the projection cannot drift from the thing it
    projects. `assigned` is what `choose()` sorts on and what an operator reads
    as "agents on this account"; a second writer for it is how it stopped
    meaning that.
    """
    return {"holds": holds_to_firestore(holds), "assigned": len(holds)}


def _unreadable_reports(data: dict[str, Any]) -> dict[str, Any]:
    """`unreadable_by` as a map, or empty when the document holds anything else.

    The hold paths read the RAW document and never decode an `Account` (#243),
    so they meet whatever shape is stored. This field is advisory -- it only
    steers `choose()` away for a while -- and a string or a list where the map
    should be must not stop a hold being given back or pruned.
    """
    raw = data.get("unreadable_by")
    return dict(raw) if isinstance(raw, dict) else {}


def _raw_account(db: Any, account_id: str) -> dict[str, Any] | None:
    """One account document's raw fields, or None when it does not exist.

    NOT `AccountStore.get`, deliberately. That decodes the whole `Account` and
    raises `MalformedAccountError` on a document the decoder cannot read,
    which is right for a route that renders or edits the account -- and wrong
    for giving a hold back, which needs only `holds`, the owner and the
    lending list (#243). Decoding first is what froze a bad document's holds:
    the worker's release answered 500 and the hold could only age out, and the
    sweep that ages them out could not see the document either.
    """
    snap = db.collection(ACCOUNTS_COLLECTION).document(account_id).get()
    if not getattr(snap, "exists", False):
        return None
    return snap.to_dict() or {}


def _raw_may_serve(data: dict[str, Any], tenant_id: str) -> bool:
    """`Account.may_serve`, over the raw fields of a document.

    STRICTER on shape than the decoder, never looser: an owner that is not a
    string, or a `lend_to` that is not a list, serves nobody but the platform.
    A string `lend_to` is the case that matters -- `tenant in "research"` is a
    substring test, and a malformed document must not lend itself to every
    tenant whose id happens to be inside another's.
    """
    owner = data.get("owner_tenant")
    if isinstance(owner, str) and owner and tenant_id == owner:
        return True
    lend_to = data.get("lend_to")
    return isinstance(lend_to, (list, tuple)) and tenant_id in [
        t for t in lend_to if isinstance(t, str)
    ]


def acquire_hold(
    db: Any,
    account_id: str,
    *,
    tenant_id: str,
    now: datetime,
    ttl: timedelta = DEFAULT_HOLD_TTL,
    task_id: str | None = None,
    attempt_id: str | None = None,
) -> tuple[str, int] | None:
    """Record one agent's claim on an account. Returns (assignment_id, assigned).

    A TRANSACTION AND NOT A READ-MODIFY-WRITE, for the reason `accountstore`
    states: several processes move this at once, and a lost write makes an
    account look permanently busier or emptier than it is -- which quietly
    pushes every new agent onto the other accounts, or stacks them all onto
    this one.

    EXPIRED HOLDS ARE DROPPED ON THE WAY THROUGH, which is what makes the count
    self-correcting rather than monotonically wrong. A worker that is SIGKILLed
    or preempted never releases and nothing else in this repository knows its
    assignment existed, so without an expiry the count would only ever climb.

    None when the account no longer exists -- an operator may remove one
    between the listing and this call, and nothing was counted.

    THE RECORD IS OPENED IN THIS TRANSACTION (#379): `account_holds/{id}` is
    set alongside the account, and every expired hold dropped on the way
    through is closed as `expired` alongside it too. A record written after
    this commits is one a crash between the two leaves disagreeing with the
    counter.
    """
    from google.cloud import firestore

    ref = db.collection(ACCOUNTS_COLLECTION).document(account_id)
    transaction = db.transaction()
    assignment_id = uuid.uuid4().hex

    @firestore.transactional
    def _apply(txn: Any) -> int | None:
        snap = _txn_snapshot(txn.get(ref))
        if not getattr(snap, "exists", False):
            return None
        holds, expired = _split_holds(snap.to_dict() or {}, now)
        hold = Hold(
            assignment_id=assignment_id,
            tenant_id=tenant_id,
            expires_at=now + ttl,
            task_id=task_id or None,
            attempt_id=attempt_id or None,
            assigned_at=now,
        )
        holds.append(hold)
        txn.update(ref, {**_hold_payload(holds), "last_assigned_at": now})
        txn.set(_log_ref(db, assignment_id), hold_log_entry(hold, account_id))
        _close_expired(txn, db, account_id, expired)
        return len(holds)

    assigned = _apply(transaction)
    return None if assigned is None else (assignment_id, assigned)


def release_hold(
    db: Any,
    account_id: str,
    *,
    assignment_id: str,
    tenant_id: str | None,
    now: datetime,
    unusable: str = "",
) -> tuple[int | None, bool]:
    """Give one named assignment back. Returns (assigned, whether it was held).

    NAMED, and that is the fix. This used to decrement a bare counter on proof
    of nothing but `may_serve`, so any tenant an account was lent to could
    drive the owner's count to zero as often as it liked -- and `choose()`
    sorts on that number, so an account with five live agents could look idle
    and take a sixth. A release now removes the hold it was issued, and a
    caller that never held one changes nothing.

    IDEMPOTENT, because a release genuinely arrives twice: a worker releases on
    its exit path, and the same assignment can be released again after the
    response was lost. The second call finds no matching hold and says so
    rather than taking someone else's slot away.

    `unusable` records, against this TENANT only, that the account was handed
    over and its secret could not be read. Per tenant because that is the only
    honest scope: a borrower that was never granted `secretAccessor` on a lent
    secret has learned nothing about the owner, and a global mark would let one
    tenant pause another's account.

    The released hold's record is closed IN THIS TRANSACTION -- `released`, or
    `unusable` when the secret could not be read -- and so is the record of
    every expired hold the rewrite drops. A release that matched nothing writes
    no record at all, so a duplicate cannot re-close a span with a later end.
    """
    from google.cloud import firestore

    ref = db.collection(ACCOUNTS_COLLECTION).document(account_id)
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> tuple[int | None, bool]:
        snap = _txn_snapshot(txn.get(ref))
        if not getattr(snap, "exists", False):
            return None, False
        data = snap.to_dict() or {}
        holds, expired = _split_holds(data, now)
        held = [
            h
            for h in holds
            if h.assignment_id == assignment_id
            # A platform caller has no tenant, so it matches on the id alone;
            # a worker must match both, or a forged id would let one tenant
            # release another's hold.
            and (tenant_id is None or h.tenant_id == tenant_id)
        ]
        remaining = [h for h in holds if h not in held]
        payload = _hold_payload(remaining)
        if unusable and held:
            reports = _unreadable_reports(data)
            reports[held[0].tenant_id or (tenant_id or "")] = now
            payload["unreadable_by"] = reports
        txn.update(ref, payload)
        end = HOLD_END_UNUSABLE if unusable else HOLD_END_RELEASED
        for hold in held:
            txn.set(
                _log_ref(db, hold.assignment_id),
                hold_log_entry(hold, account_id, end=end, released_at=now),
            )
        _close_expired(txn, db, account_id, expired)
        return len(remaining), bool(held)

    return _apply(transaction)


def prune_holds(
    db: Any,
    account_id: str,
    *,
    now: datetime,
    forget_unreadable_after: timedelta = DEFAULT_STALE_AFTER,
) -> int:
    """Drop expired holds and aged-out unreadable reports. Returns how many went.

    THIS IS THE BACKSTOP, and it is in the broker rather than in the
    reconciler: `apps/reconciler/` has no account code at all, and comments
    here used to claim it did. A claimed safety net that does not exist is
    worse than none, because the next person reads it as a reason not to build
    one. The quota sweep calls this for every account on every tick, so a
    worker killed without warning costs one over-counted hold until the hold's
    own deadline passes and this call notices.

    Each pruned hold's record is closed as `expired` IN THIS TRANSACTION, so
    the history and the counter change together or not at all.
    """
    from google.cloud import firestore

    ref = db.collection(ACCOUNTS_COLLECTION).document(account_id)
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> int:
        snap = _txn_snapshot(txn.get(ref))
        if not getattr(snap, "exists", False):
            return 0
        data = snap.to_dict() or {}
        holds = holds_from_firestore(data.get("holds"))
        live = [h for h in holds if not h.is_expired(now)]
        recorded = _unreadable_reports(data)
        reports = {
            tenant: when
            for tenant, when in recorded.items()
            if isinstance(when, datetime)
            and now - (when if when.tzinfo else when.replace(tzinfo=timezone.utc))
            <= forget_unreadable_after
        }
        dropped = len(holds) - len(live)
        stale_reports = len(recorded) - len(reports)
        # A non-map `unreadable_by` is rewritten as the empty map it reads as,
        # the same self-correction `assigned` gets below.
        reshaped = data.get("unreadable_by") is not None and not isinstance(
            data.get("unreadable_by"), dict
        )
        if (
            not dropped
            and not stale_reports
            and not reshaped
            and data.get("assigned") == len(live)
        ):
            return 0
        payload = _hold_payload(live)
        payload["unreadable_by"] = reports
        txn.update(ref, payload)
        _close_expired(txn, db, account_id, [h for h in holds if h.is_expired(now)])
        return dropped

    return _apply(transaction)


def release_attempt_holds(
    db: Any,
    account_id: str,
    *,
    task_id: str,
    attempt_id: str,
    now: datetime,
) -> int:
    """Drop every live hold on one account stamped with (task_id, attempt_id).

    Returns how many were released. For the reconciler, once it has FENCED the
    attempt (#380): a fenced worker never runs its own exit path's release, so
    until now its hold stayed counted for the whole `DEFAULT_HOLD_TTL` and
    `choose()` handed out less of the account than it had.

    MATCHED ON BOTH STAMPS. The attempt id alone would do in practice; the
    task id as well means a hold can only be released by a caller that names
    the work it belongs to. A hold with no stamps (taken before #412/#413) is
    never matched and still ages out through `prune_holds`.

    IDEMPOTENT: a second call finds nothing stamped with the attempt and
    writes nothing at all, so no record is closed twice with a later end.

    Each released hold's record is closed as `released` in this transaction,
    and the expired holds the rewrite drops are closed as `expired`, the same
    as every other change to `holds`. `released`, not a new end: the swarm-api
    history reader and the Accounts screen accept exactly the three ends in
    `accounts.py`, and a hold given back on the worker's behalf ended the same
    way as one the worker gave back.
    """
    from google.cloud import firestore

    ref = db.collection(ACCOUNTS_COLLECTION).document(account_id)
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> int:
        snap = _txn_snapshot(txn.get(ref))
        if not getattr(snap, "exists", False):
            return 0
        holds, expired = _split_holds(snap.to_dict() or {}, now)
        held = [
            h for h in holds if h.attempt_id == attempt_id and h.task_id == task_id
        ]
        if not held:
            return 0
        remaining = [h for h in holds if h not in held]
        txn.update(ref, _hold_payload(remaining))
        for hold in held:
            txn.set(
                _log_ref(db, hold.assignment_id),
                hold_log_entry(hold, account_id, end=HOLD_END_RELEASED, released_at=now),
            )
        _close_expired(txn, db, account_id, expired)
        return len(held)

    return _apply(transaction)


def _release_attempt_holds_everywhere(
    db: Any, *, task_id: str, attempt_id: str, now: datetime
) -> dict[str, Any]:
    """`release_attempt_holds` over every account document. One transaction each.

    Every document id straight from the collection, as `_prune_all_holds`
    walks them and for its reason (#243): a malformed account is the one a
    decoded listing would skip, and its holds count all the same.
    """
    released = 0
    accounts: list[str] = []
    for doc in db.collection(ACCOUNTS_COLLECTION).stream():
        account_id = str(doc.id)
        try:
            count = release_attempt_holds(
                db, account_id, task_id=task_id, attempt_id=attempt_id, now=now
            )
        except _MALFORMED as exc:
            log.warning(
                "could not release a fenced attempt's holds on an account document",
                extra={"account_id": account_id, "error": type(exc).__name__},
            )
            continue
        if count:
            released += count
            accounts.append(account_id)
    return {"released": released, "accounts": accounts}


#: The history route's page: the default, and the most one request may ask.
HISTORY_PAGE = 100
HISTORY_PAGE_MAX = 500

#: The window the history route reads when the caller names none.
HISTORY_DEFAULT_SPAN = timedelta(days=7)


def _iso(value: Any) -> str | None:
    return _aware_utc(value).isoformat() if isinstance(value, datetime) else None


def _hold_to_api(hold: Hold) -> dict[str, Any]:
    """One live hold for the platform read. NO assignment id: it authorises release."""
    return {
        "tenant_id": hold.tenant_id,
        "task_id": hold.task_id,
        "attempt_id": hold.attempt_id,
        "assigned_at": _iso(hold.assigned_at),
        "expires_at": _iso(hold.expires_at),
    }


def _span_to_api(record: dict[str, Any]) -> dict[str, Any]:
    """One `account_holds` record, by an allow-list of fields.

    An allow-list rather than the document minus some keys: the document id is
    the assignment id, and a record that one day grows a field must not start
    serving it by default.
    """
    end = record.get("end")
    return {
        "tenant_id": str(record.get("tenant_id") or ""),
        "task_id": record.get("task_id") if isinstance(record.get("task_id"), str) else None,
        "attempt_id": (
            record.get("attempt_id") if isinstance(record.get("attempt_id"), str) else None
        ),
        "assigned_at": _iso(record.get("assigned_at")),
        "released_at": _iso(record.get("released_at")),
        "end": end if end in (HOLD_END_RELEASED, HOLD_END_UNUSABLE, HOLD_END_EXPIRED) else None,
        "hold_expires_at": _iso(record.get("hold_expires_at")),
    }


def _instant(raw: str, name: str, now: datetime) -> datetime:
    """An instant within [now - retention - 1 day, now + 1 day], as UTC.

    The bound is what keeps `0001-01-01T00:00:00+01:00` and
    `9999-12-31T23:59:59-01:00` from reaching arithmetic: `astimezone` raises
    OverflowError (not ValueError) on them, which was an unhandled 500. No
    record outlives HOLD_LOG_RETENTION and none is dated in the future, so an
    instant outside the range can only be a probe or a mistake.
    """
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise BrokerValidationError(f"`{name}` must carry a UTC offset")
        value = value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise BrokerValidationError(
            f"`{name}` must be an ISO 8601 instant with an offset"
        ) from None
    if not now - HOLD_LOG_RETENTION - timedelta(days=1) <= value <= now + timedelta(days=1):
        raise BrokerValidationError(f"`{name}` is outside the range the hold log retains")
    return value


def _history_cursor(raw: str, now: datetime) -> tuple[datetime, int]:
    """`<instant>|<n served at that instant>`, as the previous page minted it.

    `n` is BOUNDED by `HISTORY_PAGE_MAX`. It sizes the Firestore read
    (`limit + n + 1`), so an unbounded one let a forged cursor read an
    account's whole hold log, and past int32 it was an unhandled 500. A page
    never serves more than `HISTORY_PAGE_MAX` rows, and the account holds a
    handful of concurrent holds, so a genuine `n` never nears the bound.

    NOT a (assigned_at, document id) cursor, though that would need no count:
    the document id IS the assignment id, which authorises a release, and a
    cursor is handed to the caller.
    """
    instant, sep, count = raw.rpartition("|")
    if not sep or not (count.isascii() and count.isdigit()) or len(count) > 4:
        raise BrokerValidationError("the cursor is not one this route issued")
    skip = int(count)
    if skip > HISTORY_PAGE_MAX:
        raise BrokerValidationError("the cursor is not one this route issued")
    return _instant(instant, "cursor", now), skip


def quota_to_api(state: QuotaState) -> dict[str, Any]:
    payload = quota_to_firestore(state)
    payload["effective_limit"] = state.effective_limit
    payload["quota_derived_limit_now"] = quota_derived_limit_for(state)
    return payload



def _prune_all_holds(db: Any, store: Any, now: datetime) -> dict[str, Any]:
    """Expire abandoned holds across the whole pool. One transaction per account.

    Per account rather than one batch, because each is an independent
    read-modify-write against a document a live assign or release may be
    touching at the same moment, and a batch would have to lose one of them.
    The pool is tens of accounts, not thousands.

    EVERY DOCUMENT, NOT `store.list()` (#243). The listing leaves a malformed
    document out by design (#180), and walking it meant the one backstop for a
    killed worker's hold never visited that document: its holds stayed counted
    until someone repaired it by hand. `prune_holds` needs only the raw
    `holds`, so the sweep enumerates document ids straight from the
    collection and prunes the readable and the unreadable alike. The readable
    list is still read, for the alarm below, and it is also what names each
    unreadable document by id and exception class (`accountstore._decode`).
    """
    reclaimed = 0
    touched = 0
    accounts = store.list()
    readable = [account.account_id for account in accounts]
    streamed = [str(doc.id) for doc in db.collection(ACCOUNTS_COLLECTION).stream()]
    unreadable = sorted(set(streamed) - set(readable))
    for account_id in [*readable, *unreadable]:
        try:
            dropped = prune_holds(db, account_id, now=now)
        except _MALFORMED as exc:
            # One document whose hold fields themselves cannot be read must not
            # stop the rest of the pool being swept. The id and the CLASS only:
            # `str(exc)` can quote a field value (see `MalformedAccountError`).
            log.warning(
                "could not prune the holds on an account document",
                extra={"account_id": account_id, "error": type(exc).__name__},
            )
            continue
        if dropped:
            reclaimed += dropped
            touched += 1
            log.warning(
                "reclaimed assignments whose worker never released them",
                extra={
                    "account_id": account_id,
                    "reclaimed": dropped,
                    # Says the reclaim happened on a document nothing else can
                    # read, so the operator fixing it knows its count is right.
                    "unreadable": account_id in unreadable,
                },
            )

    # THE ONE ALARM FOR A POOL NOTHING CAN REACH.
    #
    # The failure this names has no other symptom. A worker with no
    # QUOTA_BROKER_URL never calls; a worker whose service account is missing
    # from this service's `run.invoker` list is rejected by Cloud Run before
    # the application runs, and its client is written to fall back rather than
    # fail. Either way nothing errors, nothing is logged here, the listing
    # shows a healthy idle pool, and every agent runs on the one shared
    # per-tenant subscription -- which is the contention the pool exists to
    # remove.
    #
    # It can only be said from HERE, because only the broker knows both halves:
    # that accounts are registered, and that nothing has ever asked for one. A
    # deployment that has simply not adopted the pool has no accounts and gets
    # no warning, which is why this is not a plan-time check on a variable.
    never_used = [a for a in accounts if a.last_assigned_at is None]
    if accounts and len(never_used) == len(accounts):
        log.warning(
            "accounts are registered and none has ever been assigned; the "
            "workers cannot reach this service. Check that the scheduler sets "
            "QUOTA_BROKER_URL on the jobs it dispatches and that each tenant's "
            "worker service account holds roles/run.invoker on swarm-quota-broker",
            extra={"accounts": len(accounts)},
        )

    return {
        # `pruned`, not `accounts`: this block sits beside the refresher's own
        # `accounts` summary in the sweep response and the two count different
        # things.
        "pruned": touched,
        "reclaimed": reclaimed,
        "registered": len(accounts),
        "never_assigned": len(never_used),
    }


#: Refresh outcomes that mean the credential is dead and only a person can fix
#: it.
#:
#: `unreadable` sits beside `reauth_required` because the stored payload cannot
#: be parsed at all: there is nothing to present to the token endpoint, so no
#: number of retries changes the answer, and the account is exactly as unusable
#: as one whose token was revoked. The on-demand refresh route has treated the
#: two identically since it was written; naming them once is what stops the
#: sweep growing a second opinion about what "dead" means.
CREDENTIAL_NEEDS_A_HUMAN = ("reauth_required", "unreadable")

#: Outcomes that added at least one Secret Manager version. `refreshed` writes
#: the pair; the other two write the worker-facing half alone. Named here
#: rather than inline because both sweep summarisers count it and a second
#: spelling would drift from this one.
_WROTE_A_VERSION = ("refreshed", "published", "published_unverified")


def _sweep_account_pool(
    refresher: Any, store: Any, now: datetime, *, keep_going: Any = None
) -> dict[str, Any]:
    """Refresh every registered account, and RECORD what came back.

    THE RECORDING IS THE POINT, and its absence was the audit's first finding
    (docs/audits/2026-09-18/06-quota-broker-accounts.md). The sweep already
    detected a revoked refresh token, logged it and counted it into a response
    body that Cloud Scheduler throws away. Nothing wrote the account document,
    so:

      * `choose()` kept handing the account to new agents, which then failed to
        authenticate for a reason nothing connected to the account;
      * `due_for_refresh()` kept presenting a dead token to the endpoint every
        five minutes, forever -- the exact behaviour `AccountState`'s own
        docstring says must not happen;
      * and every listing, the UI's included, showed AVAILABLE with an empty
        reason. An operator looking at a pool with one dead account saw a pool
        with nothing wrong with it.

    KEYED BY ACCOUNT ID, NOT BY LABEL. `refresh_secret` puts whatever second
    element it is given into `RefreshOutcome.tenant_id`, and this used to hand
    it `a.label` -- which is unique only WITHIN a tenant. Two tenants may both
    have an account labelled `personal`, so a write-back keyed on the label
    would mark whichever one it found first, and the response named an account
    nobody could look up. The account id is `tenant:label` and is the document
    id, so it is both unambiguous and directly usable.

    Never raises: this runs inside the tick that un-parks throttled tenants,
    and one unreadable account document must not stall every tenant's quota
    recovery. A failure to WRITE one account is caught per account for the same
    reason -- the other marks are still worth making.

    `keep_going` is the sweep lease's fence (`sweeplease.LeaseFence`): asked
    before every exchange, here and inside the refresher, and a False ends the
    pass there -- the lease is somebody else's now.
    """
    try:
        due = due_for_refresh(store.list(), now)
        outcomes = refresher.sweep_accounts(
            [(store.secret_for(a), a.account_id) for a in due],
            keep_going=keep_going,
        )
    except Exception as exc:
        log.error(
            "account pool sweep failed",
            extra={"error": type(exc).__name__, "detail": str(exc)[:200]},
        )
        return {"error": type(exc).__name__}

    needs_human = [
        (o.tenant_id, o.reason)
        for o in outcomes
        if o.reason in CREDENTIAL_NEEDS_A_HUMAN
    ]
    marked: list[str] = []
    for account_id, reason in needs_human:
        # The confirmation below is an exchange like any other, so it is
        # fenced like any other.
        if keep_going is not None and not keep_going():
            break
        try:
            account = store.get(account_id)
            if account is None:
                # Removed between the listing and now. `remove` is allowed to
                # race a sweep in flight; nothing is wrong.
                continue

            # CONFIRMED BEFORE IT IS WRITTEN, with a second exchange against
            # the credential AS IT STANDS NOW.
            #
            # REAUTH_REQUIRED is a state only a person can leave -- the sweep
            # cannot lift it, because `due_for_refresh` skips it -- so a false
            # one costs an operator a sign-in they did not need and the pool an
            # account that was never broken. That is worth one extra call.
            #
            # The concrete way a false one happened is finding 3 of
            # docs/audits/2026-09-18/06-quota-broker-accounts.md: nothing
            # serialised this endpoint, Cloud Scheduler retries a tick it
            # thinks timed out without cancelling the first, and the service
            # takes many concurrent requests. Two sweeps then read the
            # same refresh token, one exchanges it, and the OTHER is told
            # `invalid_grant` for a token that was merely rotated away from it
            # -- a perfectly healthy account. The second attempt re-reads the
            # secret, finds the rotated credential the winner just wrote, and
            # answers `still_valid`, so the account is not marked.
            #
            # This is confirmation, not mutual exclusion: it makes the race
            # harmless HERE. The exclusion is now the sweep lease
            # (`sweeplease`), which stops two sweeps exchanging at once; the
            # confirmation stays, because a lease taken over from a sweep that
            # was merely slow still leaves one exchange in flight on each side.
            second = refresher.refresh_secret(
                store.secret_for(account), label=account_id
            )
            if second.reason not in CREDENTIAL_NEEDS_A_HUMAN:
                log.info(
                    "an account's refresh failed and then worked on a second "
                    "attempt; leaving its state alone",
                    extra={
                        "account_id": account_id,
                        "first": reason,
                        "second": second.reason,
                    },
                )
                continue

            recorded = store.mark_reauth_required(
                account_id, f"refresh failed: {second.reason}"
            )
        except Exception as exc:
            log.error(
                "could not record that an account's credential is dead",
                extra={"account_id": account_id, "error": type(exc).__name__},
            )
            continue
        if recorded is None:
            continue
        marked.append(account_id)
        log.warning(
            "an account's credential is dead and the pool has stopped trying; "
            "it needs a person to sign in again",
            extra={"account_id": account_id, "reason": second.reason},
        )
    return {
        "examined": len(outcomes),
        "refreshed": sum(1 for o in outcomes if o.refreshed),
        #: ACCOUNT IDS -- every account this tick's first pass found dead. Ids
        #: rather than labels because a label is unique only within a tenant.
        "reauth_required": [account_id for account_id, _ in needs_human],
        #: The subset that was still dead on a second attempt AND written. The
        #: two lists differ when an account was removed mid-sweep, when the
        #: second attempt succeeded, or when the write failed -- each of which
        #: is logged, because a detection that was never recorded is one the
        #: listing will not show.
        "marked_reauth_required": marked,
        #: The same write-cadence counters `_sweep_block` reports, and for the
        #: same reason: the account secrets carried 1,741 and 1,698 identical
        #: versions while every tick of this sweep reported success.
        "wrote": sum(1 for o in outcomes if o.reason in _WROTE_A_VERSION),
        "unverified": sum(1 for o in outcomes if o.reason == "published_unverified"),
        "unverified_skipped": [
            o.tenant_id for o in outcomes if o.reason == "unverified_skipped"
        ],
    }


def _sweep_block(run: Any, failure_message: str) -> dict[str, Any]:
    """Run one refresh sweep and summarise it, never raising.

    Neither sweep may take the quota sweep down with it: the same tick is what
    un-parks throttled tenants, so one bad credential must not stall every
    tenant's quota recovery. Reported per sweep rather than merged, because
    "credentials are broken" and "the account pool is broken" want different
    people to do different things.
    """
    try:
        outcomes = run()
    except Exception as exc:
        log.error(failure_message, extra={"error": type(exc).__name__, "detail": str(exc)[:200]})
        return {"error": type(exc).__name__}
    return {
        "examined": len(outcomes),
        "refreshed": sum(1 for o in outcomes if o.refreshed),
        "reauth_required": [o.tenant_id for o in outcomes if o.reason == "reauth_required"],
        # THE WRITE CADENCE, stated on every tick. Nothing reported the
        # 1,741-version leak for four days except the secret-version count
        # itself, because the sweep's own summary never said how many versions
        # it had written. A steady state is `wrote: 0` on every tick but the
        # one that actually rotates a credential; a `wrote` that is nonzero
        # tick after tick is the leak, visible from the response and the log
        # line that carries it rather than from a gcloud count days later.
        "wrote": sum(1 for o in outcomes if o.reason in _WROTE_A_VERSION),
        # Published without being able to confirm it was needed. Should be at
        # most one per credential per deployment; a number that keeps growing
        # means the ledger is not sticking.
        "unverified": sum(1 for o in outcomes if o.reason == "published_unverified"),
        # Declined to write because neither the secret nor the ledger could be
        # read. Not a failure of the credential -- a failure of this service's
        # own evidence, and the credential is going stale while it lasts.
        "unverified_skipped": [
            o.tenant_id for o in outcomes if o.reason == "unverified_skipped"
        ],
    }


def _poll_block(run: Any, failure_message: str) -> dict[str, Any]:
    """Summarise one usage-poll round, never raising.

    ITS OWN SUMMARISER, and it has to be. This used to hand
    `[o.__dict__ for o in poller.poll_round(...)]` to `_sweep_block`, which
    reads `o.refreshed` and `o.reason` off each element -- attributes a
    PollOutcome does not have and a plain dict certainly does not. The
    AttributeError is raised while building the RETURN value, outside that
    function's try block, so it escaped and 500'd the whole sweep as soon as
    the poll returned anything at all.

    That is not a cosmetic bug in a summary line: /v1/quota/sweep is the tick
    that un-parks throttled tenants, refreshes every account's credential and
    -- now -- reclaims the holds of workers that were killed before they could
    release. All of it stopped the moment one account was polled.
    """
    try:
        outcomes = run()
    except Exception as exc:
        log.error(failure_message, extra={"error": type(exc).__name__, "detail": str(exc)[:200]})
        return {"error": type(exc).__name__}
    return {
        "examined": len(outcomes),
        "polled": sum(1 for o in outcomes if o.polled),
        # The account id and the reason, never the secret name or the token --
        # see UsagePoller, which is careful about the same thing.
        "skipped": [
            {"account_id": o.account_id, "reason": o.reason}
            for o in outcomes
            if not o.polled
        ],
    }


def create_app(
    broker: QuotaBroker | None = None,
    *,
    identity: WorkerIdentity | None = None,
    metrics: BrokerMetrics | None = None,
    credential_refresher: CredentialRefresher | None = None,
    subscription_tenants: Any | None = None,
    account_store: AccountStore | None = None,
    token_endpoint: Any | None = None,
) -> FastAPI:
    # The other three services do this and the broker did not, so every
    # `extra={...}` field it logged was discarded and its records arrived as
    # unstructured text. The first thing that cost: a refresher failing on every
    # sweep with the error type it attached invisible, leaving only a message
    # that reads like a permissions problem.
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

    app = FastAPI(title="swarm quota broker", version="0.1.0")
    app.state.broker = broker if broker is not None else build_broker()
    app.state.metrics = metrics or BrokerMetrics()
    environment = os.environ.get("ENVIRONMENT", "dev").strip().lower()
    app.state.identity = identity or WorkerIdentity(
        audience=os.environ.get("BROKER_AUDIENCE", ""),
        platform_accounts=tuple(
            a.strip()
            for a in os.environ.get("PLATFORM_SERVICE_ACCOUNTS", "").split(",")
            if a.strip()
        ),
        required=os.environ.get("REQUIRE_OIDC", "true").strip().lower()
        not in {"0", "false", "no", "off"},
        project_id=os.environ.get("PROJECT_ID", ""),
        hardened=environment not in {"dev", "test", "local"},
    )

    # This service is the platform's single writer of subscription credentials.
    # See quota_broker.credentials for why exactly one writer is required; the
    # refresh runs on the existing sweep tick rather than a timer of its own,
    # so there is no second thing to schedule, monitor or forget.
    # The account pool. Registered accounts are swept on the same tick, and
    # EVERY one is visited -- busy, idle or paused -- because a refresh token
    # that is never exchanged expires on its own. An "only what is in use"
    # optimisation here is what turns "no human ever steps in" back into "no
    # human steps in until the day they urgently need the idle account".
    if account_store is not None:
        app.state.account_store = account_store
    else:
        # Built from the BROKER's own client, not a second one. A separate
        # client would carry separate settings, and the first symptom of that
        # would be an account pool that is mysteriously empty in one process
        # and populated in another.
        app.state.account_store = AccountStore(app.state.broker.db)

    # THE RECORD THAT HAS TO OUTLIVE THE PROCESS. The broker cannot read the
    # worker-facing secrets it writes -- it holds `versions.add` and not
    # `versions.access` -- so when it asks "does that secret already hold this
    # token?" the only admissible answer is a positive record of what it put
    # there. An in-process note is not that record: this service runs at
    # minScale=0 and is redeployed several times a day, and each new process
    # published one more identical version before it learned anything. See
    # quota_broker.publishledger for the four deploys and four writes that
    # measured it.
    #
    # Built from the BROKER's own Firestore handle, for the same reason the
    # account store is: a second client carries second settings, and the first
    # symptom of that is a record that exists in one process and not another --
    # which is precisely the failure this closes.
    app.state.publish_ledger = (
        FirestorePublishLedger(app.state.broker.db)
        if getattr(app.state.broker, "db", None) is not None
        else InMemoryPublishLedger()
    )

    # ONE CREDENTIAL SWEEP AT A TIME, across every instance -- see
    # quota_broker.sweeplease. On the broker's own Firestore handle for the
    # reason the two above are: a lease held in one client's database and read
    # through another's is no lease at all.
    app.state.sweep_lease = build_sweep_lease(getattr(app.state.broker, "db", None))

    refresher, tenants = credential_refresher, subscription_tenants
    # Bound here rather than only inside the branch below: the usage poller
    # needs the same store, and reading it conditionally raised
    # UnboundLocalError in exactly the configuration meant to skip it.
    secret_store = None
    if refresher is None and _flag("CREDENTIAL_REFRESH_ENABLED", True):
        project_id = os.environ.get("PROJECT_ID", "").strip()
        if project_id:
            store = secret_store = SecretManagerStore(project_id)
            refresher = CredentialRefresher(
                store,
                HttpTokenEndpoint(),
                logger=log,
                ledger=app.state.publish_ledger,
            )
            if tenants is None:
                tenants = store.subscription_tenants
        else:
            log.warning(
                "PROJECT_ID is unset, so subscription credentials will not be "
                "refreshed; tenants using a Claude subscription will stop "
                "working when their access token expires"
            )
    app.state.credential_refresher = refresher

    # THE SEAM THAT WAS MISSING. `finish_account_authorization` reads
    # `app.state.token_endpoint` to redeem the authorization code, and nothing
    # ever set it -- so every /v1/accounts/exchange raised
    # `AttributeError: 'State' object has no attribute 'token_endpoint'`,
    # answered 500, and swarm-api turned that into a 503 reading "the account
    # pool answered HTTP 500". Registering an account has never once completed.
    #
    # HttpTokenEndpoint's own docstring says "Injected so tests never make a
    # network call" -- it was written to be injected and then never was. The
    # refresh path does not hit this because it calls `exchange` through
    # CredentialRefresher, which IS constructed above; only the redeem path
    # went through app.state.
    #
    # Injectable for the same reason as every other collaborator here: a test
    # that reaches the real endpoint is a test that needs the network and a
    # real authorization code.
    app.state.token_endpoint = token_endpoint or HttpTokenEndpoint()
    app.state.subscription_tenants = tenants or (lambda: [])
    # Registering an account writes two secrets, so the account routes need the
    # same store the refresher and the poller use. Exposed on app.state rather
    # than rebuilt, so all three are provably the same writer -- which is the
    # property that keeps a rotating credential from being corrupted.
    app.state.secret_store = secret_store

    # What an account's secrets need at creation time: where to replicate them,
    # what to label them, and who may read them.
    app.state.region = os.environ.get("REGION", "").strip()
    app.state.environment = os.environ.get("ENVIRONMENT", "").strip() or "dev"
    app.state.broker_service_account = os.environ.get(
        "BROKER_SERVICE_ACCOUNT", ""
    ).strip()

    # THE TENANT WORKER'S IDENTITY IS A TEMPLATE FROM TERRAFORM, not a pattern
    # rebuilt here. terraform/modules/tenancy owns what a tenant's service
    # account is called; a second copy of that rule in this file would be
    # correct until the day it was not, and the symptom -- a pod that cannot
    # read the credential it was assigned -- appears inside a job, nowhere near
    # this line.
    template = os.environ.get("WORKER_SERVICE_ACCOUNT_TEMPLATE", "").strip()

    def _worker_sa(tenant_id: str) -> str:
        # Empty means REFUSE, not "grant nobody". A secret created with no
        # worker accessor is one the tenant's pod cannot read, and that failure
        # surfaces much later as an unexplained auth error inside a job. The
        # register route turns this into a 422 naming the variable.
        if not template or "{tenant}" not in template:
            raise BrokerValidationError(
                "WORKER_SERVICE_ACCOUNT_TEMPLATE is unset or has no {tenant} "
                "placeholder, so an account's secret cannot be bound to the "
                "tenant that must read it. Refusing rather than creating a "
                "secret no pod can read."
            )
        return f"serviceAccount:{template.format(tenant=tenant_id)}"

    app.state.worker_service_account = _worker_sa

    # The usage poller needs the same Secret Manager store the refresher uses --
    # it reads each account's CURRENT access token, which the refresh above has
    # just made fresh. Ordering matters: polling with a token the refresh is
    # about to replace wastes one of about five calls per five minutes.
    #
    # Absent without PROJECT_ID, exactly like the refresher, so an
    # API-key-only deployment builds neither and the sweep reports neither.
    poller = None
    if (
        refresher is not None
        and secret_store is not None
        and getattr(app.state, "account_store", None) is not None
    ):
        poller = UsagePoller(
            secret_store,
            app.state.account_store,
            logger=log,
            max_polls=_int("USAGE_MAX_POLLS_PER_SWEEP", DEFAULT_MAX_POLLS_PER_SWEEP),
        )
    app.state.usage_poller = poller

    def _authorize(request: Request, authorization: str | None, tenant_id: str) -> str:
        """Confirm the caller may speak for `tenant_id`."""
        try:
            caller_tenant, is_platform = request.app.state.identity.resolve(authorization)
        except BrokerAuthError as exc:
            request.app.state.metrics.auth_failures.labels(kind="token").inc()
            raise exc
        if is_platform:
            return tenant_id
        if caller_tenant != tenant_id:
            request.app.state.metrics.auth_failures.labels(kind="tenant_mismatch").inc()
            raise BrokerAuthError("caller may not report quota for another tenant")
        return tenant_id

    @app.exception_handler(BrokerAuthError)
    async def auth_handler(request: Request, exc: BrokerAuthError) -> Response:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=403,
            content={"code": "forbidden", "message": str(exc)},
        )

    @app.exception_handler(BrokerValidationError)
    async def validation_handler(request: Request, exc: BrokerValidationError) -> Response:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=422,
            content={"code": "validation_failed", "message": str(exc)},
        )

    @app.exception_handler(MalformedAccountError)
    async def malformed_account_handler(
        request: Request, exc: MalformedAccountError
    ) -> Response:
        """One unreadable document fails the one request that touched it.

        500, because the fault is the broker's own stored data and not the
        request: a 4xx would send the caller looking for something it did
        wrong, and a retry will not help either way. NAMED, because a bare
        500 says nothing an operator can act on, and this one can say which
        document to go and fix. The body is the error's own message -- the
        id and the exception class, never a field value (see
        `MalformedAccountError`). Every other account keeps answering: the
        listing skips this document and the single-account routes only ever
        read the one they were asked about.
        """
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=500,
            content={
                "code": "account_malformed",
                "account_id": exc.account_id,
                "message": str(exc),
            },
        )

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(request: Request, response: Response) -> dict[str, Any]:
        try:
            request.app.state.broker.list(limit=1)
        except Exception as exc:  # pragma: no cover - depends on live Firestore
            response.status_code = 503
            return {"status": "not-ready", "detail": type(exc).__name__}
        return {"status": "ready"}

    @app.get("/metrics")
    def metrics_endpoint(request: Request) -> Response:
        body, content_type = request.app.state.metrics.render()
        return Response(content=body, media_type=content_type)

    @app.get("/v1/quota")
    def list_quota(
        request: Request,
        tenant_id: str | None = Query(default=None),
        provider: str | None = Query(default=None),
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        caller_tenant, is_platform = request.app.state.identity.resolve(authorization)
        scope = tenant_id if is_platform else caller_tenant
        states = request.app.state.broker.list(tenant_id=scope, provider=provider)
        for state in states:
            request.app.state.metrics.observe(state)
        return {"quota": [quota_to_api(s) for s in states], "tenant_id": scope}

    @app.get("/v1/quota/{provider}/{tenant_id}")
    def get_quota(
        request: Request,
        provider: str,
        tenant_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        name = _known_provider(provider)
        _authorize(request, authorization, tenant_id)
        state = request.app.state.broker.current(name, tenant_id)
        request.app.state.metrics.observe(state)
        return {"quota": quota_to_api(state)}

    @app.post("/v1/quota/{provider}/{tenant_id}/success")
    def report_success(
        request: Request,
        provider: str,
        tenant_id: str,
        body: SuccessReport = Body(default_factory=SuccessReport),
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        name = _known_provider(provider)
        _authorize(request, authorization, tenant_id)
        state = request.app.state.broker.observe_success(
            name,
            tenant_id,
            requests_remaining=body.requests_remaining,
            tokens_remaining=body.tokens_remaining,
            reset_at=body.reset_at,
        )
        request.app.state.metrics.reports.labels(provider=name, kind="success").inc()
        request.app.state.metrics.observe(state)
        return {"quota": quota_to_api(state)}

    @app.post("/v1/quota/{provider}/{tenant_id}/rate-limit")
    def report_rate_limit(
        request: Request,
        provider: str,
        tenant_id: str,
        body: RateLimitReport = Body(default_factory=RateLimitReport),
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        name = _known_provider(provider)
        _authorize(request, authorization, tenant_id)
        state = request.app.state.broker.observe_rate_limit(
            name,
            tenant_id,
            retry_after_seconds=body.retry_after_seconds,
            reset_at=body.reset_at,
        )
        request.app.state.metrics.reports.labels(provider=name, kind="rate_limit").inc()
        request.app.state.metrics.decreases.labels(provider=name).inc()
        request.app.state.metrics.observe(state)
        return {"quota": quota_to_api(state)}

    @app.post("/v1/quota/{provider}/{tenant_id}/exhausted")
    def report_exhausted(
        request: Request,
        provider: str,
        tenant_id: str,
        body: RateLimitReport = Body(default_factory=RateLimitReport),
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        name = _known_provider(provider)
        _authorize(request, authorization, tenant_id)
        state = request.app.state.broker.observe_exhausted(
            name,
            tenant_id,
            retry_after_seconds=body.retry_after_seconds,
            reset_at=body.reset_at,
        )
        request.app.state.metrics.reports.labels(provider=name, kind="exhausted").inc()
        request.app.state.metrics.observe(state)
        return {"quota": quota_to_api(state)}

    @app.put("/v1/quota/{provider}/{tenant_id}/hard-max")
    def set_hard_max(
        request: Request,
        provider: str,
        tenant_id: str,
        body: HardMaxRequest,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        name = _known_provider(provider)
        _, is_platform = request.app.state.identity.resolve(authorization)
        if not is_platform:
            request.app.state.metrics.auth_failures.labels(kind="not_platform").inc()
            raise BrokerAuthError("only the platform may change a configured hard max")
        state = request.app.state.broker.set_hard_max(name, tenant_id, body.hard_max)
        request.app.state.metrics.observe(state)
        return {"quota": quota_to_api(state)}

    # ----------------------------------------------------------------------
    # The account pool
    # ----------------------------------------------------------------------
    #
    # WHY THESE LIVE HERE AND NOT IN swarm-api. This service is the platform's
    # single writer for subscription credentials, and that is not a layering
    # preference -- refreshing an OAuth credential REVOKES the previous one, so
    # two writers racing on the same account brick it. swarm-api proxies to
    # these routes rather than touching Secret Manager itself, which keeps the
    # number of writers at one by construction rather than by agreement.

    def _accounts(request: Request) -> Any:
        store = getattr(request.app.state, "account_store", None)
        if store is None:
            raise BrokerValidationError("this deployment has no account store configured")
        return store

    @app.get("/v1/accounts")
    def list_accounts(
        request: Request,
        tenant_id: str | None = Query(default=None),
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        caller_tenant, is_platform = request.app.state.identity.resolve(authorization)
        store = _accounts(request)
        scope = tenant_id if is_platform else caller_tenant
        # `list_reporting` rather than `list`/`for_tenant`, because a listing is
        # READ BY A PERSON and a document the store could not parse is part of
        # the answer: without it the page shows four accounts where there are
        # five, says "4 accounts registered", and computes the pool's headroom
        # over four -- all silently. See `AccountStore.list_reporting`.
        listing = store.list_reporting()
        if scope:
            # `may_serve` is what `for_tenant` applies: owned AND lent-to, which
            # is the set a tenant may actually run on, not the set it owns.
            accounts = [a for a in listing.accounts if a.may_serve(scope)]
        else:
            accounts = listing.accounts
        return {
            "accounts": [account_to_api(a) for a in accounts],
            "tenant_id": scope,
            #: WHICH documents could not be read, narrowed to the ones this
            #: caller is entitled to see. A document id is
            #: `<owner_tenant>:<label>` by construction, so handing the whole
            #: list to a borrower would name another tenant's accounts --
            #: invariant 9 is about exactly that. A document whose id does not
            #: carry a tenant is shown to nobody but the platform.
            "unreadable_documents": (
                listing.unreadable
                if not scope
                else [d for d in listing.unreadable if d.split(":", 1)[0] == scope]
            ),
            #: The TOTAL, unfiltered, for every caller. A borrower may not learn
            #: whose account is broken; it must still learn that the list it is
            #: reading is incomplete, because otherwise it reads a short list as
            #: the whole pool.
            "unreadable_document_count": len(listing.unreadable),
        }

    def _provision_and_register(
        request: Request,
        *,
        store: Any,
        secrets: Any,
        owner_tenant: str,
        label: str,
        provider: str,
        lend_to: list[str],
        credential_payload: str,
        access_token: str,
    ) -> Any:
        """Create both secrets, bind their readers, then publish the account.

        ONE implementation for both ways in -- a pasted credential and a
        browser sign-in. Two would drift, and the thing they would drift on is
        which service account may read which secret.

        PROVISION FIRST, REGISTER LAST. A probe against the deployed service
        proved what the other order costs: `add_version` refuses to invent a
        secret, so a failure left a Firestore document for an account the pool
        would assign to an agent that then cannot authenticate. A secret with
        no document is invisible and harmless; a document with no secret is a
        live trap.
        """
        base = secret_name(owner_tenant, label)
        labels = {
            "managed-by": "swarm-secrets",
            "component": "swarm-account",
            "tenant": owner_tenant,
            "account": label,
            "provider": provider,
            "environment": request.app.state.environment,
        }
        broker_sa = request.app.state.broker_service_account
        if broker_sa and not broker_sa.startswith("serviceAccount:"):
            broker_sa = f"serviceAccount:{broker_sa}"
        worker_sa = request.app.state.worker_service_account(owner_tenant)
        region = request.app.state.region

        try:
            # `{base}-refresh` holds the PAIR and only this service may read it.
            # The worker is deliberately not an accessor: a pod that could read
            # the refresh token could mint successors forever, which is the
            # blast radius the two-secret split exists to prevent.
            secrets.ensure_secret(
                f"{base}{REFRESH_SUFFIX}",
                labels=labels,
                accessors=[broker_sa] if broker_sa else [],
                region=region,
            )
            secrets.ensure_secret(
                base,
                labels=labels,
                accessors=[sa for sa in (broker_sa, worker_sa) if sa],
                region=region,
            )
            secrets.add_version(f"{base}{REFRESH_SUFFIX}", credential_payload)
            secrets.add_version(base, access_token)
        except Exception as exc:
            log.error(
                "could not provision an account's secrets",
                extra={
                    "tenant_id": owner_tenant,
                    "label": label,
                    "error": type(exc).__name__,
                },
            )
            raise BrokerValidationError(
                f"could not provision the secrets for {owner_tenant}:{label}: "
                f"{type(exc).__name__}. No account was registered."
            ) from None

        # THIS PATH IS A WRITER TOO, so it owes the ledger a record. Without
        # it, the next sweep finds a base secret it cannot read and no record
        # of what is in it, and publishes one more identical version -- the
        # exact leak, restarted by the one code path that already knew the
        # answer. Best effort: a registration that provisioned both secrets
        # must not fail because a bookkeeping write did, and the cost of
        # losing it is one redundant version within five minutes.
        ledger = getattr(request.app.state, "publish_ledger", None)
        if ledger is not None:
            try:
                ledger.record(base, publish_fingerprint(access_token))
            except Exception as exc:
                log.warning(
                    "provisioned an account but could not record which access "
                    "token was published; the next sweep may add one "
                    "identical secret version",
                    extra={"secret": base, "error": type(exc).__name__},
                )

        account = store.register(
            owner_tenant, label, provider=provider, lend_to=lend_to
        )

        # THE WAY BACK, for the way people actually recover an account.
        #
        # Signing in again, or pasting a fresh credential, IS the fix for
        # REAUTH_REQUIRED -- and `register` deliberately preserves state, so
        # without this the account would keep refusing work after the thing
        # that was wrong with it had been replaced, carrying a `reason` that
        # is no longer true. The sweep cannot lift it either: `due_for_refresh`
        # skips marked accounts.
        #
        # A freshly written pair is the best evidence available at this
        # moment -- `parse_credential` has already refused a payload with no
        # refresh token. If the token turns out to be dead anyway, the next
        # sweep marks it again within five minutes, which is the cheap
        # direction to be wrong in.
        if account.state is AccountState.REAUTH_REQUIRED:
            account = store.clear_reauth_required(account.account_id) or account
        return account

    # ------------------------------------------------------------------
    # Adding an account from a browser
    # ------------------------------------------------------------------
    #
    # Two steps, because Anthropic's OAuth client accepts exactly one redirect
    # target -- its own callback page, which DISPLAYS a code. A third-party
    # application cannot register its own redirect, so the browser cannot come
    # back here and the code the page shows is what closes the loop.
    #
    # The PKCE verifier has to survive between the two steps, so it is held
    # server-side keyed by `state` rather than handed to the browser. A
    # verifier the client holds is a PKCE flow that proves nothing.

    PENDING_AUTH = "account_auth"
    PENDING_TTL = timedelta(minutes=15)

    @app.post("/v1/accounts/authorize")
    def begin_account_authorization(
        request: Request,
        body: AccountAuthorize,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        _authorize(request, authorization, body.owner_tenant)
        _accounts(request)  # refuse early if the pool is not configured
        validate_label(body.label)

        verifier = new_verifier()
        state = new_state()
        url = build_authorize_url(state=state, code_challenge=challenge_for(verifier))

        request.app.state.broker.db.collection(PENDING_AUTH).document(state).set(
            {
                "state": state,
                "verifier": verifier,
                "owner_tenant": body.owner_tenant,
                "label": body.label,
                "provider": body.provider,
                "lend_to": list(body.lend_to),
                "created_at": datetime.now(timezone.utc),
            }
        )
        return {
            "authorize_url": url,
            "state": state,
            "expires_in_seconds": int(PENDING_TTL.total_seconds()),
        }

    @app.post("/v1/accounts/exchange", status_code=201)
    def finish_account_authorization(
        request: Request,
        body: AccountExchange,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        store = _accounts(request)
        secrets_store = getattr(request.app.state, "secret_store", None)
        if secrets_store is None:
            raise BrokerValidationError("this deployment has no secret store configured")

        code, pasted_state = split_pasted_code(body.code)
        # The state in the paste is advisory; the one the client sent is what
        # identifies the pending sign-in. They should match, and a mismatch is
        # worth refusing rather than guessing between them.
        if pasted_state and pasted_state != body.state:
            raise BrokerValidationError(
                "the pasted code belongs to a different sign-in than the one "
                "this page started. Press Add account and sign in again."
            )

        ref = request.app.state.broker.db.collection(PENDING_AUTH).document(body.state)
        snap = ref.get()
        if not getattr(snap, "exists", False):
            raise BrokerValidationError(
                "this sign-in has expired or was already completed. Codes are "
                "single-use; press Add account to start again."
            )
        pending = snap.to_dict() or {}

        started = pending.get("created_at")
        if isinstance(started, datetime):
            age = datetime.now(timezone.utc) - _aware_utc(started)
            if age > PENDING_TTL:
                ref.delete()
                raise BrokerValidationError(
                    "this sign-in took too long and the code will have expired. "
                    "Press Add account to start again."
                )

        owner = str(pending.get("owner_tenant") or "")
        # The pending record decides the tenant, never the request. Otherwise a
        # caller could complete somebody else's sign-in into their own tenant.
        _authorize(request, authorization, owner)

        try:
            payload = request.app.state.token_endpoint.redeem(
                code=code,
                verifier=str(pending.get("verifier") or ""),
                redirect_uri=OAUTH_REDIRECT_URI,
                # The state the pending record is keyed by, which is the same
                # value the authorize URL carried. Claude Code sends it on the
                # token request and the form-encoded version here omitted it.
                state=body.state,
            )
        except CredentialError as exc:
            raise BrokerValidationError(str(exc)) from None

        # Inside its own guard, not the one above: the endpoint can answer 200
        # with a response this platform cannot use -- an access token and no
        # refresh token. That is not a transport failure and it is not the
        # person's mistake, and it must not 500.
        try:
            credential = credential_from_token_response(payload)
        except CredentialError as exc:
            raise BrokerValidationError(str(exc)) from None
        account = _provision_and_register(
            request,
            store=store,
            secrets=secrets_store,
            owner_tenant=owner,
            label=str(pending.get("label") or ""),
            provider=str(pending.get("provider") or "anthropic"),
            lend_to=list(pending.get("lend_to") or ()),
            credential_payload=json.dumps(
                {
                    "accessToken": credential.access_token,
                    "refreshToken": credential.refresh_token,
                    "expiresAt": int(credential.expires_at.timestamp() * 1000),
                }
            ),
            access_token=credential.access_token,
        )
        # Single-use, and deleted only once the account exists: a failure above
        # leaves the pending record so the person can paste again rather than
        # having to start the whole sign-in over.
        ref.delete()
        return {
            "account": account_to_api(account),
            "expires_at": credential.expires_at.isoformat(),
            "note": "stored write-only; no route in this service returns key material",
        }

    @app.post("/v1/accounts", status_code=201)
    def register_account(
        request: Request,
        body: AccountRegister,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        _authorize(request, authorization, body.owner_tenant)
        store = _accounts(request)
        secrets = getattr(request.app.state, "secret_store", None)
        if secrets is None:
            raise BrokerValidationError("this deployment has no secret store configured")

        # Parsed BEFORE anything is written. A credential with no refresh token
        # cannot be kept alive, and the whole promise of this feature is that
        # the operator logs in once. Refusing it here costs them one error
        # message; accepting it costs them a pool entry that looks healthy and
        # dies silently at its first expiry.
        try:
            credential = parse_credential(body.credential)
        except CredentialError as exc:
            raise BrokerValidationError(str(exc)) from None

        account = _provision_and_register(
            request,
            store=store,
            secrets=secrets,
            owner_tenant=body.owner_tenant,
            label=body.label,
            provider=body.provider,
            lend_to=body.lend_to,
            credential_payload=body.credential,
            access_token=credential.access_token,
        )

        return {
            "account": account_to_api(account),
            "expires_at": credential.expires_at.isoformat(),
            "note": "stored write-only; no route in this service returns key material",
        }

    @app.put("/v1/accounts/{account_id}/lending")
    def set_lending(
        request: Request,
        account_id: str,
        body: AccountLending,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        store = _accounts(request)
        account = store.get(account_id)
        if account is None:
            raise BrokerValidationError(f"no account {account_id!r}")
        _authorize(request, authorization, account.owner_tenant)
        updated = store.register(
            account.owner_tenant,
            account.label,
            provider=account.provider,
            lend_to=body.lend_to,
        )
        return {"account": account_to_api(updated)}

    @app.put("/v1/accounts/{account_id}/state")
    def set_account_state(
        request: Request,
        account_id: str,
        body: AccountStateChange,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        store = _accounts(request)
        account = store.get(account_id)
        if account is None:
            raise BrokerValidationError(f"no account {account_id!r}")
        _authorize(request, authorization, account.owner_tenant)
        try:
            state = AccountState(body.state.upper())
        except ValueError:
            raise BrokerValidationError(
                f"unknown account state {body.state!r}; "
                f"known: {', '.join(s.value for s in AccountState)}"
            ) from None
        store.set_state(account_id, state, body.reason)
        return {"account": account_to_api(store.get(account_id))}

    @app.post("/v1/accounts/{account_id}/refresh")
    def refresh_account(
        request: Request,
        account_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        """Exchange this account's credential now, instead of waiting for the sweep.

        The sweep already does this on a timer and is what makes the pool
        self-maintaining. This exists because a timer gives an operator no way
        to ANSWER the question "did that work?" -- they paste a credential,
        and then wait an unknown number of minutes to find out whether it was
        accepted. A button that reports back closes that loop.

        It goes through the same CredentialRefresher as the sweep rather than
        exchanging the token inline. Two code paths that both rotate a
        credential is precisely how a rotating credential gets corrupted: this
        service is the single writer, and "single" has to mean one
        implementation as well as one process.
        """
        store = _accounts(request)
        account = store.get(account_id)
        if account is None:
            raise BrokerValidationError(f"no account {account_id!r}")
        _authorize(request, authorization, account.owner_tenant)

        refresher = getattr(request.app.state, "credential_refresher", None)
        if refresher is None:
            raise BrokerValidationError(
                "this deployment has no credential refresher configured"
            )
        outcome = refresher.refresh_secret(store.secret_for(account), label=account.label)
        result = outcome.as_dict()

        # A refresh that fails because the refresh token is gone is not a
        # transient error and must not be retried by the sweep every five
        # minutes. Recording it against the account is what turns "it keeps
        # failing" into "this one needs a human", which is the only thing an
        # operator can act on.
        reason = result.get("reason")
        if reason in CREDENTIAL_NEEDS_A_HUMAN:
            store.mark_reauth_required(account_id, f"refresh failed: {reason}")
        elif reason == "refreshed":
            # AND THE WAY BACK. A completed exchange is proof the refresh token
            # works, which is the only thing REAUTH_REQUIRED ever claimed was
            # false. Leaving the mark would make the pool refuse an account
            # that demonstrably works, and the sweep cannot lift it --
            # `due_for_refresh` skips marked accounts, so the state it writes
            # is one the sweep can never undo.
            #
            # Only on `refreshed`, deliberately. `still_valid` says the stored
            # ACCESS token has hours left; it never presents the refresh token,
            # so it is silent about the half that died.
            store.clear_reauth_required(account_id)
        return {"refresh": result, "account": account_to_api(store.get(account_id))}

    @app.delete("/v1/accounts/{account_id}")
    def remove_account(
        request: Request,
        account_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        store = _accounts(request)
        account = store.get(account_id)
        if account is None:
            raise BrokerValidationError(f"no account {account_id!r}")
        _authorize(request, authorization, account.owner_tenant)
        # The DOCUMENT goes; the secret stays. Deleting a Secret Manager secret
        # is irreversible and takes its version history with it, and an account
        # removed by mistake is then unrecoverable rather than re-registerable.
        # The secret is left to a PERSON, deliberately, and nothing collects it
        # later: `apps/reconciler/` has no account code, so a comment here that
        # named it as the cleanup would have been describing a job nobody runs.
        # The response says "retained" so the caller knows there is one left.
        store.remove(account_id)
        return {"removed": account_id, "secret": "retained"}

    # ----------------------------------------------------------------------
    # Assignment: which account a starting agent runs on, and giving it back.
    #
    # These two are the pool's hot path and the only routes a WORKER calls.
    # Everything above is an operator's. They are deliberately the thinnest
    # possible pair -- pick, count, hand back the secret NAME; then un-count --
    # because the broker is the single writer of credentials and a worker that
    # could do more than this would be a second one.

    def _assignment_tenant(request: Request, authorization: str | None) -> str:
        """The tenant this caller may be assigned an account for.

        Derived from the caller's identity and never from the request. A
        PLATFORM caller is refused rather than allowed everything: it has no
        tenant, so `may_serve` has nothing to check, and "the platform" is not
        a tenant that any account was ever lent to. Refusing is the same rule
        as "'not configured' must mean refuse, never accept-anything" -- the
        alternative would hand out an arbitrary tenant's subscription
        credential to whatever ran the sweep tick.
        """
        try:
            caller_tenant, _is_platform = request.app.state.identity.resolve(authorization)
        except BrokerAuthError:
            request.app.state.metrics.auth_failures.labels(kind="token").inc()
            raise
        if not caller_tenant:
            request.app.state.metrics.auth_failures.labels(kind="no_tenant").inc()
            raise BrokerAuthError(
                "an account is assigned to a tenant, and this caller has none; "
                "accounts are assigned to a tenant's worker service account"
            )
        return caller_tenant

    @app.post("/v1/accounts/assign")
    def assign_account(
        request: Request,
        body: AccountAssign | None = Body(default=None),
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        """Lease the best account this tenant may run on, or say why there is none.

        NOT FINDING ONE IS A 200, and that is the whole shape of this route.
        `accounts.choose` already documents why it returns None rather than an
        exhausted account: the caller's correct response is to PARK, which
        costs nothing and resumes by itself the moment an account frees up,
        while a 4xx or 5xx here would cost the task one of its three attempts
        for a condition nobody did anything wrong to cause. An error would also
        be indistinguishable, at the caller, from the broker being down -- and
        those two want opposite responses.

        The `reason` is what makes the 200 actionable, and it separates two
        situations that look identical from the outside:

          * `no_accounts_registered` -- this tenant has no account it may use
            at all. The pool is not how this deployment runs, so the caller
            falls back to its per-tenant secret. This is what keeps every
            existing deployment working unchanged.
          * `no_account_available` -- accounts exist and none can take a new
            agent right now. The pool IS how this tenant runs, so the caller
            parks and comes back.

        `next_reset_at` is the instant the earliest blocking window clears, and
        `reason` says which kind of wait this is. The two are computed by
        `accounts.eligibility`, with the SAME arguments `choose()` was just
        called with, so they cannot disagree about what "unavailable" meant --
        which they did: `choose()` rejects on `headroom()`, which halves an
        aged reading, while the instant was computed from the raw remaining
        with no staleness at all. An account blocked only by that halving was
        rejected here and reported no blocking window, so the route answered
        "none available, and no idea when" and the worker fell back to its long
        poll for precisely the case the known-instant design was written for.

        A null `next_reset_at` therefore does NOT mean one thing, and the
        reason is what separates them: `pool_paused` is waiting on a person,
        `no_recent_reading` is waiting on the broker's own next usage poll --
        minutes, not hours -- and neither is a claim that the accounts are
        spent.
        """
        tenant_id = _assignment_tenant(request, authorization)
        # Built per call rather than as a default argument: a model instance in
        # a signature default is shared by every request that omits the body.
        asked = body or AccountAssign()
        provider = _known_provider(asked.provider)
        exclude = tuple(a for a in asked.exclude if a)
        store = _accounts(request)
        now = datetime.now(timezone.utc)

        # Owned AND lent-to, of this request's provider: exactly the set
        # invariant 9 permits. `accounts_serving` is the one statement of it,
        # and the scheduler's admission asks the same function whether it is
        # empty, so the two cannot disagree about who the pool serves (#169).
        candidates = accounts_serving(store.list(), tenant_id, provider)

        chosen = choose(candidates, tenant_id, now, exclude=exclude)
        if chosen is None:
            reason, next_reset = eligibility(
                candidates, tenant_id, now, exclude=exclude
            )
            return {
                "account_id": None,
                "assignment_id": None,
                "secret": None,
                "account": None,
                "reason": reason.value,
                "next_reset_at": next_reset.isoformat() if next_reset else None,
            }

        # The account store is built from the broker's own Firestore client,
        # deliberately (see create_app), so this is the same client and the
        # same database the listing above read from.
        held = acquire_hold(
            request.app.state.broker.db,
            chosen.account_id,
            tenant_id=tenant_id,
            now=now,
            task_id=asked.task_id,
            attempt_id=asked.attempt_id,
        )
        if held is None:
            # Removed between the read and the hold. Nothing was counted, so
            # there is nothing to undo and nothing to hand out.
            return {
                "account_id": None,
                "assignment_id": None,
                "secret": None,
                "account": None,
                "reason": Unavailable.NO_ACCOUNT_AVAILABLE.value,
                "next_reset_at": None,
            }
        assignment_id, assigned = held

        # NOTHING BELOW MAY FAIL, because the hold above is committed (#243).
        # This used to re-read the account with `store.get`, which raises on a
        # document that went malformed since the listing -- answering 500 over
        # a hold whose id the caller never learned, so nothing could release
        # it. Everything the response needs is already here: the account as
        # `choose()` saw it, and the count the hold's own transaction wrote.
        current = replace(chosen, assigned=assigned, last_assigned_at=now)
        log.info(
            "assigned an account",
            extra={
                "account_id": chosen.account_id,
                "assignment_id": assignment_id,
                "tenant_id": tenant_id,
                "provider": provider,
                "borrowed": chosen.owner_tenant != tenant_id,
                "assigned": assigned,
            },
        )
        return {
            "account_id": chosen.account_id,
            # The HOLD this worker now has. It carries it back on release, so a
            # release can only ever give back the assignment it was issued
            # for, and the hold expires on its own if the worker is killed
            # before it can release anything.
            "assignment_id": assignment_id,
            # THE NAME, NEVER THE VALUE. `{base}` holds only the access token
            # and the caller reads it from Secret Manager under its own service
            # account -- so the credential travels over Google's authorized
            # path rather than through this response body, and this service
            # keeps the property that no route of its returns key material.
            "secret": store.secret_for(chosen),
            "account": account_to_api(current, now=now),
            "reason": "",
            "next_reset_at": None,
        }

    @app.post("/v1/accounts/{account_id}/release")
    def release_account(
        request: Request,
        account_id: str,
        body: AccountRelease,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        """Give one named assignment back. Idempotent.

        TWO CHECKS, NOT ONE. `may_serve` (as `_raw_may_serve`, over the raw
        document -- see `_raw_account`) still gates the route -- an account
        lent from tenant A to tenant B is assigned to B's agents, so B is who
        releases it, and requiring the OWNER would leave every borrowed
        assignment counted forever and the account sorting last in `choose()`
        for good. But `may_serve` alone is not proof that this caller held
        anything: it was the only check here, so any tenant an account was lent
        to could decrement the owner's counter as often as it liked. The
        assignment id is the second check, and the hold it names records which
        tenant it was issued to.

        Releasing twice is not an error and does not take someone else's slot:
        the second call finds no matching hold and says so. That genuinely
        happens -- a worker releases on its exit path and the same call is
        retried after a lost response.

        A worker that never releases at all is covered by the hold's own
        expiry, pruned by the quota sweep. It is NOT covered by the reconciler,
        which has no account code; this docstring used to say otherwise.
        """
        _accounts(request)  # refuse early if the pool is not configured
        db = request.app.state.broker.db
        # The RAW document, never the decoded `Account` (#243): a hold on a
        # document that has since gone malformed must still be given back, and
        # this route needs only the holds, the owner and the lending list.
        account = _raw_account(db, account_id)
        if account is None:
            # Removed while an agent was still on it. There is no hold left to
            # give back, and refusing would make the worker's exit path look
            # like a failure for something an operator did on purpose.
            return {
                "account_id": account_id,
                "assigned": None,
                "reason": "account_removed",
            }

        try:
            caller_tenant, is_platform = request.app.state.identity.resolve(authorization)
        except BrokerAuthError:
            request.app.state.metrics.auth_failures.labels(kind="token").inc()
            raise
        if not is_platform and not (
            caller_tenant and _raw_may_serve(account, caller_tenant)
        ):
            request.app.state.metrics.auth_failures.labels(kind="tenant_mismatch").inc()
            raise BrokerAuthError(
                "caller may not release an account it could not have been assigned"
            )

        now = datetime.now(timezone.utc)
        assigned, was_held = release_hold(
            db,
            account_id,
            assignment_id=body.assignment_id,
            tenant_id=None if is_platform else caller_tenant,
            now=now,
            unusable=body.unusable,
        )
        if body.unusable and was_held:
            owner = account.get("owner_tenant")
            owner = owner if isinstance(owner, str) else ""
            log.error(
                "a worker could not read the account it was assigned",
                extra={
                    "account_id": account_id,
                    "tenant_id": caller_tenant,
                    "owner_tenant": owner,
                    "borrowed": bool(caller_tenant) and caller_tenant != owner,
                    "detail": body.unusable,
                },
            )
        # NO RENDERED ACCOUNT HERE, deliberately. This used to answer with
        # `account_to_api(store.get(account_id))` as a courtesy -- but
        # `release_hold`'s own transaction just rewrote `holds` and `assigned`
        # on this document (`_hold_payload`, always, whether or not anything
        # was held), and that rewrite can be exactly what turns a malformed
        # document decodable: a document bad only in its `assigned` field
        # decodes clean the moment `assigned` is freshly written, while every
        # OTHER field the document was planted or hand-edited with -- `reason`
        # not least -- decodes right along with it and reaches this response.
        # The caller (`agent_worker.accountlease`) only ever reads `assigned`;
        # nothing here needs a rendered account, so nothing here reads one.
        return {
            "account_id": account_id,
            "assigned": assigned,
            # "" when the hold was found and released; `not_held` when it was
            # not, which is what a duplicate release and a forged id both look
            # like. Not an error either way -- but not silence either.
            "reason": "" if was_held else "not_held",
        }

    # -- who holds an account, and who held it (#379) ----------------------
    #
    # PLATFORM-ONLY, both of them. They name every tenant's tasks on the
    # account, which no single tenant may see; the one caller is swarm-api,
    # which resolves the human's tenant and filters per viewer
    # (swarm_api/accountholds.py). A worker's token is a tenant's token and is
    # refused here exactly as `set_hard_max` and the sweep refuse it.
    #
    # NEITHER SERVES THE ASSIGNMENT ID. It is what authorises a release, so a
    # reader holding it could give back somebody else's hold. Nor the secret
    # name, nor anything else of the account's beyond who owns and borrows it.

    def _platform_only(
        request: Request,
        header: str | None,
        refusal: str = (
            "only the platform may read an account's holders; they name "
            "other tenants' work"
        ),
    ) -> None:
        try:
            _, is_platform = request.app.state.identity.resolve(header)
        except BrokerAuthError:
            request.app.state.metrics.auth_failures.labels(kind="token").inc()
            raise
        if not is_platform:
            request.app.state.metrics.auth_failures.labels(kind="not_platform").inc()
            raise BrokerAuthError(refusal)

    def _account_or_refuse(request: Request, account_id: str) -> dict[str, Any]:
        _accounts(request)  # refuse early if the pool is not configured
        # RAW, like the release path (#243): who holds a malformed document is
        # exactly what an operator repairing it needs to see.
        data = _raw_account(request.app.state.broker.db, account_id)
        if data is None:
            raise BrokerValidationError(f"no account {account_id!r}")
        return data

    @app.get("/v1/accounts/{account_id}/holds")
    def account_holds(
        request: Request,
        account_id: str,
        bearer: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        """The LIVE holds on one account, with the task each worker named.

        Expired holds are filtered here with the same `is_expired` the counter
        uses, so a hold whose worker was killed is not served as a current
        holder in the minutes before the sweep prunes it.
        """
        _platform_only(request, bearer)
        data = _account_or_refuse(request, account_id)
        now = datetime.now(timezone.utc)
        owner = data.get("owner_tenant")
        lend_to = data.get("lend_to")
        return {
            "account_id": account_id,
            "owner_tenant": owner if isinstance(owner, str) else "",
            "lend_to": [t for t in lend_to if isinstance(t, str)]
            if isinstance(lend_to, (list, tuple))
            else [],
            "holds": [_hold_to_api(h) for h in _live_holds(data, now)],
            "observed_at": now.isoformat(),
        }

    @app.get("/v1/accounts/{account_id}/holds/history")
    def account_hold_history(
        request: Request,
        account_id: str,
        start: str | None = Query(default=None, alias="from"),
        end: str | None = Query(default=None, alias="to"),
        cursor: str | None = Query(default=None, max_length=80),
        limit: int = Query(default=HISTORY_PAGE, ge=1, le=HISTORY_PAGE_MAX),
        bearer: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        """Recorded spans on one account taken in [from, to), newest first.

        Served from `account_holds`, which the hold transactions write; one
        query on (account_id ==, assigned_at range, order by assigned_at
        desc), which is the composite index terraform declares as
        `account-holds-account-assigned`. A hold from before #379 has no
        `assigned_at` and so falls outside every window -- it is not dated, and
        is not given a date it never had.
        """
        _platform_only(request, bearer)
        _account_or_refuse(request, account_id)
        now = datetime.now(timezone.utc)
        until = _instant(end, "to", now) if end else now
        if start:
            since = _instant(start, "from", now)
        else:
            try:
                since = until - HISTORY_DEFAULT_SPAN
            except OverflowError:
                raise BrokerValidationError("`to` leaves no default window before it") from None
        if since >= until:
            raise BrokerValidationError("`from` must be earlier than `to`")
        if until - since > HOLD_LOG_RETENTION:
            # No record outlives its TTL, so a longer window can only be a scan
            # of everything the index holds.
            raise BrokerValidationError(
                f"the window `from`..`to` may span at most {HOLD_LOG_RETENTION.days} days"
            )
        position = _history_cursor(cursor, now) if cursor else None
        if position is not None and position[0] >= until:
            raise BrokerValidationError("the cursor is outside the window")

        from google.cloud.firestore_v1.base_query import FieldFilter

        top = position[0] if position is not None else until
        skip = position[1] if position is not None else 0
        query = (
            request.app.state.broker.db.collection(HOLD_LOG_COLLECTION)
            .where(filter=FieldFilter("account_id", "==", account_id))
            .where(filter=FieldFilter("assigned_at", ">=", since))
            .where(filter=FieldFilter("assigned_at", "<=" if position else "<", top))
            .order_by("assigned_at", direction="DESCENDING")
            .limit(limit + skip + 1)
        )
        rows = [snap.to_dict() or {} for snap in query.stream()]
        # The first `skip` rows at the cursor's instant were served on the
        # previous page. Ties share an instant; skipping by COUNT at that
        # instant rather than "strictly before it" is what keeps them.
        rows = rows[skip:]
        more = len(rows) > limit
        page = rows[:limit]
        next_cursor = None
        if more and page:
            last = page[-1]["assigned_at"]
            at_last = sum(1 for r in page if r.get("assigned_at") == last)
            if position is not None and last == position[0]:
                at_last += skip
            next_cursor = f"{_aware_utc(last).isoformat()}|{at_last}"
        return {
            "account_id": account_id,
            "from": since.isoformat(),
            "to": until.isoformat(),
            "spans": [_span_to_api(r) for r in page],
            "next_cursor": next_cursor,
        }

    @app.post("/v1/holds/release-attempt")
    def release_fenced_attempt_holds(
        request: Request,
        body: AttemptHoldsRelease,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        """Give back every account hold of one FENCED attempt, now (#380).

        PLATFORM-ONLY, by the same rule as the sweep and the holders reads: the
        caller is the reconciler, and it names another tenant's work. A
        worker's token is refused -- a tenant able to call this could release
        holds by guessing ids, and the worker's own path is the named,
        tenant-checked `/v1/accounts/{id}/release`.

        The reconciler calls it only AFTER its fence has committed, so a
        fence that loses its race (#372) releases nothing a live worker is
        still using. Idempotent: a repeat finds nothing and says so.
        """
        _platform_only(
            request,
            authorization,
            "only the platform may release another attempt's account holds",
        )
        result = _release_attempt_holds_everywhere(
            request.app.state.broker.db,
            task_id=body.task_id,
            attempt_id=body.attempt_id,
            now=datetime.now(timezone.utc),
        )
        if result["released"]:
            log.warning(
                "released the account holds of a fenced attempt",
                extra={
                    "task_id": body.task_id,
                    "attempt_id": body.attempt_id,
                    "released": result["released"],
                    "accounts": result["accounts"],
                },
            )
        return {"task_id": body.task_id, "attempt_id": body.attempt_id, **result}

    @app.post("/v1/quota/sweep")
    def sweep(
        request: Request,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, Any]:
        _, is_platform = request.app.state.identity.resolve(authorization)
        if not is_platform:
            request.app.state.metrics.auth_failures.labels(kind="not_platform").inc()
            raise BrokerAuthError("only the platform may run a quota sweep")
        result = request.app.state.broker.sweep()

        # THE HOLD BACKSTOP, and it runs on its own -- not inside the refresher
        # block below, because it has nothing to do with credentials and must
        # still happen on a deployment that has no refresher configured.
        #
        # A worker releases its account on its own exit path, which covers
        # every orderly exit. It does not cover SIGKILL, an OOM kill, a node
        # preemption or a Cloud Run task kill, and nothing else in this
        # repository knows the assignment existed: `apps/reconciler/` has no
        # account code at all. Without this, `assigned` would drift upward
        # forever -- and it is `choose()`'s load-spreading tiebreak and the
        # number an operator reads as "agents on this account", so the pool's
        # spreading would degrade permanently and silently.
        account_store = getattr(request.app.state, "account_store", None)
        if account_store is not None:
            # Guarded like the two sweeps below and for the same reason -- this
            # tick is what un-parks throttled tenants, and one unreadable
            # account document must not stall every tenant's quota recovery.
            # Its own try block rather than `_sweep_block`, which summarises a
            # list of refresh outcomes and has nothing to say about holds.
            try:
                result["holds"] = _prune_all_holds(
                    request.app.state.broker.db,
                    account_store,
                    datetime.now(timezone.utc),
                )
            except Exception as exc:  # pragma: no cover - defence in depth
                log.error(
                    "account hold sweep failed",
                    extra={"error": type(exc).__name__, "detail": str(exc)[:200]},
                )
                result["holds"] = {"error": type(exc).__name__}

        # The same scheduled tick refreshes subscription credentials, because
        # this service is the platform's single writer for them -- see
        # quota_broker.credentials for why more than one writer corrupts a
        # rotating credential. A refresher is only present when the deployment
        # has subscription tenants; an API-key-only deployment has none and this
        # is a no-op.
        refresher = getattr(request.app.state, "credential_refresher", None)
        if refresher is not None:
            # THE CREDENTIAL PHASE RUNS UNDER THE SWEEP LEASE, and only this
            # phase. See quota_broker.sweeplease for the race it closes. The
            # quota recompute and the hold backstop above stay outside it on
            # purpose: both are idempotent, and they are what un-parks
            # throttled tenants, so a stuck credential sweep must never be able
            # to delay them.
            lease = request.app.state.sweep_lease
            holder = new_holder()
            try:
                taken = lease.acquire(holder)
            except Exception as exc:
                # FAIL CLOSED. No proof of exclusion, no exchange: skipping one
                # tick costs five minutes on a token refreshed hours before it
                # expires, and an unexcluded exchange can cost the token.
                log.error(
                    "could not take the sweep lease; skipping the credential phase this tick",
                    extra={"error": type(exc).__name__, "detail": str(exc)[:200]},
                )
                result["sweep_lease"] = {"acquired": False, "error": type(exc).__name__}
                return result
            if not taken.acquired:
                # Not an error: another sweep is doing this work right now --
                # typically Cloud Scheduler's retry of a tick that is still
                # running. Answered 200 so the scheduler does not retry this
                # one as well.
                log.info(
                    "another sweep holds the lease; skipping the credential phase this tick",
                    extra={
                        "held_by": taken.holder,
                        "expires_at": taken.expires_at.isoformat() if taken.expires_at else None,
                    },
                )
                result["sweep_lease"] = {
                    "acquired": False,
                    "held_by": taken.holder,
                    "expires_at": taken.expires_at.isoformat() if taken.expires_at else None,
                }
                return result

            fence = LeaseFence(lease, holder)
            try:
                _credential_phase(request, refresher, fence, result)
            finally:
                released = fence.release()
            result["sweep_lease"] = {
                "acquired": True,
                "holder": holder,
                "generation": taken.generation,
                # True when another sweep took the lease over mid-pass. The
                # pass stopped before its next exchange, and whatever it had
                # not reached is the next tick's.
                "lost": fence.lost,
                "released": released,
            }
        return result

    def _credential_phase(
        request: Request, refresher: Any, fence: LeaseFence, result: dict[str, Any]
    ) -> None:
        """Refresh credentials and poll usage, with `fence` asked before each exchange."""
        # Credential refresh must never take the quota sweep down with it.
        # The sweep is what un-parks throttled tenants; if a Secret Manager
        # permission error could 500 this endpoint, one broken credential
        # would stall every tenant's quota recovery.
        # Two sweeps, reported separately. Sharing one try block meant a
        # bug in either was indistinguishable from a failure of the other,
        # and the first thing it hid was a missing method on the account
        # path masking a perfectly good credential result.
        result["credentials"] = _sweep_block(
            lambda: refresher.sweep(
                request.app.state.subscription_tenants(), keep_going=fence
            ),
            "subscription credential sweep failed",
        )

        # Every registered account, whether or not anything is using it.
        # See quota_broker.accounts.due_for_refresh for why there is no
        # "in use" filter.
        #
        # Its own summariser rather than `_sweep_block`, because this one
        # WRITES: an account whose refresh token is gone is marked
        # REAUTH_REQUIRED on its document, which is what stops the retry
        # loop and what puts the dead account in front of an operator.
        store = getattr(request.app.state, "account_store", None)
        if store is None:
            return
        result["accounts"] = _sweep_account_pool(
            refresher, store, datetime.now(timezone.utc), keep_going=fence
        )

        # Read what each account has LEFT. Separate from the refresh
        # above and reported separately, for the reason the comment
        # there gives: sharing a block makes a bug in one
        # indistinguishable from a failure of the other.
        #
        # This is the caller `record_reading` never had. Without it
        # every account reported full headroom forever, and the assign
        # floor -- the check that stops an agent starting on an account
        # with 2% left -- could never fire.
        #
        # Budgeted rather than exhaustive: the endpoint allows roughly
        # five calls per five minutes and shares that with anything else
        # polling the same accounts. See quota_broker.usagepoll. Under the
        # lease too, because two concurrent rounds spend that budget twice.
        poller = getattr(request.app.state, "usage_poller", None)
        if poller is not None:
            result["usage"] = _poll_block(
                lambda: poller.poll_round(store.list(), keep_going=fence),
                "account usage poll failed",
            )

    return app


def main() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    uvicorn.run(
        "quota_broker.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "BrokerAuthError",
    "BrokerMetrics",
    "BrokerValidationError",
    "ProviderState",
    "WorkerIdentity",
    "acquire_hold",
    "build_broker",
    "prune_holds",
    "release_hold",
    "create_app",
]
