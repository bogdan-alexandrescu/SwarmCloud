"""The live browser, swarm-api's half (docs/design/live-browser.md §3.3, §4.2, §5.1, §5.2, §5.8).

Lane LB-L9. Drives the real routes over the in-memory Firestore. Each test
names the acceptance rule of the build plan it holds (§9, row L9):

  * another tenant's member gets 404 -- on every route and on the socket;
  * a non-submitter can view, and cannot take control until the submitter
    opened it to the tenant;
  * one controller at a time, and preemption by the submitter only;
  * the audit entry is written in the control change's own transaction;
  * membership is re-checked: on the open socket, and freshly on a take;
  * no ticket in a URL.

The relay (lane L7) is a test double here (`FakeRelay`), never code in apps/:
the socket route takes its relay connector from `app.state.live_relay`.

Every key, token and ticket below is built at runtime: nothing here is a
credential.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from swarm_api.auth import StaticTokenVerifier
from swarm_api.childkey import AttemptTuple, nonce
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import CloudIdentityGroups
from swarm_api.handoff import LIVE_AUDIT
from swarm_api.live import (
    LIVE_SESSIONS,
    ROLE_TUNNEL,
    ROLE_VIEWER,
    TicketRefused,
    live_session_message,
    mint_ticket,
    session_id_for,
    verify_ticket,
)
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ENG_GROUP, PROJECT, RESEARCH_GROUP, api_settings, seed_task, seed_tenant
from .fakes import FakeFirestore, FakeTransaction

#: Built at runtime, never one literal (CLAUDE.md).
CHILD_KEY = "-".join(("live", "child", "key")) + "-" + "c" * 24
LIVE_KEY = "-".join(("live", "relay", "key")) + "-" + "r" * 24
WORKER_TOKEN = "-".join(("token", "worker", "eng"))
TASK = "task_live"
ATTEMPT = "att_live_1"
LEASE = "lease_live_1"
PROFILE = "claude-code-browser"
RELAY_URL = "ws://swarm-live.swarm-system.svc:8443/v1/viewer"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def bearer(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {'-'.join(('token', user))}"}


class Clock:
    """The service's datetime clock, moved by a test."""

    def __init__(self) -> None:
        self.at = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at = self.at + timedelta(seconds=seconds)


class MutableGroups:
    """Group membership a test can change while a socket is open, with a
    cache it serves until `forget` is called -- the shape of
    `CloudIdentityGroups` that a fresh check must get past."""

    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        self.mapping = {k.lower(): tuple(v) for k, v in mapping.items()}
        self.cached: dict[tuple[str, str], bool] = {}
        self.forgotten: list[tuple[str, str]] = []
        self.caching = False

    def groups_for(self, member_email: str, candidate_groups: tuple[str, ...]) -> tuple[str, ...]:
        out = []
        for group in candidate_groups:
            key = (member_email.lower(), group.lower())
            if self.caching and key in self.cached:
                held = self.cached[key]
            else:
                held = group.lower() in {g.lower() for g in self.mapping.get(key[0], ())}
                self.cached[key] = held
            if held:
                out.append(group)
        return tuple(out)

    def forget(self, member_email: str, group_email: str) -> None:
        key = (member_email.lower(), group_email.lower())
        self.forgotten.append(key)
        self.cached.pop(key, None)


class CommitLog(FakeFirestore):
    """Records what each committed transaction wrote, as (op, path) lists."""

    def __init__(self) -> None:
        super().__init__()
        self.commits: list[list[tuple[str, str]]] = []
        self.fail_audit = False

    def transaction(self, **kwargs: Any) -> FakeTransaction:
        log = self

        class _Txn(FakeTransaction):
            def set(self, ref, data, merge=False):  # type: ignore[override]
                if log.fail_audit and ref.path.startswith(f"{LIVE_AUDIT}/"):
                    raise RuntimeError("the audit write failed")
                super().set(ref, data, merge=merge)

            def _commit(self):  # type: ignore[override]
                log.commits.append([(op, ref.path) for op, ref, _ in self._buffer])
                return super()._commit()

        return _Txn(self)


