"""The live gate's mask: nothing sensitive leaves the pod (design §5.3, LB-3).

docs/design/live-browser.md §5.3. Between capture (`livegate.capture`) and
encoding, the gate paints opaque boxes into each frame over:

* every input whose type is or HAS EVER BEEN `password` on this page -- a
  "show password" toggle turns the field into `type=text` and the set is
  sticky -- and every input whose `autocomplete` names a credential;
* every block of visible text a `swarm_redaction` rule matches;
* the address bar, when the top frame's URL matches a rule.

WHERE THE RECTANGLES COME FROM. `observer.js`, installed by `MaskCdp` on the
gate's own CDP session (on Chromium's pipe, which the agent never sees) into
an isolated world in every frame -- a world page scripts and the agent's world
cannot see. It reports the sensitive fields, the visible text and the iframe
boxes through a binding that exists only in that world. The rules run HERE,
in Python, by importing `swarm_redaction`: not a JavaScript restatement of
them, the kind of second copy that has drifted in this repository before.

IT FAILS CLOSED. A rectangle report is always a little older than the frame
it is painted on, so:

* a frame is masked only with a report measured after the frame's last
  layout-changing event; otherwise the whole page area below the address bar
  is painted out (`Outcome.PAGE_BLANKED`). Scrolling flickers; that is the
  cost;
* with the CDP session down, or no report from the top frame since its last
  navigation, the whole frame is a blank "masking unavailable" frame
  (`Outcome.UNAVAILABLE`);
* every box is dilated by `DILATE_PX`.

Each frame's observer numbers its layout changes (its "epoch"), says so the
moment one happens (a `layout` message, sent from the mutation or event
callback, before the next paint), and measures in the next animation frame
(a `report` carrying the epoch it measured at). A report is fresh while no
later epoch has been announced.

PAGE CONTENT CANNOT HIDE THE ADDRESS BAR. Every box from a page is clipped to
the page area: a page claiming a password field over the whole screen blanks
its own area and nothing above it, because the address bar is part of the
anti-phishing design (§5.7). Only the URL rule paints the address bar.

NOTHING READ HERE IS LOGGED. Reports carry the page's text and the values of
its text fields, which while a person holds control are what they type. Log
lines from this module carry counts and outcomes only (§5.3.3).

GEOMETRY. The display holds one maximised Chromium window (§3.1), so the page
area is at the bottom-left, the browser's own UI the band above it, and its
size is the top frame's VISUAL VIEWPORT in CSS px (`visualViewport.width` x
`height`) times the page scale (`visualViewport.scale`) times the screen scale
(`scale`: `Device.screen_scale`, one screen pixel per CSS pixel on every
preset -- headed Chromium draws an emulated device scale factor at that size,
presets.py, §6.1). Page rectangles are layout-viewport client rectangles; the
visual viewport's offset and scale map them onto the screen.

WHY THE PAGE SCALE IS IN THE PAGE'S SIZE (QA12-01, 2026-10-13). A phone or
tablet preset is emulated with `mobile: true`, so Chromium zooms a page laid
out wider than the device -- 980 CSS px without a `<meta name=viewport>`, its
content's width when that overflows -- out to fit, as a phone does. Then
`innerWidth` x `innerHeight` are the visible area in CSS px (980 x 2123 on
iPhone 15 portrait) and `visualViewport.scale` is 393 / 980. Sizing the page
as `innerWidth * scale` called it 980 screen px on a 393 px frame and blanked
every frame grey: iPhone 15 and iPad portrait showed #555 for as long as the
page was up, while iPad landscape (1080, no zoom) and the desktop (never
`mobile`) drew. A page drawn at the window's own metrics before the device
override lands is still wider than the frame at scale 1, so it is still
unavailable. A child
frame's rectangles are offset by its owner element's content box, which the
parent frame's own report carries, linked by `MaskCdp.link`.
"""

from __future__ import annotations

import enum
import json
import logging
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from swarm_redaction import redact

from .capture import Frame

log = logging.getLogger(__name__)

#: Every box grows by this many screen pixels on each side (§5.3.2): a
#: report is measured a moment before the frame it is painted on.
DILATE_PX = 8

