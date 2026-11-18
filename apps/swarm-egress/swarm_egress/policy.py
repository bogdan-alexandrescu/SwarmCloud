"""Which CONNECT targets swarm-egress carries to the vendor (docs/browser-egress.md §2.3, §3.2).

Three refusals, each with its own cause for the log:

  * `bypass` -- a host on the bypass list. Those go DIRECT, through
    swarm-nat, as they do today: SwarmCloud's own console and API, GitHub
    (whose sign-in should come from the address the agent's git uses), and
    Chromium's background services once phase 0 has measured them. The gate
    gives Chromium the same list as `--proxy-bypass-list`
    (`chromium_bypass_list`), so a CONNECT for one of them arriving here
    means the gate forgot the flag; refusing it fails closed instead of
    paying per GB for our own UI and showing the vendor our hostnames.
  * `internal` -- a single-label name (Chromium's `<local>`), a
    cluster-internal suffix, or an address that is not a public unicast
    one. The vendor must never see an internal hostname, and a private
    address through a residential peer is at best nonsense.
  * `port` -- anything but 443. The browser is HTTPS-only in a proxied pod
    (`HttpsOnlyMode: force_enabled`, §2.3), and a plain-http page through a
    residential peer is readable by the peer (§3.2).

A target that does not parse is `target`. Nothing here returns or raises the
host in a message: the caller logs the cause, never the target.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Iterable

#: Suffixes that only ever name something inside a cluster, a VPC or a LAN.
INTERNAL_SUFFIXES = (
    ".local", ".localhost", ".internal", ".cluster.local", ".svc", ".localdomain",
    ".home.arpa", ".intranet", ".lan",
)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
MAX_HOST_CHARS = 253


class TargetRefused(Exception):
    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


def _split(target: str) -> tuple[str, int]:
    if not isinstance(target, str) or not target or len(target) > MAX_HOST_CHARS + 8:
        raise TargetRefused("target")
    if target.startswith("["):
        host, sep, port = target[1:].partition("]:")
        if not sep:
            raise TargetRefused("target")
    else:
        host, sep, port = target.rpartition(":")
        if not sep:
            raise TargetRefused("target")
    if not host or not port.isdigit() or len(port) > 5:
        raise TargetRefused("target")
    number = int(port)
    if not 0 < number < 65536:
        raise TargetRefused("target")
    return host.lower(), number


def _bypassed(host: str, bypass_hosts: Iterable[str]) -> bool:
    for entry in bypass_hosts:
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry.startswith("*."):
            entry = entry[1:]
        if entry.startswith("."):
            if host.endswith(entry) or host == entry[1:]:
                return True
        elif host == entry:
            return True
    return False


def check_target(target: str, *, bypass_hosts: Iterable[str],
                 allowed_ports: Iterable[int]) -> tuple[str, int]:
    """`(host, port)` when swarm-egress may carry `target`; TargetRefused otherwise."""
    host, port = _split(target)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global or address.is_multicast:
            raise TargetRefused("internal")
    else:
        if len(host) > MAX_HOST_CHARS:
            raise TargetRefused("target")
        labels = host.rstrip(".").split(".")
        if not all(_LABEL.fullmatch(label) for label in labels):
            raise TargetRefused("target")
        host = ".".join(labels)
        if len(labels) < 2 or host == "localhost" or any(
                ("." + host).endswith(suffix) for suffix in INTERNAL_SUFFIXES):
            raise TargetRefused("internal")
        if _bypassed(host, bypass_hosts):
            raise TargetRefused("bypass")
    if port not in set(allowed_ports):
        raise TargetRefused("port")
    return host, port


def chromium_bypass_list(bypass_hosts: Iterable[str]) -> str:
    """The `--proxy-bypass-list` value for the same list: `<local>` first,
    then each entry, a leading-dot suffix written as Chromium's `*.` form."""
    out = ["<local>"]
    for entry in bypass_hosts:
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry.startswith("."):
            entry = "*" + entry
        out.append(entry)
    return ";".join(out)


__all__ = ["INTERNAL_SUFFIXES", "TargetRefused", "check_target", "chromium_bypass_list"]
