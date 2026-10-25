"""The CDP proxy: the agent's only way to Chromium (live browser L2).

docs/design/live-browser.md sections 3.2, 5.4.3 and 5.4.4. The gate launches
Chromium with `--remote-debugging-pipe` -- no debugging port at all -- and holds
the pipe. The agent's Playwright connects to this proxy on 127.0.0.1:9222
(`chromium.connectOverCDP`), and every message it sends passes the method
policy (cdppolicy.py) before it reaches the pipe. So "the agent's actions are
paused while a person is in control" is enforced here, not asked for.

ONE PIPE, SEVERAL CLIENTS. Chromium sees a single connection. The gate uses
its root session; each agent connection gets its OWN browser session
(`Target.attachToBrowserTarget`), so the agent's auto-attach settings are its
own, and every session created under it -- by `attachToTarget` or by
auto-attach -- is recorded as that connection's. A message naming a session
the connection does not own is refused, which is what keeps the agent off the
gate's sessions (the security watch, the fresh page). Command ids are
re-numbered on the way in and restored on the way out, so the gate's and the
agents' ids never collide.

CONTROL MODE (`pause` .. `resume`), section 5.4.3:

* every agent command is HELD (Playwright's call blocks) and released, in the
  order it arrived, on return; a new agent connection is refused;
* every event to the agent is DROPPED, and the ids of requests that started
  in the window are kept: after return their `postData` is stripped from any
  event, and `Network.getRequestPostData` / `getResponseBody` are refused
  for them. What the dropped events said about the page's STATE is told on
  return, once, as it stands then (HANDOFF-RESYNC, below);
* a response to a command sent BEFORE control that completes during it is
  held, and if it carries page content (`CONTENT_METHODS`) it is replaced by
  an error on return -- an `awaitPromise` waiting on the password field
  resolves during control, and must not carry what was typed;
* `Fetch.requestPaused` and `Fetch.authRequired` are answered by the proxy
  (`continueRequest`, `continueWithAuth: Default`): an agent that enabled
  interception would otherwise freeze the sign-in on a handler that is held;
* the agent's page scripts and bindings (scripts.py) are removed, and
  restored on return;
* a page that auto-attaches to the agent in the window is DETACHED from the
  agent: it would otherwise wait for a debugger whose commands are held, and
  report to the agent once released.

A REFUSED AGENT IS TOLD WHY (CONTROL-LAPSE; part of #1030). A new agent
connection while the agent is held is answered 503 with a `Retry-After` and a
one-line reason (`refusal`): a person is in control, or the agent is held for
a hand-off, and since when -- so an agent can wait sensibly instead of
retrying blind. MEASURED 2026-10-13: 72 bare refusals in 54 minutes on one
task. The reason names no person: the agent's output is logged.

THE FRESH PAGE. `open_fresh_page` creates a browser context on the gate's own
root session, with no proxy parameters and downloads set to `downloads/`, and
opens the hand-off's site in it: a document no agent script touched, in a
context whose network settings no agent call set. On return the context is the
agent's, and the proxy attaches the agent's auto-attaching browser session to
its page so Playwright sees it.

THE CERTIFICATE RULE (section 5.4.4). The gate's own session on the controlled
page watches `Security.visibleSecurityStateChanged` (the gate's session, so
the method policy does not apply to it). `take_control` is refused unless the
page is `secure`, served over https, and not at an IP literal; and control
ENDS if the state leaves `secure` while held. An unknown state is not secure:
the check fails closed.

LOOPBACK IS SECURE (GATE-LOOPBACK, part of #1333). BGD decided on 2026-10-22
that a page whose origin is exactly loopback -- scheme `http`, host
`127.0.0.1`, `::1` or `localhost`, any port, no userinfo -- counts as secure,
as Chromium counts it a secure context; the live-browser acceptance fixture
is served there. Chromium rates such a page `neutral`, never `secure`, so for
it the state is not consulted. Nothing else is loosened: every other http
host (private ranges, 0.0.0.0, other 127/8 addresses, `*.localhost`,
IPv4-mapped IPv6), userinfo, and every other scheme stay refused, and https
is judged as above. `_secure_origin` is the one rule every
`insecure_connection` site applies. A state that is not `secure` while held
on a loopback page reads the URL again, as on about:blank below, so a page
that has left loopback for a broken certificate still ends control.

ABOUT:BLANK HAS NO SCHEME TO JUDGE (lane LB-R4). A page that has not made its
first navigation is `about:blank`: no origin, nothing from any network, and a
security state Chromium never reports as `secure`. MEASURED 2026-10-12: a
person who took control of the agent's page before its first navigation read
"You took control", then "Taking control was refused: the page is not on a
secure https connection". So control on `about:blank` is granted without
waiting on the security state, and the certificate rule starts at the first
navigation: the gate's session also watches `Page.frameNavigated`, and a top
frame that commits anything but a named https host ends control; a state that
is not `secure` while held on a page granted blank ends control unless the
page is, read again, still `about:blank` (or by then secure https). Either
signal alone ends it, in either order, so the exception never outlives the
blank page.

THE ORDER ON RETURN (section 4.2, step 3): clear every password input the
gate's sessions can reach, restore the agent's scripts, hand the context over,
deliver the held responses, then release the held commands. A cross-origin
frame is a target of its own; the gate auto-attaches to those on the watched
page, so the clear reaches them too.

This module never awaits a reply inside `on_chromium_message`: the pipe reader
delivers replies one at a time, so a handler that waited on one would wait on
itself. Commands the proxy sends from there are fire-and-forget.

Nothing here is logged but a quiet refusal (`cdppolicy.QUIET_REFUSALS`), at
debug, by method name and count. A refusal is recorded through `record` by
method name only; a page's content, a request body, a key or a parameter never
is.

THE AGENT GETS ITS PAGE BACK (HANDOFF-RESYNC; DEVB-GATE, part of #1030).
Dropping events in control has a cost the drop alone does not show: when the
person navigates, Chromium destroys the page's execution contexts and makes new
ones, and the agent's client -- Playwright and Puppeteer alike keep a context
id per frame and world, learnt only from `Runtime.executionContextCreated` --
never hears of either. Its first evaluate after return names a context
Chromium no longer has ("Cannot find context with specified id"), and its
frame still has the URL the person left. MEASURED by
tests/unit/worker/test_live_cdp_clients.py against a modelled Chromium, red
before this.

So while the agent is held, the proxy keeps a JOURNAL per agent session of what
the dropped events did to the page's state, and on return -- after the
hand-over, before the held replies and commands -- tells the agent the NET
result, in the order Chromium itself would: `Runtime.executionContextsCleared`;
per frame that changed, its attach, last `frameNavigated`, last
`navigatedWithinDocument`, the lifecycle events of its current document and its
last loading event, or its detach; each target's last `targetInfoChanged`; then
`Runtime.executionContextCreated` for every context live now. The proxy knows
which those are because it tracks every agent session's contexts from the
events it relays, held or not. Then, because the agent's page scripts were
lifted for the window (above), a document the person loaded has no utility
world -- the `worldName` script Playwright and Puppeteer both install, and
both wait on for actionability and `waitForSelector` -- so the proxy asks
Chromium for one (`Page.createIsolatedWorld`) in every frame that lacks it, on
the agent's session, and the agent hears of it as it would of any.

WHY THIS AND NOT THE ALTERNATIVES. `Target.detachedFromTarget` and a
re-attach resync everything, but both clients read a detach as the page
CLOSING: Playwright MCP's tab and a script's `page` die, and only dev-browser
(which re-attaches named pages by targetId) would survive it.
`Runtime.disable` + `Runtime.enable` re-reports the live contexts, but releases
every remote object the agent holds even when nothing navigated, and says
nothing of the frame. Replaying the window's events as they happened would
show the agent the URLs the person passed through -- an OAuth callback's
`code` among them -- which it never sees this way: only the state at return,
which the agent's page can read for itself the moment it is released anyway.
A hand-off with no navigation journals nothing and replays nothing, so the
agent's handles stay valid. Known limit: a frame the person's navigation
created and removed again is never mentioned, and an agent command HELD during
control was written against the old context and fails on release, honestly.

THE ACTION LOG (section 7; lane LB-V3). Every agent method the proxy forwards,
and every one it refuses, is handed to `on_action` as one line (`action_line`):
the method's name, a navigation's URL with no query, fragment or user name
(`action_url`), and `Input.insertText` as a LENGTH. Nothing else of the
parameters, so a line can never carry typed text, a key or a cookie. The gate
sends the lines to the relay, which writes the recording's action log.

THE AGENT'S POINTER (LB-CURSOR; part of #1030). The x and y of every
`Input.dispatchMouseEvent` the proxy forwards are handed to `on_pointer`, so
the gate can paint the pointer where the agent left it (pointer.py). They go
nowhere else: not into the action log, not into a log line.

WHETHER THE AGENT IS USING THIS BROWSER (LIVE-UNUSED; part of #1030). On
task_7cda0d5335f049b4b442 (2026-10-13) the agent did its work in a browser of
its own while the live view, which nobody had told it to use, stayed blank,
and the console said nothing. An agent drives this browser only through this
proxy, so the proxy is the one place that knows: `agent_connected_at` is the
wall time the FIRST agent connection was accepted (None until one is), and
`agent_connections` how many are open now. `on_agents` is told the count on
every accept and close, so the gate can report it to the console the moment
it changes rather than on a poll.

WHICH TOOL DROVE IT (BSTATUS-API; part of #1030). Each of the agent's browser
tools connects with a CLIENT TAG on its endpoint
(`agent_worker.browser_tools.tag_endpoint`): `http://127.0.0.1:9222/client/<tool>`.
`split_client` reads the tag off the path (or a `client=` query) and STRIPS
it: the version answer and the websocket path are matched on the bare path,
and nothing of the tag is ever sent to Chromium. The version answer echoes it
into the websocket URL it hands back, so the connection that follows carries
it too. A tagged connection is that tool's; each command it sends dates
`tool_last_used`, and `on_tool` is told on a change of tool and at most every
`TOOL_REPORT_SECONDS` otherwise, so a busy agent is not a report per command.
An untagged connection, or a tag naming no tool, is served exactly as before
and attributed to nothing.

THE QA IDENTITY (QI-GATE; docs/design/qa-identity.md §4.4, part of #1649).
When the gate holds the task's QA identity (`qa`, qaidentity.py), every
message this proxy sends an agent -- event, reply, held reply, resync -- goes
through `QaIdentity.scrub` on the agent's link (`_ScrubbedLink`), so the
credential the gate adds to console requests never reaches the agent, by any
domain. The proxy also keeps which of the agent's request ids were on the
console host (`ConsoleRequests`), for the method policy's
`qa_identity_check` and for a `getResponseBody` the scrub cannot decode
(`qa_identity_body`). And a person never takes control of a page on the
console host (`qa_identity_host`, Q7): they would sign in as themselves in a
browser the agent then drives with their workspaces. Control already held
ends if the page navigates there.

THE QA TARGET (VQ-4; docs/design/visual-qa-any-app.md §2.2 to §2.4 and §3,
part of #1933). When the gate holds the attempt's QA target (`qa_target`,
qatarget.py):

* every agent-bound message passes `QaTarget.scrub` on the agent's link, so
  the password, URL-encoded or base64, reaches the agent by no domain; a
  base64 body the scrub cannot decode is refused (`qa_target_body`);
* the target's guard (`QaTargetGuard`) is fed every event from the pipe, and
  enforces §3's read-only, origin and rate rules on sessions the gate owns;
* the agent may not continue a paused request rewritten
  (`QaTarget.agent_refusal`), which would route around the guard;
* a challenge bypass the target's owner configured (§2.5, lane
  CAPTCHA-BYPASS) is added by that guard alone, on the target's approved
  origins alone: a header on the requests it continues, or a host-only
  `HttpOnly` cookie it plants on this pipe; its value is one more scrub
  needle on the agent's link;
* `qa_sign_in` is THE HELD SIGN-IN: the agent is held exactly as for a
  hand-off (`pause`), the gate signs in on a tab of its own (`SignIn`), and
  the return keeps §2.2 step 4's order -- clear the gate tab's password
  inputs and close it, send the agent's tab to `base_url`, then `resume`,
  which clears every password input on the gate's sessions, delivers the
  held responses and only then releases the held commands. A person cannot
  take control while it runs (`qa_sign_in_in_progress`), and it does not
  start while a person is in control or the agent is already held
  (`qa_sign_in_busy`). An agent connection refused meanwhile is told why
  (`held_for_qa_sign_in`).

THE DEVICE AGAIN AFTER A CLIPPED CAPTURE (QA-REASSERT; part of #1981). A
`Page.captureScreenshot` carrying a `clip`, sent from an agent's session,
lifts the device the gate set on that page from its own session (device.py
says what was measured). After the reply to one is relayed (or held, in
control), the proxy runs the hook registered with `after_clipped_capture`
for the page's targetId, on a task of its own: the hook waits on replies,
and this runs inside the pipe reader. The agent's command and reply pass
through unchanged, and nothing about what an agent may send changes.

AN AGENT THAT GOES AWAY TAKES ONLY ITSELF (GATE-STAYS-UP; #2205). Every reply
and event for an agent is sent from inside Chromium's ONE pipe reader
(`PipeTransport.run`), which also carries the answers to the gate's own
calls -- the device, the theme, the page follower, a new agent's
`Target.attachToBrowserTarget`. MEASURED 2026-10-24
(task_23c48f150cbf4220a301): after one refused `set_viewport_size` the step's
`swarm-handoff device` and `theme` failed with `gate_unavailable` until the step
ended, and a fresh CDP attach was refused. A send to an agent that had gone
(`ConnectionResetError` out of `drain`) went up through the reader and ended
it, so Chromium was never read again and every gate call waited forever. A
send to an agent that has stopped reading blocks the reader the same way.
So every message to an agent goes through `_deliver`: a send that fails, or
that has not left within `AGENT_SEND_SECONDS`, drops THAT connection
(`cdp_agent_dropped`, its sessions detached, its link closed on a task of its
own) and the reader goes on. And `PipeTransport.run` survives its sink:
a handler that raises on one message is logged by its exception's class, and
the next message is read.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from .. import browser_tools
from . import cdppolicy
from .cdppolicy import PolicyPaths
from .qaidentity import ConsoleRequests, QaIdentity
from .qatarget import (
    BROWSER_GONE,
    BUSY,
    REFUSALS_PATH,
    SIGN_IN_FAILED,
    STATE_FAILED,
    QaTarget,
    QaTargetGuard,
    SignIn,
)
from .scripts import ADD_BINDING, ADD_SCRIPT, REMOVE_BINDING, REMOVE_SCRIPT, ScriptRegistry

log = logging.getLogger(__name__)

SendToChromium = Callable[[dict[str, Any]], Awaitable[None]]
Record = Callable[[str, Mapping[str, str]], None]

_OPEN = "open"
_PAUSED = "paused"
_RELEASING = "releasing"

# CDP's own "server error" code; Playwright surfaces the message verbatim.
_ERROR_CODE = -32000

#: A CDP method name (`Domain.method`). Anything else is not a method, and is
#: logged as `unknown` rather than as whatever string the agent sent.
_METHOD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}\.[A-Za-z][A-Za-z0-9]{0,63}")
#: The methods whose URL the action log shows, and the parameter holding it.
ACTION_URL_PARAMS: Mapping[str, str] = {"Page.navigate": "url", "Target.createTarget": "url"}
#: A logged path is cut here: the relay refuses a value over 2,048 characters,
#: and what a person reads in the timeline is the site and where on it.
MAX_ACTION_PATH = 256
#: How long a refused agent connection is asked to wait before it tries
#: again (`Retry-After`): long enough not to spin, short next to a hand-off.
RETRY_AFTER_SECONDS = 5
#: The capture whose clipped form lifts the gate's device (module docstring).
CAPTURE_SCREENSHOT = "Page.captureScreenshot"
#: How often a tool still in use is reported again (module docstring).
TOOL_REPORT_SECONDS = 15.0
#: How long one message to an agent may take to leave before that agent's
#: connection is dropped (AN AGENT THAT GOES AWAY TAKES ONLY ITSELF). WHY 5 s:
#: asyncio buffers every write and `drain` waits only above its 64 KiB high
#: water mark, so on loopback this is an agent that has read nothing for
#: seconds -- and while it waits, Chromium's reader, and so every gate call,
#: waits too. Under the ctl operations' 8 s (ctl.OP_SECONDS), so a stalled
#: agent costs a device switch a delay, not a refusal.
AGENT_SEND_SECONDS = 5.0
#: A client tag as the path carries it, and the rest of the path after it.
_CLIENT_PATH = re.compile(r"/" + browser_tools.CLIENT_SEGMENT + r"/([A-Za-z0-9_-]{1,32})(/.*)?")


def split_client(target: str) -> tuple[str, str | None]:
    """A request target's bare path (no tag, no query) and the tool its client
    tag names, or None when there is no tag or it names no tool."""
    path, _, query = target.partition("?")
    tag: str | None = None
    found = _CLIENT_PATH.fullmatch(path)
    if found is not None:
        tag, path = found.group(1), found.group(2) or "/"
    for pair in query.split("&") if query else ():
        name, _, value = pair.partition("=")
        if name == browser_tools.CLIENT_SEGMENT:
            tag = value
    return path, tag if tag in browser_tools.TOOLS else None

#: Why the proxy refused an agent method, as the timeline names it: the method
#: policy (cdppolicy.py), a session the agent's connection does not own, or a
#: request body from a request made while a person was in control (§5.4.3).
REFUSAL_CAUSES = ("method_policy", "not_its_session", "control_window_request")


def action_url(raw: Any) -> str | None:
    """`raw` as the action log may hold it: scheme, host, port and path of an
    http(s) URL. Never the query, the fragment or a user name or password --
    the places a token or a typed value rides in a URL. None for anything else."""
    if not isinstance(raw, str):
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    netloc = host if port is None else f"{host}:{port}"
    return f"{parts.scheme}://{netloc}{(parts.path or '/')[:MAX_ACTION_PATH]}"


def action_line(method: Any, params: Any, cause: str | None = None) -> dict[str, Any]:
    """One agent method as an action-log line: `{"kind": "cdp", "method"}`, a
    navigation's `url` (`action_url`), `Input.insertText`'s `length`, and,
    when the gate refused it, `outcome` `refused` with the refusal's `cause`
    (`REFUSAL_CAUSES`). No other parameter, ever."""
    name = method if isinstance(method, str) and _METHOD_NAME.fullmatch(method) else "unknown"
    line: dict[str, Any] = {"kind": "cdp", "method": name}
    given = params if isinstance(params, Mapping) else {}
    if name == "Input.insertText" and isinstance(given.get("text"), str):
        line["length"] = len(given["text"])
    key = ACTION_URL_PARAMS.get(name)
    url = action_url(given.get(key)) if key is not None else None
    if url is not None:
        line["url"] = url
    if cause is not None:
        line["outcome"] = "refused"
        line["cause"] = cause
    return line

# Clears every password-shaped input in the document and its same-origin
# frames. `autocomplete` catches a field a page toggled from `password` to
# `text` to show it.
_CLEAR_PASSWORDS = """(() => {
  const selector = 'input[type=password], input[autocomplete~="current-password"], '
    + 'input[autocomplete~="new-password"], input[autocomplete~="one-time-code"]';
  let cleared = 0;
  const walk = (doc) => {
    for (const el of doc.querySelectorAll(selector)) { el.value = ''; cleared++; }
    for (const frame of doc.querySelectorAll('iframe, frame')) {
      try { if (frame.contentDocument) walk(frame.contentDocument); } catch (e) {}
    }
  };
  walk(document);
  return cleared;
})()"""


class CdpError(Exception):
    """Chromium answered one of the gate's own commands with an error."""


