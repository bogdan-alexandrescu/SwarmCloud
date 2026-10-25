"""The live gate's page watch and capture: no painting, nothing read is logged (LIVE-NOMASK; part of #2298).

docs/design/live-browser.md §5.3. Until 2026-10-25 this file held the mask
(LB-L1): boxes over password fields, credential autocomplete kinds, text a
`swarm_redaction` rule matched and the address bar, and the fail-closed
blanks. BGD decided on 2026-10-25 that the live view shows the screen as
drawn, in every situation, so those tests went with the painting
(test_live_nomask.py holds the frame end to end). What is held here is what
mask.py and capture.py still do:

* `MaskCdp.install` runs no script in the page: no isolated world, no
  binding, no `Runtime` domain -- it reads the frame tree and navigations;
* the frames it hears of are tracked through navigations, a swap into a
  renderer of its own, and detaches;
* nothing read from a page -- a URL with a token in it -- reaches the log
  (§5.3.3);
* the capture reads Xvfb's framebuffer file and returns a copy.

Each case builds its frames by hand, so it runs with no browser and no
display. Every credential-shaped value is assembled at run time (CLAUDE.md,
"Nothing you add may look like a credential").
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import struct

import pytest

from agent_worker.livegate.capture import CaptureError, Frame, XvfbFramebuffer, parse_xwd
from agent_worker.livegate.mask import MaskCdp, MaskState

WHITE = (0xFF, 0xFF, 0xFF)
PAINT = (0x22, 0x22, 0x22)
TOP = "frame-top"
CHILD = "frame-child"


# --------------------------------------------------------------------------
# Fixtures: an XWD framebuffer file like Xvfb -fbdir writes
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


def _fake_github_token() -> str:
    # A `github_token` rule match, assembled so no literal here is one.
    return "gh" + "p_" + secrets.token_hex(18)


class _FakeCdp:
    def __init__(self, replies: dict[str, dict] | None = None):
        self.sent: list[tuple[str, dict]] = []
        self.replies = replies or {}

    async def __call__(self, method: str, params: dict) -> dict:
        self.sent.append((method, params))
        return self.replies.get(method, {})


# --------------------------------------------------------------------------
# The gate's own CDP session: frames and navigations, no script
# --------------------------------------------------------------------------


def test_install_runs_no_script_in_the_page_and_learns_the_frame_tree():
    cdp = _FakeCdp({
        "Page.getFrameTree": {"frameTree": {
            "frame": {"id": TOP, "url": "https://example.test/"},
            "childFrames": [{"frame": {"id": CHILD, "parentId": TOP, "url": "about:blank"}}],
        }},
    })
    state = MaskState()
    asyncio.run(MaskCdp(state, cdp).install())

    methods = [m for m, _ in cdp.sent]
    assert methods == ["Page.enable", "Target.setAutoAttach", "Page.getFrameTree"]
    assert dict(cdp.sent)["Target.setAutoAttach"] == {"autoAttach": True, "waitForDebuggerOnStart": False,
                                                      "flatten": True}
    assert state.has_frame(TOP) and state.has_frame(CHILD)


def test_a_detached_frame_is_forgotten_with_its_children_and_a_swapped_one_keeps_its_place():
    state = MaskState()
    mask = MaskCdp(state, _FakeCdp())

    async def run():
        await mask.on_event("Page.frameNavigated", {"frame": {"id": TOP, "url": "https://example.test/"}})
        await mask.on_event("Page.frameAttached", {"frameId": CHILD, "parentFrameId": TOP})
        await mask.on_event("Page.frameAttached", {"frameId": "grandchild", "parentFrameId": CHILD})
        # Into a renderer of its own: still the page's frame, heard on a child session.
        await mask.on_event("Page.frameDetached", {"frameId": CHILD, "reason": "swap"})
        assert state.has_frame(CHILD)
        await mask.on_event("Page.frameDetached", {"frameId": CHILD, "reason": "remove"})

    asyncio.run(run())
    assert state.has_frame(TOP)
    assert not state.has_frame(CHILD) and not state.has_frame("grandchild")


def test_a_child_session_never_names_a_top_frame():
    state = MaskState()
    child = MaskCdp(state, _FakeCdp(), child=True)
    asyncio.run(child.on_event("Page.frameNavigated", {"frame": {"id": "stray", "url": "https://x.test/"}}))
    assert not state.has_frame("stray")


def test_neither_page_urls_nor_tokens_in_them_reach_the_log(caplog):
    token = _fake_github_token()
    caplog.set_level(logging.DEBUG)

    def hook(host: str) -> None:
        raise RuntimeError("the hook failed with " + token)

    state = MaskState(on_challenge=hook)
    mask = MaskCdp(state, _FakeCdp())

    async def run():
        await mask.on_event("Page.frameNavigated", {"frame": {"id": TOP, "url": "https://example.test/?k=" + token}})
        await mask.on_event("Page.navigatedWithinDocument", {"frameId": TOP, "url": "https://example.test/#" + token})
        await mask.on_event("Page.frameAttached", {"frameId": CHILD, "parentFrameId": TOP})
        await mask.on_event("Page.frameNavigated", {"frame": {
            "id": CHILD, "parentId": TOP, "url": "https://www.google.com/recaptcha/api2/anchor?k=" + token}})

    asyncio.run(run())
    assert "challenge hook failed" in caplog.text  # the hook ran, and only its class was logged
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
        frame.fill(0, 0, 8, 4, PAINT)
        # Writing to the copy (a viewer's pointer is painted on one) never
        # touches the display's own memory.
        assert fb.grab().rgb_at(3, 2) == (10, 20, 30)
    assert (frame.width, frame.height) == (8, 4)
    assert frame.rgb_at(3, 2) == PAINT


def test_capture_honours_msb_first_pixels():
    frame = parse_xwd(_xwd(4, 2, (1, 2, 3), byte_order=1))
    assert frame.rgb_at(0, 0) == (1, 2, 3)
    frame.fill(0, 0, 1, 1, (9, 8, 7))
    assert frame.rgb_at(0, 0) == (9, 8, 7)
    assert frame.rgb_at(1, 0) == (1, 2, 3)


@pytest.mark.parametrize("kwargs", [{"depth_bpp": 16}, {"pixmap_format": 1}])
def test_capture_refuses_a_format_it_cannot_read(kwargs):
    with pytest.raises(CaptureError):
        parse_xwd(_xwd(4, 2, (1, 2, 3), **kwargs))


def test_capture_refuses_a_truncated_file():
    with pytest.raises(CaptureError):
        parse_xwd(_xwd(4, 2, (1, 2, 3))[:-5])


def test_fill_is_clipped_to_the_frame():
    frame = Frame.blank(4, 4, WHITE)
    frame.fill(-10, -10, 2, 2, PAINT)
    frame.fill(3, 3, 100, 100, PAINT)
    assert frame.rgb_at(0, 0) == PAINT and frame.rgb_at(1, 1) == PAINT
    assert frame.rgb_at(2, 2) == WHITE
    assert frame.rgb_at(3, 3) == PAINT