# --------------------------------------------------------------------------
# The relay, as a test double (lane L7 is the real one)
# --------------------------------------------------------------------------


class FakeLink:
    def __init__(self, url: str, frames: list[Any]) -> None:
        self.url = url
        self.sent: list[Any] = []
        self.closed = False
        self._frames = list(frames)
        self._done: asyncio.Event | None = None

    async def send(self, data: str | bytes) -> None:
        self.sent.append(data)

    async def recv(self) -> str | bytes | None:
        if self._frames:
            return self._frames.pop(0)
        if self._done is None:
            self._done = asyncio.Event()
        await self._done.wait()
        return None

    async def close(self) -> None:
        self.closed = True
        if self._done is not None:
            self._done.set()

    def texts(self) -> list[dict[str, Any]]:
        return [json.loads(m) for m in self.sent if isinstance(m, str)]


class FakeRelay:
    def __init__(self, frames: list[Any] | None = None) -> None:
        self.links: list[FakeLink] = []
        self._frames = frames or []

    async def connect(self, url: str) -> FakeLink:
        link = FakeLink(url, self._frames)
        self.links.append(link)
        return link


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def people() -> dict[str, tuple[str, ...]]:
    """alice submits; root and dave are other members of eng; bob is research."""
    return {
        "alice@saga.xyz": (ENG_GROUP,),
        "bob@saga.xyz": (RESEARCH_GROUP,),
        "carol@saga.xyz": (),
        "dave@saga.xyz": (ENG_GROUP,),
        "root@saga.xyz": (ENG_GROUP,),
    }


@pytest.fixture
def groups(people) -> MutableGroups:
    return MutableGroups(people)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def ldb() -> CommitLog:
    return CommitLog()


@pytest.fixture
def relay() -> FakeRelay:
    return FakeRelay()


def _tokens(people) -> dict[str, dict[str, Any]]:
    tokens = {
        "-".join(("token", email.split("@")[0])): {
            "email": email,
            "email_verified": True,
            "sub": f"sub-{email}",
            "hd": "saga.xyz",
        }
        for email in people
    }
    tokens[WORKER_TOKEN] = {
        "email": f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        "email_verified": True,
        "sub": "1000000000000000000001",
    }
    return tokens


def make_client(db, people, groups, objects, clock, relay, **settings: Any) -> TestClient:
    base = {
        "child_key": CHILD_KEY,
        "live_key": LIVE_KEY,
        "live_relay_url": RELAY_URL,
        "live_session_poll_seconds": 0.02,
        "live_membership_recheck_seconds": 0.05,
        "live_input_flush_seconds": 0.02,
    }
    base.update(settings)
    ctx = build_context(
        settings=api_settings(**base),
        db=db,
        verifier=StaticTokenVerifier(_tokens(people)),
        groups=groups,
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        now=clock,
    )
    app = create_app(ctx)
    app.state.live_relay = relay
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(ldb, people, groups, objects, clock, relay) -> TestClient:
    return make_client(ldb, people, groups, objects, clock, relay)


class Attempt:
    def __init__(self, generation: int = 1) -> None:
        self.tuple = AttemptTuple("eng", TASK, ATTEMPT, LEASE, generation)
        self.private = Ed25519PrivateKey.generate()
        self.public = _b64url(
            self.private.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )

    def body(self, **over: Any) -> bytes:
        t = self.tuple
        return json.dumps(
            {"tenant_id": t.tenant_id, "task_id": t.task_id, "attempt_id": t.attempt_id,
             "lease_id": t.lease_id, "generation": t.generation, **over}
        ).encode()

    def sign(self, path: str, body: bytes) -> dict[str, str]:
        stamp = str(int(time.time()))
        sig = self.private.sign(live_session_message("POST", path, body, stamp))
        return {"X-Swarm-Attempt-Proof": _b64url(sig), "X-Swarm-Attempt-Timestamp": stamp}


