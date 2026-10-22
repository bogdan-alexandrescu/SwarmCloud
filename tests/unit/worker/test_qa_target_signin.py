"""The QA target's sign-in, the gate's and the worker's halves (VQ-4; part of #1933).

docs/design/visual-qa-any-app.md §2.2 to §2.4. The row's red-first tests:

* the password's bytes, URL-encoded and base64 forms reach no agent-bound
  frame, including a base64 response body;
* the agent's commands are held during the sign-in, and every password
  input is cleared before release;
* an MFA or CAPTCHA page answers `qa_sign_in_challenge` and offers no hand-off;
* `session_export` excludes the target's origins;
* `wipe` clears the credential;
* the worker logs only the target id.

The gate is the real `QaTarget`, `QaTargetGuard`, `SignIn` and `CdpProxy`
against test_livegate_qa_identity's scripted Chromium, extended with a login
page whose state the test sets; swarm-api is a recording fake that verifies
the attempt proof with `swarm_api.qatargets`' own message builder and parses
the body with its own model. No Chromium, network or cloud credential. Every
password and username is built at run time from random parts.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.machinery
import importlib.util
import json
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, quote_plus

import pytest

from agent_worker import children as children_mod
from agent_worker import handoff as handoff_mod
from agent_worker import qa_target
from agent_worker.hardening import PROTECTED, MemoryProtection
from agent_worker.livegate import cdpproxy as cdpproxy_mod
from agent_worker.livegate import ctl as gate_ctl
from agent_worker.livegate import mask as gate_mask
from agent_worker.livegate import qatarget as gate_qt
from agent_worker.livegate.cdppolicy import PolicyPaths
from agent_worker.livegate.cdpproxy import CdpProxy
from agent_worker.livegate.session import SessionCarry
from swarm_api import childkey as api_childkey
from swarm_api import qaspec as api_qaspec
from swarm_api import qatargets as api_qt

import test_livegate_qa_identity as qi
from test_handoff import CDP, COMMAND, CTL, CtlRefused, FakeGateClient, FakeLiveRoutes, _Log

ORIGIN = "https://app.example.com"
LOGIN_ORIGIN = "https://auth.example.com"
BASE_URL = f"{ORIGIN}/home"


def _target_id() -> str:
    return "qat_" + secrets.token_hex(8)


def _password() -> str:
    # Spaces, a slash, a plus, an ampersand and a non-ASCII letter: every one
    # changes under URL encoding. Random in the middle; never one literal.
    return "p w/" + secrets.token_hex(8) + "+&é"


def _username() -> str:
    return "qa-" + secrets.token_hex(4) + "@example.com"


def _args(target_id: str, password: str, username: str | None = None, **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "target_id": target_id, "base_url": BASE_URL, "origins": [ORIGIN, LOGIN_ORIGIN],
        "environment": "production", "rate": {"navigation_ms": 1000, "requests_per_s": 10},
        "read_posts": ["/graphql"], "exclusions": {"paths": ["/logout"]}, "mutating": False,
        "sign_in": {"kind": "own_login", "login_url": f"{LOGIN_ORIGIN}/login", "signed_in_selector": "#account"},
        "credential": {"username": username or _username(), "password": password},
    }
    args.update(over)
    return args


def _forms(password: str) -> list[str]:
    """What the row names: the bytes, the URL encodings and the base64 encodings."""
    raw = password.encode()
    return [password, quote(password, safe=""), quote_plus(password, safe=""),
            base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode().rstrip("=")]


# --------------------------------------------------------------------------
# The gate: a login page Chromium
# --------------------------------------------------------------------------


class LoginChromium(qi.FakeChromium):
    """test_livegate_qa_identity's Chromium, with tabs the gate creates and a
    login page: `probes` is what the page reads as before the submit, in turn
    (the last repeats), `after_submit` what it reads as after."""

    def __init__(self) -> None:
        super().__init__()
        self.probes: list[str] = ["form"]
        self.after_submit = "signed_in"
        self.submitted = False
        self.typed: list[tuple[str | None, str]] = []
        self.on_first_probe: Any = None
        self.closed: list[str] = []

    async def send(self, message: dict[str, Any]) -> None:
        method = message["method"]
        params = message.get("params", {})
        session = message.get("sessionId")
        if method == "Target.createTarget":
            self.received.append(message)
            tab = self._next("TAB")
            self.pages[tab] = params.get("url", "about:blank")
            self.emit("Target.targetCreated", {"targetInfo": {"targetId": tab, "type": "page"}})
            self.queue.put_nowait({"id": message["id"], "result": {"targetId": tab}})
            return
        if method == "Target.closeTarget":
            self.received.append(message)
            self.closed.append(params["targetId"])
            self.pages.pop(params["targetId"], None)
            self.queue.put_nowait({"id": message["id"], "result": {"success": True}})
            return
        if method == "Runtime.evaluate" and str(params.get("expression", "")).startswith("/*swarm-qa:"):
            self.received.append(message)
            op = params["expression"][len("/*swarm-qa:"):].split("*/", 1)[0]
            self.queue.put_nowait({"id": message["id"], "result": {"result": {"value": self._page(op)}}})
            return
        if method == "Input.insertText":
            self.typed.append((session, params["text"]))
        await super().send(message)

    def _page(self, op: str) -> Any:
        if op == "probe":
            if self.on_first_probe is not None:
                hook, self.on_first_probe = self.on_first_probe, None
                hook()
            if self.submitted:
                return self.after_submit
            return self.probes.pop(0) if len(self.probes) > 1 else self.probes[0]
        if op == "submit":
            self.submitted = True
            return True
        if op == "clear":
            return 1
        return True  # focus

    def index(self, predicate: Any) -> list[int]:
        return [i for i, m in enumerate(self.received) if predicate(m)]


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []
        self.clock = qi.Clock()
        self.fake = LoginChromium()
        self.target = gate_qt.QaTarget(self.record, clock=self.clock)
        self.proxy = CdpProxy(self.fake.send, paths=PolicyPaths(downloads_dir=tmp, uploads_dir=tmp),
                              record=self.record, qa_target=self.target, qa_sleep=self.sleep,
                              security_wait_seconds=1.0, wall=self.clock)

    def record(self, event: str, fields: Any) -> None:
        self.records.append((event, dict(fields)))

    async def sleep(self, seconds: float) -> None:
        self.clock.now += seconds
        await asyncio.sleep(0)

    async def sink(self, message: dict[str, Any]) -> None:
        await self.proxy.on_chromium_message(message)

    async def settle(self) -> None:
        for _ in range(30):
            await self.fake.queue.join()
            await asyncio.sleep(0)

    async def finished(self) -> None:
        for _ in range(400):
            await self.settle()
            if self.target.state in gate_qt.FINAL and not self.proxy._qa_tasks:
                return
        raise AssertionError(f"the sign-in never ended: {self.target.status()}")

    def names(self) -> list[str]:
        return [e for e, _ in self.records]


@pytest.fixture
def tmp() -> Any:
    path = Path(tempfile.mkdtemp(prefix="qt"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def run(body: Any) -> None:
    asyncio.run(body())


def _held_command(conn: Any) -> Any:
    return lambda m: m["method"] == "Runtime.evaluate" and m.get("sessionId") == conn.browser_session


def _is_clear(m: dict[str, Any]) -> bool:
    """The gate tab's clear, or the return's (`cdpproxy._CLEAR_PASSWORDS`)."""
    expression = str((m.get("params") or {}).get("expression", ""))
    return m["method"] == "Runtime.evaluate" and (
        expression.startswith("/*swarm-qa:clear") or expression == cdpproxy_mod._CLEAR_PASSWORDS)


# --------------------------------------------------------------------------
# The two halves agree
# --------------------------------------------------------------------------


def test_the_worker_speaks_swarm_apis_purpose_and_the_gates_verb():
    # MUTATION: change qa_target.QA_TARGET_PURPOSE and swarm-api refuses every proof.
    assert qa_target.QA_TARGET_PURPOSE == api_qt.QA_TARGET_PURPOSE
    # MUTATION: change qa_target.PROFILE and no QA capture step ever asks for its target.
    assert qa_target.PROFILE == api_qaspec.CAPTURE_PROFILE
    # MUTATION: rename livegate.qatarget.OP and the worker's verb is unknown to the gate.
    assert qa_target.CTL_OP == gate_qt.OP
    # MUTATION: drop QA_TARGET_OP from handoff.GATE_OPS and Gate.call refuses it.
    assert qa_target.CTL_OP in handoff_mod.GATE_OPS
    # MUTATION: drop QA_SIGN_IN_OP from ctl.OPS and the gate answers unknown_op.
    assert qa_target.CTL_OP in gate_ctl.OPS
    # MUTATION: change livegate.qatarget.MAX_SIGN_INS and the two halves count differently.
    assert qa_target.MAX_SIGN_INS == gate_qt.MAX_SIGN_INS == 3
    # MUTATION: drop a host from CHALLENGE_FRAMES and that CAPTCHA is typed past.
    assert tuple(gate_qt.CHALLENGE_FRAMES) == tuple(gate_mask.SHOWN_IN_CONTROL)


# --------------------------------------------------------------------------
# The scrub (§2.2 step 5)
# --------------------------------------------------------------------------


def test_the_password_its_url_and_base64_forms_reach_no_agent_bound_frame(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.start(rig.sink)
        password, username = _password(), _username()
        rig.target.handle(_args(_target_id(), password, username))
        link = qi.FakeLink()
        conn = await rig.proxy.connect_agent(link)
        assert conn is not None
        session = conn.browser_session
        # "Basic user:password" at each of the three byte alignments the
        # password can sit at inside a longer base64 text.
        basics = [base64.b64encode(f"{'u' * pad}{username}:{password}".encode()).decode() for pad in range(3)]
        assert len({(len(username) + 1 + pad) % 3 for pad in range(3)}) == 3
        for at, form in enumerate(_forms(password)):
            rig.fake.emit("Runtime.consoleAPICalled", {"args": [{"value": f"echo {at}: <{form}>"}]}, session)
        for at, basic in enumerate(basics):
            rig.fake.emit("Network.requestWillBeSentExtraInfo",
                          {"requestId": f"N1{at}", "headers": {"Authorization": f"Basic {basic}"}}, session)
        rig.fake.emit("Network.requestWillBeSent", {"requestId": "N2", "request": {
            "url": f"{ORIGIN}/login?pw={quote_plus(password)}", "postData": f"password={quote(password, safe='')}",
            "postDataEntries": [{"bytes": base64.b64encode(f"password={password}".encode()).decode()}]}},
            session)
        await rig.settle()
        # A base64 response body: decoded, scrubbed, re-encoded.
        reflected = json.dumps({"you_typed": password, "url": quote(password, safe="")}).encode()
        rig.fake.results["Network.getResponseBody"] = {"body": base64.b64encode(reflected).decode(),
                                                       "base64Encoded": True}
        await rig.proxy.on_agent_message(conn, {"id": 7, "method": "Network.getResponseBody",
                                                "params": {"requestId": "N2"}})
        await rig.settle()
        text = json.dumps(link.sent, ensure_ascii=False)
        for form in _forms(password):
            # MUTATION: drop a form from qatarget.needles and it reaches the agent here.
            assert form not in text
        # MUTATION: drop _aligned_cores from qatarget.needles and the Basic header carries it.
        assert all(basic not in text for basic in basics) and gate_qt.PLACEHOLDER in text
        reply = next(m for m in link.sent if m.get("id") == 7)
        decoded = base64.b64decode(reply["result"]["body"]).decode()
        # MUTATION: skip QaTarget._scrub_bodies and the decoded body still carries the password.
        assert password not in decoded and quote(password, safe="") not in decoded
        assert gate_qt.PLACEHOLDER in decoded
        entries = next(m for m in link.sent if m.get("method") == "Network.requestWillBeSent")
        chunk = base64.b64decode(entries["params"]["request"]["postDataEntries"][0]["bytes"]).decode()
        # MUTATION: skip QaTarget._scrub_entry and the post body chunk carries the password.
        assert password not in chunk
        scrubbed = [f for e, f in rig.records if e == "qa_target_scrubbed"]
        assert scrubbed and all(isinstance(f["count"], int) and f["count"] > 0 for f in scrubbed)
        # MUTATION: record the matched text in qa_target_scrubbed and this fails.
        assert password not in json.dumps(rig.records, ensure_ascii=False)
        await rig.fake.stop()

    run(body)


def test_a_base64_body_the_gate_cannot_decode_is_refused_while_a_password_is_held(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.start(rig.sink)
        rig.target.handle(_args(_target_id(), _password()))
        link = qi.FakeLink()
        conn = await rig.proxy.connect_agent(link)
        rig.fake.results["Network.getResponseBody"] = {"body": "not base64 !!", "base64Encoded": True}
        await rig.proxy.on_agent_message(conn, {"id": 8, "method": "Network.getResponseBody",
                                                "params": {"requestId": "N3"}})
        await rig.settle()
        reply = next(m for m in link.sent if m.get("id") == 8)
        # MUTATION: drop the qa_target_body branch in CdpProxy._on_response and the body is delivered.
        assert "error" in reply and "result" not in reply
        await rig.fake.stop()

    run(body)


# --------------------------------------------------------------------------
# The held sign-in (§2.2 steps 3-4)
# --------------------------------------------------------------------------


def test_the_agent_is_held_during_the_sign_in_and_every_password_input_is_cleared_before_release(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.start(rig.sink)
        link = qi.FakeLink()
        conn = await rig.proxy.connect_agent(link)
        assert conn is not None
        rig.fake.probes = ["loading", "form"]
        password, username = _password(), _username()

        def agent_acts() -> None:
            # The agent sends a command while the gate is on the login page.
            asyncio.get_running_loop().create_task(rig.proxy.on_agent_message(
                conn, {"id": 41, "method": "Runtime.evaluate", "params": {"expression": "document.title"}}))

        rig.fake.on_first_probe = agent_acts
        started = await gate_qt.handle_ctl(rig.target, rig.proxy, _args(_target_id(), password, username))
        assert started["qa_sign_in"] == "signing_in"
        await rig.finished()
        assert rig.target.state == "signed_in" and rig.target.cause is None

        fake = rig.fake
        (tab,) = fake.closed
        typed_at = fake.index(lambda m: m["method"] == "Input.insertText")
        closed_at = fake.index(lambda m: m["method"] == "Target.closeTarget")
        landed_at = fake.index(lambda m: m["method"] == "Page.navigate" and m["params"]["url"] == BASE_URL)
        clears = fake.index(_is_clear)
        (released_at,) = fake.index(_held_command(conn))
        # The gate typed both fields on its own tab, never the agent's session.
        assert [t for _, t in fake.typed] == [username, password]
        assert all(s not in (conn.browser_session, *conn.sessions) for s, _ in fake.typed)
        # MUTATION: forward agent commands while _qa_signing (skip `pause` in qa_sign_in) and this fails.
        assert released_at > max(typed_at)
        # MUTATION: move sign_in.close() after resume() in CdpProxy.qa_sign_in and this fails.
        assert closed_at and released_at > closed_at[0]
        # MUTATION: drop _qa_land and the agent's tab is never sent to base_url.
        assert landed_at and closed_at[0] < landed_at[0] < released_at
        # The gate's tab cleared before it closed, and the agent's tab before release.
        before_close = [i for i in clears if i < closed_at[0]]
        after_close = [i for i in clears if closed_at[0] < i < released_at]
        # MUTATION: drop the _CLEAR evaluate in SignIn.close and this fails.
        assert before_close
        # MUTATION: skip resume()'s password clear (or release before it) and this fails.
        assert after_close
        assert not rig.proxy.paused and rig.target.signing_in is False
        assert any(m.get("id") == 41 for m in link.sent), "the held command is answered after release"
        assert tab not in fake.pages
        await rig.fake.stop()

    run(body)


def test_an_agent_connection_during_the_sign_in_is_told_why_it_waits(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.start(rig.sink)
        seen: list[tuple[str, str]] = []
        rig.fake.on_first_probe = lambda: seen.append(rig.proxy.refusal())
        await gate_qt.handle_ctl(rig.target, rig.proxy, _args(_target_id(), _password()))
        await rig.finished()
        (cause, sentence) = seen[0]
        # MUTATION: drop the _qa_signing branch in CdpProxy.refusal and the agent reads "hand-off".
        assert cause == "held_for_qa_sign_in" and "QA target" in sentence
        await rig.fake.stop()

    run(body)


# --------------------------------------------------------------------------
# MFA and CAPTCHA (§2.4)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("before_submit", [True, False])
def test_an_mfa_or_captcha_page_answers_qa_sign_in_challenge_and_offers_no_hand_off(tmp, before_submit):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.start(rig.sink)
        if before_submit:
            rig.fake.probes = ["challenge"]
        else:
            rig.fake.probes, rig.fake.after_submit = ["form"], "challenge"
        await gate_qt.handle_ctl(rig.target, rig.proxy, _args(_target_id(), _password()))
        await rig.finished()
        status = rig.target.status()
        # MUTATION: map a "challenge" probe to failed in SignIn.run and this fails.
        assert status["qa_sign_in"] == "challenge" and status["cause"] == gate_qt.CHALLENGE
        if before_submit:
            # MUTATION: type before checking for a challenge and the account is typed into it.
            assert rig.fake.typed == []
        # No hand-off: the agent is released, nobody is in control, no person's page was opened.
        assert not rig.proxy.paused and not rig.proxy.in_control
        assert not rig.fake.sent("Target.createBrowserContext")
        # MUTATION: retry after a challenge in SignIn.run and a second submit is pressed.
        assert len(rig.fake.index(lambda m: str(m["params"].get("expression", "")).startswith(
            "/*swarm-qa:submit"))) == (0 if before_submit else 1)
        assert gate_qt.CHALLENGE_SENTENCE.startswith("the target's login asks for MFA or a CAPTCHA")
        await rig.fake.stop()

    run(body)


def test_the_probe_looks_for_a_one_time_code_webauthn_and_every_challenge_frame():
    probe = gate_qt._PROBE
    # MUTATION: drop the one-time-code check from _PROBE and an OTP page reads as a form.
    assert 'one-time-code' in probe
    # MUTATION: drop the WebAuthn trap and a passkey prompt reads as loading until timeout.
    assert "__swarmQaWebAuthn" in probe and "__swarmQaWebAuthn" in gate_qt._WEBAUTHN_TRAP
    for host, path in gate_mask.SHOWN_IN_CONTROL:
        assert json.dumps(host) in probe and json.dumps(path) in probe


def test_a_person_cannot_take_control_while_the_gate_signs_in(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.start(rig.sink)
        decisions: list[Any] = []

        def person() -> None:
            decisions.append(asyncio.get_running_loop().create_task(rig.proxy.take_control("PAGE1")))

        rig.fake.on_first_probe = person
        await gate_qt.handle_ctl(rig.target, rig.proxy, _args(_target_id(), _password()))
        await rig.finished()
        decision = await decisions[0]
        # MUTATION: drop the _qa_signing check in CdpProxy.take_control and a person drives the login.
        assert not decision.granted and decision.cause == "qa_sign_in_in_progress"
        await rig.fake.stop()

    run(body)


# --------------------------------------------------------------------------
# The session carry and the wipe (§2.3)
# --------------------------------------------------------------------------


def test_session_export_and_import_leave_the_targets_origins_out():
    target = gate_qt.QaTarget(lambda e, f: None)
    target.handle(_args(_target_id(), _password()))
    calls: list[tuple[str, Any]] = []
    jar = {"cookies": [
        {"name": "app", "value": secrets.token_hex(8), "domain": "app.example.com", "path": "/"},
        {"name": "auth", "value": secrets.token_hex(8), "domain": "auth.example.com", "path": "/"},
        {"name": "parent", "value": secrets.token_hex(8), "domain": ".example.com", "path": "/"},
        {"name": "other", "value": secrets.token_hex(8), "domain": "other.example", "path": "/"},
    ]}
    pages = {"targetInfos": [{"targetId": "P1", "type": "page", "url": f"{ORIGIN}/"}]}

    async def call(method: str, params: Any, session: Any) -> dict[str, Any]:
        calls.append((method, params))
        if method == "Storage.getCookies":
            return jar
        if method == "Target.getTargets":
            return pages
        return {}

    async def body() -> None:
        carry = SessionCarry(call, record=lambda e, f: None, agent_connected=lambda: False,
                             excluded_origins=target.excluded_origins)
        exported = await carry.export(park=True)
        # MUTATION: drop excluded_origins from SessionCarry._excluded_hosts and the target's cookies are carried.
        assert [c["name"] for c in exported["cookies"]] == ["other"]
        # MUTATION: skip _excluded_origin for the target and its storage is read and carried.
        assert exported["origins"] == []
        assert "Target.attachToTarget" not in [m for m, _ in calls]
        await carry.import_jar({"v": 1, "cookies": jar["cookies"], "origins": [
            {"origin": ORIGIN, "localStorage": [["k", "v"]]}]})
        written = next(p for m, p in calls if m == "Storage.setCookies")
        assert [c["name"] for c in written["cookies"]] == ["other"]

    run(body)


def test_wipe_clears_the_credential_and_refuses_the_verb_from_then_on(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.start(rig.sink)
        target_id, password = _target_id(), _password()
        rig.target.handle(_args(target_id, password))
        assert rig.target.held
        rig.target.wipe()
        # MUTATION: keep _password in QaTarget.wipe and the gate still holds the credential.
        assert not rig.target.held
        with pytest.raises(gate_qt.QaTargetRefused):
            rig.target.credential()
        assert rig.target.status()["qa_sign_in"] == "wiped"
        # MUTATION: drop the `wiped` check in QaTarget.handle and a new credential is taken after the wipe.
        with pytest.raises(gate_qt.QaTargetRefused) as refused:
            await gate_qt.handle_ctl(rig.target, rig.proxy, _args(target_id, _password()))
        assert refused.value.code == "wiped"
        assert password not in json.dumps(rig.records, ensure_ascii=False)
        await rig.fake.stop()

    run(body)


def test_the_verb_refuses_another_target_a_bad_login_origin_and_a_fourth_sign_in():
    target = gate_qt.QaTarget(lambda e, f: None)
    first = _target_id()
    with pytest.raises(gate_qt.QaTargetRefused) as refused:
        target.handle(_args(first, _password(), sign_in={"kind": "own_login", "login_url": "https://evil.example/x",
                                                         "signed_in_selector": "#a"}))
    assert refused.value.code == "login_url"
    for n in range(3):
        target.handle(_args(first, _password()))
        target.finished("signed_in")
    with pytest.raises(gate_qt.QaTargetRefused) as refused:
        target.handle(_args(first, _password()))
    # MUTATION: drop the MAX_SIGN_INS check in QaTarget.handle and a fourth sign-in runs.
    assert refused.value.code == gate_qt.EXHAUSTED
    with pytest.raises(gate_qt.QaTargetRefused) as refused:
        target.handle(_args(_target_id(), _password()))
    assert refused.value.code == "target_changed"


# --------------------------------------------------------------------------
# The worker
# --------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.at = 1_800_000_000.0

    def __call__(self) -> float:
        return self.at


class TargetRoutes(FakeLiveRoutes):
    """swarm-api with `/qa-target`: the proof verified with swarm-api's own
    message builder, the body parsed with its own model, then the target answered."""

    def __init__(self, db, trace, target_id: str | None, *, own_login: bool = True) -> None:
        super().__init__(db, trace)
        self.target_id = target_id
        self.own_login = own_login
        self.refuse: Exception | None = None
        self.issued: list[tuple[str, str]] = []

    def call(self, method, path, *, body=None, headers=None):
        if not path.endswith("/qa-target"):
            return super().call(method, path, body=body, headers=headers)
        headers = dict(headers or {})
        self.calls.append({"path": path, "body": json.loads(body)})
        self.trace.append("route:qa-target")
        message = api_qt.qa_target_message("POST", path, body, headers.get(children_mod.TIMESTAMP_HEADER) or "")
        proof = headers.get(children_mod.PROOF_HEADER)
        if not (proof and self.public_key and api_childkey.verify_proof(self.public_key, proof, message)):
            raise children_mod.ChildApiError(401, "bad_signature", "unproven", retryable=False)
        parsed = api_qt.QaTargetRequest.model_validate_json(body)
        assert path == f"/v1/attempts/{parsed.attempt_id}/qa-target"
        if self.refuse is not None:
            raise self.refuse
        if self.target_id is None:
            return 204, {}
        sign_in: dict[str, Any] = {"kind": "none"}
        if self.own_login:
            username, password = _username(), _password()
            self.issued.append((username, password))
            sign_in = {"kind": "own_login", "login_url": f"{LOGIN_ORIGIN}/login", "signed_in_selector": "#account",
                       "username": username, "password": password}
        return 200, {"target_id": self.target_id, "revision": 2, "base_url": BASE_URL,
                     "origins": [ORIGIN, LOGIN_ORIGIN], "environment": "production",
                     "rate": dict(api_qt.DEFAULT_RATE), "read_posts": [],
                     "exclusions": {"paths": ["/logout"], "controls": ["delete"]}, "sign_in": sign_in}

    def target_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["path"].endswith("/qa-target")]


class TargetGate(FakeGateClient):
    """The gate, whose `qa_sign_in` is the real `QaTarget.handle`; a poll finds
    the sign-in ended as `outcome` says."""

    def __init__(self, trace: list[str]) -> None:
        super().__init__(trace)
        self.target = gate_qt.QaTarget(lambda e, f: None)
        self.outcome: tuple[str, str | None] = ("signed_in", None)

    def call(self, op, args):
        super().call(op, args)
        if op != gate_qt.OP:
            return {}
        if args.get("poll") is True and self.target.state == "signing_in":
            self.target.finished(*self.outcome)
        try:
            return self.target.handle(args)
        except gate_qt.QaTargetRefused as exc:
            raise CtlRefused(exc.code) from None

    def handoffs(self) -> list[dict[str, Any]]:
        return [a for op, a in self.ops if op == gate_qt.OP and not a.get("poll")]


class SecretLog(_Log):
    def __init__(self) -> None:
        super().__init__()
        self.secrets: list[str] = []

    def register_secret(self, value):
        self.secrets.append(value)


def _task() -> dict[str, Any]:
    return {"id": "task_1", "tenant_id": "eng", "state": "RUNNING", "input": {}}


def _world(tmp_path: Path, db, *, marked: bool = True, own_login: bool = True, outcome=("signed_in", None),
           refuse: Exception | None = None, profile: str = "claude-code-browser"):
    trace: list[str] = []
    clock = Clock()
    target_id = _target_id()
    gate = TargetGate(trace)
    gate.outcome = outcome
    routes = TargetRoutes(db, trace, target_id if marked else None, own_login=own_login)
    routes.refuse = refuse
    key = children_mod.AttemptKey()
    routes.public_key = key.public
    children = SimpleNamespace(_key=key, _api=routes, unavailable=None)
    cfg = SimpleNamespace(tenant_id="eng", task_id="task_1", attempt_id="att_1", lease_id="lease_1",
                          generation=3, runner_profile=profile)
    log = SecretLog()
    path = handoff_mod.HandoffPath(
        cfg, logger=log, memory=MemoryProtection(PROTECTED, "test"), children=children, live=True,
        gate_client=lambda p: gate, environ={"SWARM_GATE_CTL": CTL, "SWARM_CDP_ENDPOINT": CDP}, wall=clock,
    )
    path.qa_target._sleep = lambda s: setattr(clock, "at", clock.at + s)
    path.setup(lambda n: None)
    db.seed("tasks/task_1", _task())
    path.start_qa_identity(_task())
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    env = path.prepare(work)
    return SimpleNamespace(path=path, routes=routes, gate=gate, work=work, env=env, log=log, clock=clock,
                           target_id=target_id, trace=trace)


def _spool(world) -> Path:
    return world.work / handoff_mod.SPOOL_DIR_NAME


def _everything_readable(world) -> str:
    parts = [json.dumps(world.env), json.dumps(world.log.lines, default=str, ensure_ascii=False)]
    for file in _spool(world).rglob("*"):
        if file.is_file():
            parts.append(file.read_text())
    return "\n".join(parts)


def test_the_worker_signs_in_before_the_runner_and_logs_only_the_target_id(tmp_path, db):
    world = _world(tmp_path, db)
    (call,) = world.routes.target_calls()
    assert call["body"] == {"tenant_id": "eng", "task_id": "task_1", "attempt_id": "att_1",
                            "lease_id": "lease_1", "generation": 3}
    (handed,) = world.gate.handoffs()
    ((username, password),) = world.routes.issued
    assert handed["credential"] == {}, "the worker's copy is cleared once the gate answered"
    assert world.gate.target.credential() == (username, password)
    assert world.path.qa_target.state == "signed_in"
    # Gate registered, then swarm-api, then the gate gets the target: all inside prepare.
    assert world.trace.index("gate:register") < world.trace.index("route:qa-target") \
        < world.trace.index("gate:qa_sign_in")
    readable = _everything_readable(world)
    # MUTATION: log the answer (or the hand-off args) in QaTargetSignIn._ask and this fails.
    assert password not in readable and username not in readable
    assert "#account" not in readable, "nor a selector"
    logged = [kw for _, kw in world.log.lines if kw.get("target_id")]
    # MUTATION: drop target_id from the "qa target handed to the gate" log line and this fails.
    assert logged and all(kw["target_id"] == world.target_id for kw in logged)
    # MUTATION: skip register_secret in QaTargetSignIn._ask and the redaction never learns it.
    assert world.log.secrets == [password]
    assert not (_spool(world) / qa_target.NOTICE_NAME).exists()


def test_an_attempt_whose_agent_has_no_target_hands_nothing_to_the_gate(tmp_path, db):
    world = _world(tmp_path, db, marked=False)
    assert len(world.routes.target_calls()) == 1, "asked once; swarm-api answered 204"
    # MUTATION: hand a 204 to the gate (or treat it as a failure) in QaTargetSignIn._ask and this fails.
    assert world.gate.handoffs() == [] and world.path.qa_target.has_target is False
    assert not (_spool(world) / qa_target.NOTICE_NAME).exists()
    world.path.tick(world.work, lambda: {})
    assert len(world.routes.target_calls()) == 1, "never asked again"


def test_another_profile_asks_nothing(tmp_path, db):
    world = _world(tmp_path, db, profile="mock")
    # MUTATION: drop the PROFILE check in QaTargetSignIn.__init__ and every live attempt asks swarm-api.
    assert world.routes.target_calls() == [] and world.gate.handoffs() == []


def test_a_first_ask_that_says_nothing_of_a_target_writes_no_notice(tmp_path, db):
    unreachable = children_mod.ChildApiError(None, "api_unavailable", "", retryable=True)
    world = _world(tmp_path, db, refuse=unreachable)
    assert world.path.qa_target.cause == qa_target.UNREACHABLE
    # MUTATION: write the notice whenever the state is failed and an agent with no target is told of one.
    assert not (_spool(world) / qa_target.NOTICE_NAME).exists()


def test_a_refused_target_leaves_the_browser_signed_out_with_a_notice_not_a_failed_task(tmp_path, db):
    refusal = children_mod.ChildApiError(409, "qa_target_host_pending", "", retryable=False)
    world = _world(tmp_path, db, refuse=refusal)
    assert world.gate.handoffs() == [], "nothing reaches the gate"
    assert world.path.qa_target.cause == "qa_target_host_pending"
    assert handoff_mod.HANDOFF_ENV in world.env, "the agent still starts: public screens still run"
    notice = (_spool(world) / qa_target.NOTICE_NAME).read_text()
    # MUTATION: skip _write_notice in QaTargetSignIn.start and the agent is never told why.
    assert "qa_target_host_pending" in notice and "swarm-handoff sign-in" in notice


def test_a_challenge_notice_names_its_cause_and_offers_neither_a_retry_nor_a_hand_off(tmp_path, db):
    world = _world(tmp_path, db, outcome=("challenge", gate_qt.CHALLENGE))
    assert world.path.qa_target.cause == gate_qt.CHALLENGE
    notice = (_spool(world) / qa_target.NOTICE_NAME).read_text()
    assert "qa_sign_in_challenge" in notice and "MFA or a CAPTCHA" in notice
    # MUTATION: offer `swarm-handoff sign-in` after a challenge in qa_target.notice and this fails.
    assert "swarm-handoff sign-in" not in notice and "never ask for a hand-off" in notice


def _load_command() -> Any:
    loader = importlib.machinery.SourceFileLoader("swarm_handoff_vq4", str(COMMAND))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _sign_in(world, monkeypatch) -> tuple[int, dict[str, Any]]:
    """`swarm-handoff sign-in` in a thread while the worker polls."""
    command = _load_command()
    monkeypatch.setenv("SWARM_HANDOFF", str(_spool(world)))
    out: dict[str, Any] = {}

    def agent() -> None:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            out["code"] = command.main(["sign-in", "--max-seconds", "10", "--poll", "0.1"])
        out["answer"] = json.loads(buffer.getvalue().strip().splitlines()[-1])

    thread = threading.Thread(target=agent)
    thread.start()
    deadline = time.monotonic() + 10
    while thread.is_alive() and time.monotonic() < deadline:
        world.path.tick(world.work, lambda: {})
        time.sleep(0.05)
    thread.join(5)
    return out["code"], out["answer"]


def test_swarm_handoff_sign_in_asks_swarm_api_again_and_stops_at_three(tmp_path, db, monkeypatch):
    world = _world(tmp_path, db)
    code, answer = _sign_in(world, monkeypatch)
    # MUTATION: drop `sign-in` from swarm-handoff (or the tick's keep) and the agent cannot sign in again.
    assert code == 0 and answer["outcome"] == "applied" and answer["signed_in"] is True
    assert len(world.routes.target_calls()) == 2, "the worker asked swarm-api again"
    code, _ = _sign_in(world, monkeypatch)
    assert code == 0 and world.path.qa_target.sign_ins == 3
    code, answer = _sign_in(world, monkeypatch)
    # MUTATION: drop the MAX_SIGN_INS check in QaTargetSignIn._answer_requests and a fourth is asked.
    assert code == 5 and answer["code"] == qa_target.EXHAUSTED
    assert len(world.routes.target_calls()) == 3
    assert all(p not in _everything_readable(world) for _, p in world.routes.issued)


def test_a_challenge_is_answered_to_swarm_handoff_without_asking_again(tmp_path, db, monkeypatch):
    world = _world(tmp_path, db, outcome=("challenge", gate_qt.CHALLENGE))
    code, answer = _sign_in(world, monkeypatch)
    # MUTATION: re-ask swarm-api after a challenge and the gate types into the MFA page again.
    assert code == 5 and answer["code"] == gate_qt.CHALLENGE
    assert len(world.routes.target_calls()) == 1 and len(world.gate.handoffs()) == 1


def test_a_public_target_is_handed_over_with_no_credential(tmp_path, db):
    world = _world(tmp_path, db, own_login=False)
    (handed,) = world.gate.handoffs()
    assert "credential" not in handed and handed["sign_in"] == {"kind": "none"}
    assert world.gate.target.state == "ready" and world.path.qa_target.state == "ready"
    assert world.log.secrets == []
