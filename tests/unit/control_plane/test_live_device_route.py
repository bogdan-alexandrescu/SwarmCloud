"""The person in control switches the device (LB-DEV-B, part of #1030).

docs/design/live-browser.md §6.1, Q13: the agent, and the person in control,
choose the device; a viewer's switcher is disabled. Owner decision
2026-10-12 ("device-disabled": match the mockup) turned the console's
disabled chips into working ones, which needs a route: `POST
/v1/tasks/{id}/live/device`.

What this file holds, over the real routes and the in-memory Firestore:

  * only the current controller may switch; everyone else -- a viewer, the
    submitter while someone else holds control, anyone while nobody does --
    gets 409 `not_controller`, as renew and return answer, and nothing is
    sent to the relay;
  * a preset is chosen BY NAME from the gate's list: an unknown name, a size
    or any other key is 422, and so is touch on a preset with none
    (invariant 10);
  * the choice is audited and sent to the relay as a MAC'd device request
    that the REAL relay verifies and turns into a `DEVICE` frame for the
    session's gate (swarm_live.rendezvous, in process);
  * `you.can_switch_device` is true for the controller only;
  * `session.device` is the gate's REPORT, not the request: it changes when
    the gate reports the device it applied.

Every key and ticket is built at runtime: nothing here is a credential.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from swarm_api import live as live_module

from .test_live_api import (  # noqa: F401  (pytest fixtures, used by name)
    ATTEMPT,
    LIVE_KEY,
    READY,
    SOCKET,
    TASK,
    _ask_for_help,
    audit,
    bearer,
    client,
    clock,
    groups,
    ldb,
    live,
    make_client,
    people,
    relay,
    session_doc,
    take,
    ticket_for,
)

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-live")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_live import rendezvous as rz  # noqa: E402

ROUTE = f"/v1/tasks/{TASK}/live/device"


def switch(client, user: str, body: Any) -> Any:
    return client.post(ROUTE, json=body, headers=bearer(user))


def _in_control(client, ldb, user: str = "alice") -> None:
    _ask_for_help(ldb)
    taken = take(client, user)
    assert taken.status_code == 200, taken.text


# --------------------------------------------------------------------------
# Who may switch
# --------------------------------------------------------------------------


def test_the_controller_switches_and_the_request_reaches_the_relay(client, ldb, live, relay):
    _in_control(client, ldb)
    response = switch(client, "alice", {"preset": "iphone-15", "orientation": "landscape"})
    assert response.status_code == 200, response.text
    assert response.json()["session"]["you"]["can_switch_device"] is True
    (request,) = relay.devices
    claims = json.loads(rz._b64decode(request.split(".")[0]))
    assert {k: claims[k] for k in ("session_id", "tenant_id", "task_id", "attempt_id", "generation",
                                   "preset", "orientation", "touch")} == {
        "session_id": "live_" + ATTEMPT, "tenant_id": "eng", "task_id": TASK,
        "attempt_id": ATTEMPT, "generation": 1, "preset": "iphone-15",
        "orientation": "landscape", "touch": None}
    entries = audit(ldb, "device_requested")
    assert [(r["email"], r["detail"]) for r in entries] == [
        ("alice@example.com", {"preset": "iphone-15", "orientation": "landscape", "touch": None})]


def test_a_viewer_is_refused_with_409_and_nothing_is_sent(client, ldb, live, relay):
    _in_control(client, ldb)
    refused = switch(client, "root", {"preset": "desktop"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "not_controller"
    assert relay.devices == []
    assert audit(ldb, "device_requested") == []


def test_the_submitter_is_refused_while_someone_else_holds_control(client, ldb, live, relay):
    _ask_for_help(ldb)
    assert client.post(f"/v1/tasks/{TASK}/live/open-to-members", headers=bearer("alice")).status_code == 200
    assert take(client, "root").status_code == 200
    assert switch(client, "alice", {"preset": "desktop"}).status_code == 409
    assert switch(client, "root", {"preset": "desktop"}).status_code == 200
    assert len(relay.devices) == 1


def test_nobody_switches_while_nobody_is_in_control(client, ldb, live, relay):
    """The agent drives, and it picks its device through the gate's ctl
    `device` operation, never through this route."""
    refused = switch(client, "alice", {"preset": "desktop"})
    assert refused.status_code == 409
    assert refused.json()["code"] == "not_controller"
    assert relay.devices == []


def test_another_tenants_member_gets_404_whatever_they_send(client, ldb, live, relay):
    _in_control(client, ldb)
    for body in ({"preset": "desktop"}, {"preset": "galaxy-fold"}, {"width": 4000}, {}):
        assert switch(client, "bob", body).status_code == 404, body
    assert relay.devices == []


def test_can_switch_device_is_the_controllers_only(client, ldb, live):
    def you(user: str) -> dict:
        read = client.get(f"/v1/tasks/{TASK}/live/session", headers=bearer(user))
        assert read.status_code == 200, read.text
        return read.json()["session"]["you"]

    assert you("alice")["can_switch_device"] is False
    _in_control(client, ldb)
    assert you("alice")["can_switch_device"] is True
    assert you("root")["can_switch_device"] is False
    assert client.delete(f"/v1/tasks/{TASK}/live/control", headers=bearer("alice")).status_code == 200
    assert you("alice")["can_switch_device"] is False


def test_a_lapsed_controller_is_refused(client, ldb, live, clock, relay):
    _in_control(client, ldb)
    clock.advance(61)  # past renew_by: control lapsed, settled by this very request
    assert switch(client, "alice", {"preset": "desktop"}).status_code == 409
    assert relay.devices == []


# --------------------------------------------------------------------------
# A preset by name, never a size (invariant 10)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("body", [
    {"preset": "galaxy-fold"},
    {"preset": "desktop", "width": 1920, "height": 1080},
    {"preset": "iphone-15", "scale": 2},
    {"preset": "iphone-15", "user_agent": "x"},
    {"preset": "iphone-15", "orientation": "upside-down"},
    {"preset": "iphone-15", "touch": "yes"},
    {"preset": "desktop", "touch": True},
    {"preset": ""},
    {"preset": "p" * 64},
    {"orientation": "portrait"},
    {},
])
def test_anything_but_a_preset_by_name_is_422_and_nothing_is_sent(client, ldb, live, relay, body):
    _in_control(client, ldb)
    refused = switch(client, "alice", body)
    assert refused.status_code == 422, (body, refused.text)
    assert relay.devices == []
    assert audit(ldb, "device_requested") == []


def test_every_served_preset_and_orientation_is_accepted(client, ldb, live, relay):
    _in_control(client, ldb)
    for preset in live_module.LIVE_PRESETS:
        for orientation in ("portrait", "landscape"):
            body = {"preset": preset["name"], "orientation": orientation}
            assert switch(client, "alice", body).status_code == 200, body
    assert len(relay.devices) == 2 * len(live_module.LIVE_PRESETS)


def test_touch_off_and_on_a_touch_preset_is_accepted(client, ldb, live, relay):
    _in_control(client, ldb)
    assert switch(client, "alice", {"preset": "pixel-8", "touch": False}).status_code == 200
    assert switch(client, "alice", {"preset": "pixel-8", "touch": True}).status_code == 200


# --------------------------------------------------------------------------
# The relay
# --------------------------------------------------------------------------


def test_a_relay_that_refuses_is_503_after_the_choice_is_audited(client, ldb, live, relay):
    _in_control(client, ldb)
    relay.accept_devices = False
    refused = switch(client, "alice", {"preset": "desktop"})
    assert refused.status_code == 503, refused.text
    assert refused.json()["code"] == "live_unavailable"


def test_a_deployment_with_no_device_route_at_the_relay_is_503(ldb, people, groups, objects, clock, live):
    class CloseOnly:
        def __init__(self) -> None:
            self.closed: list[str] = []

        def close_session(self, session_id: str) -> None:
            self.closed.append(session_id)

    bare = make_client(ldb, people, groups, objects, clock, relay=None)
    bare.app.state.live_relay_closer = CloseOnly()
    _ask_for_help(ldb)
    assert take(bare, "alice").status_code == 200
    assert switch(bare, "alice", {"preset": "desktop"}).status_code == 503


def test_what_swarm_api_sends_is_a_device_frame_the_real_relay_hands_the_gate(client, ldb, live, relay):
    """The request swarm-api minted, verified by the REAL relay against the
    session its tunnel ticket defined, becomes exactly one `DEVICE` frame on
    that gate's tunnel, carrying the device and nothing else."""
    _in_control(client, ldb)
    assert switch(client, "alice", {"preset": "ipad-7", "orientation": "landscape",
                                    "touch": False}).status_code == 200
    (request,) = relay.devices
    real = rz.Rendezvous(LIVE_KEY.encode())
    doc = session_doc(ldb)

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

    async def go() -> None:
        await real.open_tunnel(live_module.mint_ticket(LIVE_KEY, {
            "session_id": doc["id"], "tenant_id": "eng", "task_id": TASK,
            "attempt_id": ATTEMPT, "generation": 1, "role": "tunnel",
            "exp": int(time.time()) + 30}), gate)
        assert await real.device_request(request) == doc["id"]
        await real.flush()
        with pytest.raises(rz.Refused) as replayed:
            await real.device_request(request)
        assert replayed.value.reason == "replayed"

    asyncio.run(go())
    frames = [rz.decode_frame(b) for b in gate.binary]
    devices = [(c, json.loads(p)) for op, c, p in frames if op == rz.OP_DEVICE]
    assert devices == [("", {"preset": "ipad-7", "orientation": "landscape", "touch": False})]


