"""The live gate's CDP proxy: the agent's only way to Chromium (live browser L2).

docs/design/live-browser.md section 5.4.3 (while a person is in control the
agent's CDP is held) and 5.4.4 (a method policy, always on). Every test drives
the real `CdpProxy` against a scripted Chromium that records what reached it,
so "the agent could not do X" is read off what Chromium actually received, not
off the proxy's own bookkeeping.

No test here runs Chromium, a network or a cloud credential. Post bodies and
script sources are built at run time.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate import cdppolicy
from agent_worker.livegate.cdppolicy import PolicyPaths
from agent_worker.livegate.cdpproxy import CdpProxy, PipeTransport, serve_agents
from agent_worker.livegate.scripts import ScriptRegistry

DEFER = object()


class FakeChromium:
    """Answers CDP over one connection the way `--remote-debugging-pipe` does.

    Every message the proxy sends is kept in `received`. Replies and events
    go through one queue pumped by one task, in order, as the gate's pipe
    reader delivers them -- so a proxy that awaited a reply inside its own
    message handler would deadlock here exactly as it would on the pipe.
    """

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.proxy: CdpProxy | None = None
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.deferred: dict[str, int] = {}
        self.defer_methods: set[str] = set()
        self.urls: dict[str, str] = {}
        self.targets: dict[str, str] = {}  # targetId -> browserContextId
        self._n = 0
        self._pump: asyncio.Task[None] | None = None

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def start(self, proxy: CdpProxy) -> None:
        self.proxy = proxy
        self._pump = asyncio.create_task(self._run())

    async def _run(self) -> None:
        assert self.proxy is not None
        while True:
            message = await self.queue.get()
            await self.proxy.on_chromium_message(message)
            self.queue.task_done()

    async def stop(self) -> None:
        if self._pump is not None:
            self._pump.cancel()

    def emit(self, method: str, params: dict[str, Any], session_id: str | None = None) -> None:
        message: dict[str, Any] = {"method": method, "params": params}
        if session_id is not None:
            message["sessionId"] = session_id
        self.queue.put_nowait(message)

    def respond(self, method: str, result: dict[str, Any]) -> None:
        self.queue.put_nowait({"id": self.deferred.pop(method), "result": result})

    async def send(self, message: dict[str, Any]) -> None:
        self.received.append(message)
        method = message["method"]
        params = message.get("params", {})
        session = message.get("sessionId")
        if method in self.defer_methods:
            self.deferred[method] = message["id"]
            return
        result: dict[str, Any] = {}
        if method == "Target.attachToBrowserTarget":
            result = {"sessionId": self._next("B")}
        elif method == "Target.attachToTarget":
            child = self._next("S")
            target = params["targetId"]
            self.emit(
                "Target.attachedToTarget",
                {
                    "sessionId": child,
                    "targetInfo": {
                        "targetId": target,
                        "type": "page",
                        "browserContextId": self.targets.get(target, "DEFAULT"),
                        "url": self.urls.get(target, "about:blank"),
                    },
                    "waitingForDebugger": False,
                },
                session,
            )
            result = {"sessionId": child}
        elif method == "Target.createBrowserContext":
            result = {"browserContextId": self._next("CTX")}
        elif method == "Target.createTarget":
            target = self._next("T")
            self.targets[target] = params.get("browserContextId", "DEFAULT")
            self.urls[target] = params.get("url", "about:blank")
            result = {"targetId": target}
        elif method == "Target.getTargetInfo":
            target = params["targetId"]
            result = {
                "targetInfo": {
                    "targetId": target,
                    "type": "page",
                    "url": self.urls.get(target, "about:blank"),
                    "browserContextId": self.targets.get(target, "DEFAULT"),
                }
            }
        elif method == "Target.getTargets":
            result = {
                "targetInfos": [
                    {"targetId": t, "type": "page", "browserContextId": c, "url": self.urls[t]}
                    for t, c in self.targets.items()
                ]
            }
        elif method == "Page.addScriptToEvaluateOnNewDocument":
            result = {"identifier": self._next("")}
        elif method == "Runtime.evaluate":
            result = {"result": {"type": "number", "value": 0}}
        self.queue.put_nowait({"id": message["id"], "result": result})

    def from_agent(self, method: str) -> list[dict[str, Any]]:
        return [m for m in self.received if m["method"] == method]


class FakeAgent:
    """One agent connection: what the proxy delivered to it."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True

    def response(self, request_id: int) -> dict[str, Any] | None:
        for message in self.sent:
            if message.get("id") == request_id:
                return message
        return None

    def events(self, method: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m.get("method") == method]


async def settle() -> None:
    for _ in range(200):
        await asyncio.sleep(0)


class World:
    def __init__(self, tmp_path: Path, **proxy_options: Any) -> None:
        self.downloads = tmp_path / "downloads"
        self.uploads = tmp_path / "uploads"
        self.downloads.mkdir()
        self.uploads.mkdir()
        self.records: list[tuple[str, dict[str, str]]] = []
        self.ended: list[str] = []
        self.chromium = FakeChromium()
        self.proxy = CdpProxy(
            self.chromium.send,
            paths=PolicyPaths(downloads_dir=self.downloads, uploads_dir=self.uploads),
            record=lambda event, fields: self.records.append((event, dict(fields))),
            on_control_ended=self.ended.append,
            security_wait_seconds=0.05,
            **proxy_options,
        )
        self.chromium.start(self.proxy)
        self.agent = FakeAgent()
        self.conn: Any = None
        self.page_session = ""
        self._id = 100

    async def connect(self) -> None:
        self.conn = await self.proxy.connect_agent(self.agent)
        assert self.conn is not None

    async def send(self, method: str, params: dict[str, Any] | None = None, session: str | None = None) -> int:
        self._id += 1
        message: dict[str, Any] = {"id": self._id, "method": method, "params": params or {}}
        if session is not None:
            message["sessionId"] = session
        await self.proxy.on_agent_message(self.conn, message)
        await settle()
        return self._id

    async def attach_page(self, target: str = "T-agent") -> str:
        """The agent's Playwright auto-attaches, and Chromium hands it a page."""
        self.chromium.targets.setdefault(target, "DEFAULT")
        self.chromium.urls.setdefault(target, "https://example.com/")
        await self.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
        )
        child = f"S-{target}"
        self.chromium.emit(
            "Target.attachedToTarget",
            {
                "sessionId": child,
                "targetInfo": {"targetId": target, "type": "page", "browserContextId": "DEFAULT", "url": "https://example.com/"},
                "waitingForDebugger": True,
            },
            self.conn.browser_session,
        )
        await settle()
        self.page_session = child
        return child

    def refused(self) -> list[str]:
        return [fields["method"] for event, fields in self.records if event == "cdp_method_refused"]


