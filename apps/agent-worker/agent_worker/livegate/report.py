"""What the gate tells the console about the page on screen and the device (LB-DEV-A, part of #1030).

docs/design/live-browser.md section 5.7 (the origin strip) and 6.1 (the
device). The console draws the top frame's origin OUTSIDE the canvas, so a
page cannot draw it, and the device the browser is. Only the gate reads either
from Chromium, so the gate reports both, over the tunnel it already holds to
the relay (`tunnel.OP_REPORT`): the relay keeps the latest per session and
hands it to every viewer link, and swarm-api -- which proxies each of those
links -- checks it again and records it on `live_sessions/{id}`.

WHAT IS REPORTED, AND WHAT IS NOT. The origin is the scheme, host and port of
the URL Chromium committed in the top frame, and the registrable domain
Chromium itself computed from the public suffix list
(`Page.Frame.domainAndRegistry`); nothing here keeps a suffix list of its own.
Never the path, the query, the fragment or a user name or password in the
URL: the console shows a site, and the report crosses two services. A page
with no web origin (about:blank, chrome://, data:, file:, an error page) is
reported as null. `cert_error` is null -- unknown -- because the page the
console shows is followed by the mask's session, which does not watch the
security state; the gate still refuses control on a page that is not secure
(cdpproxy.CdpProxy.take_control), so null never lets one through.

The device is the preset's NAME, its orientation and whether it is touch:
invariant 10's spirit, as presets.py keeps it -- a name, never a size.

THE PAGE IS UNTRUSTED. Every field is length-limited here, and the relay and
swarm-api check the report again; nothing in it is logged.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlsplit

from .presets import Device

#: The largest report the relay keeps (swarm_live.rendezvous.MAX_REPORT_BYTES;
#: test_livegate_main.py holds the two equal). The largest legal origin -- a
#: 253-character host, a port -- and a device fit in well under half of it.
MAX_REPORT_BYTES = 4096

#: A URL longer than this is not read at all.
MAX_URL_CHARS = 8192
#: RFC 1035's limit on a host name.
MAX_HOST_CHARS = 253

_SCHEMES = {"http": 80, "https": 443}
_HOST = re.compile(r"[a-z0-9_]([a-z0-9_-]{0,62})(\.[a-z0-9_]([a-z0-9_-]{0,62}))*\.?")


def page_origin(url: Any, registrable: Any = None) -> dict[str, Any] | None:
    """The console's `LiveOrigin` for a committed top-frame `url`, or None when it has no web origin.

    `registrable` is Chromium's `domainAndRegistry` for the frame; when it is
    empty or is not the host's own suffix (a host the suffix list does not
    know, such as `localhost`), the host stands in for it.
    """
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if scheme not in _SCHEMES or not host or len(host) > MAX_HOST_CHARS:
        return None
    ip_literal = _is_ip(host)
    if not ip_literal and not _HOST.fullmatch(host):
        return None
    shown = f"[{host}]" if ip_literal and ":" in host else host
    origin = f"{scheme}://{shown}" + (f":{port}" if port is not None and port != _SCHEMES[scheme] else "")
    if ip_literal:
        domain = ""
    else:
        domain = registrable.lower().rstrip(".") if isinstance(registrable, str) else ""
        if not domain or not (host == domain or host.endswith("." + domain)):
            domain = host
    return {
        "origin": origin,
        "host": shown,
        "display_host": shown if ip_literal else _decoded(host),
        "registrable_domain": domain,
        "scheme": scheme + ":",
        "ip_literal": ip_literal,
        "cert_error": None,
    }


def device_report(device: Device) -> dict[str, Any]:
    """The console's `LiveDevice`: the preset by name, its orientation, and touch."""
    return {"preset": device.name, "orientation": device.orientation, "touch": device.touch}


def encode_report(origin: dict[str, Any] | None, device: dict[str, Any] | None) -> bytes:
    """One `OP_REPORT` payload: the latest of both, never a delta."""
    raw = json.dumps({"origin": origin, "device": device}, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("report too large")
    return raw


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _decoded(host: str) -> str:
    """`host` with each punycode label decoded; a label that does not decode is kept as it is."""
    labels = []
    for label in host.split("."):
        if label.startswith("xn--"):
            try:
                label = label.encode("ascii").decode("idna")
            except UnicodeError:
                pass
        labels.append(label)
    return ".".join(labels)
