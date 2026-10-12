"""`python -m agent_worker.livegate`: the display sidecar's one process (live browser L3m).

docs/design/live-browser.md §3.1, §3.2, §3.3 and §5.4. The scheduler runs this
as the `display` native sidecar of every live-browser pod
(`scheduler.dispatch.DISPLAY_COMMAND`, under tini). Each component beside it
was built by its own lane; this module is the process that composes them, in
this order:

1. THE DISPLAY. Xvfb on `:0`, listening only on its unix socket in the
   sidecar's own /tmp (`-nolisten tcp -nolisten local`: the second closes
   Linux's abstract-namespace socket, which lives in the pod's NETWORK
   namespace and so would be reachable from the worker container, agent
   included, with no X authorisation), with its framebuffer file in /tmp
   (`-fbdir`, capture.py) and a screen as large as the largest device preset
   (device.py: RandR shrinks a screen, never grows it). Then the X
   connection input is injected through (inject.py).
2. THE BROWSER. Headed Chromium on that display, launched with
   `--remote-debugging-pipe` (no debugging port at all) and the profile on
   the sidecar-only volume; the gate holds the pipe (cdpproxy.PipeTransport)
   and serves the agent the CDP proxy on 127.0.0.1:9222 (`serve_agents`,
   under the method policy of cdppolicy.py).
3. THE WORKER'S CHANNEL. ctl.sock under /run/swarm-gate (ctl.py), answering
   the worker's MAC'd operations with the device presets (device.py,
   presets.py), touch (touch.py) and the session carry (session.py). The
   dispatcher's startup probe is `test -S <ctl.sock>`, so the worker starts
   only once this socket exists, and its registration is the first (§5.4.2).
4. THE VIEW. The capture loop (capture.py) paints every frame with the mask
   (mask.py, observer.js, on a gate-only CDP session on the page on screen)
   before the RFB server (rfb.py) may publish it; the tunnel to the relay
   (tunnel.py) opens when the worker hands over a ticket, and each viewer is
   an RFB stream through it, with the controller's input injected only in
   control mode.

A GATE THAT RESTARTS keeps Chromium closed (ctl.py): when the first start's
marker is already in /tmp, only ctl.sock is served -- it refuses every
registration and records `gate_restarted` -- and nothing else starts.

FATAL SETUP ERRORS end the process with one `live gate: fatal: ...` line and
exit 1: a missing binary, Xvfb or Chromium exiting or not answering, a port or
socket that cannot be bound. SIGTERM (the kubelet stopping the native sidecar
after the worker exits) shuts down in reverse: the tunnel, ctl.sock, the CDP
proxy, the capture, Chromium, Xvfb; exit 0.

NOTHING CARRIED HERE IS LOGGED: the ticket, a page's content, a key, a URL.
Records (`_record`) carry event names, causes and counts, as every component's
own contract says.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import glob
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .capture import CaptureError, XvfbFramebuffer
from .cdppolicy import PolicyPaths
from .cdpproxy import AgentConnection, AgentLink, CdpProxy, PipeTransport, serve_agents
from .ctl import DEFAULT_MARKER, CtlServer
from .device import DeviceController, XRandrDisplay, crop
from .inject import InjectError, XInjector, display_socket
from .mask import MaskCdp, MaskState
from .presets import DEFAULT_PRESET, MAX_DISPLAY_HEIGHT, MAX_DISPLAY_WIDTH, resolve
from .rfb import ControlState, FrameHub, RfbSession, masked
from .session import OPS as SESSION_OPS
from .session import SessionCarry
from .touch import TouchInjector
from .tunnel import StreamMux, Tunnel, TunnelError, TunnelWriter, relay_url

log = logging.getLogger("agent_worker.livegate")

#: Where the image puts Playwright's Chromium (images/agent-runtime-browser/Dockerfile).
DEFAULT_BROWSERS_PATH = "/opt/playwright"
XVFB = "/usr/bin/Xvfb"

#: How long each piece of setup may take before it is a fatal error. Xvfb and
#: Chromium start in about a second; the probe allows the whole sidecar 120 s
#: (dispatch.py `failureThreshold`), so these leave room for both and the rest.
XVFB_READY_SECONDS = 30.0
CHROMIUM_READY_SECONDS = 45.0
#: Under the worker's ctl.sock timeout (CtlClient, 10 s), so a relay that does
#: not answer is a refused `tunnel_ticket`, not a lost channel.
TUNNEL_OPEN_SECONDS = 8.0
#: Frames per second while anyone (a viewer, the recorder) is watching.
CAPTURE_FPS = 10
#: The longest ticket the relay accepts (swarm_live.rendezvous MAX_TICKET_CHARS).
MAX_TICKET_CHARS = 4096

#: A hand-off's `expect`: a host (the worker checked it with `expect_refusal`).
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?")


class SetupError(Exception):
    """A fatal setup error. Its message is the one log line; it carries no content."""


def _record(event: str, fields: Mapping[str, Any]) -> None:
    """Every component's `record`: an event name and its causes, never content."""
    log.info("live gate: record=%s %s", event,
             json.dumps({str(k): str(v) for k, v in fields.items()}, sort_keys=True))


