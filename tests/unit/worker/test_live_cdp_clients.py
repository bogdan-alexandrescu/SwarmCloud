"""Puppeteer through the live gate, and both browser tools across a hand-off (DEVB-GATE).

Two questions, each answered against the real `CdpProxy` and method policy:

* Can puppeteer-core 25.8.0 (dev-browser v1's driver) open a page through the
  gate? Its sequence for connect + newPage + goto + evaluate + screenshot is a
  fixture derived from its source (fixtures/puppeteer-core-25.8.0-*.json); every
  method Puppeteer treats as fatal must get a non-error reply.
* Does an agent get its page back after a person had control? While a person
  is in control the proxy drops every event to the agent, so a navigation the
  person made leaves the agent's client holding execution contexts Chromium no
  longer has. The first `Runtime.evaluate` after return must succeed, for a
  Playwright-shaped and a Puppeteer-shaped client.

The Chromium here is a model, not a recording: it keeps targets, sessions,
frames and execution contexts, emits the events a real one emits for them, and
refuses an evaluate in a context it does not have -- which is the failure this
file exists to catch. No test runs a browser, a network or a cloud credential.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import secrets
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate.cdppolicy import PolicyPaths
from agent_worker.livegate.cdpproxy import CdpProxy

FIXTURE = Path(__file__).parent / "fixtures" / "puppeteer-core-25.8.0-connect-newpage-goto-evaluate-screenshot.json"
PUPPETEER = json.loads(FIXTURE.read_text())
PUPPETEER_WORLD = "__puppeteer_utility_world__" + PUPPETEER["version"]
PLAYWRIGHT_WORLD = "__playwright_utility_world__"


def run(body):
    asyncio.run(body())


async def settle() -> None:
    for _ in range(300):
        await asyncio.sleep(0)


class ModelChromium:
    """A browser behind `--remote-debugging-pipe`, modelled far enough to hold
    execution contexts honestly.

    A target is a tab with one page; the page's main frame id is its targetId.
    A session is attached to the browser, a tab or a page. Execution contexts
    belong to the page and are reported to every session on it that enabled
    Runtime -- the gate's and the agent's alike -- as Chromium does.
    """

    def __init__(self) -> None:
        self.proxy: CdpProxy | None = None
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.received: list[dict[str, Any]] = []
        self._ids = itertools.count(1)
        self.pages: dict[str, dict[str, Any]] = {}  # page targetId -> state
        self.sessions: dict[str, dict[str, Any]] = {}  # sessionId -> state
        self._pump: asyncio.Task[None] | None = None

    def start(self, proxy: CdpProxy) -> None:
        self.proxy = proxy
        self._pump = asyncio.create_task(self._run())

    async def _run(self) -> None:
        assert self.proxy is not None
        while True:
            message = await self.queue.get()
            await self.proxy.on_chromium_message(message)

    async def stop(self) -> None:
        if self._pump is not None:
            self._pump.cancel()

    def _id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids)}"

    def emit(self, method: str, params: dict[str, Any], session: str | None) -> None:
        message: dict[str, Any] = {"method": method, "params": params}
        if session is not None:
            message["sessionId"] = session
        self.queue.put_nowait(message)

    # ----------------------------------------------------------- the model

    def _session(self, kind: str, target: str | None, parent: str | None) -> str:
        sid = self._id("S")
        self.sessions[sid] = {"kind": kind, "target": target, "parent": parent, "runtime": False,
                              "page": False, "lifecycle": False, "auto": None, "scripts": {}}
        return sid

    def _info(self, target: str, kind: str) -> dict[str, Any]:
        page = self.pages[target if kind == "page" else target.removeprefix("TAB-")]
        return {"targetId": target, "type": kind, "url": page["url"], "title": "", "attached": True,
                "browserContextId": "DEFAULT"}

    def _attach(self, parent: str | None, kind: str, target: str, waiting: bool) -> str:
        sid = self._session(kind, target if kind == "page" else target.removeprefix("TAB-"), parent)
        self.sessions[sid]["tab"] = kind == "tab"
        self.emit("Target.attachedToTarget",
                  {"sessionId": sid, "targetInfo": self._info(target, kind), "waitingForDebugger": waiting}, parent)
        return sid

    def _auto_attach(self, sid: str, page: str) -> None:
        """`sid` auto-attaches: a browser session to a page, or to its tab when
        pages are filtered out (Puppeteer); a tab session to its page."""
        state = self.sessions[sid]
        auto = state["auto"]
        if not auto:
            return
        excluded = any(f.get("type") == "page" and f.get("exclude") for f in auto.get("filter") or [])
        if state["kind"] == "browser":
            if excluded:
                self._attach(sid, "tab", "TAB-" + page, bool(auto.get("waitForDebuggerOnStart")))
            else:
                self._attach(sid, "page", page, bool(auto.get("waitForDebuggerOnStart")))
        elif state.get("tab") and state["target"] == page:
            self._attach(sid, "page", page, bool(auto.get("waitForDebuggerOnStart")))

    def new_page(self, url: str = "about:blank") -> str:
        target = self._id("T")
        self.pages[target] = {"url": url, "loader": self._id("L"), "contexts": {}}
        self._new_context(target, None)
        for sid in list(self.sessions):
            self._auto_attach(sid, target)
        return target

    def _runtime_sessions(self, target: str) -> list[str]:
        return [s for s, st in self.sessions.items() if st["target"] == target and st["kind"] == "page" and st["runtime"]]

    def _new_context(self, target: str, world: str | None) -> int:
        cid = next(self._ids)
        page = self.pages[target]
        origin = page["url"].split("/", 3)[:3]
        context = {
            "id": cid,
            "origin": "/".join(origin) if page["url"].startswith("http") else "://",
            "name": world or "",
            "uniqueId": f"{cid}.{secrets.token_hex(4)}",
            "auxData": {"isDefault": world is None, "type": "default" if world is None else "isolated",
                        "frameId": target},
        }
        page["contexts"][cid] = context
        for sid in self._runtime_sessions(target):
            self.emit("Runtime.executionContextCreated", {"context": context}, sid)
        return cid

    def frame(self, target: str) -> dict[str, Any]:
        page = self.pages[target]
        return {"id": target, "loaderId": page["loader"], "url": page["url"], "domainAndRegistry": "",
                "securityOrigin": page["url"], "mimeType": "text/html", "secureContextType": "Secure",
                "crossOriginIsolatedContextType": "NotIsolated", "gatedAPIFeatures": []}

    def navigate(self, target: str, url: str) -> None:
        """A new document in the page's main frame: what a person's click does."""
        page = self.pages[target]
        on_page = [s for s, st in self.sessions.items() if st["target"] == target and st["kind"] == "page"]
        for sid in on_page:
            if self.sessions[sid]["runtime"]:
                self.emit("Runtime.executionContextsCleared", {}, sid)
        page["contexts"].clear()
        page["url"] = url
        page["loader"] = self._id("L")
        for sid in on_page:
            if self.sessions[sid]["page"]:
                self.emit("Page.frameStartedLoading", {"frameId": target}, sid)
                self.emit("Page.frameNavigated", {"frame": self.frame(target), "type": "Navigation"}, sid)
        self._new_context(target, None)
        for sid in on_page:
            for script in self.sessions[sid]["scripts"].values():
                if script.get("worldName"):
                    self._new_context(target, script["worldName"])
        for sid in on_page:
            if self.sessions[sid]["lifecycle"]:
                for name in ("init", "DOMContentLoaded", "load"):
                    self.emit("Page.lifecycleEvent", {"frameId": target, "loaderId": page["loader"], "name": name,
                                                      "timestamp": 1.0}, sid)
            if self.sessions[sid]["page"]:
                self.emit("Page.frameStoppedLoading", {"frameId": target}, sid)
        for sid, state in self.sessions.items():
            if state["kind"] == "browser":
                self.emit("Target.targetInfoChanged", {"targetInfo": self._info(target, "page")}, sid)

    # ------------------------------------------------------------ commands

    async def send(self, message: dict[str, Any]) -> None:
        self.received.append(message)
        reply = self._answer(message["method"], message.get("params") or {}, message.get("sessionId"))
        out: dict[str, Any] = {"id": message["id"]}
        if isinstance(reply, str):
            out["error"] = {"code": -32000, "message": reply}
        else:
            out["result"] = reply
        if "sessionId" in message:
            out["sessionId"] = message["sessionId"]
        self.queue.put_nowait(out)

    def _context_ok(self, sid: str | None, cid: Any) -> bool:
        target = self.sessions.get(sid or "", {}).get("target")
        return target in self.pages and cid in self.pages[target]["contexts"]

    def _answer(self, method: str, params: dict[str, Any], sid: str | None) -> dict[str, Any] | str:
        state = self.sessions.get(sid) if sid is not None else None
        target = state["target"] if state else None
        if method == "Target.attachToBrowserTarget":
            return {"sessionId": self._session("browser", None, None)}
        if method == "Target.setAutoAttach":
            if state is None:
                return {}
            state["auto"] = params if params.get("autoAttach") else None
            for page in list(self.pages):
                if not any(s["parent"] == sid and s["target"] == page for s in self.sessions.values()):
                    self._auto_attach(sid, page)
            return {}
        if method == "Target.attachToTarget":
            return {"sessionId": self._attach(sid, "page", params["targetId"], False)}
        if method == "Target.detachFromTarget":
            gone = params.get("sessionId")
            if gone in self.sessions:
                parent = self.sessions.pop(gone)["parent"]
                self.emit("Target.detachedFromTarget", {"sessionId": gone}, parent)
            return {}
        if method == "Target.createTarget":
            return {"targetId": self.new_page(params.get("url", "about:blank"))}
        if method == "Target.getTargets":
            return {"targetInfos": [self._info(t, "page") for t in self.pages]}
        if method == "Target.getTargetInfo":
            return {"targetInfo": self._info(params["targetId"], "page")}
        if method == "Target.getBrowserContexts":
            return {"browserContextIds": []}
        if method == "Browser.getVersion":
            return {"protocolVersion": "1.3", "product": "Chrome/141.0.0.0"}
        if method == "Security.enable":
            self.emit("Security.visibleSecurityStateChanged",
                      {"visibleSecurityState": {"securityState": "secure"}}, sid)
            return {}
        if method == "Page.enable" and state:
            state["page"] = True
            return {}
        if method == "Page.setLifecycleEventsEnabled" and state:
            state["lifecycle"] = bool(params.get("enabled"))
            return {}
        if method == "Page.getFrameTree" and target:
            return {"frameTree": {"frame": self.frame(target), "childFrames": []}}
        if method == "Runtime.enable" and state and target:
            if not state["runtime"]:
                state["runtime"] = True
                for context in self.pages[target]["contexts"].values():
                    self.emit("Runtime.executionContextCreated", {"context": context}, sid)
            return {}
        if method == "Page.addScriptToEvaluateOnNewDocument" and state:
            identifier = self._id("")
            state["scripts"][identifier] = dict(params)
            return {"identifier": identifier}
        if method == "Page.removeScriptToEvaluateOnNewDocument" and state:
            state["scripts"].pop(params.get("identifier"), None)
            return {}
        if method == "Page.createIsolatedWorld" and target:
            return {"executionContextId": self._new_context(target, params.get("worldName"))}
        if method == "Page.navigate" and target:
            self.navigate(target, params["url"])
            return {"frameId": target, "loaderId": self.pages[target]["loader"]}
        if method == "Runtime.evaluate":
            if "contextId" in params and not self._context_ok(sid, params["contextId"]):
                return "Cannot find context with specified id"
            return {"result": {"type": "string", "value": self.pages[target]["url"] if target else ""}}
        if method == "Runtime.callFunctionOn":
            if not self._context_ok(sid, params.get("executionContextId")):
                return "Cannot find context with specified id"
            return {"result": {"type": "string", "value": self.pages[target]["url"]}}
        if method == "Page.captureScreenshot":
            return {"data": ""}
        return {}


class Client:
    """A CDP client on one agent connection, keeping the state Playwright's and
    Puppeteer's own clients keep from events: per page session, its frames'
    URLs and, per frame, the main world's and the utility world's context."""

    def __init__(self, world: Any, utility: str) -> None:
        self.world = world
        self.utility = utility
        self.sent: list[dict[str, Any]] = []
        self.futures: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self.conn: Any = None
        self.contexts: dict[str, dict[int, dict[str, Any]]] = {}  # session -> id -> description
        self.urls: dict[str, dict[str, str]] = {}  # session -> frameId -> url
        self.attached: list[tuple[str | None, dict[str, Any]]] = []

    # The AgentLink the proxy writes to.
    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        if "id" in message:
            future = self.futures.pop(message["id"], None)
            if future is not None and not future.done():
                future.set_result(message)
            return
        self.on_event(message.get("method", ""), message.get("params") or {}, message.get("sessionId"))

    async def close(self) -> None:
        pass

    def on_event(self, method: str, params: dict[str, Any], sid: str | None) -> None:
        if method == "Target.attachedToTarget":
            self.attached.append((sid, params))
            child = params["sessionId"]
            self.contexts.setdefault(child, {})
            self.urls.setdefault(child, {})
            return
        if sid is None:
            return
        if method == "Runtime.executionContextCreated":
            context = params["context"]
            self.contexts.setdefault(sid, {})[context["id"]] = context
        elif method == "Runtime.executionContextDestroyed":
            self.contexts.get(sid, {}).pop(params.get("executionContextId"), None)
        elif method == "Runtime.executionContextsCleared":
            self.contexts.get(sid, {}).clear()
        elif method == "Page.frameNavigated":
            frame = params["frame"]
            self.urls.setdefault(sid, {})[frame["id"]] = frame["url"]

    async def call(self, method: str, params: dict[str, Any] | None = None, sid: str | None = None) -> dict[str, Any]:
        """One command; its reply, once it arrives. Never awaited while held."""
        request = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.futures[request] = future
        message: dict[str, Any] = {"id": request, "method": method, "params": params or {}}
        if sid is not None:
            message["sessionId"] = sid
        await self.world.proxy.on_agent_message(self.conn, message)
        await settle()
        return await asyncio.wait_for(future, 2)

    def context(self, sid: str, frame: str, world: str) -> int:
        """The context id the client would evaluate in: what a real client
        holds for that frame and world. A real client with none would wait."""
        for context in self.contexts.get(sid, {}).values():
            aux = context.get("auxData") or {}
            if aux.get("frameId") != frame:
                continue
            if world == "main" and aux.get("isDefault"):
                return context["id"]
            if world == "utility" and context.get("name") == self.utility:
                return context["id"]
        raise AssertionError(f"the client holds no {world} context for {frame}: a real one would wait forever")

    def last_page_session(self) -> tuple[str, str]:
        for _parent, params in reversed(self.attached):
            if params["targetInfo"]["type"] == "page":
                return params["sessionId"], params["targetInfo"]["targetId"]
        raise AssertionError("no page attached")


class PlaywrightClient(Client):
    """chromium.connect_over_cdp, the way crBrowser / crPage drive it."""

    def __init__(self, world: Any) -> None:
        super().__init__(world, PLAYWRIGHT_WORLD)

    async def connect(self) -> tuple[str, str]:
        assert (await self.call("Browser.getVersion")).get("error") is None
        await self.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
        sid, target = self.last_page_session()
        for method, params in [
            ("Page.enable", {}), ("Page.getFrameTree", {}), ("Log.enable", {}),
            ("Page.setLifecycleEventsEnabled", {"enabled": True}), ("Runtime.enable", {}),
            ("Page.addScriptToEvaluateOnNewDocument", {"source": "", "worldName": PLAYWRIGHT_WORLD}),
            ("Network.enable", {}),
            ("Page.createIsolatedWorld", {"frameId": target, "grantUniveralAccess": True, "worldName": PLAYWRIGHT_WORLD}),
            ("Runtime.runIfWaitingForDebugger", {}),
        ]:
            reply = await self.call(method, params, sid)
            assert "error" not in reply, (method, reply)
            if method == "Page.getFrameTree":
                frame = reply["result"]["frameTree"]["frame"]
                self.urls[sid][frame["id"]] = frame["url"]
        return sid, target

    async def evaluate(self, sid: str, frame: str, world: str = "main") -> dict[str, Any]:
        # rawEvaluateJSON / the injected script's creation: Runtime.evaluate in a context id.
        return await self.call("Runtime.evaluate", {"expression": "location.href", "contextId": self.context(sid, frame, world),
                                                    "returnByValue": True}, sid)


class PuppeteerClient(Client):
    """puppeteer.connect + browser.newPage, as the fixture records it."""

    def __init__(self, world: Any) -> None:
        super().__init__(world, PUPPETEER_WORLD)

    async def connect(self) -> tuple[str, str]:
        sid = target = ""
        for step in PUPPETEER["sequence"]:
            if step["method"] in ("Page.navigate", "Runtime.callFunctionOn", "Page.captureScreenshot"):
                break
            if step["session"] == "browser":
                sessions: list[str | None] = [None]
            elif step["session"] == "tab":
                # TargetManager#onAttachedToTarget runs for every tab it attaches.
                sessions = [p["sessionId"] for _s, p in self.attached if p["targetInfo"]["type"] == "tab"]
            else:
                sid, target = self.last_page_session()
                sessions = [sid]
            params = json.loads(json.dumps(step["params"]).replace("{frame}", target))
            for session in sessions:
                reply = await self.call(step["method"], params, session)
                if step["fatal"]:
                    assert "error" not in reply, (step["method"], reply)
        return sid, target

    async def evaluate(self, sid: str, frame: str, world: str = "main") -> dict[str, Any]:
        return await self.call("Runtime.callFunctionOn", {
            "functionDeclaration": "() => location.href", "executionContextId": self.context(sid, frame, world),
            "arguments": [], "returnByValue": True, "awaitPromise": True, "userGesture": True}, sid)


class World:
    def __init__(self, tmp_path: Path) -> None:
        (tmp_path / "downloads").mkdir()
        (tmp_path / "uploads").mkdir()
        self.records: list[tuple[str, dict[str, Any]]] = []
        self.chromium = ModelChromium()
        self.proxy = CdpProxy(
            self.chromium.send,
            paths=PolicyPaths(downloads_dir=tmp_path / "downloads", uploads_dir=tmp_path / "uploads"),
            record=lambda event, fields: self.records.append((event, dict(fields))),
            security_wait_seconds=0.05,
        )
        self.chromium.start(self.proxy)

    async def client(self, kind: type[Client]) -> Client:
        client = kind(self)
        client.conn = await self.proxy.connect_agent(client)
        assert client.conn is not None
        return client

    def refused(self) -> list[str]:
        return [f["method"] for e, f in self.records if e == "cdp_method_refused"]


CLIENTS = [PlaywrightClient, PuppeteerClient]


# --------------------------------------------------------------------------- #
# Puppeteer opens a page through the gate                                     #
# --------------------------------------------------------------------------- #


def test_the_fixture_pins_puppeteer_core_25_8_0_and_marks_what_puppeteer_treats_as_fatal():
    assert PUPPETEER["client"] == "puppeteer-core" and PUPPETEER["version"] == "25.8.0"
    assert "25.8.0" in FIXTURE.name
    fatal = {s["method"] for s in PUPPETEER["sequence"] if s["fatal"]}
    # The two the gate refused before DEVB-GATE, which stopped every newPage().
    assert {"Performance.enable", "Audits.enable"} <= fatal
    # Puppeteer catches these: refused is survivable, and must stay quiet.
    caught = {s["method"] for s in PUPPETEER["sequence"] if not s["fatal"]}
    assert "WebMCP.enable" in caught


def test_every_method_puppeteer_treats_as_fatal_gets_a_non_error_reply(tmp_path):
    async def body():
        w = World(tmp_path)
        w.chromium.new_page("https://example.com/")
        client = await w.client(PuppeteerClient)
        sid, target = await client.connect()
        main = client.context(sid, target, "main")
        session_of = {"browser": None, "tab": None, "page": sid}
        session_of["tab"] = next(p["sessionId"] for _s, p in client.attached if p["targetInfo"]["type"] == "tab")
        failures = []
        for step in PUPPETEER["sequence"]:
            params = json.loads(json.dumps(step["params"]).replace("{frame}", target))
            if params.get("executionContextId") == "{main_context}":
                params["executionContextId"] = client.context(sid, target, "main")
            reply = await client.call(step["method"], params, session_of[step["session"]])
            if step["fatal"] and "error" in reply:
                failures.append((step["method"], reply["error"]["message"]))
        assert failures == []
        assert main  # the main world was reported before any evaluate
        # Audits is answered by the gate and never reaches Chromium.
        assert [m for m in w.chromium.received if m["method"].startswith("Audits.")] == []
        assert any(m["method"] == "Performance.enable" for m in w.chromium.received)
        await w.chromium.stop()

    run(body)


def test_puppeteers_caught_probes_are_refused_without_a_refusal_record(tmp_path):
    """WebMCP.enable and Network.emulateNetworkConditionsByRule are refused,
    and Puppeteer goes on; recorded on every page they were the refusal
    noise of ANALYSIS issue 19, so they are counted and logged at debug."""

    async def body():
        w = World(tmp_path)
        w.chromium.new_page("https://example.com/")
        client = await w.client(PuppeteerClient)
        sid, _target = await client.connect()
        probe = await client.call("Network.emulateNetworkConditionsByRule",
                                  {"matchedNetworkConditions": [{"urlPattern": "", "offline": True}]}, sid)
        assert "error" in probe
        assert not [m for m in w.chromium.received if m["method"] in ("WebMCP.enable", "Network.emulateNetworkConditionsByRule")]
        assert w.refused() == []
        assert w.proxy.quiet_refusals == {"WebMCP.enable": 1, "Network.emulateNetworkConditionsByRule": 1}
        # A refusal that is not a known probe is still recorded.
        await client.call("Network.setExtraHTTPHeaders", {"headers": {}}, sid)
        assert w.refused() == ["Network.setExtraHTTPHeaders"]
        await w.chromium.stop()

    run(body)


def test_no_audits_event_ever_reaches_the_agent(tmp_path):
    """Audits.issueAdded's cookie details carry `rawCookieLine`, a Set-Cookie
    header with its value, when Chromium cannot parse the cookie: the agent
    would read the jar the method policy keeps from it (LB-7)."""

    async def body():
        w = World(tmp_path)
        w.chromium.new_page("https://example.com/")
        client = await w.client(PuppeteerClient)
        sid, _target = await client.connect()
        line = "sid=" + secrets.token_hex(16) + "; Path=/; SameSite=Bogus"
        issue = {"issue": {"code": "CookieIssue", "details": {"cookieIssueDetails": {
            "rawCookieLine": line, "cookieWarningReasons": [], "cookieExclusionReasons": [], "operation": "SetCookie"}}}}
        w.chromium.emit("Audits.issueAdded", issue, sid)
        await settle()
        await w.proxy.pause()
        w.chromium.emit("Audits.issueAdded", issue, sid)
        await settle()
        await w.proxy.resume()
        await settle()
        assert [m for m in client.sent if str(m.get("method", "")).startswith("Audits.")] == []
        assert line not in json.dumps(client.sent)
        await w.chromium.stop()

    run(body)


# --------------------------------------------------------------------------- #
# HANDOFF-RESYNC: the agent's page after a person had control                 #
# --------------------------------------------------------------------------- #


async def _on_form(w: World, client: Client, sid: str, frame: str) -> str:
    """The client's page on the form; Puppeteer's is the one its newPage()
    opened on about:blank, Playwright's the one it attached to."""
    if w.chromium.pages[frame]["url"] != "https://example.com/form":
        reply = await client.call("Page.navigate", {"url": "https://example.com/form", "frameId": frame}, sid)
        assert "error" not in reply, reply
    return frame