class AgentLink(Protocol):
    async def send(self, message: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...


@dataclass
class AgentConnection:
    link: AgentLink
    browser_session: str
    sessions: dict[str, str] = field(default_factory=dict)  # child session -> targetId
    auto_attach: bool = False
    closed: bool = False
    #: The tool its client tag named (`split_client`), or None.
    client: str | None = None
    #: Held while the return's resync is sent, so a live event cannot land
    #: between its messages (`_replay`).
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


#: The events that carry a session's execution contexts.
_CONTEXT_EVENTS = (
    "Runtime.executionContextCreated",
    "Runtime.executionContextDestroyed",
    "Runtime.executionContextsCleared",
)
#: The events that carry a frame's state, keyed by their `frameId` (the
#: frame of `Page.frameNavigated` is its `frame.id`).
_FRAME_EVENTS = (
    "Page.frameAttached",
    "Page.frameNavigated",
    "Page.navigatedWithinDocument",
    "Page.frameDetached",
    "Page.lifecycleEvent",
    "Page.frameStartedLoading",
    "Page.frameStoppedLoading",
)


def _frame_of(method: str, params: Mapping[str, Any]) -> str | None:
    frame = (params.get("frame") or {}).get("id") if method == "Page.frameNavigated" else params.get("frameId")
    return frame if isinstance(frame, str) else None


@dataclass
class _Frame:
    """One frame's net change in the control window (module docstring)."""

    attached: dict[str, Any] | None = None
    navigated: dict[str, Any] | None = None
    within: dict[str, Any] | None = None
    lifecycle: list[dict[str, Any]] = field(default_factory=list)
    loading: tuple[str, dict[str, Any]] | None = None
    detached: dict[str, Any] | None = None


@dataclass
class _Journal:
    """What the events dropped for one agent session did to its page."""

    contexts_changed: bool = False
    frames: dict[str, _Frame] = field(default_factory=dict)
    targets: dict[str, dict[str, Any]] = field(default_factory=dict)

    def note(self, method: str, params: Mapping[str, Any]) -> None:
        if method in _CONTEXT_EVENTS:
            self.contexts_changed = True
            return
        if method == "Target.targetInfoChanged":
            target = (params.get("targetInfo") or {}).get("targetId")
            if isinstance(target, str):
                self.targets[target] = dict(params)
            return
        frame_id = _frame_of(method, params) if method in _FRAME_EVENTS else None
        if frame_id is None:
            return
        frame = self.frames.setdefault(frame_id, _Frame())
        if method == "Page.frameAttached":
            self.frames[frame_id] = _Frame(attached=dict(params))
        elif method == "Page.frameDetached":
            if frame.attached is not None:
                # Born and gone inside the window: the agent never knew it.
                del self.frames[frame_id]
            else:
                self.frames[frame_id] = _Frame(detached=dict(params))
        elif method == "Page.frameNavigated":
            frame.navigated, frame.within = dict(params), None
            frame.lifecycle = [e for e in frame.lifecycle if e.get("loaderId") == (params.get("frame") or {}).get("loaderId")]
        elif method == "Page.navigatedWithinDocument":
            frame.within = dict(params)
        elif method == "Page.lifecycleEvent":
            if params.get("name") == "init":
                frame.lifecycle = []
            frame.lifecycle.append(dict(params))
        else:
            frame.loading = (method, dict(params))

    def superseded(self, method: str, params: Mapping[str, Any]) -> None:
        """A live event the agent has seen after return: the journal's older
        state of the same frame or target must not be replayed over it."""
        if method == "Target.targetInfoChanged":
            self.targets.pop(str((params.get("targetInfo") or {}).get("targetId")), None)
        elif method in _FRAME_EVENTS:
            self.frames.pop(_frame_of(method, params) or "", None)

    def messages(self, session: str, live: Mapping[int, dict[str, Any]], to_browser: bool) -> list[dict[str, Any]]:
        """The resync, in the order Chromium itself sends a navigation's events."""
        events: list[tuple[str, dict[str, Any]]] = []
        if self.contexts_changed:
            events.append(("Runtime.executionContextsCleared", {}))
        for frame in self.frames.values():
            if frame.detached is not None:
                events.append(("Page.frameDetached", frame.detached))
                continue
            if frame.attached is not None:
                events.append(("Page.frameAttached", frame.attached))
            if frame.navigated is not None:
                events.append(("Page.frameNavigated", frame.navigated))
            if frame.within is not None:
                events.append(("Page.navigatedWithinDocument", frame.within))
            events.extend(("Page.lifecycleEvent", e) for e in frame.lifecycle)
            if frame.loading is not None:
                events.append(frame.loading)
        events.extend(("Target.targetInfoChanged", info) for info in self.targets.values())
        if self.contexts_changed:
            events.extend(("Runtime.executionContextCreated", {"context": c}) for c in live.values())
        out = []
        for method, params in events:
            message: dict[str, Any] = {"method": method, "params": params}
            if not to_browser:
                message["sessionId"] = session
            out.append(message)
        return out


@dataclass
class _Pending:
    owner: AgentConnection | None
    agent_id: Any
    method: str
    session: str | None
    agent_session: str | None
    params: dict[str, Any]
    future: asyncio.Future[dict[str, Any]] | None


@dataclass(frozen=True)
class ControlDecision:
    granted: bool
    cause: str | None = None


class CdpProxy:
    def __init__(
        self,
        send_to_chromium: SendToChromium,
        *,
        paths: PolicyPaths,
        record: Record,
        on_control_ended: Callable[[str], None] | None = None,
        security_wait_seconds: float = 5.0,
        on_action: Callable[[dict[str, Any]], None] | None = None,
        on_pointer: Callable[[float, float], None] | None = None,
        on_agents: Callable[[int], None] | None = None,
        on_tool: Callable[[str, float], None] | None = None,
        wall: Callable[[], float] = time.time,
        qa: QaIdentity | None = None,
        qa_target: QaTarget | None = None,
        qa_sleep: Callable[[float], Awaitable[None]] | None = None,
        agent_send_seconds: float = AGENT_SEND_SECONDS,
    ) -> None:
        self._send = send_to_chromium
        self._agent_send_seconds = agent_send_seconds
        #: Links of dropped agents, closing on tasks of their own (`_drop_agent`).
        self._closing: set[asyncio.Task[None]] = set()
        #: The task's QA identity, or None (module docstring).
        self._qa = qa
        self._console_requests = ConsoleRequests(qa) if qa is not None else None
        #: The attempt's QA target, its guard on this Chromium, and the held
        #: sign-in running now (module docstring, THE QA TARGET).
        self._qa_target = qa_target
        self._qa_sleep = qa_sleep
        self._qa_guard = (QaTargetGuard(self, qa_target, record=record, sleep=qa_sleep)
                          if qa_target is not None else None)
        self._qa_signing = False
        self._qa_tasks: set[asyncio.Task[None]] = set()
        self._paths = paths
        self._record = record
        self._on_control_ended = on_control_ended
        self._on_action = on_action
        self._on_pointer = on_pointer
        self._on_agents = on_agents
        self._on_tool = on_tool
        self._wall = wall
        #: The tool that sent the last command, and when (epoch seconds):
        #: None until a tagged connection has (module docstring).
        self.tool_last_used: tuple[str, float] | None = None
        self._tool_told_at = 0.0
        #: Wall time (epoch seconds) the first agent connection was accepted;
        #: None until one is. Never cleared: an agent that connected and left
        #: did use this browser.
        self.agent_connected_at: float | None = None
        self._security_wait = security_wait_seconds
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._owner: dict[str, AgentConnection] = {}
        self._connections: list[AgentConnection] = []
        self._scripts = ScriptRegistry()
        self._mode = _OPEN
        self._held: list[tuple[AgentConnection, dict[str, Any]]] = []
        self._held_responses: list[tuple[_Pending, dict[str, Any]]] = []
        self._control_requests: set[str] = set()
        #: Every agent session's live execution contexts, id -> description,
        #: and the journal of what control dropped (module docstring).
        self._contexts: dict[str, dict[int, dict[str, Any]]] = {}
        self._journals: dict[str, _Journal] = {}
        #: Quiet refusals (`cdppolicy.QUIET_REFUSALS`) so far, by method.
        self.quiet_refusals: dict[str, int] = {}
        self._gate_sessions: dict[str, str] = {}  # watched page session -> targetId
        self._gate_children: dict[str, str] = {}  # frame session under a watched page -> targetId
        self._gate_contexts: set[str] = set()
        self._security: dict[str, str] = {}
        self._security_seen: dict[str, asyncio.Event] = {}
        self._controlled: str | None = None
        #: Wall times (epoch seconds) the agent was held and a person took
        #: control, for the reason a refused connection is given (`refusal`).
        self._paused_at: float | None = None
        self._controlled_at: float | None = None
        #: The gate's control lease, said in the refusal when it is set.
        self.control_lease_seconds: float | None = None
        # Control was granted on about:blank and the page has not navigated
        # since: a state that is not `secure` is checked against the URL.
        self._granted_blank = False
        # The controlled page's top-frame URL as last granted or committed:
        # what a state that is not `secure` is judged against (`_secure_origin`).
        self._controlled_url = ""
        self._checks: set[asyncio.Task[None]] = set()
        #: Chromium's pipe closed (`chromium_closed`): nothing written is answered.
        self._gone = False
        #: Run with a page's targetId after an agent's clipped capture of it
        #: (module docstring); the gate's `DeviceController.reassert`.
        self._after_clipped_capture: Callable[[str], Awaitable[None]] | None = None
        self._reasserts: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ state

    @property
    def paused(self) -> bool:
        return self._mode != _OPEN

    @property
    def in_control(self) -> bool:
        return self._controlled is not None

    @property
    def agent_connections(self) -> int:
        """The agent connections open now."""
        return sum(1 for c in self._connections if not c.closed)

    def _agents_changed(self) -> None:
        if self._on_agents is not None:
            self._on_agents(self.agent_connections)

    def _tool_used(self, tool: str | None) -> None:
        """`tool` sent something now: dated, and told on a change or when due."""
        if tool is None:
            return
        now = self._wall()
        before = self.tool_last_used
        self.tool_last_used = (tool, now)
        if self._on_tool is None:
            return
        if before is None or before[0] != tool or now - self._tool_told_at >= TOOL_REPORT_SECONDS:
            self._tool_told_at = now
            self._on_tool(tool, now)

    def after_clipped_capture(self, hook: Callable[[str], Awaitable[None]]) -> None:
        """`hook(targetId)` runs after the reply to an agent's clipped
        `Page.captureScreenshot` is relayed (module docstring)."""
        self._after_clipped_capture = hook

    # ------------------------------------------------------- the gate's calls

    def _allocate(self, pending: _Pending) -> int:
        self._next_id += 1
        self._pending[self._next_id] = pending
        return self._next_id

    async def _write(self, command_id: int, method: str, params: Mapping[str, Any], session: str | None) -> None:
        message: dict[str, Any] = {"id": command_id, "method": method, "params": dict(params)}
        if session is not None:
            message["sessionId"] = session
        await self._send(message)

    async def call(self, method: str, params: Mapping[str, Any] | None = None, session_id: str | None = None) -> dict[str, Any]:
        """The gate's own command; the method policy does not apply to it."""
        if self._gone:
            raise CdpError("the browser closed")
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        pending = _Pending(None, None, method, session_id, None, dict(params or {}), future)
        command_id = self._allocate(pending)
        await self._write(command_id, method, pending.params, session_id)
        return await future

    async def _post(self, method: str, params: Mapping[str, Any], session_id: str | None) -> None:
        """A command whose reply nobody waits for (safe inside the reader)."""
        if self._gone:
            return
        pending = _Pending(None, None, method, session_id, None, dict(params), None)
        await self._write(self._allocate(pending), method, pending.params, session_id)

    async def _call_quietly(self, method: str, params: Mapping[str, Any], session_id: str | None) -> dict[str, Any] | None:
        try:
            return await self.call(method, params, session_id)
        except CdpError:
            return None

    def qa_refusals(self) -> dict[str, Any] | None:
        """The QA target's refusal record (`QaTarget.refusals`), or None when
        no target is held: the visual-QA capture reads it at `REFUSALS_PATH`."""
        return self._qa_target.refusals() if self._qa_target is not None else None

    # ------------------------------------------------------ agent connections

    def refusal(self) -> tuple[str, str]:
        """Why a new agent connection is refused now: a cause code and the
        sentence the agent reads. Never a person's name (module docstring)."""
        now = self._wall()
        if self._controlled is not None:
            cause, since = "person_in_control", self._controlled_at
            what = "a person is in control of this browser"
            after = ("the agent's browser comes back when they return control or their "
                     "control lapses")
            if self.control_lease_seconds:
                after += (f" (the gate takes it back by itself {int(self.control_lease_seconds)} s "
                          "after their console stops renewing it)")
        elif self._qa_signing:
            cause, since = "held_for_qa_sign_in", self._paused_at
            what = "the agent is held while the live gate signs in to the QA target"
            after = "the agent's browser comes back when the sign-in ends (at most about a minute)"
        else:
            cause, since = "held_for_handoff", self._paused_at
            what = "the agent is held while the browser is handed to a person"
            after = "the agent's browser comes back when the hand-off ends"
        if since is not None:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
            what += f" since {stamp} ({max(0, int(now - since))} s)"
        return cause, (f"refused by the live gate: {what}; {after}. "
                       f"Retry in {RETRY_AFTER_SECONDS} s.")

    def _refuse_agent(self) -> None:
        cause, _ = self.refusal()
        since = self._controlled_at if self._controlled is not None else self._paused_at
        held = int(self._wall() - since) if since is not None else None
        self._record("cdp_connection_refused", {"cause": cause, "held_seconds": held})

    async def connect_agent(self, link: AgentLink, client: str | None = None) -> AgentConnection | None:
        """A new agent connection, or None while the agent is held (`refusal` says why).

        `client` is the tool its tag named (`split_client`), or None."""
        if self._mode != _OPEN:
            self._refuse_agent()
            return None
        result = await self.call("Target.attachToBrowserTarget")
        session = result["sessionId"]
        if self._mode != _OPEN:
            await self._post("Target.detachFromTarget", {"sessionId": session}, None)
            self._refuse_agent()
            return None
        if self._qa is not None:
            link = _ScrubbedLink(link, self._qa)
        if self._qa_target is not None:
            link = _ScrubbedLink(link, self._qa_target)
        connection = AgentConnection(link=link, browser_session=session,
                                     client=client if client in browser_tools.TOOLS else None)
        self._owner[session] = connection
        self._connections.append(connection)
        if self.agent_connected_at is None:
            self.agent_connected_at = self._wall()
        self._agents_changed()
        self._tool_used(connection.client)
        return connection

    async def disconnect_agent(self, connection: AgentConnection) -> None:
        if connection.closed:
            return
        connection.closed = True
        for child in list(connection.sessions):
            self._forget_session(child)
        self._owner.pop(connection.browser_session, None)
        if connection in self._connections:
            self._connections.remove(connection)
        self._agents_changed()
        await self._post("Target.detachFromTarget", {"sessionId": connection.browser_session}, None)

    async def chromium_closed(self) -> None:
        """Chromium's pipe closed: nothing it was asked will be answered (BROWSER-RELAUNCH).

        Every gate call still waiting fails now rather than never, and every
        agent connection is closed: its sessions were in the browser that
        ended, so the agent reconnects to the browser started next. A gate
        call made after this fails at once.
        """
        self._gone = True
        for task in list(self._qa_tasks):
            task.cancel()
        self._contexts.clear()
        self._journals.clear()
        pending, self._pending = self._pending, {}
        for entry in pending.values():
            if entry.future is not None and not entry.future.done():
                entry.future.set_exception(CdpError("the browser closed"))
        connections, self._connections = self._connections, []
        self._owner.clear()
        if connections:
            self._agents_changed()
        for connection in connections:
            connection.closed = True
            try:
                await connection.link.close()
            except Exception:  # noqa: BLE001 -- a link already gone is closed
                pass

    def _forget_session(self, session: str) -> None:
        owner = self._owner.pop(session, None)
        if owner is not None:
            owner.sessions.pop(session, None)
        self._scripts.forget_session(session)
        self._contexts.pop(session, None)
        self._journals.pop(session, None)

    def _note(self, method: Any, params: Any, refused: str | None = None) -> None:
        """One action-log line (`action_line`); never awaited."""
        if self._on_action is not None:
            self._on_action(action_line(method, params, refused))

    def _pointer(self, params: Mapping[str, Any]) -> None:
        """The agent's mouse position, to the gate (module docstring); a non-number is ignored."""
        x, y = params.get("x"), params.get("y")
        if self._on_pointer is None or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in (x, y)):
            return
        self._on_pointer(float(x), float(y))

    async def _reply_error(self, connection: AgentConnection, message: Mapping[str, Any], text: str) -> None:
        reply: dict[str, Any] = {"id": message.get("id"), "error": {"code": _ERROR_CODE, "message": text}}
        if "sessionId" in message:
            reply["sessionId"] = message["sessionId"]
        await self._deliver(connection, reply)

    async def _deliver(self, connection: AgentConnection, message: dict[str, Any]) -> bool:
        """`message` to one agent. A send that fails or stalls drops that
        connection and returns False; it never raises into the caller, which
        is Chromium's reader (module docstring, AN AGENT THAT GOES AWAY)."""
        if connection.closed:
            return False
        try:
            await asyncio.wait_for(connection.link.send(message), self._agent_send_seconds)
        except asyncio.TimeoutError:
            await self._drop_agent(connection, "send_stalled")
            return False
        except Exception as exc:  # noqa: BLE001 -- a link that went is that agent's end, not the reader's
            await self._drop_agent(connection, "send_failed", exc)
            return False
        return True

    async def _drop_agent(self, connection: AgentConnection, cause: str, exc: BaseException | None = None) -> None:
        if connection.closed:
            return
        self._record("cdp_agent_dropped", {"cause": cause})
        log.warning("live gate: an agent connection dropped (%s%s); the browser goes on", cause,
                    f": {type(exc).__name__}" if exc is not None else "")
        await self.disconnect_agent(connection)
        task = asyncio.get_running_loop().create_task(self._close_link(connection.link))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def _close_link(self, link: AgentLink) -> None:
        try:
            await asyncio.wait_for(link.close(), self._agent_send_seconds)
        except Exception:  # noqa: BLE001 -- already gone or stalled: nothing more to close
            pass

    async def on_agent_message(self, connection: AgentConnection, message: Mapping[str, Any]) -> None:
        if connection.closed:
            return
        self._tool_used(connection.client)
        method = message.get("method")
        if "id" not in message:
            return
        if not isinstance(method, str):
            await self._reply_error(connection, message, "a command needs a method")
            return
        session = message.get("sessionId")
        if session is not None and self._owner.get(session) is not connection:
            self._record("cdp_session_refused", {"method": method})
            self._note(method, message.get("params"), "not_its_session")
            await self._reply_error(connection, message, "No session with given id")
            return
        params = message.get("params")
        if isinstance(params, Mapping):
            confined = cdppolicy.confine(method, params, self._paths)
            if confined is not params:
                params = confined
                message = {**message, "params": dict(confined)}
        refusal = cdppolicy.check(method, params if isinstance(params, Mapping) else {}, self._paths)
        if refusal is not None:
            if method in cdppolicy.QUIET_REFUSALS:
                count = self.quiet_refusals[method] = self.quiet_refusals.get(method, 0) + 1
                log.debug("live gate: %s refused quietly (%d so far)", method, count)
            else:
                self._record("cdp_method_refused", {"method": method})
            self._note(method, params, "method_policy")
            await self._reply_error(connection, message, refusal.message())
            return
        if self._qa is not None and self._qa.armed and self._console_requests is not None:
            refusal = cdppolicy.qa_identity_check(
                method, params if isinstance(params, Mapping) else {}, self._console_requests)
            if refusal is not None:
                self._record("cdp_method_refused", {"method": method, "cause": refusal.cause or "qa_identity"})
                self._note(method, params, "method_policy")
                await self._reply_error(connection, message, refusal.message())
                return
        if self._qa_target is not None:
            why = self._qa_target.agent_refusal(method, params if isinstance(params, Mapping) else {})
            if why is not None:
                self._record("cdp_method_refused", {"method": method, "cause": "qa_target"})
                self._note(method, params, "method_policy")
                await self._reply_error(connection, message, why)
                return
        if self._mode != _OPEN:
            self._held.append((connection, dict(message)))
            return
        await self._forward(connection, message)

    async def _forward(self, connection: AgentConnection, message: Mapping[str, Any]) -> None:
        method = message["method"]
        agent_session = message.get("sessionId")
        if agent_session is not None and self._owner.get(agent_session) is not connection:
            # The session detached while the command was held.
            await self._reply_error(connection, message, "No session with given id")
            return
        session = agent_session if agent_session is not None else connection.browser_session
        raw = message.get("params")
        params: dict[str, Any] = dict(raw) if isinstance(raw, Mapping) else {}
        if method in cdppolicy.LOCAL_REPLIES:
            # Answered here and never forwarded (cdppolicy.py, DEVB-GATE).
            self._note(method, params)
            reply: dict[str, Any] = {"id": message["id"], "result": dict(cdppolicy.LOCAL_REPLIES[method])}
            if agent_session is not None:
                reply["sessionId"] = agent_session
            await self._deliver(connection, reply)
            return
        if method in cdppolicy.REQUEST_BODY_METHODS and params.get("requestId") in self._control_requests:
            self._record("cdp_method_refused", {"method": method})
            self._note(method, params, "control_window_request")
            await self._reply_error(
                connection, message, f"refused by the live gate: {method} for a request made while a person was in control"
            )
            return
        if method == REMOVE_SCRIPT and isinstance(params.get("identifier"), str):
            params["identifier"] = self._scripts.removed_script(session, params["identifier"])
        elif method == REMOVE_BINDING and isinstance(params.get("name"), str):
            self._scripts.removed_binding(session, params["name"])
        elif method == "Target.setAutoAttach" and session == connection.browser_session:
            connection.auto_attach = bool(params.get("autoAttach"))
        self._note(method, params)
        if method == "Input.dispatchMouseEvent":
            self._pointer(params)
        pending = _Pending(connection, message["id"], method, session, agent_session, params, None)
        await self._write(self._allocate(pending), method, params, session)

    # --------------------------------------------------------- from Chromium

    async def on_chromium_message(self, message: Mapping[str, Any]) -> None:
        if self._qa_guard is not None and "method" in message:
            # Never waits: the guard answers each pause on a task of its own.
            self._qa_guard.on_event(message)
        if "id" in message:
            pending = self._pending.pop(message["id"], None)
            if pending is not None:
                await self._on_response(pending, message)
            return
        method = message.get("method")
        if not isinstance(method, str):
            return
        raw = message.get("params")
        params: dict[str, Any] = raw if isinstance(raw, dict) else {}
        session = message.get("sessionId")
        if session is None or session in self._gate_sessions or session in self._gate_children:
            self._on_gate_event(method, params, session)
            return
        owner = self._owner.get(session)
        if owner is None or owner.closed:
            return
        await self._on_agent_event(owner, session, method, params)

    async def _on_response(self, pending: _Pending, message: Mapping[str, Any]) -> None:
        if pending.owner is None:
            if pending.future is not None and not pending.future.done():
                if "error" in message:
                    pending.future.set_exception(CdpError(f"{pending.method}: {message['error'].get('message', '')}"))
                else:
                    pending.future.set_result(message.get("result") or {})
            return
        connection = pending.owner
        if connection.closed:
            return
        result = message.get("result")
        if "error" not in message and isinstance(result, dict):
            await self._track(pending, result)
            if self._undecodable_console_body(pending, result):
                self._record("cdp_method_refused", {"method": pending.method, "cause": "qa_identity_body"})
                message = {"error": {"code": _ERROR_CODE, "message": (
                    f"refused by the live gate: {pending.method} for a console request whose body "
                    "the gate cannot read for the QA identity")}}
            elif (self._qa_target is not None and pending.method in _BODY_METHODS
                  and self._qa_target.undecodable_body(result)):
                self._record("cdp_method_refused", {"method": pending.method, "cause": "qa_target_body"})
                message = {"error": {"code": _ERROR_CODE, "message": (
                    f"refused by the live gate: {pending.method} for a body the gate cannot read "
                    "while it holds the QA target's sign-in")}}
        reply: dict[str, Any] = {"id": pending.agent_id}
        if "error" in message:
            reply["error"] = message["error"]
        else:
            reply["result"] = result if isinstance(result, dict) else {}
        if pending.agent_session is not None:
            reply["sessionId"] = pending.agent_session
        if self._mode == _PAUSED:
            self._held_responses.append((pending, reply))
        elif not await self._deliver(connection, reply):
            return
        self._clipped_capture_answered(pending, connection)

    def _clipped_capture_answered(self, pending: _Pending, connection: AgentConnection) -> None:
        """The device again on the page an agent's clipped capture lifted it
        from, on a task: the hook waits on replies, and the reader must not."""
        hook = self._after_clipped_capture
        if hook is None or pending.method != CAPTURE_SCREENSHOT or not isinstance(pending.params.get("clip"), Mapping):
            return
        target = connection.sessions.get(pending.agent_session) if pending.agent_session is not None else None
        if not target:
            return
        task = asyncio.get_running_loop().create_task(self._reassert(hook, target))
        self._reasserts.add(task)
        task.add_done_callback(self._reasserts.discard)

    @staticmethod
    async def _reassert(hook: Callable[[str], Awaitable[None]], target: str) -> None:
        try:
            await hook(target)
        except Exception as exc:  # noqa: BLE001 -- a page that closed: nothing to put back
            log.info("live gate: device not put back after a clipped capture (%s)", type(exc).__name__)

    def _undecodable_console_body(self, pending: _Pending, result: Mapping[str, Any]) -> bool:
        """`qa_identity_body` (module docstring): a body the scrub could not read, on the console host."""
        if self._qa is None or not self._qa.armed or self._console_requests is None:
            return False
        if pending.method not in _BODY_METHODS:
            return False
        return self._qa.undecodable_body(result) and self._console_requests(pending.params.get("requestId"))

    async def _track(self, pending: _Pending, result: Mapping[str, Any]) -> None:
        session = pending.session
        assert session is not None and pending.owner is not None
        if pending.method == ADD_SCRIPT and isinstance(result.get("identifier"), str):
            self._scripts.added_script(session, result["identifier"], pending.params)
            if self._mode == _PAUSED:
                # Added just before control and answered during it: lift it now.
                await self._post(REMOVE_SCRIPT, {"identifier": result["identifier"]}, session)
        elif pending.method == ADD_BINDING:
            self._scripts.added_binding(session, pending.params)
            if self._mode == _PAUSED and isinstance(pending.params.get("name"), str):
                await self._post(REMOVE_BINDING, {"name": pending.params["name"]}, session)
        elif pending.method == "Target.attachToTarget" and isinstance(result.get("sessionId"), str):
            child = result["sessionId"]
            self._owner[child] = pending.owner
            pending.owner.sessions.setdefault(child, str(pending.params.get("targetId", "")))

    def _track_contexts(self, session: str, method: str, params: Mapping[str, Any]) -> None:
        contexts = self._contexts.setdefault(session, {})
        if method == "Runtime.executionContextsCleared":
            contexts.clear()
        elif method == "Runtime.executionContextDestroyed":
            contexts.pop(params.get("executionContextId"), None)
        else:
            context = params.get("context")
            if isinstance(context, dict) and isinstance(context.get("id"), int):
                contexts[context["id"]] = dict(context)

    async def _on_agent_event(self, owner: AgentConnection, session: str, method: str, params: dict[str, Any]) -> None:
        if method.startswith(cdppolicy.DROPPED_EVENT_DOMAINS):
            # Never the agent's, enabled or not (cdppolicy.py, DEVB-GATE).
            return
        paused = self._mode == _PAUSED
        if self._console_requests is not None and self._qa is not None and self._qa.armed:
            if method == "Fetch.requestPaused":
                url = (params.get("request") or {}).get("url")
                self._console_requests.note(params.get("requestId"), url)
                self._console_requests.note(params.get("networkId"), url)
            elif method == "Network.requestWillBeSent":
                self._console_requests.note(params.get("requestId"), (params.get("request") or {}).get("url"))
        if method in _CONTEXT_EVENTS:
            self._track_contexts(session, method, params)
        if method == "Target.attachedToTarget":
            child = params.get("sessionId")
            info = params.get("targetInfo") or {}
            if isinstance(child, str):
                self._owner[child] = owner
                owner.sessions[child] = str(info.get("targetId", ""))
                if paused:
                    # Forgotten now, not on Chromium's detachedFromTarget: the
                    # return hand-over attaches the agent to every page it
                    # does not hold, and this one it no longer does.
                    await self._post("Target.detachFromTarget", {"sessionId": child}, session)
                    self._forget_session(child)
                    return
        elif method == "Target.detachedFromTarget":
            child = params.get("sessionId")
            if isinstance(child, str):
                self._forget_session(child)
        if paused:
            if method == "Network.requestWillBeSent" and isinstance(params.get("requestId"), str):
                self._control_requests.add(params["requestId"])
            elif method == "Fetch.requestPaused":
                if isinstance(params.get("networkId"), str):
                    self._control_requests.add(params["networkId"])
                await self._post("Fetch.continueRequest", {"requestId": params.get("requestId")}, session)
            elif method == "Fetch.authRequired":
                await self._post(
                    "Fetch.continueWithAuth",
                    {"requestId": params.get("requestId"), "authChallengeResponse": {"response": "Default"}},
                    session,
                )
            else:
                self._journals.setdefault(session, _Journal()).note(method, params)
            return
        journal = self._journals.get(session)
        if journal is not None:
            journal.superseded(method, params)
        params = self._strip(method, params)
        event: dict[str, Any] = {"method": method, "params": params}
        if session != owner.browser_session:
            event["sessionId"] = session
        async with owner.send_lock:
            await self._deliver(owner, event)

    def _strip(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """A request from the control window, seen after return, without its body."""
        if not self._control_requests:
            return params
        if method.startswith("Network."):
            request_id = params.get("requestId")
        elif method == "Fetch.requestPaused":
            request_id = params.get("networkId")
        else:
            return params
        request = params.get("request")
        if request_id not in self._control_requests or not isinstance(request, dict):
            return params
        stripped = {k: v for k, v in request.items() if k not in ("postData", "postDataEntries")}
        return {**params, "request": stripped}

    def _on_gate_event(self, method: str, params: Mapping[str, Any], session: str | None) -> None:
        if method == "Security.visibleSecurityStateChanged" and session in self._gate_sessions:
            target = self._gate_sessions[session]
            state = (params.get("visibleSecurityState") or {}).get("securityState")
            self._security[target] = state if isinstance(state, str) else "unknown"
            self._security_seen.setdefault(target, asyncio.Event()).set()
            if self._controlled == target and self._security[target] != "secure":
                if self._granted_blank or _secure_origin(self._controlled_url, self._security[target]):
                    # about:blank, or loopback, which is never rated `secure`:
                    # the page may have navigated on, so read its URL again.
                    self._check_page(target, blank=self._granted_blank)
                else:
                    self._end_control("insecure_connection")
        elif method == "Page.frameNavigated" and session in self._gate_sessions:
            frame = params.get("frame") or {}
            target = self._gate_sessions[session]
            if self._controlled != target or not isinstance(frame, dict) or frame.get("parentId"):
                return
            url = str(frame.get("url", ""))
            self._controlled_url = url
            if _is_blank(url):
                return
            self._granted_blank = False
            if not _secure_origin(url, None):
                self._end_control("insecure_connection")
            elif self._on_qa_host(url):
                self._end_control("qa_identity_host")
        elif method == "Target.attachedToTarget" and session in self._gate_sessions:
            child = params.get("sessionId")
            if isinstance(child, str):
                self._gate_children[child] = str((params.get("targetInfo") or {}).get("targetId", ""))
        elif method == "Target.detachedFromTarget":
            child = params.get("sessionId")
            if child in self._gate_sessions:
                target = self._gate_sessions.pop(child)
                if self._controlled == target:
                    self._end_control("page_closed")
            self._gate_children.pop(child, None)

    # ------------------------------------------------------------ control

    async def pause(self) -> None:
        """Hold the agent: from here nothing it sends reaches Chromium."""
        if self._mode != _OPEN:
            return
        self._mode = _PAUSED
        self._paused_at = self._wall()
        for session, method, params in self._scripts.removal_commands():
            await self._call_quietly(method, params, session)

    async def watch_page(self, target_id: str) -> str:
        """Attach the gate's own session to a page and watch its security state."""
        for session, target in self._gate_sessions.items():
            if target == target_id:
                return session
        result = await self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session = result["sessionId"]
        self._gate_sessions[session] = target_id
        await self._call_quietly(
            "Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}, session
        )
        await self.call("Security.enable", {}, session)
        # The top frame's navigations: control granted on about:blank is held
        # to the certificate rule from the first one (LB-R4).
        await self._call_quietly("Page.enable", {}, session)
        return session

    async def open_fresh_page(self, url: str) -> str:
        """Open `url` in a context the gate owns; returns the page's targetId."""
        if self._mode != _PAUSED:
            raise ValueError("the fresh page opens only while the agent is held")
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("the fresh page opens only an https URL")
        context = (await self.call("Target.createBrowserContext", {}))["browserContextId"]
        self._gate_contexts.add(context)
        await self.call(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allowAndName",
                "browserContextId": context,
                "downloadPath": str(self._paths.downloads_dir),
                "eventsEnabled": True,
            },
        )
        target = (await self.call("Target.createTarget", {"url": url, "browserContextId": context}))["targetId"]
        await self.watch_page(target)
        return target

    # ------------------------------------------------------- the QA target

    async def start_qa_target(self) -> None:
        """The target was handed over (`qatarget.handle_ctl`): its guard runs on
        this Chromium from now, and a queued sign-in starts, held (module docstring)."""
        target = self._qa_target
        if target is None or self._gone or not target.armed:
            return
        if self._qa_guard is not None:
            await self._qa_guard.start()
        if target.wants_sign_in:
            target.signing_in = True
            task = asyncio.get_running_loop().create_task(self._run_qa_sign_in())
            self._qa_tasks.add(task)
            task.add_done_callback(self._qa_tasks.discard)

    async def _run_qa_sign_in(self) -> None:
        target = self._qa_target
        assert target is not None
        try:
            state, cause = await self.qa_sign_in()
        except asyncio.CancelledError:
            target.finished(STATE_FAILED, BROWSER_GONE)
            raise
        except Exception as exc:  # noqa: BLE001 -- the sign-in fails; the gate does not
            log.warning("live gate: the QA sign-in failed (%s)", type(exc).__name__)
            state, cause = STATE_FAILED, SIGN_IN_FAILED
        target.finished(state, cause)

    async def qa_sign_in(self) -> tuple[str, str | None]:
        """THE HELD SIGN-IN (module docstring): (state, cause). The agent is
        released, in §2.2 step 4's order, whatever happened."""
        target = self._qa_target
        if target is None:
            return STATE_FAILED, SIGN_IN_FAILED
        if self._mode != _OPEN or self._controlled is not None:
            target.signing_in = False
            return STATE_FAILED, BUSY
        target.signing_in = True
        self._qa_signing = True
        await self.pause()
        sign_in = SignIn(self, target, clock=self._wall, sleep=self._qa_sleep)
        outcome: tuple[str, str | None] = (STATE_FAILED, SIGN_IN_FAILED)
        try:
            outcome = await sign_in.run()
        finally:
            # 1. The gate's tab: its password inputs cleared, then closed.
            await sign_in.close()
            # From here the login's other origin is no top-frame destination.
            target.signing_in = False
            # 2. The agent's tab goes to the target's base URL.
            if target.base_url and not self._gone:
                await self._qa_land(target.base_url)
            self._qa_signing = False
            # 3. `resume`: every password input on the gate's sessions cleared,
            #    the held responses delivered, then the held commands released.
            await self.resume()
        return outcome

    async def _qa_land(self, url: str) -> None:
        """Send the agent's tab (the first page that is not the gate's) to `url`. Never raises."""
        listed = await self._call_quietly("Target.getTargets", {}, None) or {}
        page = next((t for t in listed.get("targetInfos") or []
                     if isinstance(t, Mapping) and t.get("type") == "page" and isinstance(t.get("targetId"), str)),
                    None)
        if page is None:
            return
        try:
            session = await self.watch_page(page["targetId"])
        except Exception as exc:  # noqa: BLE001 -- the agent finds its tab where it was
            log.debug("live gate: the agent's tab was not reached (%s)", type(exc).__name__)
            return
        await self._call_quietly("Page.navigate", {"url": url}, session)

    def _refuse(self, cause: str) -> ControlDecision:
        self._record("control_refused", {"cause": cause})
        return ControlDecision(False, cause)

    async def take_control(self, target_id: str) -> ControlDecision:
        """A person takes control of `target_id`, if its connection is secure."""
        if self._mode != _PAUSED:
            return self._refuse("not_paused")
        if self._qa_signing:
            return self._refuse("qa_sign_in_in_progress")
        await self.watch_page(target_id)
        if _is_blank(await self._url(target_id)):
            # No origin yet: nothing to call insecure (LB-R4). The first
            # navigation is judged as it commits (`_on_gate_event`).
            if self._mode != _PAUSED:
                return self._refuse("not_paused")
            self._controlled = target_id
            self._controlled_at = self._wall()
            self._granted_blank = True
            self._controlled_url = "about:blank"
            return ControlDecision(True)
        if target_id not in self._security:
            seen = self._security_seen.setdefault(target_id, asyncio.Event())
            try:
                await asyncio.wait_for(seen.wait(), self._security_wait)
            except asyncio.TimeoutError:
                return self._refuse("security_state_unknown")
        url = await self._url(target_id)
        if not _secure_origin(url, self._security.get(target_id, "unknown")):
            return self._refuse("insecure_connection")
        if self._on_qa_host(url):
            return self._refuse("qa_identity_host")
        if self._mode != _PAUSED:
            return self._refuse("not_paused")
        self._controlled = target_id
        self._controlled_at = self._wall()
        self._granted_blank = False
        self._controlled_url = url
        return ControlDecision(True)

    def _on_qa_host(self, url: str) -> bool:
        """The page is on the console host of the task's QA identity (Q7)."""
        if self._qa is None or self._qa.host is None:
            return False
        try:
            return urlsplit(url).hostname == self._qa.host
        except ValueError:
            return False

    async def _url(self, target_id: str) -> str:
        info = (await self.call("Target.getTargetInfo", {"targetId": target_id})).get("targetInfo") or {}
        return str(info.get("url", ""))

    def _check_page(self, target_id: str, *, blank: bool) -> None:
        """A state that is not `secure` on a page granted blank, or held at
        loopback: read its URL again, off the pipe reader (this runs inside
        `on_chromium_message`)."""
        task = asyncio.get_running_loop().create_task(self._recheck_page(target_id, blank))
        self._checks.add(task)
        task.add_done_callback(self._checks.discard)

    async def _recheck_page(self, target_id: str, blank: bool) -> None:
        try:
            url = await self._url(target_id)
        except Exception:  # noqa: BLE001 -- unreadable is not blank: fail closed
            url = ""
        if self._controlled != target_id or (blank and not self._granted_blank):
            return
        if blank and _is_blank(url):
            return
        if _secure_origin(url, self._security.get(target_id, "unknown")):
            # Still at loopback, or navigated on and the secure state arrived
            # before this read.
            self._granted_blank = False
            self._controlled_url = url
            return
        self._end_control("insecure_connection")

    def end_control(self, cause: str) -> None:
        """Control ends without a return: idle, renew lapsed, preempted, ..."""
        if self._controlled is not None:
            self._end_control(cause)

    def _end_control(self, cause: str) -> None:
        self._controlled = None
        self._controlled_at = None
        self._granted_blank = False
        self._record("control_ended", {"cause": cause})
        if self._on_control_ended is not None:
            self._on_control_ended(cause)

    async def resume(self) -> None:
        """Return: clear, restore, hand over, then release the agent in order."""
        if self._mode != _PAUSED:
            return
        self._controlled = None
        self._granted_blank = False
        self._mode = _RELEASING
        try:
            for session in [*self._gate_sessions, *self._gate_children]:
                await self._call_quietly("Runtime.evaluate", {"expression": _CLEAR_PASSWORDS, "returnByValue": True}, session)
            await self._restore_scripts()
            await self._hand_over()
            await self._resync()
            held_responses, self._held_responses = self._held_responses, []
            for pending, reply in held_responses:
                assert pending.owner is not None
                if pending.owner.closed:
                    continue
                if pending.method in cdppolicy.CONTENT_METHODS and "result" in reply:
                    reply = {k: v for k, v in reply.items() if k != "result"}
                    reply["error"] = {
                        "code": _ERROR_CODE,
                        "message": f"{pending.method} completed while a person was in control; its result is withheld",
                    }
                await self._deliver(pending.owner, reply)
            while self._held:
                connection, message = self._held.pop(0)
                if not connection.closed:
                    await self._forward(connection, message)
        finally:
            self._mode = _OPEN
            self._paused_at = None
            self._controlled_at = None

    async def _resync(self) -> None:
        """Tell each agent session the net state control dropped, then give
        back the utility worlds a new document lacks (module docstring)."""
        journals, self._journals = self._journals, {}
        worlds: dict[str, set[str]] = {}
        for session, method, params, _ in self._scripts.restore_commands():
            if method == ADD_SCRIPT and isinstance(params.get("worldName"), str) and params["worldName"]:
                worlds.setdefault(session, set()).add(params["worldName"])
        for session, journal in journals.items():
            owner = self._owner.get(session)
            if owner is None or owner.closed:
                continue
            live = self._contexts.get(session, {})
            messages = journal.messages(session, live, session == owner.browser_session)
            async with owner.send_lock:
                for message in messages:
                    if not await self._deliver(owner, message):
                        break
            if not journal.contexts_changed:
                continue
            for frame, name in self._missing_worlds(live, worlds.get(session, set())):
                await self._call_quietly(
                    "Page.createIsolatedWorld", {"frameId": frame, "worldName": name, "grantUniveralAccess": True}, session
                )

    @staticmethod
    def _missing_worlds(live: Mapping[int, Mapping[str, Any]], names: set[str]) -> list[tuple[str, str]]:
        """(frame, world) for each frame with a main world and no `names` world."""
        frames: list[str] = []
        have: set[tuple[str, str]] = set()
        for context in live.values():
            aux = context.get("auxData") or {}
            frame = aux.get("frameId")
            if not isinstance(frame, str):
                continue
            if aux.get("isDefault") and frame not in frames:
                frames.append(frame)
            have.add((frame, str(context.get("name", ""))))
        return [(f, n) for f in frames for n in sorted(names) if (f, n) not in have]

    async def _restore_scripts(self) -> None:
        for session, method, params, agent_identifier in self._scripts.restore_commands():
            result = await self._call_quietly(method, params, session)
            if method == ADD_SCRIPT and agent_identifier is not None:
                if result is not None and isinstance(result.get("identifier"), str):
                    self._scripts.restored_script(session, agent_identifier, result["identifier"])
                else:
                    self._scripts.lost_script(session, agent_identifier)
            elif result is None and isinstance(params.get("name"), str):
                self._scripts.removed_binding(session, params["name"])

    async def _hand_over(self) -> None:
        for session in list(self._gate_sessions):
            await self._call_quietly("Target.detachFromTarget", {"sessionId": session}, None)
        self._gate_sessions.clear()
        self._gate_children.clear()
        self._security.clear()
        self._security_seen.clear()
        self._gate_contexts.clear()
        attaching = [c for c in self._connections if c.auto_attach and not c.closed]
        if not attaching:
            return
        targets = (await self._call_quietly("Target.getTargets", {}, None) or {}).get("targetInfos") or []
        for connection in attaching:
            known = set(connection.sessions.values())
            for info in targets:
                target = info.get("targetId")
                if info.get("type") == "page" and isinstance(target, str) and target not in known:
                    await self._call_quietly(
                        "Target.attachToTarget", {"targetId": target, "flatten": True}, connection.browser_session
                    )


