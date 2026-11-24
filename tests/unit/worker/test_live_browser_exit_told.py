"""Why the live browser's Chromium exited reaches the worker and the agent (part of issue 272).

MEASURED 2026-11-23: two review tasks on the browser runner profile lost the
hosted live browser ("connect_over_cdp: Connection closed"). The gate had
already started Chromium again or given up (BROWSER-RELAUNCH), but the exit's
cause stayed in the sidecar's log, the agent saw only its CDP connection
close, and the task record never said the browser died.

Now:

* the gate keeps its last unexpected exit -- cause, exit code, signal, when,
  and how far the sidecar's cgroup `oom_kill` counter rose -- in `status`
  (`browser_exit`);
* the worker writes what that status says to `$SWARM_HANDOFF/live-browser.json`
  (`running` / `restarting` / `crashed`), keeps one note per restart or loss
  for the task's events, and `browser_gone` once it will not come back;
* `swarm_browser.live()` waits out a `restarting` browser and otherwise raises
  with the file's message.

WHY `live-browser.json` and not `browser.json`: `$SWARM_HANDOFF/browser.json` is
already the runner's file (browser_tools.STATE_NAME, BSTATUS-API), which the
worker forwards to the gate on every poll; one file with two writers would
lose one of them.

The gate's tests drive the real `LiveGate` against the scripted Chromiums of
test_live_browser_relaunch; the worker's drive the real `HandoffPath` over
test_handoff's scripted gate client. No Chromium, no cloud, no emulator.
"""

from __future__ import annotations

import asyncio
import functools
import importlib.machinery
import importlib.util
import json
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest
from test_handoff import FakeGateClient, trace  # noqa: F401 -- `trace` is a fixture
from test_handoff_gate_reason import Clock, CtlTimeout, _path
from test_live_browser_relaunch import SIGSEGV, Rig, _until

from agent_worker import handoff as handoff_mod
from agent_worker.livegate import __main__ as gate_main

REPO = Path(__file__).resolve().parents[3]
IMAGE = REPO / "images" / "agent-runtime-browser"
SIGKILL = -9


# -- the gate -----------------------------------------------------------------


class OomCounter:
    """The sidecar's cgroup `oom_kill` count, as the test sets it."""

    def __init__(self, value: int | None) -> None:
        self.value = value

    def __call__(self) -> int | None:
        return self.value


def _gate_run(body: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oom: OomCounter, **kw: Any) -> None:
    monkeypatch.setattr(gate_main, "Deps", functools.partial(gate_main.Deps, oom_kills=oom))

    async def main() -> None:
        rig = Rig(tmp_path, **kw)
        try:
            await rig.start()
            await body(rig)
        finally:
            await rig.stop()

    asyncio.run(asyncio.wait_for(main(), 20))


def test_status_has_no_browser_exit_before_chromium_exits(tmp_path, monkeypatch):
    async def body(rig: Rig) -> None:
        status = rig.gate.status()
        assert "browser_exit" in status and status["browser_exit"] is None

    _gate_run(body, tmp_path, monkeypatch, OomCounter(2))


def test_an_oom_kill_is_reported_with_its_signal_and_the_counters_rise(tmp_path, monkeypatch):
    oom = OomCounter(2)  # the cgroup had two kills before the gate started: not this gate's

    async def body(rig: Rig) -> None:
        before = time.time()
        oom.value = 3
        await rig.crash("[1:1:FATAL:memory.cc(22)] Out of memory https://user:pw@example.test/x?token=t",
                        code=SIGKILL)
        await _until(rig.showing)
        status = rig.gate.status()
        exit_ = status["browser_exit"]
        assert exit_["exit_cause"] == "signal_SIGKILL"
        assert exit_["signal"] == "SIGKILL" and exit_["exit_code"] == SIGKILL
        assert exit_["oom_kills"] == 1
        assert before - 1 <= exit_["at"] <= time.time() + 1
        assert status["browser"] == "running" and status["browser_restarts"] == 1
        # Never Chromium's stderr, nor anything from it, in `status`.
        text = json.dumps(status)
        assert "memory.cc" not in text and "example.test" not in text and "user:pw" not in text
        # The record and the gate's log line carry the counter too.
        assert rig.recorded("browser_exited")[0]["oom_kills"] == 1

    _gate_run(body, tmp_path, monkeypatch, oom)