def run(body):
    async def main():
        await body()

    asyncio.run(main())


def ids_at_chromium(world: World, method: str) -> list[Any]:
    return [m.get("params", {}) for m in world.chromium.from_agent(method)]


# --------------------------------------------------------------------------- #
# Control mode: held, dropped, stripped, continued, removed, refused          #
# --------------------------------------------------------------------------- #


def test_commands_in_control_are_held_and_released_in_order_on_return(tmp_path):
    async def body():
        w = World(tmp_path)
        await w.connect()
        page = await w.attach_page()
        await w.proxy.pause()
        held = [await w.send("Page.navigate", {"url": f"https://example.com/{n}"}, page) for n in range(3)]
        await settle()
        assert ids_at_chromium(w, "Page.navigate") == []
        assert all(w.agent.response(i) is None for i in held)

        await w.proxy.resume()
        await settle()
        assert [p["url"] for p in ids_at_chromium(w, "Page.navigate")] == [
            f"https://example.com/{n}" for n in range(3)
        ]
        answered = [m["id"] for m in w.agent.sent if m.get("id") in held]
        assert answered == held
        assert all("error" not in w.agent.response(i) for i in held)
        # After return, nothing is held.
        later = await w.send("Page.reload", {}, page)
        assert w.agent.response(later) is not None
        await w.chromium.stop()

    run(body)


def test_events_to_the_agent_are_dropped_in_control(tmp_path):
    async def body():
        w = World(tmp_path)
        await w.connect()
        page = await w.attach_page()
        w.chromium.emit("Page.frameNavigated", {"frame": {"id": "F", "url": "https://a.example/"}}, page)
        await settle()
        assert len(w.agent.events("Page.frameNavigated")) == 1

        await w.proxy.pause()
        w.chromium.emit("Page.frameNavigated", {"frame": {"id": "F", "url": "https://b.example/"}}, page)
        w.chromium.emit("Page.screencastFrame", {"data": "AAAA", "sessionId": 1}, page)
        w.chromium.emit("Runtime.bindingCalled", {"name": "b", "payload": "typed"}, page)
        await settle()
        assert len(w.agent.events("Page.frameNavigated")) == 1
        assert w.agent.events("Page.screencastFrame") == []
        assert w.agent.events("Runtime.bindingCalled") == []

        await w.proxy.resume()
        await settle()
        # Dropped means dropped: no event from the control window is replayed
        # as it happened. The frame's state at return is told once
        # (HANDOFF-RESYNC, test_live_cdp_clients.py), so the agent's client
        # knows where its page is.
        assert [e["params"]["frame"]["url"] for e in w.agent.events("Page.frameNavigated")] == [
            "https://a.example/", "https://b.example/"]
        assert w.agent.events("Page.screencastFrame") == []
        assert w.agent.events("Runtime.bindingCalled") == []
        w.chromium.emit("Page.frameNavigated", {"frame": {"id": "F", "url": "https://c.example/"}}, page)
        await settle()
        assert len(w.agent.events("Page.frameNavigated")) == 3
        await w.chromium.stop()

    run(body)


def test_post_data_of_a_control_window_request_is_never_shown_to_the_agent(tmp_path):
    async def body():
        w = World(tmp_path)
        await w.connect()
        page = await w.attach_page()
        body_text = "login=" + secrets.token_hex(4) + "&pw=" + secrets.token_hex(12)

        def sent(request_id: str) -> dict[str, Any]:
            return {
                "requestId": request_id,
                "request": {
                    "url": "https://example.com/session",
                    "method": "POST",
                    "postData": body_text,
                    "hasPostData": True,
                    "postDataEntries": [{"bytes": base64.b64encode(body_text.encode()).decode()}],
                },
            }

        # A request from before control keeps its body: the strip is scoped.
        w.chromium.emit("Network.requestWillBeSent", sent("before"), page)
        await settle()
        assert w.agent.events("Network.requestWillBeSent")[0]["params"]["request"]["postData"] == body_text

        await w.proxy.pause()
        w.chromium.emit("Network.requestWillBeSent", sent("during"), page)
        await settle()
        await w.proxy.resume()
        await settle()

        # The request that started in control redirects after return.
        w.chromium.emit("Network.requestWillBeSent", sent("during"), page)
        await settle()
        after = w.agent.events("Network.requestWillBeSent")[-1]["params"]
        assert after["requestId"] == "during"
        assert "postData" not in after["request"]
        assert "postDataEntries" not in after["request"]
        assert sum(body_text in json.dumps(m) for m in w.agent.sent) == 1  # the "before" event only

        for method in ("Network.getRequestPostData", "Network.getResponseBody"):
            refused = await w.send(method, {"requestId": "during"}, page)
            assert "error" in w.agent.response(refused)
            allowed = await w.send(method, {"requestId": "before"}, page)
            assert "error" not in w.agent.response(allowed)
        assert [p["requestId"] for p in ids_at_chromium(w, "Network.getRequestPostData")] == ["before"]
        assert [p["requestId"] for p in ids_at_chromium(w, "Network.getResponseBody")] == ["before"]
        assert w.refused().count("Network.getRequestPostData") == 1
        assert w.refused().count("Network.getResponseBody") == 1
        await w.chromium.stop()

    run(body)


