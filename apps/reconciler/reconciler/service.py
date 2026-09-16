"""The reconciler as a Cloud Run service on a Cloud Scheduler trigger.

Cloud Scheduler POSTs to `/reconcile` with an OIDC token; Cloud Run's IAM check
(`roles/run.invoker`, granted only to the scheduler's service account) is the
authentication boundary, and this service adds a second, cheap one: when
`RECONCILER_ALLOWED_INVOKERS` is set, the caller's verified email must be in it.
Defence in depth for a service whose job is to terminate other people's work.

Only one pass runs at a time. Two concurrent passes would both see the same
stale lease, both invalidate, both terminate and both release -- and the second
release is the one that decrements a pool below its true active count. The
frozen release function is idempotent per lease, so the damage is bounded, but
serialising is free and removes the question.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response
from swarm_common.config import Settings
from swarm_common.logging_setup import configure_logging

from .backends import Backend, CloudRunBackend, GkeBackend
from .config import ReconcilerConfig
from .logs import build_logger
from .repair import ReconcileReport, Reconciler
from .store import ControlStore

_PASS_LOCK = threading.Lock()


def _firestore_client(settings: Settings) -> Any:
    from google.cloud import firestore

    return firestore.Client(project=settings.project_id, database=settings.firestore_database)


def build_backends(config: ReconcilerConfig, logger: Any) -> list[Backend]:
    backends: list[Backend] = []
    if config.enable_cloud_run:
        backends.append(
            CloudRunBackend(
                config.project_id,
                config.region,
                job_name_prefix=config.job_name_prefix,
                logger=logger,
            )
        )
    if config.enable_gke:
        backends.append(GkeBackend(namespace_prefix=config.namespace_prefix, logger=logger))
    return backends


def build_reconciler(config: ReconcilerConfig, settings: Settings, logger: Any) -> Reconciler:
    db = _firestore_client(settings)
    return Reconciler(
        store=ControlStore(db, logger=logger),
        backends=build_backends(config, logger),
        config=config,
        logger=logger,
    )


def _verify_invoker(authorization: str | None, logger: Any) -> None:
    allowed = [
        entry.strip()
        for entry in os.environ.get("RECONCILER_ALLOWED_INVOKERS", "").split(",")
        if entry.strip()
    ]
    if not allowed:
        return                      # Cloud Run IAM is the only gate, by configuration
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(token, google_requests.Request())
    except Exception as exc:
        logger.warning("rejected an invoker with an unverifiable token", error=str(exc))
        raise HTTPException(status_code=401, detail="token verification failed") from exc
    email = claims.get("email", "")
    if email not in allowed:
        logger.warning("rejected an invoker not on the allowlist", email=email)
        raise HTTPException(status_code=403, detail="caller is not an allowed invoker")


def create_app(reconciler: Reconciler | None = None, logger: Any | None = None) -> FastAPI:
    # FIRST, before anything can log. Nothing configured the root logger, so
    # every record this service produced was discarded -- which is why a
    # failing drain showed only uvicorn access lines and never a reason.
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

    logger = logger or build_logger()
    app = FastAPI(title="swarm-reconciler", version="0.1.0")
    state: dict[str, Any] = {"reconciler": reconciler, "last_report": None}

    def _get_reconciler() -> Reconciler:
        if state["reconciler"] is None:
            settings = Settings.from_env()
            state["reconciler"] = build_reconciler(
                ReconcilerConfig.from_env(settings), settings, logger
            )
        return state["reconciler"]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "swarm-reconciler"}

    @app.get("/readyz")
    def readyz() -> dict[str, Any]:
        last: ReconcileReport | None = state["last_report"]
        return {
            "status": "ok",
            "last_pass_at": last.started_at.isoformat() if last else None,
            "last_pass_findings": last.findings if last else None,
        }

    @app.post("/reconcile")
    def reconcile(
        response: Response, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _verify_invoker(authorization, logger)
        if not _PASS_LOCK.acquire(blocking=False):
            # Cloud Scheduler retries; a concurrent pass is not an error.
            response.status_code = 409
            return {"status": "already_running"}
        try:
            report = _get_reconciler().run_once()
            state["last_report"] = report
            return {"status": "ok", **report.as_dict()}
        finally:
            _PASS_LOCK.release()

    return app


app = create_app()
