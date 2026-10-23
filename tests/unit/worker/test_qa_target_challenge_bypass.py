"""A QA target's challenge bypass, the worker's and the gate's half
(docs/design/visual-qa-any-app.md §2.5; lane CAPTCHA-BYPASS, part of #2058).

BGD, 2026-10-23: a QA target may carry a challenge bypass its owner
configures, and only the gate applies it, only on the target's own origins.
What these tests hold:

* the worker hands the bypass to the gate in `qa_sign_in`'s args, exactly as
  it hands the password, registers it with the redaction, and drops its copy;
  nothing it logs or writes to the spool carries the value;
* on the gate's own sessions (the Request-stage pause `QaTargetGuard` already
  makes), a HEADER bypass reaches a request to the target's host and to its
  approved extra origin, and never one to any other origin;
* a COOKIE bypass is planted by the gate, host-only, `HttpOnly` and `Secure`,
  for each approved origin and no other;
* the value is a scrub needle, like the password: it reaches no message the
  agent receives, and no record;
* the gate refuses a reserved header name itself (its list is swarm-api's).

Driven through the real `QaTarget`, `QaTargetGuard` and `CdpProxy` against
test_qa_target_signin's scripted Chromium: what "reached the network" is a
Request-stage pause the gate continued, with the headers it continued it with.
No Chromium, network or cloud credential; every value is built at run time.
"""

from __future__ import annotations

import base64
import json
import secrets
from typing import Any

import pytest

from agent_worker import qa_target
from agent_worker.livegate import qatarget as gate_qt
from swarm_api import qaspec as api_qaspec

import test_livegate_qa_identity as qi
import test_qa_target_gate_policy as policy
import test_qa_target_signin as signin
from test_qa_target_signin import LOGIN_ORIGIN, ORIGIN, Rig, run, tmp  # noqa: F401  (tmp is a fixture)

ELSEWHERE = "https://pay.example.net"
NAME = "X-QA-Bypass"


def _value() -> str:
    return "qa-bypass-" + secrets.token_hex(16)


def _bypass(value: str, kind: str = "header", name: str = NAME) -> dict[str, str]:
    return {"kind": kind, "name": name, "value": value}


async def _rig(tmp: Any, value: str, kind: str = "header") -> tuple[Rig, str]:
    return await policy._rig(tmp, challenge_bypass=_bypass(value, kind, NAME if kind == "header" else "qa_bypass"))


def _carried(rig: Rig, url: str) -> list[str]:
    """The values the request to `url` left with under the bypass's name."""
    return [v for u, headers in rig.fake.network if u == url
            for n, v in headers.items() if n.lower() == NAME.lower()]


# --------------------------------------------------------------------------
# The worker: hands it over and forgets it
# --------------------------------------------------------------------------


def test_handoff_args_moves_the_bypass_into_the_gates_args():
    value = _value()
    answer = {"target_id": signin._target_id(), "base_url": signin.BASE_URL, "origins": [ORIGIN],
              "sign_in": {"kind": "none"}, "challenge_bypass": _bypass(value)}
    args = qa_target.handoff_args(answer)
    # MUTATION: drop the bypass from `handoff_args` and the gate never adds it.
    assert args["challenge_bypass"] == _bypass(value)
    assert "challenge_bypass" not in qa_target.handoff_args({**answer, "challenge_bypass": None})
    for broken in ({"kind": "header", "name": NAME}, {"kind": "header", "name": NAME, "value": 7}, "x"):
        # MUTATION: pass a malformed bypass through `handoff_args` and the gate is handed half of one.
        with pytest.raises(ValueError):
            qa_target.handoff_args({**answer, "challenge_bypass": broken})