# --------------------------------------------------------------------------
# Configuration: the sidecar's environment (scheduler.dispatch.with_live_display)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GateConfig:
    ctl_path: str
    profile_dir: Path
    downloads_dir: Path
    uploads_dir: Path
    cdp_host: str
    cdp_port: int
    relay_url: str
    display: str = ":0"
    marker: str = DEFAULT_MARKER
    #: Xvfb's `-fbdir`: the sidecar's own /tmp (capture.py).
    fbdir: Path = Path("/tmp")
    xvfb: str = XVFB
    browsers_path: str = DEFAULT_BROWSERS_PATH

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "GateConfig":
        names = ("SWARM_GATE_CTL", "SWARM_BROWSER_PROFILE_DIR", "SWARM_BROWSER_DOWNLOADS",
                 "SWARM_BROWSER_UPLOADS", "SWARM_CDP_LISTEN")
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise SetupError(f"the sidecar's environment lacks {', '.join(missing)}")
        host, _, port = env["SWARM_CDP_LISTEN"].rpartition(":")
        if host not in ("127.0.0.1", "localhost") or not port.isdigit():
            raise SetupError("SWARM_CDP_LISTEN must be a loopback host:port")
        try:
            url = relay_url(env)
        except ValueError as exc:
            raise SetupError(str(exc)) from None
        return cls(
            ctl_path=env["SWARM_GATE_CTL"],
            profile_dir=Path(env["SWARM_BROWSER_PROFILE_DIR"]),
            downloads_dir=Path(env["SWARM_BROWSER_DOWNLOADS"]),
            uploads_dir=Path(env["SWARM_BROWSER_UPLOADS"]),
            cdp_host=host,
            cdp_port=int(port),
            relay_url=url,
            display=env.get("DISPLAY") or ":0",
            browsers_path=env.get("PLAYWRIGHT_BROWSERS_PATH") or DEFAULT_BROWSERS_PATH,
        )


# --------------------------------------------------------------------------
# Processes: Xvfb and Chromium
# --------------------------------------------------------------------------


async def _stop_process(proc: Any, timeout: float = 5.0) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()


class Child:
    """A started process the gate owns (Xvfb)."""

    def __init__(self, proc: Any) -> None:
        self.proc = proc

    async def stop(self) -> None:
        await _stop_process(self.proc)


class Browser:
    """Chromium and the pipe the gate holds to it."""

    def __init__(self, proc: Any, transport: Any) -> None:
        self.proc = proc
        self.transport = transport

    async def stop(self) -> None:
        try:
            await self.transport.close()
        finally:
            await _stop_process(self.proc)


async def start_xvfb(cfg: GateConfig) -> Child:
    """Xvfb on `cfg.display`, on its unix socket only, at the largest preset's size."""
    if not os.access(cfg.xvfb, os.X_OK):
        raise SetupError(f"Xvfb is not installed at {cfg.xvfb}")
    socket_path = Path(display_socket(cfg.display))
    screen = cfg.fbdir / "Xvfb_screen0"
    proc = await asyncio.create_subprocess_exec(
        cfg.xvfb, cfg.display,
        "-screen", "0", f"{MAX_DISPLAY_WIDTH}x{MAX_DISPLAY_HEIGHT}x24",
        "-fbdir", str(cfg.fbdir),
        "-nolisten", "tcp", "-nolisten", "local",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
    )
    deadline = time.monotonic() + XVFB_READY_SECONDS
    while not (socket_path.is_socket() and screen.exists()):
        if proc.returncode is not None:
            raise SetupError(f"Xvfb exited with code {proc.returncode} before its socket appeared")
        if time.monotonic() > deadline:
            await _stop_process(proc)
            raise SetupError(f"Xvfb did not open {socket_path} within {XVFB_READY_SECONDS:.0f}s")
        await asyncio.sleep(0.1)
    return Child(proc)


def find_chromium(browsers_path: str) -> str:
    """Playwright's headed Chromium (not its headless shell), the newest installed."""
    found = sorted(glob.glob(os.path.join(browsers_path, "chromium-*", "chrome-linux*", "chrome")))
    if not found:
        raise SetupError(f"no Chromium under {browsers_path}")
    return found[-1]


