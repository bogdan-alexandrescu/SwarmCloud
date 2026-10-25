"""The mouse pointer: a shape for the controller's viewer, a mark for everyone else (LB-CURSOR).

docs/design/live-browser.md §3.1 and §4.2; part of #1030. Xvfb's `-fbdir`
framebuffer (capture.py) never contains the X pointer -- the server draws its
cursor only on a real display -- and noVNC hides the local pointer over its
canvas (`cursor: none`) unless the server sends a cursor shape. So without
this module the person in control clicks blind, and a watcher cannot tell
where the mouse is. It gives the gate two things:

THE SHAPE (`ARROW`, `HOTSPOT`). A standard arrow, black outline on white,
with its transparency. `rfb.py` sends it as the Cursor pseudo-encoding (-239,
RFC 6143 §7.8.1) to a viewer that lists it, and noVNC then draws it as the
local pointer: zero latency for the person moving it, because it never
travels to the pod and back. The shape is the arrow only. Chromium does not
report the cursor it would draw (no CDP event carries it), and following the
page's `cursor` CSS would mean a script reporting from the page on every
pointer move; a hand or text beam is not worth a channel into a page the
agent controls.

THE MARK (`paint`). Where the pointer last was, in the frame's coordinates --
the controller's last PointerEvent, or the agent's last
`Input.dispatchMouseEvent` (`css_to_frame`) -- painted as the same arrow into
a COPY of the published frame, so a view-only watcher and the relay's
recording (which never sees a local cursor) show where the mouse is. Never
into Xvfb's mapping and never into the frame the hub publishes: the arrow is
the only thing ever painted on a frame, and only on a viewer's own copy.

NOTHING HERE IS LOGGED (§5.3.3): a coordinate is never a log argument.
"""

from __future__ import annotations

import math
from typing import Callable

from .capture import BYTES_PER_PIXEL, Frame
from .presets import Device

#: The arrow, one string per row: `X` outline, `.` fill, space transparent.
ARROW: tuple[str, ...] = (
    "X          ",
    "XX         ",
    "X.X        ",
    "X..X       ",
    "X...X      ",
    "X....X     ",
    "X.....X    ",
    "X......X   ",
    "X.......X  ",
    "X........X ",
    "X.....XXXXX",
    "X..X..X    ",
    "X.X X..X   ",
    "XX  X..X   ",
    "X    X..X  ",
    "     X..X  ",
    "      X..X ",
    "      X..X ",
    "       XX  ",
)
WIDTH = len(ARROW[0])
HEIGHT = len(ARROW)
#: The arrow's tip: the pixel that is the pointer's position.
HOTSPOT = (0, 0)

OUTLINE_RGB = (0, 0, 0)
FILL_RGB = (0xFF, 0xFF, 0xFF)

_COLOURS = {"X": OUTLINE_RGB, ".": FILL_RGB}


def opaque() -> list[tuple[int, int, tuple[int, int, int]]]:
    """Every pixel the arrow covers: (dx, dy, colour), from its top-left."""
    return [(dx, dy, _COLOURS[c]) for dy, row in enumerate(ARROW) for dx, c in enumerate(row) if c in _COLOURS]


def bitmask() -> bytes:
    """The arrow's transparency as RFC 6143 §7.8.1 sends it: one bit per pixel, rows padded to a byte, MSB first."""
    stride = (WIDTH + 7) // 8
    out = bytearray(stride * HEIGHT)
    for dx, dy, _ in opaque():
        out[dy * stride + dx // 8] |= 0x80 >> (dx % 8)
    return bytes(out)


def shape(encode: Callable[[tuple[int, int, int]], bytes]) -> bytes:
    """The arrow's WIDTH x HEIGHT pixels, each as `encode` packs a colour; transparent ones are zero."""
    out = bytearray(WIDTH * HEIGHT * BYTES_PER_PIXEL)
    for dx, dy, rgb in opaque():
        at = (dy * WIDTH + dx) * BYTES_PER_PIXEL
        out[at:at + BYTES_PER_PIXEL] = encode(rgb)
    return bytes(out)


def paint(frame: Frame, x: int, y: int) -> None:
    """Paint the arrow on `frame` with its tip at (x, y), clipped to the frame.

    `frame` must be a viewer's own copy of the published frame (module docstring).
    """
    ox, oy = x - HOTSPOT[0], y - HOTSPOT[1]
    colours = {rgb: frame.encode(rgb) for rgb in (OUTLINE_RGB, FILL_RGB)}
    for dx, dy, rgb in opaque():
        px, py = ox + dx, oy + dy
        if 0 <= px < frame.width and 0 <= py < frame.height:
            at = py * frame.stride + px * BYTES_PER_PIXEL
            frame.pixels[at:at + BYTES_PER_PIXEL] = colours[rgb]


def css_to_frame(device: Device, x: float, y: float) -> tuple[int, int] | None:
    """An `Input.dispatchMouseEvent` point (CSS pixels in the page) in the viewer's frame.

    The inverse of touch.py's mapping: the page sits at `page_top` on the
    screen at `screen_scale`, and the frame starts at `frame_top`. None for a
    point that is not a finite number.
    """
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    fx = round(x * device.screen_scale)
    fy = round(y * device.screen_scale) + device.page_top - device.frame_top
    return (fx, fy)


class PointerTracker:
    """The pointer's last position in the viewer's frame, from whoever moved it last.

    `on_move` is told after every move (the hub wakes its viewers with it).
    """

    def __init__(self, on_move: Callable[[], None] | None = None) -> None:
        self._at: tuple[int, int] | None = None
        self._on_move = on_move

    @property
    def position(self) -> tuple[int, int] | None:
        return self._at

    def move(self, x: int, y: int) -> None:
        at = (int(x), int(y))
        if at == self._at:
            return
        self._at = at
        if self._on_move is not None:
            self._on_move()