#: The methods whose result is a response body (`qa_identity_body`).
_BODY_METHODS = frozenset({"Network.getResponseBody", "Fetch.getResponseBody"})


class _Scrubber(Protocol):
    def scrub(self, message: dict[str, Any]) -> dict[str, Any]: ...


class _ScrubbedLink:
    """An agent's link, every message through `QaIdentity.scrub` or `QaTarget.scrub` (module docstring)."""

    def __init__(self, link: AgentLink, qa: _Scrubber) -> None:
        self._link = link
        self._qa = qa

    async def send(self, message: dict[str, Any]) -> None:
        await self._link.send(self._qa.scrub(message))

    async def close(self) -> None:
        await self._link.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._link, name)


def _is_blank(url: str) -> bool:
    """`about:blank`: a page before its first navigation, with no origin."""
    parts = urlsplit(url)
    return parts.scheme == "about" and parts.path == "blank"


def _https_host(url: str) -> bool:
    """https, at a host name rather than an IP literal (section 5.4.4)."""
    parts = urlsplit(url)
    return parts.scheme == "https" and bool(parts.hostname) and not _is_address(parts.hostname or "")


#: Exactly the loopback hosts BGD decided on 2026-10-22, as `urlsplit` gives
#: `.hostname` (lower-cased, brackets stripped). Not 127/8, not `*.localhost`.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _secure_origin(url: str, state: str | None) -> bool:
    """The https rule, the one every `insecure_connection` site applies.

    Secure is either a loopback origin (`_loopback_origin`), whatever
    Chromium's `state` -- it rates loopback `neutral`, never `secure` -- or
    https at a host name whose `state` is `secure`. `state` None judges the URL
    alone: at a commit, before Chromium rates the new document, whose own
    state event follows and is judged in turn.
    """
    if _loopback_origin(url):
        return True
    return _https_host(url) and state in (None, "secure")


