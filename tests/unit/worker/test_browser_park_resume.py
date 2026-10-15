"""A resumed live-browser attempt gets back the parked attempt's tabs and site storage (issue #1362).

docs/design/live-browser.md §5.5.2. MEASURED 2026-10-15 (QA batch full-1015,
S5-resume, 6/6 failed: 3 Playwright MCP, 3 dev-browser): part one opened
https://example.com/?qa=<nonce>, set localStorage.qa_state, asked for a
hand-off and parked; attempt 2 resumed from ckpt-00001 and its browser had
only about:blank.

These tests drive the real gate modules (`livegate.tabs.TabKeeper`,
`livegate.session.SessionCarry`) against a fake Chromium that keeps pages,
cookies, per-origin localStorage and new-document scripts the way Chromium's
pipe does, and the real worker half (`HandoffPath`, `browser_resume`) against
a gate channel that forwards each ctl op, JSON-encoded as ctl.sock carries it,
to those gate modules. No test runs Chromium, a network or a credential; every
cookie and storage value is built at run time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import browser_login, browser_resume
from agent_worker import handoff as handoff_mod
from agent_worker import children as children_mod
from agent_worker.hardening import PROTECTED, MemoryProtection
from agent_worker.livegate import ctl as ctl_mod
from agent_worker.livegate import tabs as tabs_mod
from agent_worker.livegate.session import SessionCarry
from agent_worker.livegate.tabs import TabKeeper, TabsRefused, refusal

PERSONAL = "u-bogdan-example-com"
GROUP = "eng"
TASK = "task_s5resume"
CTL = "/run/swarm-gate/ctl.sock"


def value() -> str:
    # Built at run time: nothing here may look like a credential in the diff.
    return "v" + secrets.token_hex(20)


def nonce() -> str:
    return secrets.token_hex(8)


# --------------------------------------------------------------------------
# A fake Chromium: pages, cookies, localStorage, new-document scripts
# --------------------------------------------------------------------------

_ITEMS = re.compile(r"var origin = (\"[^\"]*\"); var items = (\[.*?\]);")


def _origin_of(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "null"
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{rest.split('/', 1)[0].split('?', 1)[0].split('#', 1)[0]}"


class FakeChromium:
    """The gate's CDP calls answered as Chromium's pipe would, for what a park and a resume use.

    A navigation creates a document: every new-document script the page's
    attached sessions added runs then (the storage script is read for its
    origin and items and applied when the origin matches), and only after that
    the page's own `on_load` hook -- the page's scripts -- sees localStorage.
    Detaching a session drops the scripts it added, as Chromium does.
    """

    def __init__(self) -> None:
        self.pages: list[dict[str, Any]] = [{"targetId": "blank-0", "type": "page", "url": "about:blank"}]
        self.cookies: list[dict[str, Any]] = []
        self.storage: dict[str, dict[str, str]] = {}
        self.active: str | None = "blank-0"
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.sessions: dict[str, str] = {}
        self.scripts: dict[str, list[tuple[str, str | None]]] = {}
        #: What each page's own scripts saw in localStorage on load, by target.
        self.seen_on_load: dict[str, dict[str, str]] = {}
        self._n = 0

    def _page(self, target: str) -> dict[str, Any]:
        return next(p for p in self.pages if p["targetId"] == target)

    def _navigate(self, target: str, url: str) -> None:
        page = self._page(target)
        page["url"] = url
        origin = _origin_of(url)
        for session, attached_to in self.sessions.items():
            if attached_to != target:
                continue
            for source, world in self.scripts.get(session, []):
                assert world == tabs_mod.RESTORE_WORLD, "the storage script runs in the gate's isolated world"
                match = _ITEMS.search(source)
                assert match, source
                if json.loads(match.group(1)) == origin:
                    for k, v in json.loads(match.group(2)):
                        self.storage.setdefault(origin, {})[k] = v
        self.seen_on_load[target] = dict(self.storage.get(origin, {}))

    async def call(self, method: str, params: dict[str, Any] | None = None, session: str | None = None) -> dict[str, Any]:
        params = dict(params or {})
        self.calls.append((method, params, session))
        if method == "Target.getTargets":
            return {"targetInfos": [dict(p) for p in self.pages]}
        if method == "Target.getBrowserContexts":
            return {"browserContextIds": []}
        if method == "Target.createTarget":
            self._n += 1
            target = f"tab-{self._n}"
            self.pages.append({"targetId": target, "type": "page", "url": "about:blank"})
            if params.get("background") is not True:
                self.active = target
            if params["url"] != "about:blank":
                self._navigate(target, params["url"])
            return {"targetId": target}
        if method == "Target.closeTarget":
            self.pages = [p for p in self.pages if p["targetId"] != params["targetId"]]
            return {"success": True}
        if method == "Target.activateTarget":
            self._page(params["targetId"])
            self.active = params["targetId"]
            return {}
        if method == "Target.attachToTarget":
            assert params.get("flatten") is True
            self._n += 1
            session = f"s-{self._n}"
            self.sessions[session] = params["targetId"]
            return {"sessionId": session}
        if method == "Target.detachFromTarget":
            self.sessions.pop(params["sessionId"], None)
            self.scripts.pop(params["sessionId"], None)
            return {}
        if method in ("Page.enable", "DOMStorage.enable"):
            assert session in self.sessions
            return {}
        if method == "Page.addScriptToEvaluateOnNewDocument":
            assert session in self.sessions
            self.scripts.setdefault(session, []).append((params["source"], params.get("worldName")))
            return {"identifier": "1"}
        if method == "Page.navigate":
            self._navigate(self.sessions[session], params["url"])
            return {"frameId": self.sessions[session]}
        if method == "Runtime.evaluate":
            assert params["expression"] == "location.origin"
            return {"result": {"type": "string", "value": _origin_of(self._page(self.sessions[session])["url"])}}
        if method == "Storage.getCookies":
            return {"cookies": [dict(c) for c in self.cookies]}
        if method == "Storage.setCookies":
            self.cookies.extend(dict(c) for c in params["cookies"])
            return {}
        if method == "DOMStorage.getDOMStorageItems":
            origin = params["storageId"]["securityOrigin"]
            # Chromium reads an origin's storage only through a frame of it.
            assert _origin_of(self._page(self.sessions[session])["url"]) == origin
            return {"entries": [[k, v] for k, v in self.storage.get(origin, {}).items()]}
        raise AssertionError(f"unexpected CDP call {method}")

    def tab_urls(self) -> list[str]:
        return [p["url"] for p in self.pages]


class Records:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, fields: Any) -> None:
        self.items.append((event, dict(fields)))

    def names(self) -> list[str]:
        return [e for e, _ in self.items]


class Pod:
    """One pod's gate: a FakeChromium behind the real SessionCarry and TabKeeper,
    reached as the worker reaches it, through ctl.sock's JSON."""

    def __init__(self) -> None:
        self.chromium = FakeChromium()
        self.records = Records()
        self.agent_connected = False
        self.shown: str | None = None
        connected = lambda: self.agent_connected  # noqa: E731
        self.carry = SessionCarry(self.chromium.call, record=self.records, agent_connected=connected)

        async def no_sleep(_: float) -> None:
            return None

        self.tabs = TabKeeper(self.chromium.call, record=self.records, agent_connected=connected,
                              shown=lambda: self.shown, sleep=no_sleep)
        self.ops: list[tuple[str, dict[str, Any]]] = []
        self.registered = False

    # ctl.sock's client surface, as handoff.Gate drives it
    def register(self) -> None:
        self.registered = True

    def close(self) -> None:
        pass

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        assert op in ctl_mod.OPS, op
        wire = json.loads(json.dumps(args))
        self.ops.append((op, wire))
        if op in tabs_mod.OPS:
            result = asyncio.run(self.tabs.handle(op, wire))
        elif op in ("session_export", "session_import"):
            result = asyncio.run(self.carry.handle(op, wire))
        elif op == "end_control":
            result = {"ended": True, "wiped": bool(wire.get("wipe"))}
        else:
            result = {}
        return json.loads(json.dumps(result))

    def op_names(self) -> list[str]:
        return [op for op, _ in self.ops]


