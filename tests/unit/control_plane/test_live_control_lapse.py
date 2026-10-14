"""Every end of control in swarm-api reaches the gate (CONTROL-LAPSE, part of #1030).

MEASURED 2026-10-13 in the live-browser analysis of 26 browser tasks:

* task e8cb...: the console's control lapsed (`renew_lapsed` at 15:11:12Z, the
  API recorded control returned), but `_settle` -> `_Plan.end_control` only
  wrote Firestore; the gate held control until a relay restart at 16:03:47Z and
  refused the agent 72 times in 54 minutes (3458 g2: 44 in 8 min; 8fcb g4: 12);
* task c6bf0368...: control taken 11:34:19, the task SUCCEEDED 11:40:50 with
  the episode still open.

These tests hold swarm-api's half: every end of control -- a lapse, idle, a
preemption, a Return press, a lapsed membership -- POSTs a revoke to the
relay's `/internal/control` after its transaction commits, with its cause; a
renewal POSTs a renewal; what swarm-api mints is what the REAL relay accepts,
and it reaches the gate as `CONTROL` with nobody in control within 2 s; and an
episode still open when the task ends is closed, by a read and by the socket
whose relay link the task's end dropped.

The relay is a test double for the routes (test_live_api.py's `FakeRelay`,
with a control route) and the real `swarm_live.rendezvous.Rendezvous` for the
wire. Every key and ticket is built at runtime.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from swarm_api import live as api
from swarm_api.live import LIVE_SESSIONS, session_id_for

from .test_live_api import (  # noqa: F401  (fixtures)
    ATTEMPT,
    LEASE,
    LIVE_KEY,
    READY,
    SESSION,
    SOCKET,
    TASK,
    Attempt,
    _ask_for_help,
    FakeLink,
    FakeRelay,
    audit,
    bearer,
    claims_of,
    clock,
    groups,
    ldb,
    live,
    make_client,
    open_session,
    people,
    seed_running,
    session_doc,
    take,
    ticket_for,
    wait_until,
)

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-live")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_live import rendezvous as rz  # noqa: E402


class ControlRelay(FakeRelay):
    """test_live_api's relay double, with the relay's control route."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.controls: list[str] = []

    def send_control(self, request: str) -> bool:
        self.controls.append(request)
        return True

    def asked(self) -> list[tuple[str, str, str]]:
        """(action, email, cause) of each control request, read without verifying."""
        out = []
        for raw in self.controls:
            body = claims_of(raw)
            out.append((body["action"], body["email"], body["cause"]))
        return out


@pytest.fixture
def relay() -> ControlRelay:
    return ControlRelay()


@pytest.fixture
def client(ldb, people, groups, objects, clock, relay):
    return make_client(ldb, people, groups, objects, clock, relay)


def _renew(client, user: str):
    return client.post(f"/v1/tasks/{TASK}/live/control/renew", headers=bearer(user))


def _view(client, user: str = "alice"):
    response = client.get(f"/v1/tasks/{TASK}/live/session", headers=bearer(user))
    assert response.status_code == 200, response.text
    return response.json()


def _end_task(ldb, state: str = "SUCCEEDED") -> None:
    """The worker's final write: the task ends and its lease goes."""
    now = datetime.now(timezone.utc)
    ldb.docs[f"tasks/{TASK}"].update({"state": state, "current_lease_id": None,
                                      "completed_at": now})
    ldb.docs[f"leases/{LEASE}"].update({"state": "RELEASED", "released_at": now})


# --------------------------------------------------------------------------
# Every end of control is a revoke at the relay
# --------------------------------------------------------------------------


def test_a_lapsed_renewal_is_revoked_at_the_relay(client, ldb, live, relay, clock):
    """The e8cb shape: no renewal for 61 s; the next read settles it."""
    assert take(client, "alice").status_code == 200
    clock.advance(61)
    assert _view(client)["session"]["controller"] is None
    assert [r["detail"]["cause"] for r in audit(ldb, "control_ended")] == ["renew_lapsed"]
    assert relay.asked()[-1] == ("revoke", "alice@saga.xyz", "renew_lapsed")


def test_idle_is_revoked_at_the_relay(client, ldb, live, relay, clock):
    assert take(client, "alice").status_code == 200
    for _ in range(6):
        clock.advance(50)
        assert _renew(client, "alice").status_code == 200
    clock.advance(50)
    _view(client)
    assert relay.asked()[-1] == ("revoke", "alice@saga.xyz", "idle")


