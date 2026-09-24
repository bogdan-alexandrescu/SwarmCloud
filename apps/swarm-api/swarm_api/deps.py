"""Composition root and FastAPI dependencies.

Every collaborator the API uses is constructed here and hung off
`app.state.ctx`, so the routes never reach for a global and a test can build the
whole app around an in-memory Firestore and a static token verifier without
patching a single import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import logging

from fastapi import Depends, Header, Request

from swarm_common.models import utcnow

from .auth import (
    AuthContext,
    Authenticator,
    GoogleTokenVerifier,
    IapAssertionVerifier,
    TokenVerifier,
    require_admin,
)
from .credentials import CredentialWriter, SecretManagerCredentials
from .errors import ValidationFailed
from .groups import CloudIdentityGroups, MembershipResolver
from .inspect import InspectionService
from .metrics import ApiMetrics
from .objects import ObjectReader, build_object_reader
from .ratelimit import TokenBucketLimiter
from .rollup import WorkflowRollups
from .service import SubmissionService
from .settings import ApiSettings
from .store import Store
from .waker import NullWaker, PubSubWaker, SchedulerWaker

#: Sentinel for "the caller did not pass this", kept distinct from None because
#: None is a real value for `objects` -- it is how a deployment says it has no
#: artifact store at all.
_MISSING = object()


@dataclass
class AppContext:
    settings: ApiSettings
    db: Any
    store: Store
    authenticator: Authenticator
    submissions: SubmissionService
    credentials: CredentialWriter
    limiter: TokenBucketLimiter
    metrics: ApiMetrics
    waker: SchedulerWaker
    #: Reads checkpoints and logs out of the artifact bucket. The SERVICE is
    #: always present; the READER inside it may be None when no bucket is
    #: configured, which the service turns into "no artifact store is
    #: configured for this deployment" -- a deployment problem with a named
    #: fix -- rather than into an empty list.
    inspection: InspectionService
    #: Derives a workflow's state from its steps, reports drift against the
    #: stored copy, and writes the stored copy back. Constructed here like every
    #: other collaborator so a test gets the shipped code path over an in-memory
    #: Firestore rather than a patched import.
    rollups: WorkflowRollups
    now: Callable[[], Any] = utcnow

    def ready(self) -> tuple[bool, str]:
        """Readiness is a real Firestore round trip, not a hard-coded 200.

        The control document is one small read that proves credentials, network
        and the named database all work. A readiness probe that cannot fail is
        worse than none: it makes a broken revision look healthy.
        """
        try:
            self.store.get_control()
        except Exception as exc:  # pragma: no cover - depends on live Firestore
            return False, f"firestore unavailable: {type(exc).__name__}"
        return True, "ok"


def build_firestore(settings: ApiSettings) -> Any:
    from google.cloud import firestore

    # The NAMED database, never `(default)`: this is a shared project and the
    # default database belongs to other teams.
    return firestore.Client(
        project=settings.project_id,
        database=settings.core.firestore_database,
    )


def build_context(
    *,
    settings: ApiSettings | None = None,
    db: Any | None = None,
    verifier: TokenVerifier | None = None,
    groups: MembershipResolver | None = None,
    credentials: CredentialWriter | None = None,
    waker: SchedulerWaker | None = None,
    metrics: ApiMetrics | None = None,
    # The artifact-bucket reader. Injected by the tests with an in-memory one
    # for the same reason `db` is: the inspection routes must be exercisable
    # with no credentials and no network. `_MISSING` rather than None, because
    # None is a MEANINGFUL value here -- it is how a caller says "this
    # deployment has no artifact store" -- and a default of None would make
    # that indistinguishable from "not supplied".
    objects: ObjectReader | None | object = _MISSING,
    now: Callable[[], Any] = utcnow,
) -> AppContext:
    settings = settings or ApiSettings.from_env()
    db = db if db is not None else build_firestore(settings)
    metrics = metrics or ApiMetrics()
    store = Store(db, now=now)
    # `require_audience` outside local development. google-auth SKIPS the `aud`
    # check entirely when no audience is passed, so an unset API_AUDIENCE means
    # any Google ID token from an allowed domain authenticates -- including one
    # an unrelated third-party SaaS obtained when an employee signed in with
    # Google, which it could then replay here as that employee. Nothing about a
    # service running with the check off looks wrong, so it is refused at start.
    verifier = verifier or GoogleTokenVerifier(
        settings.core.api_audience, require_audience=settings.hardened
    )
    groups = groups or CloudIdentityGroups(
        settings.project_id,
        impersonate_user=settings.groups_impersonate_user,
        ttl_seconds=settings.group_cache_ttl_seconds,
    )
    credentials = credentials or SecretManagerCredentials(settings.project_id)
    waker = waker or (PubSubWaker(settings.dispatch_topic)
                      if settings.dispatch_topic else NullWaker())
    # OFF unless an audience is pinned. A verifier that accepts an assertion
    # without checking which backend minted it would accept one issued to any
    # IAP-protected resource anywhere, so "not configured" must mean "not used"
    # rather than "used without the check".
    iap = IapAssertionVerifier(settings.iap_audiences)
    authenticator = Authenticator(settings, verifier, groups, iap=iap)
    submissions = SubmissionService(
        settings=settings, store=store, waker=waker, metrics=metrics, now=now
    )
    limiter = TokenBucketLimiter(
        rate_per_second=settings.core.requests_per_second,
        burst=settings.rate_limit_burst,
    )
    reader = (
        build_object_reader(
            bucket=settings.core.artifact_bucket, project_id=settings.project_id
        )
        if objects is _MISSING
        else objects
    )
    inspection = InspectionService(
        store=store,
        objects=reader,  # type: ignore[arg-type]
        scan_limit=settings.object_scan_limit,
        max_log_bytes=settings.max_log_bytes,
        default_log_bytes=settings.default_log_bytes,
        min_log_bytes=settings.min_log_bytes,
    )
    rollups = WorkflowRollups(store=store, metrics=metrics)
    return AppContext(
        settings=settings,
        db=db,
        store=store,
        authenticator=authenticator,
        submissions=submissions,
        credentials=credentials,
        limiter=limiter,
        metrics=metrics,
        waker=waker,
        inspection=inspection,
        rollups=rollups,
        now=now,
    )


# --------------------------------------------------------------------------
# FastAPI dependencies
log = logging.getLogger(__name__)


# --------------------------------------------------------------------------

def get_context(request: Request) -> AppContext:
    return request.app.state.ctx


def current_auth(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    # Identity-Aware Proxy puts the authenticated identity here. A browser behind
    # IAP sends NO Authorization header at all -- there is no Google ID token a
    # single-page app can mint -- so for every web caller this is the only
    # credential that arrives.
    iap_assertion: str | None = Header(default=None, alias="X-Goog-IAP-JWT-Assertion"),
    ctx: AppContext = Depends(get_context),
) -> AuthContext:
    """Authenticate, then rate-limit by principal.

    The Authorization header is consumed here and nowhere else. It is never
    placed on `request.state`, never logged and never echoed in an error.

    TWO HEADERS, because Cloud Run may claim one of them. When a service
    requires IAM authentication, the platform validates the caller's token
    itself; depending on configuration it can forward its OWN token in
    `Authorization` and move the caller's original into
    `X-Serverless-Authorization`. An app that reads only `Authorization` then
    sees a token minted for the Cloud Run runtime rather than the human, and
    rejects every request with "id token verification failed" while the same
    token verifies correctly everywhere else.

    So the caller's original is preferred when present. The header NAMES that
    arrived are logged on failure -- never their values, which are full
    impersonation of that caller's tenant.
    """
    presented = serverless_authorization or authorization
    try:
        auth = ctx.authenticator.authenticate(presented, iap_assertion)
    except Exception as exc:
        ctx.metrics.auth_failures.labels(kind=type(exc).__name__).inc()
        log.warning(
            "authentication failed (%s); auth headers present: %s",
            type(exc).__name__,
            ",".join(
                n for n, v in (
                    ("Authorization", authorization),
                    ("X-Serverless-Authorization", serverless_authorization),
                ) if v
            ) or "none",
        )
        raise
    try:
        ctx.limiter.check(auth.principal.email)
    except Exception:
        ctx.metrics.rate_limited.labels(tenant=auth.tenant_id).inc()
        raise
    request.state.tenant_id = auth.tenant_id
    return auth


def tenant_scope(
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> str:
    """The caller's tenant id for a tenant-scoped route, through the guard.

    WHY A ROUTE MUST NOT USE `auth.tenant_id` DIRECTLY. The id is derived from
    the verified identity and can never be supplied by a caller -- that half was
    always true -- but it is not UNIQUE to an identity. The frozen
    `tenant_id_for_group` slugs the local part only, so two registered groups
    can derive one id, and the personal-tenant prefix `u-` is one a group's own
    slug can produce. A route that hands the raw string to the store is then
    filtering by a value two unrelated principals both hold.

    `SubmissionService.scope_for` compares the caller's tenant principal against
    the one the tenant document was created for and refuses a mismatch, exactly
    as `ensure_tenant` does on the submit paths. Declaring this dependency is
    what makes that impossible to forget when a route is added, which is the
    same reason the store owns the tenant filter rather than the routes.
    """
    return ctx.submissions.scope_for(auth)


def admin_auth(
    request: Request, auth: AuthContext = Depends(current_auth)
) -> AuthContext:
    """The dependency every admin route declares.

    It hands `require_admin` the route being called -- method and template,
    from the route FastAPI matched -- because that is what the narrow pool-admin
    capability is decided on (`auth.POOL_ADMIN_ROUTES`). The template, not the
    concrete path: `/v1/admin/limits/runner/{runner_profile}`, so a profile
    name in the URL can never be spelled to look like some other route.

    The template is the path in the router that DECLARES the route, not the
    public URL: an `include_router(prefix=...)` is not part of it on the pinned
    FastAPI (see the comment on `auth.POOL_ADMIN_ROUTES`).

    No matched route (which FastAPI does not produce for a route that declared
    this dependency) means no route to allow, and the full-admin rule applies.
    """
    path = getattr(request.scope.get("route"), "path", None)
    route = (request.method.upper(), path) if path else None
    return require_admin(auth, route)


def paged_limit(ctx: AppContext, requested: int | None) -> int:
    if requested is None:
        return ctx.settings.default_page_size
    if requested < 1:
        raise ValidationFailed("limit must be at least 1")
    return min(requested, ctx.settings.max_page_size)
