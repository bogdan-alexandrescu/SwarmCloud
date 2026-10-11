"""Signed-in sessions carried gate to gate, and named browser logins (live browser L4c).

docs/design/live-browser.md section 5.5.1 (LB-7). A session is a cookie jar
plus origin storage. In a personal tenant (`u-<user>`) the gate exports it on
its own pipe session when a workflow step ends, the worker stores it as
`swarm-tenant-u-<user>-browser-wf-<workflow>`, and the next step's gate
imports it before the agent's first CDP connection. A group tenant stores and
carries nothing. A task opts into a named login by name, and an unknown or
expired name fails before the agent runs. The agent's CDP can neither read nor
plant cookies.

Every test drives the real modules against scripted fakes: a Chromium that
answers the gate's CDP calls, a gate channel that forwards the worker's ctl
ops to the real `SessionCarry`, and a vault standing in for Secret Manager.
No test runs Chromium, a network or a cloud credential. Every cookie value is
built at run time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_worker import browser_login
from agent_worker.browser_login import BrowserLoginRefused, SessionCarrier, plan_for
from agent_worker.livegate import cdppolicy, session
from agent_worker.livegate.cdppolicy import PolicyPaths
from agent_worker.livegate.cdpproxy import CdpProxy
from agent_worker.livegate.session import SessionCarry, SessionRefused

PERSONAL = "u-alice-example-com"
GROUP = "eng"
WORKFLOW = "wf-7c1a"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def run(coro_fn):
    return asyncio.run(coro_fn())


def cookie_value() -> str:
    # Built at run time: nothing here may look like a credential in the diff.
    return "v" + secrets.token_hex(20)


def fake_cookie(name: str, domain: str, **extra: Any) -> dict[str, Any]:
    cookie = {
        "name": name,
        "value": cookie_value(),
        "domain": domain,
        "path": "/",
        "expires": time.time() + 3600,
        "size": 40,
        "httpOnly": True,
        "secure": True,
        "session": False,
        "sameSite": "Lax",
        "priority": "Medium",
        "sourceScheme": "Secure",
        "sourcePort": 443,
    }
    cookie.update(extra)
    return cookie


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeBrowser:
    """The gate's CDP calls, answered the way Chromium's pipe would.

    Cookies are kept per browser context (`None` is the default context), and
    local storage per origin. Every call is kept in `calls`.
    """

    def __init__(self) -> None:
        self.cookies: dict[str | None, list[dict[str, Any]]] = {None: []}
        self.storage: dict[str, dict[str, str]] = {}
        self.pages: list[dict[str, Any]] = [
            {"targetId": "page-1", "type": "page", "url": "about:blank", "browserContextId": "default"}
        ]
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self._n = 0
        self._attached: dict[str, str] = {}

    async def call(self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None) -> dict[str, Any]:
        params = dict(params or {})
        self.calls.append((method, params, session_id))
        if method == "Target.getBrowserContexts":
            return {"browserContextIds": [c for c in self.cookies if c is not None]}
        if method == "Storage.getCookies":
            return {"cookies": [dict(c) for c in self.cookies.get(params.get("browserContextId"), [])]}
        if method == "Storage.setCookies":
            self.cookies.setdefault(params.get("browserContextId"), []).extend(dict(c) for c in params["cookies"])
            return {}
        if method == "Target.getTargets":
            return {"targetInfos": [dict(p) for p in self.pages]}
        if method == "Target.attachToTarget":
            assert params.get("flatten") is True
            self._n += 1
            sid = f"gate-s{self._n}"
            self._attached[sid] = params["targetId"]
            return {"sessionId": sid}
        if method == "Target.detachFromTarget":
            self._attached.pop(params.get("sessionId", ""), None)
            return {}
        if method in ("DOMStorage.enable", "DOMStorage.disable"):
            assert session_id in self._attached
            return {}
        if method == "DOMStorage.getDOMStorageItems":
            assert session_id in self._attached
            origin = params["storageId"]["securityOrigin"]
            return {"entries": [[k, v] for k, v in self.storage.get(origin, {}).items()]}
        if method == "DOMStorage.setDOMStorageItem":
            assert session_id in self._attached
            origin = params["storageId"]["securityOrigin"]
            self.storage.setdefault(origin, {})[params["key"]] = params["value"]
            return {}
        raise AssertionError(f"unexpected gate call {method}")

    def methods(self) -> list[str]:
        return [m for m, _, _ in self.calls]


class Records:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, fields: Any) -> None:
        self.items.append((event, dict(fields)))

    def names(self) -> list[str]:
        return [e for e, _ in self.items]

    def text(self) -> str:
        return json.dumps(self.items, default=str)


class Gate:
    """One pod's gate: a FakeBrowser behind a real SessionCarry, reached the
    way the worker reaches it -- `call(op, args)` -- with each message passed
    through JSON as ctl.sock would carry it."""

    def __init__(self, browser: FakeBrowser | None = None) -> None:
        self.browser = browser or FakeBrowser()
        self.records = Records()
        self.agent_connected = False
        self.carry = SessionCarry(self.browser.call, record=self.records, agent_connected=lambda: self.agent_connected)
        self.ops: list[str] = []

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        self.ops.append(op)
        wire_args = json.loads(json.dumps(args))
        result = asyncio.run(self.carry.handle(op, wire_args))
        return json.loads(json.dumps(result))


class FakeVault:
    """Secret Manager, as far as the carrier sees it."""

    def __init__(self) -> None:
        self.secrets: dict[str, list[str]] = {}
        self.meta: dict[str, dict[str, Any]] = {}
        self.reads: list[str] = []

    def read(self, name: str) -> str | None:
        self.reads.append(name)
        versions = self.secrets.get(name)
        return versions[-1] if versions else None

    def write(self, name: str, payload: str, *, ttl_seconds: int | None = None,
              expire_time: datetime | None = None, labels: dict[str, str] | None = None) -> None:
        self.secrets.setdefault(name, []).append(payload)
        self.meta.setdefault(name, {"ttl_seconds": ttl_seconds, "expire_time": expire_time, "labels": dict(labels or {})})


def task(tenant: str = PERSONAL, workflow: str | None = WORKFLOW, login: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"prompt": "check the dashboard"}
    if login is not None:
        payload["browser_login"] = login
    return {"task_id": "t-1", "tenant_id": tenant, "workflow_id": workflow, "input": payload}


def carrier(vault: FakeVault, gate: Gate, records: Records | None = None, **kw: Any) -> SessionCarrier:
    return SessionCarrier(vault, gate, record=records if records is not None else Records(), now=lambda: NOW, **kw)


def signed_in(gate: Gate, *cookies: dict[str, Any], origin: str | None = None) -> None:
    """A hand-off returned on `gate`, leaving these cookies in Chromium."""
    gate.browser.cookies[None].extend(cookies)
    if origin is not None:
        gate.browser.pages.append({"targetId": "page-2", "type": "page", "url": origin + "/home", "browserContextId": "default"})
        gate.browser.storage[origin] = {"token-cache": cookie_value()}
    gate.carry.note_returned()


# --------------------------------------------------------------------------
# Workflow carry, personal tenant
# --------------------------------------------------------------------------


def test_a_personal_workflow_step_carries_its_jar_to_the_next_step_before_the_agent_connects(caplog):
    caplog.set_level(logging.DEBUG)
    vault = FakeVault()
    first, second = Gate(), Gate()
    jar_cookie = fake_cookie("sid", ".github.com")

    plan = plan_for(PERSONAL, task())
    assert plan.carry_secret == f"swarm-tenant-{PERSONAL}-browser-wf-{WORKFLOW}"

    # Step 1: nothing stored yet, so nothing is imported; the person signs in.
    one = carrier(vault, first)
    assert one.before_agent(plan) == "none"
    assert "session_import" not in first.ops
    signed_in(first, jar_cookie, origin="https://github.com")
    written = one.after_agent(plan)

    # Exported on the gate's pipe, with Storage.getCookies, then written once.
    assert first.ops == ["session_export"]
    assert "Storage.getCookies" in first.browser.methods()
    assert written == [plan.carry_secret]
    assert list(vault.secrets) == [plan.carry_secret]
    assert vault.meta[plan.carry_secret]["ttl_seconds"] == 24 * 3600

    # Step 2: the next pod's gate gets it before any agent connection.
    assert second.agent_connected is False
    two = carrier(vault, second)
    assert two.before_agent(plan) == "carry"
    assert second.ops == ["session_import"]
    imported = second.browser.cookies[None]
    assert [(c["name"], c["domain"], c["value"]) for c in imported] == [
        (jar_cookie["name"], jar_cookie["domain"], jar_cookie["value"])
    ]
    # Into the default context, which is the one connectOverCDP hands the agent.
    set_calls = [p for m, p, _ in second.browser.calls if m == "Storage.setCookies"]
    assert set_calls and all("browserContextId" not in p for p in set_calls)
    # The signed-in origin's local storage came across too.
    assert second.browser.storage["https://github.com"] == first.browser.storage["https://github.com"]


def test_the_export_covers_every_browser_context_and_drops_expired_cookies():
    gate = Gate()
    live = fake_cookie("sid", "github.com")
    stale = fake_cookie("old", "github.com", expires=time.time() - 60)
    gate.browser.cookies["ctx-fresh"] = [live, stale]  # the gate-owned fresh page's context
    gate.carry.note_returned()
    jar = asyncio.run(gate.carry.export())
    assert jar is not None
    assert [c["name"] for c in jar["cookies"]] == ["sid"]
    # A cookie is carried as Storage.setCookies takes it: no read-only fields.
    assert "size" not in jar["cookies"][0] and "session" not in jar["cookies"][0]


def test_a_step_with_no_hand_off_exports_nothing_and_overwrites_nothing():
    vault = FakeVault()
    plan = plan_for(PERSONAL, task())
    first = Gate()
    signed_in(first, fake_cookie("sid", "github.com"))
    carrier(vault, first).after_agent(plan)
    before = list(vault.secrets[plan.carry_secret])

    # Step 2 imports, does no hand-off, and must not write a new version.
    second = Gate()
    two = carrier(vault, second)
    assert two.before_agent(plan) == "carry"
    assert two.after_agent(plan) == []
    assert vault.secrets[plan.carry_secret] == before


def test_the_gate_refuses_an_import_once_an_agent_has_connected():
    gate = Gate()
    gate.agent_connected = True
    jar = {"v": 1, "cookies": [{"name": "sid", "value": cookie_value(), "domain": "github.com", "path": "/"}], "origins": []}
    with pytest.raises(SessionRefused):
        asyncio.run(gate.carry.handle(session.IMPORT_OP, {"jar": jar}))
    assert "Storage.setCookies" not in gate.browser.methods()
    assert "browser_session_import_refused" in gate.records.names()


def test_an_oversized_jar_is_not_carried():
    gate = Gate()
    big = [fake_cookie(f"c{i}", "example.com", value="x" * 4000) for i in range(20)]
    signed_in(gate, *big)
    assert asyncio.run(gate.carry.export()) is None
    assert "browser_session_too_large" in gate.records.names()


# --------------------------------------------------------------------------
# Group tenants, and steps outside a workflow
# --------------------------------------------------------------------------


def test_a_group_tenant_writes_nothing_and_carries_nothing():
    vault = FakeVault()
    plan = plan_for(GROUP, task(tenant=GROUP))
    assert plan.carry_secret is None
    gate = Gate()
    c = carrier(vault, gate)
    assert c.before_agent(plan) == "none"
    signed_in(gate, fake_cookie("sid", "github.com"))
    assert c.after_agent(plan) == []
    assert vault.secrets == {} and vault.reads == []
    # The gate is never even asked: nothing leaves Chromium.
    assert gate.ops == []
    assert "Storage.getCookies" not in gate.browser.methods()


def test_a_group_tenant_naming_a_login_is_refused_personal_only():
    with pytest.raises(BrowserLoginRefused) as caught:
        plan_for(GROUP, task(tenant=GROUP, login="github-qa"))
    assert caught.value.cause == "browser_login_personal_only"


def test_a_step_outside_a_workflow_carries_nothing():
    vault = FakeVault()
    plan = plan_for(PERSONAL, task(workflow=None))
    assert plan.carry_secret is None
    gate = Gate()
    c = carrier(vault, gate)
    assert c.before_agent(plan) == "none"
    signed_in(gate, fake_cookie("sid", "github.com"))
    assert c.after_agent(plan) == []
    assert vault.secrets == {} and gate.ops == []


def test_the_secret_names_are_the_tenants_own_and_refuse_odd_identifiers():
    assert browser_login.carry_secret_name(PERSONAL, WORKFLOW) == f"swarm-tenant-{PERSONAL}-browser-wf-{WORKFLOW}"
    assert browser_login.login_secret_name(PERSONAL, "github-qa") == f"swarm-tenant-{PERSONAL}-browser-login-github-qa"
    with pytest.raises(ValueError):
        browser_login.carry_secret_name(GROUP, WORKFLOW)
    with pytest.raises(ValueError):
        browser_login.carry_secret_name(PERSONAL, "../other")
    # A workflow id that cannot form a secret id carries nothing rather than fail the step.
    assert plan_for(PERSONAL, task(workflow="has spaces")).carry_secret is None


# --------------------------------------------------------------------------
# Named logins
# --------------------------------------------------------------------------


def saved_login(vault: FakeVault, name: str, *, expires: datetime, cookie: dict[str, Any]) -> None:
    gate = Gate()
    signed_in(gate, cookie)
    c = carrier(vault, gate)
    plan = plan_for(PERSONAL, task(workflow=None))
    days = max(1, (expires - NOW).days)
    c.save_login(plan, name, expires_days=days, site="github.com")
    # Rewrite the stored expiry for the tests that need one in the past.
    secret = browser_login.login_secret_name(PERSONAL, name)
    envelope = json.loads(vault.secrets[secret][-1])
    envelope["expires_at"] = expires.isoformat()
    vault.secrets[secret][-1] = json.dumps(envelope)


def test_input_browser_login_loads_the_named_login_before_the_agent_runs():
    vault = FakeVault()
    cookie = fake_cookie("user_session", "github.com")
    saved_login(vault, "github-qa", expires=NOW + timedelta(days=10), cookie=cookie)

    records = Records()
    gate = Gate()
    plan = plan_for(PERSONAL, task(workflow=None, login="github-qa"))
    assert plan.login_name == "github-qa"
    assert carrier(vault, gate, records).before_agent(plan) == "login"
    assert gate.ops == ["session_import"]
    assert [c["value"] for c in gate.browser.cookies[None]] == [cookie["value"]]
    assert ("browser_login_used", {"name": "github-qa"}) in records.items


@pytest.mark.parametrize("case", ["unknown", "expired"])
def test_an_unknown_or_expired_login_fails_before_the_agent_runs(case):
    vault = FakeVault()
    if case == "expired":
        saved_login(vault, "github-qa", expires=NOW - timedelta(minutes=1), cookie=fake_cookie("s", "github.com"))
    gate = Gate()
    plan = plan_for(PERSONAL, task(workflow=None, login="github-qa"))
    with pytest.raises(BrowserLoginRefused) as caught:
        carrier(vault, gate).before_agent(plan)
    assert caught.value.cause == f"browser_login_{case}"
    # Nothing reached the browser.
    assert "session_import" not in gate.ops
    assert "Storage.setCookies" not in gate.browser.methods()


@pytest.mark.parametrize("bad", ["", "Has Upper", "a" * 80, 7, {"cookies": []}, "../x"])
def test_a_login_is_named_never_supplied(bad):
    """Invariant 10: a caller names a login; a value or an odd name is refused."""
    with pytest.raises(BrowserLoginRefused) as caught:
        plan_for(PERSONAL, task(workflow=None, login=bad))
    assert caught.value.cause == "browser_login_invalid"


def test_save_login_bounds_the_expiry_and_a_new_sign_in_refreshes_it():
    vault = FakeVault()
    plan = plan_for(PERSONAL, task(workflow=None))
    gate = Gate()
    c = carrier(vault, gate)
    with pytest.raises(BrowserLoginRefused):
        c.save_login(plan, "github-qa", expires_days=31, site="github.com")
    signed_in(gate, fake_cookie("s", "github.com"))
    c.save_login(plan, "github-qa", site="github.com")
    secret = browser_login.login_secret_name(PERSONAL, "github-qa")
    first = json.loads(vault.secrets[secret][-1])
    assert first["expires_at"] == (NOW + timedelta(days=14)).isoformat()
    assert vault.meta[secret]["expire_time"] == NOW + timedelta(days=14)

    # A later task uses it, the site asks for a sign-in anyway, the person
    # signs in: a new version, same expiry.
    records = Records()
    later = Gate()
    use = plan_for(PERSONAL, task(workflow=None, login="github-qa"))
    lc = carrier(vault, later, records)
    lc.before_agent(use)
    signed_in(later, fake_cookie("s2", "github.com"))
    assert lc.after_agent(use) == [secret]
    assert len(vault.secrets[secret]) == 2
    assert json.loads(vault.secrets[secret][-1])["expires_at"] == first["expires_at"]
    assert ("browser_login_refreshed", {"name": "github-qa"}) in records.items


def test_a_group_tenant_cannot_save_a_login():
    vault = FakeVault()
    gate = Gate()
    signed_in(gate, fake_cookie("s", "github.com"))
    with pytest.raises(BrowserLoginRefused) as caught:
        carrier(vault, gate).save_login(plan_for(GROUP, task(tenant=GROUP)), "github-qa")
    assert caught.value.cause == "browser_login_personal_only"
    assert vault.secrets == {}


# --------------------------------------------------------------------------
# The agent's CDP cannot read or plant cookies
# --------------------------------------------------------------------------

COOKIE_METHODS = [
    "Network.getCookies",
    "Network.getAllCookies",
    "Storage.getCookies",
    "Network.setCookie",
    "Network.setCookies",
    "Storage.setCookies",
    "Network.clearBrowserCookies",
    "Storage.clearDataForOrigin",
    "Storage.clearCookies",
    "Network.deleteCookies",
]


@pytest.mark.parametrize("method", COOKIE_METHODS)
def test_the_method_policy_refuses_the_agents_cookie_methods(method):
    paths = PolicyPaths(downloads_dir=Path("/run/downloads"), uploads_dir=Path("/run/uploads"))
    refusal = cdppolicy.check(method, {}, paths)
    assert refusal is not None, method
    assert method in cdppolicy.DENIED
    assert method not in cdppolicy.ALLOWED_METHODS


class _Link:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        pass


def test_through_the_real_proxy_the_agent_is_refused_while_the_gate_reads_on_its_pipe(tmp_path):
    """The same browser, one proxy: the agent's Storage.getCookies never reaches
    Chromium and is recorded by name, and the gate's own export still works."""

    async def body() -> None:
        browser = FakeBrowser()
        browser.cookies[None].append(fake_cookie("sid", "github.com"))
        reached: list[str] = []
        records = Records()
        proxy: CdpProxy

        async def to_chromium(message: dict[str, Any]) -> None:
            reached.append(message["method"])
            if message["method"] == "Target.attachToBrowserTarget":
                result: dict[str, Any] = {"sessionId": "browser-s1"}
            else:
                result = await browser.call(message["method"], message.get("params"), message.get("sessionId"))
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(proxy.on_chromium_message({"id": message["id"], "result": result}))
            )

        paths = PolicyPaths(downloads_dir=tmp_path / "downloads", uploads_dir=tmp_path / "uploads")
        proxy = CdpProxy(to_chromium, paths=paths, record=records)
        link = _Link()
        connection = await proxy.connect_agent(link)
        assert connection is not None
        for n, method in enumerate(("Storage.getCookies", "Network.getAllCookies", "Storage.setCookies"), start=1):
            await proxy.on_agent_message(connection, {"id": n, "method": method, "params": {}})
        assert [m.get("error") is not None for m in link.sent] == [True, True, True]
        assert reached == ["Target.attachToBrowserTarget"]
        assert [f for e, f in records.items if e == "cdp_method_refused"] == [
            {"method": "Storage.getCookies"}, {"method": "Network.getAllCookies"}, {"method": "Storage.setCookies"}
        ]

        carry = SessionCarry(proxy.call, record=records, agent_connected=lambda: True)
        carry.note_returned()
        jar = await carry.export()
        assert jar is not None and [c["name"] for c in jar["cookies"]] == ["sid"]
        assert "Storage.getCookies" in reached

    run(body)


