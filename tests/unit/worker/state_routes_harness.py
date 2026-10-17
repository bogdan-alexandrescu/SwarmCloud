"""The worker's state client against swarm-api's REAL state routes, in process.

Lane ISO-WORKER-CORE (docs/design/tenant-settings-integrity.md §6): "a
`RemoteControlPlane` passes the existing `ControlPlane` method tests through a
fake transport". The transport here is fake only at the socket: the worker's
own `children.ChildApi` builds each `urllib` request -- its headers, its body,
its error parsing -- and an opener hands it to swarm-api's app through
`TestClient`, over the control-plane suite's in-memory Firestore. So the
request and response shapes are swarm-api's own (lane ISO-STATE-API), not a
restatement that could drift from them.

A module of its own name, not `conftest`: see test_helpers_import_by_unique_name.py.
"""

from __future__ import annotations

import io
import secrets
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient

from agent_worker.children import ChildApi
from agent_worker.control import RemoteControlPlane
from agent_worker.hardening import PROTECTED
from agent_worker.logs import build_logger
from agent_worker.statecall import StateCall
from control_plane.conftest import PROJECT, api_settings, seed_pool, seed_task, seed_tenant
from control_plane.fakes import FakeFirestore as ApiFirestore
from swarm_api.auth import StaticTokenVerifier
from swarm_api.childkey import AttemptTuple, nonce
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.waker import NullWaker

#: Built at runtime, never one literal (CLAUDE.md: nothing added may look like
#: a credential).
CHILD_KEY = "test-" + secrets.token_hex(16)
WORKER_TOKEN = "tok-worker-eng-" + secrets.token_hex(4)
OTHER_TOKEN = "tok-worker-research-" + secrets.token_hex(4)
BASE = "https://swarm-api.test"

TENANT = "eng"
TASK = "task_remote_1"
ATTEMPT = "att_remote_1"
LEASE = "lease_remote_1"
POOLS = ("global", f"tenant:{TENANT}")


class _Answer:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Answer":
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None


class RoutesOpener:
    """`urllib.request.urlopen`, answered by swarm-api's app.

    `calls` is every request the worker sent, as `(method, route)`: the
    route's last path segment, so a test can read the ORDER of state calls.
    `fail_next` answers the next N requests with a 503, as an outage would.
    """

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.calls: list[tuple[str, str]] = []
        self.headers: list[dict[str, str]] = []
        self.fail_next = 0

    def __call__(self, request: Any, timeout: float | None = None) -> _Answer:
        url = request.full_url
        assert url.startswith(BASE), url
        path = url[len(BASE):]
        route = path.split("?", 1)[0].rsplit("/", 1)[-1]
        self.calls.append((request.get_method(), route))
        headers = dict(request.header_items())
        self.headers.append(headers)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise urllib.error.HTTPError(url, 503, "unavailable", None,
                                         io.BytesIO(b'{"code":"state_unavailable"}'))
        response = self.client.request(request.get_method(), path, content=request.data,
                                       headers=headers)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(url, response.status_code, "refused", None,
                                         io.BytesIO(response.content))
        return _Answer(response.status_code, response.content)

    def routes(self) -> list[str]:
        return [route for _method, route in self.calls]


def api_client(db: ApiFirestore) -> TestClient:
    tokens = {
        WORKER_TOKEN: {"email": f"swarm-agent-worker-{TENANT}@{PROJECT}.iam.gserviceaccount.com",
                       "email_verified": True, "sub": "1000000000000000000001"},
        OTHER_TOKEN: {"email": f"swarm-agent-worker-research@{PROJECT}.iam.gserviceaccount.com",
                      "email_verified": True, "sub": "1000000000000000000002"},
    }
    ctx = build_context(
        settings=api_settings(child_key=CHILD_KEY),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups({}),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def seed_attempt(
    db: ApiFirestore,
    *,
    task_id: str = TASK,
    attempt_id: str = ATTEMPT,
    lease_id: str = LEASE,
    generation: int = 1,
    state: str = "LEASED",
    **extra: Any,
) -> dict[str, Any]:
    """A task under this attempt's live lease, holding one unit of each pool."""
    seed_tenant(db, TENANT)
    now = datetime.now(timezone.utc)
    for name in POOLS:
        if f"pools/{name}" not in db.docs:
            seed_pool(db, name, hard_limit=10, active=0)
        db.docs[f"pools/{name}"]["active"] += 1
    doc = seed_task(db, task_id=task_id, tenant_id=TENANT, state=state)
    doc.update({"current_lease_id": lease_id, "current_generation": generation,
                "submitted_by": "alice@saga.xyz", "attempt_count": 1, "max_attempts": 3,
                **extra})
    db.docs[f"leases/{lease_id}"] = {
        "lease_id": lease_id, "task_id": task_id, "attempt_id": attempt_id,
        "tenant_id": TENANT, "generation": generation, "pools": list(POOLS),
        "units": 1, "state": state, "created_at": now, "heartbeat_at": None,
        "dispatch_deadline": now + timedelta(minutes=10),
        "expires_at": now + timedelta(minutes=2), "released_at": None,
    }
    return doc


class Recorder:
    """A finish announcer that records what it was asked to announce."""

    def __init__(self) -> None:
        self.announced: list[Any] = []

    def announce(self, *, task_id: str, tenant_id: str, state: Any) -> bool:
        self.announced.append((task_id, tenant_id, state))
        return True


def remote_control(
    opener: RoutesOpener,
    *,
    task_id: str = TASK,
    attempt_id: str = ATTEMPT,
    lease_id: str = LEASE,
    generation: int = 1,
    token: str = WORKER_TOKEN,
    memory_status: str = PROTECTED,
    stream: io.StringIO | None = None,
    finish_announcer: Any = None,
    sleeps: list[float] | None = None,
    quota_reporter: Any = None,
) -> RemoteControlPlane:
    """A `RemoteControlPlane` for one attempt, as `__main__.build_worker`
    builds it, its transport the worker's own `ChildApi` over `opener`."""
    api = ChildApi(BASE, token_fetcher=lambda _audience: token, opener=opener)
    # A clock the backoff advances, so a test of an outage waits out no budget.
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds
        if sleeps is not None:
            sleeps.append(seconds)

    calls = StateCall(
        api, tenant_id=TENANT, task_id=task_id, attempt_id=attempt_id, lease_id=lease_id,
        generation=generation, sleep=sleep, clock=lambda: now[0],
    )
    logger = build_logger(task_id=task_id, attempt_id=attempt_id, tenant_id=TENANT,
                          generation=generation, runner_profile="mock",
                          stream=stream or io.StringIO())
    return RemoteControlPlane(
        calls,
        nonce=nonce(CHILD_KEY, AttemptTuple(TENANT, task_id, attempt_id, lease_id, generation)),
        memory_status=memory_status,
        logger=logger,
        finish_announcer=finish_announcer,
        quota_reporter=quota_reporter,
    )


def events(db: ApiFirestore, task_id: str = TASK, kind: str | None = None) -> list[dict[str, Any]]:
    prefix = f"tasks/{task_id}/events/"
    return [d for p, d in db.docs.items()
            if p.startswith(prefix) and p.count("/") == 3
            and (kind is None or d.get("type") == kind)]