# --------------------------------------------------------------------------
# session.device is the gate's report, not the request
# --------------------------------------------------------------------------


def test_the_session_shows_the_new_device_once_the_gate_reports_it(client, ldb, live, relay):
    _in_control(client, ldb)
    session_doc(ldb)["device"] = {"preset": "desktop", "orientation": "landscape", "touch": False}
    answered = switch(client, "alice", {"preset": "iphone-15"})
    assert answered.status_code == 200
    # Sent, not yet applied: the read still says what the gate last reported.
    assert answered.json()["session"]["device"]["preset"] == "desktop"
    report = json.dumps({"type": "report", "origin": None,
                         "device": {"preset": "iphone-15", "orientation": "portrait", "touch": True}})
    relay._frames, relay._refuse = [READY, report], True
    ticket = ticket_for(client, "alice")
    with client.websocket_connect(SOCKET, headers=bearer("alice")) as ws:
        ws.send_text(json.dumps({"ticket": ticket}))
        try:
            while True:
                ws.receive_json()
        except Exception:  # noqa: BLE001 -- the relay double went away (1012)
            pass
    read = client.get(f"/v1/tasks/{TASK}/live/session", headers=bearer("alice")).json()
    assert read["session"]["device"] == {"preset": "iphone-15", "orientation": "portrait", "touch": True}


