"""The gate writes what the agent did to the recording's action log (lane
LB-V3, part of #1030; design §7).

MEASURED 2026-10-12 on SwarmCloud: the theater's timeline held no agent line
at all. §7 says the action log holds "one line per agent CDP method the proxy
forwarded (`Page.navigate` with the redacted URL; `Input.insertText` as a
length, never text)", and TheaterTimeline.tsx draws them -- but the proxy
never handed a line to anyone: the only `OP_ACTION` frames the gate sent were
`granted`, `refused` and `ended`. A device the gate refused was in its own
record and nowhere a person could read it.

Every value that could look like a credential is built at runtime.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

from agent_worker.livegate import cdpproxy, tunnel
from test_live_cdp_proxy import World, run
from test_livegate_main import Rig, _device_frame, _started, _until, short_tmp  # noqa: F401


def _secret() -> str:
    return "pw-" + secrets.token_hex(8)


# --------------------------------------------------------------------------
# One line per method: names, a redacted URL, a length -- nothing else
# --------------------------------------------------------------------------


def test_a_navigation_url_loses_its_query_fragment_and_user():
    value = _secret()
    url = f"https://someone:{value}@github.com:8443/login/oauth?code={value}#t={value}"
    assert cdpproxy.action_url(url) == "https://github.com:8443/login/oauth"
    assert cdpproxy.action_url("https://github.com") == "https://github.com/"
    for not_a_page in ("about:blank", "data:text/html,hi", "javascript:alert(1)", "file:///etc/passwd",
                       "https://", 12, None, "https://h:99999/"):
        assert cdpproxy.action_url(not_a_page) is None, not_a_page


def test_typed_text_is_a_length_and_no_other_parameter_is_kept():
    typed = _secret()
    line = cdpproxy.action_line("Input.insertText", {"text": typed})
    assert line == {"kind": "cdp", "method": "Input.insertText", "length": len(typed)}
    evaluated = cdpproxy.action_line("Runtime.evaluate", {"expression": f"login('{typed}')"})
    assert evaluated == {"kind": "cdp", "method": "Runtime.evaluate"}
    assert typed not in json.dumps([line, evaluated])
    assert cdpproxy.action_line(f"Not a method {typed}", {})["method"] == "unknown"


def test_the_proxy_hands_every_forwarded_and_refused_method_to_the_log(tmp_path):
    typed = _secret()

    async def body() -> None:
        w = World(tmp_path)
        lines: list[dict[str, Any]] = []
        w.proxy._on_action = lines.append
        await w.connect()
        page = await w.attach_page()
        await w.send("Page.navigate", {"url": f"https://github.com/login?return_to={typed}"}, page)
        await w.send("Input.insertText", {"text": typed}, page)
        await w.send("Browser.close", {})
        await w.send("Page.navigate", {"url": "https://example.com/"}, "S-not-mine")
        by_method = [(x["method"], x.get("outcome"), x.get("cause")) for x in lines]
        assert ("Page.navigate", None, None) in by_method
        assert ("Input.insertText", None, None) in by_method
        assert ("Browser.close", "refused", "method_policy") in by_method
        assert ("Page.navigate", "refused", "not_its_session") in by_method
        navigate = next(x for x in lines if x["method"] == "Page.navigate" and "outcome" not in x)
        assert navigate["url"] == "https://github.com/login"
        assert next(x for x in lines if x["method"] == "Input.insertText")["length"] == len(typed)
        assert typed not in json.dumps(lines)
        assert all(set(x) <= {"kind", "method", "url", "length", "outcome", "cause"} for x in lines)
        await w.chromium.stop()

    run(body)


def test_without_a_listener_the_proxy_forwards_as_before(tmp_path):
    """The control: `on_action` is optional, and its absence changes nothing."""

    async def body() -> None:
        w = World(tmp_path)
        await w.connect()
        page = await w.attach_page()
        request = await w.send("Page.navigate", {"url": "https://example.com/"}, page)
        assert "error" not in w.agent.response(request)
        assert w.chromium.received[-1]["method"] == "Page.navigate"
        await w.chromium.stop()

    run(body)


# --------------------------------------------------------------------------
# The gate sends them over its tunnel, with a refused device
# --------------------------------------------------------------------------


def _actions(rig: Rig) -> list[dict[str, Any]]:
    return [json.loads(f[2]) for f in rig.relay.frames if f[0] == tunnel.OP_ACTION]


def test_the_gate_sends_the_proxys_lines_over_its_tunnel(short_tmp):  # noqa: F811
    async def body() -> None:
        rig = Rig(short_tmp)
        stop, task = await _started(rig)
        gate = rig.gate
        # Before a tunnel there is nothing to record into.
        gate.proxy._on_action(cdpproxy.action_line("Page.reload", {}))
        await gate.open_tunnel({"ticket": "t." + secrets.token_urlsafe(8)})
        gate.proxy._on_action(cdpproxy.action_line("Page.navigate", {"url": "https://example.test/a?b=c"}))
        await _until(lambda: any(a.get("kind") == "cdp" for a in _actions(rig)))
        cdp = [a for a in _actions(rig) if a.get("kind") == "cdp"]
        assert cdp == [{"kind": "cdp", "method": "Page.navigate", "url": "https://example.test/a"}]
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_a_device_the_gate_refuses_is_on_the_action_log(short_tmp):  # noqa: F811
    async def body() -> None:
        rig = Rig(short_tmp)
        stop, task = await _started(rig)
        await rig.gate.open_tunnel({"ticket": "t." + secrets.token_urlsafe(8)})
        rig.relay.push(tunnel.OP_DEVICE, "", _device_frame(preset="desktop", touch=True))
        await _until(lambda: any(a.get("event") == "device_refused" for a in _actions(rig)))
        refused = [a for a in _actions(rig) if a.get("event") == "device_refused"]
        assert refused == [{"kind": "control", "event": "device_refused", "cause": "no_touch"}]
        # The control: a device the gate applies writes no refusal.
        rig.relay.push(tunnel.OP_DEVICE, "", _device_frame(preset="pixel-8", orientation="landscape"))
        await _until(lambda: rig.gate.devices.device.name == "pixel-8")
        await asyncio.sleep(0.05)
        assert len([a for a in _actions(rig) if a.get("event") == "device_refused"]) == 1
        stop.set()
        assert await task == 0

    asyncio.run(body())
