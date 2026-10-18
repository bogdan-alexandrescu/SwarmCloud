"""swarm-api and the relay speak one wire (LB-L8d, part of #1030).

Lanes L7 (the relay, apps/swarm-live) and L9 (swarm-api's live routes) were
built in parallel and disagreed on the ticket's shape, its MAC domain, its
one-use field, the viewer socket's first message and how control is granted.
Owner decision 2026-10-11 (a) settles it: swarm-api mints, the relay checks the
MAC, expiry and single use and trusts only the ticket, and swarm-api ends a
session through the relay's `POST /internal/close`.

The format cannot live in the frozen contract without a request, so each side
states it and this file holds the two to each other: what swarm-api mints is
fed to the REAL relay (`swarm_live.rendezvous.Rendezvous`, in process), never
to a double.

Every key here is built at runtime; nothing is a credential.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-live")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_api import live as api  # noqa: E402
from swarm_api.routes import live as api_routes  # noqa: E402
from swarm_live import main as relay_main  # noqa: E402
from swarm_live import rendezvous as rz  # noqa: E402

TENANT, TASK, ATTEMPT, GEN = "eng", "task_w", "att_w1", 4
SESSION = api.session_id_for(ATTEMPT)


class Endpoint:
    def __init__(self) -> None:
        self.binary: list[bytes] = []
        self.text: list[str] = []
        self.closed: tuple[int, str] | None = None

    async def send_bytes(self, data: bytes) -> None:
        self.binary.append(data)

    async def send_text(self, text: str) -> None:
        self.text.append(text)

    async def close(self, code: int, reason: str) -> None:
        if self.closed is None:
            self.closed = (code, reason)


def _key() -> str:
    return secrets.token_hex(24)


def _claims(role: str, **over) -> dict:
    claims = {"session_id": SESSION, "tenant_id": TENANT, "task_id": TASK, "attempt_id": ATTEMPT,
              "generation": GEN, "role": role, "exp": int(time.time()) + 20}
    if role != api.ROLE_TUNNEL:
        claims["email"] = "ana@example.test"
    claims.update(over)
    return claims


def _admission(ticket: str = "") -> api.Admission:
    return api.Admission(email="ana@example.test", tenant_id=TENANT, task_id=TASK,
                         session_id=SESSION, attempt_id=ATTEMPT, generation=GEN, ticket=ticket)


def _service(key: str) -> api.LiveService:
    return api.LiveService(settings=SimpleNamespace(live_key=key, live_key_previous=""),
                           db=None, store=None, authenticator=None)


def test_both_sides_state_the_same_format():
    assert api.TICKET_PURPOSE == rz.TICKET_DOMAIN
    assert api.CLOSE_PURPOSE == rz.CLOSE_DOMAIN
    assert api.TICKET_PURPOSE != api.CLOSE_PURPOSE
    assert api.TICKET_FIELDS == set(rz._TICKET_FIELDS)
    assert api.TICKET_MAX_SECONDS == rz.TICKET_TTL_SECONDS
    assert api.CLOSE_SECONDS == rz.CLOSE_TTL_SECONDS
    assert api_routes.RELAY_CLOSE_FULL == rz.CLOSE_FULL
    assert api_routes.CLOSE_REFUSED == rz.CLOSE_REFUSED
    # Decision (b): one port, plain ws, for both lanes.
    assert relay_main.PORT == 8080


def test_the_relay_serves_the_paths_swarm_api_dials():
    paths = {getattr(r, "path", None) for r in relay_main.create_app(rz.Rendezvous(b"k" * 32)).routes}
    assert api_routes.RELAY_VIEW_PATH in paths
    assert "/internal/close" in paths
    assert api.relay_http_url("ws://10.10.0.42:8080", "/internal/close").endswith("/internal/close")


@pytest.mark.parametrize("role", [api.ROLE_TUNNEL, api.ROLE_VIEWER])
def test_a_ticket_swarm_api_mints_verifies_at_the_relay(role):
    key = _key()
    ticket = rz.verify_ticket(key.encode(), api.mint_ticket(key, _claims(role)), now=time.time())
    assert (ticket.session_id, ticket.tenant_id, ticket.task_id, ticket.attempt_id,
            ticket.generation, ticket.role) == (SESSION, TENANT, TASK, ATTEMPT, GEN, role)
    assert ticket.nonce  # filled in by swarm-api, one per ticket


def test_swarm_api_refuses_to_mint_a_field_the_relay_would_refuse():
    with pytest.raises(ValueError):
        api.mint_ticket(_key(), {**_claims(api.ROLE_VIEWER), "jti": "x"})


def test_a_configured_lifetime_never_exceeds_what_the_relay_accepts():
    assert api.ticket_seconds(api.ROLE_TUNNEL, 600) == 60
    assert api.ticket_seconds(api.ROLE_VIEWER, 600) == 30
    assert api.ticket_seconds(api.ROLE_VIEWER, 10) == 10


def test_the_whole_session_over_the_real_relay_and_its_close(monkeypatch):
    """Tunnel, viewer and control tickets from swarm-api open a session at the
    relay; swarm-api's close request, as HttpRelayCloser sends it, drops the
    connected viewer and refuses the tickets still in flight."""
    key = _key()
    relay = rz.Rendezvous(key.encode())
    service = _service(key)
    gate, viewer = Endpoint(), Endpoint()
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

    async def go():
        await relay.open_tunnel(api.mint_ticket(key, _claims(api.ROLE_TUNNEL)), gate)
        held = await relay.open_viewer(api.mint_ticket(key, _claims(api.ROLE_VIEWER)), viewer)
        await relay.viewer_text(held, json.dumps(
            {"type": "control", "ticket": service.control_ticket(_admission(), True)}))
        assert relay.controller(SESSION) == held.conn_id
        await relay.viewer_text(held, json.dumps(
            {"type": "control", "ticket": service.control_ticket(_admission(), False)}))
        assert relay.controller(SESSION) is None
        late = api.mint_ticket(key, _claims(api.ROLE_VIEWER))

        api.HttpRelayCloser("ws://10.10.0.42:8080", key).close_session(SESSION)
        (url, body), = posted
        assert url == "http://10.10.0.42:8080/internal/close"
        assert await relay.close_request(body.decode()) == (SESSION, 2)
        with pytest.raises(rz.Refused) as e:
            await relay.open_viewer(late, Endpoint())
        assert e.value.reason == "session_closed"

    asyncio.run(go())
    assert viewer.closed == (rz.CLOSE_ENDED, "session_ended")
    assert gate.closed == (rz.CLOSE_ENDED, "session_ended")


def relay_report_text(url: str, registrable: str, preset: str, orientation: str,
                      agent: dict | None = None) -> str:
    """What swarm-api's viewer link receives when the gate reports `url` and a
    device (and, from a LIVE-UNUSED gate, `agent`): the GATE's own report,
    framed by the gate's tunnel module and passed through the REAL relay.
    LB-DEV-A, part of #1030."""
    from agent_worker.livegate import report, tunnel
    from agent_worker.livegate.presets import resolve

    key = _key()
    relay = rz.Rendezvous(key.encode())
    gate, viewer = Endpoint(), Endpoint()
    payload = report.encode_report(report.page_origin(url, registrable),
                                   report.device_report(resolve(preset, orientation)), agent)

    async def go() -> None:
        tunnel_ = await relay.open_tunnel(api.mint_ticket(key, _claims(api.ROLE_TUNNEL)), gate)
        await relay.open_viewer(api.mint_ticket(key, _claims(api.ROLE_VIEWER)), viewer)
        await relay.from_tunnel(tunnel_, tunnel.encode_frame(tunnel.OP_REPORT, "", payload))
        await relay.flush()

    asyncio.run(go())
    reports = [t for t in viewer.text if json.loads(t).get("type") == api.REPORT_TYPE]
    assert len(reports) == 1, viewer.text
    return reports[0]