def test_the_worker_hands_the_bypass_to_the_gate_and_drops_its_copy(tmp_path, db, monkeypatch):
    value = _value()
    original = signin.TargetRoutes.call

    def call(self, method, path, *, body=None, headers=None):
        status, answer = original(self, method, path, body=body, headers=headers)
        if path.endswith("/qa-target") and status == 200:
            answer = {**answer, "challenge_bypass": _bypass(value)}
        return status, answer

    monkeypatch.setattr(signin.TargetRoutes, "call", call)
    world = signin._world(tmp_path, db, own_login=False)
    (handed,) = world.gate.handoffs()
    # MUTATION: keep the worker's copy in QaTargetSignIn._ask and this still holds the value.
    assert handed["challenge_bypass"] == {}, "the worker's copy is cleared once the gate answered"
    assert world.gate.target.bypass_header(f"{ORIGIN}/home") == (NAME, value)
    # MUTATION: skip register_secret for the bypass in QaTargetSignIn._ask and the redaction never learns it.
    assert world.log.secrets == [value]
    # MUTATION: log the answer (or the hand-off args) in QaTargetSignIn._ask and this fails.
    assert value not in signin._everything_readable(world)


# --------------------------------------------------------------------------
# The gate: the header on the target's origins and nowhere else
# --------------------------------------------------------------------------


def test_the_header_reaches_the_target_and_its_extra_origin_and_never_a_third(tmp):
    async def body() -> None:
        value = _value()
        rig, session = await _rig(tmp, value)
        own = f"{ORIGIN}/api/list"
        login = f"{LOGIN_ORIGIN}/realms/qa/auth"
        third = f"{ELSEWHERE}/logo.png"
        lookalike = "https://app.example.com.example.net/x"
        for url in (own, login, third, lookalike):
            policy._pause(rig, session, url)
        await rig.settle()
        # MUTATION: skip the bypass in QaTargetGuard._paused and the target never sees it.
        assert _carried(rig, own) == [value]
        # MUTATION: add it to the base origin only and the approved extra origin's challenge stays.
        assert _carried(rig, login) == [value]
        # MUTATION: add it to every paused request (as Network.setExtraHTTPHeaders would) and these carry it.
        assert policy._sent(rig, third) and _carried(rig, third) == []
        assert policy._sent(rig, lookalike) and _carried(rig, lookalike) == []
        assert not any(value in json.dumps(h) for u, h in rig.fake.network if not u.startswith((ORIGIN, LOGIN_ORIGIN)))
        # The request's own headers are kept beside it.
        assert next(h for u, h in rig.fake.network if u == own)["Accept"] == "*/*"
        # MUTATION: use Network.setExtraHTTPHeaders for the bypass and it reaches every origin.
        assert rig.fake.sent("Network.setExtraHTTPHeaders") == []
        await rig.fake.stop()

    run(body)


def test_a_header_of_the_same_name_is_replaced_not_doubled(tmp):
    async def body() -> None:
        value = _value()
        rig, session = await _rig(tmp, value)
        request_id = rig.fake._next("R")
        params = {"requestId": request_id, "resourceType": "XHR", "networkId": rig.fake._next("N"), "frameId": "PAGE1",
                  "request": {"url": f"{ORIGIN}/x", "method": "GET", "headers": {NAME.lower(): "agent-made"}}}
        rig.fake.paused[request_id] = (session, params)
        rig.fake.emit("Fetch.requestPaused", params, session)
        await rig.settle()
        # MUTATION: append without dropping a same-named header and the agent's own value travels too.
        assert _carried(rig, f"{ORIGIN}/x") == [value]
        await rig.fake.stop()

    run(body)


def test_after_the_wipe_no_request_carries_it(tmp):
    async def body() -> None:
        value = _value()
        rig, session = await _rig(tmp, value)
        policy._pause(rig, session, f"{ORIGIN}/before")
        await rig.settle()
        # CONTROL: held, the request carries it.
        assert _carried(rig, f"{ORIGIN}/before") == [value]
        rig.target.wipe()
        policy._pause(rig, session, f"{ORIGIN}/after")
        await rig.settle()
        # MUTATION: keep the value through QaTarget.wipe and it outlives the attempt's hold.
        assert policy._sent(rig, f"{ORIGIN}/after") and _carried(rig, f"{ORIGIN}/after") == []
        await rig.fake.stop()

    run(body)


# --------------------------------------------------------------------------
# The gate: the cookie, host-only, for the approved origins alone
# --------------------------------------------------------------------------


