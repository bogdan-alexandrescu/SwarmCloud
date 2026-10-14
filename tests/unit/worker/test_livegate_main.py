"""The display sidecar's process: `python -m agent_worker.livegate` (live browser L3m).

Measured 2026-10-11 on swarm-autopilot: every `claude-code-browser` pod's
`display` sidecar crash-looped with "No module named agent_worker.livegate.__main__",
because each lane built a component of the gate and none built the process
that composes them. These tests hold:

* the scheduler's sidecar command names a module that runs -- read from the
  manifest the scheduler renders, not restated here -- so the gap cannot recur;
* the process starts the design's pieces in order (Xvfb, Chromium and the CDP
  proxy, ctl.sock, the view), and the dispatcher's startup probe passes only
  once ctl.sock is listening;
* the worker's operations reach the components over the real ctl.sock, the
  tunnel opens on the worker's ticket, a viewer's control is granted and
  released, the wipe empties the profile;
* a fatal setup error is one log line and exit 1; SIGTERM stops everything.

Offline: no X server, no Chromium, no relay. Each is a fake in `Deps`. Every
ticket is built at run time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from importlib.util import find_spec, module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate import __main__ as gate_main
from agent_worker.livegate import ctl, report, session, tunnel
from agent_worker.livegate.capture import Frame
from agent_worker.livegate.ctl import CtlClient, CtlRefused
from agent_worker.livegate.rfb import PROTOCOL_VERSION

REPO = Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------
# What the scheduler renders for the sidecar
# --------------------------------------------------------------------------


def _rendered_display() -> dict[str, Any]:
    """The `display` init container exactly as the scheduler renders it."""
    from swarm_common.profiles import RESOURCE_CLASSES

    from scheduler.dispatch import with_live_display

    job = {"spec": {"template": {"metadata": {"labels": {}}, "spec": {
        "containers": [{"name": "worker", "image": "agent-runtime-browser@sha256:" + "0" * 64,
                        "volumeMounts": [], "env": []}],
        "volumes": [],
    }}}}
    rendered = with_live_display(job, rc=RESOURCE_CLASSES["browser"])
    (display,) = [c for c in rendered["spec"]["template"]["spec"]["initContainers"] if c["name"] == "display"]
    return display


def _module_of(command: list[str]) -> str:
    assert "-m" in command, f"the sidecar command no longer runs a module: {command}"
    return command[command.index("-m") + 1]


def test_the_sidecar_command_names_a_module_with_a_main():
    """THE REGRESSION. `python -m <package>` needs `<package>/__main__.py`."""
    module = _module_of(_rendered_display()["command"])
    spec = find_spec(module)
    assert spec is not None, f"{module} is not importable"
    if spec.submodule_search_locations is not None:
        main = find_spec(f"{module}.__main__")
        assert main is not None and main.origin and Path(main.origin).is_file(), (
            f"the scheduler runs `python -m {module}`, and {module} is a package with no __main__.py: "
            "every live-browser pod's display sidecar would crash-loop")


def test_the_sidecar_module_runs_and_answers_help():
    module = _module_of(_rendered_display()["command"])
    result = subprocess.run([sys.executable, "-m", module, "--help"], capture_output=True, text=True,
                            timeout=60, env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout.lower()


def test_the_sidecars_environment_is_all_the_gate_needs():
    env = {e["name"]: e["value"] for e in _rendered_display()["env"]}
    cfg = gate_main.GateConfig.from_env(env)
    assert cfg.ctl_path == env["SWARM_GATE_CTL"]
    assert f"{cfg.cdp_host}:{cfg.cdp_port}" == env["SWARM_CDP_LISTEN"]
    assert cfg.relay_url == tunnel.DEFAULT_RELAY_URL + tunnel.TUNNEL_PATH
    assert cfg.display == env["DISPLAY"]


def test_the_relay_url_comes_from_swarm_live_url_when_set():
    assert tunnel.relay_url({"SWARM_LIVE_URL": "ws://10.0.0.9:8080"}) == "ws://10.0.0.9:8080/v1/tunnel"
    with pytest.raises(ValueError):
        tunnel.relay_url({"SWARM_LIVE_URL": "https://relay.example"})


def test_a_missing_variable_is_a_fatal_setup_error():
    with pytest.raises(gate_main.SetupError):
        gate_main.GateConfig.from_env({"SWARM_GATE_CTL": "/run/swarm-gate/ctl.sock"})


def test_every_operation_the_worker_sends_is_one_ctl_sock_accepts():
    """browser_login.py sends session_export/import; before L3m ctl.sock refused both."""
    from agent_worker import handoff

    assert session.OPS <= ctl.OPS
    assert handoff.GATE_OPS <= ctl.OPS


def _relay_rendezvous() -> Any:
    """The relay's own module, by path: swarm-live is not a member of the root
    workspace (tests/unit/live/test_relay.py says why), and rendezvous.py is
    standard library only."""
    path = REPO / "apps" / "swarm-live" / "swarm_live" / "rendezvous.py"
    spec = spec_from_file_location("_swarm_live_rendezvous_for_the_gate", path)
    assert spec is not None and spec.loader is not None, path
    module = module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolves annotations through it
    spec.loader.exec_module(module)
    return module


def test_the_tunnel_frames_are_the_relays():
    rendezvous = _relay_rendezvous()
    for name in ("OP_OPEN", "OP_DATA", "OP_CLOSE", "OP_CONTROL", "OP_ACTION", "OP_REPORT", "OP_DEVICE",
                 "MAX_CONN_ID"):
        assert getattr(tunnel, name) == getattr(rendezvous, name), name
    assert gate_main.MAX_TICKET_CHARS == rendezvous.MAX_TICKET_CHARS
    assert report.MAX_REPORT_BYTES == rendezvous.MAX_REPORT_BYTES
    frame = rendezvous.encode_frame(rendezvous.OP_DATA, "v1", b"abc")
    assert tunnel.decode_frame(frame) == (tunnel.OP_DATA, "v1", b"abc")
    assert tunnel.encode_frame(tunnel.OP_CLOSE, "v1") == rendezvous.encode_frame(rendezvous.OP_CLOSE, "v1")


def test_xvfb_listens_on_its_unix_socket_only():
    """The abstract socket is in the pod's network namespace, which the agent shares."""
    import inspect

    source = inspect.getsource(gate_main.start_xvfb)
    assert '"-nolisten", "tcp", "-nolisten", "local"' in source


