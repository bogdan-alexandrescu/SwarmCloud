"""The live gate's RFB server: masked frames out, the controller's input in (L3).

docs/design/live-browser.md §3.1 (the gate encodes the masked framebuffer as
RFB), §4.2 (control mode), §5.6 (keyboard and clipboard), §5.3.3 (keys never
logged) and §5.10 (controller input is rate limited at the gate). The console
runs noVNC; between it and this module are swarm-api and the relay, which
carry each viewer's RFB byte stream through the tunnel as its own connection
with its own id. This module speaks RFB 3.8 on one such stream.

ONLY MASKED FRAMES. `FrameHub.publish` takes a `MaskedFrame`, and the only way
to make one is `masked(frame, mask_state)`, which paints the frame with the
mask (§5.3) first. A raw `Frame` is refused with `TypeError`, so a capture
loop that forgot the mask fails at once instead of streaming.

INPUT ONLY FROM THE CONTROLLER, ONLY IN CONTROL MODE (§5.6). Every key,
pointer and paste message is read -- the stream must stay in step -- and then
discarded unless `ControlState.controller` is this connection's id. With no
controller (view mode) every connection's input is discarded; with one, every
other connection's is. Control changes hands through `ControlState.grant` and
`revoke`, which release every key and button the previous controller held.

THE CLIPBOARD IS ONE-WAY (§5.6, Q10). This server NEVER SENDS ServerCutText:
pod-to-console clipboard would hand a viewer whatever the agent copied,
unmasked, and the module has no code that writes that message. Console-to-pod
paste (`ClientCutText`) from the controller is typed as keys through the
injector, so it is masked and unlogged like typing; from anyone else it is
discarded.

RATE LIMIT (§5.10). The controller's injected events are limited to
`MAX_INPUT_EVENTS_PER_SECOND` by a token bucket; a paste costs one token per
character and is dropped whole if it does not fit -- half a password typed is
worse than none.

MOTION IS COALESCED, NEVER DROPPED (LB-CURSOR2; part of #1030). A PointerEvent
that changes no button is a move. Moves have their own limit,
`MAX_MOTION_PER_SECOND`, and one above it is not thrown away: it becomes the
held point, replaced by every later move, and is injected (and counted) as soon
as the limit allows, or superseded by the next event that is let through.
MEASURED 2026-10-13 (task_1d1785b21d5b4e1f894e): 20 moves, a click and two keys
reached the console's canvas and the audit counted two pointer events -- the
press and the release -- and the painted pointer sat at the click point. A
pointer that follows only clicks is one the person "sees but cannot move".

NOTHING READ FROM A VIEWER IS LOGGED (§5.3.3). Log lines carry the connection
id, counts and outcomes. No keysym, character, pasted text or coordinate is
ever an argument to a log call; `SessionCounts` is what the audit's "41 key
events, 9 pointer events" is made from.

AUTHENTICATION IS NOT HERE. The security type offered is None: the viewer was
authenticated by swarm-api on its own websocket (§5.2), and the relay admits
the stream only with a one-use ticket. A VNC password would be a second,
weaker secret on the same path.

ENCODINGS (STREAM-SMOOTH; part of #1030). The first of Tight, Zlib and Raw in
the viewer's own order of preference. noVNC 1.4.0 offers Tight and never
Zlib, so until this lane the console was sent Raw: 4 bytes a pixel, 5.2 MB per
update of a scrolling Desktop page (measured with `livegate.bench`). The
relay's recorder offers Zlib and Raw, and gets Zlib. Tight here is LOSSLESS,
because the owner asked for a sharp picture: a one-colour rectangle is sent as
Fill, anything else as Basic with the copy filter through zlib stream 0 --
never JPEG, never PNG, whatever quality level the viewer names. zlib runs at
the level the viewer names (the CompressLevel pseudo-encoding, which noVNC's
`compressionLevel` sets) and at `ZLIB_LEVEL` for a viewer that names none;
the benchmark in the pull request is why `ZLIB_LEVEL` is what it is. A resize
is announced with the DesktopSize pseudo-encoding, and a viewer that cannot
take one is disconnected (noVNC reconnects).

ONLY WHAT CHANGED, AND ONLY WHEN ASKED. Each viewer's last update is kept, as
the pixels it was sent: masked, with its pointer painted. A new update is
diffed against it in `TILE` x `TILE` tiles, and carries only the tiles that
changed -- adjacent changed tiles of a band merged into one rectangle,
trimmed to the rows that changed. The diff runs on the masked frame, so a
masked area whose contents change is still the mask's colour in it, and
nothing is ever sent from the unmasked screen. An update is sent only in
answer to a FramebufferUpdateRequest, from the newest frame at that moment,
so frames never queue for a slow viewer: it gets the latest when it asks.

NOTHING HEAVY ON THE EVENT LOOP. The diff and the encoding run on a thread
(`_ENCODE_POOL`), so the loop that reads the controller's keys and pointer
never waits behind a frame: input is read and injected while an update is
being encoded. zlib and the byte compares release or barely hold the GIL.
One encode per connection at a time, so a connection's zlib stream is only
ever touched by one thread.

INPUT NEVER HOLDS UP THE STREAM (STREAM-STALL; part of #1030). The read loop
only reads, checks and queues: an admitted key, pointer event or paste goes
on the connection's input queue, and `_inject_loop` injects it, in order, on
a task of its own. MEASURED 2026-10-14 (pod swarm-84cb2038aa704c829d48-5-lqs4k):
the owner's 188 s in control closed with `keys=0 pointer=87 updates=8
input_to_frame_ms p50=9777`, because the read loop awaited each injection --
on a touch preset `Input.dispatchTouchEvent`, which Chromium answers only once
the renderer has handled it -- and read nothing behind it: not the next
FramebufferUpdateRequest, so no update was sent, and not the keys, still
unread when the connection closed. A move waiting in the queue is replaced by
the next move, so a slow page gets the latest point and not every one; one
injection that takes longer than `INJECT_TIMEOUT_SECONDS`, or fails (the
page's session went), is counted as dropped and the next is injected. Control
is checked again when an event leaves the queue: nothing queued before a
revoke reaches the display after it.

A STALL IS SAID. A connection sent no update for `STALL_SECONDS` while the
published frame kept changing is logged as a warning, once until it is sent
one again, with whether a request is pending and how much input waits;
`stalls=` is in its close line.

INPUT TO FRAME. `SessionCounts.input_to_frame_ms` holds, per connection, the
time from a controller's input reaching the gate to the first update sent
from a frame captured after it that changed anything -- the latency the
person feels, inside the pod. Its p50 and p95 are logged when the connection
closes, with the update and byte counts: numbers, never content.

WHAT A VIEWER WAS SHOWN IS MEASURED (LIVE-MEASURE; part of #1030). Owner
decision 2026-10-13: Phase 1 of the live-browser plan is "measure", and
whether to keep warm pods is decided from these numbers. Each connection has
a `ViewerMetrics`, fed by the hub with every published frame and by the
session with every input and update, and logs it as ONE JSON line on the
`agent_worker.livegate.metrics` logger (`event=live_viewer_metrics`) when it
closes and every `METRICS_INTERVAL_SECONDS` while it is open. The gate writes
that logger's lines to stderr as bare JSON, so Cloud Logging parses them into
jsonPayload and scripts/live-report.sh can query them. The line holds frames
captured and changed, updates, the update rate in the seconds the page
changed, the longest a changed frame waited for an update, the time the frame
was the unavailable (#555) or page-blanked (#222) fill, input-to-frame p50
and p95 for the inputs a changed frame followed within
`INPUT_CHANGE_WINDOW_SECONDS` (a key that changed nothing is not timed to a
later, unrelated change), and the input counts. Numbers only, as above.

THE POINTER (LB-CURSOR; part of #1030). Xvfb's framebuffer never holds the X
pointer, and noVNC hides its own over the canvas, so without this the person
in control clicks blind. A viewer that lists the Cursor pseudo-encoding (-239,
RFC 6143 §7.8.1) is sent `pointer.ARROW` with its bitmask in its first update
and again after every DesktopSize, and noVNC draws it as its local pointer.
Every other viewer -- a watcher, the relay's recorder, a controller with no
local cursor -- gets the arrow painted where the pointer last was
(`FrameHub.pointer`: the controller's last PointerEvent, or the agent's last
mouse event, which the gate passes on from the CDP proxy), into its own copy
of the frame after the mask has painted it: never into the published frame,
never into Xvfb's mapping, and never under the mask. The controller with a
local cursor is not painted one too: it would trail its own pointer.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import statistics
import struct
import time
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .capture import Frame
from .inject import MAX_TYPED_CHARS, Injector
from .mask import UNAVAILABLE_RGB, MaskState, Outcome
from .pointer import HEIGHT as CURSOR_HEIGHT
from .pointer import HOTSPOT as CURSOR_HOTSPOT
from .pointer import WIDTH as CURSOR_WIDTH
from .pointer import PointerTracker, bitmask, paint, shape

log = logging.getLogger(__name__)
#: The per-connection metrics line's logger: bare JSON on stderr in the gate
#: (`__main__.configure_metrics_logging`), so Cloud Logging reads its fields.
metrics_log = logging.getLogger(__name__.rsplit(".", 1)[0] + ".metrics")

PROTOCOL_VERSION = b"RFB 003.008\n"
_SECURITY_NONE = 1

ENCODING_RAW = 0
ENCODING_COPY_RECT = 1
ENCODING_ZLIB = 6
ENCODING_TIGHT = 7
ENCODING_DESKTOP_SIZE = -223
ENCODING_CURSOR = -239
#: The CompressLevel pseudo-encodings: -256 is level 0 ... -247 is level 9.
_COMPRESS_LEVEL_0 = -256

#: The diff's tile, in pixels: the size TightVNC and TigerVNC use. A typed
#: character or a blinking caret is one tile; a full-width band of rows was
#: 1440 pixels wide.
TILE = 64
#: zlib's level for a viewer that names none (the relay's recorder), and the
#: level the console names (LiveBrowser.tsx `compressionLevel`). Chosen from
#: `livegate.bench` (the STREAM-SMOOTH pull request): level 1 encodes a
#: changed Desktop frame several times faster than the default 6 for a few
#: per cent more bytes, and the gate's CPU, not the link, is what bounds the
#: frame rate in control.
ZLIB_LEVEL = 1
_TIGHT_FILL = 0x80
_TIGHT_RESET_STREAM_0 = 0x01
#: A scroll is looked for only when at least this share of the frame changed,
#: and used only when at least `TILE` rows moved, in runs of at least
#: `_SCROLL_MIN_RUN` (a CopyRect is 16 bytes; checking a run is a compare per row).
_SCROLL_MIN_CHANGED = 0.25
_SCROLL_MIN_RUN = 8
#: Rows sampled from the changed area to find how far it moved, and how often
#: a row may repeat in the old frame and still be a witness to the shift.
_SCROLL_PROBES = 32
_SCROLL_REPEATS = 8
_SCROLL_CANDIDATES = 3
#: The right edge left out of the scroll match: a page's scrollbar thumb moves
#: against the scroll, so its rows never match whole. The edge goes through
#: the tile diff like any other change.
_SCROLL_EDGE = TILE
#: Tight's Basic rectangle under 12 bytes of pixels is sent as they are.
_TIGHT_MIN_TO_COMPRESS = 12

#: Where every connection's diff and encode run: off the event loop (module
#: docstring). A few threads, since a viewer and the recorder encode at once.
_ENCODE_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="live-rfb")
#: How many input-to-frame samples a connection keeps for its close log.
_LATENCY_SAMPLES = 512

#: An open connection's metrics line, this often (module docstring). The
#: close line covers what is left; a minute keeps a long session queryable
#: while it runs without a line per frame.
METRICS_INTERVAL_SECONDS = 60.0
#: A changed frame captured this long after an input is taken to be that
#: input's doing; an input no change followed within it changed nothing and is
#: not timed (module docstring). Two seconds is ten capture intervals watched
#: and longer than a page takes to echo a key, so a slow STREAM still counts
#: in full: the wait is measured to the update, not to the capture.
INPUT_CHANGE_WINDOW_SECONDS = 2.0
#: The input-to-frame histogram's upper bounds, in ms; the last bucket is
#: everything above. Per-connection percentiles cannot be pooled, a histogram
#: can, and 200 is a bound so the Phase-2 target (p95 < 200 ms) is read exactly.
INPUT_TO_FRAME_BOUNDS_MS = (50, 100, 150, 200, 300, 500, 1000, 2000, 5000)
#: `jsonPayload.event` of the metrics line: scripts/live-report.sh filters on it.
VIEWER_METRICS_EVENT = "live_viewer_metrics"

#: §5.10: controller input events per second, dropped above.
MAX_INPUT_EVENTS_PER_SECOND = 200
#: Moves (a PointerEvent that changes no button) per second. Above it the
#: latest move is held and sent when the limit allows: coalesced, never
#: dropped (module docstring). 60 is a display's refresh; noVNC itself sends
#: at most one move per 17 ms.
MAX_MOTION_PER_SECOND = 60
#: Admitted input waiting to be injected, at most; above it an event is
#: dropped (counted). Moves coalesce in the queue, so only keys, clicks and
#: pastes fill it: 512 is over two seconds of the input limit, reached only
#: when injection has stopped answering.
MAX_QUEUED_INPUT = 512
#: The longest one injection may take before it is given up (counted as
#: dropped) and the next is injected: a CDP touch event whose page never
#: answers must not hold every key behind it. Chromium acknowledges a touch
#: in milliseconds when the renderer is responsive.
INJECT_TIMEOUT_SECONDS = 5.0
#: No update for this long while the published frame changed is a stall,
#: logged as a warning (module docstring). The capture sends 10 frames a
#: second to a watcher, so 5 s is 50 frames nobody was sent.
STALL_SECONDS = 5.0

#: A ClientCutText longer than this is not read into memory; the connection
#: is closed. A paste longer than `MAX_TYPED_CHARS` is read and discarded.
MAX_CUT_TEXT_BYTES = 64 * 1024
_MAX_ENCODINGS = 256

_DESKTOP_NAME = b"swarm live browser"

# Client -> server message types (RFC 6143 §7.5).
_SET_PIXEL_FORMAT = 0
_SET_ENCODINGS = 2
_FRAMEBUFFER_UPDATE_REQUEST = 3
_KEY_EVENT = 4
_POINTER_EVENT = 5
_CLIENT_CUT_TEXT = 6

# Server -> client: FramebufferUpdate is the only message this server sends.
_FRAMEBUFFER_UPDATE = 0

_PIXEL_FORMAT = struct.Struct(">BBBBHHHBBB3x")


class RfbError(Exception):
    """The viewer sent something this server will not handle; the connection ends."""


class MaskedFrame:
    """A frame the mask has painted. Made only by `masked`."""

    __slots__ = ("frame", "outcome", "at")

    def __init__(self, frame: Frame, outcome: Outcome, *, _token: object) -> None:
        if _token is not _MASKED:
            raise TypeError("a MaskedFrame is made by rfb.masked(), which applies the mask")
        self.frame = frame
        self.outcome = outcome
        #: `time.monotonic()` when it was masked, just after the grab: an
        #: update from it can show an input that arrived before this.
        self.at = time.monotonic()


_MASKED = object()


def masked(frame: Frame, mask: MaskState) -> MaskedFrame:
    """Paint `frame` with the mask, in place, and mark it fit to leave the pod."""
    outcome = mask.apply(frame)
    return MaskedFrame(frame, outcome, _token=_MASKED)


class FrameHub:
    """The latest masked frame, and a wake-up for every session waiting on one."""

    def __init__(self, width: int = 1280, height: int = 800) -> None:
        # Until the first frame: the "masking unavailable" fill (§5.3.2).
        blank = Frame.blank(width, height, UNAVAILABLE_RGB)
        self._current = MaskedFrame(blank, Outcome.UNAVAILABLE, _token=_MASKED)
        self.version = 0
        #: How many published frames differed from the one before: what a
        #: stalled connection is measured against (module docstring).
        self.changes = 0
        self._waiters: set[asyncio.Event] = set()
        #: Every open connection's `ViewerMetrics`, told of each published frame.
        self._metrics: set[ViewerMetrics] = set()
        #: Where the pointer last was, in this frame's coordinates (module docstring).
        self.pointer = PointerTracker(on_move=self._pointer_moved)

    @property
    def current(self) -> MaskedFrame:
        return self._current

    def publish(self, frame: MaskedFrame) -> None:
        if not isinstance(frame, MaskedFrame):
            raise TypeError("only a MaskedFrame may be published: apply the mask with rfb.masked()")
        old, new = self._current.frame, frame.frame
        # One memcmp of the masked pixels: a static page, or the mask's
        # fill frame after frame, is not a change.
        changed = ((old.width, old.height, old.stride) != (new.width, new.height, new.stride)
                   or old.pixels != new.pixels)
        if changed:
            self.changes += 1
        self._current = frame
        if self._metrics:
            now = time.monotonic()
            for metrics in self._metrics:
                metrics.frame(now, changed=changed, outcome=frame.outcome, at=frame.at)
        self._wake_all()

    def _pointer_moved(self) -> None:
        # A move is news to a viewer even with no new capture: its mark moved.
        self._wake_all()

    def _wake_all(self) -> None:
        self.version += 1
        for event in self._waiters:
            event.set()

    def subscribe(self, event: asyncio.Event) -> None:
        self._waiters.add(event)

    def unsubscribe(self, event: asyncio.Event) -> None:
        self._waiters.discard(event)

    def measure(self, metrics: ViewerMetrics) -> None:
        self._metrics.add(metrics)

    def unmeasure(self, metrics: ViewerMetrics) -> None:
        self._metrics.discard(metrics)


class ControlState:
    """Which connection, if any, is the person in control (§4.2).

    Set by the gate when the relay passes on swarm-api's decision; this
    object does not decide who may control (§5.1), only enforces it.
    """

    def __init__(self, injector: Injector) -> None:
        self._injector = injector
        self._controller: str | None = None
        self._lock = asyncio.Lock()

    @property
    def controller(self) -> str | None:
        return self._controller

    def holds(self, conn_id: str) -> bool:
        return self._controller is not None and self._controller == conn_id

    async def grant(self, conn_id: str) -> None:
        async with self._lock:
            if self._controller == conn_id:
                return
            if self._controller is not None:
                await self._injector.release_all()
            self._controller = conn_id
            log.info("live rfb: control granted conn=%s", conn_id)

    async def revoke(self) -> None:
        async with self._lock:
            if self._controller is None:
                return
            try:
                await self._injector.release_all()
            finally:
                # Revoked even when a release could not be sent (the page's
                # session went with it): no input reaches the display from here.
                log.info("live rfb: control revoked conn=%s", self._controller)
                self._controller = None

    async def connection_closed(self, conn_id: str) -> None:
        """The controller's stream ended: let go of what it held.

        Control itself is swarm-api's to end (renewal, idle, §4.2); until it
        does, the connection id stays the controller and a reconnect under a
        new id is a viewer.
        """
        async with self._lock:
            if self._controller == conn_id:
                await self._injector.release_all()


@dataclass
class SessionCounts:
    """What the audit records about a connection's input: counts, never content."""

    key_events: int = 0
    pointer_events: int = 0
    pastes: int = 0
    dropped: int = 0
    updates: int = 0
    bytes_sent: int = 0
    #: Times the connection went `STALL_SECONDS` without an update while the frame changed.
    stalls: int = 0
    #: Input to the first changed frame after it, in ms (module docstring).
    input_to_frame_ms: deque[float] = field(default_factory=lambda: deque(maxlen=_LATENCY_SAMPLES))

    def latency(self, q: int) -> float | None:
        """The input-to-frame latency's `q`th percentile, or None with no sample."""
        samples = list(self.input_to_frame_ms)
        if len(samples) < 2:
            return samples[0] if samples else None
        return statistics.quantiles(samples, n=100, method="inclusive")[q - 1]


