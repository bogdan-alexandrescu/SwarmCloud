"""The challenge allowlist decides the CAPTCHA hand-off, and nothing else any more (issue #1340, LIVE-NOMASK).

docs/design/live-browser.md §4.1 and §5.3. Issue #1340 (MEASURED 2026-10-15 on
task_2ac5d7e22a51460995a2) made a child frame on an exact allowlist of
challenge hosts (`SHOWN_IN_CONTROL`) visible through a hand-off while the mask
covered every frame it could not check. BGD decided on 2026-10-25 that the
live view paints nothing, so every frame is visible and the allowlist's only
job is the one CAPTCHA-HANDOFF (#2058) gave it: a frame on it raises the
hand-off, once per page load. Its matching is what this file holds -- exact
hosts, "*." subdomains only, https, no port, no userinfo, the path prefix --
so a lookalike never raises a hand-off and a real challenge always does.

Every case builds its frames by hand: no browser, no display.
"""

from __future__ import annotations

import pytest

from agent_worker.livegate.mask import MaskState, challenge_host

TOP = "frame-top"
CHILD = "frame-captcha"
# A site key the hand-off must never carry; fixed, so xdist workers collect the same ids.
SITE_KEY = "k=site-key-under-test"
RECAPTCHA = f"https://www.google.com/recaptcha/api2/anchor?{SITE_KEY}"
PAGE = "https://shop.example.test/cart"


def _page_with_child(url: str) -> list[str]:
    told: list[str] = []
    state = MaskState(on_challenge=told.append)
    state.frame_attached(TOP, None)
    state.navigated(TOP, PAGE)
    state.frame_attached(CHILD, TOP)
    state.navigated(CHILD, url)
    return told


@pytest.mark.parametrize("url", [
    RECAPTCHA,
    "https://www.recaptcha.net/recaptcha/api2/bframe?" + SITE_KEY,
    "https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=challenge",
    "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv0",
])
def test_a_challenge_frame_raises_the_hand_off_for_the_pages_host_only(url):
    told = _page_with_child(url)
    assert told == ["shop.example.test"]
    assert challenge_host(url) == url.split("/")[2]
    # Host only: never the path, the query or anything the page drew.
    assert SITE_KEY not in " ".join(told)


@pytest.mark.parametrize("url", [
    "https://recaptcha.evil.com/recaptcha/api2/anchor",
    "https://www.google.com.evil.com/recaptcha/api2/anchor",
    "https://www.google.com/recaptchaX/api2/anchor",
    "https://www.google.com/search?q=recaptcha",
    "https://www.google.com/x/recaptcha/",
    "https://evil.com/recaptcha/?www.google.com/recaptcha/",
    "https://www.google.com@evil.com/recaptcha/",
    "https://hcaptcha.com.evil.com/",
    "https://evilhcaptcha.com/",
    "https://challenges.cloudflare.com.evil.com/",
    "http://www.google.com/recaptcha/api2/anchor",
    "https://www.google.com:8443/recaptcha/api2/anchor",
    "about:blank",
    "",
])
def test_a_lookalike_host_or_another_path_raises_nothing(url):
    assert _page_with_child(url) == []
    assert challenge_host(url) is None


def test_a_challenge_frame_raises_once_per_page_load():
    told: list[str] = []
    state = MaskState(on_challenge=told.append)
    state.frame_attached(TOP, None)
    state.navigated(TOP, PAGE)
    state.frame_attached(CHILD, TOP)
    state.navigated(CHILD, RECAPTCHA)
    state.url_changed(CHILD, RECAPTCHA + "&x=1")
    state.frame_attached("bframe", TOP)
    state.navigated("bframe", RECAPTCHA.replace("anchor", "bframe"))
    assert told == ["shop.example.test"]
    # A new load of the page may raise again.
    state.navigated(TOP, PAGE + "?step=2")
    state.frame_attached("again", TOP)
    state.navigated("again", RECAPTCHA)
    assert told == ["shop.example.test", "shop.example.test"]