#: The opaque box over a sensitive area, and the whole-frame fill when the
#: mask cannot vouch for anything. Different, so a viewer can tell "hidden on
#: purpose" from "masking unavailable".
MASK_RGB = (0x22, 0x22, 0x22)
UNAVAILABLE_RGB = (0x55, 0x55, 0x55)

#: `autocomplete` tokens that name a credential (§5.3.1). Matched per token:
#: the attribute is a list ("section-pay billing cc-number").
SENSITIVE_AUTOCOMPLETE = frozenset({
    "current-password", "new-password", "one-time-code", "cc-number", "cc-csc",
})

#: The isolated world the observer runs in, and the binding only that world
#: has (`Runtime.addBinding` with `executionContextName`).
WORLD_NAME = "swarm-live-mask"
BINDING_NAME = "__swarmLiveMask"

#: The observer's function, in its own world, that names an iframe element
#: with the id its reports use for it (`MaskCdp.link`).
_OWNER_ID_FUNCTION = "function () { return globalThis.__swarmLiveMaskOwnerId(this); }"

#: A message larger than this is not parsed. The observer caps what it
#: reports well below it (`observer.js`, MAX_TEXT_CHARS), so one this large
#: is not a report this gate's observer wrote; the frame goes stale.
MAX_MESSAGE_CHARS = 4 * 1024 * 1024

#: Redaction verdicts kept per block text, so a page whose text does not
#: change is not re-scanned on every report.
_VERDICT_CACHE = 2048

_PROTOCOL = 1

_OBSERVER = Path(__file__).with_name("observer.js")


def observer_source() -> str:
    """The observer script, as installed into every frame's isolated world."""
    return _OBSERVER.read_text(encoding="utf-8")


class Outcome(enum.Enum):
    """What `MaskState.apply` did to a frame. The console's strip says which."""

    MASKED = "masked"
    PAGE_BLANKED = "page_blanked"
    UNAVAILABLE = "unavailable"


class MalformedReport(ValueError):
    """A message from an observer that is not one this gate understands."""


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Box:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def empty(self) -> bool:
        return self.x1 <= self.x0 or self.y1 <= self.y0

    def shift(self, dx: float, dy: float) -> "_Box":
        return _Box(self.x0 + dx, self.y0 + dy, self.x1 + dx, self.y1 + dy)

    def intersect(self, other: "_Box") -> "_Box":
        return _Box(max(self.x0, other.x0), max(self.y0, other.y0),
                    min(self.x1, other.x1), min(self.y1, other.y1))


@dataclass(frozen=True)
class _Field:
    id: int
    type: str
    autocomplete: str
    ever_password: bool
    box: _Box


@dataclass(frozen=True)
class _Text:
    text: str
    block: int
    box: _Box


@dataclass(frozen=True)
class _Report:
    doc: str
    epoch: int
    width: float
    height: float
    visual_x: float
    visual_y: float
    visual_scale: float
    #: The visual viewport's size in CSS px: `visualViewport.width` x
    #: `height`, or `innerWidth` x `innerHeight` (the same area, floored) from
    #: an observer without it.
    visual_width: float
    visual_height: float
    fields: tuple[_Field, ...]
    texts: tuple[_Text, ...]
    owners: dict[int, _Box]
    truncated: bool


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise MalformedReport("not a finite number")
    return float(value)


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedReport("not an integer")
    return value


def _string(value: Any, limit: int = 256) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise MalformedReport("not a short string")
    return value


def _rect(value: Any) -> _Box:
    if not isinstance(value, list) or len(value) != 4:
        raise MalformedReport("rectangle is not [x, y, w, h]")
    x, y, w, h = (_number(v) for v in value)
    if w < 0 or h < 0:
        raise MalformedReport("negative rectangle")
    return _Box(x, y, x + w, y + h)


def _records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise MalformedReport("not a list of records")
    return value


