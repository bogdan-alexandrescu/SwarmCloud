"""The live gate's mask: nothing sensitive leaves the pod (LB-L1, design §5.3).

docs/design/live-browser.md §5.3 says the gate paints opaque boxes into the
framebuffer before encoding, over password fields (sticky: a field that was
ever `password` stays masked after a "show password" toggle), over the
sensitive `autocomplete` kinds, over text a `swarm_redaction` rule matches, and
over the address bar when the URL matches one -- and that it FAILS CLOSED: a
report older than the last layout change blanks the whole page area, and with
no report since a navigation, or no CDP session, the frame is blank.

Each case below builds its frame and its observer reports by hand, so it runs
with no browser and no display. Every credential-shaped value is assembled at
run time (CLAUDE.md, "Nothing you add may look like a credential").
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import struct
from pathlib import Path

import pytest

from agent_worker.livegate.capture import CaptureError, Frame, XvfbFramebuffer, parse_xwd
from agent_worker.livegate.mask import (
    BINDING_NAME,
    DILATE_PX,
    MASK_RGB,
    UNAVAILABLE_RGB,
    WORLD_NAME,
    MaskCdp,
    MaskState,
    Outcome,
    observer_source,
)

WHITE = (0xFF, 0xFF, 0xFF)
TOP = "frame-top"
CHILD = "frame-child"
DOC = "doc-1"

# A 200x150 screen whose bottom 120 rows are the page: a 30-row browser chrome
# (address bar, tabs) above it, as a maximised window has.
SCREEN_W, SCREEN_H = 200, 150
PAGE_H = 120
CHROME_H = SCREEN_H - PAGE_H


# --------------------------------------------------------------------------
# Fixtures: an XWD framebuffer file like Xvfb -fbdir writes, and reports
# --------------------------------------------------------------------------


def _xwd(width: int, height: int, rgb: tuple[int, int, int], *, depth_bpp: int = 32,
         pixmap_format: int = 2, byte_order: int = 0) -> bytes:
    """An XWD file of one colour, in the layout Xvfb's -fbdir file has."""
    name = b"Xvfb main window\x00"
    header_size = 100 + len(name)
    bytes_per_line = width * (depth_bpp // 8)
    ncolors = 4
    header = struct.pack(
        ">25I",
        header_size, 7, pixmap_format, 24, width, height, 0,
        byte_order, 32, byte_order, 32, depth_bpp, bytes_per_line,
        4, 0xFF0000, 0x00FF00, 0x0000FF, 8, 256, ncolors,
        width, height, 0, 0, 0,
    )
    colours = b"".join(struct.pack(">IHHHBB", i, 0, 0, 0, 7, 0) for i in range(ncolors))
    value = (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
    pixel = value.to_bytes(4, "little" if byte_order == 0 else "big")
    return header + name + colours + pixel * (width * height)


def _frame(tmp_path: Path, rgb: tuple[int, int, int] = WHITE) -> Frame:
    path = tmp_path / "Xvfb_screen0"
    path.write_bytes(_xwd(SCREEN_W, SCREEN_H, rgb))
    with XvfbFramebuffer(path) as fb:
        return fb.grab()


def _rgb(frame: Frame, x: int, y: int) -> tuple[int, int, int]:
    return frame.rgb_at(x, y)


def _report(epoch: int = 1, *, doc: str = DOC, fields=(), texts=(), owners=(),
            viewport=(SCREEN_W, PAGE_H, 0, 0, 1), truncated: bool = False) -> str:
    return json.dumps({
        "v": 1, "k": "report", "doc": doc, "e": epoch,
        "vp": list(viewport),
        "f": [dict(f) for f in fields],
        "t": [dict(t) for t in texts],
        "o": [dict(o) for o in owners],
        "tr": truncated,
    })


def _layout(epoch: int, *, doc: str = DOC) -> str:
    return json.dumps({"v": 1, "k": "layout", "doc": doc, "e": epoch})


def _field(fid: int, ftype: str, x: float, y: float, w: float, h: float, *,
           ac: str = "", ever: bool = False) -> dict:
    return {"id": fid, "type": ftype, "ac": ac, "ever": ever, "r": [x, y, w, h]}


def _text(text: str, block: int, x: float, y: float, w: float, h: float) -> dict:
    return {"s": text, "b": block, "r": [x, y, w, h]}


def _state(url: str = "https://example.test/login", scale: float = 1.0) -> MaskState:
    state = MaskState(scale=scale)
    state.session_up()
    state.frame_attached(TOP, None)
    state.navigated(TOP, url)
    return state


def _painted(frame: Frame, x0: int, y0: int, x1: int, y1: int, rgb=MASK_RGB) -> bool:
    return all(_rgb(frame, x, y) == rgb for y in range(y0, y1) for x in range(x0, x1))


def _untouched(frame: Frame, x0: int, y0: int, x1: int, y1: int) -> bool:
    return _painted(frame, x0, y0, x1, y1, rgb=WHITE)


def _fake_github_token() -> str:
    # A `github_token` rule match, assembled so no literal here is one.
    return "gh" + "p_" + secrets.token_hex(18)


# --------------------------------------------------------------------------
# The four acceptance cases of lane L1 (design §9)
# --------------------------------------------------------------------------


def test_a_fixture_frame_plus_rectangles_comes_out_with_those_pixels_painted(tmp_path):
    frame = _frame(tmp_path)
    state = _state()
    state.on_message(TOP, _report(fields=[_field(1, "password", 50, 40, 60, 12)]))

    assert state.apply(frame) is Outcome.MASKED

    # Page CSS (50,40)-(110,52) is screen (50,70)-(110,82); dilated by 8.
    d = DILATE_PX
    assert _painted(frame, 50 - d, 70 - d, 110 + d, 82 + d)
    assert _untouched(frame, 0, CHROME_H, 50 - d, SCREEN_H)  # left of the box
    assert _untouched(frame, 110 + d, CHROME_H, SCREEN_W, SCREEN_H)  # right of it
    assert _untouched(frame, 0, 82 + d, SCREEN_W, SCREEN_H)  # below it
    assert _untouched(frame, 0, 0, SCREEN_W, CHROME_H)  # the address bar is visible


def test_a_field_toggled_from_password_to_text_stays_masked(tmp_path):
    state = _state()
    state.on_message(TOP, _report(1, fields=[_field(7, "password", 20, 20, 80, 10)]))
    # "Show password": the same element, now type=text. The observer's own
    # sticky flag is deliberately false here: the gate's memory alone holds it.
    state.on_message(TOP, _layout(2))
    state.on_message(TOP, _report(2, fields=[
        _field(7, "text", 20, 20, 80, 10, ever=False),
        _field(8, "text", 20, 90, 80, 10),  # an ordinary text field, never a password
    ]))

    frame = _frame(tmp_path)
    assert state.apply(frame) is Outcome.MASKED
    assert _painted(frame, 20, 50, 100, 60)  # field 7 stays masked
    assert _untouched(frame, 20, 120, 100, 130)  # field 8 is shown


def test_a_stale_report_blanks_the_page_area(tmp_path):
    state = _state()
    state.on_message(TOP, _report(1, fields=[_field(1, "password", 50, 40, 60, 12)]))
    # A scroll: the report above was measured before it.
    state.on_message(TOP, _layout(2))

    frame = _frame(tmp_path)
    assert state.apply(frame) is Outcome.PAGE_BLANKED
    assert _painted(frame, 0, CHROME_H, SCREEN_W, SCREEN_H)
    assert _untouched(frame, 0, 0, SCREEN_W, CHROME_H)

    # A report measured before the scroll, arriving late, does not unblank it.
    state.on_message(TOP, _report(1, fields=[_field(1, "password", 50, 40, 60, 12)]))
    assert state.apply(_frame(tmp_path)) is Outcome.PAGE_BLANKED

    # One measured after it does.
    state.on_message(TOP, _report(2, fields=[_field(1, "password", 50, 10, 60, 12)]))
    frame = _frame(tmp_path)
    assert state.apply(frame) is Outcome.MASKED
    assert _untouched(frame, 0, 100, SCREEN_W, SCREEN_H)


def test_a_redaction_rule_match_in_text_is_masked(tmp_path):
    token = _fake_github_token()
    state = _state()
    state.on_message(TOP, _report(texts=[
        _text("Your new token: " + token, 1, 10, 10, 150, 10),
        _text("Welcome back to the dashboard", 2, 10, 60, 150, 10),
    ]))

    frame = _frame(tmp_path)
    assert state.apply(frame) is Outcome.MASKED
    assert _painted(frame, 10, 40, 160, 50)
    assert _untouched(frame, 0, 50 + DILATE_PX, SCREEN_W, SCREEN_H)


# --------------------------------------------------------------------------
# The rest of §5.3: what else is masked, and every way it fails closed
# --------------------------------------------------------------------------


def test_a_secret_split_across_text_nodes_of_one_block_is_masked(tmp_path):
    token = _fake_github_token()
    head, tail = token[:6], token[6:]
    state = _state()
    state.on_message(TOP, _report(texts=[
        _text(head, 3, 10, 10, 30, 10),
        _text(tail, 3, 40, 10, 120, 10),
    ]))
    frame = _frame(tmp_path)
    state.apply(frame)
    assert _painted(frame, 10, 40, 40, 50)
    assert _painted(frame, 40, 40, 160, 50)


@pytest.mark.parametrize("autocomplete", [
    "current-password", "new-password", "one-time-code", "cc-number", "cc-csc",
    "section-billing billing cc-number",
])
def test_sensitive_autocomplete_fields_are_masked(tmp_path, autocomplete):
    state = _state()
    state.on_message(TOP, _report(fields=[_field(1, "text", 20, 20, 80, 10, ac=autocomplete)]))
    frame = _frame(tmp_path)
    state.apply(frame)
    assert _painted(frame, 20, 50, 100, 60)


def test_an_ordinary_autocomplete_field_is_not_masked(tmp_path):
    state = _state()
    state.on_message(TOP, _report(fields=[_field(1, "text", 20, 20, 80, 10, ac="username")]))
    frame = _frame(tmp_path)
    state.apply(frame)
    assert _untouched(frame, 0, 0, SCREEN_W, SCREEN_H)


def test_the_address_bar_is_masked_when_the_url_matches_a_rule(tmp_path):
    state = _state("https://example.test/cb?access=" + _fake_github_token())
    state.on_message(TOP, _report())
    frame = _frame(tmp_path)
    assert state.apply(frame) is Outcome.MASKED
    assert _painted(frame, 0, 0, SCREEN_W, CHROME_H)
    assert _untouched(frame, 0, CHROME_H, SCREEN_W, SCREEN_H)


def test_a_same_document_url_change_is_checked_and_makes_the_report_stale(tmp_path):
    state = _state()
    state.on_message(TOP, _report())
    state.url_changed(TOP, "https://example.test/#t=" + _fake_github_token())
    frame = _frame(tmp_path)
    assert state.apply(frame) is Outcome.PAGE_BLANKED
    assert _painted(frame, 0, 0, SCREEN_W, SCREEN_H)


def test_a_page_rectangle_cannot_paint_over_the_address_bar(tmp_path):
    # A page that marks a huge "password field" over everything must not be
    # able to hide the address bar, which is part of the anti-phishing design.
    state = _state()
    state.on_message(TOP, _report(fields=[_field(1, "password", -500, -500, 5000, 5000)]))
    frame = _frame(tmp_path)
    state.apply(frame)
    assert _untouched(frame, 0, 0, SCREEN_W, CHROME_H)
    assert _painted(frame, 0, CHROME_H, SCREEN_W, SCREEN_H)


def test_dilation_near_the_top_of_the_page_stops_at_the_page_edge(tmp_path):
    state = _state()
    state.on_message(TOP, _report(fields=[_field(1, "password", 20, 2, 40, 10)]))
    frame = _frame(tmp_path)
    state.apply(frame)
    assert _painted(frame, 12, CHROME_H, 68, CHROME_H + 20)
    assert _untouched(frame, 0, 0, SCREEN_W, CHROME_H)


def test_no_report_since_a_navigation_is_an_unavailable_frame(tmp_path):
    state = _state()
    state.on_message(TOP, _report(fields=[_field(1, "password", 50, 40, 60, 12)]))
    state.navigated(TOP, "https://example.test/next")
    frame = _frame(tmp_path)
    assert state.apply(frame) is Outcome.UNAVAILABLE
    assert _painted(frame, 0, 0, SCREEN_W, SCREEN_H, rgb=UNAVAILABLE_RGB)


def test_no_cdp_session_is_an_unavailable_frame(tmp_path):
    state = _state()
    state.on_message(TOP, _report())
    state.session_down()
    frame = _frame(tmp_path)
    assert state.apply(frame) is Outcome.UNAVAILABLE
    assert _painted(frame, 0, 0, SCREEN_W, SCREEN_H, rgb=UNAVAILABLE_RGB)
    # Coming back is not enough: the reports from before are not trusted.
    state.session_up()
    assert state.apply(_frame(tmp_path)) is Outcome.UNAVAILABLE


def test_a_new_document_without_a_navigation_event_is_unavailable_until_it_reports(tmp_path):
    state = _state()
    state.on_message(TOP, _report())
    state.on_message(TOP, _layout(1, doc="doc-2"))
    assert state.apply(_frame(tmp_path)) is Outcome.UNAVAILABLE
    state.on_message(TOP, _report(1, doc="doc-2"))
    assert state.apply(_frame(tmp_path)) is Outcome.MASKED


def test_stickiness_does_not_carry_to_a_new_document(tmp_path):
    state = _state()
    state.on_message(TOP, _report(fields=[_field(7, "password", 20, 20, 80, 10)]))
    state.navigated(TOP, "https://example.test/other")
    state.on_message(TOP, _report(1, doc="doc-2", fields=[_field(7, "text", 20, 20, 80, 10)]))
    frame = _frame(tmp_path)
    state.apply(frame)
    assert _untouched(frame, 0, 0, SCREEN_W, SCREEN_H)


def test_a_truncated_report_blanks_the_page_area(tmp_path):
    state = _state()
    state.on_message(TOP, _report(truncated=True))
    frame = _frame(tmp_path)
    assert state.apply(frame) is Outcome.PAGE_BLANKED
    assert _painted(frame, 0, CHROME_H, SCREEN_W, SCREEN_H)


@pytest.mark.parametrize("payload", [
    "not json",
    json.dumps({"v": 1, "k": "report", "doc": DOC, "e": 2, "vp": [200, 120, 0, 0, 1],
                "f": [{"id": 1, "type": "password", "r": [0, 0, "x", 1]}], "t": [], "o": []}),
    '{"v": 1, "k": "report", "doc": "doc-1", "e": 2, "vp": [200, 120, 0, 0, 1e999],'
    ' "f": [], "t": [], "o": [], "tr": false}',
    json.dumps({"v": 9, "k": "report"}),
    json.dumps([1, 2, 3]),
])
def test_a_malformed_message_makes_the_frame_stale(tmp_path, payload):
    state = _state()
    state.on_message(TOP, _report(1))
    state.on_message(TOP, payload)
    assert state.apply(_frame(tmp_path)) in (Outcome.PAGE_BLANKED, Outcome.UNAVAILABLE)


def test_a_viewport_larger_than_the_screen_is_unavailable(tmp_path):
    state = _state()
    state.on_message(TOP, _report(viewport=(SCREEN_W, SCREEN_H + 40, 0, 0, 1)))
    assert state.apply(_frame(tmp_path)) is Outcome.UNAVAILABLE


def test_rectangles_scale_by_the_device_scale_factor(tmp_path):
    # A phone preset at 2x: the page is 100x60 CSS pixels, 200x120 on screen.
    state = _state(scale=2.0)
    state.on_message(TOP, _report(viewport=(100, 60, 0, 0, 1),
                                  fields=[_field(1, "password", 10, 10, 20, 5)]))
    frame = _frame(tmp_path)
    state.apply(frame)
    assert _painted(frame, 20, CHROME_H + 20, 60, CHROME_H + 30)
    assert _untouched(frame, 60 + DILATE_PX, CHROME_H, SCREEN_W, SCREEN_H)


def test_a_device_change_makes_every_report_stale(tmp_path):
    state = _state()
    state.on_message(TOP, _report(viewport=(100, 60, 0, 0, 1)))
    assert state.apply(_frame(tmp_path)) is Outcome.MASKED
    state.set_scale(2.0)
    assert state.apply(_frame(tmp_path)) is Outcome.PAGE_BLANKED
    state.on_message(TOP, _report(viewport=(100, 60, 0, 0, 1)))
    assert state.apply(_frame(tmp_path)) is Outcome.MASKED


def test_a_pinch_zoomed_visual_viewport_is_mapped(tmp_path):
    # Zoomed 2x into the layout viewport, scrolled 10,5 CSS px into it. As
    # Chromium reports it, `innerWidth` x `innerHeight` are the visible area,
    # half the page each way (QA12-01, mask.py GEOMETRY).
    state = _state()
    state.on_message(TOP, _report(viewport=(SCREEN_W // 2, PAGE_H // 2, 10, 5, 2),
                                  fields=[_field(1, "password", 20, 15, 10, 5)]))
    frame = _frame(tmp_path)
    state.apply(frame)
    # (20-10)*2 = 20, (15-5)*2 = 20 -> screen (20, 50) to (40, 60).
    assert _painted(frame, 20, CHROME_H + 20, 40, CHROME_H + 30)


def test_a_child_frame_is_masked_at_its_owner_offset(tmp_path):
    state = _state()
    state.frame_attached(CHILD, TOP)
    state.frame_linked(CHILD, 42)
    state.on_message(TOP, _report(owners=[{"id": 42, "r": [100, 50, 80, 60]}]))
    state.on_message(CHILD, _report(doc="child-doc", viewport=(80, 60, 0, 0, 1),
                                    fields=[_field(1, "password", 5, 5, 30, 8)]))
    frame = _frame(tmp_path)
    assert state.apply(frame) is Outcome.MASKED
    # (100+5, 50+5) in the top page -> screen (105, 85) to (135, 93).
    assert _painted(frame, 105, CHROME_H + 55, 135, CHROME_H + 63)
    assert _untouched(frame, 0, CHROME_H, 90, SCREEN_H)


def test_an_unlinked_child_frame_covers_the_box_left_for_it_and_a_silent_one_covers_its_box(tmp_path):
    # LIVE-BLANK (part of #1030): a child that cannot vouch for itself covers
    # its own box -- its content is clipped to it -- not the whole page.
    # DEVB-MASK (part of #1344): so does an unlinked one, when its parent
    # reports an iframe box for every unlinked child that no linked one claims.
    state = _state()
    state.on_message(TOP, _report(owners=[{"id": 42, "r": [100, 50, 80, 60]}]))
    state.frame_attached(CHILD, TOP)
    # Not linked to its owner: the one unclaimed iframe box is covered.
    frame = _frame(tmp_path)
    assert state.apply(frame) is Outcome.MASKED
    assert _painted(frame, 100, CHROME_H + 50, 180, CHROME_H + 110)
    assert _untouched(frame, 0, CHROME_H, 90, SCREEN_H)
    state.frame_linked(CHILD, 42)
    # Linked, visible, and no report yet: its box is covered, the page shown.
    frame = _frame(tmp_path)
    assert state.apply(frame) is Outcome.MASKED
    assert _painted(frame, 100, CHROME_H + 50, 180, CHROME_H + 110)
    assert _untouched(frame, 0, CHROME_H, 90, SCREEN_H)
    state.on_message(CHILD, _report(doc="child-doc", viewport=(80, 60, 0, 0, 1)))
    frame = _frame(tmp_path)
    assert state.apply(frame) is Outcome.MASKED
    assert _untouched(frame, 100, CHROME_H + 50, 180, CHROME_H + 110)
    # Its own layout change makes it stale: its box is covered again.
    state.on_message(CHILD, _layout(2, doc="child-doc"))
    frame = _frame(tmp_path)
    assert state.apply(frame) is Outcome.MASKED
    assert _painted(frame, 100, CHROME_H + 50, 180, CHROME_H + 110)
    assert _untouched(frame, 0, CHROME_H, 90, SCREEN_H)


def test_a_child_frame_drawn_nowhere_does_not_blank_the_page(tmp_path):
    # A zero-size tracking iframe never reports anything worth painting.
    state = _state()
    state.frame_attached(CHILD, TOP)
    state.frame_linked(CHILD, 42)
    state.on_message(TOP, _report(owners=[{"id": 42, "r": [0, 0, 0, 0]}]))
    assert state.apply(_frame(tmp_path)) is Outcome.MASKED
    state.frame_detached(CHILD)
    assert state.apply(_frame(tmp_path)) is Outcome.MASKED


def test_neither_text_nor_typed_values_reach_the_log(tmp_path, caplog):
    token = _fake_github_token()
    caplog.set_level(logging.DEBUG)
    state = _state("https://example.test/?k=" + token)
    state.on_message(TOP, _report(texts=[_text(token, 1, 10, 10, 150, 10)],
                                  fields=[_field(1, "password", 50, 40, 60, 12)]))
    state.on_message(TOP, "not json " + token)
    state.apply(_frame(tmp_path))
    assert token not in caplog.text
    assert token[:12] not in caplog.text


# --------------------------------------------------------------------------
# Capture: the framebuffer file
# --------------------------------------------------------------------------


def test_capture_reads_an_xvfb_framebuffer_and_returns_a_copy(tmp_path):
    path = tmp_path / "Xvfb_screen0"
    path.write_bytes(_xwd(8, 4, (10, 20, 30)))
    with XvfbFramebuffer(path) as fb:
        frame = fb.grab()
        frame.fill(0, 0, 8, 4, MASK_RGB)
        # Painting the copy never touches the display's own memory.
        assert fb.grab().rgb_at(3, 2) == (10, 20, 30)
    assert (frame.width, frame.height) == (8, 4)
    assert frame.rgb_at(3, 2) == MASK_RGB


def test_capture_honours_msb_first_pixels():
    frame = parse_xwd(_xwd(4, 2, (1, 2, 3), byte_order=1))
    assert frame.rgb_at(0, 0) == (1, 2, 3)
    frame.fill(0, 0, 1, 1, (9, 8, 7))
    assert frame.rgb_at(0, 0) == (9, 8, 7)
    assert frame.rgb_at(1, 0) == (1, 2, 3)


@pytest.mark.parametrize("kwargs", [{"depth_bpp": 16}, {"pixmap_format": 1}])
def test_capture_refuses_a_format_it_cannot_mask(kwargs):
    with pytest.raises(CaptureError):
        parse_xwd(_xwd(4, 2, (1, 2, 3), **kwargs))


def test_capture_refuses_a_truncated_file():
    with pytest.raises(CaptureError):
        parse_xwd(_xwd(4, 2, (1, 2, 3))[:-5])


def test_fill_is_clipped_to_the_frame():
    frame = Frame.blank(4, 4, WHITE)
    frame.fill(-10, -10, 2, 2, MASK_RGB)
    frame.fill(3, 3, 100, 100, MASK_RGB)
    assert frame.rgb_at(0, 0) == MASK_RGB and frame.rgb_at(1, 1) == MASK_RGB
    assert frame.rgb_at(2, 2) == WHITE
    assert frame.rgb_at(3, 3) == MASK_RGB


# --------------------------------------------------------------------------
# The gate's own CDP session: the isolated world and the binding
# --------------------------------------------------------------------------


class _FakeCdp:
    def __init__(self, replies: dict[str, dict] | None = None):
        self.sent: list[tuple[str, dict]] = []
        self.replies = replies or {}

    async def __call__(self, method: str, params: dict) -> dict:
        self.sent.append((method, params))
        return self.replies.get(method, {})


def test_install_puts_the_observer_in_an_isolated_world_with_a_world_only_binding():
    cdp = _FakeCdp({
        "Page.getFrameTree": {"frameTree": {
            "frame": {"id": TOP, "url": "https://example.test/"},
            "childFrames": [{"frame": {"id": CHILD, "parentId": TOP, "url": "about:blank"}}],
        }},
    })
    state = MaskState()
    asyncio.run(MaskCdp(state, cdp).install())

    methods = [m for m, _ in cdp.sent]
    binding = dict(cdp.sent)["Runtime.addBinding"]
    assert binding == {"name": BINDING_NAME, "executionContextName": WORLD_NAME}
    script = dict(cdp.sent)["Page.addScriptToEvaluateOnNewDocument"]
    assert script == {"source": observer_source(), "worldName": WORLD_NAME, "runImmediately": True}
    # The binding exists before any world that could call it, and there is
    # exactly one observer world per frame: a second world would report a
    # second document and the gate would never see a settled page.
    assert methods.index("Runtime.addBinding") < methods.index("Page.addScriptToEvaluateOnNewDocument")
    assert "Page.createIsolatedWorld" not in methods
    assert BINDING_NAME in observer_source()
    # The frames already there are known, and none has reported yet.
    assert state.apply(Frame.blank(SCREEN_W, SCREEN_H, WHITE)) is Outcome.UNAVAILABLE


def test_only_the_isolated_world_can_report():
    state = MaskState()
    mask = MaskCdp(state, _FakeCdp())
    state.session_up()

    async def run():
        await mask.on_event("Page.frameNavigated", {"frame": {"id": TOP, "url": "https://example.test/"}})
        await mask.on_event("Runtime.executionContextCreated", {"context": {
            "id": 1, "name": "", "auxData": {"frameId": TOP, "isDefault": True}}})
        await mask.on_event("Runtime.executionContextCreated", {"context": {
            "id": 2, "name": WORLD_NAME, "auxData": {"frameId": TOP, "isDefault": False}}})
        # The page's main world cannot have the binding; if a call ever
        # claimed to come from it, it is not a report.
        await mask.on_event("Runtime.bindingCalled", {
            "name": BINDING_NAME, "payload": _report(), "executionContextId": 1})

    asyncio.run(run())
    assert state.apply(Frame.blank(SCREEN_W, SCREEN_H, WHITE)) is Outcome.UNAVAILABLE

    asyncio.run(mask.on_event("Runtime.bindingCalled", {
        "name": BINDING_NAME, "payload": _report(), "executionContextId": 2}))
    assert state.apply(Frame.blank(SCREEN_W, SCREEN_H, WHITE)) is Outcome.MASKED


def test_a_child_frame_is_linked_to_its_owner_through_the_parent_world():
    cdp = _FakeCdp({
        "DOM.getFrameOwner": {"backendNodeId": 99},
        "DOM.resolveNode": {"object": {"objectId": "obj-1"}},
        "Runtime.callFunctionOn": {"result": {"type": "number", "value": 42}},
    })
    state = MaskState()
    state.session_up()
    mask = MaskCdp(state, cdp)

    async def run():
        await mask.on_event("Page.frameNavigated", {"frame": {"id": TOP, "url": "https://example.test/"}})
        await mask.on_event("Runtime.executionContextCreated", {"context": {
            "id": 2, "name": WORLD_NAME, "auxData": {"frameId": TOP}}})
        await mask.on_event("Page.frameAttached", {"frameId": CHILD, "parentFrameId": TOP})

    asyncio.run(run())
    sent = dict(cdp.sent)
    assert sent["DOM.getFrameOwner"] == {"frameId": CHILD}
    assert sent["DOM.resolveNode"] == {"backendNodeId": 99, "executionContextId": 2}
    state.on_message(TOP, _report(owners=[{"id": 42, "r": [100, 50, 80, 60]}]))
    state.on_message(CHILD, _report(doc="c", viewport=(80, 60, 0, 0, 1),
                                    fields=[_field(1, "password", 5, 5, 30, 8)]))
    frame = Frame.blank(SCREEN_W, SCREEN_H, WHITE)
    assert state.apply(frame) is Outcome.MASKED
    assert frame.rgb_at(110, CHROME_H + 58) == MASK_RGB


def test_a_frame_navigation_from_cdp_resets_the_frame():
    state = MaskState()
    state.session_up()
    mask = MaskCdp(state, _FakeCdp())
    asyncio.run(mask.on_event("Page.frameNavigated", {"frame": {"id": TOP, "url": "https://a.test/"}}))
    state.on_message(TOP, _report())
    assert state.apply(Frame.blank(SCREEN_W, SCREEN_H, WHITE)) is Outcome.MASKED
    asyncio.run(mask.on_event("Page.frameNavigated", {"frame": {"id": TOP, "url": "https://b.test/"}}))
    assert state.apply(Frame.blank(SCREEN_W, SCREEN_H, WHITE)) is Outcome.UNAVAILABLE


# --- the observer's layout triggers (design §5.3.2) ---------------------------
#
# The gate trusts a report only until the observer announces a layout change,
# so a change the observer never announces leaves a stale report marked fresh:
# a secret painted out at its old position, or not reported at all, until the
# heartbeat. There is no browser here, so these hold the trigger list in the
# source: removing a trigger fails a test instead of silently opening a gap.


def _js_string_list(source: str, name: str) -> set[str]:
    import re

    match = re.search(r"const %s = \[(.*?)\];" % re.escape(name), source, re.S)
    assert match, f"observer.js no longer declares {name}"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_the_observer_hears_loads_that_never_reach_window():
    # An <img> or <iframe> finishing loading after the page did: the spec's
    # Document "get the parent" is null for `load`, so a capture listener on
    # `window` never sees it, and the image reflows the text below it.
    source = observer_source()
    assert {"load", "error"} <= _js_string_list(source, "DOCUMENT_TRIGGERS")
    assert "document.addEventListener(type, layout, capture)" in source
    # A load inside a shadow root does not leave the root either.
    assert "root.addEventListener(type, layout, capture)" in source


def test_the_observer_watches_sizes_with_a_resize_observer():
    source = observer_source()
    assert "new ResizeObserver(" in source
    assert "resizes.observe(" in source


def test_the_observer_watches_mutations_inside_open_shadow_roots():
    # A MutationObserver on `document` is not told about mutations inside a
    # shadow root, though measure() reports the text inside it.
    source = observer_source()
    assert "mutations.observe(root, MUTATIONS)" in source
    assert "watchRoot(el.shadowRoot)" in source


def test_the_observer_hears_css_only_changes_from_pointer_and_keyboard():
    # :hover, :active and :focus-visible reveal or move content with no
    # mutation; the event that causes them is the only announcement.
    triggers = _js_string_list(observer_source(), "WINDOW_TRIGGERS")
    assert {"pointerover", "pointerout", "pointerdown", "pointerup", "keydown", "keyup"} <= triggers
    assert {"scroll", "resize", "input", "change", "load", "pageshow", "transitionend",
            "animationend", "focusin", "focusout"} <= triggers


def test_the_observer_hears_cancelled_transitions_and_animations():
    # A cancelled run fires neither transitionend nor animationend, and the
    # text snaps back to where it was.
    triggers = _js_string_list(observer_source(), "WINDOW_TRIGGERS")
    assert {"transitioncancel", "animationcancel"} <= triggers


def test_the_observer_announces_layout_every_frame_while_animations_run():
    # A transition or animation moves text for its whole run; the run's start
    # event is announced once. A transform fires no ResizeObserver, so the
    # per-frame watch must announce while anything is running.
    source = observer_source()
    assert "document.getAnimations()" in source
    assert 'a.playState === "running"' in source
    assert 'typeof document.getAnimations !== "function"' in source
    start = source.index("function watch()")
    body = source[start:source.index("\n  }\n", start)]
    assert "if (animating()) layout();" in body
    assert "requestAnimationFrame(watch)" in body
