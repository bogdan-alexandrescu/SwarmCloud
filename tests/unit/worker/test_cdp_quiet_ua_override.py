"""Puppeteer's and Playwright's page-setup user-agent probe is refused
quietly, and the launch's untouched about:blank tab is closed once the
agent's first real page has navigated (#1346 comment 4).

MEASURED 2026-10-15 on the live deployment (task_b7080998): every page
dev-browser opened added two "was refused" lines to the theater's timeline --
`WebMCP.enable` and `Emulation.setUserAgentOverride` -- and the task ended with
five tabs, one of them the about:blank Chromium was launched on. The client
sends the user-agent override on every page with an EMPTY user agent (no
override at all; images/agent-runtime-browser/selftest.py reads it off
Playwright's protocol log) and swallows the refusal, so the line meant nothing.

Still refused, still never forwarded: the device stays the gate's, by preset
name. Only the probe's exact shape is quiet; a user agent the agent chose is
recorded as before. A quiet refusal is counted and logged at debug, and writes
neither a `cdp_method_refused` record nor a refused action line (the line is
what TheaterTimeline.tsx draws as "was refused").

No test here runs Chromium, a network or a cloud credential.
"""

from __future__ import annotations

from typing import Any

import pytest

from test_live_cdp_proxy import World, run, settle

PROBES = ["Emulation.setUserAgentOverride", "Network.setUserAgentOverride"]


def _world(tmp_path) -> tuple[World, list[dict[str, Any]]]:
    lines: list[dict[str, Any]] = []
    return World(tmp_path, on_action=lines.append), lines


def _refused_lines(lines: list[dict[str, Any]], method: str) -> list[dict[str, Any]]:
    return [line for line in lines if line.get("method") == method and line.get("outcome") == "refused"]


# --------------------------------------------------------------------------- #
# The user-agent probe                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method", PROBES)
def test_the_empty_user_agent_probe_on_a_page_is_refused_quietly(tmp_path, method):
    async def body():
        w, lines = _world(tmp_path)
        await w.connect()
        page = await w.attach_page()
        before = len(w.chromium.received)
        for _ in range(2):
            request = await w.send(method, {"userAgent": ""}, page)
            assert "error" in w.agent.response(request)
        assert w.chromium.received[before:] == []
        assert w.refused() == []
        assert w.proxy.quiet_refusals == {method: 2}
        assert _refused_lines(lines, method) == []
        await w.chromium.stop()

    run(body)


@pytest.mark.parametrize("method", PROBES)
def test_a_user_agent_the_agent_chose_is_still_recorded(tmp_path, method):
    async def body():
        w, lines = _world(tmp_path)
        await w.connect()
        page = await w.attach_page()
        before = len(w.chromium.received)
        chosen = await w.send(method, {"userAgent": "agent-chosen/1.0"}, page)
        metadata = await w.send(method, {"userAgent": "", "platform": "iPhone"}, page)
        browser = await w.send(method, {"userAgent": ""})
        for request in (chosen, metadata, browser):
            assert "error" in w.agent.response(request)
        assert w.chromium.received[before:] == []
        assert w.refused() == [method, method, method]
        assert w.proxy.quiet_refusals == {}
        assert len(_refused_lines(lines, method)) == 3
        await w.chromium.stop()

    run(body)


def test_the_page_setup_probes_write_no_timeline_line(tmp_path):
    """The pair every page added: neither is a refused action line now."""

    async def body():
        w, lines = _world(tmp_path)
        await w.connect()
        page = await w.attach_page()
        await w.send("WebMCP.enable", {}, page)
        await w.send("Emulation.setUserAgentOverride", {"userAgent": ""}, page)
        assert [line for line in lines if line.get("outcome") == "refused"] == []
        assert w.proxy.quiet_refusals == {"WebMCP.enable": 1, "Emulation.setUserAgentOverride": 1}
        await w.chromium.stop()

    run(body)


# --------------------------------------------------------------------------- #
# The launch's about:blank tab                                                #
# --------------------------------------------------------------------------- #


def _launch_tab(w: World, target: str = "T-launch") -> str:
    """The tab Chromium was launched on: about:blank, nobody's."""
    w.chromium.targets[target] = "DEFAULT"
    w.chromium.urls[target] = "about:blank"
    return target


def _closed(w: World) -> list[str]:
    return [m["params"]["targetId"] for m in w.chromium.from_agent("Target.closeTarget")]


async def _navigate(w: World, session: str, target: str, url: str) -> None:
    request = await w.send("Page.navigate", {"url": url}, session)
    assert "result" in w.agent.response(request)
    w.chromium.urls[target] = url
    await settle()


def test_the_untouched_blank_tab_is_closed_once_a_page_the_agent_opened_navigates(tmp_path):
    async def body():
        w, _ = _world(tmp_path)
        launch = _launch_tab(w)
        await w.connect()
        created = await w.send("Target.createTarget", {"url": "about:blank"})
        mine = w.agent.response(created)["result"]["targetId"]
        session = await w.attach_page(mine)
        assert _closed(w) == []
        await _navigate(w, session, mine, "https://example.com/")
        assert _closed(w) == [launch]
        # Once: a later navigation closes nothing more.
        later = _launch_tab(w, "T-later")
        await _navigate(w, session, mine, "https://example.com/next")
        assert later not in _closed(w)
        assert _closed(w) == [launch]
        await w.chromium.stop()

    run(body)


def test_a_new_tab_opened_on_a_real_url_closes_the_blank_one(tmp_path):
    async def body():
        w, _ = _world(tmp_path)
        launch = _launch_tab(w)
        await w.connect()
        created = await w.send("Target.createTarget", {"url": "https://example.com/"})
        mine = w.agent.response(created)["result"]["targetId"]
        await settle()
        assert _closed(w) == [launch]
        assert mine not in _closed(w)
        await w.chromium.stop()

    run(body)


def test_the_only_tab_is_never_closed(tmp_path):
    """The agent navigated the launch tab itself: it is the page, kept."""

    async def body():
        w, _ = _world(tmp_path)
        launch = _launch_tab(w)
        await w.connect()
        session = await w.attach_page(launch)
        await _navigate(w, session, launch, "https://example.com/")
        assert _closed(w) == []
        await w.chromium.stop()

    run(body)


def test_a_blank_tab_the_agent_opened_itself_is_kept(tmp_path):
    async def body():
        w, _ = _world(tmp_path)
        launch = _launch_tab(w)
        await w.connect()
        spare = await w.send("Target.createTarget", {"url": "about:blank"})
        kept = w.agent.response(spare)["result"]["targetId"]
        created = await w.send("Target.createTarget", {"url": "about:blank"})
        mine = w.agent.response(created)["result"]["targetId"]
        session = await w.attach_page(mine)
        await _navigate(w, session, mine, "https://example.com/")
        assert _closed(w) == [launch]
        assert kept not in _closed(w)
        await w.chromium.stop()

    run(body)


def test_a_navigation_to_about_blank_closes_nothing(tmp_path):
    async def body():
        w, _ = _world(tmp_path)
        _launch_tab(w)
        await w.connect()
        created = await w.send("Target.createTarget", {"url": "about:blank"})
        mine = w.agent.response(created)["result"]["targetId"]
        session = await w.attach_page(mine)
        await _navigate(w, session, mine, "about:blank")
        assert _closed(w) == []
        await w.chromium.stop()

    run(body)
