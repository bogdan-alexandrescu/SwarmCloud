"""The device presets the live browser can be (live browser L3b, LB-6).

docs/design/live-browser.md section 6.1. The browser can run as a phone or a
tablet, not only a desktop, and the list of what it can be is FIXED AND NAMED,
here. A caller -- the agent through `swarm-handoff device`, or the person in
control through the console -- names a preset and an orientation; it never
sends a width, a height, a scale factor or a user agent. That keeps invariant
10's spirit on this path: a choice by name, never a parameter. A name outside
the list is refused with `UnknownPreset` before anything is changed.

WHAT A PRESET FIXES, and where each value goes (device.py applies them):

* the CSS viewport (`width` x `height` in the preset's own orientation), the
  device scale factor and `mobile` -> `Emulation.setDeviceMetricsOverride`;
* the user agent, `navigator.platform` and the client hints ->
  `Emulation.setUserAgentOverride`. `desktop` keeps Chromium's own user agent
  (read from `Browser.getVersion` at the time it is applied), so the desktop
  preset is the browser as it is;
* touch -> `Emulation.setTouchEmulationEnabled`, and whether the console's
  pointer becomes touch (touch.py);
* the display: `Device.display_width` x `display_height` screen pixels.

THE DISPLAY IS THE PAGE AT ITS DEVICE SCALE, PLUS THE BROWSER'S OWN UI.
`livegate.mask` maps a page's CSS rectangles to the screen by multiplying by
the device scale factor (`MaskState.set_scale`) and anchors the page at the
bottom-left of the frame, with the address bar in the band above it. So the
page area is `ceil(css * scale)` screen pixels each way, and the display is
that plus `BROWSER_UI_HEIGHT` rows for the tab strip and the address bar, which
stay visible because they are part of the anti-phishing design (section 5.7).
A display sized like this carries no letterbox.

`BROWSER_UI_HEIGHT` IS A STARTING VALUE, NOT A MEASUREMENT: the height of
Chromium's tab strip and toolbar at scale 1 on Linux with the default theme.
If it is too small the page is clipped at the bottom by the difference; too
large and a strip of desktop shows above the tab strip. Neither leaks, because
the mask places the page from the bottom edge and blanks what it cannot place.

The user agents are the strings each device's own browser sends (Safari on
iOS and iPadOS, Chrome on Android in its reduced form, where the model is in
the client hints, not the string). `{major}` and `{full}` are filled from the
running Chromium's version, so the Android preset never claims a Chrome that
is not the one answering.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

PORTRAIT = "portrait"
LANDSCAPE = "landscape"
ORIENTATIONS = (PORTRAIT, LANDSCAPE)

#: The tab strip and toolbar above the page, in screen pixels. A starting
#: value (see the module docstring), not a measurement.
BROWSER_UI_HEIGHT = 88

#: How many simultaneous touch points a touch preset reports
#: (`navigator.maxTouchPoints`); what current phones and tablets report.
MAX_TOUCH_POINTS = 5

_SAFARI_IOS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
# iPadOS Safari presents itself as desktop Safari on a Mac, by default.
_SAFARI_IPADOS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
# Chrome's reduced user agent: the Android version and model are frozen in the
# string and carried by the client hints instead.
_CHROME_ANDROID = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Mobile Safari/537.36"
)


class UnknownPreset(ValueError):
    """A device name or orientation that is not on the list. Nothing was changed."""


@dataclass(frozen=True)
class Preset:
    """One named device, in its natural orientation."""

    name: str
    width: int
    height: int
    scale: float
    mobile: bool
    touch: bool
    #: None: Chromium's own user agent (the desktop preset).
    user_agent: str | None
    platform: str
    #: The client hints (`userAgentMetadata`) without brands, which device.py
    #: fills from the running Chromium.
    hints_platform: str
    hints_platform_version: str
    hints_architecture: str
    hints_model: str
    natural: str = PORTRAIT


PRESETS: Mapping[str, Preset] = {
    p.name: p
    for p in (
        Preset("desktop", 1440, 900, 1.0, False, False, None, "Linux x86_64",
               "Linux", "", "x86", "", natural=LANDSCAPE),
        Preset("iphone-15", 393, 852, 3.0, True, True, _SAFARI_IOS, "iPhone",
               "iOS", "17.0", "arm", "iPhone"),
        Preset("pixel-8", 412, 915, 2.625, True, True, _CHROME_ANDROID, "Linux armv81",
               "Android", "14.0.0", "arm", "Pixel 8"),
        Preset("iphone-se", 375, 667, 2.0, True, True, _SAFARI_IOS, "iPhone",
               "iOS", "17.0", "arm", "iPhone"),
        Preset("ipad-7", 810, 1080, 2.0, True, True, _SAFARI_IPADOS, "MacIntel",
               "iOS", "17.0", "arm", "iPad"),
    )
}

DEFAULT_PRESET = "desktop"


@dataclass(frozen=True)
class Device:
    """A preset in one orientation, with every size device.py needs."""

    preset: Preset
    orientation: str
    #: The CSS viewport, in this orientation.
    width: int
    height: int

    @property
    def name(self) -> str:
        return self.preset.name

    @property
    def scale(self) -> float:
        return self.preset.scale

    @property
    def mobile(self) -> bool:
        return self.preset.mobile

    @property
    def touch(self) -> bool:
        return self.preset.touch

    @property
    def page_width(self) -> int:
        """The page area's width in screen pixels."""
        return math.ceil(self.width * self.scale)

    @property
    def page_height(self) -> int:
        return math.ceil(self.height * self.scale)

    @property
    def display_width(self) -> int:
        return self.page_width

    @property
    def display_height(self) -> int:
        return self.page_height + BROWSER_UI_HEIGHT

    @property
    def page_top(self) -> int:
        """The screen row the page starts on: the page is anchored at the bottom, as the mask places it."""
        return self.display_height - self.page_height

    def screen_orientation(self) -> dict[str, Any]:
        """`Emulation.ScreenOrientation` for this orientation."""
        if self.orientation == self.preset.natural:
            kind = "portraitPrimary" if self.orientation == PORTRAIT else "landscapePrimary"
            return {"type": kind, "angle": 0}
        kind = "landscapePrimary" if self.orientation == LANDSCAPE else "portraitPrimary"
        return {"type": kind, "angle": 90}

    def user_agent(self, browser_user_agent: str, browser_version: str) -> str:
        """The user agent string, with the running Chromium's version where it carries one."""
        if self.preset.user_agent is None:
            return browser_user_agent
        major = browser_version.split(".", 1)[0]
        return self.preset.user_agent.format(major=major, full=browser_version)

    def user_agent_metadata(self, browser_version: str) -> dict[str, Any]:
        """`Emulation.UserAgentMetadata`: the client hints, matching the string."""
        major = browser_version.split(".", 1)[0]
        return {
            "brands": [{"brand": "Chromium", "version": major}],
            "fullVersionList": [{"brand": "Chromium", "version": browser_version}],
            "platform": self.preset.hints_platform,
            "platformVersion": self.preset.hints_platform_version,
            "architecture": self.preset.hints_architecture,
            "model": self.preset.hints_model,
            "mobile": self.mobile,
        }


def resolve(name: Any, orientation: Any = None) -> Device:
    """The device `name` in `orientation` (its natural one when None), or `UnknownPreset`."""
    if not isinstance(name, str) or name not in PRESETS:
        raise UnknownPreset("not a device preset")
    preset = PRESETS[name]
    if orientation is None:
        orientation = preset.natural
    if orientation not in ORIENTATIONS:
        raise UnknownPreset("not an orientation")
    natural = (preset.width, preset.height)
    width, height = natural if orientation == preset.natural else natural[::-1]
    return Device(preset, orientation, width, height)


def all_devices() -> list[Device]:
    """Every preset in both orientations."""
    return [resolve(name, o) for name in PRESETS for o in ORIENTATIONS]


#: The largest display any device needs. Xvfb is started at least this large
#: (RandR can shrink a screen below its starting size, not grow it past it).
MAX_DISPLAY_WIDTH = max(d.display_width for d in all_devices())
MAX_DISPLAY_HEIGHT = max(d.display_height for d in all_devices())