def test_chromium_is_headed_on_the_pipe_with_the_profile_on_the_sidecar_volume(tmp_path):
    cfg = _config(tmp_path)
    args = gate_main.chromium_args(cfg)
    assert "--remote-debugging-pipe" in args
    assert f"--user-data-dir={cfg.profile_dir}" in args
    assert not any(a.startswith(("--headless", "--remote-debugging-port", "--kiosk", "--app")) for a in args)


def test_the_headed_chromium_is_found_not_the_headless_shell(tmp_path):
    for name in ("chromium-1200/chrome-linux/chrome", "chromium_headless_shell-1200/chrome-linux/headless_shell"):
        path = tmp_path / name
        path.parent.mkdir(parents=True)
        path.write_text("")
    assert gate_main.find_chromium(str(tmp_path)) == str(tmp_path / "chromium-1200/chrome-linux/chrome")
    with pytest.raises(gate_main.SetupError):
        gate_main.find_chromium(str(tmp_path / "absent"))


# --------------------------------------------------------------------------
# Fakes for everything that touches the operating system
# --------------------------------------------------------------------------


class Calls(list):
    def names(self) -> list[str]:
        return [c[0] for c in self]


class FakePipe:
    """Chromium's pipe: answers the gate's commands and emits what a page would."""

    def __init__(self, calls: Calls) -> None:
        self.calls = calls
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.methods: list[str] = []
        self.closed = False
        self._sessions = 0
        # What `Target.getTargetInfo` and `Security.enable` report for a page.
        self.page_url = "https://example.test/signin"
        self.security = "secure"

    def _session(self) -> str:
        self._sessions += 1
        return f"S{self._sessions}"

    async def send(self, message: dict[str, Any]) -> None:
        method = message["method"]
        self.methods.append(method)
        session = message.get("sessionId")
        result: dict[str, Any] = {}
        if method == "Browser.getVersion":
            result = {"product": "Chrome/130.0.6723.0", "userAgent": "Mozilla/5.0 (X11; Linux x86_64)"}
        elif method == "Target.getTargets":
            result = {"targetInfos": [{"targetId": "PAGE1", "type": "page", "url": "about:blank"}]}
        elif method in ("Target.attachToTarget", "Target.attachToBrowserTarget"):
            result = {"sessionId": self._session()}
        elif method == "Browser.getWindowForTarget":
            result = {"windowId": 1}
        elif method == "Page.getFrameTree":
            result = {"frameTree": {"frame": {"id": "PAGE1", "url": "about:blank", "domainAndRegistry": ""}}}
        elif method == "Target.getTargetInfo":
            result = {"targetInfo": {"targetId": message["params"]["targetId"], "type": "page",
                                     "url": self.page_url, "title": "Sign in"}}
        elif method == "Target.createBrowserContext":
            result = {"browserContextId": "CTX1"}
        elif method == "Target.createTarget":
            result = {"targetId": "PAGE2"}
        elif method == "Storage.getCookies":
            result = {"cookies": []}
        elif method == "Target.getBrowserContexts":
            result = {"browserContextIds": []}
        self.queue.put_nowait({"id": message["id"], "result": result})
        if method == "Security.enable":
            self.queue.put_nowait({"method": "Security.visibleSecurityStateChanged", "sessionId": session,
                                   "params": {"visibleSecurityState": {"securityState": self.security}}})

    async def run(self, sink: Any) -> None:
        while True:
            message = await self.queue.get()
            if message is None:
                return
            await sink.on_chromium_message(message)

    async def close(self) -> None:
        self.closed = True
        self.queue.put_nowait(None)