def test_fetch_request_paused_in_control_is_continued_by_the_proxy(tmp_path):
    async def body():
        w = World(tmp_path)
        await w.connect()
        page = await w.attach_page()
        await w.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, page)

        await w.proxy.pause()
        w.chromium.emit("Fetch.requestPaused", {"requestId": "interception-1", "networkId": "net-1", "request": {"url": "https://example.com/"}}, page)
        w.chromium.emit("Fetch.authRequired", {"requestId": "interception-2", "request": {"url": "https://example.com/"}}, page)
        await settle()
        continued = [m for m in w.chromium.from_agent("Fetch.continueRequest")]
        assert [(m["params"], m.get("sessionId")) for m in continued] == [({"requestId": "interception-1"}, page)]
        auth = w.chromium.from_agent("Fetch.continueWithAuth")
        assert auth[0]["params"] == {"requestId": "interception-2", "authChallengeResponse": {"response": "Default"}}
        assert auth[0]["sessionId"] == page
        assert w.agent.events("Fetch.requestPaused") == []
        assert w.agent.events("Fetch.authRequired") == []

        await w.proxy.resume()
        await settle()
        # Outside control the agent's interception is the agent's to answer.
        w.chromium.emit("Fetch.requestPaused", {"requestId": "interception-3", "request": {"url": "https://example.com/"}}, page)
        await settle()
        assert len(w.agent.events("Fetch.requestPaused")) == 1
        assert len(w.chromium.from_agent("Fetch.continueRequest")) == 1
        # The paused request's network id is a control-window request.
        refused = await w.send("Network.getRequestPostData", {"requestId": "net-1"}, page)
        assert "error" in w.agent.response(refused)
        await w.chromium.stop()

    run(body)


def test_agent_init_scripts_and_bindings_are_removed_in_control_and_restored(tmp_path):
    async def body():
        w = World(tmp_path)
        await w.connect()
        page = await w.attach_page()
        source = "window.__agent_" + secrets.token_hex(3) + " = 1"
        add = await w.send("Page.addScriptToEvaluateOnNewDocument", {"source": source, "worldName": "agent"}, page)
        old_identifier = w.agent.response(add)["result"]["identifier"]
        await w.send("Runtime.addBinding", {"name": "agentBinding"}, page)

        await w.proxy.pause()
        await settle()
        removed = w.chromium.from_agent("Page.removeScriptToEvaluateOnNewDocument")
        assert [(m["params"], m["sessionId"]) for m in removed] == [({"identifier": old_identifier}, page)]
        unbound = w.chromium.from_agent("Runtime.removeBinding")
        assert [(m["params"], m["sessionId"]) for m in unbound] == [({"name": "agentBinding"}, page)]

        await w.proxy.resume()
        await settle()
        readded = w.chromium.from_agent("Page.addScriptToEvaluateOnNewDocument")
        assert len(readded) == 2
        assert readded[1]["params"] == {"source": source, "worldName": "agent"}
        assert readded[1]["sessionId"] == page
        rebound = w.chromium.from_agent("Runtime.addBinding")
        assert [m["params"] for m in rebound] == [{"name": "agentBinding"}, {"name": "agentBinding"}]

        # The agent still holds its original identifier; removing it removes
        # the restored script, not a stale one.
        await w.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": old_identifier}, page)
        removed = w.chromium.from_agent("Page.removeScriptToEvaluateOnNewDocument")
        assert removed[-1]["params"]["identifier"] != old_identifier
        # And a later control window has nothing left to remove.
        before = len(w.chromium.from_agent("Page.removeScriptToEvaluateOnNewDocument"))
        await w.proxy.pause()
        await settle()
        assert len(w.chromium.from_agent("Page.removeScriptToEvaluateOnNewDocument")) == before
        await w.proxy.resume()
        await w.chromium.stop()

    run(body)


def test_new_agent_connections_are_refused_in_control(tmp_path):
    async def body():
        w = World(tmp_path)
        await w.connect()
        await w.proxy.pause()
        second = FakeAgent()
        assert await w.proxy.connect_agent(second) is None
        refused = [f for e, f in w.records if e == "cdp_connection_refused"]
        assert [f["cause"] for f in refused] == ["held_for_handoff"]
        assert len(w.chromium.from_agent("Target.attachToBrowserTarget")) == 1
        await w.proxy.resume()
        assert await w.proxy.connect_agent(FakeAgent()) is not None
        await w.chromium.stop()

    run(body)


def test_a_content_response_completed_in_control_is_withheld(tmp_path):
    """A `Runtime.evaluate` sent before control that resolves during it --
    a promise waiting on the password field's change event -- must not
    carry what the person typed back to the agent."""

    async def body():
        w = World(tmp_path)
        await w.connect()
        page = await w.attach_page()
        w.chromium.defer_methods.add("Runtime.evaluate")
        w.chromium.defer_methods.add("Page.getNavigationHistory")
        evaluate = await w.send("Runtime.evaluate", {"expression": "new Promise(() => {})", "awaitPromise": True}, page)
        history = await w.send("Page.getNavigationHistory", {}, page)
        await w.proxy.pause()
        typed = "typed-" + secrets.token_hex(6)
        w.chromium.respond("Runtime.evaluate", {"result": {"type": "string", "value": typed}})
        w.chromium.respond("Page.getNavigationHistory", {"currentIndex": 0, "entries": []})
        await settle()
        assert w.agent.response(evaluate) is None
        assert w.agent.response(history) is None

        await w.proxy.resume()
        await settle()
        assert "error" in w.agent.response(evaluate)
        assert typed not in json.dumps(w.agent.sent)
        assert w.agent.response(history)["result"] == {"currentIndex": 0, "entries": []}
        await w.chromium.stop()

    run(body)


# --------------------------------------------------------------------------- #
# The method policy, outside control too                                      #
# --------------------------------------------------------------------------- #


