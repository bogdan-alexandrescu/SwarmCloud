"""A target's own 503 `no healthy upstream` is named as the target's (UPSTREAM_5XX).

A peer's QA run saw the target's sign-in configuration and current-user calls
answer 503 `no healthy upstream` while the app logged none. No hop of ours
writes that answer (`agent_worker.livegate.qatarget`'s module docstring), and
until now the gate recorded nothing about it, so the run could not tell the
target's edge from SwarmCloud. These hold that the guard sees such an answer
on its own sessions, records it as `qa_target_upstream_5xx` with `source`
`target_edge`, classifies the body without keeping it, and leaves every other
response alone. Driven through the real `QaTarget` and `QaTargetGuard` with a
fake CDP pipe; no Chromium, no network.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
from collections.abc import Mapping
from typing import Any

import pytest

from agent_worker.livegate import qatarget as gate_qt

ORIGIN = "https://app.example.com"
ELSEWHERE = "https://cdn.example.net"
CONFIG_URL = f"{ORIGIN}/api/auth/config"


def _target() -> gate_qt.QaTarget:
    target = gate_qt.QaTarget(lambda e, f: None, wall=lambda: 1_700_000_000.0)
    target.handle({
        "target_id": "qat_" + secrets.token_hex(8), "base_url": f"{ORIGIN}/home",
        "origins": [ORIGIN], "environment": "non_production",
        "rate": {"navigation_ms": 1000, "requests_per_s": 10}, "read_posts": [],
        "exclusions": {"paths": []}, "mutating": False,
        "sign_in": {"kind": "own_login", "login_url": f"{ORIGIN}/login", "signed_in_selector": "#me"},
        # Built at runtime: nothing here is a literal credential.
        "credential": {"username": "qa-" + secrets.token_hex(4) + "@example.com",
                       "password": "pw-" + secrets.token_hex(12)},
    })
    return target


class _Cdp:
    def __init__(self, body: bytes | None) -> None:
        self.body = body
        self.sent: list[tuple[str, dict[str, Any], str | None]] = []

    async def call(self, method: str, params: Mapping[str, Any] | None = None,
                   session_id: str | None = None) -> dict[str, Any]:
        self.sent.append((method, dict(params or {}), session_id))
        if method == "Network.getResponseBody" and self.body is not None:
            return {"body": base64.b64encode(self.body).decode("ascii"), "base64Encoded": True}
        return {}


def _through_the_guard(status: int, url: str, *, body: bytes | None = b"no healthy upstream",
                       remote: str = "127.0.0.1", finished: str = "Network.loadingFinished"):
    target, events = _target(), []
    cdp = _Cdp(body)
    guard = gate_qt.QaTargetGuard(cdp, target, record=lambda e, f: events.append((e, dict(f))),
                                  sleep=lambda s: asyncio.sleep(0))

    async def run() -> None:
        guard.started = True
        guard._sessions["S1"] = gate_qt._Session("T1", "page")
        guard.on_event({"method": "Network.responseReceived", "sessionId": "S1", "params": {
            "requestId": "R1", "type": "Fetch",
            "response": {"url": url, "status": status, "remoteIPAddress": remote,
                         "headers": {"content-type": "text/plain", "content-length": "19"}}}})
        guard.on_event({"method": finished, "sessionId": "S1", "params": {"requestId": "R1"}})
        await asyncio.gather(*list(guard._tasks))

    asyncio.run(run())
    return target, events, cdp


def test_the_guard_enables_network_on_each_of_its_sessions():
    target = _target()
    cdp = _Cdp(None)
    guard = gate_qt.QaTargetGuard(cdp, target, record=lambda e, f: None, sleep=lambda s: asyncio.sleep(0))
    asyncio.run(guard._setup("S1"))
    # MUTATION: drop `Network.enable` from `_setup` and the guard never sees a response.
    assert [m for m, _, s in cdp.sent if s == "S1"][:3] == ["Fetch.enable", "Network.enable",
                                                            "Target.setAutoAttach"]


def test_a_target_503_no_healthy_upstream_is_recorded_as_the_targets_edge():
    target, events, _ = _through_the_guard(503, CONFIG_URL)
    # MUTATION: ignore `Network.responseReceived` in `on_event` and both are empty: the
    # run cannot tell the target's edge from SwarmCloud, which is the defect.
    assert events == [(gate_qt.UPSTREAM_5XX, {"origin": ORIGIN, "status": 503, "source": "target_edge",
                                              "proxied": True, "body": "no_healthy_upstream"})]
    upstream = target.refusals()["upstream"]
    assert upstream == [{"n": 1, "at": 1_700_000_000.0, "origin": ORIGIN, "path": "/api/auth/config",
                         "code": gate_qt.UPSTREAM_5XX, "status": 503, "source": "target_edge",
                         "proxied": True, "body": "no_healthy_upstream"}]
    # Kept apart from what the gate answered itself.
    assert target.refusals()["refusals"] == []
    assert target.refusals()["upstream_dropped"] == 0


def test_a_direct_503_with_another_body_is_recorded_without_the_body():
    secret_word = "body-" + secrets.token_hex(6)
    target, events, _ = _through_the_guard(503, CONFIG_URL, body=secret_word.encode(), remote="203.0.113.9")
    assert events[0][1]["proxied"] is False
    assert events[0][1]["body"] == "other"
    # MUTATION: keep the body in the record and this fails: a body can carry the app's data.
    assert secret_word not in repr(target.refusals())


def test_a_5xx_whose_body_never_loaded_is_still_recorded():
    _, events, cdp = _through_the_guard(502, CONFIG_URL, finished="Network.loadingFailed")
    assert [e for e, _ in events] == [gate_qt.UPSTREAM_5XX]
    assert events[0][1]["body"] == "other"
    assert "Network.getResponseBody" not in [m for m, _, _ in cdp.sent]


@pytest.mark.parametrize("status,url", [(200, CONFIG_URL), (500, CONFIG_URL), (404, CONFIG_URL),
                                        (503, f"{ELSEWHERE}/lib.js")])
def test_other_responses_and_other_origins_are_left_alone(status, url):
    target, events, cdp = _through_the_guard(status, url)
    assert events == []
    assert target.refusals()["upstream"] == []
    assert "Network.getResponseBody" not in [m for m, _, _ in cdp.sent]