def test_an_unreadable_counter_is_none_and_a_plain_exit_has_no_signal(tmp_path, monkeypatch):
    async def body(rig: Rig) -> None:
        await rig.crash(code=21)
        await _until(rig.showing)
        exit_ = rig.gate.status()["browser_exit"]
        assert exit_["exit_cause"] == "exit_code_21" and exit_["exit_code"] == 21
        assert exit_["signal"] is None and exit_["oom_kills"] is None

    _gate_run(body, tmp_path, monkeypatch, OomCounter(None))


def test_the_last_exit_stays_when_the_relaunch_budget_is_spent(tmp_path, monkeypatch):
    async def body(rig: Rig) -> None:
        await rig.crash(code=SIGSEGV)
        await _until(rig.showing)
        await rig.crash(code=SIGKILL)
        await _until(lambda: rig.gate.status()["browser"] == "closed")
        status = rig.gate.status()
        assert status["browser_exit"]["signal"] == "SIGKILL"

    _gate_run(body, tmp_path, monkeypatch, OomCounter(0), limit=1)


def test_a_wipe_is_not_an_exit_to_report(tmp_path, monkeypatch):
    async def body(rig: Rig) -> None:
        await rig.gate.wipe("attempt_ended")
        await _until(lambda: rig.gate.browser_closed)
        await asyncio.sleep(0.1)
        assert rig.gate.status()["browser_exit"] is None

    _gate_run(body, tmp_path, monkeypatch, OomCounter(0))


def test_the_default_reader_reads_the_cgroup_counter(tmp_path, monkeypatch):
    from agent_worker import metrics

    (tmp_path / "memory.events").write_text("low 0\nhigh 0\nmax 4\noom 2\noom_kill 2\n")
    monkeypatch.setattr(metrics, "CGROUP_ROOT", tmp_path)
    assert gate_main.read_oom_kills() == 2
    (tmp_path / "memory.events").unlink()
    assert gate_main.read_oom_kills() is None


# -- the worker ----------------------------------------------------------------


class ScriptedGate(FakeGateClient):
    """A gate whose `status` answers what the test last set."""

    def __init__(self, trace: list[str]) -> None:
        super().__init__(trace)
        self.status: dict[str, Any] = {"browser": "running", "browser_restarts": 0, "browser_exit": None}

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        result = super().call(op, args)
        if op == "status":
            return json.loads(json.dumps(self.status))
        return result


EXIT = {"exit_cause": "signal_SIGKILL", "exit_code": -9, "signal": "SIGKILL", "oom_kills": 1, "at": 1_790_000_000.5}


def _state(work: Path) -> dict[str, Any]:
    return json.loads((work / handoff_mod.SPOOL_DIR_NAME / "live-browser.json").read_text())


def test_the_file_goes_restarting_then_running_with_the_restart_counted_then_crashed(tmp_path, db, trace):
    clock = Clock()
    gate = ScriptedGate(trace)
    path, work = _path(tmp_path, db, trace, gate, clock)
    tick = functools.partial(path.tick, work, lambda: db.doc("tasks/task_1"))

    assert tick() is None
    first = _state(work)
    assert first["state"] == "running" and first["restarts"] == 0 and first["last_exit"] is None
    assert path.drain_browser_notes() == [] and path.browser_gone is None

    gate.status = {"browser": "restarting", "browser_restarts": 0, "browser_exit": EXIT}
    assert tick() is None
    restarting = _state(work)
    assert restarting["state"] == "restarting" and restarting["restarts"] == 0
    assert restarting["last_exit"] == {"cause": "signal_SIGKILL", "signal": "SIGKILL", "exit_code": -9,
                                       "oom_kills": 1, "at": "2026-09-21T14:13:20.500000+00:00"}
    assert "restarting" in restarting["message"] and "SIGKILL" in restarting["message"]

    gate.status = {"browser": "running", "browser_restarts": 1, "browser_exit": EXIT}
    assert tick() is None
    running = _state(work)
    assert running["state"] == "running" and running["restarts"] == 1
    assert running["last_exit"]["cause"] == "signal_SIGKILL"
    notes = path.drain_browser_notes()
    assert [n["event"] for n in notes] == ["browser_restarted"]
    assert notes[0]["restarts"] == 1 and notes[0]["last_exit"]["oom_kills"] == 1
    assert path.drain_browser_notes() == [], "a note is handed over once"
    assert path.browser_gone is None

    gate.status = {"browser": "closed", "browser_restarts": 1, "browser_exit": EXIT}
    assert tick() is None, "the tick returns what it returned before: a lost browser parks nobody"
    crashed = _state(work)
    assert crashed["state"] == "crashed" and crashed["restarts"] == 1
    assert "gone" in crashed["message"]
    assert path.browser_gone == crashed
    gone = path.drain_browser_notes()
    assert [n["event"] for n in gone] == ["browser_gone"] and gone[0]["state"] == "crashed"