def test_a_preemption_revokes_the_person_preempted(client, ldb, live, relay):
    _ask_for_help(ldb)
    assert client.post(f"/v1/tasks/{TASK}/live/open-to-members",
                       headers=bearer("alice")).status_code == 200
    assert take(client, "root").status_code == 200
    relay.controls.clear()
    assert take(client, "alice", preempt=True).status_code == 200
    assert relay.asked() == [("revoke", "root@saga.xyz", "preempted")]


def test_a_return_is_revoked_at_the_relay(client, ldb, live, relay):
    assert take(client, "alice").status_code == 200
    relay.controls.clear()
    returned = client.delete(f"/v1/tasks/{TASK}/live/control", headers=bearer("alice"))
    assert returned.status_code == 200, returned.text
    assert relay.asked() == [("revoke", "alice@saga.xyz", "returned")]
    # Idempotent at swarm-api: a second Return ends nothing and asks nothing.
    assert client.delete(f"/v1/tasks/{TASK}/live/control", headers=bearer("alice")).status_code == 409
    assert relay.asked() == [("revoke", "alice@saga.xyz", "returned")]


def test_a_lapsed_membership_is_revoked_at_the_relay(client, ldb, live, relay):
    assert take(client, "alice").status_code == 200
    relay.controls.clear()
    ctx = client.app.state.ctx
    service = api.LiveService(settings=ctx.settings, db=ctx.db, store=ctx.store,
                              authenticator=ctx.authenticator, relay=relay, now=ctx.now)
    admission = api.Admission(email="alice@saga.xyz", tenant_id="eng", task_id=TASK,
                              session_id=SESSION, attempt_id=ATTEMPT, generation=1, ticket="")
    assert service.end_control(admission, "membership_lost") is True
    assert relay.asked() == [("revoke", "alice@saga.xyz", "membership_lost")]


def test_a_renewal_is_renewed_at_the_relay_and_never_asked_when_nothing_changed(
        client, ldb, live, relay):
    _view(client)
    assert relay.controls == [], "a read that changes nothing asks the relay nothing"
    assert take(client, "alice").status_code == 200
    assert relay.controls == [], "a take is granted on the person's own link"
    assert _renew(client, "alice").status_code == 200
    assert relay.asked() == [("renew", "alice@saga.xyz", "")]


# --------------------------------------------------------------------------
# The wire: what swarm-api mints, the real relay accepts and the gate gets
# --------------------------------------------------------------------------


def test_both_sides_state_the_same_control_request():
    assert api.CONTROL_PURPOSE == rz.CONTROL_DOMAIN
    assert api.CONTROL_FIELDS == set(rz._CONTROL_FIELDS)
    assert api.CONTROL_SECONDS == rz.CONTROL_TTL_SECONDS
    assert len({api.CONTROL_PURPOSE, api.TICKET_PURPOSE, api.CLOSE_PURPOSE,
                api.DEVICE_PURPOSE}) == 4
    assert api.relay_http_url("ws://10.10.0.42:8080", "/internal/control") == (
        "http://10.10.0.42:8080/internal/control")


def test_a_lapse_reaches_the_gate_through_the_real_relay_within_two_seconds(
        client, ldb, live, relay, clock):
    """Lapse -> the gate's `CONTROL` with nobody in control, measured from the
    read that settled the lapse to the frame on the gate's tunnel."""
    claims = claims_of(live["session"]["ticket"])
    real = rz.Rendezvous(LIVE_KEY.encode())

    class Gate:
        def __init__(self) -> None:
            self.binary: list[bytes] = []

        async def send_bytes(self, data: bytes) -> None:
            self.binary.append(data)

        async def send_text(self, text: str) -> None:
            pass

        async def close(self, code: int, reason: str) -> None:
            pass

    gate = Gate()
    tunnel = rz.mint_ticket(LIVE_KEY.encode(), session_id=claims["session_id"],
                            tenant_id=claims["tenant_id"], task_id=claims["task_id"],
                            attempt_id=claims["attempt_id"], generation=claims["generation"],
                            role="tunnel")

    async def go() -> float:
        await real.open_tunnel(tunnel, gate)
        assert take(client, "alice").status_code == 200
        clock.advance(61)
        started = time.monotonic()
        _view(client)
        (raw,) = relay.controls
        assert await real.control_request(raw) == (SESSION, "revoke", True)
        await real.flush()
        return time.monotonic() - started

    elapsed = asyncio.run(go())
    frames = [rz.decode_frame(b) for b in gate.binary]
    assert (rz.OP_CONTROL, "", b"renew_lapsed") in frames
    assert elapsed < 2.0