# --------------------------------------------------------------------------
# No cookie value anywhere it could leak
# --------------------------------------------------------------------------


def test_no_cookie_value_reaches_a_log_a_record_an_error_or_the_filesystem(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.chdir(tmp_path)
    vault = FakeVault()
    records = Records()
    registered: list[str] = []
    cookie = fake_cookie("sid", "github.com")
    first, second = Gate(), Gate()
    plan = plan_for(PERSONAL, task())

    one = carrier(vault, first, records, register_secret=registered.append)
    one.before_agent(plan)
    signed_in(first, cookie, origin="https://github.com")
    one.after_agent(plan)
    two = carrier(vault, second, records, register_secret=registered.append)
    two.before_agent(plan)
    storage_value = first.browser.storage["https://github.com"]["token-cache"]

    # The worker registers what it carried, so the logger and the output
    # scrub redact a value an agent prints from document.cookie.
    assert cookie["value"] in registered and storage_value in registered

    # A login that fails says so by name only.
    with pytest.raises(BrowserLoginRefused) as caught:
        carrier(vault, Gate(), records).before_agent(plan_for(PERSONAL, task(login="nope")))

    haystacks = [
        caplog.text,
        records.text(),
        first.records.text(),
        second.records.text(),
        str(caught.value),
        repr(plan),
    ]
    for value in (cookie["value"], storage_value):
        for text in haystacks:
            assert value not in text
    # Nothing was written under the working directory: the jar is never a file,
    # so neither the checkpoint nor the outputs can pick it up.
    assert list(tmp_path.iterdir()) == []
    # And the only place it went is the tenant's own secret.
    assert all(name.startswith(f"swarm-tenant-{PERSONAL}-browser-") for name in vault.secrets)


def test_a_carry_the_gate_cannot_import_fails_open_and_a_login_it_cannot_import_fails_closed():
    vault = FakeVault()
    plan = plan_for(PERSONAL, task())
    first = Gate()
    signed_in(first, fake_cookie("sid", "github.com"))
    carrier(vault, first).after_agent(plan)
    saved_login(vault, "github-qa", expires=NOW + timedelta(days=3), cookie=fake_cookie("s", "github.com"))

    class Broken(Gate):
        def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
            raise browser_login.GateUnavailable("channel closed")

    records = Records()
    # The carry: this step signs in fresh, and says why.
    assert carrier(vault, Broken(), records).before_agent(plan) == "none"
    assert ("browser_session_carry_failed", {"cause": "gate_unavailable"}) in records.items
    # The login: the task asked for it by name, so it fails before the agent runs.
    with pytest.raises(BrowserLoginRefused) as caught:
        carrier(vault, Broken(), records).before_agent(plan_for(PERSONAL, task(workflow=None, login="github-qa")))
    assert caught.value.cause == "browser_login_unavailable"