def part_one(pod: Pod, qa: str, state: str, cookie: str) -> None:
    """S5 part one: the agent opened example.com/?qa=<nonce>, set localStorage.qa_state, and a cookie came with it."""
    chromium = pod.chromium
    chromium.pages = [
        {"targetId": "t-docs", "type": "page", "url": "https://docs.example.org/guide#setup"},
        {"targetId": "t-qa", "type": "page", "url": f"https://example.com/?qa={qa}"},
        {"targetId": "t-captcha", "type": "page", "url": "https://www.google.com/recaptcha/api2/anchor?k=x"},
        {"targetId": "t-blank", "type": "page", "url": "about:blank"},
    ]
    chromium.storage["https://example.com"] = {"qa_state": state}
    chromium.cookies.append({"name": "sid", "value": cookie, "domain": "example.com", "path": "/",
                             "secure": True, "httpOnly": True, "session": True, "expires": -1})
    pod.shown = "t-qa"


# --------------------------------------------------------------------------
# The gate: park, then resume, round-trips the tabs and a localStorage value
# --------------------------------------------------------------------------


def test_a_park_then_resume_round_trips_the_tab_list_and_a_local_storage_value(caplog):
    caplog.set_level(logging.DEBUG)
    qa, state, cookie = nonce(), value(), value()
    parked = Pod()
    part_one(parked, qa, state, cookie)

    # At park, before the wipe. No hand-off returned: a park's export needs none.
    tabs = parked.call("tabs_export", {})["tabs"]
    jar = parked.call("session_export", {"scope": "park"})["jar"]
    assert tabs == [
        {"url": "https://docs.example.org/guide#setup", "active": False},
        {"url": f"https://example.com/?qa={qa}", "active": True},
    ], "in order, the shown tab active; the challenge page and the blank are not kept"
    assert jar["origins"] == [{"origin": "https://example.com", "localStorage": [["qa_state", state]]}]

    # The resumed attempt: a new pod, a fresh Chromium on about:blank.
    resumed = Pod()
    assert resumed.chromium.tab_urls() == ["about:blank"]
    resumed.call("session_import", {"jar": {**jar, "origins": []}})
    answer = resumed.call("tabs_restore", {"tabs": tabs, "origins": jar["origins"]})

    chromium = resumed.chromium
    assert answer == {"opened": [t["url"] for t in tabs], "storage_origins": 1}
    assert chromium.tab_urls() == [t["url"] for t in tabs], "reopened in order, the blank closed"
    assert chromium._page(chromium.active)["url"] == f"https://example.com/?qa={qa}", "the active tab is in front"
    assert chromium.storage["https://example.com"] == {"qa_state": state}
    qa_target = next(p["targetId"] for p in chromium.pages if p["url"].startswith("https://example.com/"))
    assert chromium.seen_on_load[qa_target] == {"qa_state": state}, \
        "the page's own scripts see the value on their first load"
    assert [c["value"] for c in chromium.cookies] == [cookie]
    # The storage script went with the gate's session, which is gone: a later
    # navigation of the agent's is untouched.
    assert chromium.sessions == {} and chromium.scripts == {}
    assert "browser_tabs_restored" in resumed.records.names()