def test_the_relay_closer_posts_a_control_request_to_the_internal_route(monkeypatch):
    posted: list[tuple[str, bytes]] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout=None):
        posted.append((request.full_url, request.data))
        return Response()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    closer = api.HttpRelayCloser("ws://10.10.0.42:8080", LIVE_KEY)
    request = api.mint_control(LIVE_KEY, {
        "session_id": SESSION, "tenant_id": "eng", "task_id": TASK, "attempt_id": ATTEMPT,
        "generation": 1, "email": "alice@saga.xyz", "action": "revoke", "cause": "idle",
    }, now=time.time())
    assert closer.send_control(request) is True
    ((url, body),) = posted
    assert url == "http://10.10.0.42:8080/internal/control"
    verified = rz.verify_control(LIVE_KEY.encode(), body.decode(), now=time.time())
    assert (verified.action, verified.cause, verified.email) == ("revoke", "idle", "alice@saga.xyz")
    # Unreachable: logged, never raised (the gate's lease is the backstop).
    assert api.HttpRelayCloser("ws://127.0.0.1:9", LIVE_KEY, timeout=0.2).send_control(request) is False


# --------------------------------------------------------------------------
# The task's end closes the episode (task c6bf0368...)
# --------------------------------------------------------------------------


def test_a_read_after_the_task_ended_closes_the_open_episode(client, ldb, live, relay):
    assert take(client, "alice").status_code == 200
    _end_task(ldb)
    assert _view(client)["session"] is None
    assert session_doc(ldb)["state"] == "closed"
    assert session_doc(ldb)["controller"] is None
    assert [r["detail"]["cause"] for r in audit(ldb, "control_ended")] == ["attempt_ended"]
    events = [d for p, d in ldb.docs.items() if p.startswith(f"tasks/{TASK}/events/")]
    assert [e["detail"]["cause"] for e in events if e["type"] == "human_control_returned"] == [
        "attempt_ended"]
    assert SESSION in relay.closed
    # Once: a later read finds nothing open.
    _view(client)
    assert len(audit(ldb, "control_ended")) == 1


class _DroppingRelay(ControlRelay):
    """A relay whose viewer link ends when the test says: the gate's tunnel
    went with the task. A threading event, since the socket's loop is the
    test client's, not the test's."""

    def __init__(self) -> None:
        super().__init__()
        self.drop = threading.Event()

    async def connect(self, url: str) -> FakeLink:
        drop = self.drop

        class Link(FakeLink):
            async def recv(self):
                if self._frames:
                    return self._frames.pop(0)
                while not drop.is_set():
                    await asyncio.sleep(0.01)
                return None

        link = Link(url, [READY])
        self.links.append(link)
        return link


def test_the_socket_whose_relay_link_the_task_end_dropped_closes_the_episode(
        ldb, people, groups, objects, clock):
    relay = _DroppingRelay()
    # A poll too slow to notice the end first: the relay link's end must.
    client = make_client(ldb, people, groups, objects, clock, relay,
                         live_session_poll_seconds=30.0,
                         live_membership_recheck_seconds=300.0)
    attempt = Attempt()
    seed_running(client, ldb, attempt)
    assert open_session(client, attempt).status_code == 201
    assert take(client, "alice").status_code == 200
    ticket = ticket_for(client, "alice")
    with client.websocket_connect(SOCKET, headers=bearer("alice")) as ws:
        ws.send_text(json.dumps({"ticket": ticket}))
        assert ws.receive_json()["open"] is True
        _end_task(ldb)
        relay.drop.set()
        assert wait_until(lambda: session_doc(ldb)["state"] == "closed")
    assert [r["detail"]["cause"] for r in audit(ldb, "control_ended")] == ["attempt_ended"]
    assert ldb.docs[f"{LIVE_SESSIONS}/{session_id_for(ATTEMPT)}"]["controller"] is None
