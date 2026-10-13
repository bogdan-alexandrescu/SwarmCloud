"""Taking control keeps the agent's own tab, and a reopened tunnel takes input (LB-CURSOR2, part of #1030).

MEASURED 2026-10-13 02:20-02:23Z (QA playground task_1d1785b21d5b4e1f894e,
att_33d64adb39d54aa0b809, generation 1 -- a first attempt): the agent opened
https://httpbin.org/forms/post and asked for help with `expect` httpbin.org.
After the operator took control, the agent's Chromium had TWO page targets:
the form tab (hidden, unfocused, its field empty) and a new
https://httpbin.org/ tab, visible and focused, that nobody asked for. The
gate's `pause` had opened the hand-off's host "on a fresh page" in a context
of its own, and control, the stream and the input all went there.

And on a RESUMED attempt of task_50f0b9841b9f46db871a (att_1ccd0061355746f5951e,
generation 2, 02:13:54Z) the person in control moved, clicked and typed for
~10 s and nothing reached the page. A resumed attempt is a fresh gate whose
tunnel the worker may have reopened (LB-R6); these tests drive the gate
through a reopened tunnel and hold that a controlling viewer's PointerEvent is
injected and counted there exactly as on the first, and that the release ends
the episode.

The gate runs over the fakes in test_livegate_main.py: no Xvfb, Chromium,
relay or network. Every ticket is built at run time.
"""

from __future__ import annotations

import asyncio
import secrets
import struct
from typing import Any

from agent_worker.livegate import __main__ as gate_main
from agent_worker.livegate import tunnel
from agent_worker.livegate.ctl import CtlClient

from test_livegate_main import FakeRelaySocket, Rig, _started, _until
from test_livegate_main import short_tmp  # noqa: F401  (a pytest fixture, used by name)


def _record_activations(rig: Rig) -> list[str]:
    """The targets the gate brought to the front, read off what it sends Chromium."""
    proxy = rig.gate.proxy
    activated: list[str] = []
    send = proxy._send

    async def recording(message: dict[str, Any]) -> None:
        if message["method"] == "Target.activateTarget":
            activated.append(message["params"]["targetId"])
        await send(message)

    proxy._send = recording
    return activated


def _record_pointer(rig: Rig) -> list[tuple[int, int, int]]:
    seen: list[tuple[int, int, int]] = []

    async def pointer_event(buttons: int, x: int, y: int) -> None:
        seen.append((buttons, x, y))

    rig.injector.pointer_event = pointer_event  # type: ignore[method-assign]
    return seen


def _rfb_viewer(relay: FakeRelaySocket, conn_id: str) -> None:
    """A noVNC-shaped client's handshake, as DATA frames: version, security None, shared."""
    relay.push(tunnel.OP_OPEN, conn_id)
    relay.push(tunnel.OP_DATA, conn_id, b"RFB 003.008\n" + bytes([1]) + bytes([1]))


def _pointer(buttons: int, x: int, y: int) -> bytes:
    return struct.pack(">BBHH", 5, buttons, x, y)