def test_the_file_is_written_only_when_it_changes(tmp_path, db, trace):
    clock = Clock()
    gate = ScriptedGate(trace)
    path, work = _path(tmp_path, db, trace, gate, clock)
    path.tick(work, lambda: db.doc("tasks/task_1"))
    target = work / handoff_mod.SPOOL_DIR_NAME / "live-browser.json"
    target.write_text("{}")  # had it been written again, this would be gone
    path.tick(work, lambda: db.doc("tasks/task_1"))
    assert target.read_text() == "{}"


def test_a_lost_gate_channel_is_crashed_and_a_timed_out_status_is_not(tmp_path, db, trace):
    clock = Clock()

    class Slow(ScriptedGate):
        def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
            if op == "status":
                raise CtlTimeout("the gate did not answer within 10s")
            return super().call(op, args)

    path, work = _path(tmp_path, db, trace, Slow(trace), clock)
    assert path.tick(work, lambda: db.doc("tasks/task_1")) is None
    assert path.browser_gone is None
    assert not (work / handoff_mod.SPOOL_DIR_NAME / "live-browser.json").exists()

    (tmp_path / "again").mkdir()
    gate = ScriptedGate(trace)
    path, work = _path(tmp_path / "again", db, trace, gate, clock)
    path.tick(work, lambda: db.doc("tasks/task_1"))
    gate.lose_on = "status"
    assert path.tick(work, lambda: db.doc("tasks/task_1")) is None
    lost = _state(work)
    assert lost["state"] == "crashed" and "gate" in lost["message"]
    assert path.browser_gone == lost
    assert [n["event"] for n in path.drain_browser_notes()] == ["browser_gone"]


def test_a_wiped_browser_is_neither_a_restart_nor_gone(tmp_path, db, trace):
    clock = Clock()
    gate = ScriptedGate(trace)
    path, work = _path(tmp_path, db, trace, gate, clock)
    path.tick(work, lambda: db.doc("tasks/task_1"))
    gate.status = {"browser": "wiped", "browser_restarts": 0, "browser_exit": None}
    assert path.tick(work, lambda: db.doc("tasks/task_1")) is None
    assert _state(work)["state"] == "running"
    assert path.browser_gone is None and path.drain_browser_notes() == []


def test_note_browser_never_raises(tmp_path, db, trace):
    clock = Clock()
    gate = ScriptedGate(trace)
    path, work = _path(tmp_path, db, trace, gate, clock)
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the spool would be")
    path.note_browser(blocked, {"browser": "closed", "browser_restarts": 3, "browser_exit": "nonsense"})
    path.note_browser(work, None)
    path.note_browser(work, {"browser": 7, "browser_restarts": "x", "browser_exit": {"at": "soon"}})


# -- what the agent is told -----------------------------------------------------


def test_the_guidance_says_what_a_closed_cdp_connection_means():
    text = handoff_mod.AGENT_GUIDE
    assert "If either answers `gate_unavailable`" in text  # the existing sentences stay
    assert "$SWARM_HANDOFF/live-browser.json" in text
    for word in ("restarting", "crashed", "closes or is refused", "sign-ins are kept"):
        assert word in text, word


