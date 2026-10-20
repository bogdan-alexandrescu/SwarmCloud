"""The QA identity, held by the gate: a credential added to console requests only (QI-GATE).

docs/design/qa-identity.md §4.4 (part of #1649). The worker asks swarm-api for
a short-lived IAP JWT signed as the QA service account and hands it to the
gate over ctl.sock (`qa_identity`). The gate adds it to requests for the
console host that come from console frames, on its OWN CDP sessions, and the
agent never holds it: every agent-bound message is scrubbed of it, and the
agent's CDP may neither rewrite a console request's headers nor redirect a
request (cdppolicy.py `qa_identity_check`).

THE HEADER IS `Authorization`, NEVER `Proxy-Authorization`. The design wrote
`Proxy-Authorization`; M3 (docs/design/qa-identity-measurements.md,
2026-10-20, Chrome 153.0.8010.12) measured Chromium refusing
`Fetch.continueRequest` that adds it ("Unsafe header") while accepting
`Authorization`, and M1 measured IAP admitting the JWT in either. So by
§4.9's fallback the verb accepts `Authorization` only. A refused
`continueRequest` would otherwise leave the request paused; should Chromium
ever refuse the one this module sends, the request is continued unmodified
(`_continue`), which reads as signed out, never as a hung page.

INJECTION (§4.4 rules 1-3). A `Request`-stage pause gets the header only when
its URL is `https://<host>/...` exactly (no userinfo, no port, no other
scheme or host), the request is the top frame's own navigation OR the gate's
frame map says the requesting frame's top-level origin is `https://<host>`,
and a credential is held and unexpired. A worker target has no frame, so a
dedicated or service worker's request never gets it (fails closed: IAP
answers it as a signed-out caller). Never `failRequest`: a missing credential
looks like being signed out, not a broken site.

M5: AN ADDED HEADER FOLLOWS NO REDIRECT, same-origin included. A console
redirect's own request comes back as a pause of its own (`redirectedRequestId`)
and is authenticated only because the rules above add the header again.

THE TWO REDIRECT GUARDS are built although M5 found neither load-bearing on
this Chromium: nothing holds a later build to that behaviour.

* Guard 1, `Response` stage on `https://<host>/*`: a 3xx to a request this
  gate added the header to, whose `Location` resolves to any other origin, is
  not followed. The gate answers it with a page of its own
  (`stopped_page`) and records `qa_identity_redirect_stopped` with the
  origin. A redirect staying on the host is continued.
* Guard 2, `Request` stage on `*`: a request to any other host that carries
  the held credential in a header is continued with that header removed,
  recorded `qa_identity_header_stripped`.

  WHERE THIS DIFFERS FROM §4.4, AND WHY. The design strips "`<header>`" from
  every other host's request, which for `Proxy-Authorization` costs nothing
  (no page sets it). With M3's `Authorization` it would strip every bearer
  token every other site's own script sends, breaking any API login on the
  web for the whole attempt. So guard 2 strips a header whose VALUE carries a
  credential this gate held, which is exactly the leak the guard exists for.
  The same rule decides which headers the scrubber removes (below), while
  `proxy-authorization` is removed by name, as designed.

SCRUBBING (`QaIdentity.scrub`, called by the CDP proxy on every message it
sends an agent, from the moment a host is set). Header objects in events and
results lose `proxy-authorization` and any header carrying a held credential;
every occurrence of a held credential anywhere in the message becomes
`[qa-identity]`; a `base64Encoded` body (`getResponseBody`,
`Page.getResourceContent`, `IO.read`) is decoded, scrubbed and re-encoded,
because a byte match on base64 would miss it. Every credential held in this
attempt is kept for the scrub until the gate ends, so a renewal does not
unscrub the one before it and a frame still in flight after `wipe` is scrubbed. `qa_identity_scrubbed` records a count, never a value.

KNOWN LIMIT, stated so nobody relies on more: a console response that
reflected the header could be read by the page's own script and transformed
(reversed, re-encoded) before the agent reads it. swarm-api's standing rule
never to echo a request header (§4.3) is what covers that, not this module.
An `IO.read` chunk boundary inside a credential is not matched either; the
agent reads streams of its own PDFs only.

NOTHING HERE LOGS OR RECORDS A CREDENTIAL. Records carry counts, causes,
origins and expiries.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

log = logging.getLogger(__name__)

#: The ctl.sock operation (ctl.py `OPS`).
OP = "qa_identity"
#: M3: the one header Chromium lets `Fetch.continueRequest` add and IAP reads.
HEADER = "Authorization"
SCHEME = "Bearer"
#: What a held credential reads as in anything the agent receives.
PLACEHOLDER = "[qa-identity]"
#: Removed by name from every agent-bound header object (§4.4).
_ALWAYS_REMOVED = "proxy-authorization"
#: The longest life the gate accepts: Q5 is 15 minutes; an hour is a ceiling
#: that catches a unit mistake (milliseconds) without second-guessing swarm-api.
MAX_EXPIRES_IN = 3600.0
#: A JWT is three base64url segments; anything outside this set (whitespace,
#: CR, LF) could split a header and is refused.
_CREDENTIAL = re.compile(r"[A-Za-z0-9._~+/=-]{16,8192}")
#: A host name, lower case, with at least one dot, no port, no IP literal.
_HOST = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
_CAUSE = re.compile(r"[a-z0-9_]{1,64}")
#: Credentials kept for the scrub (a renewal every 10 minutes is 6 an hour).
MAX_KEPT = 256
#: Request ids remembered, oldest forgotten first.
MAX_REQUESTS = 8192
#: The keys whose value is a header object (a dict, or a list of name/value).
_HEADER_KEYS = frozenset({"headers", "requestHeaders", "responseHeaders", "associatedHeaders"})
#: How deep the header walk looks: `params.redirectResponse.requestHeaders` is 3.
_HEADER_DEPTH = 4
#: The result fields that hold a body, base64 when `base64Encoded` is true.
_BODY_FIELDS = ("body", "content", "data")

Record = Callable[[str, Mapping[str, Any]], None]


class QaIdentityRefused(ValueError):
    """The verb is refused: malformed, a different host, or after the wipe. `code` says which."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def console_url(url: Any, host: str | None) -> bool:
    """Rule 1: `https://<host>/...` exactly: no userinfo, no port, no other scheme."""
    if not host or not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and parts.netloc == host