def test_a_hand_off_with_expect_opens_no_new_tab_and_control_is_the_agents_page(short_tmp):  # noqa: F811
    async def body() -> None:
        rig = Rig(short_tmp)
        stop, task = await _started(rig)
        activated = _record_activations(rig)
        gate = rig.gate
        client = CtlClient(rig.cfg.ctl_path)
        await asyncio.to_thread(client.register)
        await gate.open_tunnel({"ticket": "t." + secrets.token_urlsafe(8)})

        paused = await asyncio.to_thread(client.call, "pause", {"request_id": "r1", "expect": "httpbin.org"})
        assert paused == {"paused": True, "fresh_page": False}
        methods = rig.browser.transport.methods
        assert "Target.createBrowserContext" not in methods and "Target.createTarget" not in methods, (
            "taking control opened a tab nobody asked for")

        rig.relay.push(tunnel.OP_OPEN, "v1")
        rig.relay.push(tunnel.OP_CONTROL, "v1")
        await _until(lambda: gate.control.controller == "v1")
        assert gate._episode.target == "PAGE1", "control is the agent's own page"
        # The agent's tab is the one on screen, where input lands.
        await _until(lambda: activated == ["PAGE1"])

        client.close()
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_a_fresh_page_still_opens_when_it_is_asked_for_by_name(short_tmp):  # noqa: F811
    async def body() -> None:
        rig = Rig(short_tmp)
        stop, task = await _started(rig)
        client = CtlClient(rig.cfg.ctl_path)
        await asyncio.to_thread(client.register)
        paused = await asyncio.to_thread(
            client.call, "pause", {"request_id": "r1", "expect": "example.test", "fresh_page": True})
        assert paused == {"paused": True, "fresh_page": True}
        assert "Target.createBrowserContext" in rig.browser.transport.methods
        client.close()
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_a_reopened_tunnel_injects_and_counts_the_controllers_pointer_and_release_ends_it(short_tmp):  # noqa: F811
    async def body() -> None:
        rig = Rig(short_tmp)
        stop, task = await _started(rig)
        gate = rig.gate
        seen = _record_pointer(rig)

        # The first tunnel, with a viewer in control, drops (a relay restart).
        await gate.open_tunnel({"ticket": "t." + secrets.token_urlsafe(8)})
        _rfb_viewer(rig.relay, "v1")
        rig.relay.push(tunnel.OP_CONTROL, "v1")
        await _until(lambda: gate.control.controller == "v1")
        await rig.relay.close()
        await _until(lambda: gate.tunnel.closed and gate.control.controller is None)

        # The worker reopens it (LB-R6); the console reconnects under a new id.
        rig.relay = FakeRelaySocket()
        await gate.open_tunnel({"ticket": "t." + secrets.token_urlsafe(8)})
        _rfb_viewer(rig.relay, "v2")
        await _until(lambda: gate.viewers == 1)
        rig.relay.push(tunnel.OP_CONTROL, "v2")
        await _until(lambda: gate.control.controller == "v2")
        rig.relay.push(tunnel.OP_DATA, "v2", _pointer(0, 40, 30) + _pointer(1, 40, 30) + _pointer(0, 40, 30))
        await _until(lambda: len(seen) == 3)
        assert [b for b, _x, _y in seen] == [0, 1, 0], "a move, a press and a release"
        assert gate.hub.pointer.position == (40, 30)

        # Return control with nothing more typed: the episode ends.
        rig.relay.push(tunnel.OP_CONTROL, "")
        await _until(lambda: gate.control.controller is None and not gate.proxy.paused)
        assert gate.last_episode is not None
        rig.relay.push(tunnel.OP_CLOSE, "v2")
        await _until(lambda: gate.viewers == 0)
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_a_grant_that_never_answers_is_refused_and_the_stream_goes_on(short_tmp, monkeypatch):  # noqa: F811
    """The relay's frames are handled one at a time: a grant stuck on a CDP
    round used to hold every viewer's stream behind it."""
    monkeypatch.setattr(gate_main, "CONTROL_GRANT_SECONDS", 0.2)

    async def body() -> None:
        rig = Rig(short_tmp)
        stop, task = await _started(rig)
        gate = rig.gate

        async def never(target_id: str) -> Any:
            await asyncio.Event().wait()

        gate.proxy.take_control = never  # type: ignore[method-assign]
        await gate.open_tunnel({"ticket": "t." + secrets.token_urlsafe(8)})
        rig.relay.push(tunnel.OP_CONTROL, "v1")
        _rfb_viewer(rig.relay, "v2")
        await _until(lambda: any(f[:2] == (tunnel.OP_DATA, "v2") for f in rig.relay.frames), timeout=3)
        actions = [f for f in rig.relay.frames if f[0] == tunnel.OP_ACTION]
        assert any(b'"grant_timeout"' in f[2] for f in actions)
        assert gate.control.controller is None and not gate.proxy.paused
        stop.set()
        assert await task == 0

    asyncio.run(body())