def seed_running(client, db, attempt: Attempt, *, profile: str = PROFILE,
                 backend: str = "GKE_AUTOPILOT", **extra: Any) -> dict[str, Any]:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    t = attempt.tuple
    now = datetime.now(timezone.utc)
    doc = seed_task(db, task_id=t.task_id, tenant_id="eng", state="STARTING",
                    runner_profile=profile, resource_class="browser")
    doc.update({
        "current_lease_id": t.lease_id,
        "current_generation": t.generation,
        "submitted_by": "alice@saga.xyz",
        "timeout_seconds": 3600,
        "attempt_count": 1,
        "started_at": now,
        **extra,
    })
    db.docs[f"leases/{t.lease_id}"] = {
        "lease_id": t.lease_id, "task_id": t.task_id, "attempt_id": t.attempt_id,
        "tenant_id": "eng", "generation": t.generation, "pools": [], "units": 2,
        "state": "STARTING", "created_at": now,
        "dispatch_deadline": now + timedelta(minutes=10),
        "expires_at": now + timedelta(hours=2), "released_at": None,
    }
    db.docs[f"attempts/{t.attempt_id}"] = {
        "attempt_id": t.attempt_id, "task_id": t.task_id, "tenant_id": "eng",
        "generation": t.generation, "lease_id": t.lease_id, "backend": backend,
        "created_at": now,
    }
    response = client.post(
        f"/v1/attempts/{t.attempt_id}/child-key",
        content=json.dumps({
            "tenant_id": "eng", "task_id": t.task_id, "lease_id": t.lease_id,
            "generation": t.generation, "nonce": nonce(CHILD_KEY, t),
            "public_key": attempt.public,
        }).encode(),
        headers={"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"},
    )
    assert response.status_code == 201, response.text
    doc["state"] = "RUNNING"
    db.docs[f"leases/{t.lease_id}"]["state"] = "RUNNING"
    return doc


def open_session(client, attempt: Attempt, *, sign: bool = True, **over: Any):
    path = f"/v1/attempts/{attempt.tuple.attempt_id}/live-session"
    body = attempt.body(**over)
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"}
    if sign:
        headers.update(attempt.sign(path, body))
    return client.post(path, content=body, headers=headers)


@pytest.fixture
def live(client, ldb) -> dict[str, Any]:
    """A RUNNING claude-code-browser task with an open live session."""
    attempt = Attempt()
    seed_running(client, ldb, attempt)
    response = open_session(client, attempt)
    assert response.status_code == 201, response.text
    return {"attempt": attempt, "session": response.json()}


def audit(db, entry: str | None = None) -> list[dict[str, Any]]:
    rows = [d for p, d in db.docs.items() if p.startswith(f"{LIVE_AUDIT}/")]
    return [r for r in rows if entry is None or r["entry"] == entry]


def session_doc(db) -> dict[str, Any]:
    return db.docs[f"{LIVE_SESSIONS}/{session_id_for(ATTEMPT)}"]


def take(client, user: str, **body: Any):
    return client.post(f"/v1/tasks/{TASK}/live/control", json=body, headers=bearer(user))


def ticket_for(client, user: str) -> str:
    response = client.post(f"/v1/tasks/{TASK}/live/ticket", headers=bearer(user))
    assert response.status_code == 200, response.text
    return response.json()["ticket"]