REFUSED_OUTSIDE_CONTROL = [
    ("Target.createBrowserContext", {"proxyServer": "http://agent-proxy.example:3128"}),
    ("Target.createBrowserContext", {"proxyBypassList": "<-loopback>"}),
    ("Security.setIgnoreCertificateErrors", {"ignore": True}),
    ("Security.handleCertificateError", {"eventId": 1, "action": "continue"}),
    ("Security.setOverrideCertificateErrors", {"override": True}),
    ("Security.enable", {}),
    ("Security.disable", {}),
    ("Page.setBypassCSP", {"enabled": True}),
    ("Network.setExtraHTTPHeaders", {"headers": {"X-Agent": "1"}}),
    ("Browser.setDownloadBehavior", {"behavior": "allowEverywhere", "downloadPath": "{downloads}"}),
    ("Page.setDownloadBehavior", {"downloadPath": "/home"}),
    ("Debugger.enable", {}),
    ("Target.exposeDevToolsProtocol", {"targetId": "T1"}),
    ("Target.sendMessageToTarget", {"message": "{}", "sessionId": "S1"}),
    ("Target.attachToTarget", {"targetId": "T1"}),
    ("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False}),
    ("Input.setIgnoreInputEvents", {"ignore": True}),
    ("Emulation.setDeviceMetricsOverride", {"width": 1, "height": 1, "deviceScaleFactor": 1, "mobile": False}),
    ("Network.setUserAgentOverride", {"userAgent": "agent"}),
    ("DOM.setFileInputFiles", {"files": ["/etc/passwd"], "nodeId": 1}),
    ("Browser.close", {}),
    # DEVB-GATE: Audits is answered by the gate, never opened to Chromium; its
    # other methods read a response body or a form's state.
    ("Audits.getEncodedResponse", {"requestId": "R1", "encoding": "webp"}),
    ("Audits.checkFormsIssues", {}),
    ("Performance.setTimeDomain", {"timeDomain": "threadTicks"}),
]


@pytest.mark.parametrize(("method", "params"), REFUSED_OUTSIDE_CONTROL, ids=lambda v: v if isinstance(v, str) else "")
def test_the_method_policy_refuses_outside_control(tmp_path, method, params):
    async def body():
        w = World(tmp_path)
        await w.connect()
        page = await w.attach_page()
        filled = json.loads(json.dumps(params).replace("{downloads}", str(w.downloads)))
        before = len(w.chromium.received)
        browser_level = method.split(".")[0] in ("Target", "Browser", "Security")
        request = await w.send(method, filled, None if browser_level else page)
        reply = w.agent.response(request)
        assert reply is not None and "error" in reply
        assert method in reply["error"]["message"]
        assert w.chromium.received[before:] == []
        assert w.refused() == [method]
        # The record names the method, never the parameters.
        for event, fields in w.records:
            if event == "cdp_method_refused":
                assert fields == {"method": method}
        await w.chromium.stop()

    run(body)


ALLOWED_CONTROLS = [
    ("Target.createBrowserContext", {"disposeOnDetach": True}),
    ("Browser.setDownloadBehavior", {"behavior": "allowAndName", "downloadPath": "{downloads}/playwright-artifacts-x", "eventsEnabled": True}),
    ("Browser.setDownloadBehavior", {"behavior": "deny"}),
    ("Browser.getVersion", {}),
    ("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}),
    ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
    ("Network.enable", {}),
    ("DOM.setFileInputFiles", {"files": ["{uploads}/report.pdf"], "nodeId": 1}),
    ("Runtime.evaluate", {"expression": "1"}),
    ("Storage.getStorageKeyForFrame", {"frameId": "f1"}),
    # DEVB-GATE: Puppeteer's CdpPage#initialize sends Performance.enable and
    # fails newPage() on an error; page.metrics() reads the counters.
    ("Performance.enable", {}),
    ("Performance.getMetrics", {}),
    ("Performance.disable", {}),
]


@pytest.mark.parametrize(("method", "params"), ALLOWED_CONTROLS, ids=lambda v: v if isinstance(v, str) else "")
def test_the_method_policy_forwards_what_playwright_needs(tmp_path, method, params):
    """The control for the refusals above: the same check lets these through."""

    async def body():
        w = World(tmp_path)
        await w.connect()
        page = await w.attach_page()
        filled = json.loads(
            json.dumps(params).replace("{downloads}", str(w.downloads)).replace("{uploads}", str(w.uploads))
        )
        browser_level = method.split(".")[0] in ("Target", "Browser")
        request = await w.send(method, filled, None if browser_level else page)
        assert "error" not in w.agent.response(request)
        assert w.chromium.received[-1]["method"] == method
        assert w.refused() == []
        await w.chromium.stop()

    run(body)


# LB-POLISH (part of #1030). MEASURED 2026-10-13 on task_1f28d5bfa5a14f618f83:
# the QA agent's `connect_over_cdp` was refused on Browser.setDownloadBehavior,
# because Playwright names ITS OWN artifacts directory (a mkdtemp under the
# client's tmpdir) as the download path, and the agent fell back to raw CDP.
# A path outside downloads/ is now confined to downloads/, never forwarded.
CONFINED_DOWNLOADS = [
    ("Browser.setDownloadBehavior", {"behavior": "allowAndName", "downloadPath": "/tmp/playwright-artifacts-Xy12Ab", "eventsEnabled": True}),
    ("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": "/tmp/elsewhere"}),
    ("Browser.setDownloadBehavior", {"behavior": "allowAndName", "downloadPath": "{downloads}/../profile"}),
    ("Browser.setDownloadBehavior", {"behavior": "allow"}),
    ("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": "relative/dir"}),
    ("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": "/home"}),
]