def _loopback_origin(url: str) -> bool:
    """Exactly http at 127.0.0.1, ::1 or localhost, any port, no userinfo."""
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 -- a malformed port raises: fail closed
    except ValueError:
        return False
    return parts.scheme == "http" and "@" not in parts.netloc and parts.hostname in _LOOPBACK_HOSTS


def _is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Chromium's side: --remote-debugging-pipe                                    #
# --------------------------------------------------------------------------- #

# A screenshot or a PDF arrives as one message; 256 MiB is far above either.
_MAX_MESSAGE = 256 * 1024 * 1024


class PipeTransport:
    """Chromium's debugging pipe: JSON messages, each ended by a NUL byte.

    Chromium reads commands from its fd 3 and writes replies to its fd 4; the
    gate passes the other ends here. `run` delivers each message in order and
    returns when Chromium closes its end.
    """

    def __init__(self, *, read_fd: int, write_fd: int) -> None:
        self._read_fd = read_fd
        self._write_fd = write_fd
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        self._reader = asyncio.StreamReader(limit=_MAX_MESSAGE)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(self._reader), os.fdopen(self._read_fd, "rb", buffering=0)
        )
        transport, protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, os.fdopen(self._write_fd, "wb", buffering=0)
        )
        self._writer = asyncio.StreamWriter(transport, protocol, None, loop)

    async def send(self, message: dict[str, Any]) -> None:
        assert self._writer is not None, "open() first"
        self._writer.write(json.dumps(message, separators=(",", ":")).encode() + b"\0")
        await self._writer.drain()

    async def run(self, sink: Any) -> None:
        assert self._reader is not None, "open() first"
        while True:
            try:
                data = await self._reader.readuntil(b"\0")
            except asyncio.IncompleteReadError:
                return
            try:
                message = json.loads(data[:-1])
            except ValueError:
                continue
            if isinstance(message, dict):
                try:
                    await sink.on_chromium_message(message)
                except Exception as exc:  # noqa: BLE001 -- one message's handler never stops the reader
                    log.warning("live gate: a message from Chromium was not handled (%s); reading on",
                                type(exc).__name__)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()


