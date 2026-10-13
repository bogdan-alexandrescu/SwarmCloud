"""The live gate's pointer: its shape, where it is, and how it is painted (LB-CURSOR; part of #1030).

Measured 2026-10-12 on task_4e0cf1c649634b949703: Xvfb's -fbdir framebuffer
never holds the X pointer, and noVNC hides its own over the canvas, so the
person in control clicked blind. `livegate.pointer` is the arrow the gate
sends as the Cursor pseudo-encoding and paints into a masked frame's copy for
everyone watching; the RFB side of it is tested in test_live_rfb.py.

Nothing here runs Xvfb, Chromium or a network.
"""

from __future__ import annotations

from agent_worker.livegate import pointer
from agent_worker.livegate.capture import Frame
from agent_worker.livegate.presets import resolve

BACKGROUND = (10, 20, 30)


def test_the_arrow_is_one_rectangle_with_its_tip_at_the_hotspot() -> None:
    assert all(len(row) == pointer.WIDTH for row in pointer.ARROW)
    assert pointer.ARROW[pointer.HOTSPOT[1]][pointer.HOTSPOT[0]] == "X"
    # The bitmask is exactly the opaque pixels: one bit each, rows padded to a byte.
    stride = (pointer.WIDTH + 7) // 8
    mask = pointer.bitmask()
    assert len(mask) == stride * pointer.HEIGHT
    bits = {(x, y) for y in range(pointer.HEIGHT) for x in range(pointer.WIDTH)
            if mask[y * stride + x // 8] & (0x80 >> (x % 8))}
    assert bits == {(dx, dy) for dx, dy, _ in pointer.opaque()}


def test_paint_draws_the_arrow_at_the_point_and_clips_at_the_edge() -> None:
    frame = Frame.blank(30, 20, BACKGROUND)
    pointer.paint(frame, 25, 15)
    for dx, dy, rgb in pointer.opaque():
        x, y = 25 + dx, 15 + dy
        if x < 30 and y < 20:
            assert frame.rgb_at(x, y) == rgb
    assert frame.rgb_at(24, 15) == BACKGROUND
    assert frame.rgb_at(0, 0) == BACKGROUND
    # Past the edge, or wholly off the frame: no error, nothing else touched.
    pointer.paint(frame, -50, -50)
    pointer.paint(frame, 500, 500)
    assert frame.rgb_at(0, 0) == BACKGROUND


def test_paint_follows_the_frames_own_byte_order() -> None:
    frame = Frame(4, 4, 16, bytearray(64), little_endian=False)
    frame.fill(0, 0, 4, 4, BACKGROUND)
    pointer.paint(frame, 0, 0)
    assert frame.rgb_at(0, 0) == pointer.OUTLINE_RGB
    assert frame.rgb_at(1, 2) == pointer.FILL_RGB


def test_the_agents_css_point_lands_where_touch_would_send_it_from() -> None:
    desktop = resolve("desktop")
    # The desktop frame keeps the browser's UI band: the page starts at page_top.
    assert pointer.css_to_frame(desktop, 100, 50) == (
        round(100 * desktop.screen_scale), round(50 * desktop.screen_scale) + desktop.page_top)
    phone = resolve("iphone-15")
    # A phone's frame IS the page (frame_top == page_top), so CSS 0,0 is the frame's corner.
    assert phone.frame_top == phone.page_top
    assert pointer.css_to_frame(phone, 0, 0) == (0, 0)
    assert pointer.css_to_frame(phone, float("nan"), 3) is None
    assert pointer.css_to_frame(phone, 3, float("inf")) is None


def test_the_tracker_tells_its_listener_only_when_the_pointer_moved() -> None:
    told: list[tuple[int, int] | None] = []
    tracker = pointer.PointerTracker()
    assert tracker.position is None
    tracker = pointer.PointerTracker(on_move=lambda: told.append(tracker.position))
    tracker.move(3, 4)
    tracker.move(3, 4)
    tracker.move(5, 6)
    assert told == [(3, 4), (5, 6)]
