"""The live gate starts Chromium again when it exits unexpectedly (BROWSER-RELAUNCH; part of #1030).

MEASURED 2026-10-13 (att_59f378aac5c340ca9507): after device switches a take
was refused "the browser had closed" -- Chromium's pipe had closed, and the
gate (DEVICE-RECOVER, #1237) could re-attach pages but not bring back a
process that had ended. Owner decision the same day: relaunch Chromium.

Each test drives the real gate (`LiveGate.start_browser` and the relaunch
behind it) against scripted Chromiums (test_live_device_recover's
`FakeChromium`): one per launch, each exiting when its pipe is closed with
the exit code and the stderr the test gives it. No Chromium, no Xvfb, no
network.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable

from test_live_device_recover import (
    URL,
    FakeChromium,
    FakeDisplay,
    FakeInjector,
    FakeTunnel,
    _config,
    _until,
)

from agent_worker.livegate import __main__ as gate_main

SIGSEGV = -11


class Chromium(FakeChromium):
    """`FakeChromium`, also answering an agent's connection (`Target.attachToBrowserTarget`)."""

    def _answer(self, method: str, params: dict[str, Any], session: str | None) -> dict[str, Any]:
        if method == "Target.attachToBrowserTarget":
            self._next += 1
            return {"result": {"sessionId": f"B{self._next}"}}
        return super()._answer(method, params, session)


class ExitingBrowser:
    """One launched Chromium: its pipe, and how it ended."""

    def __init__(self) -> None:
        self.transport = Chromium()
        self.returncode: int | None = None
        self.stderr: list[str] = []
        self.stopped = False

    def die(self, returncode: int, *stderr: str) -> None:
        self.returncode = returncode
        self.stderr.extend(stderr)
        self.transport.close_pipe()

    async def exited(self, timeout: float) -> tuple[int | None, list[str]]:
        return self.returncode, list(self.stderr)

    async def stop(self) -> None:
        self.stopped = True
        if self.returncode is None:
            self.returncode = -15
        self.transport.close_pipe()


class Server:
    def __init__(self, proxy: Any, host: str, port: int) -> None:
        self.proxy, self.host, self.port = proxy, host, port
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None: ...


class Link:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict[str, Any]] = []

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True