async def _hand_off(w: World, target: str, *urls: str) -> None:
    """Control starts, the person navigates through `urls`, control ends."""
    await w.proxy.pause()
    decision = await w.proxy.take_control(target)
    assert decision.granted, decision
    for url in urls:
        w.chromium.navigate(target, url)
        await settle()
    await w.proxy.resume()
    await settle()


@pytest.mark.parametrize("kind", CLIENTS, ids=lambda k: k.__name__)
def test_the_agents_first_evaluate_after_return_succeeds_in_a_live_context(tmp_path, kind):
    async def body():
        w = World(tmp_path)
        w.chromium.new_page("https://example.com/form")
        client = await w.client(kind)
        sid, frame = await client.connect()
        target = await _on_form(w, client, sid, frame)
        before = client.context(sid, frame, "main")
        assert "error" not in await client.evaluate(sid, frame)

        await _hand_off(w, target, "https://example.com/signed-in")

        reply = await client.evaluate(sid, frame)
        assert "error" not in reply, reply
        assert reply["result"]["result"]["value"] == "https://example.com/signed-in"
        after = client.context(sid, frame, "main")
        assert after != before
        # Every context the client holds is one Chromium has.
        live = set(w.chromium.pages[target]["contexts"])
        assert set(client.contexts[sid]) <= live
        await w.chromium.stop()

    run(body)


