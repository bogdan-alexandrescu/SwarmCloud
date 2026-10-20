"""The live gate's side of the QA identity (QI-GATE; docs/design/qa-identity.md §4.4, §6; part of #1649).

The gate adds the QA account's IAP JWT to console requests from console frames,
on its own CDP sessions, and the agent never holds it. Every test here drives
the real `QaIdentity`, `QaInjector` and `CdpProxy` against a scripted Chromium
that records what it was asked and what "reached the network" (a Request-stage
pause the gate continued), so "the header never left for evil.example" is read
off what the fake saw, not off the gate's own bookkeeping.

The fake follows what docs/design/qa-identity-measurements.md measured on
Chrome 153.0.8010.12: Chromium refuses `Fetch.continueRequest` adding
`Proxy-Authorization` ("Unsafe header") and accepts `Authorization` (M3); a
header added by `Fetch.continueRequest` follows no redirect, and the
redirect's own request comes back as a pause with `redirectedRequestId` (M5).

No test runs Chromium, a network or a cloud credential. The fake JWT is made
at run time from random parts, never written as one literal.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate import cdppolicy, ctl
from agent_worker.livegate.cdppolicy import PolicyPaths
from agent_worker.livegate.cdpproxy import CdpProxy
from agent_worker.livegate.ctl import CtlClient, CtlRefused
from agent_worker.livegate.qaidentity import PLACEHOLDER, QaIdentity, QaIdentityRefused, QaInjector
from agent_worker.livegate.session import SessionCarry

import test_livegate_main as rigs

HOST = "console.example.com"
CONSOLE = f"https://{HOST}"
EVIL = "https://evil.example"


def _jwt() -> str:
    # Three base64url segments, the shape of a JWT, from random bytes.
    return ".".join(secrets.token_urlsafe(24) for _ in range(3))


def _args(credential: str, **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"host": HOST, "header": "Authorization", "scheme": "Bearer",
                            "credential": credential, "expires_in": 900}
    args.update(over)
    return args


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeLink:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    """Chromium's pipe, for the gate's sessions and an agent's (module docstring)."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.pages: dict[str, str] = {"PAGE1": "about:blank"}
        #: page targetId -> [(type, targetId)] its auto-attach reports.
        self.children: dict[str, list[tuple[str, str]]] = {}
        self.sessions: dict[str, str] = {}  # session -> targetId
        self.paused: dict[str, tuple[str | None, dict[str, Any]]] = {}
        #: Every request a Request-stage continue let out: (url, headers).
        self.network: list[tuple[str, dict[str, str]]] = []
        self.redirects: dict[str, tuple[int, str]] = {}
        self.unsafe = {"proxy-authorization"}
        self.results: dict[str, dict[str, Any]] = {}
        self.security = "secure"
        self._n = 0
        self._pump: asyncio.Task[None] | None = None
        self.sink: Any = None

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def start(self, sink: Any) -> None:
        self.sink = sink
        self._pump = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            message = await self.queue.get()
            try:
                await self.sink(message)
            finally:
                self.queue.task_done()

    async def stop(self) -> None:
        if self._pump is not None:
            self._pump.cancel()

    def emit(self, method: str, params: dict[str, Any], session: str | None = None) -> None:
        message: dict[str, Any] = {"method": method, "params": params}
        if session is not None:
            message["sessionId"] = session
        self.queue.put_nowait(message)

    def request(self, session: str, url: str, *, frame: str | None = "PAGE1", kind: str = "Document",
                headers: dict[str, str] | None = None, redirected: str | None = None) -> str:
        request_id = self._next("R")
        params: dict[str, Any] = {
            "requestId": request_id,
            "request": {"url": url, "method": "GET", "headers": dict(headers or {"Accept": "*/*"})},
            "resourceType": kind,
            "networkId": self._next("N"),
        }
        if frame is not None:
            params["frameId"] = frame
        if redirected is not None:
            params["redirectedRequestId"] = redirected
        self.paused[request_id] = (session, params)
        self.emit("Fetch.requestPaused", params, session)
        return request_id

    def sent(self, method: str) -> list[dict[str, Any]]:
        return [m for m in self.received if m["method"] == method]

    def reached(self, origin: str) -> list[tuple[str, dict[str, str]]]:
        return [(u, h) for u, h in self.network if u.startswith(origin + "/") or u == origin]

    async def send(self, message: dict[str, Any]) -> None:
        self.received.append(message)
        method = message["method"]
        params = message.get("params", {})
        session = message.get("sessionId")
        reply: dict[str, Any] = {"id": message["id"], "result": dict(self.results.get(method, {}))}
        if method == "Target.attachToBrowserTarget":
            reply["result"] = {"sessionId": self._next("B")}
        elif method == "Target.getTargets":
            reply["result"] = {"targetInfos": [{"targetId": t, "type": "page", "url": u}
                                               for t, u in self.pages.items()]}
        elif method == "Target.attachToTarget":
            child = self._next("S")
            self.sessions[child] = params["targetId"]
            self.emit("Target.attachedToTarget", {"sessionId": child, "waitingForDebugger": False, "targetInfo": {
                "targetId": params["targetId"], "type": "page", "url": self.pages.get(params["targetId"], "")}},
                session)
            reply["result"] = {"sessionId": child}
        elif method == "Target.setAutoAttach" and session in self.sessions:
            for kind, target in self.children.pop(self.sessions[session], []):
                child = self._next("C")
                self.sessions[child] = target
                self.emit("Target.attachedToTarget", {"sessionId": child, "waitingForDebugger": True,
                                                      "targetInfo": {"targetId": target, "type": kind}}, session)
        elif method == "Target.getTargetInfo":
            reply["result"] = {"targetInfo": {"targetId": params["targetId"], "type": "page",
                                              "url": self.pages.get(params["targetId"], "about:blank")}}
        elif method == "Security.enable":
            self.queue.put_nowait(reply)
            self.emit("Security.visibleSecurityStateChanged",
                      {"visibleSecurityState": {"securityState": self.security}}, session)
            return
        elif method == "Fetch.continueRequest":
            self._continue_request(params, reply)
        elif method == "Fetch.continueResponse":
            self._continue_response(params)
        self.queue.put_nowait(reply)

    def _continue_request(self, params: dict[str, Any], reply: dict[str, Any]) -> None:
        names = [h["name"] for h in params.get("headers") or []]
        refused = [n for n in names if n.lower() in self.unsafe]
        if refused:
            reply.pop("result")
            reply["error"] = {"code": -32602, "message": f"Unsafe header: {refused[0]}"}
            return
        if params["requestId"] not in self.paused:
            return  # one of the agent's own pauses: nothing for the network model
        session, paused = self.paused[params["requestId"]]
        url = paused["request"]["url"]
        if "headers" in params:
            headers = {h["name"]: h["value"] for h in params["headers"]}
        else:
            headers = dict(paused["request"]["headers"])
        self.network.append((url, headers))
        if url in self.redirects:
            status, location = self.redirects[url]
            self.emit("Fetch.requestPaused", {**paused, "responseStatusCode": status,
                                              "responseHeaders": [{"name": "Location", "value": location}]},
                      session)

    def _continue_response(self, params: dict[str, Any]) -> None:
        session, paused = self.paused[params["requestId"]]
        url = paused["request"]["url"]
        if url in self.redirects and session is not None:
            location = self.redirects[url][1]
            target = location if "://" in location else CONSOLE + location
            # M5: the redirect's request is a pause of its own, WITHOUT the added header.
            self.request(session, target, frame=paused.get("frameId"), kind=paused["resourceType"],
                         headers={"Accept": "*/*"}, redirected=paused["requestId"])


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []
        self.clock = Clock()
        self.fake = FakeChromium()
        self.qa = QaIdentity(self.record, clock=self.clock)
        self.proxy = CdpProxy(self.fake.send, paths=PolicyPaths(downloads_dir=tmp, uploads_dir=tmp),
                              record=self.record, qa=self.qa, security_wait_seconds=1.0)
        self.injector = QaInjector(self.proxy, self.qa, record=self.record)
        self.jwt = _jwt()

    def record(self, event: str, fields: Any) -> None:
        self.records.append((event, dict(fields)))

    def events(self, name: str) -> list[dict[str, Any]]:
        return [f for e, f in self.records if e == name]

    async def sink(self, message: dict[str, Any]) -> None:
        # What LiveGate.on_chromium_message does: the proxy, then the gate's listeners.
        await self.proxy.on_chromium_message(message)
        if "method" in message:
            self.injector.on_event(message)

    async def start(self, *, credential: bool = True) -> None:
        self.fake.start(self.sink)
        if credential:
            self.qa.handle(_args(self.jwt))
            await self.injector.refresh()
        await self.settle()

    async def settle(self) -> None:
        for _ in range(30):
            await self.fake.queue.join()
            await asyncio.sleep(0)

    def page_session(self, target: str = "PAGE1") -> str:
        for session, t in self.fake.sessions.items():
            if t == target and session.startswith("S"):
                return session
        raise AssertionError(f"the gate never attached to {target}")

    def child_session(self, target: str) -> str:
        for session, t in self.fake.sessions.items():
            if t == target and session.startswith("C"):
                return session
        raise AssertionError(f"the gate never auto-attached to {target}")

    async def navigated(self, session: str, url: str, frame: str = "PAGE1", parent: str | None = None) -> None:
        info: dict[str, Any] = {"id": frame, "url": url, "loaderId": "L"}
        if parent is not None:
            info["parentId"] = parent
        self.fake.emit("Page.frameNavigated", {"frame": info}, session)
        await self.settle()

    def leaked(self, value: Any) -> bool:
        return self.jwt in json.dumps(value)