class ViewerMetrics:
    """What one connection was shown, measured (LIVE-MEASURE, module docstring).

    Fed with explicit `time.monotonic()` readings: `frame` by the hub for every
    published frame, `input` and `update` by the session. `snapshot` is the
    log line. Constant memory: per-second counters, not a list of frames.
    """

    def __init__(self, conn_id: str, *, now: float, outcome: Outcome) -> None:
        self.conn_id = conn_id
        self._opened = now
        self.frames_captured = 0
        self.frames_changed = 0
        self.updates = 0
        # The update rate while the page changed: whole seconds since the
        # connection opened, the one being counted and the closed ones' totals.
        self._second = 0
        self._second_changed = False
        self._second_updates = 0
        self._changing_seconds = 0
        self._updates_while_changing = 0
        # The latest changed frame: (its capture time, when it was published).
        self._last_change: tuple[float, float] | None = None
        # The oldest change this viewer has not been sent: (captured, published).
        self._owed: tuple[float, float] | None = None
        self._longest_gap = 0.0
        self._outcome = outcome
        self._outcome_since = now
        self._unavailable = 0.0
        self._page_blanked = 0.0
        # The input being timed, and the capture time of the change it caused.
        self._input_at: float | None = None
        self._input_change_at: float | None = None
        self.input_to_frame_ms: deque[float] = deque(maxlen=_LATENCY_SAMPLES)
        self._input_hist = [0] * (len(INPUT_TO_FRAME_BOUNDS_MS) + 1)
        self.inputs_unchanged = 0
        #: KeyEvents read while this connection held control: what `keys`
        #: (injected) is measured against, so a lost key shows.
        self.keys_read = 0

    def _tick(self, now: float) -> None:
        second = int(now - self._opened)
        if second != self._second:
            if self._second_changed:
                self._changing_seconds += 1
                self._updates_while_changing += self._second_updates
            self._second, self._second_changed, self._second_updates = second, False, 0

    def _expire_input(self, now: float) -> None:
        """An input no change followed within the window changed nothing."""
        if (self._input_at is not None and self._input_change_at is None
                and now - self._input_at > INPUT_CHANGE_WINDOW_SECONDS):
            self.inputs_unchanged += 1
            self._input_at = None

    def frame(self, now: float, *, changed: bool, outcome: Outcome, at: float) -> None:
        """The hub published a frame (`changed`: its pixels differ from the last)."""
        self._tick(now)
        self.frames_captured += 1
        if outcome is not self._outcome:
            self._close_outcome(now)
            self._outcome = outcome
        if not changed:
            return
        self.frames_changed += 1
        self._second_changed = True
        self._last_change = (at, now)
        if self._owed is None:
            self._owed = self._last_change
        self._expire_input(now)
        if self._input_at is not None and self._input_change_at is None and at >= self._input_at:
            self._input_change_at = at

    def input(self, now: float) -> None:
        """The controller's input reached the display."""
        self._expire_input(now)
        if self._input_at is None:
            self._input_at, self._input_change_at = now, None

    def update(self, now: float, *, sent_at: float) -> None:
        """An update was sent, rendered from the frame captured at `sent_at`."""
        self._tick(now)
        self.updates += 1
        self._second_updates += 1
        owed, last = self._owed, self._last_change
        if owed is not None and last is not None and owed[0] <= sent_at:
            # It carried the oldest change owed. One published while it
            # rendered is still owed, from when it was published.
            self._longest_gap = max(self._longest_gap, now - owed[1])
            self._owed = None if last[0] <= sent_at else last
        if self._input_change_at is not None and self._input_change_at <= sent_at:
            assert self._input_at is not None
            waited = (now - self._input_at) * 1000
            self.input_to_frame_ms.append(waited)
            self._input_hist[bisect.bisect_left(INPUT_TO_FRAME_BOUNDS_MS, waited)] += 1
            self._input_at = self._input_change_at = None

    def _close_outcome(self, now: float) -> None:
        held = now - self._outcome_since
        if self._outcome is Outcome.UNAVAILABLE:
            self._unavailable += held
        elif self._outcome is Outcome.PAGE_BLANKED:
            self._page_blanked += held
        self._outcome_since = now

    def snapshot(self, now: float, counts: SessionCounts, *, final: bool) -> dict[str, Any]:
        """The log line's fields as of `now`; reads the open second and gap
        without closing them, so a periodic line changes nothing."""
        self._expire_input(now)
        changing, updates = self._changing_seconds, self._updates_while_changing
        if self._second_changed:
            changing, updates = changing + 1, updates + self._second_updates
        gap = self._longest_gap
        if self._owed is not None:
            gap = max(gap, now - self._owed[1])
        unavailable, blanked = self._unavailable, self._page_blanked
        if self._outcome is Outcome.UNAVAILABLE:
            unavailable += now - self._outcome_since
        elif self._outcome is Outcome.PAGE_BLANKED:
            blanked += now - self._outcome_since
        samples = sorted(self.input_to_frame_ms)

        def pct(q: int) -> float | None:
            if not samples:
                return None
            if len(samples) == 1:
                return round(samples[0], 1)
            return round(statistics.quantiles(samples, n=100, method="inclusive")[q - 1], 1)

        return {
            "event": VIEWER_METRICS_EVENT,
            "conn": self.conn_id,
            # The relay's recorder connects as "rec-...", a person as "v-...".
            "role": "recorder" if self.conn_id.startswith("rec-") else "viewer",
            "final": final,
            "seconds": round(now - self._opened, 3),
            "frames_captured": self.frames_captured,
            "frames_changed": self.frames_changed,
            "updates": self.updates,
            "bytes": counts.bytes_sent,
            "changing_seconds": changing,
            "updates_while_changing": updates,
            "fps_while_changing": round(updates / changing, 2) if changing else None,
            "longest_gap_ms": round(gap * 1000, 1),
            "unavailable_ms": round(unavailable * 1000, 1),
            "page_blanked_ms": round(blanked * 1000, 1),
            "blank_ms": round((unavailable + blanked) * 1000, 1),
            "input_samples": len(samples),
            "inputs_unchanged": self.inputs_unchanged,
            "input_to_frame_p50_ms": pct(50),
            "input_to_frame_p95_ms": pct(95),
            "input_to_frame_bounds_ms": list(INPUT_TO_FRAME_BOUNDS_MS),
            "input_to_frame_hist": list(self._input_hist),
            "keys_read": self.keys_read,
            "keys": counts.key_events,
            "pointer": counts.pointer_events,
            "pastes": counts.pastes,
            "dropped": counts.dropped,
            "stalls": counts.stalls,
        }