@pytest.mark.parametrize("kind", CLIENTS, ids=lambda k: k.__name__)
def test_the_agents_utility_world_is_back_after_a_navigation_in_control(tmp_path, kind):
    """The proxy lifts the client's utility-world script for the window (its
    page scripts, section 5.4.3), so the document the person reached has no
    utility world until the proxy makes one: without it Playwright's
    actionability checks and Puppeteer's waitForSelector wait forever."""

    async def body():
        w = World(tmp_path)
        w.chromium.new_page("https://example.com/form")
        client = await w.client(kind)
        sid, frame = await client.connect()
        target = await _on_form(w, client, sid, frame)
        assert "error" not in await client.evaluate(sid, frame, "utility")

        await _hand_off(w, target, "https://example.com/signed-in")

        reply = await client.evaluate(sid, frame, "utility")
        assert "error" not in reply, reply
        await w.chromium.stop()

    run(body)


@pytest.mark.parametrize("kind", CLIENTS, ids=lambda k: k.__name__)
def test_the_agent_learns_where_the_page_is_now_and_never_where_it_passed(tmp_path, kind):
    async def body():
        w = World(tmp_path)
        w.chromium.new_page("https://example.com/form")
        client = await w.client(kind)
        sid, frame = await client.connect()
        target = await _on_form(w, client, sid, frame)
        code = secrets.token_hex(12)
        passed = f"https://example.com/callback?code={code}"

        await _hand_off(w, target, passed, "https://example.com/home")

        assert client.urls[sid][frame] == "https://example.com/home"
        assert code not in json.dumps(client.sent)
        await w.chromium.stop()

    run(body)


@pytest.mark.parametrize("kind", CLIENTS, ids=lambda k: k.__name__)
def test_a_hand_off_without_a_navigation_leaves_the_agents_contexts_alone(tmp_path, kind):
    """Nothing changed, so nothing is replayed: the client's handles stay valid."""

    async def body():
        w = World(tmp_path)
        w.chromium.new_page("https://example.com/form")
        client = await w.client(kind)
        sid, frame = await client.connect()
        target = await _on_form(w, client, sid, frame)
        before = client.context(sid, frame, "main")
        events = len([m for m in client.sent if "id" not in m])

        await _hand_off(w, target)

        assert client.context(sid, frame, "main") == before
        assert "error" not in await client.evaluate(sid, frame)
        assert [m.get("method") for m in client.sent if "id" not in m][events:] == []
        await w.chromium.stop()

    run(body)
