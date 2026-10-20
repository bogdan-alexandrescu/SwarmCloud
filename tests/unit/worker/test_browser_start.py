"""A live-browser attempt starts its agent with the live browser, or not at all (BROWSER-START, part of #1366).

MEASURED 2026-10-15 (benchmark devb-1015): 8 of 60 claude-code-browser
attempts ran with no live browser -- four with no Playwright MCP tools, four
with SWARM_CDP_ENDPOINT empty -- and each fell back to a headless browser of
its own. NOLIVE-GUARD (#1372) cut the agent's browser loose from the viewer's
tunnel, which was that batch's cause. What it left, and these tests pin:

  * the gate's registration was tried ONCE: a channel that failed (ctl.sock
    there, not yet accepting, as while a gate restarts) failed the attempt at
    once instead of waiting a bounded moment for it;
  * a registration alone made `browser_ready`: nothing checked that a CDP
    endpoint was passed, or that the proxy behind it answered, so an attempt
    with no SWARM_CDP_ENDPOINT still started its agent, with no Playwright MCP;
  * the runner started a live-browser attempt's agent whatever it found: an
    empty endpoint, no browser image or no Playwright MCP entry in its
    `--mcp-config` all ran the agent headless, silently.

Now: the worker waits, bounded, for the gate and for the CDP proxy's
`/json/version`; past the bound the attempt fails retryably as
`live_gate_unavailable` before the agent exists, its lease and capacity
released; and the runner refuses (`live_browser_unavailable`) rather than run
headless. Every key here is built at runtime.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import handoff as handoff_mod
from agent_worker.errors import ExitCode
from agent_worker.hardening import PROTECTED, MemoryProtection
from agent_worker.runners import claude_code
from agent_worker.runners.base import RunnerContext, RunnerFailure
from swarm_common.states import TaskState

from test_handoff import (  # noqa: F401  (agent and trace are pytest fixtures, used by name)
    CDP,
    CTL,
    CtlError,
    FakeGateClient,
    FakeLiveRoutes,
    _live_worker,
    _Log,
    agent,
    trace,
)
from worker_seeds import seed_attempt

#: The module's own probe, taken before the suite's conftest stands in for it.
REAL_PROBE = getattr(handoff_mod, "_probe_cdp", None)


class Clock:
    """Wall time that moves only when the path sleeps."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class RestartingGate(FakeGateClient):
    """A gate whose ctl.sock does not accept the first `losses` registrations."""

    def __init__(self, trace: list[str], losses: int) -> None:
        super().__init__(trace)
        self.losses = losses
        self.attempts = 0

    def register(self) -> None:
        self.attempts += 1
        if self.losses > 0:
            self.losses -= 1
            self.trace.append("gate:register:lost")
            raise CtlError("ctl.sock is not accepting")
        super().register()


class Probe:
    """The CDP proxy's `/json/version`, unanswered for the first `misses` asks."""

    def __init__(self, misses: int) -> None:
        self.misses = misses
        self.asked: list[str] = []

    def __call__(self, endpoint: str, timeout: float) -> bool:
        self.asked.append(endpoint)
        if self.misses > 0:
            self.misses -= 1
            return False
        return True


def _path(gate: FakeGateClient, db, trace, monkeypatch, *, probe: Any, clock: Clock | None = None,  # noqa: F811
          environ: dict[str, str] | None = None, notes: list | None = None):
    routes = FakeLiveRoutes(db, trace)
    key = children_mod.AttemptKey()
    routes.public_key = key.public
    clock = clock or Clock()
    cfg = SimpleNamespace(tenant_id="eng", task_id="task_1", attempt_id="att_1", lease_id="lease_1",
                          generation=1, runner_profile="mock")
    # The module's probe, as the worker's own construction (lifecycle) uses it.
    monkeypatch.setattr(handoff_mod, "_probe_cdp", probe, raising=False)
    path = handoff_mod.HandoffPath(
        cfg, logger=_Log(), memory=MemoryProtection(PROTECTED, "test"),
        children=SimpleNamespace(_key=key, _api=routes, unavailable=None),
        live=True, gate_client=lambda p: gate,
        environ=environ if environ is not None else {"SWARM_GATE_CTL": CTL, "SWARM_CDP_ENDPOINT": CDP},
        wall=clock, sleep=clock.sleep, rand=lambda: 0.5,
    )
    path.setup(notes.append if notes is not None else None)
    return path


# --------------------------------------------------------------------------
# The gate: a channel that failed is waited for, bounded; a refusal is not
# --------------------------------------------------------------------------


def test_a_registration_whose_channel_failed_is_retried_until_the_gate_takes_it(tmp_path, db, trace, monkeypatch):  # noqa: F811
    gate, clock = RestartingGate(trace, losses=3), Clock()
    path = _path(gate, db, trace, monkeypatch, clock=clock, probe=Probe(0))
    assert gate.attempts == 4, "the registration was not tried again after its channel failed"
    assert path.browser_ready and not path.gate_missing
    assert len(clock.slept) == 3 and sum(clock.slept) <= handoff_mod.BROWSER_WAIT_SECONDS
    work = tmp_path / "work"
    work.mkdir()
    assert path.prepare(work).get("SWARM_CDP_ENDPOINT") == CDP