def test_without_the_park_scope_an_export_still_needs_a_returned_hand_off():
    pod = Pod()
    part_one(pod, nonce(), value(), value())
    assert pod.call("session_export", {})["jar"] is None


def test_the_restore_is_refused_once_an_agent_has_connected():
    pod = Pod()
    pod.agent_connected = True
    with pytest.raises(TabsRefused):
        asyncio.run(pod.tabs.restore([{"url": "https://example.com/", "active": True}], []))
    assert pod.chromium.tab_urls() == ["about:blank"]
    assert pod.records.names() == ["browser_tabs_restore_refused"]


def test_a_tab_whose_document_never_reaches_its_origin_opens_without_its_storage():
    pod = Pod()
    clock = iter(range(0, 1000))
    pod.tabs._clock = lambda: float(next(clock))
    pod.tabs._budget = 3.0
    chromium = pod.chromium

    real = chromium.call

    async def slow(method, params=None, session=None):
        if method == "Page.navigate":  # the document has not been created yet
            return {"frameId": chromium.sessions[session]}
        return await real(method, params, session)

    pod.tabs._call = slow
    answer = asyncio.run(pod.tabs.restore(
        [{"url": "https://example.com/", "active": True}],
        [{"origin": "https://example.com", "localStorage": [["qa_state", value()]]}],
    ))
    assert answer["storage_origins"] == 0
    assert chromium.sessions == {} and chromium.scripts == {}, "the script is gone with the session"