@pytest.mark.parametrize(("method", "params"), CONFINED_DOWNLOADS, ids=lambda v: v if isinstance(v, str) else "")
def test_a_download_path_outside_downloads_reaches_chromium_as_downloads(tmp_path, method, params):
    async def body():
        w = World(tmp_path)
        await w.connect()
        page = await w.attach_page()
        filled = json.loads(json.dumps(params).replace("{downloads}", str(w.downloads)))
        browser_level = method.startswith("Browser.")
        request = await w.send(method, filled, None if browser_level else page)
        assert "error" not in w.agent.response(request)
        assert w.refused() == []
        sent = w.chromium.received[-1]
        assert sent["method"] == method
        # Only the path changes: Chromium writes into downloads/ and nowhere else.
        assert sent["params"] == {**filled, "downloadPath": str(w.downloads)}
        await w.chromium.stop()

    run(body)


def test_a_download_path_inside_downloads_and_deny_reach_chromium_unchanged(tmp_path):
    async def body():
        w = World(tmp_path)
        await w.connect()
        inside = {"behavior": "allowAndName", "downloadPath": str(w.downloads / "sub"), "eventsEnabled": True}
        for params in (inside, {"behavior": "deny"}, {"behavior": "default"}):
            await w.send("Browser.setDownloadBehavior", params)
            assert w.chromium.received[-1]["params"] == params
        assert w.refused() == []
        await w.chromium.stop()

    run(body)


def test_playwright_connect_over_cdp_handshake_passes_the_policy(tmp_path):
    """What Playwright's `chromium.connect_over_cdp` sends (crBrowser.connect,
    CRBrowserContext._initialize, FrameSession._initialize), in order, with
    the parameters it sends them with: every one reaches Chromium, and the
    default context's downloads land in downloads/."""

    async def body():
        w = World(tmp_path)
        await w.connect()
        artifacts = "/tmp/playwright-artifacts-" + secrets.token_hex(3)
        browser = [
            ("Browser.getVersion", {}),
            ("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}),
            ("Browser.setDownloadBehavior", {"behavior": "allowAndName", "downloadPath": artifacts, "eventsEnabled": True}),
        ]
        for method, params in browser:
            request = await w.send(method, params)
            assert "error" not in (w.agent.response(request) or {}), method
        page = await w.attach_page()
        frame = [
            ("Page.enable", {}),
            ("Page.getFrameTree", {}),
            ("Log.enable", {}),
            ("Page.setLifecycleEventsEnabled", {"enabled": True}),
            ("Runtime.enable", {}),
            ("Page.addScriptToEvaluateOnNewDocument", {"source": "", "worldName": "__playwright_utility_world__"}),
            ("Runtime.addBinding", {"name": "__playwright__binding__"}),
            ("Network.enable", {}),
            ("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}),
            ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
            ("Emulation.setEmulatedMedia", {"media": "", "features": []}),
            ("Page.createIsolatedWorld", {"frameId": "F1", "grantUniveralAccess": True, "worldName": "__playwright_utility_world__"}),
            ("Runtime.runIfWaitingForDebugger", {}),
        ]
        for method, params in frame:
            request = await w.send(method, params, page)
            assert "error" not in (w.agent.response(request) or {}), method
        assert w.refused() == []
        downloads = ids_at_chromium(w, "Browser.setDownloadBehavior")
        assert [d["downloadPath"] for d in downloads] == [str(w.downloads)]
        await w.chromium.stop()

    run(body)


def test_the_agents_mouse_position_is_told_to_the_gate_and_nothing_else_of_its_input(tmp_path):
    """LB-CURSOR: the gate paints the pointer where the agent's last mouse event was."""
    async def body():
        moved: list[tuple[float, float]] = []
        w = World(tmp_path, on_pointer=lambda x, y: moved.append((x, y)))
        await w.connect()
        page = await w.attach_page()
        await w.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 120.5, "y": 44}, page)
        await w.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 121, "y": 45,
                                                  "button": "left", "clickCount": 1}, page)
        await w.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a"}, page)
        await w.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": "12", "y": None}, page)
        await w.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": True, "y": 3}, page)
        assert moved == [(120.5, 44.0), (121.0, 45.0)]
        assert len(ids_at_chromium(w, "Input.dispatchMouseEvent")) == 4
        await w.chromium.stop()

    run(body)


def test_an_agent_cannot_address_a_session_it_does_not_own(tmp_path):
    async def body():
        w = World(tmp_path)
        await w.connect()
        await w.proxy.pause()
        target = await w.proxy.open_fresh_page("https://github.com/")
        gate_session = next(s for s, t in w.proxy._gate_sessions.items() if t == target)
        await w.proxy.resume()
        await settle()
        before = len(w.chromium.received)
        request = await w.send("Runtime.evaluate", {"expression": "document.cookie"}, gate_session)
        assert "error" in w.agent.response(request)
        assert w.chromium.received[before:] == []
        await w.chromium.stop()

    run(body)


@pytest.mark.parametrize("method", ["Audits.enable", "Audits.disable"])
def test_audits_is_answered_by_the_gate_and_never_reaches_chromium(tmp_path, method):
    """DEVB-GATE: Puppeteer's FrameManager.initialize fails newPage() when
    Audits.enable errors, and Audits.issueAdded can carry a Set-Cookie line
    (`rawCookieLine`). So the gate says yes and Chromium is never asked."""

    async def body():
        w = World(tmp_path)
        await w.connect()
        page = await w.attach_page()
        before = len(w.chromium.received)
        request = await w.send(method, {}, page)
        assert w.agent.response(request) == {"id": request, "result": {}, "sessionId": page}
        assert w.chromium.received[before:] == []
        assert w.refused() == []
        # Held in control like any command, and answered on return.
        await w.proxy.pause()
        held = await w.send(method, {}, page)
        assert w.agent.response(held) is None
        await w.proxy.resume()
        await settle()
        assert w.agent.response(held) == {"id": held, "result": {}, "sessionId": page}
        assert [m for m in w.chromium.received if m["method"].startswith("Audits.")] == []
        await w.chromium.stop()

    run(body)


