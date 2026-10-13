"""The controller's pointer: the arrow noVNC draws, and moves that reach the page (LB-CURSOR2, part of #1030).

Owner report 2026-10-12 ~19:00 PDT, after LB-CURSOR (PR 1198) released: "No
pointer" while in control of task_50f0b9841b9f46db871a. Two measurements
followed, and these tests hold each.

THE SHAPE. The operator decoded the canvas's CSS cursor -- the PNG noVNC made
from the gate's Cursor rectangle -- and its alpha bytes read as a one-pixel
diagonal hairline with stray dots. The rectangle itself is the full arrow:
`test_the_cursor_decoded_as_novnc_decodes_it_is_the_whole_arrow` decodes the
bytes the gate sends exactly as noVNC 1.4.0 does (core/rfb.js
`_handleCursor`: pixels first, then a mask of ceil(w/8) bytes a row, most
significant bit first) and compares the result with a golden 11x19 picture.
The hairline is what that arrow's alpha looks like when a PNG's rows are read
WITHOUT undoing its "Up" filter (each row stored as its difference from the
row above): `test_the_measured_hairline_is_the_arrows_alpha_left_up_filtered`
reproduces the measurement row for row. So the shape was never the defect; the
moves were.

THE MOVES. On task_1d1785b21d5b4e1f894e (2026-10-13 02:24Z, a visible console
tab) 20 mousemove events, a click and two keys reached noVNC's canvas, and the
audit counted two pointer events: the press and the release. The painted
pointer stayed at the click point. Moves were dropped before the gate (the
console kept noVNC view-only until the canvas had focus: LiveBrowser.test.tsx
holds that half); at the gate a move is now injected, counted, and moves the
pointer watchers see, and a burst above `MAX_MOTION_PER_SECOND` is coalesced to
its latest point rather than dropped.

No test here runs Xvfb, Chromium or a network.
"""

from __future__ import annotations

import asyncio

from agent_worker.livegate import pointer, rfb

from test_live_rfb import CURSOR, Gate, _NOVNC_RGBX, _frame, _rgbx_at, _unmasking