def test_a_cookie_bypass_is_planted_host_only_for_each_approved_origin(tmp):
    async def body() -> None:
        value = _value()
        rig, session = await _rig(tmp, value, kind="cookie")
        planted = [c for m in rig.fake.sent("Storage.setCookies") for c in m["params"]["cookies"]]
        # MUTATION: skip QaTargetGuard's cookie in `start` and the challenge stays.
        assert {c["url"] for c in planted} == {f"{ORIGIN}/", f"{LOGIN_ORIGIN}/"}
        for cookie in planted:
            assert cookie["name"] == "qa_bypass" and cookie["value"] == value
            # MUTATION: drop httpOnly and the page's own script (and the agent's evaluate) reads it.
            assert cookie["httpOnly"] is True and cookie["secure"] is True
            # MUTATION: give it a domain and every subdomain of the host is sent it.
            assert "domain" not in cookie
        # Planted through the gate's own pipe, on no agent session.
        assert all("sessionId" not in m for m in rig.fake.sent("Storage.setCookies"))
        # A cookie bypass adds no header.
        policy._pause(rig, session, f"{ORIGIN}/api/list")
        await rig.settle()
        assert not any(value in json.dumps(h) for _, h in rig.fake.network)
        await rig.fake.stop()

    run(body)


# --------------------------------------------------------------------------
# The scrub: the value reaches nothing the agent receives
# --------------------------------------------------------------------------


def test_the_value_reaches_no_agent_bound_message_and_no_record(tmp):
    async def body() -> None:
        value = _value()
        rig, _ = await _rig(tmp, value)
        link = qi.FakeLink()
        conn = await rig.proxy.connect_agent(link)
        assert conn is not None
        session = conn.browser_session
        # What Chromium tells a client that watches the network: the sent headers.
        rig.fake.emit("Network.requestWillBeSentExtraInfo",
                      {"requestId": "N1", "headers": {NAME: value, "Cookie": f"qa_bypass={value}"}}, session)
        # A page that echoes the request's headers back.
        rig.fake.emit("Runtime.consoleAPICalled", {"args": [{"value": f"headers: {NAME}: {value}"}]}, session)
        await rig.settle()
        echoed = json.dumps({"headers": {NAME: value}}).encode()
        rig.fake.results["Network.getResponseBody"] = {"body": base64.b64encode(echoed).decode(),
                                                       "base64Encoded": True}
        await rig.proxy.on_agent_message(conn, {"id": 9, "method": "Network.getResponseBody",
                                                "params": {"requestId": "N1"}})
        await rig.settle()
        text = json.dumps(link.sent, ensure_ascii=False)
        # MUTATION: leave the bypass out of QaTarget's needles and the agent reads it here.
        assert value not in text and gate_qt.PLACEHOLDER in text
        reply = next(m for m in link.sent if m.get("id") == 9)
        # Bytes, not text: the scrub may have replaced a base64 run of it before decoding the body.
        assert value.encode() not in base64.b64decode(reply["result"]["body"])
        # MUTATION: record the bypass's value in qa_target_set and this fails.
        assert value not in json.dumps(rig.records, ensure_ascii=False)
        assert value not in json.dumps(rig.target.status())
        await rig.fake.stop()

    run(body)


# --------------------------------------------------------------------------
# The gate's own checks
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Authorization", "COOKIE", "host", "Proxy-Authorization", "x bad"])
def test_the_gate_refuses_a_reserved_or_malformed_name(name):
    target = gate_qt.QaTarget(lambda e, f: None)
    # MUTATION: drop the name check in livegate.qatarget._parse_bypass and these are taken.
    with pytest.raises(gate_qt.QaTargetRefused) as refused:
        target.handle(policy._public(challenge_bypass=_bypass(_value(), name=name)))
    assert refused.value.code == "challenge_bypass"
    assert target.bypass_header(f"{ORIGIN}/") is None


def test_the_gates_reserved_names_are_swarm_apis():
    # MUTATION: drop a name from livegate.qatarget.RESERVED_BYPASS_HEADERS and the gate takes what swarm-api refuses.
    assert gate_qt.RESERVED_BYPASS_HEADERS == api_qaspec.RESERVED_BYPASS_HEADERS
    assert gate_qt.RESERVED_BYPASS_HEADER_PREFIXES == api_qaspec.RESERVED_BYPASS_HEADER_PREFIXES
    assert {"authorization", "cookie", "host"} <= gate_qt.RESERVED_BYPASS_HEADERS
    assert "proxy-" in gate_qt.RESERVED_BYPASS_HEADER_PREFIXES