# --------------------------------------------------------------------------- #
# The agent's side: 127.0.0.1:9222, /json/version, the QA refusal record    #
# (`REFUSALS_PATH`) and one websocket path                                    #
# --------------------------------------------------------------------------- #

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_AGENT_MESSAGE = 64 * 1024 * 1024


def _unmask(data: bytes, mask: bytes) -> bytes:
    if not data:
        return data
    n = len(data)
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


def _frame(opcode: int, payload: bytes) -> bytes:
    length = len(payload)
    if length < 126:
        header = bytes([0x80 | opcode, length])
    elif length < 1 << 16:
        header = bytes([0x80 | opcode, 126]) + length.to_bytes(2, "big")
    else:
        header = bytes([0x80 | opcode, 127]) + length.to_bytes(8, "big")
    return header + payload


class _WebSocketLink:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer

    async def send(self, message: dict[str, Any]) -> None:
        if self._writer.is_closing():
            return
        self._writer.write(_frame(0x1, json.dumps(message, separators=(",", ":")).encode()))
        await self._writer.drain()

    async def control(self, opcode: int, payload: bytes = b"") -> None:
        if not self._writer.is_closing():
            self._writer.write(_frame(opcode, payload))
            await self._writer.drain()

    async def close(self) -> None:
        # Written, not drained: an agent that stopped reading must not hold
        # the close up (cdpproxy docstring, AN AGENT THAT GOES AWAY).
        # `close` flushes what is buffered on its own.
        if not self._writer.is_closing():
            self._writer.write(_frame(0x8, b""))
        self._writer.close()