@pytest.mark.parametrize("method", ["WebMCP.enable", "Network.emulateNetworkConditionsByRule"])
def test_a_probe_puppeteer_survives_is_refused_quietly(tmp_path, caplog, method):
    """ANALYSIS issue 19: refused on every page Puppeteer opens, these were
    noise in the refusal record. Still refused, still not forwarded; counted,
    and logged at debug only."""

    async def body():
        w = World(tmp_path)
        await w.connect()
        page = await w.attach_page()
        before = len(w.chromium.received)
        for _ in range(2):
            request = await w.send(method, {}, page)
            assert "error" in w.agent.response(request)
        assert w.chromium.received[before:] == []
        assert w.refused() == []
        assert w.proxy.quiet_refusals == {method: 2}
        await w.chromium.stop()

    with caplog.at_level("DEBUG", logger="agent_worker.livegate"):
        run(body)
    lines = [r for r in caplog.records if method in r.getMessage()]
    assert lines and all(r.levelname == "DEBUG" for r in lines)
    assert "2" in lines[-1].getMessage()


def test_the_policy_is_one_allowlist_with_a_named_denylist():
    paths = PolicyPaths(downloads_dir=Path("/run/downloads"), uploads_dir=Path("/run/uploads"))
    assert cdppolicy.check("Page.navigate", {"url": "https://example.com/"}, paths) is None
    for method in cdppolicy.DENIED:
        params = {"proxyServer": "x"} if method.startswith("Target.") else {"files": ["/etc/hosts"]}
        refusal = cdppolicy.check(method, params, paths)
        assert refusal is not None, method
    # A method Chromium adds next year is refused because it is not listed.
    assert cdppolicy.check("Page.someFutureMethod", {}, paths) is not None
    assert not any(m.startswith("Security.") for m in cdppolicy.ALLOWED_METHODS)


# --------------------------------------------------------------------------- #
# The fresh page and the certificate rule                                     #
# --------------------------------------------------------------------------- #


def test_the_fresh_page_opens_in_a_gate_owned_context_with_no_proxy(tmp_path):
    async def body():
        w = World(tmp_path)
        await w.connect()
        await w.attach_page()
        await w.proxy.pause()
        target = await w.proxy.open_fresh_page("https://github.com/login")
        await settle()

        created = w.chromium.from_agent("Target.createBrowserContext")
        assert len(created) == 1
        assert "sessionId" not in created[0]  # the gate's own root session
        assert not {"proxyServer", "proxyBypassList", "originsWithUniversalNetworkAccess"} & set(created[0]["params"])
        context = w.chromium.targets[target]
        downloads = [m for m in w.chromium.from_agent("Browser.setDownloadBehavior") if m["params"].get("browserContextId") == context]
        assert downloads and downloads[0]["params"]["downloadPath"] == str(w.downloads)
        opened = w.chromium.from_agent("Target.createTarget")[-1]["params"]
        assert opened == {"url": "https://github.com/login", "browserContextId": context}

        # The agent's auto-attach reaches the new page; the proxy detaches it so
        # the page neither waits on a held debugger nor reports to the agent.
        w.chromium.emit(
            "Target.attachedToTarget",
            {"sessionId": "S-auto", "targetInfo": {"targetId": target, "type": "page", "browserContextId": context, "url": "https://github.com/login"}, "waitingForDebugger": True},
            w.conn.browser_session,
        )
        await settle()
        detach = w.chromium.from_agent("Target.detachFromTarget")
        assert [(m["params"], m.get("sessionId")) for m in detach] == [({"sessionId": "S-auto"}, w.conn.browser_session)]
        assert all(e["params"]["targetInfo"]["targetId"] != target for e in w.agent.events("Target.attachedToTarget"))

        # On return the context is the agent's: the proxy attaches it there.
        await w.proxy.resume()
        await settle()
        attach = [m for m in w.chromium.from_agent("Target.attachToTarget") if m.get("sessionId") == w.conn.browser_session]
        assert [m["params"] for m in attach] == [{"targetId": target, "flatten": True}]
        assert any(e["params"]["targetInfo"]["targetId"] == target for e in w.agent.events("Target.attachedToTarget"))
        await w.chromium.stop()

    run(body)


def _security(state: str) -> dict[str, Any]:
    return {"visibleSecurityState": {"securityState": state, "securityStateIssueIds": []}}


def test_control_is_refused_and_ended_while_the_page_reports_a_certificate_error(tmp_path):
    async def body():
        w = World(tmp_path)
        await w.connect()
        await w.proxy.pause()
        target = await w.proxy.open_fresh_page("https://github.com/login")
        gate_session = next(s for s, t in w.proxy._gate_sessions.items() if t == target)
        assert any(m.get("sessionId") == gate_session for m in w.chromium.from_agent("Security.enable"))

        # No security state yet: fail closed.
        decision = await w.proxy.take_control(target)
        assert not decision.granted and decision.cause == "security_state_unknown"

        w.chromium.emit("Security.visibleSecurityStateChanged", _security("insecure-broken"), gate_session)
        await settle()
        decision = await w.proxy.take_control(target)
        assert not decision.granted and decision.cause == "insecure_connection"
        assert not w.proxy.in_control
        assert ("control_refused", {"cause": "insecure_connection"}) in w.records

        w.chromium.emit("Security.visibleSecurityStateChanged", _security("secure"), gate_session)
        await settle()
        decision = await w.proxy.take_control(target)
        assert decision.granted and w.proxy.in_control

        w.chromium.emit("Security.visibleSecurityStateChanged", _security("insecure"), gate_session)
        await settle()
        assert not w.proxy.in_control
        assert w.ended == ["insecure_connection"]
        assert ("control_ended", {"cause": "insecure_connection"}) in w.records
        # Control ended; the agent is still held until the worker returns it.
        assert w.proxy.paused
        await w.chromium.stop()

    run(body)


