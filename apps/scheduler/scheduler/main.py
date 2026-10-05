"""Scheduler HTTP surface: a Pub/Sub push target and a safety tick.

The scheduler is not a daemon. Every entry point runs ONE bounded drain and
returns; nothing here sleeps, polls or waits for capacity.

Pub/Sub push semantics matter for the response codes below. A 2xx acks the
message. We ack even when a drain does no work, because redelivering a message
that says "there might be work" changes nothing -- the queue is in Firestore, not
in Pub/Sub, so a lost nudge costs at most one safety-tick interval of latency.
We return 5xx only when the process could not read Firestore at all, which is
the one case where a retry is genuinely useful.

One wake is more than a nudge: `task_finished`, which the worker publishes when
it ends a task (#636). It names the task, and runs `release_dependants` -- that
task's dependants, then one admission pass -- instead of a full drain. Its 5xx
cases are the same plus one: a drain already holding this process's lock for
longer than `FINISH_LOCK_WAIT_SECONDS`, because acking then could leave the
dependant waiting for the safety tick, which is the delay the event removes.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
from typing import Any

from fastapi import Body, Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool

from swarm_common.models import utcnow

from .credentials import AccountPool
from .dispatch import build_router
from .loop import DrainReport, Scheduler
from .metrics import SchedulerMetrics
from .settings import SchedulerSettings
from .store import SchedulerStore

from swarm_common.logging_setup import configure_logging

log = logging.getLogger(__name__)


class PushAuthError(Exception):
    """The push request did not carry the expected OIDC identity."""


class PushVerifier:
    """Verifies the OIDC token Pub/Sub attaches to a push request.

    Cloud Run IAM already refuses unauthenticated callers; this is the second
    layer that also checks WHICH service account called, so a different
    authorised caller in the project cannot drive the admission controller.

    It is only a second layer if it is actually on. `PUSH_SERVICE_ACCOUNT` unset
    used to leave it silently disabled in every deployed environment, which is
    worse than not having it: the comment above asserts a control a reviewer then
    believes is present. `required` (driven by ENVIRONMENT) makes the process
    refuse to start instead. `PUSH_AUDIENCE` is checked the same way, because
    google-auth SKIPS the `aud` claim entirely when no audience is passed, so
    without it a token the tick service account minted for ANY other audience is
    accepted here.
    """

    def __init__(
        self, service_account: str, audience: str = "", *, required: bool = False
    ) -> None:
        self._service_account = service_account.strip()
        self._audience = audience.strip() or None
        self._request = None
        if required:
            missing = [
                name
                for name, value in (
                    ("PUSH_SERVICE_ACCOUNT", self._service_account),
                    ("PUSH_AUDIENCE", self._audience),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"{' and '.join(missing)} must be set outside local development: "
                    "the admission controller's /pubsub/push and /tick would otherwise "
                    "accept any caller Cloud Run invoker IAM lets through, with no "
                    "check on which service account called or what the token was "
                    "minted for"
                )

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
    # ONE account pool for admission and for the Cloud Run Job's secret mount,
    # so both judge a task on the same list, read once per drain
    # (credentials.py, #169). Building no client: it reads through `db`, and
    # only when a keyless tenant's task needs it.
    pool = AccountPool.for_deployment(settings, db)
    return Scheduler(
        settings=settings,
        store=SchedulerStore(db),
        router=router or build_router(settings, pool=pool),
        metrics=metrics or SchedulerMetrics(),
        pool=pool,
    )


#: The wake reason the worker publishes after a task's terminal write (#636).
TASK_FINISHED = "task_finished"

#: What a task id looks like (`swarm_common.models.new_id`: a prefix and hex).
#: Checked before the id becomes a document path: a wake is a doorbell anyone
#: allowed to publish may ring, so its text is never trusted as a path.
_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

#: How long a finish event waits for a drain already running in this process.
#: Shorter than the subscription's ack deadline, so a busy answer is a 503
#: Pub/Sub redelivers (its retry policy starts at 10 s) rather than a timeout.
FINISH_LOCK_WAIT_SECONDS = 20.0


def finished_task_id(envelope: dict[str, Any]) -> str | None:
    """The task a `task_finished` wake names, or None for any other wake."""
    if envelope.get("reason") != TASK_FINISHED:
        return None
    task_id = envelope.get("task_id")
    if isinstance(task_id, str) and _TASK_ID.match(task_id):
        return task_id
    return None


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
        "task_id": attributes.get("task_id") or payload.get("task_id"),
        "attributes": attributes,
        "message_id": message.get("messageId"),
    }


def create_app(scheduler: Scheduler | None = None) -> FastAPI:
    # FIRST, before anything can log. Nothing configured the root logger, so
    # every record this service produced was discarded -- which is why a
    # failing drain showed only uvicorn access lines and never a reason.
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

    app = FastAPI(title="swarm scheduler", version="0.1.0")
    app.state.scheduler = scheduler if scheduler is not None else build_scheduler()
    environment = os.environ.get("ENVIRONMENT", "dev").strip().lower()
    app.state.verifier = PushVerifier(
        os.environ.get("PUSH_SERVICE_ACCOUNT", ""),
        os.environ.get("PUSH_AUDIENCE", ""),
        required=environment not in {"dev", "test", "local"},
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

    async def _run_finish_event(request: Request, task_id: str) -> tuple[int, dict[str, Any]]:
        """`Scheduler.release_dependants` under the drain lock (#636).

        It WAITS for a running drain, briefly, where a second drain does not:
        a drain that started before this task ended may already be past its
        dependency sweep, so skipping would leave the dependant for the next
        tick -- the delay this path exists to remove. A wait that runs out is
        a 503, which Pub/Sub redelivers.
        """
        lock: threading.Lock = request.app.state.drain_lock
        acquired = await run_in_threadpool(lock.acquire, True, FINISH_LOCK_WAIT_SECONDS)
        if not acquired:
            return 503, {"status": "busy", "reason": TASK_FINISHED}
        try:
            report = await run_in_threadpool(
                request.app.state.scheduler.release_dependants, task_id
            )
        finally:
            lock.release()
        return 200, {"status": "ok", "reason": TASK_FINISHED, "report": report.to_dict()}

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
            task_id = finished_task_id(envelope)
            if task_id is not None:
                status, answer = await _run_finish_event(request, task_id)
                response.status_code = status
                return answer
            return await _run_drain(request, envelope["reason"])
        except Exception as exc:
            # 5xx asks Pub/Sub to redeliver, which is only useful if the failure
            # was transient infrastructure rather than bad input.
            # The exception type only. The message would carry whatever Firestore
            # or a backend API said, which routinely names the resource it was
            # handed; the full text is in the log line above.
            log.exception("drain failed")
            response.status_code = 503
            return {"status": "error", "detail": type(exc).__name__}

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