class FakeBrowser:
    def __init__(self, calls: Calls) -> None:
        self.calls = calls
        self.transport = FakePipe(calls)
        self.stopped = False

    async def stop(self) -> None:
        if not self.stopped:
            self.calls.append(("chromium.stop",))
        self.stopped = True
        await self.transport.close()


class FakeChild:
    def __init__(self, calls: Calls, name: str) -> None:
        self.calls, self.name = calls, name

    async def stop(self) -> None:
        self.calls.append((f"{self.name}.stop",))


class FakeInjector:
    def __init__(self, calls: Calls) -> None:
        self.calls = calls
        self.released = 0
        self.size: tuple[int, int] | None = None

    async def key(self, keysym: int, down: bool) -> None: ...

    async def pointer_event(self, buttons: int, x: int, y: int) -> None: ...

    async def type_text(self, text: str) -> bool:
        return True

    async def release_all(self) -> None:
        self.released += 1

    def resize(self, width: int, height: int) -> None:
        self.size = (width, height)

    async def close(self) -> None:
        self.calls.append(("injector.close",))


class FakeDisplay:
    def __init__(self) -> None:
        self.sizes: list[tuple[int, int]] = []

    async def resize(self, width: int, height: int) -> None:
        self.sizes.append((width, height))


class FakeFramebuffer:
    def __init__(self, calls: Calls) -> None:
        self.calls = calls
        self.grabs = 0

    def grab(self) -> Frame:
        self.grabs += 1
        return Frame.blank(1440, 987, (255, 255, 255))

    def close(self) -> None:
        self.calls.append(("framebuffer.close",))


class FakeServer:
    def __init__(self, calls: Calls) -> None:
        self.calls = calls
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.calls.append(("cdp.close",))
        self.closed = True

    async def wait_closed(self) -> None:
        pass


class FakeRelaySocket:
    """The relay, as the gate's websocket sees it."""

    def __init__(self, ready: bool = True) -> None:
        self.inbox: asyncio.Queue[tuple[bool, bytes] | None] = asyncio.Queue()
        self.texts: list[str] = []
        self.frames: list[tuple[int, str, bytes]] = []
        self.got_frame = asyncio.Event()
        self.close_code: int | None = None if ready else 4404
        self._ready = ready
        self.closed = False

    async def send_text(self, text: str) -> None:
        self.texts.append(text)
        if self._ready:
            self.inbox.put_nowait((True, json.dumps({"type": "ready"}).encode()))
        else:
            self.inbox.put_nowait(None)

    async def send_bytes(self, data: bytes) -> None:
        self.frames.append(tunnel.decode_frame(data))
        self.got_frame.set()

    async def receive(self) -> tuple[bool, bytes] | None:
        return await self.inbox.get()

    def push(self, op: int, conn_id: str, payload: bytes = b"") -> None:
        self.inbox.put_nowait((False, tunnel.encode_frame(op, conn_id, payload)))

    async def close(self, code: int = 1000) -> None:
        if not self.closed:
            self.closed = True
            self.inbox.put_nowait(None)


def _config(tmp: Path) -> gate_main.GateConfig:
    for name in ("profile", "downloads", "uploads"):
        (tmp / name).mkdir(exist_ok=True)
    return gate_main.GateConfig(
        ctl_path=str(tmp / "ctl.sock"),
        marker=str(tmp / "swarm-gate.started"),
        profile_dir=tmp / "profile",
        downloads_dir=tmp / "downloads",
        uploads_dir=tmp / "uploads",
        cdp_host="127.0.0.1",
        cdp_port=9222,
        relay_url="ws://relay.test:8080/v1/tunnel",
        fbdir=tmp,
    )