@pytest.mark.parametrize("url, cause", [
    ("https://www.google.com/recaptcha/api2/anchor", "challenge_host"),
    ("https://challenges.cloudflare.com/turnstile", "challenge_host"),
    ("https://newassets.hcaptcha.com/captcha/v1", "challenge_host"),
    ("https://example.com/cb?code=abc&state=xyz", "credential_param"),
    ("https://example.com/#access_token=abc", "credential_param"),
    ("https://user:pw@example.com/", "userinfo"),
    ("file:///etc/passwd", "scheme"),
    ("chrome://settings", "scheme"),
    ("https://example.com/" + "a" * 3000, "too_long"),
])
def test_a_challenge_page_or_a_credential_carrying_url_is_never_kept(url, cause):
    assert refusal(url) == cause
    assert refusal(f"https://example.com/?qa={nonce()}") is None
    assert refusal("https://app.example.com/#/dashboard") is None


# --------------------------------------------------------------------------
# The worker: HandoffPath keeps the browser at park and puts it back on resume
# --------------------------------------------------------------------------


class FakeVault:
    def __init__(self) -> None:
        self.secrets: dict[str, list[str]] = {}
        self.provisioned: list[str] = []

    def read(self, name: str) -> str | None:
        versions = self.secrets.get(name)
        return versions[-1] if versions else None

    def write(self, name: str, payload: str) -> None:
        assert name in self.provisioned, "written before swarm-api provisioned the slot"
        self.secrets.setdefault(name, []).append(payload)


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict[str, Any]]] = []
        self.registered: list[str] = []

    def info(self, msg, **kw):
        self.lines.append((msg, kw))

    warning = error = info

    def register_secret(self, secret: str | None) -> None:
        if secret:
            self.registered.append(secret)


class SlotRoutes:
    """swarm-api's slot route, as `SlotApi` calls it: records the secret id."""

    def __init__(self, vault: FakeVault) -> None:
        self.vault = vault
        self.asked: list[str] = []

    def call(self, method, path, *, body=None, headers=None):
        payload = json.loads(body)
        assert path == f"/v1/tasks/{TASK}/browser-session/slot"
        assert set(headers) == {children_mod.PROOF_HEADER, children_mod.TIMESTAMP_HEADER}
        self.asked.append(payload["secret_id"])
        self.vault.provisioned.append(payload["secret_id"])
        return 200, {"secret_id": payload["secret_id"]}


def handoff_path(pod: Pod, vault: FakeVault, tenant: str, attempt: str, generation: int,
                 log: _Log | None = None) -> tuple[handoff_mod.HandoffPath, SlotRoutes]:
    routes = SlotRoutes(vault)
    cfg = SimpleNamespace(tenant_id=tenant, task_id=TASK, attempt_id=attempt, lease_id=f"lease_{generation}",
                          generation=generation, runner_profile="mock")
    path = handoff_mod.HandoffPath(
        cfg, logger=log or _Log(), memory=MemoryProtection(PROTECTED, "test"),
        children=SimpleNamespace(_key=children_mod.AttemptKey(), _api=routes, unavailable=None),
        live=True, gate_client=lambda p: pod, environ={"SWARM_GATE_CTL": CTL}, vault=lambda: vault,
    )
    # The gate's registration only: the tunnel's routes are test_handoff's.
    path.gate = handoff_mod.Gate(pod)
    path.gate.register()
    path.unavailable = None
    return path, routes


def park(tmp_path: Path, tenant: str, log: _Log | None = None) -> tuple[Pod, FakeVault, Path, str, str, str]:
    qa, state, cookie = nonce(), value(), value()
    pod, vault = Pod(), FakeVault()
    part_one(pod, qa, state, cookie)
    path, routes = handoff_path(pod, vault, tenant, "att_1", 1, log)
    work = tmp_path / "work"
    work.mkdir()
    path.prepare(work)
    path.active = {"request_id": "s5-signin", "resolved": True}
    path._route = lambda *a, **k: (200, {"resolved": True})
    path.end(work, outcome="expired", cause=handoff_mod.HOLD_EXPIRED)
    return pod, vault, work, qa, state, cookie