class Rig:
    def __init__(self, tmp: Path, *, limit: int = 3) -> None:
        self.browsers: list[ExitingBrowser] = []
        self.launched_with: list[gate_main.GateConfig] = []
        self.servers: list[Server] = []
        self.fail_launches = 0
        self.display = FakeDisplay()
        self.records: list[tuple[str, dict[str, Any]]] = []

        async def start_chromium(cfg: gate_main.GateConfig) -> ExitingBrowser:
            self.launched_with.append(cfg)
            if self.fail_launches:
                self.fail_launches -= 1
                raise gate_main.SetupError("Chromium could not be started: scripted")
            browser = ExitingBrowser()
            self.browsers.append(browser)
            return browser

        async def serve_cdp(proxy: Any, host: str, port: int) -> Server:
            server = Server(proxy, host, port)
            self.servers.append(server)
            return server

        deps = gate_main.Deps(start_chromium=start_chromium, serve_cdp=serve_cdp,
                              open_display=lambda cfg: self.display)
        self.cfg = _config(tmp)
        self.gate = gate_main.LiveGate(self.cfg, deps,
                                       record=lambda e, f: self.records.append((e, dict(f))),
                                       relaunch_limit=limit, relaunch_delays=(0.01, 0.02))
        self.gate.injector = FakeInjector()
        self.tunnel = FakeTunnel()

    @property
    def chromium(self) -> FakeChromium:
        return self.browsers[-1].transport

    async def start(self) -> None:
        await self.gate.start_browser()
        self.gate.tunnel = self.tunnel  # type: ignore[assignment]
        await _until(self.showing)

    def showing(self) -> bool:
        """The mask has a live session on the current Chromium's page, and its origin is reported."""
        gate, chromium = self.gate, self.chromium
        pages = gate.pages
        return bool(pages is not None and not gate.browser_closed and pages.current in chromium.pages
                    and pages._session in chromium.sessions
                    and gate.mask_state._session and gate.origin is not None)

    def events(self) -> list[tuple[str, str | None]]:
        return [(str(a.get("event")), a.get("cause")) for a in self.tunnel.actions
                if a.get("kind") == "control"]

    def recorded(self, event: str) -> list[dict[str, Any]]:
        return [f for e, f in self.records if e == event]

    async def crash(self, *stderr: str, code: int = SIGSEGV) -> None:
        before = len(self.browsers)
        self.browsers[-1].die(code, *stderr)
        await _until(lambda: self.gate.browser_closed)
        await _until(lambda: len(self.browsers) > before or self.gate.status()["browser"] == "closed")

    async def stop(self) -> None:
        gate = self.gate
        if gate.pages is not None:
            await gate.pages.close()
        tasks = [*gate._tasks.values(), *gate._spawned]
        if gate.devices is not None:
            tasks.extend(gate.devices._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _run(body: Callable[[Rig], Any], tmp_path: Path, **kw: Any) -> None:
    async def main() -> None:
        rig = Rig(tmp_path, **kw)
        try:
            await rig.start()
            await body(rig)
        finally:
            await rig.stop()

    asyncio.run(asyncio.wait_for(main(), 20))


def test_an_unexpected_exit_relaunches_on_the_same_profile_and_listen_address_and_the_screen_comes_back(tmp_path):
    async def body(rig: Rig) -> None:
        gate = rig.gate
        profile = rig.cfg.profile_dir
        (profile / "Cookies").write_bytes(b"jar")
        # What a Chromium that died leaves behind, and would refuse the profile over.
        (profile / "SingletonLock").symlink_to("gate-host-4242")
        assert gate.devices is not None and gate.touch is not None
        await gate.devices.select("iphone-15", "landscape")
        gate.touch.set_touch(False)
        first = rig.browsers[0]

        await rig.crash("[1013:1013:ERROR:gpu_init.cc(1)] crashed")
        await _until(rig.showing)

        assert len(rig.browsers) == 2
        assert [c.profile_dir for c in rig.launched_with] == [profile, profile]
        assert (profile / "Cookies").read_bytes() == b"jar"
        assert not (profile / "SingletonLock").is_symlink()
        assert [(s.host, s.port) for s in rig.servers] == [("127.0.0.1", 9222)] * 2
        assert rig.servers[0].closed and not rig.servers[1].closed
        assert rig.servers[1].proxy is gate.proxy
        # The device and the touch toggle the person had are the new browser's.
        assert (gate.devices.device.name, gate.devices.device.orientation) == ("iphone-15", "landscape")
        assert gate.touch.touch_on is False
        assert rig.chromium.methods_on("PAGE1", "Emulation.setDeviceMetricsOverride")
        assert first.transport is not rig.chromium
        status = gate.status()
        assert status["browser"] == "running" and status["browser_restarts"] == 1
        assert ("browser_restarted", "signal_SIGSEGV") in rig.events()
        assert rig.recorded("browser_restarted")[0]["signal"] == "SIGSEGV"

    _run(body, tmp_path)


def test_the_exit_cause_is_logged_with_chromiums_last_stderr_lines_redacted(tmp_path, caplog):
    secret = "ghp_" + "Q" * 36
    caplog.set_level(logging.INFO, logger="agent_worker.livegate")

    async def body(rig: Rig) -> None:
        await rig.crash("[1:1:FATAL:memory.cc(22)] Out of memory",
                        f"fetching https://user:pw@example.test/private/path?token={secret}",
                        code=SIGSEGV)
        await _until(rig.showing)
        exited = rig.recorded("browser_exited")
        assert exited and exited[0]["exit_code"] == SIGSEGV and exited[0]["signal"] == "SIGSEGV"

    _run(body, tmp_path)
    text = caplog.text
    assert "SIGSEGV" in text and "Out of memory" in text
    assert "https://example.test" in text
    assert secret not in text and "/private/path" not in text and "user:pw" not in text


def test_a_plain_exit_code_is_the_cause_when_no_signal_ended_it(tmp_path):
    async def body(rig: Rig) -> None:
        await rig.crash(code=21)
        await _until(rig.showing)
        assert ("browser_restarted", "exit_code_21") in rig.events()

    _run(body, tmp_path)


def test_a_wipe_does_not_relaunch(tmp_path):
    async def body(rig: Rig) -> None:
        await rig.gate.wipe("attempt_ended")
        await _until(lambda: rig.gate.browser_closed)
        await asyncio.sleep(0.2)
        assert len(rig.launched_with) == 1
        assert rig.gate.status()["browser"] == "wiped"
        assert not [e for e, _ in rig.events() if e.startswith("browser_")]

    _run(body, tmp_path)


def test_stopping_the_gate_does_not_relaunch(tmp_path):
    async def body(rig: Rig) -> None:
        await rig.gate.stop()
        await asyncio.sleep(0.2)
        assert len(rig.launched_with) == 1
        assert rig.browsers[0].stopped
        assert not rig.recorded("browser_restarted") and not rig.recorded("browser_exited")

    _run(body, tmp_path)


def test_a_relaunch_that_fails_to_start_is_tried_again_after_a_backoff(tmp_path):
    async def body(rig: Rig) -> None:
        rig.fail_launches = 1
        rig.browsers[0].die(SIGSEGV)
        await _until(lambda: len(rig.browsers) == 2)
        await _until(rig.showing)
        assert len(rig.launched_with) == 3
        assert rig.recorded("browser_relaunch_failed")
        assert rig.gate.status()["browser"] == "running"

    _run(body, tmp_path)


def test_the_relaunch_budget_exhausted_leaves_the_browser_closed_and_says_so(tmp_path):
    async def body(rig: Rig) -> None:
        for _ in range(2):
            await rig.crash()
            await _until(rig.showing)
        rig.browsers[-1].die(SIGSEGV)
        await _until(lambda: rig.gate.status()["browser"] == "closed")
        await asyncio.sleep(0.2)
        assert len(rig.launched_with) == 3
        assert ("browser_gave_up", "signal_SIGSEGV") in rig.events()
        assert rig.recorded("browser_gave_up")
        assert rig.gate.browser_closed
        # A take is refused as it was before relaunching existed, and a resume still lets the agent go.
        await rig.gate._grant("c1")
        assert ("refused", "browser_closed") in rig.events()
        assert await asyncio.wait_for(rig.gate.resume(), 10) == {}

    _run(body, tmp_path, limit=2)


def test_a_person_in_control_has_the_episode_ended_with_browser_restarted_and_the_hand_off_stays_pending(tmp_path):
    async def body(rig: Rig) -> None:
        gate = rig.gate
        await gate.pause({})  # the worker's pause: a hand-off
        await gate._grant("c1")
        assert gate._episode is not None
        await rig.crash()
        await _until(rig.showing)
        assert gate._episode is None
        assert ("ended", "browser_restarted") in rig.events()
        assert {"cause": "browser_restarted"} in rig.recorded("control_ended")
        # Still the agent's hand-off: held on the new browser until the resume.
        assert gate.proxy is not None and gate.proxy.paused
        await gate._grant("c2")
        assert rig.events()[-1] == ("granted", None)
        page = await gate.resume()
        assert page.get("url") == URL
        assert not gate.proxy.paused

    _run(body, tmp_path)


def test_the_agents_connections_drop_on_exit_and_the_proxy_accepts_a_new_one_after_relaunch(tmp_path):
    async def body(rig: Rig) -> None:
        old_proxy = rig.gate.proxy
        assert old_proxy is not None
        link = Link()
        assert await old_proxy.connect_agent(link) is not None
        await rig.crash()
        assert link.closed
        await _until(rig.showing)
        served = rig.servers[-1].proxy
        assert served is rig.gate.proxy and served is not old_proxy
        assert await served.connect_agent(Link()) is not None

    _run(body, tmp_path)


def test_a_real_process_that_dies_on_a_signal_gives_its_signal_and_its_last_stderr_lines():
    """`Browser.exited`, on a real child: what `start_chromium` keeps of Chromium's stderr."""
    import signal
    import sys

    script = ("import os, signal, sys\n"
              "for n in range(30): sys.stderr.write(f'line {n}\\n')\n"
              "sys.stderr.flush()\n"
              "os.kill(os.getpid(), signal.SIGSEGV)\n")

    class Pipe:
        async def close(self) -> None: ...

    async def main() -> tuple[int | None, list[str]]:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", script,
                                                    stdin=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE)
        return await gate_main.Browser(proc, Pipe()).exited(10)

    code, tail = asyncio.run(asyncio.wait_for(main(), 20))
    assert code == -signal.SIGSEGV
    assert gate_main.exit_cause(code) == ("signal_SIGSEGV", "SIGSEGV")
    assert len(tail) == gate_main.STDERR_TAIL_LINES and tail[-1] == "line 29"