def test_a_gate_that_never_takes_the_registration_is_waited_for_a_bounded_time(db, trace, monkeypatch):  # noqa: F811
    gate, clock = RestartingGate(trace, losses=10_000), Clock()
    path = _path(gate, db, trace, monkeypatch, clock=clock, probe=Probe(0))
    assert path.gate_missing and not path.browser_ready
    assert path.browser_missing == handoff_mod.GATE_UNAVAILABLE
    assert gate.attempts > 1
    assert sum(clock.slept) <= handoff_mod.BROWSER_WAIT_SECONDS


def test_a_registration_the_gate_refused_is_not_retried(db, trace, monkeypatch):  # noqa: F811
    gate, clock = RestartingGate(trace, losses=0), Clock()
    gate.refuse_registration = True  # a restarted gate refuses every registration
    path = _path(gate, db, trace, monkeypatch, clock=clock, probe=Probe(0))
    assert path.gate_missing and gate.attempts == 1 and clock.slept == []


# --------------------------------------------------------------------------
# The CDP endpoint: passed, and answering, before the agent may start
# --------------------------------------------------------------------------


def test_a_cdp_endpoint_that_answers_late_is_waited_for(tmp_path, db, trace, monkeypatch):  # noqa: F811
    probe, clock = Probe(misses=3), Clock()
    path = _path(FakeGateClient(trace), db, trace, monkeypatch, probe=probe, clock=clock)
    assert probe.asked == [CDP] * 4, "the worker did not wait for the CDP proxy to answer"
    assert path.browser_ready and not path.gate_missing
    work = tmp_path / "work"
    work.mkdir()
    assert path.prepare(work).get("SWARM_CDP_ENDPOINT") == CDP


def test_a_cdp_endpoint_that_never_answers_is_not_a_ready_browser(tmp_path, db, trace, monkeypatch):  # noqa: F811
    probe, clock, notes = Probe(misses=10_000), Clock(), []
    path = _path(FakeGateClient(trace), db, trace, monkeypatch, probe=probe, clock=clock, notes=notes)
    assert path.gate_missing and not path.browser_ready, "an unanswering CDP proxy counted as a browser"
    assert path.browser_missing == handoff_mod.CDP_UNREACHABLE
    assert len(probe.asked) > 1
    assert sum(clock.slept) <= handoff_mod.BROWSER_WAIT_SECONDS
    work = tmp_path / "work"
    work.mkdir()
    assert "SWARM_CDP_ENDPOINT" not in path.prepare(work)
    assert any(n.get("live_path") == handoff_mod.CDP_UNREACHABLE for n in notes), notes


def test_a_live_attempt_with_no_cdp_endpoint_is_not_a_ready_browser(db, trace, monkeypatch):  # noqa: F811
    path = _path(FakeGateClient(trace), db, trace, monkeypatch, probe=Probe(0), environ={"SWARM_GATE_CTL": CTL})
    assert path.gate_missing and not path.browser_ready, "a gate with no SWARM_CDP_ENDPOINT counted as a browser"
    assert path.browser_missing == handoff_mod.NO_CDP_ENDPOINT


def test_the_probe_reads_the_gates_own_json_version_and_refuses_a_closed_port():
    """The real probe against the real proxy's HTTP side (livegate.cdpproxy
    `serve_agents`), whose `/json/version` touches neither the proxy nor Chromium."""
    from agent_worker.livegate import cdpproxy

    assert REAL_PROBE is not None, "handoff has no CDP probe"
    loop = asyncio.new_event_loop()
    started = threading.Event()
    box: dict[str, Any] = {}

    def run() -> None:
        asyncio.set_event_loop(loop)
        box["server"] = loop.run_until_complete(cdpproxy.serve_agents(None, host="127.0.0.1", port=0))
        started.set()
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(5)
    port = box["server"].sockets[0].getsockname()[1]
    try:
        assert REAL_PROBE(f"http://127.0.0.1:{port}", 2.0) is True
    finally:
        loop.call_soon_threadsafe(box["server"].close)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
    # The same port, closed now: nothing answers.
    assert REAL_PROBE(f"http://127.0.0.1:{port}", 0.5) is False


# --------------------------------------------------------------------------
# End to end: the agent never starts without its browser, and the attempt is retried
# --------------------------------------------------------------------------


def _capacity_released(db) -> bool:
    lease = db.doc("leases/lease_1")
    pools = [db.doc(name if name.startswith("pools/") else f"pools/{name}") for name in lease["pools"]]
    return lease.get("released_at") is not None and all(int(p["active"]) < 1 for p in pools)