def test_at_park_the_tabs_go_to_the_spool_and_the_storage_to_the_park_secret_before_the_wipe(tmp_path):
    log = _Log()
    pod, vault, work, qa, state, cookie = park(tmp_path, PERSONAL, log)
    names = pod.op_names()
    assert names.index("tabs_export") < names.index("end_control")
    assert names.index("session_export") < names.index("end_control")
    assert dict(pod.ops)["session_export"] == {"scope": "park"}

    kept = json.loads((work / handoff_mod.SPOOL_DIR_NAME / browser_resume.TABS_NAME).read_text())
    assert kept["attempt_id"] == "att_1" and kept["storage"] is True
    assert [t["url"] for t in kept["tabs"]][-1] == f"https://example.com/?qa={qa}"

    secret = browser_login.park_secret_name(PERSONAL, TASK)
    assert secret == f"swarm-tenant-{PERSONAL}-browser-park-{TASK}"
    envelope = json.loads(vault.read(secret))
    assert envelope["kind"] == "park"
    assert envelope["jar"]["origins"][0]["localStorage"] == [["qa_state", state]]
    # Cookies and storage never go into the checkpoint (§5.5): only into the secret.
    spool_text = "".join(p.read_text() for p in (work / handoff_mod.SPOOL_DIR_NAME).rglob("*") if p.is_file())
    assert cookie not in spool_text and state not in spool_text
    assert cookie in log.registered and state in log.registered, "carried values join the redaction set"


def test_a_group_tenant_keeps_its_tabs_and_no_storage(tmp_path):
    pod, vault, work, qa, _, _ = park(tmp_path, GROUP)
    assert "session_export" not in pod.op_names()
    assert vault.secrets == {}
    kept = json.loads((work / handoff_mod.SPOOL_DIR_NAME / browser_resume.TABS_NAME).read_text())
    assert kept["storage"] is False and kept["tabs"][-1]["url"] == f"https://example.com/?qa={qa}"


def test_the_resumed_attempt_reopens_the_tabs_with_their_storage_and_names_them_in_its_summary(tmp_path):
    pod, vault, work, qa, state, cookie = park(tmp_path, PERSONAL)
    resumed = Pod()
    path, _ = handoff_path(resumed, vault, PERSONAL, "att_2", 2)
    path.prepare(work)

    names = resumed.op_names()
    assert names == ["session_import", "tabs_restore"], names
    imported = dict(resumed.ops)["session_import"]["jar"]
    assert imported["origins"] == [], "origin storage goes with its tab, not onto the blank page"
    chromium = resumed.chromium
    assert chromium.tab_urls() == ["https://docs.example.org/guide#setup", f"https://example.com/?qa={qa}"]
    assert chromium._page(chromium.active)["url"] == f"https://example.com/?qa={qa}"
    assert chromium.storage["https://example.com"] == {"qa_state": state}
    assert [c["value"] for c in chromium.cookies] == [cookie]

    root = work / handoff_mod.SPOOL_DIR_NAME
    summary = (root / handoff_mod.RESUME_NAME).read_text()
    assert f"2. https://example.com/?qa={qa} (FRONT)" in summary
    assert "1. https://docs.example.org/guide#setup" in summary
    assert "site storage (cookies, localStorage) were put back" in summary
    assert handoff_mod.ARTIFACTS_NOTE in summary
    assert cookie not in summary and state not in summary
    # Read once: a later retry does not reopen a park it did not make.
    assert not (root / browser_resume.TABS_NAME).exists()


def test_a_group_tenants_resume_reopens_the_tabs_and_says_the_storage_is_gone(tmp_path):
    _, vault, work, qa, _, _ = park(tmp_path, GROUP)
    resumed = Pod()
    path, _ = handoff_path(resumed, vault, GROUP, "att_2", 2)
    path.prepare(work)
    assert resumed.op_names() == ["tabs_restore"]
    summary = (work / handoff_mod.SPOOL_DIR_NAME / handoff_mod.RESUME_NAME).read_text()
    assert f"https://example.com/?qa={qa} (FRONT)" in summary
    assert "sign-ins and site storage are gone" in summary


