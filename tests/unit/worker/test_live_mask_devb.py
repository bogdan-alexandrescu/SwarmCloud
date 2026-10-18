"""Pages a dev-browser agent opens report to the mask, and an iframe page no longer blanks whole (part of #1344).

docs/design/live-browser.md §5.3.2. MEASURED 2026-10-15 by visual QA on
release b93ad6cb:

(1) task_f7ff5ae0d8ac4612b0ff (browser_tool dev-browser): the live page stayed
    the "unavailable" grey for minutes, also while a person held control, with
    "Can't show this page safely: the page on screen has not reported its
    layout yet". The timeline shows dev-browser opening a NEW PAGE for every
    navigation: Puppeteer's `newPage()` (`Target.createTarget`) and then its
    `goto()` at once (fixtures/puppeteer-core-25.8.0-*.json).
(2) https://the-internet.herokuapp.com/iframe, a TinyMCE editor in an iframe:
    the whole page area blanked, for all three browser tools.

What the gate did. (1) `MaskCdp.install` told the state that every frame of
its first `Page.getFrameTree` had just NAVIGATED. When the page committed its
`goto` while the install was under way, the new document's observer had
already reported, and that "navigation" retired the live document: every
later report from it was dropped, until the page navigated again. (2) A child
frame the install found in that tree was never linked to its iframe element
(only a `Page.frameAttached`, or the parent's observer world being created,
asked), and an unlinked child blanked the page area for as long as it lived.
An out-of-process frame already open when the page was followed was the same.

What it does now, each held below:

* a frame the install finds already known keeps the document it reported;
* every unlinked child of a frame this session has an observer world for is
  linked at the end of the install, and again when an out-of-process frame's
  own session is installed;
* an unlinked child whose parent's fresh report has an iframe box no linked
  child claims, one box for every unlinked child, covers that box only; with
  fewer such boxes than unlinked children, or a child linked to a box the
  report does not have, the page area is still blanked.

Offline: no Chromium, Xvfb, relay or network.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

import pytest

from agent_worker.livegate.capture import Frame
from agent_worker.livegate.mask import (
    BINDING_NAME,
    MASK_RGB,
    REASON_UNPLACED,
    WORLD_NAME,
    MaskCdp,
    MaskState,
    Outcome,
)

import test_live_device_recover as recover
from test_live_blank import TabsChromium, _run, _tabs_rig

WHITE = (0xFF, 0xFF, 0xFF)
SCREEN_W, SCREEN_H = 1440, 987
PAGE_W, PAGE_H = 1440, 900
CHROME_H = SCREEN_H - PAGE_H
TOP = "PAGE1"
EDITOR = "frame-editor"
# The TinyMCE editor's iframe element on /iframe: (40, 200) 800 x 300.
EDITOR_BOX = (40, 200, 840, 500)


def _report(doc: str, epoch: int = 1, *, owners=(), viewport=(PAGE_W, PAGE_H, 0, 0, 1, PAGE_W, PAGE_H)) -> str:
    return json.dumps({"v": 1, "k": "report", "doc": doc, "e": epoch, "vp": list(viewport),
                       "f": [], "t": [], "o": list(owners), "tr": False})


def _owner(oid: int, box=EDITOR_BOX) -> dict:
    x0, y0, x1, y1 = box
    return {"id": oid, "r": [x0, y0, x1 - x0, y1 - y0]}


def _screen() -> Frame:
    return Frame.blank(SCREEN_W, SCREEN_H, WHITE)


def _all(frame: Frame, box, rgb) -> bool:
    x0, y0, x1, y1 = box
    return all(frame.rgb_at(x, CHROME_H + y) == rgb for y in range(y0, y1, 7) for x in range(x0, x1, 7))


def _iframe_page() -> MaskState:
    state = MaskState()
    state.session_up()
    state.frame_attached(TOP, None)
    state.navigated(TOP, "https://the-internet.herokuapp.com/iframe")
    return state


# --------------------------------------------------------------------------
# (2) An iframe page with a child the gate cannot place covers only the child
# --------------------------------------------------------------------------


def test_an_iframe_page_with_an_unplaceable_child_covers_only_the_child():
    state = _iframe_page()
    state.frame_attached(EDITOR, TOP)  # never linked: which element it is drawn in is unknown
    state.on_message(TOP, _report("top", owners=[_owner(9)]))
    frame = _screen()
    assert state.apply(frame) is Outcome.MASKED, "one unplaceable editor frame blanked the whole page"
    assert _all(frame, EDITOR_BOX, MASK_RGB), "the editor frame's box is not covered"
    assert _all(frame, (0, 0, PAGE_W, 180), WHITE), "the page around the editor is not shown"
    assert _all(frame, (0, 520, PAGE_W, PAGE_H), WHITE)
    assert state.reason is not None, "the viewer is not told part of the page is covered"


def test_an_unlinked_child_with_no_box_left_to_be_drawn_in_still_blanks_the_page_area():
    state = _iframe_page()
    state.frame_attached(EDITOR, TOP)
    state.on_message(TOP, _report("top"))  # no iframe element reported: it is drawn somewhere unseen
    frame = _screen()
    assert state.apply(frame) is Outcome.PAGE_BLANKED
    assert state.reason == REASON_UNPLACED
    assert _all(frame, (0, 0, PAGE_W, PAGE_H), MASK_RGB)


def test_more_unlinked_children_than_unclaimed_boxes_still_blanks_the_page_area():
    state = _iframe_page()
    state.frame_attached(EDITOR, TOP)
    state.frame_attached("frame-other", TOP)
    state.on_message(TOP, _report("top", owners=[_owner(9)]))
    assert state.apply(_screen()) is Outcome.PAGE_BLANKED
    assert state.reason == REASON_UNPLACED


def test_a_child_linked_to_a_box_the_report_does_not_have_still_blanks_the_page_area():
    # Its element is one the observer did not report (a closed shadow root,
    # an <object>): the report says nothing of where it is drawn.
    state = _iframe_page()
    state.frame_attached(EDITOR, TOP)
    state.frame_linked(EDITOR, 77)
    state.on_message(TOP, _report("top", owners=[_owner(9)]))
    assert state.apply(_screen()) is Outcome.PAGE_BLANKED
    assert state.reason == REASON_UNPLACED


def test_two_children_claiming_one_box_still_blank_the_page_area():
    state = _iframe_page()
    for child in (EDITOR, "frame-other"):
        state.frame_attached(child, TOP)
        state.frame_linked(child, 9)
    state.on_message(TOP, _report("top", owners=[_owner(9), _owner(10, (900, 200, 1000, 300))]))
    assert state.apply(_screen()) is Outcome.PAGE_BLANKED


# --------------------------------------------------------------------------
# The install links the children it finds, and keeps a document that reported
# --------------------------------------------------------------------------


class InstallCdp:
    """A page session's `send`, delivering what Chromium sends meanwhile to the mask.

    `during[method]` is a list of (event, params) handed to the mask's
    `on_event` before `method` is answered, as the page follower's queue
    would have handled them while the install awaited the reply.
    """

    def __init__(self, tree: dict[str, Any]) -> None:
        self.tree = tree
        self.mask: MaskCdp | None = None
        self.during: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.sent.append((method, params))
        assert self.mask is not None
        for event, event_params in self.during.pop(method, []):
            await self.mask.on_event(event, event_params)
        if method == "Page.getFrameTree":
            return {"frameTree": self.tree}
        if method == "DOM.getFrameOwner":
            return {"backendNodeId": 7}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "o1"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": 9}}
        return {}


def _world(context: int, frame_id: str) -> tuple[str, dict[str, Any]]:
    return ("Runtime.executionContextCreated", {"context": {
        "id": context, "name": WORLD_NAME, "auxData": {"frameId": frame_id}}})


def _bound(context: int, payload: str) -> tuple[str, dict[str, Any]]:
    return ("Runtime.bindingCalled", {"name": BINDING_NAME, "executionContextId": context, "payload": payload})


def test_the_install_links_an_iframe_already_on_the_page():
    """The follower attaches to /iframe after it loaded: its frame tree holds
    the editor, and the top frame's observer world is created (by
    `runImmediately`) before that tree is read."""
    cdp = InstallCdp({"frame": {"id": TOP, "url": "https://the-internet.herokuapp.com/iframe"},
                      "childFrames": [{"frame": {"id": EDITOR, "parentId": TOP, "url": "about:blank"}}]})
    state = MaskState()
    cdp.mask = mask = MaskCdp(state, cdp)
    cdp.during["Page.addScriptToEvaluateOnNewDocument"] = [_world(3, TOP), _world(4, EDITOR)]

    asyncio.run(mask.install())
    assert ("DOM.getFrameOwner", {"frameId": EDITOR}) in cdp.sent, "the editor frame was never linked"
    assert state.unlinked_children(TOP) == []
    asyncio.run(mask.on_event(*_bound(3, _report("top", owners=[_owner(9)]))))
    asyncio.run(mask.on_event(*_bound(4, _report("editor", viewport=(800, 300, 0, 0, 1, 800, 300)))))
    frame = _screen()
    assert state.apply(frame) is Outcome.MASKED
    assert _all(frame, (0, 0, PAGE_W, PAGE_H), WHITE), "the linked editor that reported is not shown"


def test_a_navigation_that_commits_during_the_install_keeps_its_reports():
    """Puppeteer's `goto` right after `newPage`: the page commits, and its
    observer reports, before the install has read the frame tree."""
    url = "https://example.test/next"
    cdp = InstallCdp({"frame": {"id": TOP, "url": url}})
    state = MaskState()
    cdp.mask = mask = MaskCdp(state, cdp)
    doc = "doc-" + secrets.token_hex(4)
    cdp.during["Page.getFrameTree"] = [
        ("Page.frameNavigated", {"frame": {"id": TOP, "url": url}}),
        _world(5, TOP),
        _bound(5, _report(doc)),
    ]
    asyncio.run(mask.install())
    # The observer's next report on the same document: the heartbeat.
    asyncio.run(mask.on_event(*_bound(5, _report(doc, 2))))
    frame = _screen()
    assert state.apply(frame) is Outcome.MASKED, f"the page that reported is not shown ({state.reason})"


def test_an_out_of_process_frame_open_before_the_follow_is_linked_once_its_session_installs():
    page = InstallCdp({"frame": {"id": TOP, "url": "https://news.example.test/"}})
    state = MaskState()
    page.mask = top = MaskCdp(state, page)
    page.during["Page.addScriptToEvaluateOnNewDocument"] = [_world(3, TOP)]
    asyncio.run(top.install())
    oopif = InstallCdp({"frame": {"id": "AD", "parentId": TOP, "url": "https://ads.example.test/slot"}})
    oopif.mask = child = MaskCdp(state, oopif, child=True)
    asyncio.run(child.install())
    assert state.unlinked_children(TOP) == ["AD"]
    asyncio.run(top.relink())
    assert ("DOM.getFrameOwner", {"frameId": "AD"}) in page.sent
    assert state.unlinked_children(TOP) == []


# --------------------------------------------------------------------------
# (1) A page dev-browser opens, followed through the gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize("create", [
    {"url": "about:blank"},  # Puppeteer's newPage(), as the fixture pins it
    {"url": "about:blank", "browserContextId": "CTX1"},  # a page in a context of the agent's own
], ids=["default-context", "new-context"])
def test_a_page_opened_with_create_target_and_navigated_at_once_reports_and_is_masked(tmp_path, create):
    rig = _tabs_rig(tmp_path, ["PAGE1"], shown="PAGE1")
    doc = "doc-" + secrets.token_hex(4)

    async def body(rig: recover.Rig) -> None:
        gate, chromium = rig.gate, rig.chromium
        assert isinstance(chromium, TabsChromium)
        await recover._until(lambda: gate.pages.current == "PAGE1" and gate.pages._session is not None)
        chromium.report(gate.pages._session, "PAGE1", _report("first"))
        await recover._until(lambda: gate.mask_state.apply(_screen()) is Outcome.MASKED)
        fired: list[str] = []

        def goto(message: dict[str, Any]) -> None:
            # The mask's install on the new page reads the frame tree; the
            # page's `goto` has committed and its observer has reported first.
            session = message.get("sessionId")
            if fired or chromium.sessions.get(session) != "PAGE2" or not any(
                    m == "Runtime.addBinding" and p.get("name") == BINDING_NAME and s == session
                    for m, p, s in chromium.sent):
                return
            fired.append(session)
            chromium.pages["PAGE2"]["url"] = "https://page2.example.test/next"
            chromium.emit("Page.frameNavigated", {"frame": {"id": "PAGE2", "url": "https://page2.example.test/next"}},
                          session)
            chromium.report(session, "PAGE2", _report(doc), context=81)

        chromium.during["Page.getFrameTree"] = goto

        class Link:
            async def send(self, message: dict[str, Any]) -> None: ...

            async def close(self) -> None: ...

        agent = await gate.proxy.connect_agent(Link())
        assert agent is not None
        await gate.proxy.on_agent_message(agent, {"id": 1, "method": "Target.createTarget", "params": create})
        await recover._until(lambda: bool(fired) and gate.pages.current == "PAGE2"
                             and gate.pages._session == fired[0] and gate.mask_state._session)
        # The observer's heartbeat, a second later, on the same document.
        chromium.emit("Runtime.bindingCalled", {"name": BINDING_NAME, "payload": _report(doc, 2),
                                                "executionContextId": 81}, fired[0])

        def masked() -> bool:
            return gate.mask_state.apply(_screen()) is Outcome.MASKED

        try:
            await recover._until(masked, timeout=3.0)
        except AssertionError:
            raise AssertionError(f"the new page never showed: {gate.mask_state.reason}") from None

    _run(rig, body)


def test_the_follower_links_an_out_of_process_frame_its_page_had_before_the_follow(tmp_path):
    rig = _tabs_rig(tmp_path, ["PAGE1"], shown="PAGE1")

    async def body(rig: recover.Rig) -> None:
        gate, chromium = rig.gate, rig.chromium
        await recover._until(lambda: gate.pages.current == "PAGE1" and gate.pages._session is not None)
        page_session = gate.pages._session
        chromium.report(page_session, "PAGE1", _report("top", owners=[_owner(31)]))
        await recover._until(lambda: gate.mask_state.apply(_screen()) is Outcome.MASKED)
        # The ad frame was out of process before the follow: the page's own
        # tree never listed it, and Chromium auto-attaches its session now.
        chromium.oopifs["OOP1"] = ("AD", "PAGE1")
        chromium.emit("Target.attachedToTarget", {"sessionId": "OOP1", "waitingForDebugger": False,
                                                  "targetInfo": {"targetId": "AD", "type": "iframe"}},
                      page_session)
        await recover._until(lambda: any(m == "DOM.getFrameOwner" and p.get("frameId") == "AD"
                                          and s == page_session for m, p, s in chromium.sent))

    _run(rig, body)