# --------------------------------------------------------------------------
# swarm-api and the relay state one device-request format
# --------------------------------------------------------------------------


def test_both_sides_state_the_same_device_request_format():
    assert live_module.DEVICE_PURPOSE == rz.DEVICE_DOMAIN
    assert live_module.DEVICE_PURPOSE not in (rz.TICKET_DOMAIN, rz.CLOSE_DOMAIN)
    assert live_module.DEVICE_FIELDS == set(rz._DEVICE_FIELDS)
    assert live_module.DEVICE_SECONDS == rz.DEVICE_TTL_SECONDS
    assert live_module.MAX_PRESET_CHARS == rz.MAX_PRESET_CHARS


def test_a_device_request_is_never_a_ticket_or_a_close_and_neither_is_one():
    claims = {"session_id": "live_a", "tenant_id": "eng", "task_id": TASK, "attempt_id": "a",
              "generation": 1, "preset": "desktop", "orientation": None, "touch": None}
    request = live_module.mint_device(LIVE_KEY, claims, now=time.time())
    key = LIVE_KEY.encode()
    assert rz.verify_device(key, request, now=time.time()).preset == "desktop"
    with pytest.raises(rz.Refused):
        rz.verify_ticket(key, request, now=time.time())
    with pytest.raises(rz.Refused):
        rz.verify_close(key, request, now=time.time())
    with pytest.raises(rz.Refused):
        rz.verify_device(key, live_module.mint_close(LIVE_KEY, "live_a", now=time.time()), now=time.time())
    with pytest.raises(ValueError):
        live_module.mint_device(LIVE_KEY, {**claims, "width": 4000}, now=time.time())


def test_the_relay_closer_posts_device_requests_to_the_relays_device_route(monkeypatch):
    import urllib.request

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

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    sender = live_module.HttpRelayCloser("ws://10.10.0.42:8080", LIVE_KEY)
    assert sender.send_device("a.b") is True
    assert posted == [("http://10.10.0.42:8080/internal/device", b"a.b")]
    # Unreachable: False, never raised.
    monkeypatch.undo()
    assert live_module.HttpRelayCloser("ws://127.0.0.1:9", LIVE_KEY, timeout=0.2).send_device("a.b") is False


def test_the_relay_serves_the_device_route_swarm_api_dials():
    from swarm_live import main as relay_main

    paths = {getattr(r, "path", None) for r in relay_main.create_app(rz.Rendezvous(b"k" * 32)).routes}
    assert "/internal/device" in paths