def _parse(payload: str) -> tuple[str, str, int, _Report | None]:
    """(kind, doc, epoch, report) of one observer message, or MalformedReport."""
    if not isinstance(payload, str) or len(payload) > MAX_MESSAGE_CHARS:
        raise MalformedReport("message too large")
    try:
        message = json.loads(payload)
    except ValueError:
        raise MalformedReport("not JSON") from None
    if not isinstance(message, dict) or message.get("v") != _PROTOCOL:
        raise MalformedReport("unknown protocol")
    kind = message.get("k")
    doc = _string(message.get("doc"))
    epoch = _integer(message.get("e"))
    if kind == "layout":
        return kind, doc, epoch, None
    if kind != "report":
        raise MalformedReport("unknown kind")
    viewport = message.get("vp")
    if not isinstance(viewport, list) or len(viewport) not in (5, 7):
        raise MalformedReport("viewport is not [w, h, vx, vy, vs, vw, vh]")
    width, height, visual_x, visual_y, visual_scale = (_number(v) for v in viewport[:5])
    visual_width, visual_height = (_number(v) for v in viewport[5:]) if len(viewport) == 7 else (width, height)
    if width < 0 or height < 0 or visual_width < 0 or visual_height < 0 or visual_scale <= 0:
        raise MalformedReport("viewport out of range")
    fields = tuple(
        _Field(
            id=_integer(f.get("id")),
            type=_string(f.get("type", ""), 64).lower(),
            autocomplete=_string(f.get("ac", ""), 1024).lower(),
            ever_password=f.get("ever") is True,
            box=_rect(f.get("r")),
        )
        for f in _records(message.get("f"))
    )
    texts = tuple(
        _Text(text=_string(t.get("s"), MAX_MESSAGE_CHARS), block=_integer(t.get("b")),
              box=_rect(t.get("r")))
        for t in _records(message.get("t"))
    )
    owners = {_integer(o.get("id")): _rect(o.get("r")) for o in _records(message.get("o"))}
    truncated = message.get("tr")
    if not isinstance(truncated, bool):
        raise MalformedReport("truncation flag missing")
    report = _Report(doc, epoch, width, height, visual_x, visual_y, visual_scale,
                     visual_width, visual_height, fields, texts, owners, truncated)
    return kind, doc, epoch, report


# --------------------------------------------------------------------------
# State per frame, and the mask
# --------------------------------------------------------------------------


@dataclass
class _FrameState:
    parent: str | None
    owner: int | None = None
    url: str | None = None
    doc: str | None = None
    #: The newest epoch the observer has announced for `doc`.
    epoch: int = 0
    report: _Report | None = None
    #: False once anything changed after `report` was measured.
    fresh: bool = False
    #: The id of every field ever reported as `password` on this frame's
    #: current document: the gate's half of the sticky set.
    ever_password: set[int] = field(default_factory=set)
    #: Documents this frame has navigated away from. A message still in
    #: flight from one of them is not a report on the page now shown.
    retired: set[str] = field(default_factory=set)

    def new_document(self, doc: str | None, epoch: int = 0) -> None:
        self.doc = doc
        self.epoch = epoch
        self.report = None
        self.fresh = False
        self.ever_password = set()


