"""Every frame the live gate captures is published as Chromium drew it (LIVE-NOMASK; part of #2298).

docs/design/live-browser.md §5.3. Owner decision, BGD 2026-10-25: "Stop
redacting elements or sections of the visual content that a browser agent or
session displays via the live browser. There is no need to redact parts of the
screen in any situation." So the gate paints nothing: no box over a password
field, a revealed password or text a redaction rule matches, none over the
address bar of a URL carrying a token, and none of the fail-closed blanks --
a stale report, no report since a navigation, the CDP session down, the
follower between tabs, a child frame that never reports.

Each case drives the real gate (`LiveGate.start_browser` and its capture loop)
against the scripted Chromium of test_live_device_recover.py, with a
framebuffer whose pixels are random bytes: whatever the page holds, the frame
the hub publishes must be the grab, byte for byte. The pointer is painted on
each viewer's own copy (pointer.py), never on the published frame, so the
published frame is what is compared. No Chromium, no Xvfb, no network.
"""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

from agent_worker.livegate import mask as mask_mod
from agent_worker.livegate import rfb
from agent_worker.livegate.capture import Frame
from agent_worker.livegate.mask import REASON_BROWSER_CLOSED, MaskState

import test_live_device_recover as recover

LEADS = ("Can't show this page safely", "Part of this page is covered")


def _token() -> str:
    # Built at runtime: nothing credential-shaped is a literal here.
    return "ghp_" + secrets.token_hex(18)


class RandomScreen:
    """Xvfb's framebuffer as random bytes: every grab the same screen, kept to compare against."""

    def __init__(self, width: int, height: int) -> None:
        self.frame = Frame(width, height, width * 4, bytearray(secrets.token_bytes(width * height * 4)))
        self.grabs = 0

    def grab(self) -> Frame:
        self.grabs += 1
        f = self.frame
        return Frame(f.width, f.height, f.stride, bytearray(f.pixels))

    def close(self) -> None: ...


def _run(tmp_path: Path, body: Callable[[recover.Rig, RandomScreen], Awaitable[None]],
         url: str = recover.URL) -> None:
    async def main() -> None:
        rig = recover.Rig(tmp_path)
        rig.chromium.pages["PAGE1"]["url"] = url
        try:
            await rig.start()
            gate = rig.gate
            device = gate.devices.device if gate.devices is not None else gate.desktop
            screen = RandomScreen(device.display_width, device.display_height)
            gate.framebuffer = screen
            gate._tasks["capture"] = asyncio.create_task(gate._capture())
            gate._watching.set()
            await recover._until(lambda: "first_frame" in gate.startup)
            await body(rig, screen)
        finally:
            await rig.stop()

    asyncio.run(asyncio.wait_for(main(), 20))


async def _next_publish(rig: recover.Rig, screen: RandomScreen) -> Any:
    """The hub's frame once a grab taken after this call has been published."""
    gate = rig.gate
    grabs = screen.grabs
    await recover._until(lambda: screen.grabs > grabs + 1)
    return gate.hub.current


def _as_captured(current: Any, screen: RandomScreen) -> None:
    frame = current.frame
    assert (frame.width, frame.height, frame.stride) == (screen.frame.width, screen.frame.height,
                                                         screen.frame.stride)
    # A boolean, not `a == b` in the assert: pytest would diff megabytes of pixels.
    same = bytes(frame.pixels) == bytes(screen.frame.pixels)
    assert same, "the published frame is not the capture"
    name = rfb.desktop_name(current)
    assert not name.decode("utf-8").startswith(LEADS), name


def test_a_page_with_a_password_field_rule_matching_text_and_a_token_in_its_url_is_published_as_captured(tmp_path):
    """The screen holds a password field, a revealed password and a token on the
    page, and the top frame's URL carries a token: the frame is the grab."""
    url = f"https://example.test/callback?access_token={_token()}"

    async def body(rig: recover.Rig, screen: RandomScreen) -> None:
        _as_captured(await _next_publish(rig, screen), screen)

    _run(tmp_path, body, url)


def _navigated(rig: recover.Rig) -> None:
    gate = rig.gate
    rig.chromium.emit("Page.frameNavigated", {"frame": {"id": "PAGE1", "url": recover.URL + "?next=1",
                                                        "domainAndRegistry": "example.test"}},
                      gate.pages._session)


def _stale(rig: recover.Rig) -> None:
    rig.chromium.emit("Page.navigatedWithinDocument", {"frameId": "PAGE1", "url": recover.URL + "#2"},
                      rig.gate.pages._session)


def _session_down(rig: recover.Rig) -> None:
    # A crashed renderer: the gate's session is down and every re-attach fails until a reload.
    rig.chromium.crash("PAGE1")


def _between_tabs(rig: recover.Rig) -> None:
    rig.gate.pages.tab_switching()


def _silent_child(rig: recover.Rig) -> None:
    session = rig.gate.pages._session
    rig.chromium.emit("Page.frameAttached", {"frameId": "AD1", "parentFrameId": "PAGE1"}, session)
    rig.chromium.emit("Page.frameNavigated", {"frame": {"id": "AD1", "parentId": "PAGE1",
                                                        "url": "https://ads.example.net/slot"}}, session)


@pytest.mark.parametrize("happen", [_stale, _navigated, _session_down, _between_tabs, _silent_child],
                         ids=["stale_report", "no_report_since_navigation", "cdp_session_down",
                              "between_tabs", "child_frame_never_reports"])
def test_what_used_to_fail_closed_publishes_the_capture_unchanged(tmp_path, happen):
    async def body(rig: recover.Rig, screen: RandomScreen) -> None:
        happen(rig)
        _as_captured(await _next_publish(rig, screen), screen)

    _run(tmp_path, body)


def test_no_desktop_name_says_the_page_cannot_be_shown_or_is_covered():
    hub = rfb.FrameHub(64, 48)
    assert not rfb.desktop_name(hub.current).decode().startswith(LEADS)
    frame = Frame.blank(64, 48, (1, 2, 3))
    state = MaskState()
    hub.publish(frame, reason=state.reason)
    assert rfb.desktop_name(hub.current) == b"swarm live browser"
    assert not hasattr(mask_mod, "CANNOT_SHOW") and not hasattr(mask_mod, "PARTLY_COVERED")
    assert not hasattr(rfb, "masked") and not hasattr(rfb, "MaskedFrame")


def test_the_browser_closed_name_is_unchanged_and_its_frame_is_the_capture():
    state = MaskState()
    state.browser_closed()
    hub = rfb.FrameHub(64, 48)
    frame = Frame(64, 48, 256, bytearray(secrets.token_bytes(64 * 48 * 4)))
    before = bytes(frame.pixels)
    hub.publish(frame, reason=state.reason)
    assert rfb.desktop_name(hub.current) == b"The agent has finished; its browser is closed."
    assert REASON_BROWSER_CLOSED == "The agent has finished; its browser is closed."
    same = bytes(hub.current.frame.pixels) == before
    assert same


def test_the_observer_and_the_painting_are_gone():
    livegate = Path(mask_mod.__file__).parent
    assert not (livegate / "observer.js").exists()
    for name in ("DILATE_PX", "HOLD_CHANGING_SECONDS", "MASK_RGB", "UNAVAILABLE_RGB", "Outcome",
                 "observer_source"):
        assert not hasattr(mask_mod, name), name
    assert not hasattr(MaskState, "apply")