def test_control_is_refused_on_plain_http_or_an_address(tmp_path):
    async def body():
        w = World(tmp_path)
        await w.connect()
        await w.proxy.pause()
        for url in ("http://github.com/login", "https://192.0.2.7/login"):
            target = f"T-{secrets.token_hex(2)}"
            w.chromium.urls[target] = url
            w.chromium.targets[target] = "DEFAULT"
            await w.proxy.watch_page(target)
            gate_session = next(s for s, t in w.proxy._gate_sessions.items() if t == target)
            w.chromium.emit("Security.visibleSecurityStateChanged", _security("secure"), gate_session)
            await settle()
            decision = await w.proxy.take_control(target)
            assert not decision.granted and decision.cause == "insecure_connection", url
        await w.chromium.stop()

    run(body)


def _navigated(url: str) -> dict[str, Any]:
    return {"frame": {"id": "F-top", "loaderId": "L1", "url": url, "securityOrigin": "", "mimeType": "text/html"}}


def test_a_page_still_on_about_blank_is_not_refused_for_its_scheme(tmp_path):
    """LB-R4 (part of #1030). MEASURED 2026-10-12 on SwarmCloud: a person took
    control of the agent's page before its first navigation and the timeline
    read "You took control", then "Taking control was refused: the page is not
    on a secure https connection" -- for about:blank, which has no origin and
    so no scheme to be insecure about. Control is granted there, with no wait
    on a security state about:blank never reports as `secure`, and the
    certificate rule takes over at the first navigation."""

    async def body():
        w = World(tmp_path)
        await w.connect()
        await w.proxy.pause()
        target = "T-blank"
        w.chromium.urls[target] = "about:blank"
        w.chromium.targets[target] = "DEFAULT"

        # No security state reported at all: about:blank is still not refused.
        decision = await w.proxy.take_control(target)
        assert decision.granted and decision.cause is None
        assert w.proxy.in_control
        assert not [r for r in w.records if r[0] == "control_refused"]
        gate_session = next(s for s, t in w.proxy._gate_sessions.items() if t == target)
        assert any(m.get("sessionId") == gate_session for m in w.chromium.from_agent("Page.enable"))

        # about:blank's own state (`neutral`) arriving late does not end it.
        w.chromium.emit("Security.visibleSecurityStateChanged", _security("neutral"), gate_session)
        await settle()
        assert w.proxy.in_control and w.ended == []

        # The first navigation goes to a secure https page: control holds, and
        # from there the page is held to the certificate rule as any other.
        w.chromium.urls[target] = "https://github.com/login"
        w.chromium.emit("Page.frameNavigated", _navigated("https://github.com/login"), gate_session)
        w.chromium.emit("Security.visibleSecurityStateChanged", _security("secure"), gate_session)
        await settle()
        assert w.proxy.in_control and w.ended == []
        w.chromium.emit("Security.visibleSecurityStateChanged", _security("insecure-broken"), gate_session)
        await settle()
        assert not w.proxy.in_control
        assert w.ended == ["insecure_connection"]
        await w.chromium.stop()

    run(body)


@pytest.mark.parametrize("url", ["http://github.com/login", "https://192.0.2.7/login"])
@pytest.mark.parametrize("order", ["navigated_first", "security_first", "navigated_only", "security_only"])
def test_control_taken_on_about_blank_ends_when_it_leaves_for_an_insecure_page(tmp_path, url, order):
    """Granting on about:blank is not a hole in the certificate rule: the
    first navigation to plain http or an IP literal ends control, whichever of
    Chromium's two signals arrives first, or if only one ever does."""

    async def body():
        w = World(tmp_path)
        await w.connect()
        await w.proxy.pause()
        target = "T-blank"
        w.chromium.urls[target] = "about:blank"
        w.chromium.targets[target] = "DEFAULT"
        assert (await w.proxy.take_control(target)).granted
        gate_session = next(s for s, t in w.proxy._gate_sessions.items() if t == target)
        w.chromium.urls[target] = url
        if order in ("navigated_first", "navigated_only"):
            w.chromium.emit("Page.frameNavigated", _navigated(url), gate_session)
        if order != "navigated_only":
            w.chromium.emit("Security.visibleSecurityStateChanged", _security("insecure"), gate_session)
        if order == "security_first":
            w.chromium.emit("Page.frameNavigated", _navigated(url), gate_session)
        await settle()
        assert not w.proxy.in_control, (url, order)
        assert w.ended == ["insecure_connection"]
        await w.chromium.stop()

    run(body)


def test_a_subframe_navigation_does_not_end_control_on_about_blank(tmp_path):
    async def body():
        w = World(tmp_path)
        await w.connect()
        await w.proxy.pause()
        target = "T-blank"
        w.chromium.urls[target] = "about:blank"
        w.chromium.targets[target] = "DEFAULT"
        assert (await w.proxy.take_control(target)).granted
        gate_session = next(s for s, t in w.proxy._gate_sessions.items() if t == target)
        frame = {**_navigated("http://ads.example/")["frame"], "id": "F-child", "parentId": "F-top"}
        w.chromium.emit("Page.frameNavigated", {"frame": frame}, gate_session)
        await settle()
        assert w.proxy.in_control and w.ended == []
        await w.chromium.stop()

    run(body)


def test_control_needs_the_agent_paused_first(tmp_path):
    async def body():
        w = World(tmp_path)
        await w.connect()
        decision = await w.proxy.take_control("T-any")
        assert not decision.granted and decision.cause == "not_paused"
        with pytest.raises(ValueError):
            await w.proxy.open_fresh_page("https://github.com/")
        await w.proxy.pause()
        with pytest.raises(ValueError):
            await w.proxy.open_fresh_page("http://github.com/")
        await w.chromium.stop()

    run(body)


def test_return_clears_password_inputs_before_the_agent_is_released(tmp_path):
    async def body():
        w = World(tmp_path)
        await w.connect()
        page = await w.attach_page()
        await w.proxy.pause()
        target = await w.proxy.open_fresh_page("https://github.com/login")
        gate_session = next(s for s, t in w.proxy._gate_sessions.items() if t == target)
        held = await w.send("Page.reload", {}, page)
        await w.proxy.resume()
        await settle()
        methods = [(m["method"], m.get("sessionId")) for m in w.chromium.received]
        clear = methods.index(("Runtime.evaluate", gate_session))
        released = methods.index(("Page.reload", page))
        assert clear < released
        assert w.agent.response(held) is not None
        await w.chromium.stop()

    run(body)


