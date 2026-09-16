"""Scheduler HTTP surface: a Pub/Sub push target and a safety tick.

The scheduler is not a daemon. Every entry point runs ONE bounded drain and
returns; nothing here sleeps, polls or waits for capacity.

Pub/Sub push semantics matter for the response codes below. A 2xx acks the
message. We ack even when a drain does no work, because redelivering a message
that says "there might be work" changes nothing -- the queue is in Firestore, not
in Pub/Sub, so a lost nudge costs at most one safety-tick interval of latency.
We return 5xx only when the process could not read Firestore at all, which is
the one case where a retry is genuinely useful.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from typing import Any

from fastapi import Body, Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool

from swarm_common.models import utcnow

from .dispatch import build_router
from .loop import DrainReport, Scheduler
from .metrics import SchedulerMetrics
from .settings import SchedulerSettings
from .store import SchedulerStore

log = logging.getLogger(__name__)


class PushAuthError(Exception):
    """The push request did not carry the expected OIDC identity."""


class PushVerifier:
    """Verifies the OIDC token Pub/Sub attaches to a push request.

    Cloud Run IAM already refuses unauthenticated callers; this is the second
    layer that also checks WHICH service account called, so a different
    authorised caller in the project cannot drive the admission controller.
    Disabled when `PUSH_SERVICE_ACCOUNT` is unset.
    """

    def __init__(self, service_account: str, audience: str = "") -> None:
        self._service_account = service_account.strip()
        self._audience = audience.strip() or None
        self._request = None

    @property
    def enabled(self) -> bool:
        return bool(self._service_account)

    def verify(self, authorization: str | None) -> None:
        if not self.enabled:
            return
        if not authorization or not authorization.lower().startswith("bearer "):
            raise PushAuthError("push request carries no bearer token")
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
            raise PushAuthError("push token verification failed") from None
        email = str(claims.get("email", "")).lower()
        if email != self._service_account.lower():
            raise PushAuthError("push token belongs to an unexpected service account")


def build_scheduler(
    *,
    settings: SchedulerSettings | None = None,
    db: Any | None = None,
    router: Any | None = None,
    metrics: SchedulerMetrics | None = None,
) -> Scheduler:
    settings = settings or SchedulerSettings.from_env()
    if db is None:
        from google.cloud import firestore

        # The NAMED database. `(default)` belongs to other teams in this
        # shared project.
        db = firestore.Client(
            project=settings.project_id, database=settings.core.firestore_database
        )
    return Scheduler(
        settings=settings,
        store=SchedulerStore(db),
        router=router or build_router(settings),
        metrics=metrics or SchedulerMetrics(),
    )


def decode_push_envelope(body: dict[str, Any]) -> dict[str, Any]:
    """Pull the payload out of a Pub/Sub push envelope.

    A malformed envelope is not worth retrying, so this never raises: the drain
    runs regardless. The payload only ever carries a hint about WHY we were
    woken; the actual work is whatever is READY in Firestore.
    """
    message = body.get("message") or {}
    attributes = dict(message.get("attributes") or {})
    raw = message.get("data")
    payload: dict[str, Any] = {}
    if raw:
        try:
            payload = json.loads(base64.b64decode(raw).decode("utf-8"))
        except Exception:
            payload = {}
    return {
        "reason": payload.get("reason") or attributes.get("reason") or "pubsub",
        "attributes": attributes,
        "message_id": message.get("messageId"),
    }


def create_app(scheduler: Scheduler | None = None) -> FastAPI:
    app = FastAPI(title="swarm scheduler", version="0.1.0")
    app.state.scheduler = scheduler if scheduler is not None else build_scheduler()
    app.state.verifier = PushVerifier(
        os.environ.get("PUSH_SERVICE_ACCOUNT", ""),
        os.environ.get("PUSH_AUDIENCE", ""),
    )
    # One drain at a time per process. Two overlapping drains would not corrupt
    # anything -- admission is transactional -- but they would waste reads
    # racing for the same slice of READY work.
    app.state.drain_lock = threading.Lock()
    app.state.last_report = None

    def scheduler_of(request: Request) -> Scheduler:
        return request.app.state.scheduler

    async def _run_drain(request: Request, reason: str) -> dict[str, Any]:
        lock: threading.Lock = request.app.state.drain_lock
        if not lock.acquire(blocking=False):
            return {"status": "already_draining", "reason": reason}
        try:
            report = await run_in_threadpool(request.app.state.scheduler.drain)
        finally:
            lock.release()
        request.app.state.last_report = report
        return {"status": "ok", "reason": reason, "report": report.to_dict()}

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "at": utcnow()}

    @app.get("/readyz")
    def readyz(response: Response, request: Request) -> dict[str, Any]:
        try:
            paused = request.app.state.scheduler.store.dispatch_paused()
        except Exception as exc:  # pragma: no cover - depends on live Firestore
            response.status_code = 503
            return {"status": "not-ready", "detail": type(exc).__name__}
        return {"status": "ready", "dispatch_paused": paused}

    @app.get("/metrics")
    def metrics(request: Request) -> Response:
        body, content_type = request.app.state.scheduler.metrics.render()
        return Response(content=body, media_type=content_type)

    @app.post("/pubsub/push")
    async def pubsub_push(
        request: Request,
        response: Response,
        body: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        try:
            request.app.state.verifier.verify(request.headers.get("Authorization"))
        except PushAuthError as exc:
            response.status_code = 401
            return {"status": "unauthenticated", "detail": str(exc)}
        envelope = decode_push_envelope(body)
        try:
            return await _run_drain(request, envelope["reason"])
        except Exception as exc:
            # 5xx asks Pub/Sub to redeliver, which is only useful if the failure
            # was transient infrastructure rather than bad input.
            log.exception("drain failed")
            response.status_code = 503
            return {"status": "error", "detail": type(exc).__name__, "message": str(exc)[:500]}

    @app.post("/tick")
    async def tick(request: Request, response: Response) -> dict[str, Any]:
        """Cloud Scheduler's 1-minute safety tick."""
        try:
            request.app.state.verifier.verify(request.headers.get("Authorization"))
        except PushAuthError as exc:
            response.status_code = 401
            return {"status": "unauthenticated", "detail": str(exc)}
        try:
            return await _run_drain(request, "safety_tick")
        except Exception as exc:
            log.exception("tick drain failed")
            response.status_code = 503
            return {"status": "error", "detail": type(exc).__name__}

    @app.get("/last-run")
    def last_run(request: Request, _: Scheduler = Depends(scheduler_of)) -> dict[str, Any]:
        report = request.app.state.last_report
        return {"report": report.to_dict() if report else None}

    return app


def main() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    uvicorn.run(
        "scheduler.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