#: The arrow as the person must see it: `X` black outline, `.` white fill.
GOLDEN = (
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

#: What the operator read off the canvas's cursor PNG on 2026-10-13 (`#` an
#: opaque alpha byte), the rows the report gave.
MEASURED_HAIRLINE = {
    0: "#..........",
    1: ".#.........",
    2: "..#........",
    10: "..........#",
    11: "...........",
    12: ".......#...",
    13: "...........",
    14: "........#..",
    15: "...........",
    16: ".........#.",
}


def novnc_decode(w: int, h: int, data: bytes) -> list[list[tuple[int, int, int, int]]]:
    """noVNC 1.4.0's `_handleCursor`, line for line: RGBA per pixel."""
    pixels, mask = data[:w * h * 4], data[w * h * 4:]
    stride = -(-w // 8)  # Math.ceil(w / 8)
    assert len(mask) == stride * h
    rows = []
    for y in range(h):
        row = []
        for x in range(w):
            alpha = 255 if (mask[y * stride + x // 8] << (x % 8)) & 0x80 else 0
            at = (y * w + x) * 4
            # "Decode from BGRX pixels": noVNC swaps the first and third
            # bytes. Black and white read the same either way round.
            row.append((pixels[at + 2], pixels[at + 1], pixels[at], alpha))
        rows.append(row)
    return rows


async def _cursor_rect(gate: Gate) -> tuple[int, int, int, int, bytes]:
    gate.hub.publish(rfb.masked(_frame(), _unmasking()))
    viewer = await gate.connect("person-1", encodings=(0, rfb.ENCODING_DESKTOP_SIZE, CURSOR))
    viewer.set_pixel_format(**_NOVNC_RGBX)
    viewer.request(incremental=False)
    await viewer.flush()
    [(x, y, w, h, _enc, data)] = [r for r in await viewer.read_update() if r[4] == CURSOR]
    return x, y, w, h, data


def test_the_cursor_decoded_as_novnc_decodes_it_is_the_whole_arrow() -> None:
    async def main() -> None:
        gate = Gate()
        _x, _y, w, h, data = await _cursor_rect(gate)
        assert (w, h) == (11, 19)
        decoded = novnc_decode(w, h, data)
        drawn = tuple(
            "".join(" " if a == 0 else ("X" if (r, g, b) == (0, 0, 0) else "." if (r, g, b) == (255, 255, 255) else "?")
                    for r, g, b, a in row)
            for row in decoded)
        assert drawn == GOLDEN, "\n" + "\n".join(drawn)
        opaque = sum(a != 0 for row in decoded for *_rgb, a in row)
        assert opaque == sum(c != " " for row in GOLDEN for c in row)
        assert opaque > 40, "a hairline is not an arrow"
        # Visible on a dark page and on a light one: both colours are there.
        assert decoded[5][2][:3] == (255, 255, 255), "the inside is white"
        assert decoded[5][0][:3] == (0, 0, 0), "the outline is black"
        # No row is a hairline: the widest row is the arrow's.
        assert max(sum(a != 0 for *_rgb, a in row) for row in decoded) == 11
        await gate.close()

    asyncio.run(main())


def test_the_cursors_hotspot_is_the_arrows_tip() -> None:
    async def main() -> None:
        gate = Gate()
        x, y, w, _h, data = await _cursor_rect(gate)
        assert (x, y) == (0, 0) == pointer.HOTSPOT
        tip = novnc_decode(w, 19, data)[0][0]
        assert tip == (0, 0, 0, 255), "the hotspot is an opaque pixel of the outline"
        await gate.close()

    asyncio.run(main())


def test_the_measured_hairline_is_the_arrows_alpha_left_up_filtered() -> None:
    """A PNG row filtered "Up" stores alpha[y][x] - alpha[y-1][x] (mod 256);
    read without undoing it, only a pixel that turns opaque under a
    transparent one stays 255. That is the operator's picture exactly."""
    alpha = [[255 if c != " " else 0 for c in row] for row in GOLDEN]
    for y, expected in MEASURED_HAIRLINE.items():
        above = alpha[y - 1] if y else [0] * 11
        stored = [(a - b) % 256 for a, b in zip(alpha[y], above)]
        assert "".join("#" if v >= 128 else "." for v in stored) == expected, y


# --------------------------------------------------------------------------
# Moves reach the page, are counted, and move the pointer watchers see
# --------------------------------------------------------------------------


def test_a_move_with_no_button_is_injected_counted_and_moves_the_painted_pointer() -> None:
    async def main() -> None:
        gate = Gate()
        gate.hub.publish(rfb.masked(_frame(), _unmasking()))
        watcher = await gate.connect("watcher", encodings=(0, CURSOR))
        watcher.set_pixel_format(**_NOVNC_RGBX)
        person = await gate.connect("person-1", encodings=(0, CURSOR))
        await gate.control.grant("person-1")
        person.pointer(0, 10, 5)
        await person.flush()
        person.pointer(0, 30, 12)
        await person.flush()
        assert gate.injector.pointer == [(0, 10, 5), (0, 30, 12)]
        assert gate.sessions["person-1"].counts.pointer_events == 2
        assert gate.hub.pointer.position == (30, 12)
        watcher.request(incremental=False)
        await watcher.flush()
        [(_x, _y, w, _h, _enc, data)] = [r for r in await watcher.read_update() if r[4] == 0]
        assert _rgbx_at(data, w, 30, 12) == pointer.OUTLINE_RGB, "the watcher's arrow follows the move"
        await gate.close()

    asyncio.run(main())


def test_a_burst_of_moves_above_the_limit_is_coalesced_to_its_last_point_not_dropped() -> None:
    async def main() -> None:
        now = [100.0]
        gate = Gate()
        person = await gate.connect("person-1", clock=lambda: now[0], max_motion_per_second=5)
        await gate.control.grant("person-1")
        for i in range(20):
            person.pointer(0, i, 1)
        await person.flush()
        session = gate.sessions["person-1"]
        assert gate.injector.pointer == [(0, i, 1) for i in range(5)], "the limit's worth goes at once"
        assert session.counts.dropped == 0, "a move above the limit is held, never dropped"
        # The limit has room again: the held move is the LAST one sent.
        now[0] += 1.0
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(gate.injector.pointer) == 6:
                break
        assert gate.injector.pointer[-1] == (0, 19, 1)
        assert len(gate.injector.pointer) == 6
        assert session.counts.pointer_events == 6
        assert gate.hub.pointer.position == (19, 1)
        await gate.close()

    asyncio.run(main())


def test_a_click_is_never_held_behind_moves() -> None:
    async def main() -> None:
        now = [100.0]
        gate = Gate()
        person = await gate.connect("person-1", clock=lambda: now[0], max_motion_per_second=1)
        await gate.control.grant("person-1")
        person.pointer(0, 1, 1)
        person.pointer(0, 2, 2)  # held: the motion limit is spent
        person.pointer(1, 3, 3)  # a press: goes at once, and supersedes the held move
        person.pointer(0, 3, 3)  # the release
        await person.flush()
        assert gate.injector.pointer == [(0, 1, 1), (1, 3, 3), (0, 3, 3)]
        await gate.close()

    asyncio.run(main())


def test_a_move_from_a_viewer_who_is_not_in_control_is_still_discarded() -> None:
    async def main() -> None:
        gate = Gate()
        watcher = await gate.connect("watcher")
        await gate.connect("person-1")
        await gate.control.grant("person-1")
        watcher.pointer(0, 5, 5)
        await watcher.flush()
        assert gate.injector.pointer == []
        assert gate.sessions["watcher"].counts.dropped == 1
        assert gate.hub.pointer.position is None
        await gate.close()

    asyncio.run(main())
