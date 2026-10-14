"""swarm-api's end of control reaches the gate through the relay (CONTROL-LAPSE, part of #1030).

MEASURED 2026-10-13 (task e8cb...): the console's control lapsed at 15:11:12Z
(swarm-api recorded control returned) and the gate held it until a relay
restart at 16:03:47Z; the agent got 72 `cdp_connection_refused` in 54
minutes. A control ticket travels on the person's own viewer link, and that
link was gone: the relay had forgotten the controller when it left, and the
gate had not, and nothing else could tell it.

So swarm-api POSTs a control request to `/internal/control` on every end of
control and every renewal. These tests hold the relay's half: a revoke sends
the gate `CONTROL` with nobody in control and the cause -- even when the relay
had already forgotten the controller -- and is idempotent; a renewal re-sends
`CONTROL` naming the controller (the gate's lease); a request is checked like
a ticket.

Every key and ticket is built at runtime, as in test_relay.py.
"""

from __future__ import annotations

import json

import pytest
from test_relay import SESSION, Endpoint, build, run, ticket

from swarm_live import main as relay_main
from swarm_live import rendezvous as rz
from swarm_live.rendezvous import OP_CONTROL, Refused

PERSON = "viewer@example.test"
OTHER = "other@example.test"


def _control(key, clock, action: str = "revoke", cause: str | None = None, **over) -> str:
    if cause is None:
        cause = "renew_lapsed" if action == "revoke" else ""
    fields = dict(session_id=SESSION, tenant_id="eng", task_id="task_a1", attempt_id="att_a1",
                  generation=3, email=PERSON, action=action, cause=cause, now=clock())
    fields.update(over)
    return rz.mint_control(key, **fields)


def _controls(gate: Endpoint) -> list[tuple[str, bytes]]:
    return [(conn, payload) for op, conn, payload in gate.frames() if op == OP_CONTROL]


async def _grant(relay, key, clock, viewer, email: str = PERSON) -> None:
    await relay.viewer_text(viewer, json.dumps({"type": "control", "ticket": ticket(
        key, clock, "control", email=email)}))


def test_a_revoke_reaches_the_gate_after_the_controllers_link_left():
    """The e8cb shape: the link left, the relay forgot, the gate did not."""
    key, clock, relay = build()
    gate = Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        viewer = await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        await _grant(relay, key, clock, viewer)
        await relay.close_viewer(viewer)
        assert relay.controller(SESSION) is None, "the relay forgets the link"
        await relay.flush()
        before = list(_controls(gate))
        assert before == [(viewer.conn_id, b"")], "the gate was never told control ended"
        answer = await relay.control_request(_control(key, clock))
        await relay.flush()
        return answer, before

    answer, before = run(go())
    assert answer == (SESSION, "revoke", True)
    assert _controls(gate)[len(before):] == [("", b"renew_lapsed")]


def test_a_revoke_takes_every_grant_of_the_person_and_is_idempotent():
    key, clock, relay = build()
    gate = Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        one = await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        two = await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        await _grant(relay, key, clock, one)
        await _grant(relay, key, clock, two)
        assert relay.controller(SESSION) == one.conn_id
        await relay.control_request(_control(key, clock, cause="idle"))
        assert relay.controller(SESSION) is None
        # Again: nothing left at the relay, and the gate's revoke of nobody is a no-op.
        await relay.control_request(_control(key, clock, cause="idle"))
        assert relay.controller(SESSION) is None
        await relay.flush()
        return one

    one = run(go())
    assert _controls(gate) == [(one.conn_id, b""), ("", b"idle"), ("", b"idle")]


def test_a_revoke_of_one_person_leaves_another_persons_control_alone():
    key, clock, relay = build()
    gate = Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        mine = await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        theirs = await relay.open_viewer(ticket(key, clock, "viewer", email=OTHER), Endpoint())
        await _grant(relay, key, clock, theirs, email=OTHER)
        answer = await relay.control_request(_control(key, clock, cause="preempted"))
        await relay.flush()
        return mine, theirs, answer

    mine, theirs, answer = run(go())
    assert relay.controller(SESSION) == theirs.conn_id
    assert answer == (SESSION, "revoke", False)
    assert _controls(gate) == [(theirs.conn_id, b"")]


def test_a_renewal_names_the_controller_again_and_only_for_that_person():
    key, clock, relay = build()
    gate = Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        viewer = await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        nobody = await relay.control_request(_control(key, clock, "renew"))
        await _grant(relay, key, clock, viewer)
        renewed = await relay.control_request(_control(key, clock, "renew"))
        other = await relay.control_request(_control(key, clock, "renew", email=OTHER))
        await relay.flush()
        return viewer, nobody, renewed, other

    viewer, nobody, renewed, other = run(go())
    assert nobody == (SESSION, "renew", False)
    assert renewed == (SESSION, "renew", True)
    assert other == (SESSION, "renew", False)
    assert _controls(gate) == [(viewer.conn_id, b""), (viewer.conn_id, b"")]
    assert relay.controller(SESSION) == viewer.conn_id


def test_a_control_request_is_checked_like_a_ticket():
    key, clock, relay = build()

    async def go():
        with pytest.raises(Refused) as e:
            await relay.control_request(_control(key, clock))
        assert e.value.reason == "no_tunnel"
        await relay.open_tunnel(ticket(key, clock), Endpoint())
        with pytest.raises(Refused) as e:
            await relay.control_request(_control(key, clock, generation=2))
        assert e.value.reason == "wrong_generation"
        raw = _control(key, clock)
        await relay.control_request(raw)
        with pytest.raises(Refused) as e:
            await relay.control_request(raw)
        assert e.value.reason == "replayed"
        # A ticket is never a control request, nor a control request a ticket.
        with pytest.raises(Refused):
            await relay.control_request(ticket(key, clock, "release", email=PERSON))
        with pytest.raises(Refused):
            rz.verify_ticket(key, _control(key, clock), now=clock())
        for bad in (dict(action="grant"), dict(cause="Not A Code"), dict(cause="")):
            with pytest.raises(Refused) as e:
                await relay.control_request(_control(key, clock, **bad))
            assert e.value.reason == "malformed"
        with pytest.raises(Refused) as e:
            await relay.control_request(_control(key, clock, "renew", cause="idle"))
        assert e.value.reason == "malformed"
        with pytest.raises(Refused) as e:
            await relay.control_request(_control(key, clock, ttl=3600))
        assert e.value.reason == "lifetime"

    run(go())


def test_the_relay_serves_the_control_route():
    paths = {getattr(r, "path", None) for r in relay_main.create_app(rz.Rendezvous(b"k" * 32)).routes}
    assert "/internal/control" in paths