def test_a_tab_list_planted_in_the_spool_is_filtered_again_before_it_reaches_the_gate(tmp_path):
    pod = Pod()
    path, _ = handoff_path(pod, FakeVault(), GROUP, "att_2", 2)
    work = tmp_path / "work"
    root = work / handoff_mod.SPOOL_DIR_NAME
    root.mkdir(parents=True)
    (root / browser_resume.TABS_NAME).write_text(json.dumps({
        "v": 1, "attempt_id": "att_1", "storage": True,
        "tabs": [{"url": "file:///etc/shadow", "active": True},
                 {"url": "https://challenges.cloudflare.com/x"},
                 {"url": "https://example.com/ok", "active": True}],
    }))
    path.prepare(work)
    (op, args), = pod.ops
    assert op == "tabs_restore"
    assert args["tabs"] == [{"url": "https://example.com/ok", "active": True}]
    assert args["origins"] == [], "a group tenant has no park secret, whatever the file says"


def test_nothing_in_the_capture_or_the_restore_logs_a_cookie_or_storage_value(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    log = _Log()
    pod, vault, work, _, state, cookie = park(tmp_path, PERSONAL, log)
    resumed = Pod()
    path, _ = handoff_path(resumed, vault, PERSONAL, "att_2", 2, log)
    path.prepare(work)
    text = json.dumps(log.lines, default=str) + caplog.text
    text += json.dumps(pod.records.items + resumed.records.items, default=str)
    assert cookie not in text and state not in text
    assert "sid" not in json.dumps(pod.records.items + resumed.records.items)


def test_the_worker_may_send_every_tabs_and_session_op_and_the_gate_accepts_each():
    for op in ("tabs_export", "tabs_restore", "session_export", "session_import"):
        assert op in handoff_mod.GATE_OPS and op in ctl_mod.OPS


# --------------------------------------------------------------------------
# The lifecycle: the tabs are back before the resumed attempt's runner starts
# --------------------------------------------------------------------------

from test_handoff import WAIT, FakeGateClient, FakeLiveRoutes, _live_worker  # noqa: E402
from test_handoff import agent, trace  # noqa: E402,F401  (pytest fixtures, used by name)
from worker_seeds import seed_attempt  # noqa: E402

S5 = ["request", "--reason", "S5: hand off, then park", "--id", "s5-1"]


class TabGate(FakeGateClient):
    """test_handoff's gate, whose browser has the S5 tab open, and which notes
    whether the agent had started when the tabs were restored."""

    def __init__(self, trace: list[str], url: str, started: Any = None) -> None:
        super().__init__(trace)
        self.url = url
        self.started = started
        self.restored_before_agent: bool | None = None
        self.restored: list[dict[str, Any]] = []

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        super().call(op, args)
        if op == "tabs_export":
            return {"tabs": [{"url": self.url, "active": True}]}
        if op == "tabs_restore":
            self.restored = list(args["tabs"])
            self.restored_before_agent = not self.started()
            return {"opened": [t["url"] for t in args["tabs"]], "storage_origins": 0}
        return {}


def test_the_resumed_launch_reopens_the_recorded_tabs_before_the_runner_starts(
    db, worker_factory, agent, trace, monkeypatch
):
    from agent_worker.errors import ExitCode

    write_plan, records = agent
    url = f"https://example.com/?qa={nonce()}"
    write_plan(S5, WAIT)
    seed_attempt(db)
    first = _live_worker(worker_factory, FakeLiveRoutes(db, trace, hold_seconds=1), TabGate(trace, url),
                         trace, monkeypatch)
    assert first.run() == ExitCode.PARKED
    assert trace.index("gate:tabs_export") < trace.index("gate:end_control:wipe") < trace.index("checkpoint:human-wait")

    task = db.doc("tasks/task_1")
    checkpoint, metadata = task["latest_checkpoint"], dict(task["metadata"])
    write_plan()
    seed_attempt(db, attempt_id="att_2", lease_id="lease_2", generation=2, attempt_count=1,
                 latest_checkpoint=checkpoint)
    db.doc("tasks/task_1")["metadata"] = metadata
    gate = TabGate(trace, url)
    second = _live_worker(worker_factory, FakeLiveRoutes(db, trace), gate, trace, monkeypatch,
                          attempt_id="att_2", lease_id="lease_2", generation=2)
    # The runner's child process, which the lifecycle holds once it has spawned the agent.
    gate.started = lambda: second._child is not None
    assert second.run() == ExitCode.OK
    assert gate.restored == [{"url": url, "active": True}]
    assert gate.restored_before_agent is True, "the tabs must be open before the agent's runner starts"
    resumed = records()[-1]["resumed"]
    assert f"1. {url} (FRONT)" in resumed, resumed