async def _read_message(reader: asyncio.StreamReader, link: _WebSocketLink) -> bytes | None:
    """The next whole data message from a client, or None at close."""
    fragments: list[bytes] = []
    size = 0
    while True:
        first, second = await reader.readexactly(2)
        fin, opcode = first & 0x80, first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(await reader.readexactly(2), "big")
        elif length == 127:
            length = int.from_bytes(await reader.readexactly(8), "big")
        if not second & 0x80:
            return None  # a client frame must be masked (RFC 6455 5.1)
        mask = await reader.readexactly(4)
        size += length
        if size > _MAX_AGENT_MESSAGE:
            return None
        payload = _unmask(await reader.readexactly(length), mask)
        if opcode == 0x8:
            return None
        if opcode == 0x9:
            await link.control(0xA, payload)
            continue
        if opcode == 0xA:
            continue
        fragments.append(payload)
        if fin:
            return b"".join(fragments)


def _respond(writer: asyncio.StreamWriter, status: str, body: bytes = b"", content_type: str = "text/plain",
             headers: Mapping[str, str] | None = None) -> None:
    extra = "".join(f"{name}: {value}\r\n" for name, value in (headers or {}).items())
    writer.write(
        f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n{extra}Connection: close\r\n\r\n".encode()
        + body
    )