@pytest.fixture
def short_tmp():
    """A short directory: a unix socket path is limited to ~108 bytes."""
    path = Path(tempfile.mkdtemp(prefix="lg", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


class Rig:
    """A gate over fakes, recording what started and whether ctl.sock existed then."""

    def __init__(self, tmp: Path, *, relay_ready: bool = True) -> None:
        self.cfg = _config(tmp)
        self.calls = Calls()
        self.browser: FakeBrowser | None = None
        self.injector: FakeInjector | None = None
        self.display = FakeDisplay()
        self.framebuffer: FakeFramebuffer | None = None
        self.server: FakeServer | None = None
        self.relay = FakeRelaySocket(ready=relay_ready)
        self.relay_urls: list[str] = []
        self.deps = gate_main.Deps(
            start_xvfb=self.start_xvfb, open_injector=self.open_injector,
            start_chromium=self.start_chromium, serve_cdp=self.serve_cdp,
            open_display=lambda cfg: self.display, open_framebuffer=self.open_framebuffer,
            connect_relay=self.connect_relay,
        )
        self.gate = gate_main.LiveGate(self.cfg, self.deps)

    def _note(self, name: str) -> None:
        self.calls.append((name, os.path.exists(self.cfg.ctl_path)))

    async def start_xvfb(self, cfg):
        self._note("xvfb.start")
        return FakeChild(self.calls, "xvfb")

    async def open_injector(self, cfg, width, height):
        self._note("injector.open")
        self.injector = FakeInjector(self.calls)
        return self.injector

    async def start_chromium(self, cfg):
        self._note("chromium.start")
        self.browser = FakeBrowser(self.calls)
        return self.browser

    async def serve_cdp(self, proxy, host, port):
        self._note("cdp.serve")
        assert (host, port) == ("127.0.0.1", 9222)
        self.server = FakeServer(self.calls)
        return self.server

    def open_framebuffer(self, cfg):
        self._note("framebuffer.open")
        self.framebuffer = FakeFramebuffer(self.calls)
        return self.framebuffer

    async def connect_relay(self, url):
        self.relay_urls.append(url)
        return self.relay


async def _started(rig: Rig) -> tuple[asyncio.Event, asyncio.Task[int]]:
    stop = asyncio.Event()
    task = asyncio.create_task(gate_main.run(rig.cfg, stop=stop, gate=rig.gate))
    await asyncio.wait_for(rig.gate.ready.wait(), 5)
    return stop, task


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def _probe(ctl_path: str) -> list[str]:
    """The dispatcher's startup probe, pointed at this test's ctl.sock."""
    display = _rendered_display()
    command = list(display["startupProbe"]["exec"]["command"])
    env = {e["name"]: e["value"] for e in display["env"]}
    assert env["SWARM_GATE_CTL"] in command, "the probe no longer checks the gate's ctl.sock"
    return [ctl_path if part == env["SWARM_GATE_CTL"] else part for part in command]


def _probe_passes(command: list[str]) -> bool:
    if os.path.exists(command[0]):
        return subprocess.run(command, timeout=30).returncode == 0
    # `test -S <path>`, where coreutils' test is not at the image's path.
    assert command[1:2] == ["-S"], command
    return os.path.exists(command[2]) and stat.S_ISSOCK(os.stat(command[2]).st_mode)


# --------------------------------------------------------------------------
# Start, in order; the probe; SIGTERM
# --------------------------------------------------------------------------


def test_main_starts_the_components_in_the_designs_order(short_tmp):
    async def body() -> None:
        rig = Rig(short_tmp)
        stop, task = await _started(rig)
        assert rig.calls == [
            ("xvfb.start", False),
            ("injector.open", False),
            ("chromium.start", False),
            ("cdp.serve", False),
            ("framebuffer.open", True),
        ], "Xvfb, then Chromium and the CDP proxy, then ctl.sock, then the view"
        methods = rig.browser.transport.methods
        assert methods[0] == "Browser.getVersion", "Chromium answers on its pipe before anything else"
        # The mask is installed on a gate-only session on the page on screen.
        assert "Page.addScriptToEvaluateOnNewDocument" in methods
        assert rig.gate.pages.current == "PAGE1"
        # The default device was applied to the display.
        assert rig.display.sizes and rig.display.sizes[-1] == (1440, 987)
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_the_startup_probe_passes_only_once_ctl_sock_listens(short_tmp):
    async def body() -> None:
        rig = Rig(short_tmp)
        probe = _probe(rig.cfg.ctl_path)
        assert not _probe_passes(probe)
        stop, task = await _started(rig)
        assert await asyncio.to_thread(_probe_passes, probe)
        stop.set()
        assert await task == 0
        assert not _probe_passes(probe), "a stopped gate leaves no socket behind"

    asyncio.run(body())


def test_sigterm_shuts_every_piece_down(short_tmp):
    async def body() -> None:
        rig = Rig(short_tmp)
        task = asyncio.create_task(gate_main.run(rig.cfg, gate=rig.gate))
        await asyncio.wait_for(rig.gate.ready.wait(), 5)
        os.kill(os.getpid(), signal.SIGTERM)
        assert await asyncio.wait_for(task, 10) == 0
        names = rig.calls.names()
        for stopped in ("cdp.close", "injector.close", "chromium.stop", "xvfb.stop", "framebuffer.close"):
            assert stopped in names, stopped
        assert names.index("chromium.stop") < names.index("xvfb.stop"), "Chromium before its display"
        assert rig.browser.transport.closed
        assert not os.path.exists(rig.cfg.ctl_path)
        assert all(t.done() for t in rig.gate._tasks.values())

    asyncio.run(body())


def test_a_fatal_setup_error_is_one_line_and_exit_1(short_tmp, caplog):
    async def body() -> int:
        rig = Rig(short_tmp)

        async def no_xvfb(cfg):
            raise gate_main.SetupError("Xvfb is not installed at /usr/bin/Xvfb")

        rig.deps.start_xvfb = no_xvfb
        code = await gate_main.run(rig.cfg, gate=rig.gate)
        assert "chromium.start" not in rig.calls.names()
        assert not os.path.exists(rig.cfg.ctl_path), "the probe never passes for a gate that failed"
        return code

    with caplog.at_level(logging.INFO, logger="agent_worker.livegate"):
        assert asyncio.run(body()) == 1
    fatal = [r for r in caplog.records if "fatal" in r.getMessage()]
    assert len(fatal) == 1 and fatal[0].levelno == logging.ERROR
    assert "Xvfb is not installed" in fatal[0].getMessage()


def test_chromium_that_closes_its_pipe_is_fatal(short_tmp):
    async def body() -> int:
        rig = Rig(short_tmp)

        async def dead_chromium(cfg):
            browser = FakeBrowser(rig.calls)
            browser.transport.queue.put_nowait(None)  # the pipe ends at once

            async def nothing(message):
                pass

            browser.transport.send = nothing
            return browser

        rig.deps.start_chromium = dead_chromium
        return await gate_main.run(rig.cfg, gate=rig.gate)

    assert asyncio.run(body()) == 1


def test_a_restarted_gate_keeps_chromium_closed_and_refuses_registration(short_tmp):
    async def body() -> None:
        rig = Rig(short_tmp)
        Path(rig.cfg.marker).write_text("")
        stop, task = await _started(rig)
        assert rig.calls == [], "no display, no Chromium, no CDP proxy"
        client = CtlClient(rig.cfg.ctl_path)
        with pytest.raises(CtlRefused):
            await asyncio.to_thread(client.register)
        stop.set()
        assert await task == 0

    asyncio.run(body())


# --------------------------------------------------------------------------
# The worker's operations, over the real ctl.sock
# --------------------------------------------------------------------------


def test_the_worker_drives_the_gate_over_ctl_sock(short_tmp):
    async def body() -> None:
        rig = Rig(short_tmp)
        stop, task = await _started(rig)
        client = CtlClient(rig.cfg.ctl_path)
        await asyncio.to_thread(client.register)

        status = await asyncio.to_thread(client.call, "status", {})
        assert status["browser"] == "running" and status["tunnel"] == "none"
        assert status["paused"] is False and status["device"] == "desktop"

        export = await asyncio.to_thread(client.call, "session_export", {})
        assert export == {"jar": None}, "nothing to carry before a hand-off returned"

        device = await asyncio.to_thread(client.call, "device", {"preset": "iphone-15"})
        assert device["preset"] == "iphone-15"
        assert rig.injector.size == (rig.display.sizes[-1]), "the injector follows the display"

        ticket = "t." + secrets.token_urlsafe(24)
        opened = await asyncio.to_thread(client.call, "tunnel_ticket", {"ticket": ticket, "session_id": "ls_1"})
        assert opened == {"tunnel": "open"}
        assert rig.relay_urls == [rig.cfg.relay_url]
        assert rig.relay.texts == [ticket], "the ticket is the tunnel's first message"

        paused = await asyncio.to_thread(
            client.call, "pause", {"request_id": "r1", "expect": "example.test", "fresh_page": True})
        assert paused == {"paused": True, "fresh_page": True}
        assert "Target.createBrowserContext" in rig.browser.transport.methods

        resumed = await asyncio.to_thread(client.call, "resume", {"request_id": "r1"})
        assert resumed["url"] == "https://example.test/signin"
        assert (await asyncio.to_thread(client.call, "status", {}))["paused"] is False

        client.close()
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_a_refused_ticket_is_a_refused_operation(short_tmp):
    async def body() -> None:
        rig = Rig(short_tmp, relay_ready=False)
        stop, task = await _started(rig)
        client = CtlClient(rig.cfg.ctl_path)
        await asyncio.to_thread(client.register)
        with pytest.raises(CtlRefused):
            await asyncio.to_thread(client.call, "tunnel_ticket", {"ticket": "t." + secrets.token_urlsafe(8)})
        assert (await asyncio.to_thread(client.call, "status", {}))["tunnel"] == "none"
        client.close()
        stop.set()
        await task

    asyncio.run(body())


def test_a_viewer_sees_rfb_and_control_is_granted_then_released(short_tmp):
    async def body() -> None:
        rig = Rig(short_tmp)
        stop, task = await _started(rig)
        gate = rig.gate
        await gate.open_tunnel({"ticket": "t." + secrets.token_urlsafe(8)})

        rig.relay.push(tunnel.OP_OPEN, "v1")
        await _until(lambda: any(f[:2] == (tunnel.OP_DATA, "v1") for f in rig.relay.frames))
        data = [f for f in rig.relay.frames if f[:2] == (tunnel.OP_DATA, "v1")]
        assert data[0][2].startswith(PROTOCOL_VERSION), "the viewer's stream is RFB"
        assert gate.viewers == 1

        # Unasked control (Q6): the gate holds the agent itself.
        rig.relay.push(tunnel.OP_CONTROL, "v1")
        await _until(lambda: gate.control.controller == "v1")
        assert gate.proxy.paused and gate.proxy.in_control
        assert gate.status()["in_control"] is True

        rig.relay.push(tunnel.OP_CONTROL, "")
        await _until(lambda: gate.control.controller is None and not gate.proxy.paused)
        assert gate.last_episode["url"] == "https://example.test/signin"
        actions = [json.loads(f[2]) for f in rig.relay.frames if f[0] == tunnel.OP_ACTION]
        assert [a["event"] for a in actions] == ["granted", "ended"]
        assert rig.injector.released >= 1, "every key the controller held is released"

        rig.relay.push(tunnel.OP_CLOSE, "v1")
        await _until(lambda: gate.viewers == 0)
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_the_agents_mouse_events_move_the_pointer_watchers_see(short_tmp):
    """LB-CURSOR (part of #1030): the proxy's `on_pointer` is the gate's, and
    it lands in the hub's frame coordinates for the device on screen."""
    from agent_worker.livegate.pointer import css_to_frame

    async def body() -> None:
        rig = Rig(short_tmp)
        stop, task = await _started(rig)
        gate = rig.gate
        assert gate.hub.pointer.position is None
        gate.proxy._on_pointer(100.0, 50.0)
        assert gate.hub.pointer.position == css_to_frame(gate.devices.device, 100.0, 50.0)
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_one_take_on_about_blank_is_one_granted_line_and_no_refusal(short_tmp):
    """LB-R4 (part of #1030). MEASURED 2026-10-12: one take on the agent's page
    before its first navigation read "You took control" and then "Taking
    control was refused: the page is not on a secure https connection". The
    gate now grants it: one control line, `granted`, which the timeline folds
    into the audit's "You took control" (live.py `_DUPLICATED`)."""
    async def body() -> None:
        rig = Rig(short_tmp)
        stop, task = await _started(rig)
        rig.browser.transport.page_url = "about:blank"
        rig.browser.transport.security = "neutral"
        gate = rig.gate
        await gate.open_tunnel({"ticket": "t." + secrets.token_urlsafe(8)})
        rig.relay.push(tunnel.OP_OPEN, "v1")
        await _until(lambda: gate.viewers == 1)

        rig.relay.push(tunnel.OP_CONTROL, "v1")
        await _until(lambda: gate.control.controller == "v1")
        assert gate.proxy.in_control
        actions = [json.loads(f[2]) for f in rig.relay.frames if f[0] == tunnel.OP_ACTION]
        control = [a for a in actions if a.get("kind") == "control"]
        assert [(a["event"], a.get("cause")) for a in control] == [("granted", None)]

        rig.relay.push(tunnel.OP_CONTROL, "")
        await _until(lambda: gate.control.controller is None and not gate.proxy.paused)
        stop.set()
        assert await task == 0

    asyncio.run(body())


def _reports(rig: Rig) -> list[dict[str, Any]]:
    return [json.loads(f[2]) for f in rig.relay.frames if f[0] == tunnel.OP_REPORT]


def _navigated(rig: Rig, url: str, registrable: str, *, parent: str | None = None) -> None:
    frame: dict[str, Any] = {"id": "SUB1" if parent else "PAGE1", "url": url, "domainAndRegistry": registrable}
    if parent:
        frame["parentId"] = parent
    rig.browser.transport.queue.put_nowait({"method": "Page.frameNavigated", "sessionId": rig.gate.pages._session,
                                            "params": {"frame": frame, "type": "Navigation"}})


#: The report's `browser` (BSTATUS-API) before the worker has forwarded the
#: runner's tools or any tagged tool has connected, on a gate with no egress proxy.
NO_BROWSER_TOOLS = {"tools": None, "tool_last_used": None,
                    "egress": {"mode": "direct", "state": "off", "last_error": None}}


def test_the_gate_reports_the_top_frames_origin_and_the_device_over_its_tunnel(short_tmp):
    """LB-DEV-A: what the console's origin strip and device bar draw is what
    the gate read from Chromium -- the origin on every committed navigation of
    the top frame, the device on the tunnel's open and on every change --
    carried over the tunnel the gate already holds (`OP_REPORT`)."""
    async def body() -> None:
        rig = Rig(short_tmp)
        stop, task = await _started(rig)
        client = CtlClient(rig.cfg.ctl_path)
        await asyncio.to_thread(client.register)
        await asyncio.to_thread(client.call, "tunnel_ticket", {"ticket": "t." + secrets.token_urlsafe(8)})

        await _until(lambda: len(_reports(rig)) >= 1)
        assert _reports(rig)[-1] == {"origin": None,
                                     "device": {"preset": "desktop", "orientation": "landscape", "touch": False},
                                     # LIVE-UNUSED: no agent has connected, and no frame was published.
                                     "agent": {"connected_at": None, "connections": 0, "first_frame_at": None},
                                     # BSTATUS-API: nothing registered or used yet; no proxy, so direct.
                                     "browser": NO_BROWSER_TOOLS}

        _navigated(rig, "https://accounts.github.com/login?return_to=%2Fsettings", "github.com")
        await _until(lambda: _reports(rig)[-1]["origin"] is not None)
        assert _reports(rig)[-1]["origin"] == {
            "origin": "https://accounts.github.com", "host": "accounts.github.com",
            "display_host": "accounts.github.com", "registrable_domain": "github.com",
            "scheme": "https:", "ip_literal": False, "cert_error": None, "path": "/login"}
        sent = json.dumps(_reports(rig))
        assert "return_to" not in sent, "an origin and its path (LB-R6), never a query"

        # A child frame navigating is not the top frame's origin.
        count = len(_reports(rig))
        _navigated(rig, "https://evil.example/frame", "evil.example", parent="PAGE1")
        await asyncio.sleep(0.1)
        assert len(_reports(rig)) == count

        await asyncio.to_thread(client.call, "device", {"preset": "iphone-15", "orientation": "landscape"})
        await _until(lambda: _reports(rig)[-1]["device"]["preset"] == "iphone-15")
        assert _reports(rig)[-1] == {
            "origin": _reports(rig)[count - 1]["origin"],
            "device": {"preset": "iphone-15", "orientation": "landscape", "touch": True},
            "agent": {"connected_at": None, "connections": 0, "first_frame_at": None},
            "browser": NO_BROWSER_TOOLS}
        client.close()
        stop.set()
        assert await task == 0

    asyncio.run(body())


def _device_frame(**choice: Any) -> bytes:
    return json.dumps({"preset": None, "orientation": None, "touch": None, **choice}).encode()


def test_the_relays_device_frame_is_applied_and_reported_back(short_tmp):
    """LB-DEV-B: the person in control chose a device in the console; swarm-api
    checked their control and the relay handed the gate a `DEVICE` frame. The
    gate applies it through the same `DeviceController.select` as the ctl
    `device` operation -- the display is resized -- and reports the device it
    applied, which is how the session read learns it. Touch is the person's
    toggle: off is reported off; a rotate keeps it."""
    async def body() -> None:
        rig = Rig(short_tmp)
        stop, task = await _started(rig)
        await rig.gate.open_tunnel({"ticket": "t." + secrets.token_urlsafe(8)})
        await _until(lambda: len(_reports(rig)) >= 1)
        sizes = len(rig.display.sizes)

        rig.relay.push(tunnel.OP_DEVICE, "", _device_frame(preset="pixel-8", orientation="landscape"))
        await _until(lambda: _reports(rig)[-1]["device"]["preset"] == "pixel-8")
        assert _reports(rig)[-1]["device"] == {"preset": "pixel-8", "orientation": "landscape", "touch": True}
        assert len(rig.display.sizes) == sizes + 1, "the display follows the device"
        assert rig.gate.devices.device.name == "pixel-8"

        rig.relay.push(tunnel.OP_DEVICE, "", _device_frame(preset="pixel-8", orientation="landscape", touch=False))
        await _until(lambda: _reports(rig)[-1]["device"]["touch"] is False)
        assert rig.gate.touch.touch_on is False
        assert len(rig.display.sizes) == sizes + 1, "a touch toggle resizes nothing"

        rig.relay.push(tunnel.OP_DEVICE, "", _device_frame(preset="pixel-8", orientation="portrait"))
        await _until(lambda: _reports(rig)[-1]["device"]["orientation"] == "portrait")
        assert _reports(rig)[-1]["device"] == {"preset": "pixel-8", "orientation": "portrait", "touch": False}

        stop.set()
        assert await task == 0

    asyncio.run(body())


@pytest.mark.parametrize("payload", [
    _device_frame(preset="galaxy-fold"),
    _device_frame(preset="iphone-15", orientation="upside-down"),
    _device_frame(preset="desktop", touch=True),
    _device_frame(preset="iphone-15", touch="yes"),
    json.dumps({"preset": "iphone-15", "width": 1920, "height": 1080}).encode(),
    json.dumps({"preset": "iphone-15", "orientation": None, "touch": None, "scale": 3}).encode(),
    b"not json",
    pytest.param(b"x" * 4096, id="too-large"),
])
def test_a_device_frame_that_is_not_a_preset_by_name_changes_nothing(short_tmp, payload):
    async def body() -> None:
        rig = Rig(short_tmp)
        stop, task = await _started(rig)
        await rig.gate.open_tunnel({"ticket": "t." + secrets.token_urlsafe(8)})
        await _until(lambda: len(_reports(rig)) >= 1)
        sizes, reports = list(rig.display.sizes), len(_reports(rig))
        with pytest.raises((ValueError, RuntimeError)):
            await rig.gate.apply_device(payload)
        rig.relay.push(tunnel.OP_DEVICE, "", payload)
        await asyncio.sleep(0.1)
        assert rig.gate.devices.device.name == "desktop"
        assert rig.gate.touch.touch_on is True
        assert rig.display.sizes == sizes
        assert len(_reports(rig)) == reports, "nothing changed, so nothing is reported"
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_the_wipe_closes_chromium_and_empties_the_profile(short_tmp):
    async def body() -> None:
        rig = Rig(short_tmp)
        (rig.cfg.profile_dir / "Default").mkdir()
        (rig.cfg.profile_dir / "Default" / "Cookies").write_bytes(b"\0" * 16)
        (rig.cfg.profile_dir / "Local State").write_text("{}")
        stop, task = await _started(rig)
        client = CtlClient(rig.cfg.ctl_path)
        await asyncio.to_thread(client.register)
        result = await asyncio.to_thread(client.call, "end_control", {"cause": "attempt_end", "wipe": True})
        assert result == {"ended": True, "wiped": True}
        assert rig.browser.stopped and rig.server.closed
        assert list(rig.cfg.profile_dir.iterdir()) == []
        assert (await asyncio.to_thread(client.call, "status", {}))["browser"] == "wiped"
        with pytest.raises(CtlRefused):
            await asyncio.to_thread(client.call, "device", {"preset": "desktop"})
        client.close()
        stop.set()
        assert await task == 0

    asyncio.run(body())


# --------------------------------------------------------------------------
# The tunnel against the REAL relay app, on loopback
# --------------------------------------------------------------------------


def test_the_gates_tunnel_speaks_to_the_real_relay(short_tmp):
    """tunnel.py's websocket client and frames, against swarm_live.main's app under
    uvicorn: the ticket is admitted, a viewer's stream is OPENed on the gate, and
    bytes cross both ways. A forged ticket is refused."""
    uvicorn = pytest.importorskip("uvicorn")
    websockets = pytest.importorskip("websockets")
    app_dir = str(REPO / "apps" / "swarm-live")
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    from swarm_live.main import create_app
    from swarm_live.rendezvous import Rendezvous, mint_ticket

    key = secrets.token_bytes(32)
    ids = {"session_id": "ls_" + secrets.token_hex(4), "tenant_id": "t1", "task_id": "task_1",
           "attempt_id": "att_1", "generation": 3}

    async def body() -> None:
        server = uvicorn.Server(uvicorn.Config(create_app(Rendezvous(key)), host="127.0.0.1", port=0,
                                               log_level="warning", lifespan="off"))
        serving = asyncio.create_task(server.serve())
        await _until(lambda: server.started)
        port = server.servers[0].sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/v1/tunnel"
        try:
            forged = tunnel.Tunnel(url)
            with pytest.raises(tunnel.TunnelError):
                await forged.open(mint_ticket(secrets.token_bytes(32), role="tunnel", **ids))

            gate_side = tunnel.Tunnel(url)
            await gate_side.open(mint_ticket(key, role="tunnel", **ids))
            received: list[bytes] = []
            opened = asyncio.Event()

            async def serve(conn_id: str, reader: asyncio.StreamReader, writer: tunnel.TunnelWriter) -> None:
                opened.set()
                writer.write(b"hello from the gate " + b"x" * 70_000)  # a 64-bit length frame
                await writer.drain()
                received.append(await reader.readexactly(5))

            async def no_control(conn_id: str) -> None:
                pass

            mux = tunnel.StreamMux(gate_side, serve, no_control)
            running = asyncio.create_task(gate_side.run(mux.on_frame))

            async with websockets.connect(f"ws://127.0.0.1:{port}/v1/view") as viewer:
                await viewer.send(mint_ticket(key, role="viewer", email="p@example.test", **ids))
                assert json.loads(await viewer.recv()) == {"type": "ready"}
                await asyncio.wait_for(opened.wait(), 5)
                got = await asyncio.wait_for(viewer.recv(), 5)
                assert got.startswith(b"hello from the gate") and len(got) == 20 + 70_000
                await viewer.send(b"hello")
                await _until(lambda: received == [b"hello"])

            await gate_side.close()
            await asyncio.wait_for(running, 5)
            await mux.close()
        finally:
            server.should_exit = True
            await serving

    asyncio.run(body())