def _load_swarm_browser(name: str) -> Any:
    loader = importlib.machinery.SourceFileLoader(name, str(IMAGE / "swarm_browser.py"))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakePlaywright:
    """`sync_playwright().start()`: its connection fails `fail` times, then answers."""

    def __init__(self, fail: int, on_try: Any = None) -> None:
        self.fail = fail
        self.tries = 0
        self.stopped = False
        self.on_try = on_try
        self.chromium = types.SimpleNamespace(connect_over_cdp=self._connect)

    def _connect(self, endpoint: str, timeout: float) -> Any:
        self.tries += 1
        if self.on_try is not None:
            self.on_try(self.tries)
        if self.tries <= self.fail:
            raise RuntimeError("BrowserType.connect_over_cdp: Connection closed")
        return types.SimpleNamespace(contexts=["ctx"])

    def start(self) -> "FakePlaywright":
        return self

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def fake_playwright(monkeypatch):
    holder: dict[str, Any] = {}

    def install(pw: FakePlaywright) -> FakePlaywright:
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.sync_playwright = lambda: pw  # type: ignore[attr-defined]
        package = types.ModuleType("playwright")
        package.sync_api = sync_api  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "playwright", package)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
        holder["pw"] = pw
        return pw

    return install


def _spool(tmp_path: Path, monkeypatch, document: dict[str, Any] | None) -> Path:
    root = tmp_path / "spool"
    root.mkdir(exist_ok=True)
    if document is not None:
        (root / "live-browser.json").write_text(json.dumps(document))
    monkeypatch.setenv("SWARM_HANDOFF", str(root))
    return root


def test_live_waits_while_the_browser_is_restarting_then_connects(tmp_path, monkeypatch, fake_playwright):
    sb = _load_swarm_browser("swarm_browser_exit_t1")
    root = _spool(tmp_path, monkeypatch, {"state": "restarting", "restarts": 0, "message": "restarting"})

    def on_try(n: int) -> None:
        if n == 3:
            (root / "live-browser.json").write_text(json.dumps({"state": "running", "restarts": 1}))

    pw = fake_playwright(FakePlaywright(fail=2, on_try=on_try))
    clock = Clock()
    playwright, browser, context = sb.live("http://127.0.0.1:9222", sleep=clock.sleep, clock=clock.monotonic)
    assert playwright is pw and context == "ctx" and pw.tries == 3 and not pw.stopped
    assert 0 < clock.now <= 60


def test_live_gives_up_after_the_bound_with_the_files_message(tmp_path, monkeypatch, fake_playwright):
    sb = _load_swarm_browser("swarm_browser_exit_t2")
    message = "the live browser is restarting after Chromium exited (signal_SIGKILL)"
    _spool(tmp_path, monkeypatch, {"state": "restarting", "restarts": 0, "message": message})
    pw = fake_playwright(FakePlaywright(fail=10_000))
    clock = Clock()
    with pytest.raises(RuntimeError) as raised:
        sb.live("http://127.0.0.1:9222", wait_seconds=20, sleep=clock.sleep, clock=clock.monotonic)
    assert message in str(raised.value) and "restarting" in str(raised.value)
    assert 20 <= clock.now <= 25 and pw.stopped and pw.tries > 1


def test_live_raises_at_once_with_the_message_of_a_crashed_browser(tmp_path, monkeypatch, fake_playwright):
    sb = _load_swarm_browser("swarm_browser_exit_t3")
    message = "the live browser is gone for this attempt"
    _spool(tmp_path, monkeypatch, {"state": "crashed", "restarts": 3, "message": message})
    pw = fake_playwright(FakePlaywright(fail=10_000))
    clock = Clock()
    with pytest.raises(RuntimeError) as raised:
        sb.live("http://127.0.0.1:9222", sleep=clock.sleep, clock=clock.monotonic)
    assert message in str(raised.value) and "crashed" in str(raised.value)
    assert pw.tries == 1 and clock.slept == [] and pw.stopped


def test_live_raises_the_original_error_without_a_file(tmp_path, monkeypatch, fake_playwright):
    sb = _load_swarm_browser("swarm_browser_exit_t4")
    _spool(tmp_path, monkeypatch, None)
    pw = fake_playwright(FakePlaywright(fail=10_000))
    clock = Clock()
    with pytest.raises(RuntimeError, match="connect_over_cdp: Connection closed"):
        sb.live("http://127.0.0.1:9222", sleep=clock.sleep, clock=clock.monotonic)
    assert pw.tries == 1 and pw.stopped


def test_the_module_imports_without_playwright(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    sb = _load_swarm_browser("swarm_browser_exit_t5")
    assert callable(sb.live) and sb.BROWSER_STATE_NAME == "live-browser.json"