async def serve_agents(proxy: CdpProxy, *, host: str = "127.0.0.1", port: int = 9222) -> asyncio.Server:
    """The agent's CDP endpoint, as `connectOverCDP("http://127.0.0.1:9222")` expects it.

    Two refusals keep a WEB PAGE off it. Chromium shares the pod's network
    namespace, so a page it renders can reach 127.0.0.1:9222: a browser always
    sends `Origin` on a websocket, and Playwright's client never does, so a
    request carrying one is refused; and a page reaching it through a name it
    rebound to 127.0.0.1 sends that name as `Host`, so only a loopback Host is
    accepted. Chromium's own debugging endpoint applies the same two checks for
    the same reason.
    """
    browser_path = f"/devtools/browser/{uuid.uuid4()}"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _serve_one(proxy, reader, writer, browser_path)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, ValueError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port)


async def _serve_one(proxy: CdpProxy, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, browser_path: str) -> None:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or parts[0] != "GET":
        _respond(writer, "405 Method Not Allowed")
        return
    # The client tag is read and stripped here, before any path is matched
    # (`split_client`); nothing of it reaches Chromium.
    path, client = split_client(parts[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    local_port = writer.get_extra_info("sockname")[1]
    loopback = {f"127.0.0.1:{local_port}", f"localhost:{local_port}", "127.0.0.1", "localhost"}
    host = headers.get("host", "")
    if host not in loopback or "origin" in headers:
        _respond(writer, "403 Forbidden")
        return
    if path in ("/json/version", "/json/version/"):
        # The tag rides on into the websocket URL, so the connection is attributed.
        tagged = f"?{browser_tools.CLIENT_SEGMENT}={client}" if client else ""
        body = json.dumps(
            {"Browser": "swarm-live-gate", "Protocol-Version": "1.3",
             "webSocketDebuggerUrl": f"ws://{host}{browser_path}{tagged}"}
        ).encode()
        _respond(writer, "200 OK", body, "application/json")
        return
    if path == REFUSALS_PATH:
        # QA-GATE-REFUSALS (part of #2067): the requests the QA target's guard
        # answered itself, for the visual-QA capture. No target, no record: a
        # 404, so the capture writes `gate_refused: null`, never an empty list
        # that would read as "the gate refused nothing".
        document = proxy.qa_refusals()
        if document is None:
            _respond(writer, "404 Not Found")
        else:
            _respond(writer, "200 OK", json.dumps(document).encode(), "application/json")
        return
    if path != browser_path or headers.get("upgrade", "").lower() != "websocket" or "sec-websocket-key" not in headers:
        _respond(writer, "404 Not Found")
        return
    link = _WebSocketLink(writer)
    connection = await proxy.connect_agent(link, client=client)
    if connection is None:
        # Playwright puts the status line and the body in its error, so the
        # agent reads why and for how long (`CdpProxy.refusal`).
        _, reason = proxy.refusal()
        _respond(writer, "503 Service Unavailable", reason.encode("utf-8"),
                 headers={"Retry-After": str(RETRY_AFTER_SECONDS)})
        return
    accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + _WS_GUID).encode()).digest()).decode()
    writer.write(
        (
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    try:
        while True:
            data = await _read_message(reader, link)
            if data is None:
                break
            try:
                message = json.loads(data)
            except ValueError:
                continue
            if isinstance(message, dict):
                await proxy.on_agent_message(connection, message)
    finally:
        await proxy.disconnect_agent(connection)
