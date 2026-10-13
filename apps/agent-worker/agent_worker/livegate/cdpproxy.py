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
  for them;
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

Nothing here is logged. A refusal is recorded through `record` by method name
only; a page's content, a request body, a key or a parameter never is.

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
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from . import cdppolicy
from .cdppolicy import PolicyPaths
from .scripts import ADD_BINDING, ADD_SCRIPT, REMOVE_BINDING, REMOVE_SCRIPT, ScriptRegistry

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
# `text` to show it (the mask tracks those too, section 5.3).
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
    ) -> None:
        self._send = send_to_chromium
        self._paths = paths
        self._record = record
        self._on_control_ended = on_control_ended
        self._on_action = on_action
        self._on_pointer = on_pointer
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
        self._gate_sessions: dict[str, str] = {}  # watched page session -> targetId
        self._gate_children: dict[str, str] = {}  # frame session under a watched page -> targetId
        self._gate_contexts: set[str] = set()
        self._security: dict[str, str] = {}
        self._security_seen: dict[str, asyncio.Event] = {}
        self._controlled: str | None = None
        # Control was granted on about:blank and the page has not navigated
        # since: a state that is not `secure` is checked against the URL.
        self._granted_blank = False
        self._checks: set[asyncio.Task[None]] = set()
        #: Chromium's pipe closed (`chromium_closed`): nothing written is answered.
        self._gone = False

    # ------------------------------------------------------------------ state

    @property
    def paused(self) -> bool:
        return self._mode != _OPEN

    @property
    def in_control(self) -> bool:
        return self._controlled is not None

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

    # ------------------------------------------------------ agent connections

    async def connect_agent(self, link: AgentLink) -> AgentConnection | None:
        """A new agent connection, or None while a person is in control."""
        if self._mode != _OPEN:
            self._record("cdp_connection_refused", {})
            return None
        result = await self.call("Target.attachToBrowserTarget")
        session = result["sessionId"]
        if self._mode != _OPEN:
            await self._post("Target.detachFromTarget", {"sessionId": session}, None)
            self._record("cdp_connection_refused", {})
            return None
        connection = AgentConnection(link=link, browser_session=session)
        self._owner[session] = connection
        self._connections.append(connection)
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
        await self._post("Target.detachFromTarget", {"sessionId": connection.browser_session}, None)

    async def chromium_closed(self) -> None:
        """Chromium's pipe closed: nothing it was asked will be answered (BROWSER-RELAUNCH).

        Every gate call still waiting fails now rather than never, and every
        agent connection is closed: its sessions were in the browser that
        ended, so the agent reconnects to the browser started next. A gate
        call made after this fails at once.
        """
        self._gone = True
        pending, self._pending = self._pending, {}
        for entry in pending.values():
            if entry.future is not None and not entry.future.done():
                entry.future.set_exception(CdpError("the browser closed"))
        connections, self._connections = self._connections, []
        self._owner.clear()
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
        await connection.link.send(reply)

    async def on_agent_message(self, connection: AgentConnection, message: Mapping[str, Any]) -> None:
        if connection.closed:
            return
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
            self._record("cdp_method_refused", {"method": method})
            self._note(method, params, "method_policy")
            await self._reply_error(connection, message, refusal.message())
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
        reply: dict[str, Any] = {"id": pending.agent_id}
        if "error" in message:
            reply["error"] = message["error"]
        else:
            reply["result"] = result if isinstance(result, dict) else {}
        if pending.agent_session is not None:
            reply["sessionId"] = pending.agent_session
        if self._mode == _PAUSED:
            self._held_responses.append((pending, reply))
            return
        await connection.link.send(reply)

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

    async def _on_agent_event(self, owner: AgentConnection, session: str, method: str, params: dict[str, Any]) -> None:
        paused = self._mode == _PAUSED
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
            return
        params = self._strip(method, params)
        event: dict[str, Any] = {"method": method, "params": params}
        if session != owner.browser_session:
            event["sessionId"] = session
        await owner.link.send(event)

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
                if self._granted_blank:
                    self._check_blank(target)
                else:
                    self._end_control("insecure_connection")
        elif method == "Page.frameNavigated" and session in self._gate_sessions:
            frame = params.get("frame") or {}
            target = self._gate_sessions[session]
            if self._controlled != target or not isinstance(frame, dict) or frame.get("parentId"):
                return
            url = str(frame.get("url", ""))
            if _is_blank(url):
                return
            self._granted_blank = False
            if not _https_host(url):
                self._end_control("insecure_connection")
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

    def _refuse(self, cause: str) -> ControlDecision:
        self._record("control_refused", {"cause": cause})
        return ControlDecision(False, cause)

    async def take_control(self, target_id: str) -> ControlDecision:
        """A person takes control of `target_id`, if its connection is secure."""
        if self._mode != _PAUSED:
            return self._refuse("not_paused")
        await self.watch_page(target_id)
        if _is_blank(await self._url(target_id)):
            # No origin yet: nothing to call insecure (LB-R4). The first
            # navigation is judged as it commits (`_on_gate_event`).
            if self._mode != _PAUSED:
                return self._refuse("not_paused")
            self._controlled = target_id
            self._granted_blank = True
            return ControlDecision(True)
        if target_id not in self._security:
            seen = self._security_seen.setdefault(target_id, asyncio.Event())
            try:
                await asyncio.wait_for(seen.wait(), self._security_wait)
            except asyncio.TimeoutError:
                return self._refuse("security_state_unknown")
        if self._security.get(target_id) != "secure":
            return self._refuse("insecure_connection")
        if not _https_host(await self._url(target_id)):
            return self._refuse("insecure_connection")
        if self._mode != _PAUSED:
            return self._refuse("not_paused")
        self._controlled = target_id
        self._granted_blank = False
        return ControlDecision(True)

    async def _url(self, target_id: str) -> str:
        info = (await self.call("Target.getTargetInfo", {"targetId": target_id})).get("targetInfo") or {}
        return str(info.get("url", ""))

    def _check_blank(self, target_id: str) -> None:
        """A state that is not `secure` on a page granted blank: read its URL
        again, off the pipe reader (this runs inside `on_chromium_message`)."""
        task = asyncio.get_running_loop().create_task(self._recheck_blank(target_id))
        self._checks.add(task)
        task.add_done_callback(self._checks.discard)

    async def _recheck_blank(self, target_id: str) -> None:
        try:
            url = await self._url(target_id)
        except Exception:  # noqa: BLE001 -- unreadable is not blank: fail closed
            url = ""
        if self._controlled != target_id or not self._granted_blank:
            return
        if _is_blank(url):
            return
        if _https_host(url) and self._security.get(target_id) == "secure":
            # Navigated on, and the secure state arrived before this read.
            self._granted_blank = False
            return
        self._end_control("insecure_connection")

    def end_control(self, cause: str) -> None:
        """Control ends without a return: idle, renew lapsed, preempted, ..."""
        if self._controlled is not None:
            self._end_control(cause)

    def _end_control(self, cause: str) -> None:
        self._controlled = None
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
                await pending.owner.link.send(reply)
            while self._held:
                connection, message = self._held.pop(0)
                if not connection.closed:
                    await self._forward(connection, message)
        finally:
            self._mode = _OPEN

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


