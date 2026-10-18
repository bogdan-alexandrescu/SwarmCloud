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
Beside it, `path`: the top frame's path, for the caption under the device
("<device> · <W>×<H> · <path>", design §6 item 11). Until LB-R6 the report
carried no path, and every live session's caption read "path not reported"
(re-QA pass 8, 2026-10-12); the owner's call was that a live session reports
its path. The path is redacted with `swarm_redaction` before it leaves the
gate (a reset link's token is a path segment), and cut at `MAX_PATH_CHARS`.
Never the query, the fragment or a user name or password in the URL: those
carry state and secrets far more often than a path does, and the report
crosses two services. Whoever watches the live view already sees the page,
so the path tells them nothing the pixels do not. A page
with no web origin (about:blank, chrome://, data:, file:, an error page) is
reported as null. `cert_error` is null -- unknown -- because the page the
console shows is followed by the mask's session, which does not watch the
security state; the gate still refuses control on a page that is not secure
(cdpproxy.CdpProxy.take_control), so null never lets one through.

The device is the preset's NAME, its orientation and whether it is touch:
invariant 10's spirit, as presets.py keeps it -- a name, never a size.
`touch` is what the person in control gets: the preset's touch AND their
touch toggle (touch.py `TouchInjector.set_touch`, LB-DEV-B), so a phone with
touch turned off reports false and a desktop never reports true.

WHETHER THE AGENT IS USING THIS BROWSER (LIVE-UNUSED; part of #1030), as
`agent`: `connected_at`, the epoch seconds the CDP proxy accepted the agent's
first connection (null until it has); `connections`, how many it holds now;
and `first_frame_at`, when the gate first published a frame (null until it
has), which the console times its "the agent isn't using this browser yet"
from. Numbers only, read off the proxy and the capture, never off the page.
The key is left out when the caller gives no `agent`, so a report reads as it
did before for anything that does not send one.

WHICH BROWSER TOOL, AND THE EGRESS (BSTATUS-API; part of #1030), as
`browser`: what the runner registered for the task's `browser_tool`
(`tool_selected`, `tools_available`, `jev`: forwarded by the worker from the
hand-off spool, null until it has been), the tool whose tagged CDP
connection sent the last command and when (`tool_last_used`, cdpproxy
`split_client`), and the browser's egress: `mode` residential or direct,
`state` ok, failing or off, and `last_error`, one of egress.py's FIXED
sentences for the forwarder's cause word -- never a vendor's answer, which
the forwarder never reads past its status and a cause word. The exit country
is not the gate's to know; swarm-api adds it from the same setting that
mints the ticket. Left out when the caller gives no `browser`.

THE PAGE IS UNTRUSTED. Every field is length-limited here, and the relay and
swarm-api check the report again; nothing in it is logged.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlsplit

from swarm_redaction import redact

from .presets import Device

#: The largest report the relay keeps (swarm_live.rendezvous.MAX_REPORT_BYTES;
#: test_livegate_main.py holds the two equal). The largest legal origin -- a
#: 253-character host, a port -- and a device fit in well under half of it.
MAX_REPORT_BYTES = 4096

#: A URL longer than this is not read at all.
MAX_URL_CHARS = 8192
#: RFC 1035's limit on a host name.
MAX_HOST_CHARS = 253
#: The longest path reported (swarm_api.live.MAX_PATH_CHARS, held equal by
#: test_live_session_path.py's limits): a caption, not a URL to follow. A
#: longer one is cut and ends in "…", so it never reads as the whole path.
MAX_PATH_CHARS = 256

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
        "path": page_path(parts.path),
    }


def page_path(path: str) -> str:
    """The caption's path: `path` (the root when empty), redacted, at most `MAX_PATH_CHARS`."""
    shown = redact(path or "/").text
    if len(shown) > MAX_PATH_CHARS:
        shown = shown[:MAX_PATH_CHARS - 1] + "\u2026"
    return shown


def device_report(device: Device, touch_on: bool = True) -> dict[str, Any]:
    """The console's `LiveDevice`: the preset by name, its orientation, and
    touch -- the preset's, while the person's toggle (`touch_on`) is on."""
    return {"preset": device.name, "orientation": device.orientation,
            "touch": bool(device.touch and touch_on)}


def agent_report(*, connected_at: float | None, connections: int,
                 first_frame_at: float | None) -> dict[str, Any]:
    """The report's `agent`: whether the agent is driving this browser (LIVE-UNUSED)."""
    return {"connected_at": connected_at, "connections": max(0, int(connections)),
            "first_frame_at": first_frame_at}


#: The longest `last_error` reported: egress.py's sentences are under 100.
MAX_EGRESS_ERROR_CHARS = 200


def egress_report(*, proxied: bool, ok: bool, error: str | None) -> dict[str, Any]:
    """The report's `browser.egress`: `off` when the browser goes direct."""
    if not proxied:
        return {"mode": "direct", "state": "off", "last_error": None}
    text = error[:MAX_EGRESS_ERROR_CHARS] if isinstance(error, str) and error else None
    return {"mode": "residential", "state": "ok" if ok else "failing",
            "last_error": None if ok else text}


def browser_report(*, tools: dict[str, Any] | None, last_used: tuple[str, float] | None,
                   egress: dict[str, Any]) -> dict[str, Any]:
    """The report's `browser` (BSTATUS-API). `tools` is
    `browser_tools.Choice.state()` as the worker forwarded it, or None."""
    return {
        "tools": tools,
        "tool_last_used": None if last_used is None else {"name": last_used[0], "at": last_used[1]},
        "egress": egress,
    }


def encode_report(origin: dict[str, Any] | None, device: dict[str, Any] | None,
                  agent: dict[str, Any] | None = None,
                  browser: dict[str, Any] | None = None) -> bytes:
    """One `OP_REPORT` payload: the latest of all of them, never a delta."""
    message: dict[str, Any] = {"origin": origin, "device": device}
    if agent is not None:
        message["agent"] = agent
    if browser is not None:
        message["browser"] = browser
    raw = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
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
