"""swarm-api's side of the relay wiring (LB-L8d; docs/design/live-browser.md §3.3, §5.2, §5.11).

Owner decisions of 2026-10-11, held here:

  (a) the relay reads no Firestore, so swarm-api tells it when a session is
      over: `POST /internal/close`, MAC'd with swarm-live-key, whenever a
      session closes and whenever a stale generation is seen;
  (b) browsers never reach the relay: they open wss to swarm-api at
      `/v1/live/<session>/stream`, which authorises them exactly as the other
      live routes do and proxies to `<SWARM_LIVE_URL>/v1/view`.

The relay here is a fake at the HTTP seam (`swarm_api.live._http_post`): every
close request swarm-api sends is captured and checked with the relay's own
`verify_close`, so a format drift between the two fails here rather than in a
cluster. The viewer socket's relay is `FakeRelay` from test_live_api.

Every key, token and ticket is built at runtime: nothing here is a credential.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from swarm_api import live as live_mod
from swarm_api.live import (
    CLOSE_SIGNATURE_HEADER,
    MAX_TICKET_SECONDS,
    RelayCloser,
    mint_ticket,
    session_id_for,
    verify_ticket,
)

from .test_live_api import (  # noqa: F401 - fixtures are used by name
    ATTEMPT,
    LIVE_KEY,
    RELAY_URL,
    SOCKET,
    TASK,
    Attempt,
    bearer,
    client,
    clock,
    groups,
    ldb,
    live,
    make_client,
    open_session,
    people,
    relay,
    seed_running,
    ticket_for,
)

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-live")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_live.rendezvous import verify_close  # noqa: E402

SESSION = session_id_for(ATTEMPT)


class Closes:
    """What the relay's close route received, verified as the relay verifies it."""

    def __init__(self, status: int = 204) -> None:
        self.status = status
        self.requests: list[tuple[str, Any]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        request = verify_close(LIVE_KEY.encode(), body, headers.get(CLOSE_SIGNATURE_HEADER),
                               now=time.time())
        self.requests.append((url, request))
        return self.status

    def sessions(self) -> list[str]:
        return [r.session_id for _, r in self.requests]

    def causes(self) -> list[str]:
        return [r.cause for _, r in self.requests]


@pytest.fixture
def closes(monkeypatch) -> Closes:
    fake = Closes()
    monkeypatch.setattr(live_mod, "_http_post", fake)
    return fake


def _refused(client, path: str, user: str, ticket: str) -> int:
    with client.websocket_connect(path, headers=bearer(user)) as ws:
        ws.send_text(json.dumps({"ticket": ticket}))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    return closed.value.code


# --------------------------------------------------------------------------
# The proxy authorises exactly as the live routes do
# --------------------------------------------------------------------------


def test_the_proxy_refuses_another_tenants_session(client, ldb, live, relay, closes):
    # A ticket for eng's session re-signed for bob, who is research: the tenant
    # check refuses it before anything reaches the relay.
    claims = verify_ticket((LIVE_KEY,), ticket_for(client, "alice"), now=time.time())
    forged = mint_ticket(LIVE_KEY, {**claims, "email": "bob@saga.xyz",
                                    "exp": int(time.time()) + 30})
    assert _refused(client, SOCKET, "bob", forged) == 4404
    # Bob's own tenant named in the ticket, eng's session in the path: the
    # task is not research's, the same 404.
    own = mint_ticket(LIVE_KEY, {**claims, "email": "bob@saga.xyz", "tenant_id": "research",
                                 "exp": int(time.time()) + 30})
    assert _refused(client, SOCKET, "bob", own) == 4404
    assert relay.links == []
    # A refusal for another tenant closes nothing at the relay.
    assert closes.requests == []


def test_the_proxy_refuses_a_ticket_for_another_session_than_its_path(
    client, ldb, live, relay, closes
):
    ticket = ticket_for(client, "alice")
    assert _refused(client, "/v1/live/live_att_other/stream", "alice", ticket) == 4404
    assert relay.links == []


def test_the_proxy_refuses_a_stale_generation_and_closes_it_at_the_relay(
    client, ldb, live, relay, closes
):
    ticket = ticket_for(client, "alice")
    # A retry: a new lease and generation (invariant 5) before the socket opens.
    ldb.docs[f"tasks/{TASK}"].update({"current_lease_id": "lease_live_2", "current_generation": 2})
    assert _refused(client, SOCKET, "alice", ticket) == 4404
    assert relay.links == []
    # The task's current attempt has no session yet, so the ticket's session is
    # not the open one: it is closed at the relay as a stale generation.
    assert [(r.session_id, r.cause) for _, r in closes.requests] == [
        (SESSION, "stale_generation")]
    url, request = closes.requests[0]
    assert url == "http://10.10.0.42:8080/internal/close"
    assert 0 < request.exp - time.time() <= MAX_TICKET_SECONDS


def test_the_proxy_dials_the_relay_view_route_and_sends_the_ticket_first(
    client, ldb, live, relay, closes
):
    ticket = ticket_for(client, "alice")
    with client.websocket_connect(SOCKET, headers=bearer("alice")) as ws:
        ws.send_text(json.dumps({"ticket": ticket}))
        assert ws.receive_json()["open"] is True
        assert [link.url for link in relay.links] == [RELAY_URL + "/v1/view"]
        assert relay.links[0].sent[0] == ticket
        assert closes.requests == []
        # Ended by a cancel, so the socket closes from the server's side.
        ldb.docs[f"tasks/{TASK}"]["cancel_requested"] = True
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                ws.receive_json()
    assert closed.value.code == 4409
    assert (SESSION, "cancelled") in [(r.session_id, r.cause) for _, r in closes.requests]


def test_an_open_socket_whose_attempt_ends_is_closed_here_and_at_the_relay(
    client, ldb, live, relay, closes
):
    ticket = ticket_for(client, "alice")
    with client.websocket_connect(SOCKET, headers=bearer("alice")) as ws:
        ws.send_text(json.dumps({"ticket": ticket}))
        assert ws.receive_json()["open"] is True
        ldb.docs[f"tasks/{TASK}"].update(
            {"current_lease_id": "lease_live_2", "current_generation": 2})
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                ws.receive_json()
    assert closed.value.code == 4409
    assert SESSION in closes.sessions()


# --------------------------------------------------------------------------
# Every close reaches the relay
# --------------------------------------------------------------------------


def test_a_session_closed_by_a_cancel_is_closed_at_the_relay_once(client, ldb, live, closes):
    ldb.docs[f"tasks/{TASK}"]["cancel_requested"] = True
    view = client.get(f"/v1/tasks/{TASK}/live/session", headers=bearer("alice"))
    assert view.json()["state"] == "closed"
    assert [(r.session_id, r.cause) for _, r in closes.requests] == [(SESSION, "cancelled")]
    # Reading it again closes nothing new: the transaction closed nothing.
    client.get(f"/v1/tasks/{TASK}/live/session", headers=bearer("alice"))
    assert len(closes.requests) == 1


def test_a_fenced_worker_asking_for_a_tunnel_ticket_closes_its_session_at_the_relay(
    client, ldb, live, closes
):
    attempt = live["attempt"]
    ldb.docs[f"tasks/{TASK}"]["state"] = "QUEUED"
    assert open_session(client, attempt).status_code == 409
    assert closes.sessions() == [SESSION]
    assert closes.causes() == ["stale_generation"]


def test_a_live_session_that_stays_open_closes_nothing(client, ldb, live, closes):
    client.get(f"/v1/tasks/{TASK}/live/session", headers=bearer("alice"))
    ticket_for(client, "alice")
    assert closes.requests == []


# --------------------------------------------------------------------------
# The closer itself
# --------------------------------------------------------------------------


def test_the_closer_never_raises_and_says_whether_the_relay_took_it():
    calls: list[str] = []

    def down(url: str, body: bytes, headers: dict[str, str]) -> int:
        calls.append(url)
        raise OSError("connection refused")

    assert RelayCloser(RELAY_URL, LIVE_KEY, post=down).close(SESSION, "cancelled") is False
    assert calls == ["http://10.10.0.42:8080/internal/close"]
    assert RelayCloser(RELAY_URL, LIVE_KEY, post=lambda *a: 403).close(SESSION, "x") is False
    assert RelayCloser(RELAY_URL, LIVE_KEY, post=lambda *a: 204).close(SESSION, "x") is True
    # No relay, or no key: nothing is sent.
    for closer in (RelayCloser("", LIVE_KEY, post=down), RelayCloser(RELAY_URL, "", post=down)):
        assert closer.configured is False
        assert closer.close(SESSION, "x") is False
    assert len(calls) == 1


def test_tickets_never_live_longer_than_the_relay_accepts(
    ldb, people, groups, objects, clock, relay
):
    client = make_client(ldb, people, groups, objects, clock, relay,
                         live_viewer_ticket_seconds=3600, live_tunnel_ticket_seconds=3600)
    attempt = Attempt()
    seed_running(client, ldb, attempt)
    tunnel = open_session(client, attempt).json()["ticket"]
    for raw in (tunnel, ticket_for(client, "alice")):
        claims = verify_ticket((LIVE_KEY,), raw, now=time.time())
        assert 0 < claims["exp"] - time.time() <= MAX_TICKET_SECONDS
        assert "nonce" in claims and "jti" not in claims


def test_swarm_api_reads_the_relay_address_from_swarm_live_url(monkeypatch):
    from swarm_api.settings import ApiSettings

    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("SWARM_LIVE_RELAY_URL", "ws://not-this:1")
    assert ApiSettings.from_env().live_relay_url == ""
    monkeypatch.setenv("SWARM_LIVE_URL", RELAY_URL)
    assert ApiSettings.from_env().live_relay_url == RELAY_URL