def test_the_gates_report_crosses_the_real_relay_into_what_swarm_api_accepts():
    """The gate's report op, the relay's text to a viewer link and swarm-api's
    reading of it are one wire: what the gate measured is what swarm-api stores,
    and it is exactly the console's seam fixture (tests/fixtures/live)."""
    fixture = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "live"
                          / "session.reported.json").read_text())["session"]
    text = relay_report_text("https://github.com/login", "github.com", "iphone-15", "portrait")
    assert api.parse_report(text) == {"origin": fixture["origin"], "device": fixture["device"]}
    assert api.REPORT_TYPE == "report"


def test_a_ticket_from_another_key_or_tenant_or_generation_is_refused_at_the_relay():
    key = _key()
    relay = rz.Rendezvous(key.encode())

    async def go():
        await relay.open_tunnel(api.mint_ticket(key, _claims(api.ROLE_TUNNEL)), Endpoint())
        for raw, reason in (
            (api.mint_ticket(_key(), _claims(api.ROLE_VIEWER)), "bad_signature"),
            (api.mint_ticket(key, _claims(api.ROLE_VIEWER, tenant_id="ops")), "wrong_tenant"),
            (api.mint_ticket(key, _claims(api.ROLE_VIEWER, generation=GEN - 1)), "wrong_generation"),
            (api.mint_ticket(key, _claims(api.ROLE_VIEWER, exp=int(time.time()) - 10)), "expired"),
        ):
            with pytest.raises(rz.Refused) as e:
                await relay.open_viewer(raw, Endpoint())
            assert e.value.reason == reason
        used = api.mint_ticket(key, _claims(api.ROLE_VIEWER))
        await relay.open_viewer(used, Endpoint())
        with pytest.raises(rz.Refused) as e:
            await relay.open_viewer(used, Endpoint())
        assert e.value.reason == "replayed"

    asyncio.run(go())


def test_a_ticket_is_never_a_close_request():
    key = _key()
    with pytest.raises(rz.Refused):
        rz.verify_close(key.encode(), api.mint_ticket(key, _claims(api.ROLE_TUNNEL)), now=time.time())
    request = rz.verify_close(key.encode(), api.mint_close(key, SESSION, now=time.time()),
                              now=time.time())
    assert request.session_id == SESSION