def origin_of(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def stopped_page(origin: str) -> str:
    """Guard 1's page: what the person and the agent see instead of the redirect."""
    shown = html.escape(origin)
    return (
        "<!doctype html><meta charset=utf-8><title>Redirect stopped</title>"
        "<h1>Redirect stopped by the live gate</h1>"
        f"<p>The console redirected this browser to <code>{shown}</code>. The redirect was stopped "
        "because this browser holds the QA identity, which is sent to the console only.</p>"
    )


class QaIdentity:
    """The credential, its host and expiry; the scrub every agent-bound message passes.

    Lives as long as the gate: a relaunched Chromium gets a new `QaInjector`
    over the same identity. `clock` is monotonic seconds.
    """

    def __init__(self, record: Record, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._record = record
        self._clock = clock
        #: The console host, set by the first verb and never changed or cleared
        #: before the gate ends: the task named the identity (`armed`).
        self.host: str | None = None
        self._credential: str | None = None
        self._expires_at = 0.0
        self._kept: list[str] = []
        self.cause: str | None = None
        self.wiped = False
        self.renewals = 0

    # ------------------------------------------------------------- state

    @property
    def armed(self) -> bool:
        """The task named the QA identity: its host's rules apply for the rest of the gate."""
        return self.host is not None

    @property
    def held(self) -> bool:
        """Rule 3: a credential is held and has not expired."""
        return self._credential is not None and self._clock() < self._expires_at

    def header_value(self) -> str | None:
        return f"{SCHEME} {self._credential}" if self.held else None

    def carries(self, text: Any) -> bool:
        """Whether `text` holds any credential this gate has held."""
        return isinstance(text, str) and any(k in text for k in self._kept)

    def status(self) -> dict[str, Any]:
        """For `status`: never the credential."""
        if self.host is None:
            return {"state": "none"}
        if self.wiped:
            state = "wiped"
        elif self.held:
            state = "held"
        elif self._credential is not None:
            state = "expired"
        else:
            state = "failed"
        out: dict[str, Any] = {"state": state, "host": self.host, "renewals": self.renewals}
        if state == "failed" and self.cause:
            out["cause"] = self.cause
        return out

    # --------------------------------------------------------------- ctl

    def handle(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """`{host, header, scheme, credential, expires_in}` sets or renews; `{failed: cause}` clears."""
        if self.wiped:
            self._record("qa_identity_refused", {"cause": "wiped"})
            raise QaIdentityRefused("wiped")
        if "failed" in args:
            cause = str(args.get("failed") or "")
            self.cause = cause if _CAUSE.fullmatch(cause) else "unspecified"
            self._credential = None
            self._expires_at = 0.0
            self._record("qa_identity_failed", {"cause": self.cause})
            return {"qa_identity": "failed", "cause": self.cause}
        host, header, scheme = args.get("host"), args.get("header"), args.get("scheme")
        credential, expires_in = args.get("credential"), args.get("expires_in")
        code = None
        if not isinstance(host, str) or not _HOST.fullmatch(host):
            code = "host"
        elif self.host is not None and host != self.host:
            code = "host_changed"
        elif not isinstance(header, str) or header.lower() != HEADER.lower():
            # M3: Chromium refuses to add Proxy-Authorization; nothing else is IAP's.
            code = "header"
        elif scheme != SCHEME:
            code = "scheme"
        elif not isinstance(credential, str) or not _CREDENTIAL.fullmatch(credential):
            code = "credential"
        elif (not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool)
              or not 0 < expires_in <= MAX_EXPIRES_IN):
            code = "expires_in"
        if code is not None:
            self._record("qa_identity_refused", {"cause": code})
            raise QaIdentityRefused(code)
        assert isinstance(host, str) and isinstance(credential, str) and isinstance(expires_in, (int, float))
        renewal = self._credential is not None or bool(self._kept)
        self.host = host
        self._credential = credential
        self._expires_at = self._clock() + float(expires_in)
        self.cause = None
        if credential not in self._kept:
            self._kept.append(credential)
            del self._kept[:-MAX_KEPT]
        if renewal:
            self.renewals += 1
        self._record("qa_identity_set", {"host": host, "expires_in": int(expires_in), "renewal": renewal})
        return {"qa_identity": "held", "expires_in": int(expires_in)}

    def wipe(self) -> None:
        """The attempt's wipe: the credential goes first, and the verb is refused from here.

        `_kept` stays: the wipe runs before Chromium and the agents' CDP links
        close, and a console request in flight at wipe time can still reach an
        agent as a `Network.requestWillBeSentExtraInfo` carrying the header.
        The scrub matches on `_kept`, so it lives as long as the gate does.
        """
        self.wiped = True
        self._credential = None
        self._expires_at = 0.0

    # ------------------------------------------------------------- scrub

    def scrub(self, message: dict[str, Any]) -> dict[str, Any]:
        """`message` as an agent may receive it (module docstring). Unchanged when nothing matched."""
        if self.host is None:
            return message
        removed = 0
        out: Any = message
        for key in ("params", "result"):
            value = message.get(key)
            if isinstance(value, dict):
                cleaned, n = self._drop_headers(value, _HEADER_DEPTH)
                if n:
                    removed += n
                    out = {**out, key: cleaned}
        replaced = 0
        if self._kept:
            result = out.get("result")
            if isinstance(result, dict) and result.get("base64Encoded") is True:
                cleaned_result, n = self._scrub_base64(result)
                if n:
                    replaced += n
                    out = {**out, "result": cleaned_result}
            text = json.dumps(out, separators=(",", ":"))
            hits = sum(text.count(k) for k in self._kept)
            if hits:
                for k in self._kept:
                    text = text.replace(k, PLACEHOLDER)
                replaced += hits
                out = json.loads(text)
        if removed or replaced:
            fields: dict[str, Any] = {"count": removed + replaced}
            method = message.get("method")
            if isinstance(method, str):
                fields["method"] = method
            self._record("qa_identity_scrubbed", fields)
        return out

    def _bad_header(self, name: Any, value: Any) -> bool:
        return (isinstance(name, str) and name.lower() == _ALWAYS_REMOVED) or self.carries(value)

    def _drop_headers(self, value: dict[str, Any], depth: int) -> tuple[dict[str, Any], int]:
        removed = 0
        out = value
        for key, item in value.items():
            if key in _HEADER_KEYS:
                if isinstance(item, dict):
                    kept = {n: v for n, v in item.items() if not self._bad_header(n, v)}
                    if len(kept) != len(item):
                        removed += len(item) - len(kept)
                        out = {**out, key: kept}
                    continue
                if isinstance(item, list):
                    kept_list = [h for h in item if not (
                        isinstance(h, dict) and self._bad_header(h.get("name"), h.get("value")))]
                    if len(kept_list) != len(item):
                        removed += len(item) - len(kept_list)
                        out = {**out, key: kept_list}
                    continue
            if depth > 1 and isinstance(item, dict):
                cleaned, n = self._drop_headers(item, depth - 1)
                if n:
                    removed += n
                    out = {**out, key: cleaned}
        return out, removed

    def _scrub_base64(self, result: dict[str, Any]) -> tuple[dict[str, Any], int]:
        out, hits = result, 0
        for field_name in _BODY_FIELDS:
            body = result.get(field_name)
            if not isinstance(body, str):
                continue
            try:
                raw = base64.b64decode(body, validate=True)
            except (binascii.Error, ValueError):
                continue
            found = 0
            for k in self._kept:
                needle = k.encode()
                found += raw.count(needle)
                raw = raw.replace(needle, PLACEHOLDER.encode())
            if found:
                hits += found
                out = {**out, field_name: base64.b64encode(raw).decode("ascii")}
        return out, hits

    def undecodable_body(self, result: Mapping[str, Any]) -> bool:
        """A `base64Encoded` body the scrub cannot read: refused for a console request."""
        if result.get("base64Encoded") is not True:
            return False
        for field_name in _BODY_FIELDS:
            body = result.get(field_name)
            if body is None:
                continue
            if not isinstance(body, str):
                return True
            try:
                base64.b64decode(body, validate=True)
            except (binascii.Error, ValueError):
                return True
        return False


# --------------------------------------------------------------------------- #
# The browser side: the gate's own sessions                                   #
# --------------------------------------------------------------------------- #


class GateCdp(Protocol):
    async def call(self, method: str, params: Mapping[str, Any] | None = None,
                   session_id: str | None = None) -> dict[str, Any]: ...


@dataclass
class _Target:
    target_id: str
    kind: str  # "page", "iframe", "worker", ...


def _patterns(host: str, held: bool) -> list[dict[str, str]]:
    """§4.4's three patterns; without a credential, guard 2's `*` is not enabled."""
    console = f"https://{host}/*"
    patterns = [{"urlPattern": console, "requestStage": "Request"},
                {"urlPattern": console, "requestStage": "Response"}]
    if held:
        patterns.append({"urlPattern": "*", "requestStage": "Request"})
    return patterns


class _Bounded:
    """An insertion-ordered set that forgets its oldest entries."""

    def __init__(self, limit: int = MAX_REQUESTS) -> None:
        self._items: dict[str, None] = {}
        self._limit = limit

    def add(self, item: str) -> None:
        self._items[item] = None
        while len(self._items) > self._limit:
            self._items.pop(next(iter(self._items)))

    def discard(self, item: str) -> None:
        self._items.pop(item, None)

    def __contains__(self, item: object) -> bool:
        return item in self._items


class QaInjector:
    """The injection rules and both redirect guards, on sessions the gate owns.

    One per Chromium. `start` attaches the gate to every page (and, through
    each page's own auto-attach, to its out-of-process frames and workers);
    `on_event` is fed every message from the pipe (the gate's
    `on_chromium_message`) and never waits: each answer runs on a task.
    """

    def __init__(self, cdp: GateCdp, qa: QaIdentity, *, record: Record) -> None:
        self._cdp = cdp
        self._qa = qa
        self._record = record
        self.started = False
        self._sessions: dict[str, _Target] = {}
        self._attaching: set[str] = set()
        self._frames: dict[str, tuple[str | None, str | None]] = {}  # frameId -> (parentId, url)
        #: The main frames of the pages the gate attached to (a page's main
        #: frame id is its targetId): the only frames that may end a parent walk.
        self._mains: set[str] = set()
        self._added = _Bounded()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    # --------------------------------------------------------------- life

    async def start(self) -> None:
        """Attach to every page there is, and to each page created from now."""
        if self.started or self._closed:
            return
        self.started = True
        await self._quietly("Target.setDiscoverTargets", {"discover": True}, None)
        listed = await self._quietly("Target.getTargets", {}, None) or {}
        for info in listed.get("targetInfos") or []:
            if isinstance(info, Mapping) and info.get("type") == "page" and isinstance(info.get("targetId"), str):
                await self._attach_page(info["targetId"], info.get("url"))

    async def refresh(self) -> None:
        """The credential changed: every session's patterns follow (`_patterns`)."""
        if not self.started:
            await self.start()
            return
        for session in list(self._sessions):
            await self._enable(session)

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _spawn(self, work: Awaitable[Any]) -> None:
        task = asyncio.get_running_loop().create_task(work)  # type: ignore[arg-type]
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _quietly(self, method: str, params: Mapping[str, Any], session: str | None) -> dict[str, Any] | None:
        try:
            return await self._cdp.call(method, params, session)
        except Exception as exc:  # noqa: BLE001 -- a target that went is not an error here
            log.debug("qa identity: %s failed (%s)", method, type(exc).__name__)
            return None

    async def _enable(self, session: str) -> None:
        host = self._qa.host
        if host is None:
            return
        await self._quietly("Fetch.enable", {"patterns": _patterns(host, self._qa.held)}, session)

    async def _attach_page(self, target_id: str, url: Any = None) -> None:
        if target_id in self._attaching or any(t.target_id == target_id for t in self._sessions.values()):
            return
        self._attaching.add(target_id)
        try:
            result = await self._quietly("Target.attachToTarget", {"targetId": target_id, "flatten": True}, None)
            session = (result or {}).get("sessionId")
            if not isinstance(session, str):
                return
            self._sessions[session] = _Target(target_id, "page")
            # The page's main frame id is its targetId; its URL is known before
            # its first `frameNavigated` reaches this session.
            self._mains.add(target_id)
            self._frames.setdefault(target_id, (None, url if isinstance(url, str) else None))
            await self._setup(session, framed=True)
        finally:
            self._attaching.discard(target_id)

    async def _setup(self, session: str, *, framed: bool, waiting: bool = False) -> None:
        try:
            if framed:
                await self._quietly("Page.enable", {}, session)
                # `Page.enable` reports frames from now on; the ones already
                # there are learnt from the tree, or a frame of an open page
                # would have no known parent (and so no header).
                tree = await self._quietly("Page.getFrameTree", {}, session) or {}
                self._learn_tree(tree.get("frameTree"))
            await self._enable(session)
            await self._quietly("Target.setAutoAttach",
                                {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}, session)
        finally:
            if waiting:
                await self._quietly("Runtime.runIfWaitingForDebugger", {}, session)

    def _learn_tree(self, node: Any, depth: int = 0) -> None:
        if not isinstance(node, Mapping) or depth > 64:
            return
        frame = node.get("frame") or {}
        frame_id = frame.get("id")
        if isinstance(frame_id, str):
            parent = frame.get("parentId")
            known = self._frames.get(frame_id, (None, None))[0]
            self._frames[frame_id] = (parent if isinstance(parent, str) else known, str(frame.get("url", "")))
        for child in node.get("childFrames") or []:
            self._learn_tree(child, depth + 1)

    # ------------------------------------------------------------- events

    def on_event(self, message: Mapping[str, Any]) -> None:
        if self._closed or self._qa.host is None:
            return
        method = message.get("method")
        raw = message.get("params")
        params: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        session = message.get("sessionId")
        if not isinstance(method, str):
            return
        if session is None:
            if method == "Target.targetCreated" and self.started:
                info = params.get("targetInfo") or {}
                if info.get("type") == "page" and isinstance(info.get("targetId"), str):
                    self._spawn(self._attach_page(info["targetId"], info.get("url")))
            elif method == "Target.detachedFromTarget":
                self._forget(params.get("sessionId"))
            return
        if session not in self._sessions:
            return
        if method == "Fetch.requestPaused":
            self._spawn(self._paused(session, params))
        elif method == "Target.attachedToTarget":
            child = params.get("sessionId")
            info = params.get("targetInfo") or {}
            if isinstance(child, str):
                kind = str(info.get("type", ""))
                target_id = str(info.get("targetId", ""))
                self._sessions[child] = _Target(target_id, kind)
                parent = info.get("parentFrameId")
                if kind == "iframe" and isinstance(parent, str) and target_id:
                    # An out-of-process frame's id is its targetId; Chromium names its parent here.
                    self._frames[target_id] = (parent, self._frames.get(target_id, (None, None))[1])
                self._spawn(self._setup(child, framed=kind == "iframe",
                                        waiting=params.get("waitingForDebugger") is True))
        elif method == "Target.detachedFromTarget":
            self._forget(params.get("sessionId"))
        elif method == "Page.frameAttached":
            frame, parent = params.get("frameId"), params.get("parentFrameId")
            if isinstance(frame, str):
                _, url = self._frames.get(frame, (None, None))
                self._frames[frame] = (parent if isinstance(parent, str) else None, url)
        elif method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            frame_id = frame.get("id")
            if isinstance(frame_id, str):
                parent = frame.get("parentId")
                if not isinstance(parent, str):
                    parent = self._frames.get(frame_id, (None, None))[0]
                self._frames[frame_id] = (parent, str(frame.get("url", "")))
        elif method == "Page.frameDetached":
            if params.get("reason") != "swap":
                self._frames.pop(str(params.get("frameId")), None)

    def _forget(self, session: Any) -> None:
        if isinstance(session, str):
            self._sessions.pop(session, None)

    def top_origin(self, frame_id: Any) -> str | None:
        """The origin of `frame_id`'s top-level frame, from the gate's own frame map.

        The walk ends only at a page's main frame. A frame whose parent the
        gate never learnt -- an out-of-process frame reporting no `parentId`,
        under a page whose `frameAttached` it did not see -- has no known top
        and gets no header: it must not be taken for a top frame itself."""
        seen = 0
        while isinstance(frame_id, str) and seen < 64:
            parent, url = self._frames.get(frame_id, (None, None))
            if frame_id not in self._frames:
                return None
            if parent is None:
                return origin_of(url) if frame_id in self._mains else None
            frame_id = parent
            seen += 1
        return None

    def _from_console(self, session: str, params: Mapping[str, Any]) -> bool:
        """Rule 2."""
        target = self._sessions.get(session)
        host = self._qa.host
        if target is None or host is None or target.kind not in ("page", "iframe"):
            return False  # a worker has no frame (§4.4)
        frame = params.get("frameId")
        if (target.kind == "page" and params.get("resourceType") == "Document"
                and frame == target.target_id):
            return True
        return self.top_origin(frame) == f"https://{host}"

    async def _paused(self, session: str, params: Mapping[str, Any]) -> None:
        request_id = params.get("requestId")
        if not isinstance(request_id, str):
            return
        if "responseStatusCode" in params or "responseErrorReason" in params:
            await self._response(session, request_id, params)
            return
        request = params.get("request") or {}
        url = request.get("url")
        raw_headers = request.get("headers")
        headers: dict[str, Any] = dict(raw_headers) if isinstance(raw_headers, Mapping) else {}
        host = self._qa.host
        kept = {n: v for n, v in headers.items() if not self._qa.carries(v)}
        stripped = len(kept) != len(headers)
        value = self._qa.header_value()
        if value is not None and console_url(url, host) and self._from_console(session, params):
            out = [{"name": n, "value": str(v)} for n, v in kept.items() if n.lower() != HEADER.lower()]
            out.append({"name": HEADER, "value": value})
            self._added.add(request_id)
            await self._continue(session, request_id, out)
            return
        if stripped:
            self._record("qa_identity_header_stripped", {"origin": origin_of(url) or "unknown"})
            await self._continue(session, request_id, [{"name": n, "value": str(v)} for n, v in kept.items()])
            return
        await self._quietly("Fetch.continueRequest", {"requestId": request_id}, session)

    async def _continue(self, session: str, request_id: str, headers: list[dict[str, str]]) -> None:
        try:
            await self._cdp.call("Fetch.continueRequest", {"requestId": request_id, "headers": headers}, session)
        except Exception as exc:  # noqa: BLE001 -- never leave a request paused (module docstring)
            self._record("qa_identity_continue_refused", {"cause": type(exc).__name__})
            self._added.discard(request_id)
            await self._quietly("Fetch.continueRequest", {"requestId": request_id}, session)

    async def _response(self, session: str, request_id: str, params: Mapping[str, Any]) -> None:
        added = request_id in self._added
        self._added.discard(request_id)
        status = params.get("responseStatusCode")
        host = self._qa.host
        if added and host is not None and isinstance(status, int) and 300 <= status < 400:
            location = None
            for h in params.get("responseHeaders") or []:
                if isinstance(h, Mapping) and str(h.get("name", "")).lower() == "location":
                    location = h.get("value")
            request = params.get("request") or {}
            if isinstance(location, str):
                target = urljoin(str(request.get("url", "")), location)
                origin = origin_of(target) or "unknown"
                if origin != f"https://{host}":
                    self._record("qa_identity_redirect_stopped", {"origin": origin})
                    body = base64.b64encode(stopped_page(origin).encode()).decode("ascii")
                    fulfilled = await self._quietly("Fetch.fulfillRequest", {
                        "requestId": request_id, "responseCode": 200,
                        "responseHeaders": [{"name": "Content-Type", "value": "text/html; charset=utf-8"},
                                            {"name": "Cache-Control", "value": "no-store"}],
                        "body": body,
                    }, session)
                    if fulfilled is None:
                        # Never followed: a page that fails to load is the safe failure here.
                        await self._quietly("Fetch.failRequest",
                                            {"requestId": request_id, "errorReason": "BlockedByClient"}, session)
                    return
        await self._quietly("Fetch.continueResponse", {"requestId": request_id}, session)


class ConsoleRequests:
    """Which of the agent's requests were on the console host, by request id.

    Fed from the agent's own `Fetch.requestPaused` and `Network.requestWillBeSent`
    events (cdpproxy.py); asked by the method policy. An id it does not know --
    never seen, or forgotten as the oldest -- counts as a console request, so
    a forgotten one fails closed.
    """

    def __init__(self, qa: QaIdentity, limit: int = MAX_REQUESTS) -> None:
        self._qa = qa
        self._hosts: dict[str, bool] = {}
        self._limit = limit

    def note(self, request_id: Any, url: Any) -> None:
        if not isinstance(request_id, str):
            return
        self._hosts[request_id] = console_url(url, self._qa.host)
        while len(self._hosts) > self._limit:
            self._hosts.pop(next(iter(self._hosts)))

    def __call__(self, request_id: Any) -> bool:
        if not isinstance(request_id, str):
            return True
        return self._hosts.get(request_id, True)