class MaskState:
    """What the gate knows about every frame of the page, and `apply`.

    Driven by `MaskCdp` (the CDP events and the observer's messages) and by
    the device lane (`set_scale`). Single-threaded: the gate calls it from its
    event loop.
    """

    def __init__(self, *, scale: float = 1.0) -> None:
        self._scale = _positive(scale)
        self._session = False
        self._frames: dict[str, _FrameState] = {}
        self._top: str | None = None
        self._verdicts: OrderedDict[str, bool] = OrderedDict()

    # -- what the gate tells it ------------------------------------------

    def session_up(self) -> None:
        """The gate's CDP session is attached. Nothing reported before is trusted.

        The documents are the same ones (the observer outlives a reconnect),
        so they are not retired: the observer's next report re-establishes
        each frame.
        """
        self._session = True
        for frame in self._frames.values():
            frame.report = None
            frame.fresh = False

    def session_down(self) -> None:
        self._session = False

    def set_scale(self, scale: float) -> None:
        """Screen pixels per CSS pixel changed (a device preset): every report is stale."""
        self._scale = _positive(scale)
        for frame in self._frames.values():
            frame.fresh = False

    def frame_attached(self, frame_id: str, parent_id: str | None) -> None:
        if frame_id in self._frames:
            return
        self._frames[frame_id] = _FrameState(parent=parent_id)
        if parent_id is None:
            self._top = frame_id

    def frame_linked(self, frame_id: str, owner_id: int) -> None:
        """`frame_id` is drawn in the box its parent's reports give `owner_id`."""
        frame = self._frames.get(frame_id)
        if frame is not None:
            frame.owner = owner_id

    def unlinked_children(self, parent_id: str) -> list[str]:
        return [fid for fid, f in self._frames.items()
                if f.parent == parent_id and f.owner is None]

    def frame_detached(self, frame_id: str) -> None:
        self._frames.pop(frame_id, None)
        for child in [fid for fid, f in self._frames.items() if f.parent == frame_id]:
            self.frame_detached(child)
        if self._top == frame_id:
            self._top = None

    def navigated(self, frame_id: str, url: str) -> None:
        """A cross-document navigation committed: no report covers the new page yet."""
        frame = self._frames.get(frame_id)
        if frame is None:
            return
        frame.url = url
        if frame.doc is not None:
            frame.retired.add(frame.doc)
        frame.new_document(None)

    def url_changed(self, frame_id: str, url: str) -> None:
        """A same-document navigation (history API, fragment): the content may differ."""
        frame = self._frames.get(frame_id)
        if frame is None:
            return
        frame.url = url
        frame.fresh = False

    def on_message(self, frame_id: str, payload: str) -> None:
        """One message from `frame_id`'s observer."""
        frame = self._frames.get(frame_id)
        if frame is None:
            return
        try:
            kind, doc, epoch, report = _parse(payload)
        except MalformedReport as exc:
            # The reason names a shape, never the payload's content.
            log.warning("live mask: malformed observer message (%s); frame is stale", exc)
            frame.fresh = False
            return
        if doc in frame.retired:
            return
        if doc != frame.doc:
            # A document this gate has not heard from: whatever was reported
            # before belongs to another page.
            frame.new_document(doc, epoch)
        if kind == "layout":
            if epoch > frame.epoch:
                frame.epoch = epoch
            frame.fresh = frame.report is not None and frame.report.epoch >= frame.epoch
            return
        assert report is not None
        if epoch < frame.epoch:
            return  # measured before a change already announced
        frame.epoch = epoch
        frame.report = report
        frame.fresh = True
        for f in report.fields:
            if f.type == "password":
                frame.ever_password.add(f.id)
        log.debug("live mask: report frame fields=%d texts=%d owners=%d truncated=%s",
                  len(report.fields), len(report.texts), len(report.owners), report.truncated)

    # -- the mask ----------------------------------------------------------

    def apply(self, frame: Frame) -> Outcome:
        """Paint every sensitive area of `frame`, in place, and say how."""
        top = self._frames.get(self._top) if self._top is not None else None
        if (not self._session or top is None or top.url is None or top.report is None):
            return self._unavailable(frame)
        report = top.report
        # The visible area times the page scale: the device's viewport however
        # the page is zoomed (GEOMETRY in the module docstring). Not
        # `innerWidth` alone, which is 980 on a phone showing a page zoomed out
        # to fit, and half the phone under a 2x pinch (QA12-01).
        k = report.visual_scale * self._scale
        page_w = report.visual_width * k
        page_h = report.visual_height * k
        if page_w <= 0 or page_h <= 0 or page_w > frame.width + 1 or page_h > frame.height + 1:
            return self._unavailable(frame)
        page = _Box(0, frame.height - page_h, page_w, frame.height)

        if self._matches(top.url):
            _paint(frame, _Box(0, 0, frame.width, page.y0))

        boxes: list[_Box] = []
        if not self._collect(self._top, top, page, boxes):
            _paint(frame, page)
            log.debug("live mask: page area blanked")
            return Outcome.PAGE_BLANKED
        for box in boxes:
            _paint(frame, _dilate(box).intersect(page))
        return Outcome.MASKED

    def _collect(self, frame_id: str, frame: _FrameState, page: _Box, out: list[_Box]) -> bool:
        """Screen boxes of every sensitive area of `frame_id` and its children.

        False when any frame that is drawn on screen cannot vouch for itself.
        """
        if frame.report is None or not frame.fresh or frame.report.truncated:
            return False
        to_screen = self._to_screen(frame_id, page)
        if to_screen is None:
            return False
        report = frame.report
        for f in report.fields:
            if (f.type == "password" or f.ever_password or f.id in frame.ever_password
                    or SENSITIVE_AUTOCOMPLETE.intersection(f.autocomplete.split())):
                out.append(to_screen(f.box))
        blocks: dict[int, list[_Text]] = {}
        for t in report.texts:
            blocks.setdefault(t.block, []).append(t)
        for items in blocks.values():
            if self._matches("".join(t.text for t in items)):
                out.extend(to_screen(t.box) for t in items)
        for child_id, child in self._frames.items():
            if child.parent != frame_id:
                continue
            if child.owner is None or child.owner not in report.owners:
                return False  # drawn somewhere this gate cannot place
            if to_screen(report.owners[child.owner]).intersect(page).empty:
                continue  # drawn nowhere on screen
            if not self._collect(child_id, child, page, out):
                return False
        return True

    def _to_screen(self, frame_id: str, page: _Box) -> Callable[[_Box], _Box] | None:
        """The map from `frame_id`'s client rectangles to screen pixels."""
        dx = dy = 0.0
        current = frame_id
        while True:
            frame = self._frames[current]
            if frame.parent is None:
                break
            parent = self._frames.get(frame.parent)
            if (parent is None or parent.report is None or frame.owner is None
                    or frame.owner not in parent.report.owners):
                return None
            owner = parent.report.owners[frame.owner]
            dx += owner.x0
            dy += owner.y0
            current = frame.parent
        top = self._frames[current].report
        if top is None:
            return None
        k = top.visual_scale * self._scale
        ox, oy = top.visual_x, top.visual_y

        def to_screen(box: _Box) -> _Box:
            return _Box(page.x0 + (box.x0 + dx - ox) * k, page.y0 + (box.y0 + dy - oy) * k,
                        page.x0 + (box.x1 + dx - ox) * k, page.y0 + (box.y1 + dy - oy) * k)

        return to_screen

    def _matches(self, text: str) -> bool:
        """Whether any `swarm_redaction` rule matches `text`."""
        verdict = self._verdicts.get(text)
        if verdict is None:
            verdict = redact(text).count > 0
            self._verdicts[text] = verdict
            if len(self._verdicts) > _VERDICT_CACHE:
                self._verdicts.popitem(last=False)
        else:
            self._verdicts.move_to_end(text)
        return verdict

    def _unavailable(self, frame: Frame) -> Outcome:
        frame.fill(0, 0, frame.width, frame.height, UNAVAILABLE_RGB)
        log.debug("live mask: masking unavailable")
        return Outcome.UNAVAILABLE


