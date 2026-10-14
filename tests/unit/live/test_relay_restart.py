"""The relay says it is restarting, and a roll can see who is watching (RELAY-ROLL, part of #1030).

MEASURED 2026-10-12/14: swarm-live restarted 40 times in ~48 h, once per
release, and every restart cut every live session's tunnel and viewers (close
code 1012, from uvicorn) with nothing said first: queued frames were lost, the
recording's last segment was not written, and connections that dialled in
during the drain were admitted to a process about to exit.

These hold:

  * on SIGTERM the relay itself closes every gate AND every viewer with 1012
    ("restarting, reconnect now"), never 4410 (the session ended), and refuses
    every new connection with 1012 until it exits;
  * that happens before uvicorn's own shutdown runs (`relay_server`);
  * `/healthz` counts the sessions a person controls, and `swarm_live.status`
    prints those counts for scripts/roll-live-relay.sh to defer on.

MUTATIONS: close viewers with CLOSE_ENDED in `_end_tunnel` on a restart; drop
the `_restarting` check in `_admit`; call `super().shutdown` before
`app.state.restarting`; drop `controlled` from /healthz.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest
from test_relay import Endpoint, build, run, ticket

from swarm_live import main as relay_main
from swarm_live import rendezvous as rz
from swarm_live.rendezvous import CLOSE_ENDED, Refused


def test_the_restart_code_is_rfc_6455s_service_restart():
    # The code the gate (livegate/tunnel.py) and the console reconnect at once on.
    assert rz.CLOSE_RESTARTING == 1012
    assert rz.CLOSE_RESTARTING != CLOSE_ENDED


def test_restarting_tells_the_gate_and_every_viewer_to_reconnect():
    key, clock, relay = build()
    gate, a, b = Endpoint(), Endpoint(), Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        await relay.open_viewer(ticket(key, clock, "viewer"), a)
        await relay.open_viewer(ticket(key, clock, "viewer", email="b@example.test"), b)
        return await relay.restarting()

    told = run(go())
    assert told == 3
    assert gate.closed == (rz.CLOSE_RESTARTING, "relay_restarting")
    # Not CLOSE_ENDED: the session is open, on the next relay.
    assert a.closed == (rz.CLOSE_RESTARTING, "relay_restarting")
    assert b.closed == (rz.CLOSE_RESTARTING, "relay_restarting")
    assert relay.tunnel_count() == 0 and relay.viewer_count() == 0


def test_the_lifespan_shutdown_says_restarting_too():
    key, clock, relay = build()
    gate, a = Endpoint(), Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        await relay.open_viewer(ticket(key, clock, "viewer"), a)
        await relay.shutdown()

    run(go())
    assert gate.closed is not None and gate.closed[0] == rz.CLOSE_RESTARTING
    assert a.closed is not None and a.closed[0] == rz.CLOSE_RESTARTING


def test_a_connection_during_the_drain_is_refused_with_the_restart_code():
    key, clock, relay = build()

    async def go():
        await relay.restarting()
        with pytest.raises(Refused) as tunnel:
            await relay.open_tunnel(ticket(key, clock), Endpoint())
        with pytest.raises(Refused) as viewer:
            await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        return tunnel.value, viewer.value

    tunnel, viewer = run(go())
    assert tunnel.code == viewer.code == rz.CLOSE_RESTARTING


def test_the_websocket_route_closes_a_tunnel_during_the_drain_with_the_restart_code():
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    key, clock, relay = build()
    with TestClient(relay_main.create_app(relay)) as client:
        client.portal.call(relay.restarting)
        with client.websocket_connect("/v1/tunnel") as ws:
            ws.send_text(ticket(key, clock))
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_text()
            assert e.value.code == rz.CLOSE_RESTARTING


def test_healthz_counts_the_sessions_a_person_controls():
    from fastapi.testclient import TestClient

    key, clock, relay = build()

    async def grant():
        gate, a = Endpoint(), Endpoint()
        await relay.open_tunnel(ticket(key, clock), gate)
        va = await relay.open_viewer(ticket(key, clock, "viewer", email="a@example.test"), a)
        await relay.viewer_text(va, json.dumps({"type": "control", "ticket": ticket(
            key, clock, "control", email="a@example.test")}))

    with TestClient(relay_main.create_app(relay)) as client:
        client.portal.call(grant)
        assert client.get("/healthz").json() == {"ok": True, "tunnels": 1, "viewers": 1, "controlled": 1}


def test_sigterm_tells_the_relays_connections_before_uvicorn_cuts_them(monkeypatch):
    import uvicorn

    order: list[str] = []

    async def uvicorn_shutdown(self, *args, **kwargs):
        order.append("uvicorn")

    monkeypatch.setattr(uvicorn.Server, "shutdown", uvicorn_shutdown)
    key, clock, relay = build()
    app = relay_main.create_app(relay)
    real = app.state.restarting

    async def restarting():
        order.append("relay")
        return await real()

    app.state.restarting = restarting
    server = relay_main.relay_server(app)
    gate = Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        await server.shutdown()

    asyncio.run(go())
    assert order == ["relay", "uvicorn"]
    assert gate.closed is not None and gate.closed[0] == rz.CLOSE_RESTARTING
    assert server.config.port == relay_main.PORT


def test_the_status_command_prints_the_counts_and_nothing_else(monkeypatch, capsys):
    from swarm_live import status

    body = {"ok": True, "tunnels": 2, "viewers": 1, "controlled": 1, "extra": "dropped"}
    asked: list[str] = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(url, timeout):
        asked.append(url)
        return Response(json.dumps(body).encode())

    monkeypatch.setattr(status.urllib.request, "urlopen", urlopen)
    assert status.main([]) == 0
    assert asked == [f"http://127.0.0.1:{relay_main.PORT}/healthz"]
    assert json.loads(capsys.readouterr().out) == {"ok": True, "tunnels": 2, "viewers": 1, "controlled": 1}


def test_the_status_command_fails_when_the_relay_does_not_answer(monkeypatch, capsys):
    from swarm_live import status

    def urlopen(url, timeout):
        raise ConnectionRefusedError

    monkeypatch.setattr(status.urllib.request, "urlopen", urlopen)
    assert status.main([]) == 1
    assert "ConnectionRefusedError" in capsys.readouterr().err