@pytest.fixture
def tmp() -> Any:
    path = Path(tempfile.mkdtemp(prefix="qi"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def run(body: Any) -> None:
    asyncio.run(body())


def _auth(headers: dict[str, str]) -> str | None:
    return next((v for n, v in headers.items() if n.lower() == "authorization"), None)


# --------------------------------------------------------------------------
# Injection, rules 1-3
# --------------------------------------------------------------------------


def test_a_console_request_from_a_console_frame_gets_the_header_and_one_from_an_evil_frame_does_not(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        page = rig.page_session()
        enabled = [m for m in rig.fake.sent("Fetch.enable") if m.get("sessionId") == page]
        assert enabled, "the gate never intercepted on its own page session"
        stages = {(p["urlPattern"], p["requestStage"]) for p in enabled[-1]["params"]["patterns"]}
        assert stages == {(f"{CONSOLE}/*", "Request"), (f"{CONSOLE}/*", "Response"), ("*", "Request")}

        # The top frame's own navigation to the console: signed in.
        rig.fake.request(page, f"{CONSOLE}/")
        await rig.settle()
        url, headers = rig.fake.network[-1]
        assert url == f"{CONSOLE}/" and _auth(headers) == f"Bearer {rig.jwt}"

        # A fetch from the console page: signed in.
        await rig.navigated(page, f"{CONSOLE}/")
        rig.fake.request(page, f"{CONSOLE}/v1/tenants/me", kind="XHR")
        await rig.settle()
        assert _auth(rig.fake.network[-1][1]) == f"Bearer {rig.jwt}"

        # The same fetch from a page on evil.example: never.
        await rig.navigated(page, f"{EVIL}/")
        rig.fake.request(page, f"{CONSOLE}/v1/tenants/me", kind="XHR")
        await rig.settle()
        url, headers = rig.fake.network[-1]
        assert url == f"{CONSOLE}/v1/tenants/me" and _auth(headers) is None
        await rig.fake.stop()

    run(body)


@pytest.mark.parametrize("url", [
    f"https://{HOST}.evil.example/",
    f"http://{HOST}/",
    f"https://user@{HOST}/",
    f"https://{HOST}:8443/",
    f"https://evil.example/{HOST}/",
])
def test_a_look_alike_of_the_console_host_gets_no_header(tmp, url):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        rig.fake.request(rig.page_session(), url)
        await rig.settle()
        assert rig.fake.network[-1][0] == url
        assert _auth(rig.fake.network[-1][1]) is None
        await rig.fake.stop()

    run(body)


def test_an_out_of_process_console_frame_is_judged_by_its_top_level_origin(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.children["PAGE1"] = [("iframe", "F1")]
        await rig.start()
        page, frame = rig.page_session(), rig.child_session("F1")
        assert any(m.get("sessionId") == frame for m in rig.fake.sent("Fetch.enable"))
        assert any(m.get("sessionId") == frame for m in rig.fake.sent("Runtime.runIfWaitingForDebugger")), \
            "a child that waits for the debugger is released"
        rig.fake.emit("Page.frameAttached", {"frameId": "F1", "parentFrameId": "PAGE1"}, page)
        await rig.navigated(page, f"{CONSOLE}/")
        rig.fake.request(frame, f"{CONSOLE}/v1/x", frame="F1", kind="XHR")
        await rig.settle()
        assert _auth(rig.fake.network[-1][1]) == f"Bearer {rig.jwt}"

        await rig.navigated(page, f"{EVIL}/")
        rig.fake.request(frame, f"{CONSOLE}/v1/x", frame="F1", kind="XHR")
        await rig.settle()
        assert _auth(rig.fake.network[-1][1]) is None, "a console frame inside evil.example"
        await rig.fake.stop()

    run(body)


def test_a_request_from_a_dedicated_worker_target_gets_no_header(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.children["PAGE1"] = [("worker", "W1")]
        await rig.start()
        page, worker = rig.page_session(), rig.child_session("W1")
        await rig.navigated(page, f"{CONSOLE}/")
        # Even naming the console page's frame: a worker target has no frame (§4.4).
        rig.fake.request(worker, f"{CONSOLE}/v1/tenants/me", frame="PAGE1", kind="Fetch")
        await rig.settle()
        url, headers = rig.fake.network[-1]
        assert url == f"{CONSOLE}/v1/tenants/me" and _auth(headers) is None
        await rig.fake.stop()

    run(body)


def test_no_header_without_a_credential_after_expiry_or_after_failed(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        page = rig.page_session()
        rig.clock.now += 901
        rig.fake.request(page, f"{CONSOLE}/")
        await rig.settle()
        assert _auth(rig.fake.network[-1][1]) is None, "an expired credential is not sent"

        rig.qa.handle(_args(rig.jwt))
        await rig.injector.refresh()
        rig.qa.handle({"failed": "qa_identity_revoked"})
        await rig.injector.refresh()
        await rig.settle()
        rig.fake.request(page, f"{CONSOLE}/")
        await rig.settle()
        assert _auth(rig.fake.network[-1][1]) is None
        last = [m for m in rig.fake.sent("Fetch.enable") if m.get("sessionId") == page][-1]
        assert all(p["urlPattern"] != "*" for p in last["params"]["patterns"]), \
            "with no credential held guard 2's `*` pattern is not enabled"
        assert rig.events("qa_identity_failed") == [{"cause": "qa_identity_revoked"}]
        await rig.fake.stop()

    run(body)


def test_a_header_chromium_refuses_leaves_no_request_paused(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.unsafe.add("authorization")
        await rig.start()
        rig.fake.request(rig.page_session(), f"{CONSOLE}/")
        await rig.settle()
        url, headers = rig.fake.network[-1]
        assert url == f"{CONSOLE}/" and _auth(headers) is None, "continued unmodified, never left paused"
        assert rig.events("qa_identity_continue_refused")
        await rig.fake.stop()

    run(body)


# --------------------------------------------------------------------------
# The redirect guards
# --------------------------------------------------------------------------


def test_a_console_redirect_to_another_origin_is_stopped_and_one_on_the_console_is_followed(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        page = rig.page_session()
        rig.fake.redirects[f"{CONSOLE}/connect"] = (302, f"{EVIL}/callback")
        rig.fake.request(page, f"{CONSOLE}/connect")
        await rig.settle()
        fulfilled = rig.fake.sent("Fetch.fulfillRequest")
        assert len(fulfilled) == 1 and fulfilled[0]["params"]["responseCode"] == 200
        page_text = base64.b64decode(fulfilled[0]["params"]["body"]).decode()
        assert "Redirect stopped" in page_text and EVIL in page_text
        assert rig.fake.reached(EVIL) == [], "the fake never saw a request to evil.example"
        assert rig.events("qa_identity_redirect_stopped") == [{"origin": EVIL}]

        rig.fake.redirects[f"{CONSOLE}/old"] = (302, "/new")
        rig.fake.request(page, f"{CONSOLE}/old")
        await rig.settle()
        assert rig.fake.sent("Fetch.continueResponse"), "a redirect on the console host is continued"
        url, headers = rig.fake.network[-1]
        # M5: the header did not follow; the injection rules added it to the redirect's own pause.
        assert url == f"{CONSOLE}/new" and _auth(headers) == f"Bearer {rig.jwt}"
        assert len(rig.fake.sent("Fetch.fulfillRequest")) == 1
        await rig.fake.stop()

    run(body)


def test_a_request_to_another_host_carrying_the_credential_is_continued_without_it(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        page = rig.page_session()
        rig.fake.request(page, f"{EVIL}/x", headers={"Accept": "*/*", "authorization": f"Bearer {rig.jwt}"})
        await rig.settle()
        url, headers = rig.fake.network[-1]
        assert url == f"{EVIL}/x" and headers == {"Accept": "*/*"}
        assert rig.events("qa_identity_header_stripped") == [{"origin": EVIL}]

        # A site's own bearer token is its own: guard 2 strips only the QA credential.
        own = "Bearer " + secrets.token_hex(16)
        rig.fake.request(page, f"{EVIL}/api", headers={"Authorization": own})
        await rig.settle()
        assert rig.fake.network[-1] == (f"{EVIL}/api", {"Authorization": own})
        await rig.fake.stop()

    run(body)


# --------------------------------------------------------------------------
# What the agent receives
# --------------------------------------------------------------------------


async def _agent(rig: Rig) -> tuple[Any, FakeLink, str]:
    link = FakeLink()
    connection = await rig.proxy.connect_agent(link)
    assert connection is not None
    return connection, link, connection.browser_session


def test_an_agent_bound_event_loses_the_header_and_every_copy_of_the_credential(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        _, link, session = await _agent(rig)
        rig.fake.emit("Network.requestWillBeSentExtraInfo", {"requestId": "N1", "headers": {
            "Authorization": f"Bearer {rig.jwt}", "Proxy-Authorization": "Basic " + secrets.token_hex(8),
            "Accept": "*/*"}}, session)
        rig.fake.emit("Runtime.consoleAPICalled", {"type": "log", "args": [
            {"type": "string", "value": f"the page printed {rig.jwt} somewhere"}]}, session)
        await rig.settle()
        extra, console = link.sent[-2], link.sent[-1]
        assert extra["params"]["headers"] == {"Accept": "*/*"}
        assert console["params"]["args"][0]["value"] == f"the page printed {PLACEHOLDER} somewhere"
        assert not rig.leaked(link.sent)
        scrubbed = rig.events("qa_identity_scrubbed")
        assert scrubbed and all(isinstance(f["count"], int) and f["count"] > 0 for f in scrubbed)
        assert not rig.leaked(rig.records), "a record never carries the credential"
        await rig.fake.stop()

    run(body)


def test_a_renewal_keeps_the_previous_credential_scrubbed(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        old = rig.jwt
        rig.jwt = _jwt()
        rig.qa.handle(_args(rig.jwt))
        _, link, session = await _agent(rig)
        rig.fake.emit("Runtime.consoleAPICalled", {"args": [{"value": old}, {"value": rig.jwt}]}, session)
        await rig.settle()
        assert [a["value"] for a in link.sent[-1]["params"]["args"]] == [PLACEHOLDER, PLACEHOLDER]
        await rig.fake.stop()

    run(body)


def test_a_base64_response_body_carrying_the_credential_arrives_scrubbed(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        _, link, session = await _agent(rig)
        reflected = f'{{"echo": "Bearer {rig.jwt}"}}'.encode()
        rig.fake.results["Network.getResponseBody"] = {
            "body": base64.b64encode(reflected).decode(), "base64Encoded": True}
        await rig.proxy.on_agent_message(_conn(rig), {"id": 7, "method": "Network.getResponseBody",
                                                      "params": {"requestId": "N9"}})
        await rig.settle()
        reply = next(m for m in link.sent if m.get("id") == 7)
        decoded = base64.b64decode(reply["result"]["body"]).decode()
        assert PLACEHOLDER in decoded and rig.jwt not in decoded
        assert not rig.leaked(link.sent)
        await rig.fake.stop()

    run(body)


def _conn(rig: Rig) -> Any:
    return rig.proxy._connections[-1]


def test_a_console_body_the_gate_cannot_decode_is_refused(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        _, link, session = await _agent(rig)
        rig.fake.emit("Network.requestWillBeSent", {"requestId": "NC", "request": {"url": f"{CONSOLE}/x"}}, session)
        rig.fake.emit("Network.requestWillBeSent", {"requestId": "NE", "request": {"url": f"{EVIL}/x"}}, session)
        await rig.settle()
        rig.fake.results["Network.getResponseBody"] = {"body": "not base64 at all!", "base64Encoded": True}
        await rig.proxy.on_agent_message(_conn(rig), {"id": 8, "method": "Network.getResponseBody",
                                                      "params": {"requestId": "NC"}})
        await rig.proxy.on_agent_message(_conn(rig), {"id": 9, "method": "Network.getResponseBody",
                                                      "params": {"requestId": "NE"}})
        await rig.settle()
        refused = next(m for m in link.sent if m.get("id") == 8)
        passed = next(m for m in link.sent if m.get("id") == 9)
        assert "error" in refused and "result" not in refused
        assert passed["result"]["body"] == "not base64 at all!"
        assert {"method": "Network.getResponseBody", "cause": "qa_identity_body"} in rig.events("cdp_method_refused")
        await rig.fake.stop()

    run(body)


def test_the_agents_fetch_may_not_redirect_or_reheader_a_console_request(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        _, link, session = await _agent(rig)
        rig.fake.emit("Fetch.requestPaused", {"requestId": "AC", "request": {"url": f"{CONSOLE}/", "headers": {}},
                                              "resourceType": "Document"}, session)
        rig.fake.emit("Fetch.requestPaused", {"requestId": "AE", "request": {"url": f"{EVIL}/", "headers": {}},
                                              "resourceType": "Document"}, session)
        await rig.settle()
        before = len(rig.fake.sent("Fetch.continueRequest"))
        conn = _conn(rig)
        await rig.proxy.on_agent_message(conn, {"id": 1, "method": "Fetch.continueRequest",
                                                "params": {"requestId": "AE", "url": f"{CONSOLE}/"}})
        await rig.proxy.on_agent_message(conn, {"id": 2, "method": "Fetch.continueRequest", "params": {
            "requestId": "AC", "headers": [{"name": "Authorization", "value": "Bearer x"}]}})
        await rig.settle()
        for agent_id in (1, 2):
            reply = next(m for m in link.sent if m.get("id") == agent_id)
            assert "refused by the live gate" in reply["error"]["message"]
        assert len(rig.fake.sent("Fetch.continueRequest")) == before, "neither reached Chromium"
        causes = {f.get("cause") for f in rig.events("cdp_method_refused")}
        assert {"qa_identity_redirect", "qa_identity_headers"} <= causes

        # Another host's request is the agent's to re-header, as before.
        await rig.proxy.on_agent_message(conn, {"id": 3, "method": "Fetch.continueRequest", "params": {
            "requestId": "AE", "headers": [{"name": "X-Test", "value": "1"}]}})
        await rig.settle()
        assert len(rig.fake.sent("Fetch.continueRequest")) == before + 1
        await rig.fake.stop()

    run(body)


def test_the_policy_check_on_its_own():
    unknown = lambda request_id: True  # noqa: E731 -- an id the proxy never saw counts as the console's
    assert cdppolicy.qa_identity_check("Fetch.continueRequest", {"requestId": "r", "url": "https://x.example/"},
                                       unknown).cause == "qa_identity_redirect"
    assert cdppolicy.qa_identity_check("Fetch.continueRequest", {"requestId": "r", "headers": []},
                                       unknown).cause == "qa_identity_headers"
    assert cdppolicy.qa_identity_check("Fetch.continueRequest", {"requestId": "r"}, unknown) is None
    assert cdppolicy.qa_identity_check("Fetch.fulfillRequest", {"requestId": "r", "url": "x"}, unknown) is None


# --------------------------------------------------------------------------
# Control, the verb, the session carry
# --------------------------------------------------------------------------


def test_control_of_a_console_page_is_refused_and_ends_if_the_page_goes_there(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        rig.fake.pages["PAGE1"] = f"{CONSOLE}/"
        await rig.proxy.pause()
        decision = await rig.proxy.take_control("PAGE1")
        assert decision.granted is False and decision.cause == "qa_identity_host"

        rig.fake.pages["PAGE1"] = "https://example.org/signin"
        decision = await rig.proxy.take_control("PAGE1")
        assert decision.granted is True
        watched = next(s for s, t in rig.proxy._gate_sessions.items() if t == "PAGE1")
        rig.fake.emit("Page.frameNavigated", {"frame": {"id": "PAGE1", "url": f"{CONSOLE}/"}}, watched)
        await rig.settle()
        assert rig.proxy.in_control is False
        assert {"cause": "qa_identity_host"} in rig.events("control_ended")
        await rig.fake.stop()

    run(body)


def test_the_verb_refuses_proxy_authorization_another_host_a_split_header_and_anything_after_the_wipe():
    records: list[Any] = []
    qa = QaIdentity(lambda e, f: records.append((e, f)))
    jwt = _jwt()
    for bad, code in [
        (_args(jwt, header="Proxy-Authorization"), "header"),  # M3: Chromium refuses to add it
        (_args(jwt, scheme="Basic"), "scheme"),
        (_args(jwt + "\r\nX-Evil: 1"), "credential"),
        (_args(jwt, host="192.0.2.1"), "host"),
        (_args(jwt, expires_in=0), "expires_in"),
        (_args(jwt, expires_in=900_000), "expires_in"),
    ]:
        with pytest.raises(QaIdentityRefused) as refused:
            qa.handle(bad)
        assert refused.value.code == code
    assert qa.handle(_args(jwt)) == {"qa_identity": "held", "expires_in": 900}
    with pytest.raises(QaIdentityRefused) as refused:
        qa.handle(_args(_jwt(), host="other.example.com"))
    assert refused.value.code == "host_changed"
    qa.wipe()
    assert qa.held is False
    with pytest.raises(QaIdentityRefused):
        qa.handle(_args(_jwt()))
    assert jwt not in json.dumps(records)


def test_session_export_and_import_leave_the_console_host_out():
    calls: list[tuple[str, Any]] = []
    jar = {"cookies": [
        {"name": "iap", "value": secrets.token_hex(8), "domain": HOST, "path": "/"},
        {"name": "parent", "value": secrets.token_hex(8), "domain": ".example.com", "path": "/"},
        {"name": "other", "value": secrets.token_hex(8), "domain": "other.example", "path": "/"},
    ]}
    pages = {"targetInfos": [{"targetId": "P1", "type": "page", "url": f"{CONSOLE}/"}]}

    async def call(method: str, params: Any, session: Any) -> dict[str, Any]:
        calls.append((method, params))
        if method == "Storage.getCookies":
            return jar
        if method == "Target.getTargets":
            return pages
        return {}

    async def body() -> None:
        carry = SessionCarry(call, record=lambda e, f: None, agent_connected=lambda: False,
                             excluded_host=lambda: HOST)
        exported = await carry.export(park=True)
        assert [c["name"] for c in exported["cookies"]] == ["other"]
        assert exported["origins"] == []
        assert "Target.attachToTarget" not in [m for m, _ in calls], "the console's storage is never read"

        imported = {"v": 1, "cookies": jar["cookies"], "origins": []}
        await carry.import_jar(imported)
        written = next(p for m, p in calls if m == "Storage.setCookies")
        assert [c["name"] for c in written["cookies"]] == ["other"]

    run(body)


def test_the_qa_identity_is_an_operation_ctl_sock_accepts():
    assert "qa_identity" in ctl.OPS


def test_the_gate_takes_the_verb_refuses_a_console_hand_off_and_refuses_the_verb_after_the_wipe(short_tmp):
    async def body() -> None:
        records: list[tuple[str, Any]] = []
        rig = rigs.Rig(short_tmp)
        rig.gate.record = lambda e, f: records.append((e, dict(f)))
        rig.gate.qa._record = rig.gate.record
        stop, task = await rigs._started(rig)
        client = CtlClient(rig.cfg.ctl_path)
        await asyncio.to_thread(client.register)
        jwt = _jwt()
        held = await asyncio.to_thread(client.call, "qa_identity", _args(jwt))
        assert held == {"qa_identity": "held", "expires_in": 900}
        status = await asyncio.to_thread(client.call, "status", {})
        assert status["qa_identity"]["state"] == "held" and jwt not in json.dumps(status)
        assert "Fetch.enable" in rig.browser.transport.methods, "the browser's sessions were armed"

        with pytest.raises(CtlRefused) as refused:
            await asyncio.to_thread(client.call, "pause", {"request_id": "r1", "expect": HOST})
        assert "refused:qa_identity_host" in str(refused.value)
        assert ("handoff_refused", {"cause": "qa_identity_host"}) in records
        assert (await asyncio.to_thread(client.call, "status", {}))["paused"] is False

        # The page shown is on the console: refused with no `expect` too.
        rig.browser.transport.page_url = f"{CONSOLE}/"
        assert rig.gate.pages is not None and rig.gate.pages.current is not None
        with pytest.raises(CtlRefused) as refused:
            await asyncio.to_thread(client.call, "pause", {"request_id": "r2", "expect": None})
        assert "refused:qa_identity_host" in str(refused.value)

        rig.browser.transport.page_url = "https://example.org/signin"
        paused = await asyncio.to_thread(client.call, "pause", {"request_id": "r3", "expect": "example.org"})
        assert paused["paused"] is True
        await asyncio.to_thread(client.call, "resume", {"request_id": "r3"})

        await asyncio.to_thread(client.call, "end_control", {"cause": "attempt_end", "wipe": True})
        with pytest.raises(CtlRefused) as refused:
            await asyncio.to_thread(client.call, "qa_identity", _args(_jwt()))
        assert "refused:wiped" in str(refused.value)
        assert jwt not in json.dumps(records)
        client.close()
        stop.set()
        assert await task == 0

    asyncio.run(body())


@pytest.fixture
def short_tmp() -> Any:
    path = Path(tempfile.mkdtemp(prefix="qg", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)
