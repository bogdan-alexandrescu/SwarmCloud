"""The gate raises the hand-off itself when a CAPTCHA appears on the agent's page (CAPTCHA-HANDOFF; part of #2058).

docs/design/live-browser.md §4.1. Owner decision, BGD 2026-10-23: "when the
gate sees a challenge on an agent's page it raises the hand-off itself; the
console bell is the only channel". The gate detects with the lists it already
has (mask.py `SHOWN_IN_CONTROL`, read by `challenge_host` and tabs.py), never
a new one, and asks the worker over ctl.sock: `status` carries the request
(`challenge`), and the worker's `pause` for it takes it off.

What is held here, against the gate's own mask driven by a fake CDP session:

* a challenge frame on the agent's tab is exactly one request, reason
  "CAPTCHA on <host>" (host only), `expect` that host;
* a second challenge frame on the same page load raises none; a new page
  load may raise again;
* a top frame on a challenge host (any path, as tabs.py reads it) is one too;
* a challenge on the gate's own QA sign-in tab raises no hand-off: the
  sign-in still ends with `qa_sign_in_challenge` (test_qa_target_signin.py);
* nothing is raised while a person already holds the browser.

Every case builds its frames by hand: no browser, no display.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_worker import handoff as handoff_mod
from agent_worker.livegate import __main__ as gate_main
from agent_worker.livegate import qatarget as gate_qt
from agent_worker.livegate import tabs as gate_tabs
from agent_worker.livegate.mask import MaskCdp

from test_livegate_main import _config, short_tmp  # noqa: F401  (a pytest fixture, used by name)

TOP = "TARGET-1"
PAGE = "https://shop.example.com/cart?item=42#pay"
ANCHOR = "https://www.google.com/recaptcha/api2/anchor?k=site-key-under-test"
BFRAME = "https://www.google.com/recaptcha/api2/bframe?k=site-key-under-test"


class FakeSession:
    """The gate's own CDP session on the agent's page: a frame tree, and nothing else."""

    def __init__(self, url: str = PAGE) -> None:
        self.url = url

    async def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": TOP, "url": self.url}, "childFrames": []}}
        if method == "DOM.getFrameOwner":
            return {"backendNodeId": 7}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj-1"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"type": "number", "value": 3}}
        return {}


class Records(list):
    def __call__(self, event: str, fields: Any) -> None:
        self.append((event, dict(fields)))

    def named(self, event: str) -> list[dict[str, Any]]:
        return [f for e, f in self if e == event]


def _gate(tmp) -> tuple[gate_main.LiveGate, Records]:
    records = Records()
    return gate_main.LiveGate(_config(tmp), record=records), records


async def _followed(gate: gate_main.LiveGate, url: str = PAGE) -> MaskCdp:
    """The page follower's install on the agent's tab (`PageFollower.follow`)."""
    mask = MaskCdp(gate.mask_state, FakeSession(url))
    await mask.install()
    return mask


async def _frame(mask: MaskCdp, frame_id: str, url: str) -> None:
    await mask.on_event("Page.frameAttached", {"frameId": frame_id, "parentFrameId": TOP})
    await mask.on_event("Page.frameNavigated", {"frame": {"id": frame_id, "parentId": TOP, "url": url}})


def test_a_challenge_frame_on_the_agents_tab_raises_exactly_one_request(short_tmp):  # noqa: F811
    async def body() -> None:
        gate, records = _gate(short_tmp)
        mask = await _followed(gate)
        # MUTATION: report a challenge before any frame shows one and this fails.
        assert gate.status()["challenge"] is None
        await _frame(mask, "anchor", ANCHOR)
        challenge = gate.status()["challenge"]
        # MUTATION: put the page's path or query in the reason and this fails.
        assert challenge["reason"] == "CAPTCHA on shop.example.com"
        # MUTATION: name the challenge frame's host (www.google.com) as `expect` and this fails.
        assert challenge["expect"] == "shop.example.com"
        # MUTATION: an id the worker's REQUEST_ID refuses and the worker drops it.
        assert handoff_mod.REQUEST_ID.fullmatch(challenge["request_id"])
        # MUTATION: drop the record in LiveGate._on_challenge and the event log never hears of it.
        assert records.named("handoff_challenge_raised") == [{"host": "shop.example.com"}]
        assert "site-key" not in repr(records)

    asyncio.run(body())


def test_a_second_frame_on_the_same_page_raises_none_and_a_new_page_load_may(short_tmp):  # noqa: F811
    async def body() -> None:
        gate, records = _gate(short_tmp)
        mask = await _followed(gate)
        await _frame(mask, "anchor", ANCHOR)
        first = gate.status()["challenge"]["request_id"]
        await _frame(mask, "bframe", BFRAME)
        # A same-document navigation is the same page load.
        await mask.on_event("Page.navigatedWithinDocument", {"frameId": TOP, "url": PAGE + "-2"})
        await mask.on_event("Page.navigatedWithinDocument", {"frameId": "anchor", "url": ANCHOR + "&x=1"})
        # MUTATION: raise once per challenge frame instead of once per page load and this fails.
        assert gate.status()["challenge"]["request_id"] == first
        assert len(records.named("handoff_challenge_raised")) == 1
        # The tab is followed again (the agent switched away and back): still the same load.
        gate.mask_state.frame_detached(TOP)
        mask = await _followed(gate)
        await _frame(mask, "anchor-again", ANCHOR)
        # MUTATION: forget the raised page when its frames are detached on a re-follow and this fails.
        assert len(records.named("handoff_challenge_raised")) == 1
        # Control: a new page load on the same tab with a challenge raises again.
        await mask.on_event("Page.frameNavigated",
                            {"frame": {"id": TOP, "url": "https://shop.example.com/checkout"}})
        await _frame(mask, "anchor-3", ANCHOR)
        # MUTATION: never reset the page's mark on a top-frame navigation and this fails.
        assert len(records.named("handoff_challenge_raised")) == 2
        assert gate.status()["challenge"]["request_id"] != first

    asyncio.run(body())


def test_a_top_frame_on_a_challenge_host_is_one_request_too(short_tmp):  # noqa: F811
    async def body() -> None:
        gate, records = _gate(short_tmp)
        url = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b"
        # The same host list tabs.py refuses to keep, at any path.
        assert gate_tabs.refusal(url) == "challenge_host"
        await _followed(gate, url)
        challenge = gate.status()["challenge"]
        # MUTATION: look at child frames only and a challenge page shown whole raises nothing.
        assert challenge is not None and challenge["reason"] == "CAPTCHA on challenges.cloudflare.com"
        assert len(records.named("handoff_challenge_raised")) == 1

    asyncio.run(body())


def test_a_page_with_no_challenge_raises_nothing(short_tmp):  # noqa: F811
    async def body() -> None:
        gate, records = _gate(short_tmp)
        mask = await _followed(gate)
        await _frame(mask, "ad", "https://ads.example.net/frame?slot=1")
        # A reCAPTCHA host outside the challenge path (the list is host AND path).
        await _frame(mask, "search", "https://www.google.com/search?q=x")
        # MUTATION: match on the host alone for a child frame and this fails.
        assert gate.status()["challenge"] is None
        assert records.named("handoff_challenge_raised") == []

    asyncio.run(body())


def test_a_challenge_on_the_gates_sign_in_tab_raises_no_hand_off(short_tmp):  # noqa: F811
    """§2.4 of visual-qa-any-app.md is unchanged: the gate's own QA sign-in tab
    ends with `qa_sign_in_challenge` (test_qa_target_signin.py holds that end);
    a page the follower shows while the sign-in runs raises no hand-off."""
    async def body() -> None:
        gate, records = _gate(short_tmp)
        gate.qa_target.signing_in = True
        mask = await _followed(gate, "https://auth.example.com/login")
        await _frame(mask, "anchor", ANCHOR)
        # MUTATION: drop the `qa_target.signing_in` check in LiveGate._on_challenge and this fails.
        assert gate.status()["challenge"] is None
        assert records.named("handoff_challenge_raised") == []
        assert gate_qt.CHALLENGE == "qa_sign_in_challenge"

    asyncio.run(body())


class HeldProxy:
    """The proxy's surface `LiveGate.pause` uses."""

    paused = False
    agent_connections = 1

    async def pause(self) -> None:
        self.paused = True


def test_the_workers_pause_takes_the_request_off_and_a_held_browser_raises_none(short_tmp):  # noqa: F811
    async def body() -> None:
        gate, records = _gate(short_tmp)
        gate.proxy = HeldProxy()  # type: ignore[assignment]
        mask = await _followed(gate)
        await _frame(mask, "anchor", ANCHOR)
        challenge = gate.status()["challenge"]
        await gate.handle_ctl("pause", {"request_id": challenge["request_id"], "expect": challenge["expect"]})
        # MUTATION: keep the request in `status` after the pause and the worker reads it again.
        assert gate.status()["challenge"] is None
        # A person already holds the browser: a CAPTCHA on a new page is theirs to see.
        await mask.on_event("Page.frameNavigated",
                            {"frame": {"id": TOP, "url": "https://shop.example.com/checkout"}})
        await _frame(mask, "anchor-2", ANCHOR)
        # MUTATION: raise while the gate holds the agent for a hand-off and this fails.
        assert gate.status()["challenge"] is None
        assert len(records.named("handoff_challenge_raised")) == 1

    asyncio.run(body())
