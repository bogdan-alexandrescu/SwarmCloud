"""The gate's controller is the link the person's input comes from (LB-CURSOR2, part of #1030).

swarm-api decides control per person and sends a control ticket on EVERY link
of theirs -- a second console tab, a hidden one, a live thumbnail. Each used to
take the gate's single controller slot as its ticket arrived, so the link
polled last won, and the canvas the person was moving and typing on became a
viewer whose input the gate discarded. MEASURED 2026-10-13 on
task_1d1785b21d5b4e1f894e: a controller's input from a hidden console tab
counted 0 pointer events and 0 keys; from the visible one, 2 and 4.

And a control message the relay refuses now says why, so the console can say
it too (`{"type": "refused", "reason": "control", "cause": ...}`).

Every key and ticket is built at runtime, as in test_relay.py.
"""

from __future__ import annotations

import json
import struct

from test_relay import SESSION, Endpoint, build, run, ticket

from swarm_live.rendezvous import OP_CONTROL, OP_DATA

PERSON = "viewer@example.test"


def _pointer() -> bytes:
    return struct.pack(">BBHH", 5, 0, 10, 10)


def _controls(gate: Endpoint) -> list[str]:
    return [conn for op, conn, _ in gate.frames() if op == OP_CONTROL]


async def _grant(relay, key, clock, viewer, role: str = "control", email: str = PERSON) -> None:
    await relay.viewer_text(viewer, json.dumps({"type": "control", "ticket": ticket(
        key, clock, role, email=email)}))


def test_the_link_the_person_types_on_takes_control_from_their_other_link():
    key, clock, relay = build()
    gate = Endpoint()

    async def go():
        tunnel = await relay.open_tunnel(ticket(key, clock), gate)
        visible = await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        hidden = await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        await _grant(relay, key, clock, visible)
        # The hidden tab's proxy polls later: it is granted, but does not take the slot.
        await _grant(relay, key, clock, hidden)
        assert relay.controller(SESSION) == visible.conn_id
        await relay.from_viewer(visible, _pointer())
        assert relay.controller(SESSION) == visible.conn_id
        # The person moves to the other tab: its first input takes control there.
        await relay.from_viewer(hidden, _pointer())
        assert relay.controller(SESSION) == hidden.conn_id
        await relay.flush()
        return tunnel, visible, hidden

    tunnel, visible, hidden = run(go())
    frames = gate.frames()
    assert _controls(gate) == [visible.conn_id, hidden.conn_id]
    # The gate hears CONTROL before the input it grants.
    i = frames.index((OP_CONTROL, hidden.conn_id, b""))
    assert frames[i + 1] == (OP_DATA, hidden.conn_id, _pointer())


def test_a_link_without_a_control_ticket_never_takes_control_by_typing():
    key, clock, relay = build()
    gate = Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        mine = await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        watcher = await relay.open_viewer(ticket(key, clock, "viewer", email="w@example.test"), Endpoint())
        await _grant(relay, key, clock, mine)
        await relay.from_viewer(watcher, _pointer())
        await relay.flush()
        return mine

    mine = run(go())
    assert relay.controller(SESSION) == mine.conn_id
    assert _controls(gate) == [mine.conn_id]


def test_a_release_lets_go_on_every_link_of_the_person_and_ends_control_at_the_gate():
    key, clock, relay = build()
    gate = Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        a = await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        b = await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        await _grant(relay, key, clock, a)
        await _grant(relay, key, clock, b)
        # The hidden link's release arrives first; control still ends.
        await _grant(relay, key, clock, b, "release")
        assert relay.controller(SESSION) is None
        # The other link's input no longer takes it back.
        await relay.from_viewer(a, _pointer())
        await relay.flush()
        return a

    a = run(go())
    assert _controls(gate) == [a.conn_id, ""]


def test_when_the_controllers_link_leaves_another_granted_link_of_theirs_takes_over():
    key, clock, relay = build()
    gate = Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        a = await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        b = await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        await _grant(relay, key, clock, a)
        await _grant(relay, key, clock, b)
        await relay.close_viewer(a)
        await relay.flush()
        return a, b

    a, b = run(go())
    assert relay.controller(SESSION) == b.conn_id
    assert _controls(gate) == [a.conn_id, b.conn_id]


def test_a_control_ticket_for_another_session_is_refused_with_its_cause():
    key, clock, relay = build()
    gate, link = Endpoint(), Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        viewer = await relay.open_viewer(ticket(key, clock, "viewer"), link)
        # An earlier attempt's session: the ticket names it, the link is this one's.
        await relay.viewer_text(viewer, json.dumps({"type": "control", "ticket": ticket(
            key, clock, "control", email=PERSON, session_id="ls_earlier")}))
        await relay.flush()

    run(go())
    assert relay.controller(SESSION) is None
    assert _controls(gate) == []
    assert json.loads(link.text[-1]) == {"type": "refused", "reason": "control", "cause": "wrong_session"}
