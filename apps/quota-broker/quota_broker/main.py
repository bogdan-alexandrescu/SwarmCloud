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

import logging
import os
import re
from datetime import datetime, timezone
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
from .oauth import CredentialError, parse_credential
from .usagepoll import UsagePoller
from .accounts import DEFAULT_STALE_AFTER, AccountState, due_for_refresh
from .accountstore import AccountStore
from .credentials import REFRESH_SUFFIX, CredentialRefresher
from .oauth import HttpTokenEndpoint
from .secretstore import SecretManagerStore
from .service import QuotaBroker, quota_to_firestore
from .settings import BrokerSettings

log = logging.getLogger(__name__)

#: Prefixes the two provisioning paths use for a tenant's worker service
#: account: terraform's `tenancy` module writes `swarm-agent-worker-<tenant>`,
#: scripts/register-tenant.sh writes `swarm-t-<tenant>`. Both are accepted
#: because both really exist; nothing else is.
WORKER_SA_PREFIXES = ("swarm-agent-worker", "swarm-t")


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


class AccountLending(StrictModel):
    lend_to: list[str] = Field(default_factory=list, max_length=50)


class AccountStateChange(StrictModel):
    state: str = Field(min_length=1, max_length=32)
    reason: str = Field(default="", max_length=512)


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
    }


def quota_to_api(state: QuotaState) -> dict[str, Any]:
    payload = quota_to_firestore(state)
    payload["effective_limit"] = state.effective_limit
    payload["quota_derived_limit_now"] = quota_derived_limit_for(state)
    return payload



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
    }


def create_app(
    broker: QuotaBroker | None = None,
    *,
    identity: WorkerIdentity | None = None,
    metrics: BrokerMetrics | None = None,
    credential_refresher: CredentialRefresher | None = None,
    subscription_tenants: Any | None = None,
    account_store: AccountStore | None = None,
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

    refresher, tenants = credential_refresher, subscription_tenants
    # Bound here rather than only inside the branch below: the usage poller
    # needs the same store, and reading it conditionally raised
    # UnboundLocalError in exactly the configuration meant to skip it.
    secret_store = None
    if refresher is None and _flag("CREDENTIAL_REFRESH_ENABLED", True):
        project_id = os.environ.get("PROJECT_ID", "").strip()
        if project_id:
            store = secret_store = SecretManagerStore(project_id)
            refresher = CredentialRefresher(store, HttpTokenEndpoint(), logger=log)
            if tenants is None:
                tenants = store.subscription_tenants
        else:
            log.warning(
                "PROJECT_ID is unset, so subscription credentials will not be "
                "refreshed; tenants using a Claude subscription will stop "
                "working when their access token expires"
            )
    app.state.credential_refresher = refresher
    app.state.subscription_tenants = tenants or (lambda: [])
    # Registering an account writes two secrets, so the account routes need the
    # same store the refresher and the poller use. Exposed on app.state rather
    # than rebuilt, so all three are provably the same writer -- which is the
    # property that keeps a rotating credential from being corrupted.
    app.state.secret_store = secret_store

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
        if scope:
            # `for_tenant` returns owned AND lent-to accounts, which is the set
            # a tenant may actually run on -- not the set it owns.
            accounts = store.for_tenant(scope)
        else:
            accounts = store.list()
        return {
            "accounts": [account_to_api(a) for a in accounts],
            "tenant_id": scope,
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

        account = store.register(
            body.owner_tenant,
            body.label,
            provider=body.provider,
            lend_to=body.lend_to,
        )
        base = store.secret_for(account)

        # TWO SECRETS, and the split is the security boundary. `{base}-refresh`
        # holds the pair and is read only by this service, the single writer.
        # `{base}` holds ONLY the access token and is what the tenant's pod
        # reads into CLAUDE_CODE_OAUTH_TOKEN -- so a compromised pod has a
        # credential that expires, not one that can mint successors forever.
        #
        # Stored as pasted rather than re-serialised: `parse_credential` is the
        # reader, and writing back a normalised shape would make this the only
        # place that decides what that shape is.
        secrets.add_version(f"{base}{REFRESH_SUFFIX}", body.credential)
        secrets.add_version(base, credential.access_token)

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
        if result.get("reason") in ("reauth_required", "unreadable"):
            store.set_state(
                account_id,
                AccountState.REAUTH_REQUIRED,
                f"refresh failed: {result.get('reason')}",
            )
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
        # Cleaning up secrets is the reconciler's job, on its own schedule.
        store.remove(account_id)
        return {"removed": account_id, "secret": "retained"}

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

        # The same scheduled tick refreshes subscription credentials, because
        # this service is the platform's single writer for them -- see
        # quota_broker.credentials for why more than one writer corrupts a
        # rotating credential. A refresher is only present when the deployment
        # has subscription tenants; an API-key-only deployment has none and this
        # is a no-op.
        refresher = getattr(request.app.state, "credential_refresher", None)
        if refresher is not None:
            # Credential refresh must never take the quota sweep down with it.
            # The sweep is what un-parks throttled tenants; if a Secret Manager
            # permission error could 500 this endpoint, one broken credential
            # would stall every tenant's quota recovery.
            # Two sweeps, reported separately. Sharing one try block meant a
            # bug in either was indistinguishable from a failure of the other,
            # and the first thing it hid was a missing method on the account
            # path masking a perfectly good credential result.
            result["credentials"] = _sweep_block(
                lambda: refresher.sweep(request.app.state.subscription_tenants()),
                "subscription credential sweep failed",
            )

            # Every registered account, whether or not anything is using it.
            # See quota_broker.accounts.due_for_refresh for why there is no
            # "in use" filter.
            store = getattr(request.app.state, "account_store", None)
            if store is not None:
                result["accounts"] = _sweep_block(
                    lambda: refresher.sweep_accounts(
                        [
                            (store.secret_for(a), a.label)
                            for a in due_for_refresh(
                                store.list(), datetime.now(timezone.utc)
                            )
                        ]
                    ),
                    "account pool sweep failed",
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
                # polling the same accounts. See quota_broker.usagepoll.
                poller = getattr(request.app.state, "usage_poller", None)
                if poller is not None:
                    result["usage"] = _sweep_block(
                        lambda: [o.__dict__ for o in poller.poll_round(store.list())],
                        "account usage poll failed",
                    )
        return result

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
    "build_broker",
    "create_app",
]