def _positive(scale: float) -> float:
    if not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
        raise ValueError("scale must be a positive number")
    return float(scale)


def _dilate(box: _Box) -> _Box:
    return _Box(box.x0 - DILATE_PX, box.y0 - DILATE_PX, box.x1 + DILATE_PX, box.y1 + DILATE_PX)


def _paint(frame: Frame, box: _Box) -> None:
    # Outward to whole pixels: a fractional edge is covered, never left showing.
    frame.fill(math.floor(box.x0), math.floor(box.y0), math.ceil(box.x1), math.ceil(box.y1),
               MASK_RGB)


# --------------------------------------------------------------------------
# The gate's CDP session: install the observer, route its messages
# --------------------------------------------------------------------------

Send = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class MaskCdp:
    """The mask's half of the gate's own CDP session to one target.

    `send(method, params)` sends a command on that session and returns its
    result; `on_event(method, params)` is given every event the session
    receives. The gate owns the transport (Chromium's pipe, §3.2); nothing
    here is reachable from the agent's CDP proxy.

    One observer world per frame: `Page.addScriptToEvaluateOnNewDocument`
    with a `worldName` and `runImmediately` creates the isolated world in
    every frame, present and future, and runs the observer there. A second
    world in the same frame would report a second document, so binding calls
    are accepted only from the newest world context of each frame.
    """

    def __init__(self, state: MaskState, send: Send) -> None:
        self._state = state
        self._send = send
        #: execution context id -> frame id, for observer worlds only.
        self._worlds: dict[int, str] = {}
        #: frame id -> its current observer world context.
        self._world_of: dict[str, int] = {}

    async def install(self) -> None:
        await self._send("Page.enable", {})
        await self._send("Runtime.enable", {})
        # The binding first, so the world never runs without it.
        await self._send("Runtime.addBinding", {"name": BINDING_NAME,
                                                "executionContextName": WORLD_NAME})
        await self._send("Page.addScriptToEvaluateOnNewDocument", {
            "source": observer_source(), "worldName": WORLD_NAME, "runImmediately": True})
        tree = await self._send("Page.getFrameTree", {})
        self._state.session_up()
        stack = [tree.get("frameTree", {})]
        while stack:
            node = stack.pop()
            frame = node.get("frame")
            if isinstance(frame, dict) and isinstance(frame.get("id"), str):
                self._state.frame_attached(frame["id"], frame.get("parentId"))
                self._state.navigated(frame["id"], str(frame.get("url", "")))
            stack.extend(node.get("childFrames", []) or [])

    async def on_event(self, method: str, params: dict[str, Any]) -> None:
        state = self._state
        if method == "Runtime.bindingCalled":
            if params.get("name") != BINDING_NAME:
                return
            context = params.get("executionContextId")
            frame_id = self._worlds.get(context) if isinstance(context, int) else None
            if frame_id is None or self._world_of.get(frame_id) != context:
                return  # not an observer world, or one superseded in its frame
            state.on_message(frame_id, params.get("payload", ""))
        elif method == "Runtime.executionContextCreated":
            context = params.get("context") or {}
            frame_id = (context.get("auxData") or {}).get("frameId")
            if context.get("name") != WORLD_NAME or not isinstance(frame_id, str):
                return
            self._worlds[context["id"]] = frame_id
            self._world_of[frame_id] = context["id"]
            # Children whose owner could not be resolved before this world existed.
            for child_id in state.unlinked_children(frame_id):
                await self.link(child_id, frame_id)
        elif method == "Runtime.executionContextDestroyed":
            context = params.get("executionContextId")
            frame_id = self._worlds.pop(context, None)
            if frame_id is not None and self._world_of.get(frame_id) == context:
                del self._world_of[frame_id]
        elif method == "Runtime.executionContextsCleared":
            self._worlds.clear()
            self._world_of.clear()
        elif method == "Page.frameAttached":
            frame_id, parent_id = params.get("frameId"), params.get("parentFrameId")
            if isinstance(frame_id, str):
                state.frame_attached(frame_id, parent_id if isinstance(parent_id, str) else None)
                if isinstance(parent_id, str):
                    await self.link(frame_id, parent_id)
        elif method == "Page.frameDetached":
            if isinstance(params.get("frameId"), str):
                state.frame_detached(params["frameId"])
        elif method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            if isinstance(frame.get("id"), str):
                state.frame_attached(frame["id"], frame.get("parentId"))
                state.navigated(frame["id"], str(frame.get("url", "")))
        elif method == "Page.navigatedWithinDocument":
            if isinstance(params.get("frameId"), str):
                state.url_changed(params["frameId"], str(params.get("url", "")))

    async def link(self, frame_id: str, parent_id: str) -> None:
        """Name `frame_id`'s iframe element with the id its parent's reports use.

        Any failure leaves the frame unlinked, which blanks the page area
        while it is on screen: fail closed.
        """
        context = self._world_of.get(parent_id)
        if context is None:
            return
        try:
            owner = await self._send("DOM.getFrameOwner", {"frameId": frame_id})
            node = await self._send("DOM.resolveNode", {"backendNodeId": owner["backendNodeId"],
                                                       "executionContextId": context})
            object_id = node["object"]["objectId"]
            try:
                result = await self._send("Runtime.callFunctionOn", {
                    "functionDeclaration": _OWNER_ID_FUNCTION, "objectId": object_id,
                    "returnByValue": True})
            finally:
                await self._send("Runtime.releaseObject", {"objectId": object_id})
            value = result["result"]["value"]
        except Exception as exc:  # noqa: BLE001 -- any failure is "unlinked"
            log.info("live mask: frame owner unresolved (%s)", type(exc).__name__)
            return
        if isinstance(value, int) and not isinstance(value, bool):
            self._state.frame_linked(frame_id, value)