def _is_blank(url: str) -> bool:
    """`about:blank`: a page before its first navigation, with no origin."""
    parts = urlsplit(url)
    return parts.scheme == "about" and parts.path == "blank"


def _https_host(url: str) -> bool:
    """https, at a host name rather than an IP literal (section 5.4.4)."""
    parts = urlsplit(url)
    return parts.scheme == "https" and bool(parts.hostname) and not _is_address(parts.hostname or "")


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
                await sink.on_chromium_message(message)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()


# --------------------------------------------------------------------------- #
# The agent's side: 127.0.0.1:9222, /json/version and one websocket path      #
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
        await self.control(0x8)
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


def _respond(writer: asyncio.StreamWriter, status: str, body: bytes = b"", content_type: str = "text/plain") -> None:
    writer.write(
        f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
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
    path = parts[1]
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
    if path.split("?")[0] in ("/json/version", "/json/version/"):
        body = json.dumps(
            {"Browser": "swarm-live-gate", "Protocol-Version": "1.3", "webSocketDebuggerUrl": f"ws://{host}{browser_path}"}
        ).encode()
        _respond(writer, "200 OK", body, "application/json")
        return
    if path != browser_path or headers.get("upgrade", "").lower() != "websocket" or "sec-websocket-key" not in headers:
        _respond(writer, "404 Not Found")
        return
    link = _WebSocketLink(writer)
    connection = await proxy.connect_agent(link)
    if connection is None:
        _respond(writer, "503 Service Unavailable", b"a person is in control of the browser")
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
