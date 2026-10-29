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
4. THE VIEW. The capture loop (capture.py) publishes every frame to the RFB
   server (rfb.py) as Chromium drew it: nothing is painted over any part of
   it, in any situation (§5.3; owner decision, BGD 2026-10-25). A gate-only
   CDP session on the page on screen (mask.py) watches for a challenge and
   for the browser closing, and never touches a pixel. The tunnel to the relay
   (tunnel.py) opens when the worker hands over a ticket, and each viewer is
   an RFB stream through it, with the controller's input injected only in
   control mode.

CHROMIUM THAT EXITS UNEXPECTEDLY IS STARTED AGAIN (BROWSER-RELAUNCH; part of
#1030) -- its pipe closed, not by a wipe or the gate stopping. The exit code or
signal and its last stderr lines are logged; a person in control has the
episode ended (`browser_restarted`); the agent's CDP connections are closed
and the CDP proxy stops listening; then, after a backoff, the same binary with
the same flags and policy opens the SAME profile (cookies kept), a new proxy
is served on the same SWARM_CDP_LISTEN address (SWARM_CDP_ENDPOINT stays
valid; the agent connects again), the page follower attaches to the new page
and the device and touch toggle are applied again. At most `RELAUNCH_LIMIT`
times in `RELAUNCH_WINDOW_SECONDS`; past that the browser stays closed and the
timeline says `browser_gave_up`. `status` says `restarting` meanwhile.

PROXIED EGRESS (EGRESS-GATE; part of #1030; docs/browser-egress.md §2.2-§2.5).
With `SWARM_BROWSER_EGRESS=proxy` in the sidecar's environment the gate starts
its local CONNECT forwarder (egress.py) after the display and BEFORE Chromium,
and Chromium is launched -- and relaunched -- with `--proxy-server` pointing at
it. The forwarder refuses every CONNECT until the worker hands over the
attempt's egress ticket on ctl.sock (`egress_ticket`), and again whenever the
worker reports that none can be had, the ticket expires or swarm-egress is
unreachable: FAIL CLOSED (owner decision Q7). A forwarder that cannot listen
is a fatal setup error, so Chromium is never started without the proxy. When
egress is unavailable the gate opens a page saying why, which the person
watching sees and the agent reads; `status` carries the route and its state.
Without the variable nothing here changes.

THE QA IDENTITY (QI-GATE; docs/design/qa-identity.md §4.4, part of #1649).
The worker hands the task's QA identity over on ctl.sock (`qa_identity`), and
the gate holds it for the gate's life (`QaIdentity`), across relaunches: each
Chromium gets its own `QaInjector`, which adds the credential to console
requests from console frames on the gate's own sessions. The proxy scrubs it
from everything the agent receives. A hand-off on the console host is refused
(`handoff_refused`, cause `qa_identity_host`; Q7) and answered `refused:
qa_identity_host`; the session carry leaves the host out; the wipe clears the
credential BEFORE Chromium closes, and the verb is refused after it.

THE CLOUDFLARE ACCESS TOKEN (CF-ACCESS-TOKEN, part of #2442). The worker
hands the attempt's Cloudflare Access service tokens over on ctl.sock
(`cf_access`), each with the hosts its grant lists, once, before the agent
exists. The gate holds them for its life (`CfAccess`), across relaunches:
each Chromium gets its own `CfAccessInjector`, which adds
`CF-Access-Client-Id` and `CF-Access-Client-Secret` to requests for listed
hosts on the gate's own sessions and strips a held value from any other
request. The proxy scrubs the values from everything the agent receives. The
wipe drops them BEFORE Chromium closes, and the verb is refused after it.

A CAPTCHA ON THE AGENT'S PAGE IS A HAND-OFF THE GATE RAISES (CAPTCHA-HANDOFF;
docs/design/live-browser.md §4.1, part of #2058). BGD decided on 2026-10-23
that an agent need not notice a CAPTCHA itself: when a frame on the page the
follower shows is a challenge (mask.py `SHOWN_IN_CONTROL`, the list mask.py
and tabs.py already read), `MaskState` tells `_on_challenge` once per page load,
and the gate puts one request in `status` (`challenge`: `request_id`, the
reason "CAPTCHA on <host>", host only, and `expect` that host). ctl.sock is
the worker's to call, so the request waits there for the worker's next read;
the worker raises it through its own hand-off path and its `pause` takes it
off. Nothing is raised while the gate signs in on its own QA tab (that ends
with `qa_sign_in_challenge`, qatarget.py) or while the browser is already
held for a person. The gate never solves or evades a challenge.

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
own contract says. Chromium's own stderr lines, logged when it exits, are
Chromium's diagnostics, not a page's: credentials in them are masked
(swarm_redaction) and every URL is cut to its origin first.
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
import secrets
import shutil
import signal
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from swarm_redaction import redact as redact_text

from .capture import CaptureError, Frame, XvfbFramebuffer
from .cdppolicy import PolicyPaths
from .cdpproxy import AgentConnection, AgentLink, CdpError, CdpProxy, PipeTransport, serve_agents
from .ctl import DEFAULT_MARKER, CtlServer, GateDown, OpRefused
from .device import WINDOW_OVERHANG, DeviceController, XRandrDisplay, captured
from .. import browser_tools
from .egress import EgressConfigError, EgressSettings, LocalForwarder, chromium_proxy_args, describe, notice_url
from .inject import InjectError, XInjector, display_socket
from .mask import MaskCdp, MaskState
from .pointer import css_to_frame
from .presets import DEFAULT_PRESET, MAX_DISPLAY_HEIGHT, MAX_DISPLAY_WIDTH, Device, UnknownPreset, resolve
from .cfaccess import OP as CF_ACCESS_OP
from .cfaccess import CfAccess, CfAccessInjector, CfAccessRefused
from .qaidentity import OP as QA_IDENTITY_OP
from .qaidentity import QaIdentity, QaIdentityRefused, QaInjector
from .qatarget import OP as QA_SIGN_IN_OP
from .qatarget import QaTarget, QaTargetRefused
from .qatarget import handle_ctl as qa_target_ctl
from .report import agent_report, browser_report, device_report, egress_report, encode_report, page_origin
from .rfb import ControlState, FrameHub, RfbSession
from .rfb import metrics_log as rfb_metrics_log
from .session import OPS as SESSION_OPS
from .session import SessionCarry
from .tabs import OPS as TABS_OPS
from .tabs import TabKeeper
from .touch import TouchInjector
from .tunnel import StreamMux, Tunnel, TunnelError, TunnelWriter, relay_url

log = logging.getLogger("agent_worker.livegate")

XVFB = "/usr/bin/Xvfb"

#: How long each piece of setup may take before it is a fatal error. Xvfb and
#: Chromium start in about a second; the probe allows the whole sidecar 120 s
#: (dispatch.py `failureThreshold`), so these leave room for both and the rest.
XVFB_READY_SECONDS = 30.0
CHROMIUM_READY_SECONDS = 45.0
#: Under the worker's ctl.sock timeout (CtlClient, 10 s), so a relay that does
#: not answer is a refused `tunnel_ticket`, not a lost channel.
TUNNEL_OPEN_SECONDS = 8.0
#: Frames per second while a person holds control (STREAM-SMOOTH; part of
#: #1030). Measured with `livegate.bench` (the numbers are in the pull
#: request): a changed Desktop frame costs the gate about 1 ms to grab and a
#: few ms to diff and encode, so 30 a second fits one core with
#: room for the recorder. 30 is what a screen-sharing viewer reads as motion
#: rather than steps, and with a 16 ms browser paint it puts the next frame
#: after a key within ~50 ms; the viewer pulls at its own pace on top of this
#: (rfb.py), so a slower link gets fewer frames, never older ones.
CONTROL_FPS = 30
#: Frames per second while people only watch, or only the relay records:
#: as before, since nobody is waiting on a key.
WATCH_FPS = 10
#: The reason of a hand-off the gate raises for a challenge (CAPTCHA-HANDOFF):
#: fixed text and the page's host, never a path or a query.
CHALLENGE_REASON = "CAPTCHA on {host}"

#: The longest ticket the relay accepts (swarm_live.rendezvous MAX_TICKET_CHARS).
MAX_TICKET_CHARS = 4096
#: The keys of the relay's `DEVICE` frame (rendezvous.DeviceRequest.payload):
#: a preset by name, an orientation and the person's touch toggle. Anything
#: else -- a width, a scale -- is refused, as the ctl `device` operation is.
DEVICE_FRAME_KEYS = frozenset({"preset", "orientation", "touch"})
#: A `DEVICE` frame larger than this is not a device choice.
MAX_DEVICE_FRAME = 256
#: Agent method lines (cdpproxy `action_line`) waiting for the tunnel. The
#: proxy never waits on the tunnel -- an agent's command must not stall on a
#: slow relay -- so lines queue here, and past this many a line is dropped:
#: the relay keeps at most 20,000 a minute anyway (recorder.py).
MAX_QUEUED_ACTIONS = 1000

#: How long the gate may spend granting control (pausing the agent, attaching
#: to its page, reading its security state) before it refuses. The relay's
#: frames are handled one at a time, so a grant that never answered held up
#: every viewer's stream behind it -- the picture and the controller's input.
CONTROL_GRANT_SECONDS = 15.0

#: The gate's own control lease (CONTROL-LAPSE; part of #1030): control the
#: gate hears no renewal of for this long is revoked here, and the agent gets
#: its browser back. Every renewal the controller's console sends
#: (swarm-ui `LIVE_RENEW_MS`, 20 s) reaches the gate through swarm-api and the
#: relay as `CONTROL` naming the controller again. 50 s is 2.5 renewals: one
#: lost request does not take control from someone mid-sign-in, two do, and
#: it is under swarm-api's own 60 s (`live_control_renew_seconds`), so the
#: gate never holds control longer than swarm-api would have. MEASURED
#: 2026-10-13 (task e8cb...): with no lease, a gate swarm-api never told held
#: control 54 minutes after it lapsed, and refused the agent 72 times.
CONTROL_LEASE_SECONDS = 50.0
#: The longest a revoke's cause is (the relay checks `[a-z_]{1,32}`).
MAX_CONTROL_CAUSE = 32

#: How long reading a page's URL and title may take when control ends.
PAGE_INFO_SECONDS = 2.0

#: How long a CANCELLED tunnel task may spend ending the episode a person
#: holds (HANG-LEASE; part of #1335). The gate's own stop never cancels it
#: first: it closes the tunnel, and the task ends control with Chromium's
#: pipe still read. A cancel from outside -- the event loop shutting down --
#: cancels the pipe's reader with it, so the release's reads (the page's
#: title, the agent's resume) are answered by nobody, and the resume was
#: unbounded: MEASURED 2026-10-14, run 38633285923, `asyncio.run` waited on
#: the task in `_cancel_all_tasks` until the job's 30-minute timeout. Under
#: one `PAGE_INFO_SECONDS` read, because nothing is going to answer.
CANCELLED_RELEASE_SECONDS = 1.0

#: Between the page follower's attempts to attach to the agent's page again
#: (`PageFollower._reconnect`), the last repeated: fast at first, because the
#: usual cause -- a session Chromium dropped in a device switch -- is over in
#: milliseconds; then every two seconds, one `Target.getTargets` each, for as
#: long as the page is there and will not take a session (a crashed renderer).
RECONNECT_DELAYS = (0.05, 0.25, 0.5, 1.0, 2.0)

#: THE PAGE FOLLOWED IS THE TAB CHROMIUM SHOWS (LIVE-BLANK; part of #1030).
#: Every this many seconds the follower asks each page whether it is shown
#: (`PageFollower.check`), besides when a tab opens, closes or is brought
#: forward. Half a second bounds how long a tab switch nobody announced -- a
#: link opening a tab, a page closing itself -- shows the "unavailable" fill;
#: each ask is two CDP round trips per page.
VISIBLE_POLL_SECONDS = 0.5
#: A page that has not answered in this long is "unknown", not shown: a tab
#: restored from the profile may have no renderer until it is loaded.
VISIBLE_PROBE_SECONDS = 2.0
#: The isolated world the visibility probe reads `document.visibilityState`
#: in: a world no page script can reach, so a page cannot claim to be shown.
VISIBLE_WORLD = "swarm-live-visible"
#: Agent commands after which another tab may be on screen. The follower looks
#: which tab is shown when one is forwarded, when Chromium answers it, and
#: again `TAB_SWITCH_LOOK_AGAIN_SECONDS` later in case it never answers.
TAB_SWITCH_METHODS = frozenset({
    "Target.createTarget", "Target.activateTarget", "Target.closeTarget", "Page.bringToFront", "Page.close",
})
TAB_SWITCH_LOOK_AGAIN_SECONDS = 5.0

#: CHROMIUM IS STARTED AGAIN WHEN IT EXITS UNEXPECTEDLY (BROWSER-RELAUNCH;
#: part of #1030), at most this many times in this many seconds, then it
#: stays closed and the gate says so (`browser_gave_up`). Owner decision
#: 2026-10-13, after att_59f378aac5c340ca9507's take was refused "the browser
#: had closed". Three in ten minutes: one crash is bad luck the person should
#: not pay for; a browser that dies every three minutes is a page or a device
#: that kills it, and relaunching it for ever only hides that.
RELAUNCH_LIMIT = 3
RELAUNCH_WINDOW_SECONDS = 600.0
#: Before each relaunch, the last repeated: a crash loop must not spin.
RELAUNCH_DELAYS = (1.0, 3.0, 10.0)
#: How long a Chromium that closed its pipe may take to exit before it is
#: stopped: a second one cannot open the same profile while it lives.
EXIT_WAIT_SECONDS = 5.0
#: Chromium's last stderr lines kept, each cut to this many characters: the
#: one record of why it died (att_59f378aac5c340ca9507 left none).
STDERR_TAIL_LINES = 20
STDERR_LINE_CHARS = 300
#: What a Chromium that died leaves in its profile, naming its host and pid;
#: the next one refuses a profile whose lock it cannot prove stale.
PROFILE_LOCKS = ("SingletonLock", "SingletonSocket", "SingletonCookie")

#: A URL in a stderr line, reduced to its origin before it is logged: a
#: page's path and query are content (module docstring), its host is not.
_URL = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)(?:[^/\s?#@]*@)?([^/\s?#]+)\S*")

#: A hand-off's `expect`: a host (the worker checked it with `expect_refusal`).
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?")


def capture_fps(*, watching: bool, controlled: bool) -> int:
    """How often to grab: `CONTROL_FPS` in control, `WATCH_FPS` watched, 0 for nobody."""
    if not watching:
        return 0
    return CONTROL_FPS if controlled else WATCH_FPS


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
    #: Where the image puts Playwright's Chromium: the image's own
    #: PLAYWRIGHT_BROWSERS_PATH (images/agent-runtime-browser/Dockerfile), read
    #: rather than copied here so the path is written down once.
    browsers_path: str = ""
    #: Proxied egress (egress.py): off unless the dispatcher set SWARM_BROWSER_EGRESS=proxy.
    egress: EgressSettings = EgressSettings()
    #: The local forwarder's port once it listens; Chromium's `--proxy-server`.
    egress_port: int | None = None

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
        try:
            egress = EgressSettings.from_env(env)
        except EgressConfigError as exc:
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
            browsers_path=env.get("PLAYWRIGHT_BROWSERS_PATH") or "",
            egress=egress,
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
    """Chromium, the pipe the gate holds to it, and the tail of its stderr."""

    def __init__(self, proc: Any, transport: Any) -> None:
        self.proc = proc
        self.transport = transport
        self.stderr: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        stream = getattr(proc, "stderr", None)
        self._stderr_task = asyncio.create_task(self._read_stderr(stream)) if stream is not None else None

    async def _read_stderr(self, stream: asyncio.StreamReader) -> None:
        """Keep the last lines: read for as long as Chromium writes, or it would block on a full pipe."""
        while True:
            try:
                line = await stream.readline()
            except ValueError:  # a line past the reader's limit: dropped, the rest read on
                continue
            if not line:
                return
            self.stderr.append(line.decode("utf-8", "replace").rstrip()[:STDERR_LINE_CHARS])

    async def exited(self, timeout: float = EXIT_WAIT_SECONDS) -> tuple[int | None, list[str]]:
        """Its exit code (negative: the signal) once it has exited, stopping it if it will not."""
        try:
            await asyncio.wait_for(self.proc.wait(), timeout)
        except asyncio.TimeoutError:
            await _stop_process(self.proc)
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._stderr_task), 1.0)
            except Exception:  # noqa: BLE001 -- a timeout or a failed read: what was read is kept
                pass
        return self.proc.returncode, list(self.stderr)

    async def stop(self) -> None:
        try:
            await self.transport.close()
        finally:
            await _stop_process(self.proc)
            if self._stderr_task is not None:
                self._stderr_task.cancel()


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


def _clear_profile_locks(profile_dir: Path) -> None:
    """The lock files a Chromium that died left: only ever called once it has exited.

    Cookies, storage and everything else in the profile stay; the next
    Chromium opens the same profile (BROWSER-RELAUNCH).
    """
    for name in PROFILE_LOCKS:
        try:
            (profile_dir / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("live gate: %s not removed (%s)", name, type(exc).__name__)


def find_chromium(browsers_path: str) -> str:
    """Playwright's headed Chromium (not its headless shell), the newest installed."""
    if not browsers_path:
        raise SetupError("PLAYWRIGHT_BROWSERS_PATH is not set; the browser image sets it")
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
    `--disable-infobars`: the image's Chromium is Chrome for Testing, whose
    "only for automated testing" infobar took 56 rows of the page area
    `presets.BROWSER_UI_HEIGHT` measures without it (LB-R3, 2026-10-12). The
    window is one pixel past the display each way (`device.WINDOW_OVERHANG`).

    With proxied egress on, the proxy flags (egress.py `chromium_proxy_args`)
    come before the first page, and with no listening forwarder this raises
    SetupError: there is no way to render these arguments without the proxy.
    """
    desktop = resolve(DEFAULT_PRESET)
    try:
        egress = chromium_proxy_args(cfg.egress, cfg.egress_port)
    except EgressConfigError as exc:
        raise SetupError(str(exc)) from None
    return [
        "--remote-debugging-pipe",
        f"--user-data-dir={cfg.profile_dir}",
        "--no-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        "--password-store=basic",
        "--window-position=0,0",
        "--disable-infobars",
        f"--window-size={desktop.display_width + WINDOW_OVERHANG},{desktop.display_height + WINDOW_OVERHANG}",
        *egress,
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
            stderr=asyncio.subprocess.PIPE, preexec_fn=remap, close_fds=False,
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


async def start_egress(cfg: GateConfig, record: Callable[[str, Mapping[str, Any]], None],
                       on_unavailable: Callable[[str], None]) -> LocalForwarder:
    """The local CONNECT forwarder on 127.0.0.1, on an ephemeral port."""
    forwarder = LocalForwarder(cfg.egress, record=record, on_unavailable=on_unavailable)
    try:
        await forwarder.start()
    except OSError as exc:
        raise SetupError(f"the egress forwarder cannot listen: {exc.strerror}") from None
    return forwarder


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
    #: The proxied-egress forwarder, started only when egress is on.
    start_egress: Callable[..., Awaitable[Any]] = start_egress


# --------------------------------------------------------------------------
# Glue the components did not have
# --------------------------------------------------------------------------


class GateProxy(CdpProxy):
    """The proxy, remembering whether an agent has ever connected (SessionCarry asks).

    It also tells the page follower when the agent asks Chromium to open,
    close or bring forward a tab (`TAB_SWITCH_METHODS`): `on_tab_switch` as
    the command is forwarded, and `on_tab_switched` when Chromium has answered
    it, so the follower looks which tab is shown after it did (LIVE-BLANK).
    """

    agent_seen = False
    on_tab_switch: Callable[[], None] | None = None
    on_tab_switched: Callable[[], None] | None = None

    async def connect_agent(self, link: AgentLink, client: str | None = None) -> AgentConnection | None:
        connection = await super().connect_agent(link, client)
        if connection is not None:
            self.agent_seen = True
        return connection

    async def _forward(self, connection: AgentConnection, message: Mapping[str, Any]) -> None:
        if message.get("method") in TAB_SWITCH_METHODS and self.on_tab_switch is not None:
            self.on_tab_switch()
        await super()._forward(connection, message)

    async def _on_response(self, pending: Any, message: Mapping[str, Any]) -> None:
        await super()._on_response(pending, message)
        if pending.owner is not None and pending.method in TAB_SWITCH_METHODS \
                and self.on_tab_switched is not None:
            self.on_tab_switched()


class _CurrentBrowser:
    """The proxy and the devices of whichever Chromium is running now.

    Touch, the controller's state and the session carry outlive one Chromium
    (BROWSER-RELAUNCH): a viewer's RFB stream holds the TouchInjector it was
    opened with, so after a relaunch it must still reach the new browser.
    """

    def __init__(self, gate: "LiveGate") -> None:
        self._gate = gate

    async def call(self, method: str, params: Mapping[str, Any] | None = None,
                   session_id: str | None = None) -> dict[str, Any]:
        proxy = self._gate.proxy
        if proxy is None or self._gate.browser_closed:
            raise CdpError("the browser is not running")
        return await proxy.call(method, params, session_id)

    @property
    def device(self) -> Device:
        devices = self._gate.devices
        return devices.device if devices is not None else self._gate.desktop

    def session_for(self, target_id: str) -> str | None:
        devices = self._gate.devices
        return devices.session_for(target_id) if devices is not None else None


def exit_cause(returncode: int | None) -> tuple[str, str | None]:
    """Chromium's exit, as a cause the timeline can carry, and the signal's name."""
    if returncode is None:
        return "exit_unknown", None
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = str(-returncode)
        return f"signal_{name}", name
    return f"exit_code_{returncode}", None


def redacted_stderr(lines: list[str]) -> list[str]:
    """Chromium's stderr lines, fit for the log: credentials masked, every URL cut to its origin."""
    return [_URL.sub(r"\1\2", redact_text(line).text) for line in lines]


class PageFollower:
    """Which page is on screen, and the gate-only session on it (mask.py `MaskCdp`).

    The display holds one window (§3.1), and the page on screen is its tab
    Chromium shows. The session watches that page's frames for a challenge
    (CAPTCHA-HANDOFF) and its top frame's origin for the caption (report.py);
    it never touches a pixel, so every frame is published as captured whether
    the session is up or not (§5.3, LIVE-NOMASK).

    WHICH TAB IS SHOWN IS ASKED, NOT GUESSED (LIVE-BLANK; part of #1030). The
    follower used to follow the NEWEST page target, on the rule that "the
    visible tab is the one opened last", and on start the last one
    `Target.getTargets` listed. MEASURED 2026-10-13 on release 1e86c37e: the
    gate followed a tab that was not on screen for ten minutes and more. An
    agent driving Chromium over the CDP proxy (Playwright MCP, swarm_browser)
    opens tabs and brings others forward, and a profile restored on a resumed
    attempt brings back several tabs in an order that says nothing about
    which is shown; a caption and a challenge watch on a tab nobody sees are
    about the wrong page.

    So `check` asks every page for `document.visibilityState`, in an isolated
    world (`VISIBLE_WORLD`) no page script can reach, and follows the shown
    one -- of several (a second window: the gate's own fresh page), the one
    shown most recently. It runs on start, when a tab is created or
    destroyed, after the agent's every tab command (`TAB_SWITCH_METHODS`),
    and every `VISIBLE_POLL_SECONDS`. A Chromium that answers no probe at
    all gets the old rule, the newest page.

    OUT-OF-PROCESS FRAMES. The install asks Chromium to auto-attach the
    page's cross-site iframes (mask.py `MaskCdp`); each one's session is a
    CHILD of the page's, and the follower installs `MaskCdp(child=True)` on
    it and routes its events, so a reCAPTCHA in a renderer of its own is
    still seen as a challenge.

    The pipe reader must never wait on a reply (cdpproxy.py), so the
    session's events are queued here and handled on a task of their own, in
    order.

    The page it follows is the page on screen, so its top frame's origin is
    the one the console's strip shows (§5.7, report.py): `on_origin` is told
    it when a page is followed and on every navigation the top frame commits,
    and None when no page is on screen.

    IT NEVER STAYS DETACHED FROM A LIVE PAGE (DEVICE-RECOVER; part of #1030).
    MEASURED 2026-10-13 (task_3458d6bfc7d14db79af1): after eight device
    switches in two and a half minutes the canvas was the "unavailable" grey
    and the strip "has not reported the page's address yet", for good. The
    follower used to give up on its page in three ways, each silent and each
    permanent: its session detached (`Target.detachedFromTarget`) and nothing
    attached again; an attach answered but the install then failed, which
    left `_session` set on a dead session, so every later `follow` of that
    page returned early as if all were well; and a crashed renderer kept a
    session that heard nothing more. Now each of those -- and a follow that
    raised -- starts ONE reconnect loop (`_reconnect`): it re-reads the pages
    Chromium has, attaches to the current one (or the newest), installs
    again, and retries on `RECONNECT_DELAYS` until one holds. With no page
    left it says so (`closed`) and the next page Chromium creates is followed.
    `state` is what the gate tells the timeline: `following`, `reconnecting`
    (the caption and the challenge watch are down while it does), or
    `closed` (the agent has no page).
    """

    def __init__(self, cdp: CdpProxy, state: MaskState,
                 on_origin: Callable[[dict[str, Any] | None], None] | None = None,
                 on_state: Callable[[str, str | None, str | None], None] | None = None,
                 delays: tuple[float, ...] = RECONNECT_DELAYS,
                 poll: float = VISIBLE_POLL_SECONDS) -> None:
        self._cdp = cdp
        self._state = state
        self._on_origin = on_origin
        #: Told (state, cause, target) when `state` changes.
        self._on_state = on_state
        self._delays = delays
        self._poll = poll
        #: Page targets, the one shown most recently last (`check`).
        self.pages: list[str] = []
        self.current: str | None = None
        self.state = "none"
        self._session: str | None = None
        #: The session is attached but its renderer is gone (a crash).
        self._stale = False
        #: The `MaskCdp` on every session this follow holds: the page's and
        #: its out-of-process frames', by session id.
        self._masks: dict[str, MaskCdp] = {}
        #: Out-of-process frame sessions under the page's: session -> targetId.
        self._children: dict[str, str] = {}
        #: The visibility probe's own session on each page, and its world there.
        self._probes: dict[str, str] = {}
        self._probe_worlds: dict[str, int] = {}
        #: What each page last answered: True shown, False hidden, None unknown.
        self._shown_last: dict[str, bool | None] = {}
        self._checking = False
        self._check_again = False
        self._queue: asyncio.Queue[tuple[str, str, dict[str, Any]]] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._worker: asyncio.Task[None] | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._reconnecting: asyncio.Task[None] | None = None
        self._closing = False

    async def start(self) -> None:
        self._worker = asyncio.create_task(self._drain())
        targets = (await self._cdp.call("Target.getTargets", {})).get("targetInfos") or []
        for info in targets:
            if info.get("type") == "page" and isinstance(info.get("targetId"), str):
                if info["targetId"] not in self.pages:
                    self.pages.append(info["targetId"])
        try:
            await self.check()
        except Exception as exc:  # noqa: BLE001 -- the reconnect loop takes it from here
            self.recover("attach_failed", exc)
        self._watcher = asyncio.create_task(self._watch())

    def on_event(self, message: Mapping[str, Any]) -> None:
        """A message from Chromium's pipe. Never waits."""
        method = message.get("method")
        raw = message.get("params")
        params: dict[str, Any] = raw if isinstance(raw, dict) else {}
        session = message.get("sessionId")
        if not isinstance(method, str):
            return
        if session is not None:
            if session == self._session and method == "Inspector.targetCrashed":
                self._crashed()
            elif session in self._masks:
                if method == "Target.attachedToTarget":
                    self._attached(params)
                elif method == "Target.detachedFromTarget":
                    child = params.get("sessionId")
                    if child in self._children:
                        del self._children[child]
                        self._masks.pop(child, None)
                else:
                    self._queue.put_nowait((session, method, params))
            return
        if method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            target = info.get("targetId")
            if info.get("type") == "page" and isinstance(target, str) and target not in self.pages:
                self.pages.append(target)
                # A new tab is usually shown at once: look.
                self.tab_switching()
        elif method == "Target.targetDestroyed":
            target = params.get("targetId")
            self._forget_probe(target)
            if target in self.pages:
                self.pages.remove(target)
                if target == self.current:
                    self.tab_switching()
        elif method == "Target.targetCrashed":
            if params.get("targetId") == self.current and self._session is not None:
                self._crashed()
        elif method == "Target.detachedFromTarget" and session is None:
            gone = params.get("sessionId")
            for page, probe in list(self._probes.items()):
                if probe == gone:
                    self._forget_probe(page)
            if self._session is not None and gone == self._session:
                self._session = None
                self._masks.clear()
                self._children.clear()
                if self.current is not None:
                    # Its page may well still be there (a renderer swap, a session
                    # Chromium dropped): attach again rather than stay grey.
                    self.recover("detached")

    def _attached(self, params: Mapping[str, Any]) -> None:
        """Chromium auto-attached a target under the page's session: an out-of-process frame, or not ours."""
        child = params.get("sessionId")
        info = params.get("targetInfo") or {}
        if not isinstance(child, str):
            return
        if info.get("type") != "iframe":
            # A worker or anything else the auto-attach brought: no frame to watch.
            self._spawn_quiet(self._cdp.call("Target.detachFromTarget", {"sessionId": child}))
            return
        self._children[child] = str(info.get("targetId", ""))

        async def send(method: str, params: dict[str, Any]) -> dict[str, Any]:
            return await self._cdp.call(method, params, child)

        watch = MaskCdp(self._state, send, child=True)
        self._masks[child] = watch
        self._spawn_quiet(watch.install())

    def tab_switching(self) -> None:
        """Another tab may be about to be shown: look, now and once more later. Never waits."""
        if self._closing:
            return
        self._spawn(self.check())
        self._spawn_quiet(self._check_after(TAB_SWITCH_LOOK_AGAIN_SECONDS))

    def tab_switched(self) -> None:
        """Chromium answered the agent's tab command: look which tab it shows now."""
        if not self._closing:
            self._spawn(self.check())

    async def _check_after(self, seconds: float) -> None:
        """A tab command Chromium never answered is looked at again."""
        await asyncio.sleep(seconds)
        await self.check()

    async def check(self) -> None:
        """Follow the tab Chromium shows."""
        if self._checking:
            self._check_again = True
            return
        self._checking = True
        try:
            while True:
                self._check_again = False
                target = await self._shown()
                await self.follow(target)
                if not self._check_again or self._closing:
                    break
        finally:
            self._checking = False

    async def _watch(self) -> None:
        while not self._closing:
            await asyncio.sleep(self._poll)
            if self._reconnecting is not None and not self._reconnecting.done():
                continue  # the reconnect loop has the page
            try:
                await self.check()
            except Exception as exc:  # noqa: BLE001 -- the reconnect loop takes it from here
                self.recover("attach_failed", exc)

    async def _shown(self) -> str | None:
        """The page Chromium shows: of several, the one shown most recently; None with no page."""
        pages = list(self.pages)
        if not pages:
            return None
        answers = await asyncio.gather(*(self._visible(page) for page in pages))
        for page, shown in zip(pages, answers):
            if shown and self._shown_last.get(page) is not True and page in self.pages:
                # Newly shown: the most recently shown page goes last.
                self.pages.remove(page)
                self.pages.append(page)
            self._shown_last[page] = shown
        live = [p for p in self.pages if p in pages]
        if not live:
            return self.pages[-1] if self.pages else None
        shown = [p for p in live if self._shown_last.get(p) is True]
        if shown:
            return shown[-1]
        if all(self._shown_last.get(p) is None for p in live):
            return live[-1]  # a Chromium that answers no probe: the newest page
        if self.current in live:
            return self.current  # none shown (a minimised window): stay
        unknown = [p for p in live if self._shown_last.get(p) is None]
        return unknown[-1] if unknown else live[-1]

    async def _visible(self, target: str) -> bool | None:
        """Whether `target` is the tab shown, read in a world its page cannot reach; None if unknown."""
        try:
            return await asyncio.wait_for(self._probe(target), VISIBLE_PROBE_SECONDS)
        except Exception as exc:  # noqa: BLE001 -- a page closing, loading, or not answering: unknown
            self._probe_worlds.pop(target, None)
            if not isinstance(exc, (CdpError, asyncio.TimeoutError)) or "session" in str(exc).lower():
                # The probe's session is gone or never was: attach again next time.
                self._forget_probe(target)
            return None

    async def _probe(self, target: str) -> bool | None:
        session = self._probes.get(target)
        if session is None:
            result = await self._cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})
            session = self._probes[target] = str(result["sessionId"])
        for _ in range(2):
            world = self._probe_worlds.get(target)
            if world is None:
                # One world per document, made again only when its context is
                # gone (a navigation): every call to make one is a new world.
                made = await self._cdp.call("Page.createIsolatedWorld",
                                            {"frameId": target, "worldName": VISIBLE_WORLD}, session)
                world = self._probe_worlds[target] = int(made["executionContextId"])
            try:
                reply = await self._cdp.call("Runtime.evaluate", {
                    "expression": "document.visibilityState", "contextId": world,
                    "returnByValue": True}, session)
            except CdpError:
                self._probe_worlds.pop(target, None)
                continue
            value = (reply.get("result") or {}).get("value")
            if value in ("visible", "hidden", "prerender"):
                return value == "visible"
            return None
        return None

    def _forget_probe(self, target: Any) -> None:
        if not isinstance(target, str):
            return
        self._probe_worlds.pop(target, None)
        self._shown_last.pop(target, None)
        session = self._probes.pop(target, None)
        if session is not None and not self._closing:
            self._spawn_quiet(self._cdp.call("Target.detachFromTarget", {"sessionId": session}))

    def _crashed(self) -> None:
        """The page's renderer is gone: re-attach."""
        self._stale = True
        self.recover("crashed")

    def recover(self, cause: str, exc: BaseException | None = None) -> None:
        """Start the reconnect loop, unless one is running. Never waits."""
        if exc is not None:
            log.info("live gate: page follow failed (%s)", type(exc).__name__)
        if self._closing:
            return
        if self._reconnecting is not None and not self._reconnecting.done():
            return
        self._set_state("reconnecting", cause)
        self._reconnecting = asyncio.get_running_loop().create_task(self._reconnect())

    async def _reconnect(self) -> None:
        attempt = 0
        while not self._closing:
            try:
                infos = (await self._cdp.call("Target.getTargets", {})).get("targetInfos") or []
                live = [i["targetId"] for i in infos
                        if i.get("type") == "page" and isinstance(i.get("targetId"), str)]
                self.pages = [p for p in self.pages if p in live] + [p for p in live if p not in self.pages]
                target = self.current if self.current in self.pages else await self._shown()
                await self.follow(target)
                if target is None:
                    return
                if self._session is not None and not self._stale:
                    # Followed here, or by a `follow` that ran meanwhile.
                    self._set_state("following")
                    return
            except Exception as exc:  # noqa: BLE001 -- tried again below
                log.info("live gate: reconnect to the page failed (%s)", type(exc).__name__)
            await asyncio.sleep(self._delays[min(attempt, len(self._delays) - 1)])
            attempt += 1

    def _set_state(self, state: str, cause: str | None = None) -> None:
        if state == self.state:
            return
        self.state = state
        if self._on_state is not None:
            self._on_state(state, cause, self.current)

    def _spawn(self, work: Awaitable[None]) -> None:
        task = asyncio.get_running_loop().create_task(self._quietly(work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _spawn_quiet(self, work: Awaitable[Any]) -> None:
        """`work` on a task whose failure is only logged: a detach, an out-of-process frame's install."""
        async def run() -> None:
            try:
                await work
            except Exception as exc:  # noqa: BLE001 -- the session is gone
                log.info("live gate: page follower side task failed (%s)", type(exc).__name__)

        task = asyncio.get_running_loop().create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _quietly(self, work: Awaitable[None]) -> None:
        try:
            await work
        except Exception as exc:  # a page that closed before the attach answered, a session that went
            self.recover("attach_failed", exc)

    async def follow(self, target: str | None) -> None:
        async with self._lock:
            if target is not None and target == self.current and self._session is not None \
                    and not self._stale:
                return
            if target is None and self.current is None and self.state == "closed":
                return
            old, old_session = self.current, self._session
            self._session = None
            self._stale = False
            self._masks.clear()
            self._children.clear()
            if old is not None:
                # A page's main frame id is its target id.
                self._state.frame_detached(old)
            if old_session is not None:
                try:
                    await self._cdp.call("Target.detachFromTarget", {"sessionId": old_session})
                except Exception:  # noqa: BLE001 -- already gone with its page
                    pass
            self.current = target
            self._origin(None)
            if target is None:
                self._set_state("closed", "page_closed")
                return
            result = await self._cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})
            session = str(result["sessionId"])
            self._session = session

            async def send(method: str, params: dict[str, Any]) -> dict[str, Any]:
                return await self._cdp.call(method, params, session)

            try:
                watch = MaskCdp(self._state, send)
                self._masks[session] = watch
                await watch.install()
                tree = await send("Page.getFrameTree", {})
            except BaseException:
                # Never leave a session nothing is installed on looking
                # followed: the next `follow` of this page would return early.
                if self._session == session:
                    self._session = None
                    self._masks.clear()
                    self._children.clear()
                try:
                    await self._cdp.call("Target.detachFromTarget", {"sessionId": session})
                except Exception:  # noqa: BLE001 -- already gone
                    pass
                raise
            if self._session != session:
                return  # it detached while it installed; the reconnect loop has it
            self._top_frame((tree.get("frameTree") or {}).get("frame") or {})
            self._set_state("following")

    def _origin(self, origin: dict[str, Any] | None) -> None:
        if self._on_origin is not None:
            self._on_origin(origin)

    def _top_frame(self, frame: Mapping[str, Any]) -> None:
        """A frame Chromium committed: the origin, when it is the top frame (no parent)."""
        if not frame.get("parentId"):
            self._origin(page_origin(frame.get("url"), frame.get("domainAndRegistry")))

    async def _drain(self) -> None:
        while True:
            session, method, params = await self._queue.get()
            watch = self._masks.get(session)
            if watch is None:
                continue
            if method == "Page.frameNavigated" and session == self._session:
                self._top_frame(params.get("frame") or {})
            try:
                await watch.on_event(method, params)
            except Exception as exc:  # noqa: BLE001 -- one event
                log.info("live gate: page event failed (%s)", type(exc).__name__)

    def halt(self) -> None:
        """Chromium's pipe closed: there is no page to reconnect to, ever."""
        self._closing = True
        if self._reconnecting is not None:
            self._reconnecting.cancel()
        if self._watcher is not None:
            self._watcher.cancel()

    async def close(self) -> None:
        self._closing = True
        tasks = [*self._tasks, *([self._worker] if self._worker else []),
                 *([self._watcher] if self._watcher else []),
                 *([self._reconnecting] if self._reconnecting else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


@dataclass
class _Episode:
    """One person's control of one page, from grant to release."""

    target: str
    since: float
    conn_id: str


def _revoke_cause(payload: bytes) -> str:
    """Why swarm-api revoked control, from a `CONTROL` frame with no
    controller: its cause code, or `released` for a plain release."""
    try:
        cause = payload.decode("ascii")
    except UnicodeDecodeError:
        return "released"
    if not cause or len(cause) > MAX_CONTROL_CAUSE or not all(c.islower() or c == "_" for c in cause):
        return "released"
    return cause


class LiveGate:
    def __init__(self, cfg: GateConfig, deps: Deps | None = None, *,
                 record: Callable[[str, Mapping[str, Any]], None] = _record,
                 wall: Callable[[], float] = time.time,
                 relaunch_limit: int = RELAUNCH_LIMIT,
                 relaunch_window: float = RELAUNCH_WINDOW_SECONDS,
                 relaunch_delays: tuple[float, ...] = RELAUNCH_DELAYS,
                 clock: Callable[[], float] = time.monotonic,
                 control_lease_seconds: float = CONTROL_LEASE_SECONDS) -> None:
        self.cfg = cfg
        self.deps = deps or Deps()
        self.record = record
        self._wall = wall
        #: When each of the browser's start-up steps happened, in epoch
        #: seconds: `browser_launching`, `browser_ready`, `first_frame`
        #: (STARTUP-API). Served in `status`, where the worker reads them and
        #: writes each once as a `startup_progress` event; the gate itself
        #: holds no credential to write anything. Empty on a restarted gate,
        #: which launches no browser.
        self.startup: dict[str, float] = {}
        self.desktop = resolve(DEFAULT_PRESET)
        #: The page on screen's challenges (mask.py SHOWN_IN_CONTROL): it tells
        #: `_on_challenge` of one on the agent's page, once per load. And the
        #: desktop name once the browser closed (`REASON_BROWSER_CLOSED`).
        self.mask_state = MaskState(on_challenge=self._on_challenge)
        #: The hand-off the gate asks the worker for (CAPTCHA-HANDOFF), in
        #: `status` until the worker's `pause` takes it off; None otherwise.
        self.challenge: dict[str, Any] | None = None
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
        #: The tab list a park keeps and a resumed attempt reopens (tabs.py, §5.5.2).
        self.tabs: TabKeeper | None = None
        self.pages: PageFollower | None = None
        self.cdp_server: Any = None
        self.ctl: CtlServer | None = None
        self.framebuffer: Any = None
        self.tunnel: Tunnel | None = None
        self.mux: StreamMux | None = None
        #: The proxied-egress forwarder (egress.py); None when egress is off.
        self.egress: Any = None
        #: The task's QA identity, for the gate's life; and the injector of
        #: the Chromium running now (qaidentity.py, QI-GATE).
        self.qa = QaIdentity(self.record, clock=clock)
        #: The attempt's QA target (qatarget.py, VQ-4): held for the gate's
        #: life, across relaunches, like the QA identity.
        self.qa_target = QaTarget(self.record, clock=clock)
        self.qa_injector: QaInjector | None = None
        #: The attempt's Cloudflare Access tokens (cfaccess.py), for the
        #: gate's life; and the injector of the Chromium running now.
        self.cf_access = CfAccess(self.record)
        self.cf_injector: CfAccessInjector | None = None
        self.wiped = False
        #: Chromium's pipe closed (a wipe, or a crash, until a relaunch is up):
        #: nothing needing it is served.
        self.browser_closed = False
        #: The browser's state in `status`: `running`, `restarting` (it exited
        #: and is being started again), `closed` (for good) or `wiped`.
        self.browser_state = "running"
        #: The relaunch budget (`RELAUNCH_LIMIT` in `RELAUNCH_WINDOW_SECONDS`),
        #: and when each relaunch was tried, on `clock`.
        self._relaunch_limit = relaunch_limit
        self._relaunch_window = relaunch_window
        self._relaunch_delays = relaunch_delays or (0.0,)
        self._clock = clock
        self._relaunches: list[float] = []
        self.restarts = 0
        #: The browser that has finished starting: only its exit is handled.
        self._ready_browser: Any = None
        self._stopping = False
        self._agent_seen_before = False
        self._current = _CurrentBrowser(self)
        self.viewers = 0
        self.last_episode: dict[str, Any] | None = None
        self._episode: _Episode | None = None
        #: The control lease (`CONTROL_LEASE_SECONDS`): when, on `clock`,
        #: control ends unless the relay renews it first. None with nobody in control.
        self._lease_seconds = control_lease_seconds
        self._lease_until: float | None = None
        self._had_control = False
        self._self_paused = False
        #: The worker's hand-off (`pause`) holds the agent for a person,
        #: until `resume`, `end_control` or a wipe.
        self._handed_off = False
        self._fresh_target: str | None = None
        self._watching = asyncio.Event()
        #: Set when control is granted or released: the capture loop's rate changed.
        self._rate_changed = asyncio.Event()
        #: What the gate reports over the tunnel (report.py): the top frame's
        #: origin and the applied device, each None until it is known.
        self.origin: dict[str, Any] | None = None
        self.device_reported: dict[str, Any] | None = None
        self._report_due = asyncio.Event()
        #: Whether the agent is using THIS browser (LIVE-UNUSED; part of
        #: #1030): the wall time its first CDP connection was accepted, by any
        #: proxy this gate has run -- a relaunch makes a new proxy, and the
        #: agent having connected to the old one still counts -- and whether
        #: `agent_connected` has been put on the timeline, which happens once.
        self.agent_connected_at: float | None = None
        self._agent_connected_told = False
        #: WHICH BROWSER TOOL (BSTATUS-API; part of #1030): what the runner
        #: registered, as the worker forwarded it (`browser_tools`), and the
        #: tool whose tagged connection sent the last command, by any proxy
        #: this gate has run (cdpproxy `split_client`). Both None until known.
        self.browser_tools: dict[str, Any] | None = None
        self.tool_last_used: tuple[str, float] | None = None
        #: One relay device choice applied at a time. Latest wins: a choice
        #: still waiting when a newer one arrives is dropped (`_device_seq`),
        #: so a burst of switches cannot apply one after another in turn.
        self._device_lock = asyncio.Lock()
        self._device_seq = 0
        #: The page follower's state (`PageFollower.state`), in `status`.
        self.page_state = "none"
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._spawned: set[asyncio.Task[Any]] = set()
        self._actions: asyncio.Queue[dict[str, Any]] = asyncio.Queue(MAX_QUEUED_ACTIONS)

    # -- start, in the design's order ---------------------------------------

    async def start(self) -> None:
        # §5.4.2: a restart keeps Chromium closed. CtlServer.start makes the
        # marker and records the restart; this only decides what else starts.
        self.restarted = os.path.lexists(self.cfg.marker)
        if self.restarted:
            await self.start_ctl()
            return
        await self.start_display()
        await self.start_egress()
        await self.start_browser()
        await self.start_ctl()
        await self.start_view()

    async def start_display(self) -> None:
        """1. Xvfb on :0, and the X connection input is injected through."""
        self.xvfb = await self.deps.start_xvfb(self.cfg)
        self.injector = await self.deps.open_injector(
            self.cfg, self.desktop.display_width, self.desktop.display_height)
        log.info("live gate: display %s up", self.cfg.display)

    async def start_egress(self) -> None:
        """1b. With proxied egress on, the local forwarder, before any Chromium.

        Its port goes into the configuration Chromium's flags are rendered
        from, so a relaunch is proxied exactly as the first start was.
        """
        if not self.cfg.egress.proxy:
            return
        self.egress = await self.deps.start_egress(self.cfg, self._egress_record, self._egress_unavailable)
        port = getattr(self.egress, "port", None)
        if not isinstance(port, int) or port <= 0:
            raise SetupError("the egress forwarder is not listening; Chromium is not started without the proxy")
        self.cfg = replace(self.cfg, egress_port=port)
        self.record("egress_route", {"route": "proxy"})
        log.info("live gate: browser egress is proxied; forwarder on 127.0.0.1:%d", port)

    async def start_browser(self) -> None:
        """2. Headed Chromium on the pipe, and the CDP proxy on 127.0.0.1:9222."""
        self.startup.setdefault("browser_launching", self._wall())
        self.touch = TouchInjector(self.injector, self._current, self._current)
        self.control = ControlState(self.touch)
        current = self._current
        self.carry = SessionCarry(current.call, record=self.record, agent_connected=self._agent_connected,
                                  excluded_host=lambda: self.qa.host,
                                  excluded_origins=self.qa_target.excluded_origins)
        self.tabs = TabKeeper(current.call, record=self.record, agent_connected=self._agent_connected,
                              shown=self._shown_target)
        await self._launch()
        log.info("live gate: chromium up; CDP proxy on %s:%d", self.cfg.cdp_host, self.cfg.cdp_port)

    def _shown_target(self) -> str | None:
        """The tab the page follower shows: the one a park records as active."""
        return self.pages.current if self.pages is not None else None

    def _agent_connected(self) -> bool:
        return self._agent_seen_before or bool(self.proxy is not None and self.proxy.agent_seen)

    def _agents_changed(self, count: int) -> None:
        """The proxy accepted or closed an agent connection (LIVE-UNUSED).

        The first accepted one dates `agent_connected_at` and goes on the
        timeline as `agent_connected`, once; every change is reported, so the
        console's "the agent isn't using this browser" goes the moment it
        connects rather than on a later report.
        """
        if count > 0 and self.agent_connected_at is None:
            proxy = self.proxy
            at = proxy.agent_connected_at if proxy is not None else None
            self.agent_connected_at = at if at is not None else self._wall()
            self._tell_agent_connected()
        self._report_due.set()

    def _tell_agent_connected(self) -> None:
        """`agent_connected` on the timeline, once: now if a tunnel is open,
        else when one opens (`open_tunnel`), since with none nothing records."""
        if self._agent_connected_told or self.agent_connected_at is None:
            return
        if self.tunnel is None or self.tunnel.closed:
            return
        self._agent_connected_told = True
        self._queue_action({"kind": "control", "event": "agent_connected"})

    def _tool_used(self, tool: str, at: float) -> None:
        """The CDP proxy's `on_tool`: a tagged tool sent a command (BSTATUS-API)."""
        self.tool_last_used = (tool, at)
        self._report_due.set()

    def egress_report(self) -> dict[str, Any]:
        """The report's `browser.egress`: the forwarder's state, or `off`."""
        if not self.cfg.egress.proxy:
            return egress_report(proxied=False, ok=True, error=None)
        forwarder = self.egress
        if forwarder is None:
            return egress_report(proxied=True, ok=False, error=describe("forwarder_unreachable"))
        ok, cause = forwarder.healthy()
        return egress_report(proxied=True, ok=ok, error=None if ok else describe(cause or ""))

    def browser_report(self) -> dict[str, Any]:
        """The report's `browser` (report.py `browser_report`)."""
        return browser_report(tools=self.browser_tools, last_used=self.tool_last_used,
                              egress=self.egress_report())

    def agent_report(self) -> dict[str, Any]:
        """The report's `agent` (report.py `agent_report`)."""
        proxy = self.proxy
        return agent_report(
            connected_at=self.agent_connected_at,
            connections=proxy.agent_connections if proxy is not None and not self.browser_closed else 0,
            first_frame_at=self.startup.get("first_frame"),
        )

    async def _launch(self, previous: Device | None = None, touch_on: bool | None = None,
                      held_by: CdpProxy | None = None) -> None:
        """Chromium, its proxy, its devices and its page follower; the CDP proxy served last.

        On a relaunch: `previous` and `touch_on` are the device and the touch
        toggle the browser that exited had, applied to this one; `held_by` is
        its proxy, and while that still holds the agent for a hand-off the
        worker asked for (not a person's unasked take, which ended with the
        browser) the new proxy holds it too, until the worker resumes.
        """
        if self.cfg.egress.proxy and (self.egress is None or not self.cfg.egress_port):
            # Fail closed (owner decision Q7): never a browser without its proxy.
            raise SetupError("browser egress is on and its forwarder is not listening; "
                             "Chromium is not started without the proxy")
        browser = self.browser = await self.deps.start_chromium(self.cfg)
        self.proxy = GateProxy(
            self.browser.transport.send,
            paths=PolicyPaths(downloads_dir=self.cfg.downloads_dir, uploads_dir=self.cfg.uploads_dir),
            record=self.record,
            on_control_ended=self._control_ended,
            on_action=self._queue_action,
            on_pointer=self._agent_pointer,
            on_agents=self._agents_changed,
            on_tool=self._tool_used,
            wall=self._wall,
            qa=self.qa,
            qa_target=self.qa_target,
            cf_access=self.cf_access,
        )
        # A refused agent connection says when the gate takes control back.
        self.proxy.control_lease_seconds = self._lease_seconds
        if held_by is not None and held_by.paused and not self._self_paused:
            # Decided here, as the proxy is made: a resume before it let the
            # old proxy go, a resume after it lets this one go.
            await self.proxy.pause()
        if self.qa_injector is not None:
            await self.qa_injector.close()
        self.qa_injector = QaInjector(self.proxy, self.qa, record=self.record)
        if self.cf_injector is not None:
            await self.cf_injector.close()
        self.cf_injector = CfAccessInjector(self.proxy, self.cf_access, record=self.record)
        pipe = self._tasks["pipe"] = asyncio.create_task(self._read_pipe(browser))
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
        # CDP answered: the browser is up, whatever is applied to it next.
        self.startup.setdefault("browser_ready", self._wall())
        self.devices = DeviceController(self.proxy, self.deps.open_display(self.cfg),
                                        record=self.record, on_resize=self._on_resize,
                                        on_change=self._device_changed)
        try:
            await self.devices.start()
            if previous is not None and (previous.name, previous.orientation) != (
                    self.devices.device.name, self.devices.device.orientation):
                await self.devices.select(previous.name, previous.orientation)
        except Exception as exc:  # noqa: BLE001 -- the desktop still works
            preset = previous.name if previous is not None else DEFAULT_PRESET
            self.record("device_failed", {"preset": preset, "cause": type(exc).__name__})
            log.warning("live gate: device not applied (%s)", type(exc).__name__)
        if touch_on is not None and self.touch is not None:
            self.touch.set_touch(touch_on)
            self._device_changed(self.devices.device)
        self.pages = PageFollower(self.proxy, self.mask_state, on_origin=self._origin_changed,
                                  on_state=self._page_state)
        # The agent's own tab commands: the follower looks which tab is shown (LIVE-BLANK).
        self.proxy.on_tab_switch = self.pages.tab_switching
        self.proxy.on_tab_switched = self.pages.tab_switched
        await self.pages.start()
        if self.qa.armed and not self.qa.wiped:
            # A relaunch: the new browser is signed in before the agent reconnects.
            await self.qa_injector.start()
        if self.cf_access.held:
            # A relaunch: the listed hosts get the token on the new browser too.
            await self.cf_injector.start()
        if self.qa_target.armed and not self.qa_target.wiped:
            # A relaunch: the target's rules hold on the new browser too.
            await self.proxy.start_qa_target()
        self.cdp_server = await self.deps.serve_cdp(self.proxy, self.cfg.cdp_host, self.cfg.cdp_port)
        self._ready_browser = browser

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
        """4. Capture -> RFB; the tunnel opens on the worker's ticket."""
        self.framebuffer = self.deps.open_framebuffer(self.cfg)
        self._tasks["capture"] = asyncio.create_task(self._capture())

    # -- the pipe ---------------------------------------------------------------

    async def _read_pipe(self, browser: Any) -> None:
        proxy = self.proxy
        await browser.transport.run(self)
        if proxy is not None:
            # Whatever was asked of this browser is never answered now.
            await proxy.chromium_closed()
        if browser is not self._ready_browser or browser is not self.browser:
            return  # one still starting (`_launch` fails on it), or one already replaced
        self._ready_browser = None
        expected = self.wiped or self._stopping
        if not expected:
            log.warning("live gate: chromium closed its pipe")
        self.browser_closed = True
        if self.pages is not None:
            self.pages.halt()
        if self.devices is not None:
            for task in list(self.devices._tasks):
                task.cancel()
        if expected:
            return
        if self.cdp_server is not None:
            # Refused while no browser is there; served again on the same
            # address by the relaunch, so SWARM_CDP_ENDPOINT stays right.
            self.cdp_server.close()
        await self._browser_exited(browser)

    async def _browser_exited(self, browser: Any) -> None:
        """Chromium exited unexpectedly: say why, end a person's control, start it again or say it is gone.

        THE REASON IT DIED IS LOGGED (BROWSER-RELAUNCH; part of #1030): the
        exit code or signal and Chromium's last stderr lines, credentials
        masked and URLs cut to their origin. att_59f378aac5c340ca9507's
        browser closed and nothing said why.
        """
        exited = getattr(browser, "exited", None)
        code, tail = await exited(EXIT_WAIT_SECONDS) if exited is not None else (None, [])
        cause, signame = exit_cause(code)
        exit_fields = {"exit_code": code, "signal": signame}
        log.warning("live gate: chromium exited (%s); its last stderr lines: %s",
                    cause, json.dumps(redacted_stderr(tail)))
        self.record("browser_exited", exit_fields)
        if self.wiped or self._stopping:
            return  # wiped or stopped while it was exiting: closed as asked
        # The timeline says why the screen went grey, rather than nothing.
        await self._action({"kind": "control", "event": "browser_closed", "cause": cause})
        relaunch = self._may_relaunch()
        if relaunch:
            self.browser_state = "restarting"
        if self._episode is not None:
            # Nobody is left controlling a page that is gone; audited by the proxy (`control_ended`).
            await self._release("browser_restarted" if relaunch else "browser_closed")
        if not relaunch:
            self._give_up(cause, exit_fields)
            return
        previous = self.devices.device if self.devices is not None else None
        touch_on = self.touch.touch_on if self.touch is not None else None
        self._tasks["relaunch"] = asyncio.create_task(self._relaunch(cause, exit_fields, previous, touch_on))

    def _may_relaunch(self) -> bool:
        now = self._clock()
        self._relaunches = [t for t in self._relaunches if now - t < self._relaunch_window]
        return not (self.wiped or self._stopping) and len(self._relaunches) < self._relaunch_limit

    def _give_up(self, cause: str, exit_fields: Mapping[str, Any]) -> None:
        self.browser_state = "closed"
        log.warning("live gate: chromium stays closed: %d relaunches in %.0fs", len(self._relaunches),
                    self._relaunch_window)
        self.record("browser_gave_up", {**exit_fields, "restarts": self.restarts})
        self._spawn(self._action({"kind": "control", "event": "browser_gave_up", "cause": cause}))

    async def _relaunch(self, cause: str, exit_fields: Mapping[str, Any],
                        previous: Device | None, touch_on: bool | None) -> None:
        """Start Chromium again on the same profile, flags and policy, with backoff, within the budget."""
        old_proxy = self.proxy
        if old_proxy is not None and old_proxy.agent_seen:
            self._agent_seen_before = True
        tries = 0
        while True:
            if not self._may_relaunch():
                self._give_up(cause, exit_fields)
                return
            await asyncio.sleep(self._relaunch_delays[min(tries, len(self._relaunch_delays) - 1)])
            tries += 1
            if self.wiped or self._stopping:
                return
            self._relaunches.append(self._clock())
            await self._retire_browser()
            _clear_profile_locks(self.cfg.profile_dir)
            try:
                await self._launch(previous, touch_on, held_by=old_proxy)
            except Exception as exc:  # noqa: BLE001 -- tried again, within the budget
                self._ready_browser = None
                await self._retire_browser()
                log.warning("live gate: chromium relaunch failed (%s: %s)", type(exc).__name__,
                            exc if isinstance(exc, SetupError) else "")
                self.record("browser_relaunch_failed", {"cause": type(exc).__name__})
                continue
            break
        self._fresh_target = None
        self.browser_closed = False
        self.browser_state = "running"
        self.restarts += 1
        log.info("live gate: chromium relaunched (%d); CDP proxy on %s:%d", self.restarts,
                 self.cfg.cdp_host, self.cfg.cdp_port)
        self.record("browser_restarted", {**exit_fields, "restarts": self.restarts})
        await self._action({"kind": "control", "event": "browser_restarted", "cause": cause})

    async def _retire_browser(self) -> None:
        """What belonged to the browser that went: its follower, its device tasks, its process, its server."""
        if self.pages is not None:
            await self.pages.close()
        if self.devices is not None:
            tasks = list(self.devices._tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.cdp_server is not None:
            self.cdp_server.close()
        if self.browser is not None:
            try:
                await self.browser.stop()
            except Exception as exc:  # noqa: BLE001 -- it is gone either way
                log.info("live gate: stopping the old chromium failed (%s)", type(exc).__name__)

    def _spawn(self, work: Awaitable[Any]) -> None:
        task = asyncio.get_running_loop().create_task(work)
        self._spawned.add(task)
        task.add_done_callback(self._spawned.discard)

    async def on_chromium_message(self, message: Mapping[str, Any]) -> None:
        """PipeTransport's sink: the proxy first, then the gate's own listeners."""
        assert self.proxy is not None
        await self.proxy.on_chromium_message(message)
        if "method" in message:
            if self.qa_injector is not None:
                self.qa_injector.on_event(message)
            if self.cf_injector is not None:
                self.cf_injector.on_event(message)
            if self.devices is not None:
                self.devices.on_event(message)
            if self.pages is not None:
                self.pages.on_event(message)

    # -- capture ------------------------------------------------------------

    async def _capture(self) -> None:
        """Grab and publish, at `capture_fps` for who is there.

        The grab -- a 4 MB copy out of Xvfb's mapping, and the device crop --
        runs on a thread, so the event loop that reads the controller's input
        is never held by it. The frame is published as captured: nothing is
        painted over any part of it, in any situation (§5.3; owner decision,
        BGD 2026-10-25). The wait to the next frame counts the work already
        done, so 30 a second is 30, and it ends early when control changes
        hands.
        """
        loop = asyncio.get_running_loop()
        failing = False
        while True:
            await self._watching.wait()
            controlled = self.control is not None and self.control.controller is not None
            fps = capture_fps(watching=True, controlled=controlled)
            started = loop.time()
            device = self.devices.device if self.devices is not None else self.desktop
            try:
                frame = await asyncio.to_thread(self._grab, device)
            except Exception as exc:  # noqa: BLE001 -- see below
                # NOTHING IS SENT FOR THIS FRAME, AND THE LOOP GOES ON
                # (LIVE-BLANK). Only `CaptureError` was caught here, so any
                # other failure -- a header caught mid-rewrite -- ended the
                # capture task in silence: the viewer kept
                # the hub's first blank for good and `first_frame` never came.
                if not failing:
                    log.warning("live gate: capture failed (%s); nothing is sent",
                                exc if isinstance(exc, CaptureError) else type(exc).__name__)
                failing = True
                await self._pace(started + 1.0 / fps)
                continue
            failing = False
            self.hub.publish(frame, reason=self.mask_state.reason)
            # The first frame the stream is given: the capture runs only while
            # an RFB stream (a viewer, or the relay's recorder) is open, and
            # every stream sends from the hub this was published to.
            if "first_frame" not in self.startup:
                self.startup["first_frame"] = self._wall()
                # The console times "the agent isn't using this browser" from it.
                self._report_due.set()
            await self._pace(started + 1.0 / fps)

    def _grab(self, device: Any) -> Frame:
        """On a thread: the screen, cut to the device's frame."""
        return captured(self.framebuffer.grab(), device)

    async def _pace(self, until: float) -> None:
        """Wait for `until` on the loop's clock, or for control to change hands."""
        delay = until - asyncio.get_running_loop().time()
        if delay > 0:
            # Not `wait_for`: on 3.11 a cancel that lands as the event is set
            # -- the tunnel's cleanup releasing control as the gate is torn
            # down -- returns the wait's result and drops the cancel, and the
            # capture loop runs on after everything else has finished.
            try:
                async with asyncio.timeout(delay):
                    await self._rate_changed.wait()
            except TimeoutError:
                pass
        self._rate_changed.clear()

    def control_changed(self) -> None:
        """Control was granted or released: the capture rate follows at once."""
        self._rate_changed.set()

    def _agent_pointer(self, x: float, y: float) -> None:
        """The agent's `Input.dispatchMouseEvent`, in CSS pixels: where watchers see the pointer (pointer.py)."""
        device = self.devices.device if self.devices is not None else self.desktop
        at = css_to_frame(device, x, y)
        if at is not None:
            self.hub.pointer.move(*at)

    def _on_resize(self, device: Any) -> None:
        if self.touch is not None:
            self.touch.resize(device.display_width, device.display_height)

    # -- the report (report.py) -------------------------------------------------

    def _device_changed(self, device: Device) -> None:
        touch_on = self.touch.touch_on if self.touch is not None else True
        self.device_reported = device_report(device, touch_on)
        self._report_due.set()

    def _page_state(self, state: str, cause: str | None, target: str | None) -> None:
        """The page follower lost the agent's page, or has it again (DEVICE-RECOVER).

        Said on the timeline, so a grey screen is never silent: the gate's
        report has only `origin` and `device` (swarm-api's `parse_report`
        takes exactly those), so the strip cannot be told until the console
        and swarm-api read a page state too. When the page is back after a
        loss the current device is applied to it again: an emulation override
        lives on the session that set it, and that session may be the one
        that went.
        """
        previous, self.page_state = self.page_state, state
        self.record("page_" + state, {"cause": cause} if cause else {})
        if state == "reconnecting":
            self._spawn(self._action({"kind": "control", "event": "reconnecting_to_page",
                                      "cause": cause or "detached"}))
        elif state == "closed" and not self.browser_closed and not self.wiped:
            self._spawn(self._action({"kind": "control", "event": "agent_page_closed"}))
        elif state == "following" and previous in ("reconnecting", "closed"):
            self._spawn(self._action({"kind": "control", "event": "page_reconnected"}))
            if target is not None and self.devices is not None:
                self._spawn(self._reapply_device(target))

    async def _reapply_device(self, target: str) -> None:
        assert self.devices is not None
        try:
            await self.devices.attach_page(target)
        except Exception as exc:  # noqa: BLE001 -- the page went again; its next follow tries again
            self.record("device_failed", {"preset": self.devices.device.name, "cause": type(exc).__name__})

    def _origin_changed(self, origin: dict[str, Any] | None) -> None:
        if origin != self.origin:
            self.origin = origin
            self._report_due.set()

    async def _reporter(self, tunnel: Tunnel) -> None:
        """Send the latest report whenever it changes, and once as the tunnel opens.

        One task sends, and each send reads the state as it is then, so a
        burst of navigations ends with the relay holding the last one, never
        an older one overtaking it.
        """
        self._report_due.set()
        while not tunnel.closed:
            await self._report_due.wait()
            self._report_due.clear()
            try:
                payload = encode_report(self.origin, self.device_reported, self.agent_report(),
                                        self.browser_report())
            except ValueError:  # cannot happen within report.py's limits; never send a part
                payload = encode_report(None, self.device_reported, self.agent_report(),
                                        self.browser_report())
            await tunnel.report(payload)

    # -- ctl.sock -------------------------------------------------------------

    async def handle_ctl(self, op: str, args: dict[str, Any]) -> Mapping[str, Any]:
        if op == "status":
            return self.status()
        if op in SESSION_OPS:
            return await self._need(self.carry).handle(op, args)
        if op in TABS_OPS:
            return await self._need(self.tabs).handle(op, args)
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
        if op == "theme":
            return await self._need(self.devices).handle_theme(args)
        if op == "egress_ticket":
            return await self.egress_ticket(args)
        if op == browser_tools.CTL_OP:
            return self.set_browser_tools(args)
        if op == QA_IDENTITY_OP:
            return await self.qa_identity(args)
        if op == QA_SIGN_IN_OP:
            return await self.qa_sign_in(args)
        if op == CF_ACCESS_OP:
            return await self.set_cf_access(args)
        raise ValueError("not an operation of the gate")

    # -- the QA identity (qaidentity.py) -----------------------------------------

    async def qa_identity(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """The worker's `qa_identity`: set, renew or clear the task's QA identity.

        Refused after the wipe (`refused:wiped`) and for a malformed one
        (`refused:<field>`). The browser's sessions follow at once: the
        patterns guard 2 needs are enabled only while a credential is held."""
        if self.wiped:
            self.record("qa_identity_refused", {"cause": "wiped"})
            raise OpRefused("wiped")
        try:
            result = self.qa.handle(args)
        except QaIdentityRefused as exc:
            raise OpRefused(exc.code) from None
        injector = self.qa_injector
        if injector is not None and not self.browser_closed:
            await injector.refresh()
        return result

    async def set_cf_access(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """The worker's `cf_access` (cfaccess.py): the attempt's Cloudflare Access
        tokens and their hosts, once. Refused after the wipe, a second time,
        and for a malformed one (`refused:<field>`); never logged. The
        browser's sessions follow at once."""
        if self.wiped:
            self.record("cf_access_refused", {"cause": "wiped"})
            raise OpRefused("wiped")
        try:
            result = self.cf_access.handle(args)
        except CfAccessRefused as exc:
            raise OpRefused(exc.code) from None
        injector = self.cf_injector
        if injector is not None and not self.browser_closed:
            await injector.refresh()
        return result

    async def qa_sign_in(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """The worker's `qa_sign_in` (qatarget.py): set the QA target and start
        its held sign-in, or `{poll: true}` for how it ended. Refused after the
        wipe and for a malformed one (`refused:<field>`); never logged."""
        if self.wiped:
            self.record("qa_sign_in_refused", {"cause": "wiped"})
            raise OpRefused("wiped")
        proxy = self.proxy if not self.browser_closed else None
        try:
            return await qa_target_ctl(self.qa_target, proxy, args)
        except QaTargetRefused as exc:
            raise OpRefused(exc.code) from None

    async def _on_qa_host(self, expect: Any) -> bool:
        """A hand-off would be on the console host: named by `expect`, or the page shown."""
        host = self.qa.host
        if host is None:
            return False
        if isinstance(expect, str) and expect:
            # A bare host (handoff.expect_refusal), read as a URL's host too.
            try:
                named = urlsplit(expect if "://" in expect else f"https://{expect}").hostname
            except ValueError:
                named = None
            if named == host:
                return True
        target = self.pages.current if self.pages is not None else None
        if target is None or self.proxy is None:
            return False
        try:
            info = (await self.proxy.call("Target.getTargetInfo", {"targetId": target})).get("targetInfo") or {}
        except CdpError:
            return False
        try:
            return urlsplit(str(info.get("url", ""))).hostname == host
        except ValueError:
            return False

    def set_browser_tools(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """The worker's `browser_tools`: what the runner registered (BSTATUS-API).

        Checked again here (`browser_tools.clean_state`): it came from a file
        in the agent's reach. Reported on the next report; refused when it
        does not hold together."""
        state = browser_tools.clean_state(args)
        if state is None:
            raise ValueError("not a browser tool state")
        if state != self.browser_tools:
            self.browser_tools = state
            self._report_due.set()
        return {"browser_tools": "recorded"}

    # -- proxied egress (egress.py) ---------------------------------------------

    async def egress_ticket(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """The worker's `egress_ticket`: a ticket to present, or why none can be had.

        `{"ticket": ..., "expires_in": seconds}` arms the forwarder, after
        which swarm-egress is probed (a TCP connect, no byte sent): when it
        cannot be reached the answer says `failed` with
        `forwarder_unreachable`, and every CONNECT stays refused.
        `{"failed": cause}` -- the mint was refused, no attempt key, ... --
        leaves every CONNECT refused with that cause. Either way the browser
        is never sent direct. Refused when egress is off for this browser.
        """
        forwarder = self.egress
        if forwarder is None:
            raise ValueError("browser egress is off for this browser")
        failed = args.get("failed")
        if failed is not None:
            forwarder.fail(str(failed))
            return {"egress": "failed", "cause": forwarder.cause}
        forwarder.set_ticket(args.get("ticket"), args.get("expires_in"))
        self._report_due.set()
        if not await forwarder.probe():
            forwarder.unreachable()
            self.record("egress_refused", {"cause": "forwarder_unreachable"})
            self._egress_unavailable("forwarder_unreachable")
            return {"egress": "failed", "cause": "forwarder_unreachable"}
        return {"egress": "ready"}

    def _egress_record(self, event: str, fields: Mapping[str, Any]) -> None:
        """The forwarder's records, and a report: its state may have changed."""
        self.record(event, fields)
        self._report_due.set()

    def _egress_unavailable(self, cause: str) -> None:
        """Proxied egress is unavailable: put a page on screen that says why.

        The forwarder calls this once per cause. The page is a data: URL, so
        it loads with no network; it is what the person watching sees and
        what the agent reads when it looks at the browser.
        """
        self._spawn(self._show_egress_notice(cause))

    async def _show_egress_notice(self, cause: str) -> None:
        proxy = self.proxy
        if proxy is None or self.browser_closed or self.wiped:
            return
        try:
            await proxy.call("Target.createTarget", {"url": notice_url(cause)})
            self.record("egress_notice", {"cause": cause})
        except Exception as exc:  # noqa: BLE001 -- the refusals themselves still fail closed
            self.record("egress_notice_failed", {"cause": cause, "error": type(exc).__name__})

    def _need(self, component: Any) -> Any:
        """`component`, or `GateDown` saying why the browser is not there to serve
        it (GATE-STAYS-UP; #2205): the worker waits out `starting` and
        `restarting` and tells the agent which it was, where a bare `failed`
        read as a refusal of the preset itself."""
        if self.wiped:
            raise GateDown("wiped")
        if self.browser_closed or component is None:
            raise GateDown(self.down_reason())
        return component

    def down_reason(self) -> str:
        """Why the browser is not serving now: `crashed` once the relaunch budget
        is spent (`browser_state` `closed`), `restarting` from the exit until a
        relaunch is up or given up, `starting` before the first launch is done."""
        if self.browser_state == "closed":
            return "crashed"
        if self.browser_closed or self.browser_state == "restarting":
            return "restarting"
        return "starting"

    def status(self) -> dict[str, Any]:
        device = self.devices.device if self.devices is not None else self.desktop
        if self.tunnel is None:
            tunnel = "none"
        else:
            tunnel = "closed" if self.tunnel.closed else "open"
        return {
            "browser": "wiped" if self.wiped else (
                "running" if self.browser is not None and not self.browser_closed
                else "restarting" if self.browser_state == "restarting" else "closed"),
            "browser_restarts": self.restarts,
            "paused": bool(self.proxy is not None and self.proxy.paused),
            "in_control": self._episode is not None,
            "tunnel": tunnel,
            # RELAY-ROLL: 1012 from a restarting relay is reopened at once
            # by the worker (handoff.HandoffPath._keep_tunnel).
            "tunnel_close_code": getattr(self.tunnel, "close_code", None),
            "viewers": self.viewers,
            "device": device.name,
            "orientation": device.orientation,
            "last_episode": self.last_episode,
            "startup": dict(self.startup),
            "page": self.page_state,
            # LIVE-UNUSED: when the agent first drove this browser (epoch
            # seconds, null until it has) and how many CDP connections it
            # holds now.
            "agent_connected_at": self.agent_connected_at,
            "agent_connections": self.agent_report()["connections"],
            # EGRESS-GATE: the browser's route and, when proxied, its state
            # and counts (egress.py `stats`); never the ticket.
            "egress": self.egress.stats() if self.egress is not None else {"route": "direct"},
            # QI-GATE: whether the QA identity is held, never the credential.
            "qa_identity": self.qa.status(),
            # VQ-4: the QA target's sign-in state, never the credential.
            "qa_target": self.qa_target.status(),
            # CF-ACCESS-TOKEN: the listed hosts and counts, never a value.
            "cf_access": self.cf_access.status(),
            # CAPTCHA-HANDOFF: the hand-off the gate asks for, or None.
            "challenge": dict(self.challenge) if self.challenge is not None else None,
        }

    def _on_challenge(self, host: str) -> None:
        """The page watch (mask.py) saw a challenge on the page it follows: ask for a person (module docstring).

        Not on the gate's own QA sign-in tab (`qa_target.signing_in`: its
        challenge ends the sign-in), and not while a person already holds the
        browser or the agent is held for one: the person sees it there.
        """
        if self.qa_target.signing_in or self._handed_off or self._episode is not None:
            return
        self.challenge = {"request_id": "gate-" + secrets.token_hex(6),
                          "reason": CHALLENGE_REASON.format(host=host), "expect": host}
        self.record("handoff_challenge_raised", {"host": host})

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
        self.mux = StreamMux(tunnel, self._serve_rfb, self._on_control, self._on_device)
        self._tasks["tunnel"] = asyncio.create_task(self._run_tunnel(tunnel, self.mux))
        self._tasks["report"] = asyncio.create_task(self._reporter(tunnel))
        previous = self._tasks.get("actions")
        if previous is not None:
            previous.cancel()
        self._tasks["actions"] = asyncio.create_task(self._send_actions(tunnel))
        # An agent that connected before the tunnel opened is said now.
        self._tell_agent_connected()
        self.record("tunnel_open", {})
        return {"tunnel": "open"}

    async def _run_tunnel(self, tunnel: Tunnel, mux: StreamMux) -> None:
        code: int | None = None
        cancelled = False
        try:
            code = await tunnel.run(mux.on_frame)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            reporter = self._tasks.get("report")
            if reporter is not None:
                reporter.cancel()
            await mux.close()
            if self._episode is not None:
                if cancelled:
                    # Bounded, and the cancel still propagates (HANG-LEASE):
                    # see CANCELLED_RELEASE_SECONDS. Cut short, the release
                    # skips what waits on Chromium or the relay (the lift,
                    # the page read, the action, the resume); the proxy's
                    # `resume`, once started, leaves its hold in its `finally`.
                    try:
                        await asyncio.wait_for(self._release("tunnel_closed"), CANCELLED_RELEASE_SECONDS)
                    except asyncio.TimeoutError:
                        self.record("release_degraded", {"step": "cancelled", "cause": "TimeoutError"})
                else:
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

    async def _on_control(self, conn_id: str, payload: bytes = b"") -> None:
        """The relay's CONTROL: swarm-api granted `conn_id` control, or none
        (empty, with why in `payload` when swarm-api revoked it). The
        controller named again is a renewal of the control lease."""
        if conn_id:
            if self._episode is not None and self.control is not None and self.control.holds(conn_id):
                self._renew_lease()
                return
            # `asyncio.timeout`, not `wait_for`: on Python 3.11 a cancel that
            # lands as the grant finishes is swallowed by `wait_for`, which
            # returns the grant's result instead, so the tunnel task read on
            # with control held and its cleanup never ended it (FLAKE-LIVEGATE,
            # part of #2230; reproduced 2026-10-29 under CPU load in
            # test_a_gate_cancelled_while_a_person_holds_control_finishes).
            try:
                async with asyncio.timeout(CONTROL_GRANT_SECONDS):
                    await self._grant(conn_id)
            except asyncio.TimeoutError:
                await self._refused("grant_timeout")
        else:
            await self._release(_revoke_cause(payload))

    def _renew_lease(self) -> None:
        """Control goes on for another `CONTROL_LEASE_SECONDS`; the watch
        that ends it when no renewal comes is started if it is not running."""
        self._lease_until = self._clock() + self._lease_seconds
        watch = self._tasks.get("lease")
        if watch is None or watch.done():
            self._tasks["lease"] = asyncio.create_task(self._watch_lease())

    async def _watch_lease(self) -> None:
        """End control the relay stopped renewing (`CONTROL_LEASE_SECONDS`):
        `control_ended` with cause `lease_expired`, and the agent resumes."""
        while self._episode is not None and self._lease_until is not None:
            remaining = self._lease_until - self._clock()
            if remaining <= 0:
                self.record("control_lease_expired", {"lease_seconds": self._lease_seconds})
                await self._release("lease_expired")
                return
            await asyncio.sleep(min(remaining, 1.0))

    # -- the controller's device (§6.1, Q13; LB-DEV-B) -------------------------

    async def _on_device(self, payload: bytes) -> None:
        """The relay's DEVICE: swarm-api checked that the person asking holds
        control and MAC'd their choice. Applied on its own task -- a resize and
        a CDP round per page must not hold up the tunnel's RFB frames -- one at
        a time, in order."""
        self._device_seq += 1
        self._spawn(self._apply_device_frame(payload, self._device_seq))

    async def _apply_device_frame(self, payload: bytes, seq: int | None = None) -> None:
        async with self._device_lock:
            if seq is not None and seq != self._device_seq:
                # A newer choice is queued behind this one: only the latest is
                # applied (eight switches in 2.5 minutes, 2026-10-13).
                self.record("device_superseded", {})
                return
            try:
                await self.apply_device(payload)
            except Exception as exc:  # noqa: BLE001 -- refused or failed: recorded, the view goes on
                log.info("live device: relay request not applied (%s)", type(exc).__name__)

    async def apply_device(self, payload: bytes) -> Device:
        """A `DEVICE` frame's choice, through the same `DeviceController.select`
        the ctl `device` operation uses, plus the person's touch toggle. The
        result reaches swarm-api as the next report (`_device_changed`)."""
        try:
            choice = json.loads(payload) if len(payload) <= MAX_DEVICE_FRAME else None
        except ValueError:
            choice = None
        touch_on = choice.get("touch") if isinstance(choice, dict) else None
        if (not isinstance(choice, dict) or set(choice) != DEVICE_FRAME_KEYS
                or not (touch_on is None or isinstance(touch_on, bool))):
            await self._device_refused("not_a_preset_name")
            raise ValueError("a device is chosen by preset name only")
        devices = self._need(self.devices)
        touch = self._need(self.touch)
        try:
            wanted = resolve(choice["preset"], choice["orientation"])
        except UnknownPreset:
            await self._device_refused("unknown_preset")
            raise
        if touch_on and not wanted.touch:
            await self._device_refused("no_touch")
            raise UnknownPreset("this preset has no touch")
        device = devices.device
        if (wanted.name, wanted.orientation) != (device.name, device.orientation):
            # Changes nothing when it fails, the toggle included.
            try:
                device = await devices.select(wanted.name, wanted.orientation)
            except Exception:
                # `select` keeps its own record; the timeline is told here.
                await self._action({"kind": "control", "event": "device_refused",
                                    "cause": "not_applied"})
                raise
        if touch_on is not None:
            touch.set_touch(touch_on)
        self._device_changed(device)
        return device

    async def _device_refused(self, cause: str) -> None:
        """A device the person chose that the gate did not apply: in the
        gate's record and, by its cause alone, the recording's action log
        (LB-V3), so the timeline does not show a change that never happened."""
        self.record("device_refused", {"cause": cause})
        await self._action({"kind": "control", "event": "device_refused", "cause": cause})

    async def _grant(self, conn_id: str) -> None:
        proxy, control, touch = self.proxy, self.control, self.touch
        if proxy is None or control is None or touch is None or self.wiped or self.browser_closed:
            return await self._refused("browser_restarting" if self.browser_state == "restarting"
                                       else "browser_closed")
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
        # The episode opens with the controller, no await between them. This
        # runs on the tunnel task, and the tunnel's cleanup ends control only
        # when there is an episode: opened after `_bring_to_front`'s CDP round,
        # a cancel during it left the controller named with nothing to end it
        # (FLAKE-LIVEGATE, #2230; runs 40086650895 and 40090460194).
        if self._episode is None:
            self._episode = _Episode(target=target, since=time.monotonic(), conn_id=conn_id)
        else:
            self._episode.conn_id = conn_id
        self._had_control = True
        self._renew_lease()
        self.control_changed()
        touch.begin(target)
        await self._bring_to_front(target)
        await self._action({"kind": "control", "event": "granted"})

    async def _bring_to_front(self, target: str) -> None:
        """The controlled page is the tab on screen: what is streamed, and
        where X input lands. An agent that opened a second tab may have left
        its page behind it; the person's clicks must not go to the other one."""
        assert self.proxy is not None
        try:
            await self.proxy.call("Target.activateTarget", {"targetId": target})
        except Exception as exc:  # noqa: BLE001 -- control is granted; the page may already be in front
            self.record("activate_failed", {"cause": type(exc).__name__})

    async def _refused(self, cause: str) -> None:
        await self._action({"kind": "control", "event": "refused", "cause": cause})
        if self._self_paused and self._episode is None and self.proxy is not None:
            self._self_paused = False
            await self.proxy.resume()

    async def _release(self, cause: str) -> None:
        """Control ends: no input reaches the display from here.

        Lifting a finger is a CDP command on the gate's session on the page,
        and that session may be gone with the page (or the browser): MEASURED
        2026-10-13, a lift on a lost session raised out of here, so the
        episode never ended and the resume came back `GateRefused`. The
        controller is cleared either way; a lift that cannot be sent has
        nothing left to lift (`TouchInjector._lift`, `ControlState.revoke`).
        """
        self._lease_until = None
        if self.control is not None:
            try:
                await asyncio.wait_for(self.control.revoke(), PAGE_INFO_SECONDS)
            except Exception as exc:  # noqa: BLE001 -- see above
                self.record("release_degraded", {"step": "revoke", "cause": type(exc).__name__})
            self.control_changed()
        if self.touch is not None:
            try:
                await asyncio.wait_for(self.touch.end(), PAGE_INFO_SECONDS)
            except Exception as exc:  # noqa: BLE001 -- see above
                self.record("release_degraded", {"step": "touch", "cause": type(exc).__name__})
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
        self._spawn(self._release(cause))

    async def _page_info(self, target: str) -> dict[str, Any]:
        if self.proxy is None or self.wiped or self.browser_closed:
            return {}
        try:
            # Bounded: on shutdown the pipe's reader may already be gone, and an
            # episode ended then waited for a reply forever, so the gate never
            # stopped while a person was in control.
            reply = await asyncio.wait_for(
                self.proxy.call("Target.getTargetInfo", {"targetId": target}), PAGE_INFO_SECONDS)
            info = reply.get("targetInfo") or {}
        except Exception:  # noqa: BLE001 -- the page closed, or Chromium no longer answers
            return {}
        return {"url": str(info.get("url", "")), "title": str(info.get("title", ""))}

    async def _action(self, entry: Mapping[str, Any]) -> None:
        if self.tunnel is not None and not self.tunnel.closed:
            await self.tunnel.action(entry)

    def _queue_action(self, entry: dict[str, Any]) -> None:
        """The proxy's agent method line (§7). Kept only while a tunnel is
        open -- with none, nothing records -- and dropped when the queue is full."""
        if self.tunnel is None or self.tunnel.closed:
            return
        try:
            self._actions.put_nowait(entry)
        except asyncio.QueueFull:
            pass

    async def _send_actions(self, tunnel: Tunnel) -> None:
        """Send the queued agent method lines, in the order they were forwarded."""
        while not tunnel.closed:
            entry = await self._actions.get()
            if not tunnel.closed:
                await tunnel.action(entry)

    async def _resume_agent(self) -> None:
        assert self.proxy is not None
        try:
            if self.browser_closed or self._stopping:
                # Nothing answers on a closed pipe: bounded, and the proxy
                # leaves its hold however `resume` ends (its `finally`). The
                # same while stopping: the pipe's reader is cancelled with the
                # gate's tasks, and a person still in control when the task
                # ended (CONTROL-LAPSE) waited here forever, so the gate never
                # finished stopping.
                await asyncio.wait_for(self.proxy.resume(), PAGE_INFO_SECONDS)
            else:
                await self.proxy.resume()
        except Exception as exc:  # noqa: BLE001 -- the agent is let go either way
            self.record("resume_degraded", {"cause": type(exc).__name__})
        if self._had_control and self.carry is not None:
            self.carry.note_returned()
        self._had_control = False
        self._fresh_target = None

    async def pause(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """§4.1 step 2: hold the agent, on the page it is on.

        THE PERSON TAKES THE AGENT'S OWN TAB (LB-CURSOR2; part of #1030). The
        fresh page (§5.4.3) is opened only when it is asked for by name
        (`fresh_page: true`); `expect` alone no longer opens one. MEASURED
        2026-10-13 (task_1d1785b21d5b4e1f894e, generation 1): the agent asked
        for help on https://httpbin.org/forms/post with `expect` httpbin.org;
        the gate opened https://httpbin.org/ in a context of its own, the
        stream and the person's input went there, and the agent's form tab sat
        hidden and empty behind it -- "a new tab nobody asked for". Owner
        decision, the same day: taking control keeps the agent's current tab.
        """
        proxy = self._need(self.proxy)
        if await self._on_qa_host(args.get("expect")):
            # Q7: a person's sign-in in a browser the agent drives as the QA
            # account would hand the agent that person's workspaces.
            self.record("handoff_refused", {"cause": "qa_identity_host"})
            raise OpRefused("qa_identity_host")
        self._self_paused = False
        await proxy.pause()
        self._handed_off = True
        # The gate's own request is answered by this hand-off, whoever asked.
        self.challenge = None
        expect = args.get("expect")
        if (args.get("fresh_page") is True and isinstance(expect, str) and expect
                and _HOST.fullmatch(expect)):
            try:
                self._fresh_target = await proxy.open_fresh_page(f"https://{expect}/")
                if self.devices is not None:
                    await self.devices.attach_page(self._fresh_target)
            except Exception as exc:  # noqa: BLE001 -- the person can still use the agent's page
                self._fresh_target = None
                self.record("fresh_page_failed", {"cause": type(exc).__name__})
        return {"paused": True, "fresh_page": self._fresh_target is not None}

    async def resume(self) -> dict[str, Any]:
        """§4.2 step 3: clear, restore, release the agent; what page it is on.

        Never refused for a lost page or a closed browser (DEVICE-RECOVER):
        MEASURED 2026-10-13 (att_59f378aac5c340ca9507), after the browser went
        during device switches the worker's resume came back `GateRefused`
        ("the gate did not confirm the resume"). The agent is held by the
        proxy, and only this lets it go; with no browser there is nothing to
        restore and no page to name, so the answer is `{}`.
        """
        proxy = self.proxy
        if proxy is None or self.wiped:
            raise RuntimeError("the browser is not running")
        self._handed_off = False
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
        self._handed_off = False
        await self._release(cause)
        wipe = args.get("wipe") is True
        if wipe:
            await self.wipe(cause)
        return {"ended": True, "wiped": self.wiped}

    async def wipe(self, cause: str) -> None:
        """§5.5: Chromium is closed and the profile directory emptied."""
        if self.wiped:
            return
        # The credential goes before Chromium does (§4.4), and the verb is refused from here.
        self.qa.wipe()
        self.qa_target.wipe()
        self.cf_access.wipe()
        if self.qa_injector is not None:
            await self.qa_injector.close()
        if self.cf_injector is not None:
            await self.cf_injector.close()
        self.wiped = True
        self._handed_off = False
        relaunch = self._tasks.pop("relaunch", None)
        if relaunch is not None:
            relaunch.cancel()
            await asyncio.gather(relaunch, return_exceptions=True)
        if self.cdp_server is not None:
            self.cdp_server.close()
        if self.browser is not None:
            await self.browser.stop()
        # GREY-TAIL (part of #1344): at the attempt's end the viewer is told
        # the agent finished until the pod stops, not that the gate lost the page.
        if cause == "attempt_end":
            self.mask_state.browser_closed()
        self._origin_changed(None)
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
        self._stopping = True
        steps: list[tuple[str, Callable[[], Awaitable[None]]]] = [
            ("tunnel", self._stop_tunnel),
            ("ctl", self._stop_ctl),
            ("cdp", self._stop_cdp),
            ("tasks", self._stop_tasks),
            ("injector", self._stop_injector),
            ("chromium", self._stop_browser),
            ("xvfb", self._stop_xvfb),
            ("egress", self._stop_egress),
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

    async def _stop_egress(self) -> None:
        if self.egress is not None:
            await self.egress.close()


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
        except (RuntimeError, ValueError):  # a loop without signal support raises a RuntimeError subclass
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


def configure_metrics_logging(stream: Any = None) -> None:
    """The per-viewer metrics line (rfb.py, LIVE-MEASURE) as the bare JSON
    object `rfb.log_viewer_metrics` formats, nothing in front of it. Cloud
    Logging parses a container's line into jsonPayload only when the whole
    line is JSON, and scripts/live-report.sh queries its fields; every other
    gate line keeps the text format `main` sets."""
    handler = logging.StreamHandler(sys.stderr if stream is None else stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    metrics = logging.getLogger(rfb_metrics_log.name)
    metrics.handlers = [handler]
    metrics.setLevel(logging.INFO)
    metrics.propagate = False


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
    configure_metrics_logging()
    try:
        cfg = GateConfig.from_env(os.environ)
    except SetupError as exc:
        log.error("live gate: fatal: %s", exc)
        return 1
    return asyncio.run(run(cfg))


if __name__ == "__main__":
    sys.exit(main())