def chromium_args(cfg: GateConfig) -> list[str]:
    """Headed, on the pipe, profile on the sidecar-only volume, one window (§3.1).

    `--no-sandbox`: the image's own reasoning (Chromium's sandbox needs user
    namespaces and CAP_SYS_ADMIN, which the sidecar drops). No `--kiosk`, no
    `--app`: the address bar is part of the anti-phishing design (§5.7). The
    enterprise policy of §5.6 is read from /etc/chromium/policies/managed.
    """
    desktop = resolve(DEFAULT_PRESET)
    return [
        "--remote-debugging-pipe",
        f"--user-data-dir={cfg.profile_dir}",
        "--no-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        "--password-store=basic",
        "--window-position=0,0",
        f"--window-size={desktop.display_width},{desktop.display_height}",
        "about:blank",
    ]


async def start_chromium(cfg: GateConfig) -> Browser:
    """Chromium reading commands on its fd 3 and writing on its fd 4 (`--remote-debugging-pipe`)."""
    binary = find_chromium(cfg.browsers_path)
    to_chrome_r, to_chrome_w = os.pipe()
    from_chrome_r, from_chrome_w = os.pipe()
    # The child's ends, moved above 4 so the dup2 onto 3 and 4 below cannot
    # overwrite one with the other.
    child_in = fcntl.fcntl(to_chrome_r, fcntl.F_DUPFD_CLOEXEC, 10)
    child_out = fcntl.fcntl(from_chrome_w, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(to_chrome_r)
    os.close(from_chrome_w)

    def remap() -> None:  # in the child, before exec
        os.dup2(child_in, 3)
        os.dup2(child_out, 4)

    try:
        # close_fds=False: every descriptor Python opens is non-inheritable
        # (PEP 446), so only 0-2 and the two dup2'd above reach Chromium.
        proc = await asyncio.create_subprocess_exec(
            binary, *chromium_args(cfg),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, preexec_fn=remap, close_fds=False,
        )
    except OSError as exc:
        for fd in (to_chrome_w, from_chrome_r):
            os.close(fd)
        raise SetupError(f"Chromium could not be started: {exc.strerror or type(exc).__name__}") from None
    finally:
        os.close(child_in)
        os.close(child_out)
    transport = PipeTransport(read_fd=from_chrome_r, write_fd=to_chrome_w)
    await transport.open()
    return Browser(proc, transport)


async def open_injector(cfg: GateConfig, width: int, height: int) -> XInjector:
    try:
        return await XInjector.open(display_socket(cfg.display), width=width, height=height)
    except InjectError as exc:
        raise SetupError(f"the X injector could not connect: {exc}") from None


def open_framebuffer(cfg: GateConfig) -> XvfbFramebuffer:
    fb = XvfbFramebuffer(cfg.fbdir / "Xvfb_screen0")
    try:
        fb.grab()
    except (CaptureError, OSError) as exc:
        fb.close()
        raise SetupError(f"Xvfb's framebuffer cannot be captured: {exc}") from None
    return fb


def open_display(cfg: GateConfig) -> XRandrDisplay:
    return XRandrDisplay(display_socket(cfg.display))


async def serve_cdp(proxy: CdpProxy, host: str, port: int) -> Any:
    try:
        return await serve_agents(proxy, host=host, port=port)
    except OSError as exc:
        raise SetupError(f"the CDP proxy cannot listen on {host}:{port}: {exc.strerror}") from None


@dataclass
class Deps:
    """What touches the operating system. Tests replace each with a fake."""

    start_xvfb: Callable[[GateConfig], Awaitable[Any]] = start_xvfb
    open_injector: Callable[[GateConfig, int, int], Awaitable[Any]] = open_injector
    start_chromium: Callable[[GateConfig], Awaitable[Any]] = start_chromium
    serve_cdp: Callable[[CdpProxy, str, int], Awaitable[Any]] = serve_cdp
    open_display: Callable[[GateConfig], Any] = open_display
    open_framebuffer: Callable[[GateConfig], Any] = open_framebuffer
    #: The tunnel's connector; None is tunnel.WebSocketClient.connect.
    connect_relay: Callable[[str], Awaitable[Any]] | None = None


# --------------------------------------------------------------------------
# Glue the components did not have
# --------------------------------------------------------------------------


class GateProxy(CdpProxy):
    """The proxy, remembering whether an agent has ever connected (SessionCarry asks)."""

    agent_seen = False

    async def connect_agent(self, link: AgentLink) -> AgentConnection | None:
        connection = await super().connect_agent(link)
        if connection is not None:
            self.agent_seen = True
        return connection


class PageFollower:
    """Which page is on screen, and the mask's gate-only session on it.

    The display holds one window (§3.1) whose visible tab is the one opened
    last, so the mask follows the newest page and falls back to the newest
    one left when it closes. Between pages the mask's session is down, so
    every frame is the blank "masking unavailable" frame: it fails closed.

    The mask's handlers await replies (`MaskCdp.link`), and the pipe reader
    must never wait on a reply (cdpproxy.py), so its events are queued here
    and handled on a task of their own, in order.
    """

    def __init__(self, cdp: CdpProxy, state: MaskState) -> None:
        self._cdp = cdp
        self._state = state
        self.pages: list[str] = []
        self.current: str | None = None
        self._session: str | None = None
        self._mask: MaskCdp | None = None
        self._queue: asyncio.Queue[tuple[str, str, dict[str, Any]]] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._worker = asyncio.create_task(self._drain())
        targets = (await self._cdp.call("Target.getTargets", {})).get("targetInfos") or []
        for info in targets:
            if info.get("type") == "page" and isinstance(info.get("targetId"), str):
                if info["targetId"] not in self.pages:
                    self.pages.append(info["targetId"])
        if self.pages:
            await self.follow(self.pages[-1])

    def on_event(self, message: Mapping[str, Any]) -> None:
        """A message from Chromium's pipe. Never waits."""
        method = message.get("method")
        raw = message.get("params")
        params: dict[str, Any] = raw if isinstance(raw, dict) else {}
        session = message.get("sessionId")
        if not isinstance(method, str):
            return
        if session is not None:
            if session == self._session:
                self._queue.put_nowait((session, method, params))
            return
        if method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            target = info.get("targetId")
            if info.get("type") == "page" and isinstance(target, str) and target not in self.pages:
                self.pages.append(target)
                self._spawn(self.follow(target))
        elif method == "Target.targetDestroyed":
            target = params.get("targetId")
            if target in self.pages:
                self.pages.remove(target)
                if target == self.current:
                    self._spawn(self.follow(self.pages[-1] if self.pages else None))
        elif method == "Target.detachedFromTarget" and params.get("sessionId") == self._session:
            self._session = None
            self._mask = None
            self._state.session_down()

    def _spawn(self, work: Awaitable[None]) -> None:
        task = asyncio.get_running_loop().create_task(self._quietly(work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _quietly(self, work: Awaitable[None]) -> None:
        try:
            await work
        except Exception as exc:  # a page that closed before the attach answered
            log.info("live gate: mask follow failed (%s)", type(exc).__name__)

    async def follow(self, target: str | None) -> None:
        async with self._lock:
            if target is not None and target == self.current and self._session is not None:
                return
            old, old_session = self.current, self._session
            self._state.session_down()
            self._session = None
            self._mask = None
            if old is not None:
                # A page's main frame id is its target id.
                self._state.frame_detached(old)
            if old_session is not None:
                try:
                    await self._cdp.call("Target.detachFromTarget", {"sessionId": old_session})
                except Exception:  # noqa: BLE001 -- already gone with its page
                    pass
            self.current = target
            if target is None:
                return
            result = await self._cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})
            session = str(result["sessionId"])
            self._session = session

            async def send(method: str, params: dict[str, Any]) -> dict[str, Any]:
                return await self._cdp.call(method, params, session)

            self._mask = MaskCdp(self._state, send)
            await self._mask.install()

    async def _drain(self) -> None:
        while True:
            session, method, params = await self._queue.get()
            mask = self._mask
            if mask is None or session != self._session:
                continue
            try:
                await mask.on_event(method, params)
            except Exception as exc:  # noqa: BLE001 -- one event; the mask fails closed
                log.info("live gate: mask event failed (%s)", type(exc).__name__)

    async def close(self) -> None:
        tasks = [*self._tasks, *([self._worker] if self._worker else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._state.session_down()


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


@dataclass
class _Episode:
    """One person's control of one page, from grant to release."""

    target: str
    since: float
    conn_id: str


class LiveGate:
    def __init__(self, cfg: GateConfig, deps: Deps | None = None, *,
                 record: Callable[[str, Mapping[str, Any]], None] = _record) -> None:
        self.cfg = cfg
        self.deps = deps or Deps()
        self.record = record
        self.desktop = resolve(DEFAULT_PRESET)
        self.mask_state = MaskState()
        self.hub = FrameHub(self.desktop.display_width, self.desktop.display_height)
        #: Set once ctl.sock listens: the dispatcher's startup probe condition.
        self.ready = asyncio.Event()
        self.restarted = False
        self.xvfb: Any = None
        self.injector: Any = None
        self.browser: Any = None
        self.proxy: GateProxy | None = None
        self.devices: DeviceController | None = None
        self.touch: TouchInjector | None = None
        self.control: ControlState | None = None
        self.carry: SessionCarry | None = None
        self.pages: PageFollower | None = None
        self.cdp_server: Any = None
        self.ctl: CtlServer | None = None
        self.framebuffer: Any = None
        self.tunnel: Tunnel | None = None
        self.mux: StreamMux | None = None
        self.wiped = False
        #: Chromium's pipe closed (a wipe, or a crash): nothing needing it is served.
        self.browser_closed = False
        self.viewers = 0
        self.last_episode: dict[str, Any] | None = None
        self._episode: _Episode | None = None
        self._had_control = False
        self._self_paused = False
        self._fresh_target: str | None = None
        self._watching = asyncio.Event()
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._spawned: set[asyncio.Task[Any]] = set()

    # -- start, in the design's order ---------------------------------------

    async def start(self) -> None:
        # §5.4.2: a restart keeps Chromium closed. CtlServer.start makes the
        # marker and records the restart; this only decides what else starts.
        self.restarted = os.path.lexists(self.cfg.marker)
        if self.restarted:
            await self.start_ctl()
            return
        await self.start_display()
        await self.start_browser()
        await self.start_ctl()
        await self.start_view()

    async def start_display(self) -> None:
        """1. Xvfb on :0, and the X connection input is injected through."""
        self.xvfb = await self.deps.start_xvfb(self.cfg)
        self.injector = await self.deps.open_injector(
            self.cfg, self.desktop.display_width, self.desktop.display_height)
        log.info("live gate: display %s up", self.cfg.display)

    async def start_browser(self) -> None:
        """2. Headed Chromium on the pipe, and the CDP proxy on 127.0.0.1:9222."""
        self.browser = await self.deps.start_chromium(self.cfg)
        self.proxy = GateProxy(
            self.browser.transport.send,
            paths=PolicyPaths(downloads_dir=self.cfg.downloads_dir, uploads_dir=self.cfg.uploads_dir),
            record=self.record,
            on_control_ended=self._control_ended,
        )
        pipe = self._tasks["pipe"] = asyncio.create_task(self._read_pipe())
        version = asyncio.create_task(self.proxy.call("Browser.getVersion", {}))
        done, _ = await asyncio.wait({pipe, version}, timeout=CHROMIUM_READY_SECONDS,
                                     return_when=asyncio.FIRST_COMPLETED)
        if version not in done:
            version.cancel()
            if pipe in done:
                raise SetupError("Chromium closed its pipe before answering")
            raise SetupError(f"Chromium did not answer on its pipe within {CHROMIUM_READY_SECONDS:.0f}s")
        if version.exception() is not None:
            raise SetupError(f"Chromium refused Browser.getVersion ({type(version.exception()).__name__})")
        self.devices = DeviceController(self.proxy, self.deps.open_display(self.cfg), self.mask_state,
                                        record=self.record, on_resize=self._on_resize)
        self.touch = TouchInjector(self.injector, self.proxy, self.devices)
        self.control = ControlState(self.touch)
        proxy = self.proxy
        self.carry = SessionCarry(proxy.call, record=self.record, agent_connected=lambda: proxy.agent_seen)
        try:
            await self.devices.start()
        except Exception as exc:  # noqa: BLE001 -- the desktop still works; the mask fails closed
            self.record("device_failed", {"preset": DEFAULT_PRESET, "cause": type(exc).__name__})
            log.warning("live gate: default device not applied (%s)", type(exc).__name__)
        self.pages = PageFollower(self.proxy, self.mask_state)
        await self.pages.start()
        self.cdp_server = await self.deps.serve_cdp(self.proxy, self.cfg.cdp_host, self.cfg.cdp_port)
        log.info("live gate: chromium up; CDP proxy on %s:%d", self.cfg.cdp_host, self.cfg.cdp_port)

    async def start_ctl(self) -> None:
        """3. ctl.sock: the probe's condition, and the worker's channel."""
        self.ctl = CtlServer(self.cfg.ctl_path, self.cfg.marker, self.handle_ctl, self.record)
        try:
            await self.ctl.start()
        except OSError as exc:
            raise SetupError(f"ctl.sock cannot listen at {self.cfg.ctl_path}: {exc.strerror}") from None
        self.restarted = self.restarted or self.ctl.restarted
        self.ready.set()
        log.info("live gate: ctl.sock listening%s", " (restarted: refusing every registration)"
                 if self.ctl.restarted else "")

    async def start_view(self) -> None:
        """4. Capture -> mask -> RFB; the tunnel opens on the worker's ticket."""
        self.framebuffer = self.deps.open_framebuffer(self.cfg)
        self._tasks["capture"] = asyncio.create_task(self._capture())

    # -- the pipe ---------------------------------------------------------------

    async def _read_pipe(self) -> None:
        await self.browser.transport.run(self)
        if not self.wiped:
            log.warning("live gate: chromium closed its pipe")
        self.browser_closed = True
        self.mask_state.session_down()

    async def on_chromium_message(self, message: Mapping[str, Any]) -> None:
        """PipeTransport's sink: the proxy first, then the gate's own listeners."""
        assert self.proxy is not None
        await self.proxy.on_chromium_message(message)
        if "method" in message:
            if self.devices is not None:
                self.devices.on_event(message)
            if self.pages is not None:
                self.pages.on_event(message)

    # -- capture ------------------------------------------------------------

    async def _capture(self) -> None:
        interval = 1.0 / CAPTURE_FPS
        failing = False
        while True:
            await self._watching.wait()
            try:
                frame = self.framebuffer.grab()
            except CaptureError as exc:
                if not failing:
                    log.warning("live gate: capture failed (%s); nothing is sent", exc)
                failing = True
                await asyncio.sleep(interval)
                continue
            failing = False
            device = self.devices.device if self.devices is not None else self.desktop
            frame = crop(frame, device.display_width, device.display_height)
            self.hub.publish(masked(frame, self.mask_state))
            await asyncio.sleep(interval)

    def _on_resize(self, device: Any) -> None:
        if self.touch is not None:
            self.touch.resize(device.display_width, device.display_height)

    # -- ctl.sock -------------------------------------------------------------

    async def handle_ctl(self, op: str, args: dict[str, Any]) -> Mapping[str, Any]:
        if op == "status":
            return self.status()
        if op in SESSION_OPS:
            return await self._need(self.carry).handle(op, args)
        if op == "tunnel_ticket":
            return await self.open_tunnel(args)
        if op == "pause":
            return await self.pause(args)
        if op == "resume":
            return await self.resume()
        if op == "end_control":
            return await self.end_control(args)
        if op == "device":
            return await self._need(self.devices).handle_ctl(args)
        raise ValueError("not an operation of the gate")

    def _need(self, component: Any) -> Any:
        if component is None or self.wiped or self.browser_closed:
            raise RuntimeError("the browser is not running")
        return component

    def status(self) -> dict[str, Any]:
        device = self.devices.device if self.devices is not None else self.desktop
        if self.tunnel is None:
            tunnel = "none"
        else:
            tunnel = "closed" if self.tunnel.closed else "open"
        return {
            "browser": "wiped" if self.wiped else (
                "running" if self.browser is not None and not self.browser_closed else "closed"),
            "paused": bool(self.proxy is not None and self.proxy.paused),
            "in_control": self._episode is not None,
            "tunnel": tunnel,
            "viewers": self.viewers,
            "device": device.name,
            "orientation": device.orientation,
            "last_episode": self.last_episode,
        }

    async def open_tunnel(self, args: Mapping[str, Any]) -> dict[str, Any]:
        ticket = args.get("ticket")
        if not isinstance(ticket, str) or not ticket or len(ticket) > MAX_TICKET_CHARS:
            raise ValueError("no ticket")
        if self.tunnel is not None and not self.tunnel.closed:
            raise RuntimeError("the tunnel is already open")
        self._need(self.proxy)
        tunnel = Tunnel(self.cfg.relay_url, connect=self.deps.connect_relay)
        try:
            await asyncio.wait_for(tunnel.open(ticket), TUNNEL_OPEN_SECONDS)
        except (TunnelError, asyncio.TimeoutError) as exc:
            cause = str(exc) if isinstance(exc, TunnelError) else "timeout"
            self.record("tunnel_refused", {"cause": cause})
            await tunnel.close()
            raise
        self.tunnel = tunnel
        self.mux = StreamMux(tunnel, self._serve_rfb, self._on_control)
        self._tasks["tunnel"] = asyncio.create_task(self._run_tunnel(tunnel, self.mux))
        self.record("tunnel_open", {})
        return {"tunnel": "open"}

    async def _run_tunnel(self, tunnel: Tunnel, mux: StreamMux) -> None:
        code: int | None = None
        try:
            code = await tunnel.run(mux.on_frame)
        finally:
            await mux.close()
            if self._episode is not None:
                await self._release("tunnel_closed")
            self.record("tunnel_closed", {"code": code})

    async def _serve_rfb(self, conn_id: str, reader: asyncio.StreamReader, writer: TunnelWriter) -> None:
        assert self.control is not None and self.touch is not None
        self.viewers += 1
        self._watching.set()
        try:
            await RfbSession(conn_id, reader, writer, self.hub, self.control, self.touch).serve()  # type: ignore[arg-type]
        finally:
            self.viewers -= 1
            if self.viewers == 0:
                self._watching.clear()

    # -- control (§4.2) -------------------------------------------------------

    async def _on_control(self, conn_id: str) -> None:
        """The relay's CONTROL: swarm-api granted `conn_id` control, or none (empty)."""
        if conn_id:
            await self._grant(conn_id)
        else:
            await self._release("released")

    async def _grant(self, conn_id: str) -> None:
        proxy, control, touch = self.proxy, self.control, self.touch
        if proxy is None or control is None or touch is None or self.wiped or self.browser_closed:
            return await self._refused("browser_closed")
        if not proxy.paused:
            # §4.2 (Q6): a person may take control unasked; the agent is
            # held the same way, and released when they let go.
            await proxy.pause()
            self._self_paused = True
        target = (self._episode.target if self._episode is not None
                  else self._fresh_target or (self.pages.current if self.pages else None))
        if target is None:
            return await self._refused("no_page")
        decision = await proxy.take_control(target)
        if not decision.granted:
            return await self._refused(decision.cause or "refused")
        await control.grant(conn_id)
        touch.begin(target)
        if self._episode is None:
            self._episode = _Episode(target=target, since=time.monotonic(), conn_id=conn_id)
        else:
            self._episode.conn_id = conn_id
        self._had_control = True
        await self._action({"kind": "control", "event": "granted"})

    async def _refused(self, cause: str) -> None:
        await self._action({"kind": "control", "event": "refused", "cause": cause})
        if self._self_paused and self._episode is None and self.proxy is not None:
            self._self_paused = False
            await self.proxy.resume()

    async def _release(self, cause: str) -> None:
        """Control ends: no input reaches the display from here."""
        if self.control is not None:
            await self.control.revoke()
        if self.touch is not None:
            await self.touch.end()
        if self.proxy is not None and self.proxy.in_control:
            self.proxy.end_control(cause)
        episode, self._episode = self._episode, None
        if episode is None:
            return
        page = await self._page_info(episode.target)
        self.last_episode = {"held_seconds": int(time.monotonic() - episode.since), **page}
        await self._action({"kind": "control", "event": "ended", "cause": cause})
        if self._self_paused and self.proxy is not None and not self.wiped:
            # Unasked control (Q6): nobody else will resume the agent.
            self._self_paused = False
            await self._resume_agent()

    def _control_ended(self, cause: str) -> None:
        """The proxy ended control itself (an insecure page, the page closed)."""
        task = asyncio.get_running_loop().create_task(self._release(cause))
        self._spawned.add(task)
        task.add_done_callback(self._spawned.discard)

    async def _page_info(self, target: str) -> dict[str, Any]:
        if self.proxy is None or self.wiped:
            return {}
        try:
            info = (await self.proxy.call("Target.getTargetInfo", {"targetId": target})).get("targetInfo") or {}
        except Exception:  # noqa: BLE001 -- the page closed
            return {}
        return {"url": str(info.get("url", "")), "title": str(info.get("title", ""))}

    async def _action(self, entry: Mapping[str, Any]) -> None:
        if self.tunnel is not None and not self.tunnel.closed:
            await self.tunnel.action(entry)

    async def _resume_agent(self) -> None:
        assert self.proxy is not None
        await self.proxy.resume()
        if self._had_control and self.carry is not None:
            self.carry.note_returned()
        self._had_control = False
        self._fresh_target = None

    async def pause(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """§4.1 step 2: hold the agent; open the hand-off's site on a fresh page."""
        proxy = self._need(self.proxy)
        self._self_paused = False
        await proxy.pause()
        expect = args.get("expect")
        if isinstance(expect, str) and expect and _HOST.fullmatch(expect):
            try:
                self._fresh_target = await proxy.open_fresh_page(f"https://{expect}/")
                if self.devices is not None:
                    await self.devices.attach_page(self._fresh_target)
            except Exception as exc:  # noqa: BLE001 -- the person can still use the agent's page
                self._fresh_target = None
                self.record("fresh_page_failed", {"cause": type(exc).__name__})
        return {"paused": True, "fresh_page": self._fresh_target is not None}

    async def resume(self) -> dict[str, Any]:
        """§4.2 step 3: clear, restore, release the agent; what page it is on."""
        proxy = self._need(self.proxy)
        target = self._episode.target if self._episode is not None else (
            self._fresh_target or (self.pages.current if self.pages else None))
        await self._release("returned")
        page = await self._page_info(target) if target is not None else {}
        self._self_paused = False
        if proxy.paused:
            await self._resume_agent()
        return page

    async def end_control(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """§4.3, §4.4, §5.5: control ends; with `wipe`, Chromium closes and the profile goes."""
        cause = str(args.get("cause") or "ended")[:64]
        await self._release(cause)
        wipe = args.get("wipe") is True
        if wipe:
            await self.wipe(cause)
        return {"ended": True, "wiped": self.wiped}

    async def wipe(self, cause: str) -> None:
        """§5.5: Chromium is closed and the profile directory emptied."""
        if self.wiped:
            return
        self.wiped = True
        if self.cdp_server is not None:
            self.cdp_server.close()
        if self.browser is not None:
            await self.browser.stop()
        self.mask_state.session_down()
        removed = 0
        try:
            for entry in self.cfg.profile_dir.iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
        except OSError as exc:
            log.warning("live gate: profile not fully removed (%s)", type(exc).__name__)
        self.record("browser_wiped", {"cause": cause, "entries": removed})

    # -- shutdown -----------------------------------------------------------

    async def stop(self) -> None:
        """Everything this process started, in reverse. Never raises."""
        steps: list[tuple[str, Callable[[], Awaitable[None]]]] = [
            ("tunnel", self._stop_tunnel),
            ("ctl", self._stop_ctl),
            ("cdp", self._stop_cdp),
            ("tasks", self._stop_tasks),
            ("injector", self._stop_injector),
            ("chromium", self._stop_browser),
            ("xvfb", self._stop_xvfb),
        ]
        for name, step in steps:
            try:
                await step()
            except Exception as exc:  # noqa: BLE001 -- the rest still stops
                log.warning("live gate: stopping %s failed (%s)", name, type(exc).__name__)
        if self.framebuffer is not None:
            self.framebuffer.close()
        log.info("live gate: stopped")

    async def _stop_tunnel(self) -> None:
        if self.mux is not None:
            await self.mux.close()
        if self.tunnel is not None:
            await self.tunnel.close()

    async def _stop_ctl(self) -> None:
        if self.ctl is not None:
            await self.ctl.close()
            try:
                os.unlink(self.cfg.ctl_path)
            except FileNotFoundError:
                pass

    async def _stop_cdp(self) -> None:
        if self.cdp_server is not None:
            self.cdp_server.close()
            await self.cdp_server.wait_closed()

    async def _stop_tasks(self) -> None:
        tasks = [*self._tasks.values(), *self._spawned]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.pages is not None:
            await self.pages.close()

    async def _stop_injector(self) -> None:
        if self.injector is not None:
            await self.injector.close()

    async def _stop_browser(self) -> None:
        if self.browser is not None:
            await self.browser.stop()

    async def _stop_xvfb(self) -> None:
        if self.xvfb is not None:
            await self.xvfb.stop()


# --------------------------------------------------------------------------
# The process
# --------------------------------------------------------------------------


async def run(cfg: GateConfig, deps: Deps | None = None, *, stop: asyncio.Event | None = None,
              gate: LiveGate | None = None) -> int:
    """Start the gate, serve until SIGTERM (or `stop`), shut down. The exit code."""
    gate = gate or LiveGate(cfg, deps)
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    handled: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
            handled.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    try:
        try:
            await gate.start()
        except SetupError as exc:
            log.error("live gate: fatal: %s", exc)
            await gate.stop()
            return 1
        except Exception as exc:  # noqa: BLE001 -- any setup failure is fatal, said once
            log.error("live gate: fatal: %s during setup", type(exc).__name__)
            await gate.stop()
            return 1
        log.info("live gate: ready")
        await stop.wait()
        log.info("live gate: stopping on signal")
        await gate.stop()
        return 0
    finally:
        for sig in handled:
            loop.remove_signal_handler(sig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent_worker.livegate",
        description="The live browser's display sidecar: Xvfb, headed Chromium, the CDP proxy "
                    "on SWARM_CDP_LISTEN, ctl.sock at SWARM_GATE_CTL, and the tunnel to the relay "
                    "(docs/design/live-browser.md section 3). Configured by the environment "
                    "scheduler.dispatch.with_live_display sets; takes no arguments.",
    )
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = GateConfig.from_env(os.environ)
    except SetupError as exc:
        log.error("live gate: fatal: %s", exc)
        return 1
    return asyncio.run(run(cfg))


if __name__ == "__main__":
    sys.exit(main())