def test_a_cdp_endpoint_that_never_answers_fails_the_attempt_retryably_before_the_agent(
    db, worker_factory, agent, trace, monkeypatch  # noqa: F811
):
    _, records = agent
    seed_attempt(db)
    monkeypatch.setattr(handoff_mod, "_probe_cdp", lambda endpoint, timeout: False, raising=False)
    # The bound, not its length, is what this test is about: no real 20 s.
    monkeypatch.setattr(handoff_mod, "BROWSER_WAIT_SECONDS", 0.0, raising=False)
    worker = _live_worker(worker_factory, FakeLiveRoutes(db, trace), FakeGateClient(trace), trace, monkeypatch)
    assert worker.run() == ExitCode.FAILED
    assert records() == [], "the agent started without its browser"
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.READY.value, task.get("last_error")
    assert task["last_error"].startswith(f"{handoff_mod.LIVE_GATE_UNAVAILABLE}: "), task["last_error"]
    assert "cdp_unreachable" in task["last_error"]
    assert task.get("next_eligible_at") is not None
    assert _capacity_released(db), db.doc("leases/lease_1")


# --------------------------------------------------------------------------
# The runner: a live-browser attempt's agent starts with Playwright MCP, or not at all
# --------------------------------------------------------------------------


def _run_body(monkeypatch, tmp_path, *, endpoint: str | None,
              browsers: str | None = "/opt/playwright") -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["args"] = list(extra_args)
        seen["env"] = dict(extra_env or {})
        return {}

    spool = tmp_path / "work" / ".swarm-handoff"
    spool.mkdir(parents=True)
    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    monkeypatch.setattr(claude_code, "JEV_MCP_BIN", str(tmp_path / "absent"))
    monkeypatch.setenv("SWARM_HANDOFF", str(spool))
    for name, value in ((claude_code.BROWSERS_PATH_ENV, browsers), ("SWARM_CDP_ENDPOINT", endpoint)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    for name in ("SWARM_GIT", "SWARM_SEARCH", "SWARM_JEV_KEY_FILE", "SWARM_JEV_BACKEND", "JEV_API_URL"):
        monkeypatch.delenv(name, raising=False)
    ctx = RunnerContext(
        work_dir=tmp_path / "work", artifacts_dir=tmp_path / "artifacts",
        input_path=tmp_path / "input.json", result_path=tmp_path / "result.json",
        quota_path=tmp_path / "quota.json", payload={"prompt": "Check https://example.com/pricing."},
    )
    claude_code.body(ctx)
    return seen


def test_a_live_attempt_with_its_browser_starts_with_playwright_mcp(monkeypatch, tmp_path):
    seen = _run_body(monkeypatch, tmp_path, endpoint=CDP)
    args = seen["args"]
    servers = json.loads(open(args[args.index("--mcp-config") + 1]).read())["mcpServers"]
    assert claude_code.MCP_SERVER_NAME in servers
    assert seen["env"]["SWARM_CDP_ENDPOINT"] == CDP


@pytest.mark.parametrize("case", ["empty_endpoint", "no_browser_image"])
def test_a_live_attempt_without_its_browser_is_refused_never_run_headless(monkeypatch, tmp_path, case):
    kwargs: dict[str, Any] = {"endpoint": CDP}
    if case == "empty_endpoint":
        kwargs["endpoint"] = ""
    else:
        kwargs["browsers"] = None
    with pytest.raises(RunnerFailure) as refused:
        _run_body(monkeypatch, tmp_path, **kwargs)
    assert str(refused.value).startswith("live_browser_unavailable: "), str(refused.value)


def test_a_live_attempt_whose_mcp_config_lacks_playwright_is_refused(monkeypatch, tmp_path):
    original = claude_code.write_mcp_config

    def without_playwright(directory, endpoint, **kwargs):
        return original(directory, None, **kwargs)

    monkeypatch.setattr(claude_code, "write_mcp_config", without_playwright)
    with pytest.raises(RunnerFailure) as refused:
        _run_body(monkeypatch, tmp_path, endpoint=CDP)
    assert "Playwright MCP is not registered" in str(refused.value)


def test_a_profile_without_the_live_browser_is_not_refused(monkeypatch, tmp_path):
    """No SWARM_HANDOFF: not a live-browser attempt, so nothing is required of it."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(claude_code, "run_cli_agent", lambda ctx, spec, **kw: seen.setdefault("ran", True))
    for name in (claude_code.HANDOFF_ENV, *claude_code.AGENT_PASSTHROUGH_ENV, claude_code.BROWSERS_PATH_ENV):
        monkeypatch.delenv(name, raising=False)
    ctx = RunnerContext(
        work_dir=tmp_path, artifacts_dir=tmp_path / "artifacts", input_path=tmp_path / "input.json",
        result_path=tmp_path / "result.json", quota_path=tmp_path / "quota.json", payload={"prompt": "hi"},
    )
    claude_code.body(ctx)
    assert seen == {"ran": True}
