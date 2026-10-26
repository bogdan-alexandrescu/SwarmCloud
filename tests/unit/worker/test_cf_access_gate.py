"""The live gate's side of a Cloudflare Access service token (CF-ACCESS-TOKEN, part of #2442).

The gate adds `CF-Access-Client-Id` and `CF-Access-Client-Secret` to requests
for the hosts the token's grant lists, on its own CDP sessions, and the agent
never reads either value. Every test drives the real `CfAccess`,
`CfAccessInjector` and `CdpProxy` against the scripted Chromium of
test_livegate_qa_identity.py, which records what "reached the network" (a
Request-stage pause the gate continued), so "the token never left for
evil.example" is read off what the fake saw, not off the gate's bookkeeping.

A redirect is modelled as M5 measured it (docs/design/qa-identity-measurements.md):
its request comes back as a pause of its own, with `redirectedRequestId`, and
WITHOUT the header the gate added; one test also models a Chromium that did
carry it, which guard 2 must strip.

No test runs Chromium, a network or a cloud credential. The fake token is made
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

from agent_worker.livegate import ctl
from agent_worker.livegate.cdppolicy import PolicyPaths
from agent_worker.livegate.cdpproxy import CdpProxy
from agent_worker.livegate.cfaccess import (
    HEADER_ID,
    HEADER_SECRET,
    HOST,
    PLACEHOLDER,
    CfAccess,
    CfAccessInjector,
    CfAccessRefused,
)
from agent_worker.livegate.ctl import CtlClient, CtlRefused

import test_livegate_main as rigs
from test_livegate_qa_identity import FakeChromium, FakeLink

STAGING = "staging-admin.example.dev"
PRODUCTION = "admin.example.com"
EVIL = "https://evil.example"


def _token(hosts: list[str] | None = None) -> dict[str, Any]:
    # Shaped like Cloudflare's: a hex id ending `.access`, a hex secret. Random each run.
    return {"hosts": list(hosts or [STAGING, PRODUCTION]),
            "client_id": secrets.token_hex(16) + ".access", "client_secret": secrets.token_hex(32)}


class Rig:
    def __init__(self, tmp: Path, *, token: dict[str, Any] | None = None) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []
        self.fake = FakeChromium()
        self.cf = CfAccess(self.record)
        self.proxy = CdpProxy(self.fake.send, paths=PolicyPaths(downloads_dir=tmp, uploads_dir=tmp),
                              record=self.record, cf_access=self.cf, security_wait_seconds=1.0)
        self.injector = CfAccessInjector(self.proxy, self.cf, record=self.record)
        self.token = token if token is not None else _token()

    def record(self, event: str, fields: Any) -> None:
        self.records.append((event, dict(fields)))

    def events(self, name: str) -> list[dict[str, Any]]:
        return [f for e, f in self.records if e == name]

    async def sink(self, message: dict[str, Any]) -> None:
        # What LiveGate.on_chromium_message does: the proxy, then the gate's listeners.
        await self.proxy.on_chromium_message(message)
        if "method" in message:
            self.injector.on_event(message)

    async def start(self, *, set_token: bool = True) -> None:
        self.fake.start(self.sink)
        if set_token:
            self.cf.handle({"tokens": [self.token]})
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

    async def navigated(self, session: str, url: str, frame: str = "PAGE1", parent: str | None = None) -> None:
        info: dict[str, Any] = {"id": frame, "url": url, "loaderId": "L"}
        if parent is not None:
            info["parentId"] = parent
        self.fake.emit("Page.frameNavigated", {"frame": info}, session)
        await self.settle()

    def leaked(self, value: Any) -> bool:
        text = json.dumps(value)
        return self.token["client_id"] in text or self.token["client_secret"] in text


@pytest.fixture
def tmp() -> Any:
    path = Path(tempfile.mkdtemp(prefix="cfa"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def run(body: Any) -> None:
    asyncio.run(body())


def _cf(headers: dict[str, str]) -> dict[str, str]:
    return {n: v for n, v in headers.items() if n.lower() in (HEADER_ID.lower(), HEADER_SECRET.lower())}


# --------------------------------------------------------------------------
# The headers: listed hosts only
# --------------------------------------------------------------------------


def test_a_listed_host_gets_both_headers_and_an_unlisted_one_gets_neither(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        page = rig.page_session()
        rig.fake.request(page, f"https://{STAGING}/")
        await rig.settle()
        url, headers = rig.fake.network[-1]
        assert url == f"https://{STAGING}/"
        assert _cf(headers) == {HEADER_ID: rig.token["client_id"], HEADER_SECRET: rig.token["client_secret"]}

        await rig.navigated(page, f"https://{STAGING}/")
        rig.fake.request(page, f"https://{PRODUCTION}/api/users", kind="Fetch")
        rig.fake.request(page, f"{EVIL}/collect", kind="Fetch")
        await rig.settle()
        assert _cf(rig.fake.reached(f"https://{PRODUCTION}")[-1][1]), "a second listed host, from a listed page"
        assert rig.fake.reached(EVIL) and all(not _cf(h) for _, h in rig.fake.reached(EVIL))
        assert not rig.leaked(rig.records), "a record never carries a value"
        await rig.fake.stop()

    run(body)


@pytest.mark.parametrize("url", [
    f"https://{STAGING}.evil.example/",       # the listed host as a prefix
    f"https://evil.{STAGING}/",               # a subdomain of it
    f"http://{STAGING}/",                     # not https
    f"https://{STAGING}:8443/",               # another port
    f"https://user@{STAGING}/",               # userinfo
    f"https://{STAGING.upper()}/",            # not as listed
])
def test_a_look_alike_of_a_listed_host_gets_no_header(tmp, url):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        rig.fake.request(rig.page_session(), url)
        await rig.settle()
        assert rig.fake.network[-1][0] == url and not _cf(rig.fake.network[-1][1])
        await rig.fake.stop()

    run(body)


def test_a_redirect_from_a_listed_host_to_an_unlisted_one_carries_no_header(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        page = rig.page_session()
        first = rig.fake.request(page, f"https://{STAGING}/login")
        await rig.settle()
        assert _cf(rig.fake.network[-1][1])
        # M5: the redirect's own pause, without the header the gate added.
        rig.fake.request(page, f"{EVIL}/callback", redirected=first, headers={"Accept": "*/*"})
        await rig.settle()
        assert rig.fake.network[-1] == (f"{EVIL}/callback", {"Accept": "*/*"})

        # A Chromium that DID carry the headers across the redirect: guard 2 strips them.
        second = rig.fake.request(page, f"https://{STAGING}/again")
        await rig.settle()
        carried = {"Accept": "*/*", HEADER_ID: rig.token["client_id"], HEADER_SECRET: rig.token["client_secret"]}
        rig.fake.request(page, f"{EVIL}/callback2", redirected=second, headers=carried)
        await rig.settle()
        assert rig.fake.network[-1] == (f"{EVIL}/callback2", {"Accept": "*/*"})
        assert rig.events("cf_access_header_stripped") == [{"origin": EVIL}]
        assert not rig.leaked(rig.fake.reached(EVIL))
        await rig.fake.stop()

    run(body)


def test_a_redirect_between_listed_hosts_is_given_the_headers_again(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        page = rig.page_session()
        first = rig.fake.request(page, f"https://{STAGING}/")
        await rig.settle()
        rig.fake.request(page, f"https://{PRODUCTION}/", redirected=first, headers={"Accept": "*/*"})
        await rig.settle()
        url, headers = rig.fake.network[-1]
        assert url == f"https://{PRODUCTION}/" and _cf(headers)[HEADER_ID] == rig.token["client_id"]
        await rig.fake.stop()

    run(body)


def test_an_unlisted_page_fetching_a_listed_host_does_not_spend_the_token(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        page = rig.page_session()
        await rig.navigated(page, f"{EVIL}/")
        rig.fake.request(page, f"https://{STAGING}/api/me", kind="XHR")
        await rig.settle()
        assert rig.fake.network[-1][0] == f"https://{STAGING}/api/me" and not _cf(rig.fake.network[-1][1])
        await rig.fake.stop()

    run(body)


def test_a_request_from_a_worker_target_gets_no_header(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.children["PAGE1"] = [("service_worker", "W1")]
        await rig.start()
        page = rig.page_session()
        worker = next(s for s, t in rig.fake.sessions.items() if t == "W1")
        await rig.navigated(page, f"https://{STAGING}/")
        rig.fake.request(worker, f"https://{STAGING}/sw-fetch", frame="PAGE1", kind="Fetch")
        await rig.settle()
        assert not _cf(rig.fake.network[-1][1])
        await rig.fake.stop()

    run(body)


def test_without_a_token_nothing_is_intercepted_and_nothing_is_added(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start(set_token=False)
        assert rig.fake.sent("Fetch.enable") == [], "no slot, no interception at all"
        assert rig.cf.status() == {"state": "none"}
        assert rig.cf.headers_for(f"https://{STAGING}/") is None
        await rig.fake.stop()

    run(body)


def test_after_the_wipe_no_header_is_added(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        page = rig.page_session()
        rig.cf.wipe()
        rig.fake.request(page, f"https://{STAGING}/")
        await rig.settle()
        assert not _cf(rig.fake.network[-1][1])
        with pytest.raises(CfAccessRefused) as refused:
            rig.cf.handle({"tokens": [_token()]})
        assert refused.value.code == "wiped"
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


def test_an_agent_bound_event_or_result_never_carries_a_value(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        connection, link, session = await _agent(rig)
        sent_headers = {HEADER_ID: rig.token["client_id"], HEADER_SECRET: rig.token["client_secret"],
                        "Accept": "*/*"}
        rig.fake.emit("Network.requestWillBeSentExtraInfo", {"requestId": "N1", "headers": sent_headers}, session)
        rig.fake.emit("Fetch.requestPaused", {"requestId": "A1", "resourceType": "Document",
                                              "request": {"url": f"https://{STAGING}/", "headers": sent_headers}},
                      session)
        rig.fake.emit("Runtime.consoleAPICalled", {"type": "log", "args": [
            {"type": "string", "value": f"echo {rig.token['client_secret']} here"}]}, session)
        await rig.settle()
        extra, paused, console = link.sent[-3], link.sent[-2], link.sent[-1]
        assert extra["params"]["headers"] == {"Accept": "*/*"}
        assert paused["params"]["request"]["headers"] == {"Accept": "*/*"}
        assert console["params"]["args"][0]["value"] == f"echo {PLACEHOLDER} here"

        # A tool result: a response body, plain and base64.
        rig.fake.results["Network.getResponseBody"] = {
            "body": f"<pre>{rig.token['client_id']}</pre>", "base64Encoded": False}
        await rig.proxy.on_agent_message(connection, {"id": 7, "method": "Network.getResponseBody",
                                                      "params": {"requestId": "N1"}})
        await rig.settle()
        rig.fake.results["Network.getResponseBody"] = {"body": base64.b64encode(
            f"secret={rig.token['client_secret']}".encode()).decode(), "base64Encoded": True}
        await rig.proxy.on_agent_message(connection, {"id": 8, "method": "Network.getResponseBody",
                                                      "params": {"requestId": "N1"}})
        await rig.settle()
        plain = next(m for m in link.sent if m.get("id") == 7)
        encoded = next(m for m in link.sent if m.get("id") == 8)
        assert plain["result"]["body"] == f"<pre>{PLACEHOLDER}</pre>"
        assert base64.b64decode(encoded["result"]["body"]).decode() == f"secret={PLACEHOLDER}"
        assert not rig.leaked(link.sent), "nothing the agent received holds a value"
        assert not rig.leaked(rig.records)
        await rig.fake.stop()

    run(body)


def test_a_body_the_scrub_cannot_decode_is_refused(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        connection, link, _ = await _agent(rig)
        rig.fake.results["Network.getResponseBody"] = {"body": "not base64 at all!", "base64Encoded": True}
        await rig.proxy.on_agent_message(connection, {"id": 4, "method": "Network.getResponseBody",
                                                      "params": {"requestId": "N1"}})
        await rig.settle()
        reply = next(m for m in link.sent if m.get("id") == 4)
        assert "error" in reply and "result" not in reply
        assert {"method": "Network.getResponseBody", "cause": "cf_access_body"} in rig.events("cdp_method_refused")
        await rig.fake.stop()

    run(body)


def test_the_agent_may_not_continue_a_paused_request_to_another_url(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start()
        connection, link, _ = await _agent(rig)
        before = len(rig.fake.sent("Fetch.continueRequest"))
        await rig.proxy.on_agent_message(connection, {"id": 1, "method": "Fetch.continueRequest",
                                                      "params": {"requestId": "A1", "url": f"{EVIL}/x"}})
        await rig.settle()
        reply = next(m for m in link.sent if m.get("id") == 1)
        assert "refused by the live gate" in reply["error"]["message"]
        assert len(rig.fake.sent("Fetch.continueRequest")) == before, "it never reached Chromium"
        assert {"method": "Fetch.continueRequest", "cause": "cf_access"} in rig.events("cdp_method_refused")

        # Continuing as it was is the agent's to do.
        await rig.proxy.on_agent_message(connection, {"id": 2, "method": "Fetch.continueRequest",
                                                      "params": {"requestId": "A1"}})
        await rig.settle()
        assert len(rig.fake.sent("Fetch.continueRequest")) == before + 1
        await rig.fake.stop()

    run(body)


def test_without_a_token_the_agent_link_is_untouched(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        await rig.start(set_token=False)
        connection, link, session = await _agent(rig)
        headers = {HEADER_ID: "the-app's-own-header", "Accept": "*/*"}
        rig.fake.emit("Network.requestWillBeSentExtraInfo", {"requestId": "N1", "headers": headers}, session)
        await rig.settle()
        assert link.sent[-1]["params"]["headers"] == headers
        assert rig.cf.agent_refusal("Fetch.continueRequest", {"url": f"{EVIL}/"}) is None
        await rig.fake.stop()

    run(body)


# --------------------------------------------------------------------------
# The verb
# --------------------------------------------------------------------------


@pytest.mark.parametrize("change, code", [
    ({"hosts": []}, "hosts"),
    ({"hosts": ["*.example.com"]}, "hosts"),
    ({"hosts": ["203.0.113.7"]}, "hosts"),
    ({"hosts": ["admin.example.com:443"]}, "hosts"),
    ({"hosts": ["Admin.Example.com"]}, "hosts"),
    ({"client_id": "short"}, "client_id"),
    ({"client_secret": "has a space " + "x" * 20}, "client_secret"),
    ({"client_secret": "line\nbreak" + "x" * 20}, "client_secret"),
])
def test_the_verb_refuses_a_malformed_token(change, code):
    records: list[tuple[str, Any]] = []
    cf = CfAccess(lambda e, f: records.append((e, dict(f))))
    with pytest.raises(CfAccessRefused) as refused:
        cf.handle({"tokens": [{**_token(), **change}]})
    assert refused.value.code == code and not cf.armed
    assert records == [("cf_access_refused", {"cause": code})]


def test_the_verb_is_taken_once_and_a_host_is_one_tokens():
    cf = CfAccess(lambda e, f: None)
    with pytest.raises(CfAccessRefused) as refused:
        cf.handle({"tokens": [_token([STAGING]), _token([STAGING])]})
    assert refused.value.code == "host_twice"
    assert cf.handle({"tokens": [_token([STAGING]), _token([PRODUCTION])]})["hosts"] == sorted([STAGING, PRODUCTION])
    with pytest.raises(CfAccessRefused) as refused:
        cf.handle({"tokens": [_token([STAGING])]})
    assert refused.value.code == "already_set"


def test_the_host_rule_is_swarm_apis():
    from swarm_api import workspace_secrets

    assert HOST.pattern == workspace_secrets.SLOT_HOST.pattern


def test_cf_access_is_an_operation_ctl_sock_accepts():
    assert "cf_access" in ctl.OPS


def test_the_gate_takes_the_verb_arms_the_browser_and_refuses_it_after_the_wipe(short_tmp):
    async def body() -> None:
        records: list[tuple[str, Any]] = []
        rig = rigs.Rig(short_tmp)
        rig.gate.record = lambda e, f: records.append((e, dict(f)))
        rig.gate.cf_access._record = rig.gate.record
        stop, task = await rigs._started(rig)
        client = CtlClient(rig.cfg.ctl_path)
        await asyncio.to_thread(client.register)
        token = _token()
        held = await asyncio.to_thread(client.call, "cf_access", {"tokens": [token]})
        assert held == {"cf_access": "held", "hosts": sorted(token["hosts"])}
        status = await asyncio.to_thread(client.call, "status", {})
        assert status["cf_access"]["state"] == "held"
        assert token["client_secret"] not in json.dumps(status) and token["client_id"] not in json.dumps(status)
        assert "Fetch.enable" in rig.browser.transport.methods, "the browser's sessions were armed"

        await asyncio.to_thread(client.call, "end_control", {"cause": "attempt_end", "wipe": True})
        with pytest.raises(CtlRefused) as refused:
            await asyncio.to_thread(client.call, "cf_access", {"tokens": [_token()]})
        assert "refused:wiped" in str(refused.value)
        assert token["client_secret"] not in json.dumps(records)
        client.close()
        stop.set()
        assert await task == 0

    asyncio.run(body())


@pytest.fixture
def short_tmp() -> Any:
    path = Path(tempfile.mkdtemp(prefix="cg", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)
