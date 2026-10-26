"""The live gate's watch on the page on screen: challenges, and a closed browser (§4.1, §5.3).

docs/design/live-browser.md §5.3. THE GATE PAINTS NOTHING. Owner decision, BGD
2026-10-25: "Stop redacting elements or sections of the visual content that a
browser agent or session displays via the live browser. There is no need to
redact parts of the screen in any situation." Every frame the gate captures
leaves the pod as Chromium drew it -- to the live view, the thumbnails and the
relay's recordings -- in every situation. Until then this module was the mask
(LB-3): boxes over password and credential fields, over text a
`swarm_redaction` rule matched and over the address bar, and, failing closed,
the page area, a child frame's box or the whole frame painted out whenever a
rectangle report was stale, missing or could not be placed. That fail-closed
rule is what blanked ad-heavy and iframe-heavy pages for minutes (#1030
LIVE-BLANK, #1340, #1344 DEVB-MASK). The module keeps its name; what is left
is what rides the same gate-only CDP session and is not painting.

WHAT IT STILL DOES. `MaskCdp`, on the gate's own CDP session to the page on
screen (Chromium's pipe, which the agent never sees), follows that page's
frames and their navigations -- out-of-process iframes too, each through a
child session (`Target.setAutoAttach`, `MaskCdp(child=True)`) -- into
`MaskState`, which tells `on_challenge` the page's host once per page load
when a frame on it is a challenge (`SHOWN_IN_CONTROL`, read by
`challenge_host` and, through `is_challenge_host`, by tabs.py). The gate
decides whether that is a hand-off (`LiveGate._on_challenge`, CAPTCHA-HANDOFF,
part of #2058). And `reason` says, as the RFB desktop name, that the agent has
finished once the attempt ended and the gate closed Chromium
(`REASON_BROWSER_CLOSED`, GREY-TAIL, part of #1344).

NOTHING READ HERE IS LOGGED (§5.3.3). A frame's URL is read to match it
against `SHOWN_IN_CONTROL` and never logged; `on_challenge` is told a host,
never a path or a query.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

#: GREY-TAIL (part of #1344): the attempt ended and the gate closed Chromium
#: (`LiveGate.wipe`, cause `attempt_end`, through `browser_closed`); the pod
#: stops 2-12 s later. Said to every viewer as the RFB desktop name, one
#: sentence (rfb.py `desktop_name`). MEASURED 2026-10-23 (batch
#: mask1344-2026-10-23): the grey tail lasted up to 12.3 s. Owner decision
#: (BGD, 2026-10-23): "Say the agent finished."
REASON_BROWSER_CLOSED = "The agent has finished; its browser is closed."

#: The challenge frames a person must see to get past them (issue #1340):
#: (host, path prefix). Exact hosts; "*." is any subdomain of the rest and
#: never the rest itself. `https`, no port, no userinfo. A frame on this list
#: raises the CAPTCHA hand-off (`MaskState.on_challenge`), and tabs.py
#: follows a tab on one of these hosts. The name is the list's history: it
#: was the challenge frames the mask showed while a person held control.
SHOWN_IN_CONTROL = (
    ("www.google.com", "/recaptcha/"),
    ("www.recaptcha.net", "/recaptcha/"),
    ("*.hcaptcha.com", "/"),
    ("challenges.cloudflare.com", "/"),
)

#: CAPTCHA-HANDOFF (BGD, 2026-10-23; part of #2058): the most pages whose
#: hand-off was raised are remembered, so one load raises one request.
CHALLENGED_PAGES = 64


@dataclass
class _FrameState:
    parent: str | None
    url: str | None = None


class MaskState:
    """The frames of the page on screen, the challenges on it, and whether the browser is closed.

    Driven by `MaskCdp` (the CDP events of the gate's session on the page)
    and by the gate (`browser_closed`). Single-threaded: the gate calls it
    from its event loop.
    """

    def __init__(self, *, on_challenge: Callable[[str], None] | None = None) -> None:
        #: CAPTCHA-HANDOFF (part of #2058): told the followed page's host, once
        #: per page load, when a frame on it is a challenge (`SHOWN_IN_CONTROL`,
        #: the list `challenge_host` and tabs.py read). The gate decides whether
        #: that is a hand-off (`LiveGate._on_challenge`).
        self.on_challenge = on_challenge
        #: The top frames (a page's is its target id) whose current load has
        #: told `on_challenge`. Kept apart from `_frames`, which a tab switch
        #: empties: a page followed again is the same load, not a new one.
        self._challenged: OrderedDict[str, None] = OrderedDict()
        #: The attempt ended and Chromium was closed (`browser_closed`).
        self._closed = False
        self._frames: dict[str, _FrameState] = {}
        self._top: str | None = None

    @property
    def reason(self) -> str | None:
        """What a viewer is told with every frame: `REASON_BROWSER_CLOSED`, or None."""
        return REASON_BROWSER_CLOSED if self._closed else None

    # -- what the gate tells it ------------------------------------------

    def session_up(self) -> None:
        """The gate's CDP session is attached to a page: the browser is open."""
        self._closed = False

    def browser_closed(self) -> None:
        """The attempt ended and Chromium is closed: `REASON_BROWSER_CLOSED` until a session is up."""
        self._closed = True

    def has_frame(self, frame_id: str) -> bool:
        return frame_id in self._frames

    def frame_attached(self, frame_id: str, parent_id: str | None) -> None:
        if frame_id in self._frames:
            return
        self._frames[frame_id] = _FrameState(parent=parent_id)
        if parent_id is None:
            self._top = frame_id

    def frame_detached(self, frame_id: str) -> None:
        self._frames.pop(frame_id, None)
        for child in [fid for fid, f in self._frames.items() if f.parent == frame_id]:
            self.frame_detached(child)
        if self._top == frame_id:
            self._top = None

    def navigated(self, frame_id: str, url: str) -> None:
        """A cross-document navigation committed."""
        frame = self._frames.get(frame_id)
        if frame is None:
            return
        if frame_id == self._top and frame.url is not None:
            # A new load of the page; a frame first listed by a follow's
            # install (its url still unknown) is not one.
            self._challenged.pop(frame_id, None)
        frame.url = url
        self._note_challenge(frame_id, url)

    def url_changed(self, frame_id: str, url: str) -> None:
        """A same-document navigation (history API, fragment): the same load, maybe a challenge now."""
        frame = self._frames.get(frame_id)
        if frame is None:
            return
        frame.url = url
        self._note_challenge(frame_id, url)

    def listed(self, frame_id: str, url: str) -> None:
        """A frame tree the gate read lists `frame_id`, already known, at `url`.

        Not a navigation: a navigation is an event, and one that committed
        while the tree was read was handled as one (#1344).
        """
        frame = self._frames.get(frame_id)
        if frame is not None and frame.url != url:
            self.url_changed(frame_id, url)

    def _note_challenge(self, frame_id: str, url: str) -> None:
        """Tell `on_challenge` the page's host when `frame_id` at `url` is a challenge. Once per load.

        A child frame is one by `challenge_host` (host and path); the top
        frame by its host alone, at any path, as tabs.py reads a tab
        (`is_challenge_host`). The page's host is what is told -- the site
        the person is asked to act on, never a path or a query -- or the
        challenge's own when the page has none.
        """
        top = self._top
        if self.on_challenge is None or top is None or top in self._challenged:
            return
        if frame_id == top:
            try:
                host = urlsplit(url).hostname
            except ValueError:
                host = None
            if not host or not is_challenge_host(host):
                return
        else:
            host = challenge_host(url)
            if host is None:
                return
        self._challenged[top] = None
        while len(self._challenged) > CHALLENGED_PAGES:
            self._challenged.popitem(last=False)
        page = self._frames[top].url if top in self._frames else None
        try:
            parts = urlsplit(page or "")
            page_host = parts.hostname if parts.scheme in ("http", "https") else None
        except ValueError:
            page_host = None
        try:
            self.on_challenge(page_host or host)
        except Exception as exc:  # noqa: BLE001 -- the page watch never fails for the hand-off
            log.warning("live gate: the challenge hook failed (%s)", type(exc).__name__)


def is_challenge_host(host: str) -> bool:
    """Whether `host` (lower case) is one of `SHOWN_IN_CONTROL`'s, at any path.

    tabs.py reads a tab this way; `MaskState` reads the agent's top frame this
    way too (CAPTCHA-HANDOFF), so there is one list and one match.
    """
    for allowed, _ in SHOWN_IN_CONTROL:
        if allowed.startswith("*."):
            suffix = allowed[1:]
            if host.endswith(suffix) and len(host) > len(suffix):
                return True
        elif host == allowed:
            return True
    return False


def challenge_host(url: str | None) -> str | None:
    """`url`'s host when it is on `SHOWN_IN_CONTROL`; None otherwise. The host only: never logged with more."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme != "https" or port is not None or parts.username is not None
            or parts.password is not None or not parts.hostname):
        return None
    host = parts.hostname
    for allowed, prefix in SHOWN_IN_CONTROL:
        if allowed.startswith("*."):
            suffix = allowed[1:]
            matched = host.endswith(suffix) and len(host) > len(suffix)
        else:
            matched = host == allowed
        if matched and parts.path.startswith(prefix):
            return host
    return None


# --------------------------------------------------------------------------
# The gate's CDP session: the page's frames and their navigations
# --------------------------------------------------------------------------

Send = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class MaskCdp:
    """The page watch's half of the gate's own CDP session to one target.

    `send(method, params)` sends a command on that session and returns its
    result; `on_event(method, params)` is given every event the session
    receives. The gate owns the transport (Chromium's pipe, §3.2); nothing
    here is reachable from the agent's CDP proxy. It reads frames and URLs
    only: no script is run in the page and nothing is painted.

    OUT-OF-PROCESS FRAMES. A cross-site iframe -- a reCAPTCHA, say -- runs in
    a renderer of its own, and a page's session does not hear its
    navigations: Chromium detaches it from the page's frame tree with reason
    `swap`. `install` asks Chromium to auto-attach such frames
    (`Target.setAutoAttach`, flat), and the page follower installs a CHILD
    `MaskCdp` (`child=True`) on each one's session, reporting into the same
    `MaskState`, where the swapped frame kept its place. A child's frames are
    never the top frame: a frame it names with no parent this gate knows is
    ignored.
    """

    def __init__(self, state: MaskState, send: Send, *, child: bool = False) -> None:
        self._state = state
        self._send = send
        self._child = child

    async def install(self) -> None:
        await self._send("Page.enable", {})
        # Out-of-process frames, each on a session of its own (class docstring).
        await self._send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False,
                                                  "flatten": True})
        tree = await self._send("Page.getFrameTree", {})
        if not self._child:
            self._state.session_up()
        stack = [tree.get("frameTree", {})]
        while stack:
            node = stack.pop()
            frame = node.get("frame")
            if isinstance(frame, dict) and isinstance(frame.get("id"), str):
                known = self._state.has_frame(frame["id"])
                if self._attach(frame["id"], frame.get("parentId")):
                    url = str(frame.get("url", ""))
                    if not known:
                        self._state.navigated(frame["id"], url)
                    else:
                        # Already heard of during this install (a navigation
                        # that committed): the tree is not a navigation (#1344).
                        self._state.listed(frame["id"], url)
            stack.extend(node.get("childFrames", []) or [])

    def _attach(self, frame_id: str, parent_id: Any) -> bool:
        """Tell the state of `frame_id`; False when it is not a frame this session may name."""
        parent = parent_id if isinstance(parent_id, str) and parent_id else None
        if parent is None and self._child:
            # A child session's root with no parent: the frame its parent's
            # session placed, or nothing this gate can place.
            return self._state.has_frame(frame_id)
        self._state.frame_attached(frame_id, parent)
        return True

    async def on_event(self, method: str, params: dict[str, Any]) -> None:
        state = self._state
        if method == "Page.frameAttached":
            frame_id, parent_id = params.get("frameId"), params.get("parentFrameId")
            if isinstance(frame_id, str):
                self._attach(frame_id, parent_id)
        elif method == "Page.frameDetached":
            # Into a renderer of its own (`swap`), it keeps its place (class docstring).
            if isinstance(params.get("frameId"), str) and params.get("reason") != "swap":
                state.frame_detached(params["frameId"])
        elif method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            if isinstance(frame.get("id"), str) and self._attach(frame["id"], frame.get("parentId")):
                state.navigated(frame["id"], str(frame.get("url", "")))
        elif method == "Page.navigatedWithinDocument":
            if isinstance(params.get("frameId"), str):
                state.url_changed(params["frameId"], str(params.get("url", "")))
