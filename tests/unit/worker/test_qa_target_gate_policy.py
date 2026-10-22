"""The live gate's safety rules on a QA target (VQ-4; part of #1933).

docs/design/visual-qa-any-app.md §3. The row's red-first tests:

* a `POST` to the target is refused, one to a `read_posts` path is not;
* a non-GET off-origin is refused even in a mutating flow;
* a top-frame navigation off the approved origins gets the gate page;
* navigations are spaced by `navigation_ms`.

Driven through the real `QaTarget`, `QaTargetGuard` and `CdpProxy` against
test_qa_target_signin's scripted Chromium: what "reached the network" is a
Request-stage pause the gate CONTINUED (the fake's `network`), and what the
gate answered itself is a `Fetch.fulfillRequest`, so "the POST never left" is
read off what the fake saw. No Chromium, network or cloud credential.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from agent_worker.livegate import qatarget as gate_qt

import test_qa_target_signin as signin
from test_qa_target_signin import LOGIN_ORIGIN, ORIGIN, Rig, run, tmp  # noqa: F401  (tmp is a fixture)

ELSEWHERE = "https://pay.example.net"


def _public(**over: Any) -> dict[str, Any]:
    """A public target (no sign-in), so no sign-in runs beside the rules."""
    args = signin._args(signin._target_id(), signin._password())
    args.pop("credential")
    args["sign_in"] = {"kind": "none"}
    args.update(over)
    return args


async def _rig(tmp: Any, **over: Any) -> tuple[Rig, str]:
    rig = Rig(tmp)
    rig.fake.start(rig.sink)
    rig.target.handle(_public(**over))
    await rig.proxy.start_qa_target()
    await rig.settle()
    return rig, next(s for s, t in rig.fake.sessions.items() if t == "PAGE1")


def _pause(rig: Rig, session: str, url: str, *, method: str = "GET", kind: str = "XHR",
           frame: str | None = "PAGE1") -> str:
    request_id = rig.fake._next("R")
    params: dict[str, Any] = {"requestId": request_id, "resourceType": kind, "networkId": rig.fake._next("N"),
                              "request": {"url": url, "method": method, "headers": {"Accept": "*/*"}}}
    if frame is not None:
        params["frameId"] = frame
    rig.fake.paused[request_id] = (session, params)
    rig.fake.emit("Fetch.requestPaused", params, session)
    return request_id


def _sent(rig: Rig, url: str) -> bool:
    return any(u == url for u, _ in rig.fake.network)


def _fulfilled(rig: Rig, request_id: str) -> dict[str, Any] | None:
    return next((m["params"] for m in rig.fake.sent("Fetch.fulfillRequest")
                 if m["params"]["requestId"] == request_id), None)


# --------------------------------------------------------------------------
# Read-only by default
# --------------------------------------------------------------------------


def test_a_post_to_the_target_is_refused_and_one_to_a_read_posts_path_is_not(tmp):
    async def body() -> None:
        rig, session = await _rig(tmp)
        write = _pause(rig, session, f"{ORIGIN}/api/items", method="POST")
        read = _pause(rig, session, f"{ORIGIN}/graphql", method="POST")
        deleted = _pause(rig, session, f"{ORIGIN}/api/items/1", method="DELETE")
        fetched = _pause(rig, session, f"{ORIGIN}/api/list")
        await rig.settle()
        # MUTATION: return CONTINUE for target writes in QaTarget.judge and the POST leaves.
        assert not _sent(rig, f"{ORIGIN}/api/items")
        refused = _fulfilled(rig, write)
        assert refused is not None and refused["responseCode"] == 403
        assert b"read-only" in base64.b64decode(refused["body"])
        assert _fulfilled(rig, deleted) is not None and not _sent(rig, f"{ORIGIN}/api/items/1")
        # MUTATION: drop the read_posts check in QaTarget.judge and the GraphQL read is refused.
        assert _fulfilled(rig, read) is None and _sent(rig, f"{ORIGIN}/graphql")
        assert _fulfilled(rig, fetched) is None and _sent(rig, f"{ORIGIN}/api/list")
        assert {"method": "POST", "origin": ORIGIN} in rig_events(rig, "qa_write_refused")
        await rig.fake.stop()

    run(body)


def rig_events(rig: Rig, name: str) -> list[dict[str, Any]]:
    return [f for e, f in rig.records if e == name]


def test_a_non_get_off_origin_is_refused_even_in_a_mutating_flow(tmp):
    async def body() -> None:
        rig, session = await _rig(tmp, environment="non_production", mutating=True)
        assert rig.target.mutating
        posted = _pause(rig, session, f"{ELSEWHERE}/charge", method="POST")
        put = _pause(rig, session, f"{ELSEWHERE}/charge", method="PUT")
        preflight = _pause(rig, session, f"{ELSEWHERE}/charge", method="OPTIONS")
        image = _pause(rig, session, f"{ELSEWHERE}/logo.png", kind="Image")
        own = _pause(rig, session, f"{ORIGIN}/api/items", method="POST")
        await rig.settle()
        # MUTATION: let `mutating` lift the off-origin rule in QaTarget.judge and the charge is sent.
        assert all(_fulfilled(rig, r) is not None for r in (posted, put, preflight))
        assert not _sent(rig, f"{ELSEWHERE}/charge")
        assert _fulfilled(rig, image) is None and _sent(rig, f"{ELSEWHERE}/logo.png")
        # The mutating flow lifts the first rule only: its own origin's write goes.
        assert _fulfilled(rig, own) is None and _sent(rig, f"{ORIGIN}/api/items")
        await rig.fake.stop()

    run(body)


def test_mutating_is_ignored_on_a_production_target(tmp):
    async def body() -> None:
        rig, session = await _rig(tmp, environment="production", mutating=True)
        # MUTATION: drop the environment check where QaTarget.handle sets `mutating` and this fails.
        assert not rig.target.mutating
        write = _pause(rig, session, f"{ORIGIN}/api/items", method="PATCH")
        await rig.settle()
        assert _fulfilled(rig, write) is not None
        await rig.fake.stop()

    run(body)


# --------------------------------------------------------------------------
# Never leaving the origin
# --------------------------------------------------------------------------


def test_a_top_frame_navigation_off_the_approved_origins_gets_the_gate_page(tmp):
    async def body() -> None:
        rig, session = await _rig(tmp)
        away = _pause(rig, session, f"{ELSEWHERE}/checkout", kind="Document")
        login = _pause(rig, session, f"{LOGIN_ORIGIN}/login", kind="Document")
        logout = _pause(rig, session, f"{ORIGIN}/logout", kind="Document")
        home = _pause(rig, session, f"{ORIGIN}/home", kind="Document")
        framed = _pause(rig, session, f"{ELSEWHERE}/widget", kind="Document", frame="FRAME9")
        await rig.settle()
        page = _fulfilled(rig, away)
        # MUTATION: return CONTINUE for a top document off the origins in QaTarget.judge and the page leaves.
        assert page is not None and page["responseCode"] == 200
        assert b"Left the QA target" in base64.b64decode(page["body"])
        assert not _sent(rig, f"{ELSEWHERE}/checkout")
        # MUTATION: allow the login's other origin outside the gate's own sign-in and this fails.
        assert _fulfilled(rig, login) is not None
        # MUTATION: drop the excluded-path check in QaTarget.judge and the run signs itself out.
        assert _fulfilled(rig, logout) is not None
        assert _fulfilled(rig, home) is None and _sent(rig, f"{ORIGIN}/home")
        # A subframe's document is not the top frame: an embed loads, read-only.
        assert _fulfilled(rig, framed) is None
        assert {"origin": ELSEWHERE} in rig_events(rig, "qa_target_left")
        await rig.fake.stop()

    run(body)


def test_the_gate_page_escapes_what_it_names():
    page = gate_qt.left_page("https://x.example/<script>")
    assert "<script>" not in page and "&lt;script&gt;" in page


# --------------------------------------------------------------------------
# A rate
# --------------------------------------------------------------------------


def test_navigations_are_spaced_by_navigation_ms_and_requests_by_requests_per_s(tmp):
    async def body() -> None:
        rig, session = await _rig(tmp, rate={"navigation_ms": 2500, "requests_per_s": 4})
        waits: list[float] = []

        async def held(seconds: float) -> None:
            waits.append(seconds)

        rig.proxy._qa_guard._sleep = held
        for n in range(3):
            _pause(rig, session, f"{ORIGIN}/page/{n}", kind="Document")
        await rig.settle()
        # MUTATION: return 0 from QaTarget.navigation_wait and three navigations go at once.
        assert waits == [2.5, 5.0]
        assert len([u for u, _ in rig.fake.network if "/page/" in u]) == 3, "held, never failed"
        waits.clear()
        for n in range(3):
            _pause(rig, session, f"{ORIGIN}/api/{n}")
        _pause(rig, session, f"{ELSEWHERE}/font.woff", kind="Font")
        await rig.settle()
        # MUTATION: return 0 from QaTarget.request_wait and the target is hit at full speed.
        assert waits == [0.25, 0.5], "only the target's origins are rate-limited"
        await rig.fake.stop()

    run(body)


def test_the_rate_floor_is_one_navigation_a_second():
    target = gate_qt.QaTarget(lambda e, f: None)
    with pytest.raises(gate_qt.QaTargetRefused) as refused:
        target.handle(_public(rate={"navigation_ms": 500, "requests_per_s": 10}))
    # MUTATION: lower MIN_NAVIGATION_MS and a faster rate than §3's floor is taken.
    assert refused.value.code == "rate"


# --------------------------------------------------------------------------
# The agent cannot route around the guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rewrite", [{"method": "POST"}, {"url": f"{ELSEWHERE}/x"}, {"postData": "YQ=="}])
def test_the_agent_may_not_continue_a_paused_request_rewritten(tmp, rewrite):
    async def body() -> None:
        rig, _ = await _rig(tmp)
        link = signin.qi.FakeLink()
        conn = await rig.proxy.connect_agent(link)
        await rig.proxy.on_agent_message(conn, {"id": 3, "method": "Fetch.continueRequest",
                                                "params": {"requestId": "R1", **rewrite}})
        await rig.settle()
        reply = next(m for m in link.sent if m.get("id") == 3)
        # MUTATION: drop the agent_refusal check in CdpProxy.on_agent_message and the rewrite is forwarded.
        assert "error" in reply
        assert not [m for m in rig.fake.sent("Fetch.continueRequest") if m.get("sessionId") == conn.browser_session]
        await rig.fake.stop()

    run(body)


def test_judge_on_its_own():
    target = gate_qt.QaTarget(lambda e, f: None)
    # Nothing armed: nothing judged.
    assert target.judge("POST", f"{ELSEWHERE}/x", top_document=False) == gate_qt.CONTINUE
    target.handle(_public())
    assert target.judge("POST", f"{ORIGIN}/graphql?op=Q", top_document=False) == gate_qt.CONTINUE
    assert target.judge("POST", f"{ORIGIN}/graphql/other", top_document=False).action == "refuse"
    assert target.judge("GET", "data:text/plain,hi", top_document=True) == gate_qt.CONTINUE
    assert target.judge("HEAD", f"{ELSEWHERE}/x", top_document=False) == gate_qt.CONTINUE
    # A look-alike is another origin.
    assert target.judge("GET", "https://app.example.com.evil.example/", top_document=True).action == "leave"
    target.signing_in = True
    # The gate's own sign-in submits, and may go to the login's other origin.
    assert target.judge("POST", f"{LOGIN_ORIGIN}/session", top_document=False) == gate_qt.CONTINUE
    assert target.judge("GET", f"{LOGIN_ORIGIN}/login", top_document=True) == gate_qt.CONTINUE
    # Never off the approved origins, even then.
    assert target.judge("POST", f"{ELSEWHERE}/x", top_document=False).action == "refuse"


def test_nothing_is_judged_before_a_target_is_set(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.start(rig.sink)
        await rig.proxy.start_qa_target()
        await rig.settle()
        assert not rig.fake.sent("Fetch.enable"), "no target, no interception"
        await rig.fake.stop()

    run(body)