# --------------------------------------------------------------------------- #
# The script registry on its own                                              #
# --------------------------------------------------------------------------- #


def test_the_registry_translates_identifiers_across_a_restore():
    registry = ScriptRegistry()
    registry.added_script("S1", "7", {"source": "x"})
    registry.added_binding("S1", {"name": "b"})
    assert registry.removal_commands() == [
        ("S1", "Page.removeScriptToEvaluateOnNewDocument", {"identifier": "7"}),
        ("S1", "Runtime.removeBinding", {"name": "b"}),
    ]
    restores = registry.restore_commands()
    assert [(s, m, p) for s, m, p, _ in restores] == [
        ("S1", "Page.addScriptToEvaluateOnNewDocument", {"source": "x"}),
        ("S1", "Runtime.addBinding", {"name": "b"}),
    ]
    registry.restored_script("S1", restores[0][3], "9")
    assert registry.removed_script("S1", "7") == "9"
    assert registry.removal_commands() == [("S1", "Runtime.removeBinding", {"name": "b"})]
    registry.forget_session("S1")
    assert registry.removal_commands() == []


# --------------------------------------------------------------------------- #
# The transports: the agent's websocket and Chromium's pipe                   #
# --------------------------------------------------------------------------- #


async def _http(port: int, request: bytes) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    return reader, writer, head


def _frame(payload: bytes) -> bytes:
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    length = len(payload)
    if length < 126:
        header = bytes([0x81, 0x80 | length])
    else:
        header = bytes([0x81, 0x80 | 126]) + length.to_bytes(2, "big")
    return header + mask + masked


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    first = await reader.readexactly(2)
    length = first[1] & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    return await reader.readexactly(length)


def test_the_agent_endpoint_speaks_json_version_and_websocket(tmp_path):
    async def body():
        w = World(tmp_path)
        server = await serve_agents(w.proxy, host="127.0.0.1", port=0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer, head = await _http(port, f"GET /json/version/ HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            assert head.startswith(b"HTTP/1.1 200")
            length = int(next(line for line in head.split(b"\r\n") if line.lower().startswith(b"content-length")).split(b":")[1])
            version = json.loads(await reader.readexactly(length))
            writer.close()
            ws_url = version["webSocketDebuggerUrl"]
            assert ws_url.startswith(f"ws://127.0.0.1:{port}/devtools/browser/")
            path = ws_url[len(f"ws://127.0.0.1:{port}"):]

            key = base64.b64encode(os.urandom(16)).decode()
            upgrade = (
                f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
            reader, writer, head = await _http(port, upgrade)
            assert head.startswith(b"HTTP/1.1 101")
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            assert b"Sec-WebSocket-Accept: " + accept in head
            writer.write(_frame(json.dumps({"id": 1, "method": "Browser.getVersion"}).encode()))
            await writer.drain()
            reply = json.loads(await asyncio.wait_for(_read_frame(reader), 2))
            assert reply["id"] == 1 and "result" in reply
            writer.write(_frame(json.dumps({"id": 2, "method": "Security.setIgnoreCertificateErrors", "params": {"ignore": True}}).encode()))
            await writer.drain()
            reply = json.loads(await asyncio.wait_for(_read_frame(reader), 2))
            assert reply["id"] == 2 and "error" in reply
            writer.close()

            # A web page in the pod reaching 127.0.0.1:9222 sends an Origin; refused.
            _, writer, head = await _http(port, upgrade.replace(b"\r\n\r\n", b"\r\nOrigin: https://evil.example\r\n\r\n"))
            assert head.startswith(b"HTTP/1.1 403")
            writer.close()
            # A rebound DNS name is not 127.0.0.1; refused.
            _, writer, head = await _http(port, upgrade.replace(f"Host: 127.0.0.1:{port}".encode(), b"Host: rebound.example:9222"))
            assert head.startswith(b"HTTP/1.1 403")
            writer.close()
            # In control, a new connection is refused before the upgrade.
            await w.proxy.pause()
            _, writer, head = await _http(port, upgrade)
            assert head.startswith(b"HTTP/1.1 503")
            writer.close()
        finally:
            server.close()
            await w.chromium.stop()

    run(body)


def test_the_pipe_transport_frames_messages_with_nul(tmp_path):
    async def body():
        to_chromium_r, to_chromium_w = os.pipe()
        from_chromium_r, from_chromium_w = os.pipe()
        transport = PipeTransport(read_fd=from_chromium_r, write_fd=to_chromium_w)
        received: list[dict[str, Any]] = []

        class Sink:
            async def on_chromium_message(self, message):
                received.append(message)

        await transport.open()
        reader_task = asyncio.create_task(transport.run(Sink()))
        await transport.send({"id": 1, "method": "Browser.getVersion"})
        await transport.send({"id": 2, "method": "Target.getTargets"})
        await asyncio.sleep(0.05)
        written = os.read(to_chromium_r, 65536)
        assert written.split(b"\0")[:-1] == [
            json.dumps({"id": 1, "method": "Browser.getVersion"}, separators=(",", ":")).encode(),
            json.dumps({"id": 2, "method": "Target.getTargets"}, separators=(",", ":")).encode(),
        ]
        os.write(from_chromium_w, b'{"id":1,"result":{}}\0{"method":"Target.targetCreated",')
        os.write(from_chromium_w, b'"params":{}}\0')
        os.close(from_chromium_w)
        await asyncio.wait_for(reader_task, 2)
        assert received == [{"id": 1, "result": {}}, {"method": "Target.targetCreated", "params": {}}]
        await transport.close()
        os.close(to_chromium_r)

    run(body)
