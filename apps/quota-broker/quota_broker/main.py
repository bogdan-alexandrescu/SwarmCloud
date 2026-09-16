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
from datetime import datetime
from typing import Any

from fastapi import Body, FastAPI, Header, Query, Request, Response
from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from pydantic import BaseModel, ConfigDict, Field

from swarm_common.models import ProviderState, QuotaState
from swarm_common.profiles import RUNNER_PROFILES

from .aimd import quota_derived_limit_for
from .credentials import CredentialRefresher
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


def quota_to_api(state: QuotaState) -> dict[str, Any]:
    payload = quota_to_firestore(state)
    payload["effective_limit"] = state.effective_limit
    payload["quota_derived_limit_now"] = quota_derived_limit_for(state)
    return payload


def create_app(
    broker: QuotaBroker | None = None,
    *,
    identity: WorkerIdentity | None = None,
    metrics: BrokerMetrics | None = None,
    credential_refresher: CredentialRefresher | None = None,
    subscription_tenants: Any | None = None,
) -> FastAPI:
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
    refresher, tenants = credential_refresher, subscription_tenants
    if refresher is None and _flag("CREDENTIAL_REFRESH_ENABLED", True):
        project_id = os.environ.get("PROJECT_ID", "").strip()
        if project_id:
            store = SecretManagerStore(project_id)
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
            try:
                pairs = request.app.state.subscription_tenants()
                outcomes = refresher.sweep(pairs)
            except Exception as exc:
                log.error(
                    "subscription credential sweep failed",
                    extra={"error": type(exc).__name__, "detail": str(exc)[:200]},
                )
                result["credentials"] = {"error": type(exc).__name__}
            else:
                result["credentials"] = {
                    "examined": len(outcomes),
                    "refreshed": sum(1 for o in outcomes if o.refreshed),
                    "reauth_required": [
                        o.tenant_id for o in outcomes if o.reason == "reauth_required"
                    ],
                }
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
