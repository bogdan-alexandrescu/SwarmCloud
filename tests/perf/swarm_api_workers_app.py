"""swarm-api over its unit-test fakes, as a uvicorn factory, for the worker measurement.

`scripts/measure-api-workers.sh` starts this under `uvicorn --factory
--workers N` to compare one worker with two on the same instance size. Every
collaborator is the one `tests/unit/control_plane/conftest.py` injects: an
in-memory Firestore, a static token verifier, static groups, no forge
credential, no Secret Manager. Nothing here reaches a cloud API, so the run
needs no credentials and no emulator.

Each worker process calls `create_app()` itself, so each holds its OWN
in-memory Firestore. The seed is deterministic, so every worker serves the same
documents and a read answers the same whichever worker takes it. What it does
not model is Firestore's network round trip: these numbers are the API's own
CPU per read, the part a second worker can parallelise.

The bearer token is not written anywhere: the script draws it at run time
(`secrets.token_hex`) and passes it in `SWARM_PERF_TOKEN`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app as create_api_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.waker import NullWaker

from control_plane.conftest import (
    ENG_GROUP,
    PROJECT,
    RENDERED_DENY_LIST,
    NoForgeTokens,
    OfflineSecretMetadata,
    api_settings,
    seed_task,
    seed_tenant,
)
from control_plane.fakes import FakeFirestore

#: The person every request is made as: a member of the eng group, so the
#: tenant-scoped reads have a tenant and its tasks to read.
PERSON = "alice@example.com"
TENANT = "eng"
#: Enough tasks that a list read pages, sorts and serialises real rows: the
#: console's Overview reads the newest page of a tenant's tasks.
SEEDED_TASKS = int(os.environ.get("SWARM_PERF_TASKS", "300"))
STATES = ("QUEUED", "READY", "LEASED", "RUNNING", "PARKED", "SUCCEEDED", "FAILED", "CANCELLED")


def seeded_db() -> FakeFirestore:
    db = FakeFirestore()
    seed_tenant(db, TENANT, max_active=20)
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    for n in range(SEEDED_TASKS):
        seed_task(db, task_id=f"task_{n:05d}", tenant_id=TENANT, state=STATES[n % len(STATES)],
                  created_at=start + timedelta(minutes=n))
    return db


def create_app() -> FastAPI:
    token = os.environ["SWARM_PERF_TOKEN"]
    os.environ.setdefault("SWARM_SHARED_DENY_LIST", json.dumps(list(RENDERED_DENY_LIST)))
    os.environ.setdefault("SWARM_SHARED_DENY_LIST_SET", "1")
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    groups = {PERSON: (ENG_GROUP,)}
    claims = {"email": PERSON, "email_verified": True, "sub": f"sub-{PERSON}", "hd": "example.com"}
    ctx = build_context(
        # The measurement compares throughput, so the per-principal limiter
        # must not be what caps it: one person drives every request here.
        settings=api_settings(rate_limit_burst=10**9, core_overrides={"requests_per_second": 10**9}),
        db=seeded_db(),
        verifier=StaticTokenVerifier({token: claims}),
        groups=StaticGroups(groups),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        forge_tokens=NoForgeTokens(),
    )
    app = create_api_app(ctx)
    app.state.secret_metadata = OfflineSecretMetadata()
    return app
