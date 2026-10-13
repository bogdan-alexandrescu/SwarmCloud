"""What the gate reports about the page on screen and the device (LB-DEV-A, part of #1030).

docs/design/live-browser.md section 5.7 (the origin strip) and 6.1 (the
device). The gate is the one component that reads the top frame from Chromium,
so it builds the origin it reports here: the scheme, host and port of the URL
Chromium committed and the registrable domain Chromium computed
(`Page.Frame.domainAndRegistry`), with the top frame's path beside it,
redacted, for the caption under the device (LB-R6) -- never the query, the
fragment or a user's credentials in the URL. A page that is not on the web (about:blank, chrome://,
data:, file:) has no origin to show and is reported as null.

Every URL below is built from parts; nothing is a credential.
"""

from __future__ import annotations

import json

import pytest

from agent_worker.livegate.presets import ORIENTATIONS, PRESETS, resolve
from agent_worker.livegate.report import MAX_REPORT_BYTES, device_report, encode_report, page_origin


def test_an_https_page_is_reported_as_its_origin_and_chromiums_registrable_domain():
    assert page_origin("https://accounts.github.com/login?return_to=%2F#top", "github.com") == {
        "origin": "https://accounts.github.com", "host": "accounts.github.com",
        "display_host": "accounts.github.com", "registrable_domain": "github.com",
        "scheme": "https:", "ip_literal": False, "cert_error": None, "path": "/login",
    }


def test_a_port_is_kept_and_a_default_port_is_not():
    assert page_origin("http://example.test:8080/x", "example.test")["origin"] == "http://example.test:8080"
    assert page_origin("https://example.test:443/x", "example.test")["origin"] == "https://example.test"
    assert page_origin("http://example.test/x", "example.test")["scheme"] == "http:"


def test_credentials_in_the_url_never_reach_the_report():
    secret = "pw" + "x" * 6
    reported = page_origin(f"https://someone:{secret}@github.com/", "github.com")
    assert reported["origin"] == "https://github.com" and reported["host"] == "github.com"
    assert secret not in json.dumps(reported)


@pytest.mark.parametrize(("url", "host", "origin"), [
    ("https://192.0.2.10/login", "192.0.2.10", "https://192.0.2.10"),
    ("https://[2001:db8::1]:8443/", "[2001:db8::1]", "https://[2001:db8::1]:8443"),
])
def test_an_ip_literal_is_flagged_and_has_no_registrable_domain(url, host, origin):
    reported = page_origin(url, "")
    assert (reported["host"], reported["origin"]) == (host, origin)
    assert reported["ip_literal"] is True and reported["registrable_domain"] == ""


def test_punycode_is_reported_as_chromium_sees_it_and_decoded_beside_it():
    reported = page_origin("https://www.xn--bcher-kva.example/", "xn--bcher-kva.example")
    assert reported["host"] == "www.xn--bcher-kva.example"
    assert reported["display_host"] == "www.bücher.example"
    assert reported["registrable_domain"] == "xn--bcher-kva.example"


@pytest.mark.parametrize("registrable", ["", None, "evil.test", "hub.com", 7])
def test_a_registrable_domain_that_is_not_the_hosts_own_suffix_falls_back_to_the_host(registrable):
    assert page_origin("https://github.com/", registrable)["registrable_domain"] == "github.com"


@pytest.mark.parametrize("url", [
    "about:blank", "chrome://settings/", "data:text/html,hi", "file:///etc/hosts",
    "javascript:alert(1)", "chrome-error://chromewebdata/", "", None, 42,
    "https://", "https://" + "a" * 250 + ".example/", "https://bad host.example/",
    "https://example.test:99999/",
])
def test_a_page_with_no_web_origin_is_reported_as_null(url):
    assert page_origin(url, "example") is None


@pytest.mark.parametrize("name", list(PRESETS))
@pytest.mark.parametrize("orientation", ORIENTATIONS)
def test_the_device_is_reported_by_name_orientation_and_touch_only(name, orientation):
    device = resolve(name, orientation)
    assert device_report(device) == {"preset": name, "orientation": orientation, "touch": device.touch}


def test_the_largest_report_fits_the_relays_limit():
    origin = page_origin("https://" + ".".join(["a" * 60] * 4) + ".example:65535/", "a.example")
    assert origin is not None
    raw = encode_report(origin, device_report(resolve("iphone-15", "landscape")))
    assert len(raw) <= MAX_REPORT_BYTES
    assert json.loads(raw)["origin"] == origin


def test_touch_is_reported_off_when_the_person_turned_it_off_and_never_on_a_desktop():
    """LB-DEV-B: the controller's touch toggle (`TouchInjector.set_touch`) is
    what the console's Touch button shows, so it is what the gate reports. A
    preset with no touch never reports touch, whatever the toggle says."""
    assert device_report(resolve("iphone-15"), touch_on=False)["touch"] is False
    assert device_report(resolve("iphone-15"), touch_on=True)["touch"] is True
    assert device_report(resolve("desktop"), touch_on=True)["touch"] is False