def wait_until(predicate, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------
# The worker's tunnel ticket (§3.3 step 1)
# --------------------------------------------------------------------------


def test_the_worker_gets_a_one_minute_tunnel_ticket_bound_to_its_attempt(client, ldb):
    attempt = Attempt()
    seed_running(client, ldb, attempt)
    response = open_session(client, attempt)
    assert response.status_code == 201, response.text
    body = response.json()
    claims = verify_ticket((LIVE_KEY,), body["ticket"], now=time.time())
    assert claims["role"] == ROLE_TUNNEL
    assert (claims["tenant_id"], claims["task_id"], claims["attempt_id"], claims["generation"]) == (
        "eng", TASK, ATTEMPT, 1)
    assert claims["session_id"] == body["session_id"] == session_id_for(ATTEMPT)
    assert 0 < claims["exp"] - time.time() <= 60
    assert session_doc(ldb)["state"] == "open"
    assert [r["entry"] for r in audit(ldb)] == ["session_opened"]
    # The ticket is never stored: only its id is.
    assert body["ticket"] not in json.dumps(ldb.docs, default=str)
    # A second call (the tunnel dropped) re-tickets the same session.
    again = open_session(client, attempt)
    assert again.status_code == 200 and again.json()["session_id"] == body["session_id"]
    assert len(audit(ldb, "session_opened")) == 1


def test_the_tunnel_route_refuses_an_unsigned_or_stale_worker(client, ldb):
    attempt = Attempt()
    seed_running(client, ldb, attempt)
    assert open_session(client, attempt, sign=False).status_code == 403
    stale = open_session(client, attempt, generation=2)
    assert stale.status_code in (403, 409)
    ldb.docs[f"tasks/{TASK}"]["current_generation"] = 2
    assert open_session(client, attempt).status_code == 409
    assert not [p for p in ldb.docs if p.startswith(f"{LIVE_SESSIONS}/")]


def test_the_tunnel_route_refuses_a_profile_that_is_not_live_or_not_on_gke(client, ldb):
    attempt = Attempt()
    seed_running(client, ldb, attempt, profile="mock")
    response = open_session(client, attempt)
    assert response.status_code == 409
    assert response.json()["code"] == "live_not_available"
    ldb.docs[f"tasks/{TASK}"]["runner_profile"] = PROFILE
    ldb.docs[f"attempts/{ATTEMPT}"]["backend"] = "CLOUD_RUN_JOB"
    assert open_session(client, attempt).status_code == 409


def test_no_live_key_means_no_live_path(ldb, people, groups, objects, clock, relay):
    client = make_client(ldb, people, groups, objects, clock, relay, live_key="")
    attempt = Attempt()
    seed_running(client, ldb, attempt)
    response = open_session(client, attempt)
    assert response.status_code == 503
    assert response.json()["code"] == "live_unavailable"


# --------------------------------------------------------------------------
# Another tenant's member gets 404 (§5.2 step 2)
# --------------------------------------------------------------------------


def test_another_tenants_member_gets_404_on_every_route(client, ldb, live):
    for method, path in (
        ("GET", f"/v1/tasks/{TASK}/live/session"),
        ("POST", f"/v1/tasks/{TASK}/live/ticket"),
        ("POST", f"/v1/tasks/{TASK}/live/control"),
        ("POST", f"/v1/tasks/{TASK}/live/control/renew"),
        ("POST", f"/v1/tasks/{TASK}/live/control/return"),
        ("POST", f"/v1/tasks/{TASK}/live/open-to-members"),
        ("GET", f"/v1/tasks/{TASK}/live/audit"),
    ):
        response = client.request(method, path, headers=bearer("bob"), json={})
        assert response.status_code == 404, (path, response.text)
        # The same answer as a task that does not exist.
        missing = client.request(method, path.replace(TASK, "task_nope"),
                                 headers=bearer("alice"), json={})
        assert missing.status_code == 404, path
    assert session_doc(ldb)["controller"] is None


def test_another_tenants_member_is_refused_on_the_socket_even_with_a_valid_ticket(
    client, ldb, live, relay
):
    claims = verify_ticket((LIVE_KEY,), ticket_for(client, "alice"), now=time.time())
    forged = mint_ticket(LIVE_KEY, {**claims, "email": "bob@saga.xyz",
                                    "exp": int(time.time()) + 30})
    with client.websocket_connect(f"/v1/tasks/{TASK}/live", headers=bearer("bob")) as ws:
        ws.send_text(json.dumps({"ticket": forged}))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4404
    assert relay.links == []


# --------------------------------------------------------------------------
# A non-submitter can view, and cannot take until opened (§5.1, Q1)
# --------------------------------------------------------------------------


def test_a_non_submitter_can_view_and_cannot_take_until_opened(client, ldb, live):
    view = client.get(f"/v1/tasks/{TASK}/live/session", headers=bearer("root"))
    assert view.status_code == 200, view.text
    assert view.json()["you"] == {
        "may_view": True, "may_take": False, "may_preempt": False,
        "may_open_to_members": False, "in_control": False,
    }
    assert client.post(f"/v1/tasks/{TASK}/live/ticket", headers=bearer("root")).status_code == 200

    # Opening is a hand-off matter (Q6: an unrequested take is the
    # submitter's only), so the agent asks for help first.
    ldb.docs[f"tasks/{TASK}"]["human_wait"] = {
        "reason": "Sign in", "expect": "github.com", "requested_at": clock_now(),
        "deadline": clock_now() + timedelta(minutes=10), "controller": None,
        "request_id": "signin-1", "attempt_id": ATTEMPT, "generation": 1,
    }
    refused = take(client, "root")
    assert refused.status_code == 403
    assert refused.json()["code"] == "control_not_eligible"
    # Only the submitter may open it.
    assert client.post(f"/v1/tasks/{TASK}/live/open-to-members",
                       headers=bearer("root")).status_code == 403
    opened = client.post(f"/v1/tasks/{TASK}/live/open-to-members", headers=bearer("alice"))
    assert opened.status_code == 200, opened.text
    assert [r["email"] for r in audit(ldb, "control_opened_to_members")] == ["alice@saga.xyz"]
    taken = take(client, "root")
    assert taken.status_code == 200, taken.text
    assert session_doc(ldb)["controller"]["email"] == "root@saga.xyz"
    assert ldb.docs[f"tasks/{TASK}"]["human_wait"]["controller"]["email"] == "root@saga.xyz"


def test_an_unrequested_take_is_the_submitters_only_even_when_opened(client, ldb, live):
    assert client.post(f"/v1/tasks/{TASK}/live/open-to-members",
                       headers=bearer("alice")).status_code == 200
    assert take(client, "root").status_code == 403
    assert take(client, "alice").status_code == 200


def clock_now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# One controller at a time; preemption by the submitter only (§4.2, §5.1)
# --------------------------------------------------------------------------


def _ask_for_help(db) -> None:
    db.docs[f"tasks/{TASK}"]["human_wait"] = {
        "reason": "Sign in", "expect": "github.com", "requested_at": clock_now(),
        "deadline": clock_now() + timedelta(minutes=10), "controller": None,
        "request_id": "signin-1", "attempt_id": ATTEMPT, "generation": 1,
    }


def test_one_controller_at_a_time_and_only_the_submitter_preempts(client, ldb, live):
    _ask_for_help(ldb)
    assert client.post(f"/v1/tasks/{TASK}/live/open-to-members",
                       headers=bearer("alice")).status_code == 200
    assert take(client, "root").status_code == 200

    held = take(client, "dave")
    assert held.status_code == 409
    assert held.json()["code"] == "control_held"
    # A member who is not the submitter cannot preempt, even asking to.
    assert take(client, "dave", preempt=True).status_code == 409
    # The submitter is told who holds it, and preempts only when asked to.
    assert take(client, "alice").status_code == 409
    assert session_doc(ldb)["controller"]["email"] == "root@saga.xyz"
    preempted = take(client, "alice", preempt=True)
    assert preempted.status_code == 200, preempted.text
    assert session_doc(ldb)["controller"]["email"] == "alice@saga.xyz"
    ended = audit(ldb, "control_ended")
    assert [(r["email"], r["detail"]["cause"]) for r in ended] == [("root@saga.xyz", "preempted")]
    # And root, preempted, cannot take it back from the submitter.
    assert take(client, "root", preempt=True).status_code == 409


def test_return_resolves_the_hand_off_and_only_the_controller_returns(client, ldb, live):
    _ask_for_help(ldb)
    assert take(client, "alice").status_code == 200
    assert client.post(f"/v1/tasks/{TASK}/live/control/return",
                       headers=bearer("root")).status_code == 409
    returned = client.post(f"/v1/tasks/{TASK}/live/control/return", headers=bearer("alice"))
    assert returned.status_code == 200, returned.text
    assert session_doc(ldb)["controller"] is None
    assert ldb.docs[f"tasks/{TASK}"]["human_wait"] is None
    entries = [r["entry"] for r in sorted(audit(ldb), key=lambda r: r["at"])]
    assert entries[-2:] in (["control_returned", "handoff_resolved"],
                            ["handoff_resolved", "control_returned"])
    events = [d["type"] for p, d in ldb.docs.items() if p.startswith(f"tasks/{TASK}/events/")]
    assert "human_control_taken" in events and "human_control_returned" in events
    assert "human_handoff_resolved" in events


def test_control_lapses_without_renewal_and_when_idle(client, ldb, live, clock):
    assert take(client, "alice").status_code == 200
    clock.advance(61)
    # A renewal after the lapse is refused, and the end is audited with its cause.
    assert client.post(f"/v1/tasks/{TASK}/live/control/renew",
                       headers=bearer("alice")).status_code == 409
    assert [r["detail"]["cause"] for r in audit(ldb, "control_ended")] == ["renew_lapsed"]
    assert take(client, "alice").status_code == 200
    for _ in range(6):
        clock.advance(50)
        assert client.post(f"/v1/tasks/{TASK}/live/control/renew",
                           headers=bearer("alice")).status_code == 200
    clock.advance(50)
    assert client.post(f"/v1/tasks/{TASK}/live/control/renew",
                       headers=bearer("alice")).status_code == 409
    assert [r["detail"]["cause"] for r in audit(ldb, "control_ended")] == [
        "renew_lapsed", "idle"]


def test_a_cancel_closes_the_session_and_ends_control(client, ldb, live):
    assert take(client, "alice").status_code == 200
    ldb.docs[f"tasks/{TASK}"]["cancel_requested"] = True
    view = client.get(f"/v1/tasks/{TASK}/live/session", headers=bearer("alice"))
    assert view.status_code == 200
    assert view.json()["state"] == "closed" and view.json()["close_cause"] == "cancelled"
    assert [r["detail"]["cause"] for r in audit(ldb, "control_ended")] == ["cancelled"]
    assert len(audit(ldb, "session_closed")) == 1
    assert client.post(f"/v1/tasks/{TASK}/live/ticket", headers=bearer("alice")).status_code == 409


# --------------------------------------------------------------------------
# The audit is in the control change's transaction (§5.8)
# --------------------------------------------------------------------------


def test_the_audit_entry_is_written_in_the_control_changes_transaction(client, ldb, live):
    session_path = f"{LIVE_SESSIONS}/{session_id_for(ATTEMPT)}"
    ldb.commits.clear()
    assert take(client, "alice").status_code == 200
    taking = [c for c in ldb.commits if ("update", session_path) in c]
    assert len(taking) == 1
    assert any(op == "set" and path.startswith(f"{LIVE_AUDIT}/") for op, path in taking[0])

    ldb.commits.clear()
    assert client.post(f"/v1/tasks/{TASK}/live/control/return",
                       headers=bearer("alice")).status_code == 200
    returning = [c for c in ldb.commits if ("update", session_path) in c]
    assert len(returning) == 1
    assert any(path.startswith(f"{LIVE_AUDIT}/") for _, path in returning[0])

    # And a change whose audit cannot be written does not happen.
    ldb.fail_audit = True
    assert take(client, "alice").status_code == 500
    assert session_doc(ldb)["controller"] is None


# --------------------------------------------------------------------------
# Membership is re-checked (§5.2)
# --------------------------------------------------------------------------


def test_a_take_runs_a_fresh_membership_check(client, ldb, live, groups):
    _ask_for_help(ldb)
    assert client.post(f"/v1/tasks/{TASK}/live/open-to-members",
                       headers=bearer("alice")).status_code == 200
    assert client.get(f"/v1/tasks/{TASK}/live/session", headers=bearer("root")).status_code == 200
    groups.caching = True
    groups.mapping["root@saga.xyz"] = ()
    # The cached answer still authenticates root; the take asks again.
    response = take(client, "root")
    assert response.status_code == 404
    assert ("root@saga.xyz", ENG_GROUP) in groups.forgotten
    assert session_doc(ldb)["controller"] is None


def test_the_fresh_check_gets_past_the_cloud_identity_cache():
    from swarm_api.live import fresh_membership

    class Response:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload

        def json(self):
            return self._payload

    member = {"value": True}

    class Session:
        calls = 0

        def get(self, url, params=None, headers=None, timeout=None):
            Session.calls += 1
            if url.endswith("groups:lookup"):
                return Response({"name": "groups/eng"})
            return Response({"hasMembership": member["value"]})

    groups = CloudIdentityGroups(PROJECT, session=Session(), ttl_seconds=3600)

    class Auth:
        _groups = groups

        def is_tenant_member(self, email, tenant):
            held = groups.groups_for(email, (tenant.principal,))
            return tenant.principal in held

    class Tenant:
        kind = "group"
        principal = ENG_GROUP

    assert fresh_membership(Auth(), "root@saga.xyz", Tenant()) is True
    member["value"] = False
    assert Auth().is_tenant_member("root@saga.xyz", Tenant()) is True  # cached
    assert fresh_membership(Auth(), "root@saga.xyz", Tenant()) is False


def test_the_open_socket_is_re_checked_and_closed_when_membership_lapses(
    client, ldb, live, groups, relay
):
    _ask_for_help(ldb)
    assert client.post(f"/v1/tasks/{TASK}/live/open-to-members",
                       headers=bearer("alice")).status_code == 200
    ticket = ticket_for(client, "root")
    with client.websocket_connect(f"/v1/tasks/{TASK}/live", headers=bearer("root")) as ws:
        ws.send_text(json.dumps({"ticket": ticket}))
        hello = ws.receive_json()
        assert hello["type"] == "status" and hello["in_control"] is False
        assert wait_until(lambda: len(relay.links) == 1)
        link = relay.links[0]
        assert link.texts()[0] == {"type": "hello", "ticket": ticket}
        assert take(client, "root").status_code == 200
        assert wait_until(lambda: {"type": "control", "controller": True} in link.texts())
        ws.send_bytes(bytes([4, 1, 0, 0, 0, 0, 0, 0x61]))  # an RFB KeyEvent
        assert wait_until(lambda: any(isinstance(m, bytes) for m in link.sent))
        groups.mapping["root@saga.xyz"] = ()
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                ws.receive_json()
    assert closed.value.code == 4403
    assert wait_until(lambda: session_doc(ldb)["controller"] is None)
    ended = audit(ldb, "control_ended")
    assert [(r["email"], r["detail"]["cause"]) for r in ended] == [
        ("root@saga.xyz", "membership_lost")]
    assert wait_until(lambda: len(audit(ldb, "viewer_left")) == 1)
    assert [r["email"] for r in audit(ldb, "viewer_joined")] == ["root@saga.xyz"]
    assert link.closed
    # Counts, never content: the key event is counted, not recorded.
    assert ended[0]["detail"]["keys"] == 1
    assert "0x61" not in json.dumps(audit(ldb), default=str)


# --------------------------------------------------------------------------
# No ticket in a URL (§5.2)
# --------------------------------------------------------------------------


def test_no_route_takes_a_ticket_in_its_url(client):
    from swarm_api.routes import live as live_routes

    for route in live_routes.router.routes:
        assert "ticket" not in route.path_format.replace("/live/ticket", "")
        dependant = getattr(route, "dependant", None)
        if dependant is not None:
            for param in dependant.query_params + dependant.path_params:
                assert "ticket" not in param.name, route.path


def test_the_ticket_is_minted_in_a_body_and_the_socket_refuses_one_in_a_query(
    client, ldb, live, relay
):
    response = client.post(f"/v1/tasks/{TASK}/live/ticket", headers=bearer("alice"))
    body = response.json()
    assert body["socket_path"] == f"/v1/tasks/{TASK}/live"
    assert body["ticket"] not in body["socket_path"]
    with client.websocket_connect(f"/v1/tasks/{TASK}/live?ticket={body['ticket']}",
                                  headers=bearer("alice")) as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4400
    assert relay.links == []


def test_the_viewer_ticket_is_one_person_thirty_seconds_and_this_session(client, ldb, live):
    claims = verify_ticket((LIVE_KEY,), ticket_for(client, "root"), now=time.time())
    assert claims["role"] == ROLE_VIEWER
    assert claims["email"] == "root@saga.xyz"
    assert claims["session_id"] == session_id_for(ATTEMPT)
    assert 0 < claims["exp"] - time.time() <= 30
    with pytest.raises(TicketRefused):
        verify_ticket((LIVE_KEY,), ticket_for(client, "root"), now=time.time() + 31)
    tampered = ticket_for(client, "root")
    head, payload, mac = tampered.split(".")
    with pytest.raises(TicketRefused):
        verify_ticket((LIVE_KEY,), ".".join((head, payload[:-2] + "AA", mac)), now=time.time())
    with pytest.raises(TicketRefused):
        verify_ticket(("x" * 40,), ticket_for(client, "root"), now=time.time())


def test_a_ticket_for_someone_else_is_refused_on_the_socket(client, ldb, live, relay):
    ticket = ticket_for(client, "alice")
    with client.websocket_connect(f"/v1/tasks/{TASK}/live", headers=bearer("root")) as ws:
        ws.send_text(json.dumps({"ticket": ticket}))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4404
    assert relay.links == []


def test_viewer_tickets_are_rate_limited_per_person(client, ldb, live):
    codes = [client.post(f"/v1/tasks/{TASK}/live/ticket", headers=bearer("root")).status_code
             for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429


def test_the_audit_is_readable_by_the_tenant_only(client, ldb, live):
    rows = client.get(f"/v1/tasks/{TASK}/live/audit", headers=bearer("root"))
    assert rows.status_code == 200
    assert [r["entry"] for r in rows.json()["entries"]] == ["session_opened"]
    assert client.get(f"/v1/tasks/{TASK}/live/audit", headers=bearer("bob")).status_code == 404


def test_a_socket_on_an_attempt_that_ended_is_closed(client, ldb, live, relay):
    ticket = ticket_for(client, "alice")
    with client.websocket_connect(f"/v1/tasks/{TASK}/live", headers=bearer("alice")) as ws:
        ws.send_text(json.dumps({"ticket": ticket}))
        assert ws.receive_json()["open"] is True
        # A retry: a new lease and generation (invariant 5).
        ldb.docs[f"tasks/{TASK}"].update(
            {"current_lease_id": "lease_live_2", "current_generation": 2})
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                ws.receive_json()
    assert closed.value.code == 4409
    assert session_doc(ldb)["state"] == "closed"
    assert session_doc(ldb)["close_cause"] == "attempt_ended"