def log_viewer_metrics(line: dict[str, Any]) -> None:
    """One metrics line, as a JSON object Cloud Logging reads as jsonPayload."""
    payload = {
        "severity": "INFO",
        "message": (f"live viewer metrics conn={line.get('conn')} final={line.get('final')} "
                    f"updates={line.get('updates')} fps_while_changing={line.get('fps_while_changing')}"),
        **line,
    }
    metrics_log.info(json.dumps(payload, sort_keys=True))


class _Bucket:
    def __init__(self, rate: int, clock: Callable[[], float]) -> None:
        self._rate = rate
        self._clock = clock
        self._tokens = float(rate)
        self._at = clock()

    def take(self, n: int = 1) -> bool:
        now = self._clock()
        self._tokens = min(float(self._rate), self._tokens + (now - self._at) * self._rate)
        self._at = now
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False


@dataclass(frozen=True)
class _Format:
    """A 32-bit true-colour pixel format: where each channel's byte sits."""

    big_endian: bool
    shifts: tuple[int, int, int]

    def offsets(self) -> tuple[int, int, int]:
        return tuple(  # type: ignore[return-value]
            (3 - s // 8) if self.big_endian else s // 8 for s in self.shifts)

    def pack(self) -> bytes:
        r, g, b = self.shifts
        return _PIXEL_FORMAT.pack(32, 24, int(self.big_endian), 1, 255, 255, 255, r, g, b)

    def frame(self, width: int, height: int, pixels: bytearray) -> Frame:
        """`pixels`, rows packed, as a Frame in this format, so it can be painted on."""
        r, g, b = self.shifts
        return Frame(width, height, width * 4, pixels, 0xFF << r, 0xFF << g, 0xFF << b,
                     little_endian=not self.big_endian)


def _shift(mask: int) -> int:
    return (mask & -mask).bit_length() - 1


def _frame_format(frame: Frame) -> _Format:
    return _Format(not frame.little_endian,
                   (_shift(frame.red_mask), _shift(frame.green_mask), _shift(frame.blue_mask)))


def _packed(frame: Frame) -> bytes:
    """A copy of the frame's pixels, rows packed (no stride padding)."""
    row = frame.width * 4
    if frame.stride == row:
        return bytes(memoryview(frame.pixels)[:row * frame.height])
    return b"".join(bytes(frame.pixels[y * frame.stride:y * frame.stride + row])
                    for y in range(frame.height))


def _reorder(packed: bytes, source: _Format, target: _Format) -> bytes:
    """Packed pixels in `source`'s byte layout, in `target`'s."""
    if source == target or source.offsets() == target.offsets():
        return packed
    out = bytearray(len(packed))
    for src, dst in zip(source.offsets(), target.offsets()):
        out[dst::4] = packed[src::4]
    return bytes(out)


def _rgb(packed: bytes, source: _Format) -> bytes:
    """Packed pixels as Tight's TPIXEL: three bytes, red, green, blue (RFC 6143 §7.7.6)."""
    r, g, b = source.offsets()
    out = bytearray(len(packed) // 4 * 3)
    out[0::3] = packed[r::4]
    out[1::3] = packed[g::4]
    out[2::3] = packed[b::4]
    return bytes(out)


def _compact_length(n: int) -> bytes:
    """Tight's 1-3 byte length: 7 bits a byte, low first, the high bit says more follow."""
    if n < 0x80:
        return bytes((n,))
    if n < 0x4000:
        return bytes((n & 0x7F | 0x80, n >> 7))
    return bytes((n & 0x7F | 0x80, n >> 7 & 0x7F | 0x80, n >> 14))


def changed_rects(old: bytes | None, new: bytes, width: int, height: int,
                  tile: int = TILE) -> list[tuple[int, int, int, int]]:
    """The rectangles of `new` that differ from `old`, both packed 32-bit pixels.

    Per band of `tile` rows: the tiles that changed, each run of adjacent ones
    merged into one rectangle as tall as the rows that changed in it. With no
    `old`, every band, whole. A band that did not change costs one compare.
    """
    row = width * 4
    out: list[tuple[int, int, int, int]] = []
    for by in range(0, height, tile):
        bh = min(tile, height - by)
        lo, hi = by * row, (by + bh) * row
        if old is None:
            out.append((0, by, width, bh))
            continue
        if old[lo:hi] == new[lo:hi]:
            continue
        rows = [y for y in range(by, by + bh) if old[y * row:(y + 1) * row] != new[y * row:(y + 1) * row]]
        run: list[int] | None = None  # x0, x1, top, bottom (inclusive)
        for tx in range(0, width, tile):
            a, b = tx * 4, min(width, tx + tile) * 4
            top = next((y for y in rows if old[y * row + a:y * row + b] != new[y * row + a:y * row + b]), None)
            if top is None:
                if run is not None:
                    out.append((run[0], run[2], run[1] - run[0], run[3] - run[2] + 1))
                    run = None
                continue
            bottom = next(y for y in reversed(rows)
                          if old[y * row + a:y * row + b] != new[y * row + a:y * row + b])
            if run is None:
                run = [tx, b // 4, top, bottom]
            else:
                run[1], run[2], run[3] = b // 4, min(run[2], top), max(run[3], bottom)
        if run is not None:
            out.append((run[0], run[2], run[1] - run[0], run[3] - run[2] + 1))
    return out


def find_scroll(old: bytes, new: bytes, width: int, height: int,
                changed: list[tuple[int, int, int, int]]) -> tuple[int, list[tuple[int, int]]] | None:
    """`(dy, runs)` when each run of rows `top..bottom-1` of `new` is rows `top+dy..` of `old`.

    Compared over every column but the right `_SCROLL_EDGE`, byte for byte:
    a row is only counted as moved if it is exactly the old row, so the
    CopyRects built from this leave the viewer holding exactly `new` there.
    Several runs, because what does not move with the page -- a fixed header,
    the mask over a field -- splits the area that does. The runs come in the
    order they can be copied in: each one's source is rows no earlier copy
    wrote. None when less than `_SCROLL_MIN_CHANGED` of the frame changed or
    fewer than `TILE` rows moved.
    """
    span = (width - _SCROLL_EDGE) * 4
    if span <= 0 or sum(w * h for _, _, w, h in changed) < _SCROLL_MIN_CHANGED * width * height:
        return None
    row = width * 4
    lo = min(y for _, y, _, _ in changed)
    hi = max(y + h for _, y, _, h in changed)

    def seg(buf: bytes, y: int) -> bytes:
        return buf[y * row:y * row + span]

    # Where the changed area's old rows are. A uniform row (a margin, a blank
    # line) is everywhere and says nothing about the shift; a row seen many
    # times says little.
    where: dict[bytes, list[int]] = {}
    for y in range(lo, hi):
        key = seg(old, y)
        if key[:4] * (span // 4) == key:
            continue
        ys = where.setdefault(key, [])
        if len(ys) <= _SCROLL_REPEATS:
            ys.append(y)
    votes: dict[int, int] = {}
    step = max(1, (hi - lo) // _SCROLL_PROBES)
    for y in range(lo, hi, step):
        ys = where.get(seg(new, y), ())
        if len(ys) <= _SCROLL_REPEATS:
            for at in ys:
                if at != y:
                    votes[at - y] = votes.get(at - y, 0) + 1
    if not votes:
        return None

    def moved_runs(dy: int) -> list[tuple[int, int]]:
        runs: list[tuple[int, int]] = []
        start: int | None = None
        first, last = max(lo, -dy), min(hi, height - dy)
        for y in range(first, last + 1):
            moved = y < last and seg(new, y) == seg(old, y + dy)
            if moved and start is None:
                start = y
            elif not moved and start is not None:
                if y - start >= _SCROLL_MIN_RUN:
                    runs.append((start, y))
                start = None
        return runs

    # A row that repeats a few times votes for a few shifts, so the top
    # candidates are each checked, and the one that moves the most rows wins.
    best: tuple[int, list[tuple[int, int]]] | None = None
    for dy in sorted(votes, key=lambda d: -votes[d])[:_SCROLL_CANDIDATES]:
        runs = moved_runs(dy)
        if best is None or sum(b - a for a, b in runs) > sum(b - a for a, b in best[1]):
            best = (dy, runs)
    assert best is not None
    dy, runs = best
    if sum(b - a for a, b in runs) < TILE:
        return None
    # Content moving up (dy > 0) reads from below: copy top run first. Moving
    # down, bottom first. Either way no source row has been overwritten yet.
    return dy, runs if dy > 0 else runs[::-1]


def _scrolled(old: bytes, width: int, dy: int, runs: list[tuple[int, int]]) -> bytes:
    """`old` as the viewer holds it after the CopyRects of `find_scroll`'s answer.

    Whole rows, the right edge too: the edge was left out of the match, not
    the move, and whatever of it does not match (the scrollbar's thumb) is
    what the diff against this sends in the same update.
    """
    row = width * 4
    out = bytearray(old)
    for top, bottom in runs:
        out[top * row:bottom * row] = old[(top + dy) * row:(bottom + dy) * row]
    return bytes(out)


@dataclass(frozen=True)
class _Job:
    """One update to render, as the event loop saw the connection when it asked."""

    frame: Frame
    target: _Format
    encoding: int
    level: int
    copy_rect: bool
    pointer: tuple[int, int] | None
    incremental: bool
    resend: bool
    new_streams: bool


class RfbSession:
    """One viewer's RFB connection, through the tunnel."""

    def __init__(self, conn_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 hub: FrameHub, control: ControlState, injector: Injector, *,
                 max_events_per_second: int = MAX_INPUT_EVENTS_PER_SECOND,
                 max_motion_per_second: int = MAX_MOTION_PER_SECOND,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.conn_id = conn_id
        self._reader = reader
        self._writer = writer
        self._hub = hub
        self._control = control
        self._injector = injector
        self._bucket = _Bucket(max_events_per_second, clock)
        self._motion = _Bucket(max_motion_per_second, clock)
        self._motion_gap = 1.0 / max_motion_per_second
        self._buttons = 0  # the button mask last injected for this connection
        self._held: tuple[int, int] | None = None  # a coalesced move waiting for the limit
        self._held_task: asyncio.Task[None] | None = None
        # Admitted input, in order, for `_inject_loop` (module docstring).
        self._inputs: deque[tuple[Any, ...]] = deque()
        self._inputs_ready = asyncio.Event()
        self._injecting = False  # an injection is awaiting its answer: the injector is behind
        self.counts = SessionCounts()
        self.metrics = ViewerMetrics(conn_id, now=time.monotonic(), outcome=hub.current.outcome)
        self._format: _Format | None = None  # the viewer's; None until ServerInit
        self._encodings: tuple[int, ...] = (ENCODING_RAW,)
        self._cursor_due = False  # send the cursor shape with the next update
        self._size = (0, 0)
        # Set on the loop, read into the next `_Job`: resend everything (the
        # pixel format or the size changed), start new zlib streams (the
        # encodings changed).
        self._resend = False
        self._new_streams = False
        # Touched only by `_render`, one at a time, on `_ENCODE_POOL`.
        self._zlib: zlib._Compress | None = None  # the Zlib encoding's one stream
        self._tight: zlib._Compress | None = None  # Tight's stream 0
        self._last: bytes | None = None  # what this viewer has: packed, in the frame's format
        # When the controller's oldest input not yet seen in an update reached the gate.
        self._input_at: float | None = None
        self._sent_version = -1
        self._pending: bool | None = None  # None: no request; else incremental?
        self._requested_at: float | None = None  # when the last request was read
        self._wake = asyncio.Event()

    # -- lifecycle --------------------------------------------------------

    async def serve(self) -> None:
        """Run the connection until either side ends it."""
        self._hub.subscribe(self._wake)
        self._hub.measure(self.metrics)
        loops: list[asyncio.Task[None]] = []
        try:
            await self._handshake()
            loops = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._write_loop()),
                     asyncio.create_task(self._inject_loop()), asyncio.create_task(self._watch_stall()),
                     asyncio.create_task(self._report_metrics())]
            done, _ = await asyncio.wait(loops, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, (asyncio.IncompleteReadError, ConnectionError)):
                    raise exc
        except RfbError as exc:
            log.info("live rfb: conn=%s refused: %s", self.conn_id, exc)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._held = None
            self._inputs.clear()
            tasks = (*loops, self._held_task)
            for task in tasks:
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in tasks if t is not None), return_exceptions=True)
            self._hub.unsubscribe(self._wake)
            self._hub.unmeasure(self.metrics)
            await self._control.connection_closed(self.conn_id)
            self._writer.close()
            c = self.counts
            p50, p95 = c.latency(50), c.latency(95)
            log.info("live rfb: conn=%s closed keys=%d pointer=%d pastes=%d dropped=%d updates=%d "
                     "bytes=%d stalls=%d input_to_frame_ms p50=%s p95=%s", self.conn_id, c.key_events,
                     c.pointer_events, c.pastes, c.dropped, c.updates, c.bytes_sent, c.stalls,
                     "-" if p50 is None else f"{p50:.0f}", "-" if p95 is None else f"{p95:.0f}")
            log_viewer_metrics(self.metrics.snapshot(time.monotonic(), c, final=True))

    async def _report_metrics(self) -> None:
        """The metrics line every `METRICS_INTERVAL_SECONDS` while the connection is open."""
        while True:
            await asyncio.sleep(METRICS_INTERVAL_SECONDS)
            log_viewer_metrics(self.metrics.snapshot(time.monotonic(), self.counts, final=False))

    async def _handshake(self) -> None:
        self._writer.write(PROTOCOL_VERSION)
        await self._writer.drain()
        version = await self._reader.readexactly(12)
        if version != PROTOCOL_VERSION:
            raise RfbError("protocol version is not 3.8")
        self._writer.write(bytes([1, _SECURITY_NONE]))
        await self._writer.drain()
        chosen = (await self._reader.readexactly(1))[0]
        if chosen != _SECURITY_NONE:
            self._writer.write(struct.pack(">I", 1))
            await self._writer.drain()
            raise RfbError("unsupported security type")
        self._writer.write(struct.pack(">I", 0))
        # DRAIN BEFORE THE NEXT READ. The tunnel's writer sends nothing on
        # write(), only on drain(), and the viewer sends ClientInit only after
        # it has read this SecurityResult: without the drain both sides wait
        # and the Browser tab stays on "Connecting" (measured on SwarmCloud
        # 2026-10-12, test_live_rfb_tunnel_flush.py).
        await self._writer.drain()
        await self._reader.readexactly(1)  # ClientInit: shared or not, it is shared
        frame = self._hub.current.frame
        native = _frame_format(frame)
        self._format = native
        self._size = (frame.width, frame.height)
        self._writer.write(struct.pack(">HH", frame.width, frame.height) + native.pack()
                           + struct.pack(">I", len(_DESKTOP_NAME)) + _DESKTOP_NAME)
        await self._writer.drain()
        log.info("live rfb: conn=%s open", self.conn_id)

    # -- client messages --------------------------------------------------

    async def _read_loop(self) -> None:
        read = self._reader.readexactly
        while True:
            kind = (await read(1))[0]
            if kind == _SET_PIXEL_FORMAT:
                body = await read(19)
                self._set_pixel_format(body[3:])
            elif kind == _SET_ENCODINGS:
                (count,) = struct.unpack(">xH", await read(3))
                if count > _MAX_ENCODINGS:
                    raise RfbError("too many encodings")
                self._encodings = struct.unpack(f">{count}i", await read(4 * count))
                self._new_streams = True
                self._cursor_due = ENCODING_CURSOR in self._encodings
            elif kind == _FRAMEBUFFER_UPDATE_REQUEST:
                incremental = (await read(9))[0] != 0
                # A full request wins over an incremental one still pending.
                self._pending = incremental if self._pending is None else (self._pending and incremental)
                self._requested_at = time.monotonic()
                self._wake.set()
            elif kind == _KEY_EVENT:
                down, keysym = struct.unpack(">BxxI", await read(7))
                if self._control.holds(self.conn_id):
                    self.metrics.keys_read += 1
                if self._admit(1):
                    self._input()
                    self._queue(("key", keysym, bool(down)))
            elif kind == _POINTER_EVENT:
                buttons, x, y = struct.unpack(">BHH", await read(5))
                self._pointer(buttons, x, y)
            elif kind == _CLIENT_CUT_TEXT:
                (length,) = struct.unpack(">xxxI", await read(7))
                if length > MAX_CUT_TEXT_BYTES:
                    raise RfbError("cut text too long")
                text = (await read(length)).decode("latin-1")
                self._paste(text)
            else:
                raise RfbError(f"unknown client message type {kind}")

    def _admit(self, cost: int) -> bool:
        """May this connection's input reach the display now? Counts a drop if not."""
        if not self._control.holds(self.conn_id) or not self._bucket.take(cost):
            self.counts.dropped += 1
            return False
        return True

    def _pointer(self, buttons: int, x: int, y: int) -> None:
        """One PointerEvent: a button change through the input limit, a move
        through the motion limit, coalesced above it (module docstring)."""
        if not self._control.holds(self.conn_id):
            self.counts.dropped += 1
            return
        if buttons == self._buttons:
            if not self._motion.take(1):
                self._held = (x, y)
                if self._held_task is None:
                    self._held_task = asyncio.create_task(self._send_held())
                return
        elif not self._admit(1):
            return
        self._held = None  # this event is newer than any held move
        self._inject_pointer(buttons, x, y)

    def _input(self) -> None:
        """The controller's input reached the display: time it to the frame that shows it."""
        if self._input_at is None:
            self._input_at = time.monotonic()
        self.metrics.input(time.monotonic())

    def _inject_pointer(self, buttons: int, x: int, y: int) -> None:
        self._input()
        w, h = self._hub.current.frame.width, self._hub.current.frame.height
        x, y = min(x, w - 1), min(y, h - 1)
        kind = "move" if buttons == self._buttons else "pointer"
        self._buttons = buttons
        self._hub.pointer.move(x, y)
        self._queue((kind, buttons, x, y))

    async def _send_held(self) -> None:
        """The held move, once the motion limit has room for it."""
        try:
            while self._held is not None:
                await asyncio.sleep(self._motion_gap)
                if self._held is None:
                    return
                if not self._control.holds(self.conn_id):
                    self._held = None
                    self.counts.dropped += 1
                    return
                if self._motion.take(1):
                    (x, y), self._held = self._held, None
                    self._inject_pointer(self._buttons, x, y)
        finally:
            self._held_task = None

    def _paste(self, text: str) -> None:
        if not self._control.holds(self.conn_id) or not text or len(text) > MAX_TYPED_CHARS:
            self.counts.dropped += 1
            return
        if not self._admit(len(text)):
            return
        self._input()
        self._queue(("paste", text))

    # -- injection, off the read loop ---------------------------------------

    def _queue(self, event: tuple[Any, ...]) -> None:
        """Hand an admitted event to `_inject_loop`. While an injection is
        waiting on its answer, a move replaces a move still queued: a slow page
        gets the latest point. An injector that keeps up gets every point the
        motion limit let through."""
        inputs = self._inputs
        if event[0] == "move" and self._injecting and inputs and inputs[-1][0] == "move":
            inputs[-1] = event
        elif len(inputs) >= MAX_QUEUED_INPUT:
            self.counts.dropped += 1
            return
        else:
            inputs.append(event)
        self._inputs_ready.set()

    async def _inject_loop(self) -> None:
        """Inject the queued input in order, each bounded by `INJECT_TIMEOUT_SECONDS` (module docstring)."""
        while True:
            await self._inputs_ready.wait()
            self._inputs_ready.clear()
            while self._inputs:
                event = self._inputs.popleft()
                if not self._control.holds(self.conn_id):
                    # Control went while it waited: it reaches nothing now.
                    self.counts.dropped += 1
                    continue
                try:
                    # A deadline, not `wait_for`: no task per event, so an
                    # injector that answers at once drains the queue in one go.
                    self._injecting = True
                    async with asyncio.timeout(INJECT_TIMEOUT_SECONDS):
                        injected = await self._inject(event)
                except TimeoutError:
                    injected = False
                    log.warning("live rfb: conn=%s an injection took over %.0fs; given up",
                                self.conn_id, INJECT_TIMEOUT_SECONDS)
                except Exception as exc:  # noqa: BLE001 -- the page or display refused it; the next may land
                    injected = False
                    log.info("live rfb: conn=%s an injection failed (%s)", self.conn_id, type(exc).__name__)
                finally:
                    self._injecting = False
                if not injected:
                    self.counts.dropped += 1

    async def _inject(self, event: tuple[Any, ...]) -> bool:
        """One event into the display, counted once the injector has taken it,
        so an event that times out or raises is counted as dropped and nothing
        else. False if it was refused."""
        kind = event[0]
        if kind == "key":
            await self._injector.key(event[1], event[2])
            self.counts.key_events += 1
            return True
        if kind == "paste":
            if not await self._injector.type_text(event[1]):
                return False
            self.counts.pastes += 1
            return True
        await self._injector.pointer_event(event[1], event[2], event[3])
        self.counts.pointer_events += 1
        return True

    def _set_pixel_format(self, body: bytes) -> None:
        bpp, _depth, big, true_colour, rmax, gmax, bmax, rs, gs, bs = _PIXEL_FORMAT.unpack(body)
        shifts = (rs, gs, bs)
        if (bpp != 32 or not true_colour or (rmax, gmax, bmax) != (255, 255, 255)
                or any(s % 8 or s > 24 for s in shifts) or len(set(shifts)) != 3):
            raise RfbError("unsupported pixel format")
        self._format = _Format(bool(big), shifts)
        self._resend = True
        # The shape was in the old format: send it again in the new one.
        self._cursor_due = ENCODING_CURSOR in self._encodings

    # -- server messages --------------------------------------------------

    async def _write_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._pending is None:
                continue
            incremental = self._pending
            if incremental and self._hub.version == self._sent_version:
                continue  # nothing new; answer when the next frame is published
            self._pending = None
            await self._send_update(incremental)

    async def _send_update(self, incremental: bool) -> None:
        current = self._hub.current
        frame = current.frame
        self._sent_version = self._hub.version
        if (frame.width, frame.height) != self._size:
            if ENCODING_DESKTOP_SIZE not in self._encodings:
                raise RfbError("display resized and the viewer cannot take DesktopSize")
            self._size = (frame.width, frame.height)
            self._resend = True
            # §7.8.1 leaves the cursor to the client across a resize; noVNC
            # keeps it, but a viewer that drops it would be blind again.
            self._cursor_due = ENCODING_CURSOR in self._encodings
            self._write_update([struct.pack(">HHHHi", 0, 0, frame.width, frame.height,
                                            ENCODING_DESKTOP_SIZE)])
            await self._writer.drain()
            # The viewer requests again at the new size.
            return
        assert self._format is not None
        # Everything the render needs from the connection, read here on the
        # loop; the render itself runs on a thread (module docstring).
        job = _Job(frame=frame, target=self._format, encoding=self._encoding(),
                   level=self._level(), copy_rect=ENCODING_COPY_RECT in self._encodings,
                   incremental=incremental,
                   # This viewer's own copy gets the pointer, after the mask (module docstring).
                   pointer=None if self._local_pointer() else self._hub.pointer.position,
                   resend=self._resend, new_streams=self._new_streams)
        self._resend = self._new_streams = False
        loop = asyncio.get_running_loop()
        rects = await loop.run_in_executor(_ENCODE_POOL, self._render, job)
        changed = bool(rects)
        if self._cursor_due:
            self._cursor_due = False
            rects.insert(0, self._cursor_rect())
        elif not rects:
            # Published but identical (a static page): nothing to send,
            # so the request stays pending for the next frame -- unless the
            # viewer asked again while this rendered, and that request stands.
            if self._pending is None:
                self._pending = True
            return
        self._write_update(rects)
        await self._writer.drain()
        self.metrics.update(time.monotonic(), sent_at=current.at)
        if changed and self._input_at is not None and current.at >= self._input_at:
            self.counts.input_to_frame_ms.append((time.monotonic() - self._input_at) * 1000)
            self._input_at = None

    async def _watch_stall(self) -> None:
        """Warn when the frame changed for `STALL_SECONDS` and this connection was sent nothing."""
        changes, updates, stalled = self._hub.changes, self.counts.updates, False
        while True:
            await asyncio.sleep(STALL_SECONDS)
            now_changes, now_updates = self._hub.changes, self.counts.updates
            if now_updates != updates:
                if stalled:
                    log.info("live rfb: conn=%s updates resumed", self.conn_id)
                stalled = False
            elif now_changes != changes and not stalled:
                stalled = True
                self.counts.stalls += 1
                since = (None if self._requested_at is None
                         else time.monotonic() - self._requested_at)
                log.warning("live rfb: conn=%s stalled: no update in %.0fs while the frame changed "
                            "request_pending=%s last_request_s=%s input_queued=%d", self.conn_id,
                            STALL_SECONDS, self._pending is not None,
                            "-" if since is None else f"{since:.1f}", len(self._inputs))
            changes, updates = now_changes, now_updates

    def _write_update(self, rects: list[bytes]) -> None:
        message = struct.pack(">BxH", _FRAMEBUFFER_UPDATE, len(rects)) + b"".join(rects)
        self._writer.write(message)
        self.counts.updates += 1
        self.counts.bytes_sent += len(message)

    def _render(self, job: _Job) -> list[bytes]:
        """The update's rectangles: diffed against what the viewer has, encoded.

        Runs on `_ENCODE_POOL`, never on the loop; one at a time per connection.
        """
        if job.new_streams:
            self._zlib = self._tight = None
        if job.resend:
            self._last = None
        frame = job.frame
        width, height = frame.width, frame.height
        native = _frame_format(frame)
        pixels = _packed(frame)
        if job.pointer is not None:
            painted = bytearray(pixels)
            paint(native.frame(width, height, painted), *job.pointer)
            pixels = bytes(painted)
        old = self._last if job.incremental else None
        if old is not None and len(old) != len(pixels):
            old = None
        self._last = pixels
        rects = changed_rects(old, pixels, width, height)
        out: list[bytes] = []
        scroll = find_scroll(old, pixels, width, height, rects) if job.copy_rect and old else None
        if scroll is not None:
            # A scroll: the viewer moves what it already has (CopyRect, first,
            # so it copies from the picture before this update), and is sent
            # only what the move did not bring -- the rows scrolled in, the
            # scrollbar, anything that changed as well.
            dy, runs = scroll
            for top, bottom in runs:
                out.append(struct.pack(">HHHHiHH", 0, top, width, bottom - top,
                                       ENCODING_COPY_RECT, 0, top + dy))
            rects = changed_rects(_scrolled(old, width, dy, runs), pixels, width, height)
        if job.encoding == ENCODING_TIGHT:
            return out + [piece for x, y, w, h in rects
                          for piece in self._tight_pieces(job, native, pixels, width, x, y, w, h)]
        return out + [self._encode_rect(job, native, pixels, width, *rect) for rect in rects]

    def _tight_pieces(self, job: _Job, native: _Format, pixels: bytes, width: int,
                      x: int, y: int, w: int, h: int) -> list[bytes]:
        """A rectangle as Tight: its one-colour tiles as Fill, the runs between them as Basic.

        A page's margins and backgrounds are most of a frame, and a Fill is
        four bytes where zlib would still have to read the tile.
        """
        row = width * 4
        runs: list[list[object]] = []  # [x0, x1, colour bytes or None]
        for tx in range(x, x + w, TILE):
            a, b = tx * 4, min(x + w, tx + TILE) * 4
            first = pixels[y * row + a:y * row + a + 4]
            line = first * ((b - a) // 4)
            solid = all(pixels[at:at + b - a] == line for at in range(y * row + a, (y + h) * row, row))
            colour = first if solid else None
            if runs and runs[-1][1] == tx and runs[-1][2] == colour:
                runs[-1][1] = b // 4
            else:
                runs.append([tx, b // 4, colour])
        out = []
        for x0, x1, colour in runs:
            assert isinstance(x0, int) and isinstance(x1, int)
            header = struct.pack(">HHHHi", x0, y, x1 - x0, h, ENCODING_TIGHT)
            if isinstance(colour, bytes):
                out.append(header + bytes((_TIGHT_FILL,)) + _rgb(colour, native))
            else:
                out.append(self._encode_rect(job, native, pixels, width, x0, y, x1 - x0, h))
        return out

    def _encode_rect(self, job: _Job, native: _Format, pixels: bytes, width: int,
                     x: int, y: int, w: int, h: int) -> bytes:
        row = width * 4
        if x == 0 and w == width:
            data = pixels[y * row:(y + h) * row]
        else:
            data = b"".join(pixels[at:at + w * 4] for at in range(y * row + x * 4, (y + h) * row, row))
        header = struct.pack(">HHHHi", x, y, w, h, job.encoding)
        if job.encoding == ENCODING_TIGHT:
            return header + self._tight_rect(_rgb(data, native), w * h, job.level)
        data = _reorder(data, native, job.target)
        if job.encoding == ENCODING_ZLIB:
            if self._zlib is None:
                self._zlib = zlib.compressobj(job.level)
            data = self._zlib.compress(data) + self._zlib.flush(zlib.Z_SYNC_FLUSH)
            return header + struct.pack(">I", len(data)) + data
        return header + data

    def _tight_rect(self, rgb: bytes, n: int, level: int) -> bytes:
        """One Tight rectangle, losslessly: Fill if it is one colour, else zlib'd Basic."""
        if rgb == rgb[:3] * n:
            return bytes((_TIGHT_FILL,)) + rgb[:3]
        if len(rgb) < _TIGHT_MIN_TO_COMPRESS:
            return b"\x00" + rgb  # Basic, copy filter, stream 0, not compressed
        ctl = 0
        if self._tight is None:
            # A new stream: tell the viewer to reset its inflater for stream 0.
            self._tight = zlib.compressobj(level)
            ctl |= _TIGHT_RESET_STREAM_0
        data = self._tight.compress(rgb) + self._tight.flush(zlib.Z_SYNC_FLUSH)
        return bytes((ctl,)) + _compact_length(len(data)) + data

    def _local_pointer(self) -> bool:
        """Does this viewer draw the pointer itself? The controller with a cursor shape does."""
        return ENCODING_CURSOR in self._encodings and self._control.holds(self.conn_id)

    def _cursor_rect(self) -> bytes:
        """The arrow as a Cursor pseudo-rectangle (RFC 6143 §7.8.1), in this viewer's format."""
        assert self._format is not None
        fmt = self._format
        encode = fmt.frame(1, 1, bytearray(4)).encode
        return (struct.pack(">HHHHi", *CURSOR_HOTSPOT, CURSOR_WIDTH, CURSOR_HEIGHT, ENCODING_CURSOR)
                + shape(encode) + bitmask())

    def _encoding(self) -> int:
        for encoding in self._encodings:
            if encoding in (ENCODING_TIGHT, ENCODING_ZLIB, ENCODING_RAW):
                return encoding
        return ENCODING_RAW

    def _level(self) -> int:
        """zlib's level: the viewer's CompressLevel, at least 1; else `ZLIB_LEVEL`."""
        for encoding in self._encodings:
            if _COMPRESS_LEVEL_0 <= encoding <= _COMPRESS_LEVEL_0 + 9:
                return max(1, encoding - _COMPRESS_LEVEL_0)
        return ZLIB_LEVEL
